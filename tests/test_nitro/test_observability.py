"""End-to-end tests for the metrics endpoint, against a real server process."""

from __future__ import annotations

import os
import socket
import urllib.error
import urllib.request

import pytest

from nitro.settings import ImproperlyConfigured, ServerOptions, settings


# Metrics ports are chosen from below the ephemeral range. The application is
# started on a kernel-chosen port, and the kernel hands those out in ascending
# order — a metrics port taken from the same range is liable to be the very one
# the application is given a moment later.
_FIRST_CANDIDATE = 20000
_LAST_CANDIDATE = 40000
_next_candidate = _FIRST_CANDIDATE + (os.getpid() * 8) % 8000


def _bind_localhost(port: int) -> list[socket.socket]:
    """Hold `port` on every address "localhost" resolves to, or raise.

    The server binds all of them, so a port is only free if all of them are.
    """
    held: list[socket.socket] = []
    try:
        for family, kind, protocol, _, address in socket.getaddrinfo(
            "localhost", port, type=socket.SOCK_STREAM
        ):
            probe = socket.socket(family, kind, protocol)
            if family == socket.AF_INET6:
                probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            probe.bind(address)
            held.append(probe)
    except OSError:
        for probe in held:
            probe.close()
        raise
    return held


def free_port(count: int = 1) -> int:
    """The first of `count` consecutive ports nothing is listening on.

    The exporter cannot be asked which port it chose from outside the process,
    so the test picks one and hands it over. Workers take one port each,
    starting from the configured one, so several of them need a free run.
    """
    global _next_candidate

    while _next_candidate + count <= _LAST_CANDIDATE:
        first = _next_candidate
        _next_candidate += count
        held: list[socket.socket] = []
        try:
            for offset in range(count):
                held.extend(_bind_localhost(first + offset))
        except OSError:
            continue
        finally:
            for probe in held:
                probe.close()
        return first

    raise AssertionError(f"no run of {count} free ports was found")


def scrape(port: int, path: str = "/metrics", timeout: float = 10.0):
    request = urllib.request.Request(f"http://localhost:{port}{path}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            return answer.status, answer.read().decode(), dict(answer.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(), dict(error.headers)


def application(metrics_port: int, *, enabled: bool = True, workers: int = 1) -> str:
    return f"""
        from nitro import Nitro
        from nitro.protocols import HttpRequest, HttpResponse, PlainTextResponse

        app = Nitro(
            http="1",
            log_level="warning",
            workers={workers},
            observability_enabled={enabled},
            observability_port={metrics_port},
        )

        @app.route("/counted/<int:number>")
        async def counted(request: HttpRequest, number: int) -> HttpResponse:
            return PlainTextResponse(f"counted {{number}}")

        @app.route("/failing")
        async def failing(request: HttpRequest) -> HttpResponse:
            return PlainTextResponse("no", status=500)

        @app.websocket("/socket")
        async def socket(connection) -> None:
            await connection.accept()
            await connection.send_text("open")
            await connection.close()

        @app.websocket("/refused")
        async def refused(connection) -> None:
            await connection.reject(403, "no entry")
    """


class TestSettings:
    def test_the_exporter_is_off_and_loopback_only_by_default(self):
        options = ServerOptions.resolve()

        assert options.observability_enabled is False
        assert options.observability_host == "localhost"
        assert options.observability_port == 9464

    def test_the_exporter_does_not_share_the_application_port(self):
        options = ServerOptions.resolve()

        assert options.observability_port != options.port

    def test_the_settings_are_flat_not_nested_under_server(self):
        # SERVER is gone entirely; every server option is top level now, so
        # there is no nesting left for these to be confused with.
        class Source:
            SERVER = {"OBSERVABILITY_ENABLED": True}

        with pytest.raises(ImproperlyConfigured, match="no longer a setting"):
            ServerOptions.resolve(Source())

    def test_a_settings_module_can_turn_it_on(self):
        class Source:
            OBSERVABILITY_ENABLED = True
            OBSERVABILITY_HOST = "localhost"
            OBSERVABILITY_PORT = 9999

        options = ServerOptions.resolve(Source())

        assert options.observability_enabled is True
        assert options.observability_port == 9999


class TestChecking:
    @pytest.fixture(autouse=True)
    def restore_settings(self):
        yield
        settings.reset()

    def test_check_reports_a_port_clash(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        from nitro.cli.commands.check import check

        settings_module = tmp_path / "clashing.py"
        settings_module.write_text(
            "DEBUG = True\n"
            "SERVER_PORT = 9464\n"
            "OBSERVABILITY_ENABLED = True\n"
            "OBSERVABILITY_PORT = 9464\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.setenv("NITRO_SETTINGS_MODULE", "clashing")
        settings.reset()

        result = CliRunner().invoke(check, [])

        assert result.exit_code != 0
        assert "application's port" in result.output

    def test_check_reports_metrics_on_every_interface(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        from nitro.cli.commands.check import check

        settings_module = tmp_path / "exposed.py"
        settings_module.write_text(
            "DEBUG = True\n"
            "OBSERVABILITY_ENABLED = True\n"
            "OBSERVABILITY_HOST = '0.0.0.0'\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.setenv("NITRO_SETTINGS_MODULE", "exposed")
        settings.reset()

        result = CliRunner().invoke(check, [])

        assert result.exit_code != 0
        assert "every interface" in result.output


class TestScraping:
    def test_a_served_request_is_counted_by_route_and_status(self, server_factory):
        metrics_port = free_port()
        server = server_factory(application(metrics_port))

        assert server.request("/counted/12").status == 200
        status, body, headers = scrape(metrics_port)

        assert status == 200
        assert headers["content-type"].startswith("text/plain")
        assert 'route="/counted/<int:number>"' in body
        assert 'method="GET"' in body
        assert 'status="2xx"' in body
        assert "/counted/12" not in body, "the concrete path must not be a label"
        server.stop()

    def test_latency_and_in_flight_are_reported(self, server_factory):
        metrics_port = free_port()
        server = server_factory(application(metrics_port))

        server.request("/counted/1")
        _, body, _ = scrape(metrics_port)

        assert "nitro_http_request_duration_seconds_bucket" in body
        assert "nitro_http_request_duration_seconds_sum" in body
        assert "nitro_http_requests_in_flight" in body
        server.stop()

    def test_connections_and_worker_state_are_reported(self, server_factory):
        metrics_port = free_port()
        server = server_factory(application(metrics_port))

        server.request("/counted/1")
        _, body, _ = scrape(metrics_port)

        assert 'nitro_connections_total{transport="tcp"}' in body
        assert 'nitro_connections_active{transport="tcp"}' in body
        assert "nitro_worker_start_time_seconds" in body
        assert "nitro_worker_draining 0" in body
        server.stop()

    def test_a_failing_route_is_counted_as_5xx(self, server_factory):
        metrics_port = free_port()
        server = server_factory(application(metrics_port))

        assert server.request("/failing").status == 500
        _, body, _ = scrape(metrics_port)

        counted = [
            line
            for line in body.splitlines()
            if line.startswith("nitro_http_requests_total") and "/failing" in line
        ]
        assert counted, f"the failing route was not counted:\n{body}"
        assert 'status="5xx"' in counted[0]
        server.stop()

    def test_an_unmatched_path_does_not_become_its_own_series(self, server_factory):
        metrics_port = free_port()
        server = server_factory(application(metrics_port))

        assert server.request("/no-such-thing").status == 404
        _, body, _ = scrape(metrics_port)

        assert 'route="unmatched"' in body
        assert "/no-such-thing" not in body
        server.stop()

    def test_only_the_metrics_path_answers(self, server_factory):
        metrics_port = free_port()
        server = server_factory(application(metrics_port))

        status, _, _ = scrape(metrics_port, "/")

        assert status == 404
        server.stop()

    def test_the_application_port_does_not_serve_metrics(self, server_factory):
        metrics_port = free_port()
        server = server_factory(application(metrics_port))

        assert server.port != metrics_port
        assert server.request("/metrics").status == 404
        server.stop()

    def test_nothing_listens_when_it_is_switched_off(self, server_factory):
        metrics_port = free_port()
        server = server_factory(application(metrics_port, enabled=False))

        assert server.request("/counted/1").status == 200
        with pytest.raises(urllib.error.URLError):
            scrape(metrics_port, timeout=2.0)
        server.stop()

    def test_socket_handshakes_are_counted_by_outcome(self, server_factory):
        import asyncio

        import websockets

        metrics_port = free_port()
        server = server_factory(application(metrics_port))

        async def exchange() -> str:
            async with websockets.connect(
                f"ws://localhost:{server.port}/socket"
            ) as connection:
                return await connection.recv()

        assert asyncio.run(asyncio.wait_for(exchange(), timeout=20)) == "open"

        async def refused() -> None:
            async with websockets.connect(f"ws://localhost:{server.port}/refused"):
                pass

        with pytest.raises(websockets.exceptions.InvalidStatus):
            asyncio.run(asyncio.wait_for(refused(), timeout=20))

        _, body, _ = scrape(metrics_port)

        assert 'nitro_sockets_total{outcome="accepted",protocol="websocket"} 1' in body
        assert 'nitro_sockets_total{outcome="refused",protocol="websocket"} 1' in body
        assert 'nitro_sockets_active{protocol="websocket"} 0' in body, (
            f"a closed socket must not be left counted as open:\n{body}"
        )
        server.stop()

    def test_each_worker_exposes_its_own_endpoint(self, server_factory):
        first = free_port(2)
        server = server_factory(application(first, workers=2))

        for offset in (0, 1):
            status, body, _ = scrape(first + offset)
            assert status == 200, f"worker {offset} did not answer"
            assert "nitro_worker_start_time_seconds" in body

        server.stop()

    def test_the_exporter_stops_with_the_server(self, server_factory):
        metrics_port = free_port()
        server = server_factory(application(metrics_port))

        assert scrape(metrics_port)[0] == 200
        assert server.stop() == 0

        with pytest.raises(urllib.error.URLError):
            scrape(metrics_port, timeout=2.0)
