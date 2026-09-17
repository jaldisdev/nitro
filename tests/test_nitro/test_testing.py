#
# This source file is part of the Nitro open source project.
#
# Copyright (c) 2026 Jaldis B.V.
#
# Licensed under the MIT OR Apache-2.0 license (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://opensource.org/licenses/MIT
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import asyncio

import pytest

from nitro import Nitro
from nitro.middleware.base import Middleware
from nitro.protocols import (
    FileResponse,
    Http404,
    HttpRequest,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
    WebSocket,
    WebSocketDisconnect,
    WebTransportDisconnect,
    WebTransportSession,
)
from nitro.testing import TestClient, WebSocketRejected, WebTransportRejected


def application() -> Nitro:
    return Nitro(routes=[], middleware=[], debug=False)


class TestHttp:
    async def test_path_parameters_are_matched_and_converted(self):
        app = application()

        @app.route("/users/<int:user_id>")
        async def user(request: HttpRequest, user_id: int):
            return JSONResponse({"id": user_id, "type": type(user_id).__name__})

        response = await TestClient(app).get("/users/7")

        assert response.status_code == 200
        assert response.json() == {"id": 7, "type": "int"}

    async def test_a_path_the_parameter_expression_refuses_is_not_found(self):
        app = application()

        @app.route("/users/<int:user_id>")
        async def user(request: HttpRequest, user_id: int):
            return PlainTextResponse("found")

        response = await TestClient(app).get("/users/seven")

        assert response.status_code == 404

    async def test_a_known_path_with_the_wrong_method_is_405_with_allow(self):
        app = application()

        @app.route("/things", methods=["GET", "POST"])
        async def things(request: HttpRequest):
            return PlainTextResponse("things")

        response = await TestClient(app).delete("/things")

        assert response.status_code == 405
        assert response.headers["allow"] == "GET, HEAD, POST"

    async def test_head_answers_like_get_without_a_body(self):
        app = application()

        @app.route("/page")
        async def page(request: HttpRequest):
            return PlainTextResponse("content")

        response = await TestClient(app).head("/page")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert response.content == b""

    async def test_query_headers_and_json_body_reach_the_handler(self):
        app = application()

        @app.route("/echo", methods=["POST"])
        async def echo(request: HttpRequest):
            return JSONResponse(
                {
                    "query": dict(request.query_params.items()),
                    "agent": request.headers.get("user-agent"),
                    "body": await request.json(),
                }
            )

        response = await TestClient(app, headers={"user-agent": "tests"}).post(
            "/echo?a=1", params={"b": "2"}, json={"hello": "world"}
        )

        payload = response.json()
        assert payload["query"] == {"a": "1", "b": "2"}
        assert payload["agent"] == "tests"
        assert payload["body"] == {"hello": "world"}

    async def test_form_fields_and_uploads_are_encoded(self):
        app = application()

        @app.route("/upload", methods=["POST"])
        async def upload(request: HttpRequest):
            form = await request.form()
            document = form["document"]
            return JSONResponse(
                {
                    "title": form["title"],
                    "filename": document.filename,
                    "content": (await document.read()).decode(),
                }
            )

        response = await TestClient(app).post(
            "/upload",
            data={"title": "Report"},
            files={"document": ("report.txt", b"quarterly", "text/plain")},
        )

        assert response.json() == {
            "title": "Report",
            "filename": "report.txt",
            "content": "quarterly",
        }

    async def test_the_body_is_one_kind_only(self):
        with pytest.raises(ValueError, match="one of content, json or data"):
            await TestClient(application()).post("/", content=b"raw", json={})

    async def test_cookies_are_kept_sent_back_and_expired(self):
        app = application()

        @app.route("/login")
        async def login(request: HttpRequest):
            response = PlainTextResponse("in")
            response.set_cookie("session", "abc")
            return response

        @app.route("/whoami")
        async def whoami(request: HttpRequest):
            return PlainTextResponse(request.cookies.get("session") or "nobody")

        @app.route("/logout")
        async def logout(request: HttpRequest):
            response = PlainTextResponse("out")
            response.delete_cookie("session")
            return response

        client = TestClient(app)
        login_response = await client.get("/login")
        assert login_response.cookies == {"session": "abc"}
        assert (await client.get("/whoami")).text == "abc"

        await client.get("/logout")
        assert client.cookies == {}
        assert (await client.get("/whoami")).text == "nobody"

    async def test_middleware_and_exception_handlers_take_part(self):
        async def not_found(request, exception):
            return PlainTextResponse("custom missing", status_code=404)

        app = Nitro(routes=[], middleware=[], debug=False, exception_handlers={404: not_found})

        class Stamp(Middleware):
            async def __http__(self, request, call_next):
                response = await call_next(request)
                response.headers["x-stamp"] = "yes"
                return response

        app.middleware.add_middleware(Stamp())

        @app.route("/stamped")
        async def stamped(request: HttpRequest):
            return PlainTextResponse("stamped")

        @app.route("/missing")
        async def missing(request: HttpRequest):
            raise Http404()

        client = TestClient(app)
        assert (await client.get("/stamped")).headers["x-stamp"] == "yes"

        response = await client.get("/missing")
        assert response.status_code == 404
        assert response.text == "custom missing"

    async def test_a_handler_that_never_answers_is_a_500(self):
        app = application()

        @app.route("/silent")
        async def silent(request: HttpRequest):
            return None

        response = await TestClient(app).get("/silent")

        assert response.status_code == 500
        assert response.text == "Internal Server Error"

    async def test_answering_twice_is_refused(self):
        app = application()
        seen: list[str] = []

        @app.route("/twice")
        async def twice(request: HttpRequest):
            request.protocol.response_str(200, [], "first")
            try:
                request.protocol.response_str(200, [], "second")
            except RuntimeError as error:
                seen.append(str(error))

        response = await TestClient(app).get("/twice")

        assert response.text == "first"
        assert seen == ["a response has already been sent for this request"]

    async def test_a_streamed_response_is_collected(self):
        app = application()

        async def chunks():
            for index in range(3):
                yield f"chunk {index};"

        @app.route("/stream")
        async def stream(request: HttpRequest):
            return StreamingResponse(chunks(), content_type="text/plain")

        response = await TestClient(app).get("/stream")

        assert response.text == "chunk 0;chunk 1;chunk 2;"

    async def test_a_stream_is_read_while_it_is_produced_and_disconnect_is_seen(self):
        app = application()
        release = asyncio.Event()
        noticed = asyncio.Event()

        @app.route("/events")
        async def events(request: HttpRequest):
            transport = request.protocol.response_stream(
                200, [("content-type", "text/event-stream")]
            )
            await transport.send_str("data: first\n\n")
            await release.wait()
            await request.protocol.client_disconnect()
            noticed.set()

        async with TestClient(app).stream("GET", "/events") as response:
            assert response.headers["content-type"] == "text/event-stream"
            chunks = response.iter_text()
            assert await anext(chunks) == "data: first\n\n"
            release.set()

        assert noticed.is_set()

    async def test_files_are_served_by_the_compiled_file_response(self, tmp_path):
        document = tmp_path / "notes.txt"
        document.write_bytes(b"0123456789")
        app = application()

        @app.route("/whole")
        async def whole(request: HttpRequest):
            return FileResponse(document)

        @app.route("/part")
        async def part(request: HttpRequest):
            return FileResponse(document, range=(2, 5))

        @app.route("/beyond")
        async def beyond(request: HttpRequest):
            return FileResponse(document, range=(20, None))

        @app.route("/missing")
        async def missing(request: HttpRequest):
            return FileResponse(tmp_path / "absent.txt")

        client = TestClient(app)

        whole_response = await client.get("/whole")
        assert whole_response.content == b"0123456789"
        assert whole_response.headers["content-type"] == "text/plain"
        assert "last-modified" in whole_response.headers

        part_response = await client.get("/part")
        assert part_response.status_code == 206
        assert part_response.content == b"2345"
        assert part_response.headers["content-range"] == "bytes 2-5/10"

        assert (await client.get("/beyond")).status_code == 416
        assert (await client.get("/missing")).status_code == 404

    async def test_startup_and_shutdown_run_around_the_block(self):
        app = application()
        events: list[str] = []

        @app.on_startup
        async def started():
            events.append("startup")

        @app.on_shutdown
        def stopped():
            events.append("shutdown")

        @app.route("/")
        async def index(request: HttpRequest):
            events.append("request")
            return PlainTextResponse("ok")

        async with TestClient(app) as client:
            await client.get("/")

        assert events == ["startup", "request", "shutdown"]


class TestWebSocket:
    async def test_messages_flow_both_ways_until_the_client_closes(self):
        app = application()
        finished = asyncio.Event()

        @app.websocket("/rooms/<int:room>")
        async def room(socket: WebSocket, room: int):
            await socket.accept()
            async for message in socket:
                await socket.send_text(f"{room}: {message}")
            finished.set()

        async with TestClient(app).websocket("/rooms/4") as socket:
            await socket.send_text("hello")
            assert await socket.receive_text() == "4: hello"
            await socket.send_json({"a": 1})
            assert await socket.receive_text() == '4: {"a":1}'

        assert finished.is_set()

    async def test_the_chosen_subprotocol_is_reported(self):
        app = application()

        @app.websocket("/chat")
        async def chat(socket: WebSocket):
            await socket.accept("v2" if "v2" in socket.subprotocols else None)
            await socket.close()

        async with TestClient(app).websocket("/chat", subprotocols=["v1", "v2"]) as socket:
            assert socket.accepted_subprotocol == "v2"
            with pytest.raises(WebSocketDisconnect) as closed:
                await socket.receive()
            assert closed.value.code == 1000

    async def test_choosing_a_subprotocol_not_offered_fails_the_handshake(self):
        app = application()

        @app.websocket("/chat")
        async def chat(socket: WebSocket):
            await socket.accept("v3")

        with pytest.raises(WebSocketRejected) as rejected:
            async with TestClient(app).websocket("/chat", subprotocols=["v1"]):
                pass

        assert rejected.value.status == 500

    async def test_a_rejection_carries_its_status_and_reason(self):
        app = application()

        @app.websocket("/private")
        async def private(socket: WebSocket):
            if socket.headers.get("authorization") != "Bearer secret":
                await socket.reject(403, "not for you")
                return
            await socket.accept()

        client = TestClient(app)
        with pytest.raises(WebSocketRejected) as rejected:
            async with client.websocket("/private"):
                pass
        assert (rejected.value.status, rejected.value.reason) == (403, "not for you")

        async with client.websocket("/private", headers={"authorization": "Bearer secret"}):
            pass

    async def test_an_unknown_path_and_an_unanswered_handshake_are_refused(self):
        app = application()

        @app.websocket("/quiet")
        async def quiet(socket: WebSocket):
            return None

        client = TestClient(app)
        with pytest.raises(WebSocketRejected) as unknown:
            async with client.websocket("/nowhere"):
                pass
        assert unknown.value.status == 404

        with pytest.raises(WebSocketRejected) as unanswered:
            async with client.websocket("/quiet"):
                pass
        assert unanswered.value.status == 500

    async def test_a_handler_returning_with_the_socket_open_drops_it(self):
        app = application()

        @app.websocket("/brief")
        async def brief(socket: WebSocket):
            await socket.accept()
            await socket.send_bytes(b"bye")

        async with TestClient(app).websocket("/brief") as socket:
            assert await socket.receive_bytes() == b"bye"
            with pytest.raises(WebSocketDisconnect) as dropped:
                await socket.receive()
            assert dropped.value.code == 1006

    async def test_cookies_travel_with_the_upgrade(self):
        app = application()

        @app.websocket("/session")
        async def session(socket: WebSocket):
            await socket.accept()
            await socket.send_text(socket.headers.get("cookie") or "")

        async with TestClient(app, cookies={"session": "abc"}).websocket("/session") as socket:
            assert await socket.receive_text() == "session=abc"


class TestWebTransport:
    async def test_datagrams_echo_until_the_client_closes(self):
        app = application()
        finished = asyncio.Event()

        @app.webtransport("/game")
        async def game(session: WebTransportSession):
            await session.accept()
            async for datagram in session.iter_datagrams():
                session.send_datagram(datagram.upper())
            finished.set()

        async with TestClient(app).webtransport("/game") as session:
            session.send_datagram(b"ping")
            assert await session.receive_datagram() == b"PING"

        assert finished.is_set()

    async def test_a_rejection_carries_its_status(self):
        app = application()

        @app.webtransport("/closed")
        async def closed(session: WebTransportSession):
            await session.reject(401)

        with pytest.raises(WebTransportRejected) as rejected:
            async with TestClient(app).webtransport("/closed"):
                pass

        assert rejected.value.status == 401

    async def test_streams_in_every_direction(self):
        app = application()

        @app.webtransport("/streams")
        async def streams(session: WebTransportSession):
            await session.accept()

            echoed = await session.accept_stream()
            await echoed.send(await echoed.receive_all())
            await echoed.finish()

            incoming = await session.session.accept_incoming()
            received = await incoming.read_all()

            outgoing = await session.open_outgoing()
            await outgoing.send(b"from the server: " + received)
            await outgoing.finish()

            await session.close()

        async with TestClient(app).webtransport("/streams") as session:
            bidirectional = await session.open_stream()
            await bidirectional.send_text("round trip")
            await bidirectional.finish()
            assert await bidirectional.receive_text() == "round trip"

            one_way = await session.open_outgoing()
            assert not one_way.readable
            await one_way.send(b"upload")
            await one_way.finish()

            pushed = await session.accept_incoming()
            assert not pushed.writable
            assert await pushed.receive_all() == b"from the server: upload"

            with pytest.raises(WebTransportDisconnect):
                await session.receive_datagram()

    async def test_reading_a_stream_in_pieces(self):
        app = application()

        @app.webtransport("/pieces")
        async def pieces(session: WebTransportSession):
            await session.accept()
            stream = await session.open_stream()
            await stream.send(b"abcdef")
            await stream.finish()
            await session.accept_stream()

        async with TestClient(app).webtransport("/pieces") as session:
            stream = await session.accept_stream()
            assert await stream.receive(4) == b"abcd"
            assert await stream.receive(4) == b"ef"
            assert await stream.receive(4) == b""
