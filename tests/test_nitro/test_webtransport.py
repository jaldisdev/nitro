"""End-to-end WebTransport tests through the Python bindings.

A real server process serving HTTP/3, driven by a real WebTransport
client, so `WtSession`, `WtStream` and the `WebTransportSession` wrapper are
exercised over the wire.
"""

from __future__ import annotations

import asyncio

import pytest

# Imported as a plain module, not through a `tests` package: there is none, and
# reaching the repository root would shadow the installed nitro with the source
# tree, which carries no compiled extension.
from webtransport_client import SessionRefused, http3, webtransport

pytest.importorskip("aioquic", reason="a WebTransport client is needed")


SESSION_APP = """
    import asyncio

    from nitro import Nitro
    from nitro.protocols import HttpRequest, HttpResponse, PlainTextResponse
    from nitro.protocols.webtransport import WebTransportSession

    app = Nitro(
        http="3",
        log_level="warning",
        tls_cert="__CERTIFICATE__",
        tls_key="__KEY__",
        # A session handler holds its connection open, so the drain must be
        # short or shutting the test server down would wait for it.
        drain_timeout=2,
    )


    @app.route("/plain")
    async def plain(request: HttpRequest) -> HttpResponse:
        return PlainTextResponse("plain")


    @app.webtransport("/echo")
    async def echo(session: WebTransportSession) -> None:
        await session.accept()

        async def datagrams() -> None:
            async for payload in session.iter_datagrams():
                session.send_datagram(b"echo:" + payload)

        pump = asyncio.create_task(datagrams())
        try:
            async for stream in session.iter_streams():
                body = await stream.receive_all()
                await stream.send(b"echo:" + body)
                await stream.finish()
        finally:
            pump.cancel()


    @app.webtransport("/rooms/<slug:room>")
    async def room(session: WebTransportSession, room: str) -> None:
        await session.accept()
        session.send_datagram_json({"room": room, "path": session.path})
        await asyncio.sleep(20)


    @app.webtransport("/refuse")
    async def refuse(session: WebTransportSession) -> None:
        await session.reject(403)


    @app.webtransport("/greet")
    async def greet(session: WebTransportSession) -> None:
        await session.accept()
        outgoing = await session.open_outgoing()
        await outgoing.send_text("greetings")
        await outgoing.finish()
        await asyncio.sleep(20)


    @app.webtransport("/boom")
    async def boom(session: WebTransportSession) -> None:
        raise RuntimeError("handler exploded")
"""


@pytest.fixture
def certificate(tmp_path):
    """A self-signed certificate for the server under test."""
    cryptography = pytest.importorskip("cryptography")
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)

    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    certificate_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    assert cryptography is not None
    return str(certificate_path), str(key_path)


@pytest.fixture
def session_server(server_factory, certificate):
    certificate_path, key_path = certificate
    # Plain substitution rather than `format`, because the application source
    # contains braces of its own.
    source = SESSION_APP.replace("__CERTIFICATE__", certificate_path).replace(
        "__KEY__", key_path
    )
    return server_factory(source)


def run(coroutine, timeout: float = 30.0):
    return asyncio.run(asyncio.wait_for(coroutine, timeout))


class TestSessions:
    def test_a_session_is_accepted(self, session_server):
        async def exchange() -> int:
            async with webtransport("localhost", session_server.port, "/echo") as client:
                return client.session_id

        assert run(exchange()) is not None
        session_server.stop()

    def test_a_refused_session_reports_its_status(self, session_server):
        async def exchange() -> None:
            async with webtransport("localhost", session_server.port, "/refuse"):
                pass

        with pytest.raises(SessionRefused) as refusal:
            run(exchange())
        assert refusal.value.status == 403
        session_server.stop()

    def test_an_unknown_path_is_refused(self, session_server):
        async def exchange() -> None:
            async with webtransport("localhost", session_server.port, "/nowhere"):
                pass

        with pytest.raises(SessionRefused) as refusal:
            run(exchange())
        assert refusal.value.status == 404
        session_server.stop()

    def test_a_failing_handler_does_not_leave_the_client_waiting(self, session_server):
        async def exchange() -> None:
            async with webtransport("localhost", session_server.port, "/boom"):
                pass

        with pytest.raises(SessionRefused) as refusal:
            run(exchange())
        assert refusal.value.status == 500
        session_server.stop()


class TestDatagrams:
    def test_a_datagram_round_trips(self, session_server):
        async def exchange() -> bytes:
            async with webtransport("localhost", session_server.port, "/echo") as client:
                client.send_datagram(b"ping")
                return await client.receive_datagram()

        assert run(exchange()) == b"echo:ping"
        session_server.stop()

    def test_the_scope_carries_path_parameters(self, session_server):
        import json

        async def exchange() -> dict:
            async with webtransport(
                "localhost", session_server.port, "/rooms/lobby"
            ) as client:
                return json.loads(await client.receive_datagram())

        assert run(exchange()) == {"room": "lobby", "path": "/rooms/lobby"}
        session_server.stop()


class TestStreams:
    def test_a_bidirectional_stream_round_trips(self, session_server):
        async def exchange() -> bytes:
            async with webtransport("localhost", session_server.port, "/echo") as client:
                stream = client.open_stream()
                client.write(stream, b"hello")
                return await client.receive_stream()

        assert run(exchange()) == b"echo:hello"
        session_server.stop()

    def test_a_stream_the_server_opens_reaches_the_client(self, session_server):
        async def exchange() -> bytes:
            async with webtransport("localhost", session_server.port, "/greet") as client:
                return await client.receive_stream()

        assert run(exchange()) == b"greetings"
        session_server.stop()


class TestCoexistence:
    def test_ordinary_requests_still_work(self, session_server):
        # The same port answers HTTP over TCP while WebTransport is available
        # over QUIC.
        import ssl
        import urllib.request

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        url = f"https://localhost:{session_server.port}/plain"
        with urllib.request.urlopen(url, context=context, timeout=10) as answer:
            assert answer.read() == b"plain"

        session_server.stop()


class TestHttp3Requests:
    def test_a_get_carries_its_body(self, session_server):
        async def exchange():
            async with http3("localhost", session_server.port) as client:
                return await client.request(
                    "GET", f"localhost:{session_server.port}", "/plain"
                )

        response = run(exchange())
        assert response.status == 200
        assert bytes(response.body) == b"plain"
        assert response.headers["content-length"] == "5"
        session_server.stop()

    def test_a_head_carries_the_length_and_no_body(self, session_server):
        # RFC 9110 §9.3.2. A client that receives DATA on a HEAD response
        # resets the stream, which is what this looked like from curl.
        async def exchange():
            async with http3("localhost", session_server.port) as client:
                return await client.request(
                    "HEAD", f"localhost:{session_server.port}", "/plain"
                )

        response = run(exchange())
        assert response.status == 200
        assert bytes(response.body) == b""
        assert response.headers["content-length"] == "5"
        session_server.stop()

    def test_the_server_headers_match_the_tcp_path(self, session_server):
        # The response head is built by hand on the HTTP/3 path, so the headers
        # the server owns have to be added there too rather than only by hyper.
        async def exchange():
            async with http3("localhost", session_server.port) as client:
                return await client.request(
                    "GET", f"localhost:{session_server.port}", "/plain"
                )

        response = run(exchange())
        assert response.headers["server"] == "nitro"
        assert "alt-svc" in response.headers
        assert response.headers["date"].endswith(" GMT")
        session_server.stop()

    def test_requests_reach_the_access_log(self, server_factory, certificate):
        certificate_path, key_path = certificate
        server = server_factory(
            f"""
            from nitro import Nitro
            from nitro.protocols import PlainTextResponse

            app = Nitro(
                http="3",
                log_level="warning",
                access_log=True,
                tls_cert={certificate_path!r},
                tls_key={key_path!r},
                drain_timeout=2,
            )

            @app.route("/plain")
            async def plain(request):
                return PlainTextResponse("plain")
            """
        )

        async def exchange():
            async with http3("localhost", server.port) as client:
                return await client.request("GET", f"localhost:{server.port}", "/plain")

        assert run(exchange()).status == 200
        server.stop()
        assert '"GET /plain HTTP/3" 200 5' in server.output
