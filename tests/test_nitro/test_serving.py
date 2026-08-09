"""End-to-end tests against a real `nitro run` process."""

import asyncio
import os
import signal

import pytest


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TestRequestHandling:
    def test_a_route_answers(self, server_factory, hello_app):
        server = server_factory(hello_app)
        response = server.request("/")

        assert response.status == 200
        assert response.text == "hello"
        assert response.headers["server"] == "nitro"
        assert server.stop() == 0

    def test_an_unknown_path_is_a_404(self, server_factory, hello_app):
        server = server_factory(hello_app)
        assert server.request("/missing").status == 404
        server.stop()

    def test_the_request_reaches_the_handler_intact(self, server_factory):
        server = server_factory(
            """
            from nitro import Nitro

            app = Nitro(http="1", log_level="warning")

            @app.route("/echo", methods=["POST"])
            async def echo(request):
                scope, protocol = request.scope, request.protocol
                body = await protocol()
                protocol.response_str(
                    200,
                    [("content-type", "text/plain")],
                    f"{scope.method} {scope.path}?{scope.query_string} "
                    f"http/{scope.http_version} {scope.scheme} "
                    f"agent={scope.headers['x-agent']} body={body.decode()}",
                )
            """
        )

        response = server.request(
            "/echo?a=1&b=2",
            method="POST",
            data=b"payload",
            headers={"x-agent": "probe"},
        )

        assert response.status == 200
        assert response.text == (
            "POST /echo?a=1&b=2 http/1.1 http agent=probe body=payload"
        )
        server.stop()

    def test_response_headers_reach_the_client(self, server_factory):
        server = server_factory(
            """
            from nitro import Nitro

            app = Nitro(http="1", log_level="warning")

            @app.route("/cookies")
            async def cookies(request):
                scope, protocol = request.scope, request.protocol
                protocol.response_empty(
                    204,
                    [("set-cookie", "a=1"), ("set-cookie", "b=2"), ("x-custom", "value")],
                )
            """
        )

        response = server.request("/cookies")

        assert response.status == 204
        assert response.headers["x-custom"] == "value"
        server.stop()

    def test_a_handler_that_raises_becomes_a_500(self, server_factory):
        server = server_factory(
            """
            from nitro import Nitro

            app = Nitro(http="1", log_level="error")

            @app.route("/boom")
            async def boom(request):
                scope, protocol = request.scope, request.protocol
                raise RuntimeError("deliberate failure")
            """
        )

        assert server.request("/boom").status == 500
        server.stop()

    def test_a_handler_that_never_responds_becomes_a_500(self, server_factory):
        server = server_factory(
            """
            from nitro import Nitro

            app = Nitro(http="1", log_level="error")

            @app.route("/silent")
            async def silent(request):
                scope, protocol = request.scope, request.protocol
                return None
            """
        )

        assert server.request("/silent").status == 500
        server.stop()

    def test_the_headers_object_behaves_like_a_mapping(self, server_factory):
        server = server_factory(
            """
            from nitro import Nitro

            app = Nitro(http="1", log_level="warning")

            @app.route("/headers")
            async def report(request):
                scope, protocol = request.scope, request.protocol
                headers = scope.headers
                lines = [
                    f"getitem={headers['x-probe']}",
                    f"get-missing={headers.get('x-absent', 'fallback')}",
                    f"contains={'x-probe' in headers}",
                    f"absent={'x-absent' in headers}",
                    f"names={sorted(headers.keys()) == sorted(iter(headers))}",
                    f"len-matches-keys={len(headers) == len(headers.keys())}",
                    f"all={headers.get_all('x-probe')}",
                ]
                protocol.response_str(200, [], "\\n".join(lines))
            """
        )

        response = server.request("/headers", headers={"x-probe": "value"})

        assert "getitem=value" in response.text
        assert "get-missing=fallback" in response.text
        assert "contains=True" in response.text
        assert "absent=False" in response.text
        assert "names=True" in response.text
        assert "len-matches-keys=True" in response.text
        assert "all=['value']" in response.text
        server.stop()


class TestLifecycle:
    def test_startup_and_shutdown_hooks_run(self, server_factory, tmp_path):
        marker = tmp_path / "lifecycle.log"
        server = server_factory(
            f"""
            from pathlib import Path
            from nitro import Nitro

            marker = Path({str(marker)!r})
            app = Nitro(http="1", log_level="warning")

            @app.on_startup
            async def started():
                marker.write_text("started\\n")

            @app.on_shutdown
            def stopped():
                with marker.open("a") as handle:
                    handle.write("stopped\\n")

            @app.route("/")
            async def index(request):
                scope, protocol = request.scope, request.protocol
                protocol.response_str(200, [], "ok")
            """
        )

        assert server.request("/").status == 200
        assert marker.read_text() == "started\n"

        assert server.stop() == 0
        assert marker.read_text() == "started\nstopped\n"

    def test_an_in_flight_request_finishes_during_shutdown(self, server_factory):
        import threading

        server = server_factory(
            """
            import asyncio
            from nitro import Nitro

            app = Nitro(http="1", log_level="warning")

            @app.route("/slow")
            async def slow(request):
                scope, protocol = request.scope, request.protocol
                await asyncio.sleep(1.0)
                protocol.response_str(200, [], "finished")
            """
        )

        answers = []
        caller = threading.Thread(
            target=lambda: answers.append(server.request("/slow", timeout=20.0))
        )
        caller.start()
        # Long enough for the request to be accepted and dispatched.
        threading.Event().wait(0.4)

        assert server.stop() == 0
        caller.join(timeout=20.0)

        assert len(answers) == 1
        assert answers[0].status == 200
        assert answers[0].text == "finished"

    def test_the_port_is_released_after_shutdown(self, server_factory, hello_app):
        server = server_factory(hello_app)
        port = server.port
        assert server.stop() == 0

        import socket

        with socket.socket() as probe:
            probe.settimeout(2.0)
            with pytest.raises(OSError):
                probe.connect(("127.0.0.1", port))

    def test_an_interrupt_also_stops_the_server(self, server_factory, hello_app):
        server = server_factory(hello_app)
        assert server.stop(signal.SIGINT) == 0


class TestWorkers:
    def test_several_workers_all_serve(self, server_factory):
        server = server_factory(
            """
            import os
            from nitro import Nitro

            app = Nitro(http="1", log_level="warning")

            @app.route("/pid")
            async def pid(request):
                scope, protocol = request.scope, request.protocol
                protocol.response_str(200, [], str(os.getpid()))
            """,
            "-w",
            "3",
        )

        seen = {server.request("/pid").text for _ in range(30)}

        assert len(seen) >= 2, f"requests only ever reached {seen}"
        assert server.stop() == 0

    def test_no_worker_outlives_the_parent(self, server_factory, hello_app):
        import subprocess
        import time

        server = server_factory(hello_app, "-w", "2")
        assert server.request("/").status == 200

        listed = subprocess.run(
            ["pgrep", "-P", str(server.process.pid)], capture_output=True, text=True
        )
        workers = [int(line) for line in listed.stdout.split()]
        assert len(workers) == 2, f"expected two workers, saw {workers}"

        assert server.stop() == 0

        deadline = time.monotonic() + 10.0
        alive = list(workers)
        while alive and time.monotonic() < deadline:
            alive = [pid for pid in alive if _is_running(pid)]
            if alive:
                time.sleep(0.1)

        assert not alive, f"workers {alive} outlived the parent"

    def test_a_crashed_worker_is_replaced(self, server_factory):
        import subprocess
        import time

        server = server_factory(
            """
            import os
            from nitro import Nitro

            app = Nitro(http="1", log_level="warning")

            @app.route("/pid")
            async def pid(request):
                scope, protocol = request.scope, request.protocol
                protocol.response_str(200, [], str(os.getpid()))
            """,
            "-w",
            "2",
        )

        def workers() -> set[int]:
            listed = subprocess.run(
                ["pgrep", "-P", str(server.process.pid)], capture_output=True, text=True
            )
            return {int(line) for line in listed.stdout.split()}

        before = workers()
        assert len(before) == 2

        victim = next(iter(before))
        os.kill(victim, signal.SIGKILL)

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            current = workers()
            if len(current) == 2 and victim not in current:
                break
            time.sleep(0.2)
        else:
            raise AssertionError("the crashed worker was never replaced")

        assert server.request("/pid").status == 200
        assert server.stop() == 0


class TestConfiguration:
    def test_a_bad_setting_is_reported_without_starting(self, tmp_path):
        import subprocess
        import sys

        (tmp_path / "app.py").write_text(
            "from nitro import Nitro\napp = Nitro(http='nonsense')\n"
        )
        finished = subprocess.run(
            [sys.executable, "-m", "nitro.cli", "run", "app:app", "-p", "0"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert finished.returncode != 0
        assert "http" in (finished.stderr + finished.stdout)

    def test_an_occupied_port_fails_before_serving(self, tmp_path):
        import socket
        import subprocess
        import sys

        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            port = occupied.getsockname()[1]

            (tmp_path / "app.py").write_text(
                "from nitro import Nitro\napp = Nitro(http='1')\n"
            )
            finished = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "nitro.cli",
                    "run",
                    "app:app",
                    "-H",
                    "127.0.0.1",
                    "-p",
                    str(port),
                ],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=60,
            )

        assert finished.returncode != 0
        assert "Address already in use" in (finished.stderr + finished.stdout)


class TestRouting:
    ROUTING_APP = """
        from nitro import Nitro
        from nitro.routing import Mount, Router

        app = Nitro(http="1", log_level="warning")

        @app.route("/users/<int:user_id>")
        async def user(request, user_id):
            scope, protocol = request.scope, request.protocol
            protocol.response_str(200, [], f"user {user_id!r} {type(user_id).__name__}")

        @app.route("/users/new")
        async def new_user(request):
            scope, protocol = request.scope, request.protocol
            protocol.response_str(200, [], "new user form")

        @app.route("/posts/<slug:title>")
        async def post(request, title):
            scope, protocol = request.scope, request.protocol
            protocol.response_str(200, [], f"post {title}")

        @app.route("/files/<path:rest>")
        async def file(request, rest):
            scope, protocol = request.scope, request.protocol
            protocol.response_str(200, [], f"file {rest}")

        @app.route("/items/<uuid:identifier>")
        async def item(request, identifier):
            scope, protocol = request.scope, request.protocol
            protocol.response_str(200, [], f"item {identifier}")

        @app.route("/things", methods=["POST"])
        async def create(request):
            scope, protocol = request.scope, request.protocol
            protocol.response_str(201, [], "created")

        @app.route("/named/<int:number>", name="named")
        async def named(request, number):
            scope, protocol = request.scope, request.protocol
            protocol.response_str(200, [], app.url_for("named", number=number + 1))

        api = Router()

        @api.route("/status")
        async def status(request):
            scope, protocol = request.scope, request.protocol
            protocol.response_str(200, [], "api ok")

        app.mount(Mount("/api", api))
    """

    def test_a_parameter_reaches_the_handler_converted(self, server_factory):
        server = server_factory(self.ROUTING_APP)
        assert server.request("/users/42").text == "user 42 int"
        server.stop()

    def test_a_static_segment_beats_a_parameter(self, server_factory):
        server = server_factory(self.ROUTING_APP)
        assert server.request("/users/new").text == "new user form"
        server.stop()

    def test_a_value_the_converter_rejects_is_a_404(self, server_factory):
        server = server_factory(self.ROUTING_APP)
        assert server.request("/users/abc").status == 404
        assert server.request("/items/not-a-uuid").status == 404
        server.stop()

    def test_a_uuid_is_converted(self, server_factory):
        server = server_factory(self.ROUTING_APP)
        identifier = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
        assert server.request(f"/items/{identifier}").text == f"item {identifier}"
        server.stop()

    def test_a_greedy_parameter_spans_separators(self, server_factory):
        server = server_factory(self.ROUTING_APP)
        assert server.request("/files/deep/nested/name.txt").text == "file deep/nested/name.txt"
        server.stop()

    def test_a_slug_stops_at_a_separator(self, server_factory):
        server = server_factory(self.ROUTING_APP)
        assert server.request("/posts/hello-world").text == "post hello-world"
        assert server.request("/posts/hello/world").status == 404
        server.stop()

    def test_the_wrong_method_is_a_405_that_says_what_is_allowed(self, server_factory):
        server = server_factory(self.ROUTING_APP)
        response = server.request("/things", method="GET")

        assert response.status == 405
        assert response.headers["allow"] == "POST"
        server.stop()

    def test_the_right_method_is_answered(self, server_factory):
        server = server_factory(self.ROUTING_APP)
        assert server.request("/things", method="POST", data=b"").status == 201
        server.stop()

    def test_a_mounted_router_is_served_under_its_prefix(self, server_factory):
        server = server_factory(self.ROUTING_APP)
        assert server.request("/api/status").text == "api ok"
        assert server.request("/status").status == 404
        server.stop()

    def test_reverse_routing_produces_a_usable_path(self, server_factory):
        server = server_factory(self.ROUTING_APP)
        assert server.request("/named/7").text == "/named/8"
        server.stop()

    def test_a_broken_route_stops_startup(self, tmp_path):
        import subprocess
        import sys

        (tmp_path / "app.py").write_text(
            "from nitro import Nitro\n"
            "app = Nitro(http='1')\n"
            "@app.route('/broken/<bad:value>')\n"
            "async def broken(request, value):\n"
            "    return None\n"
        )
        finished = subprocess.run(
            [sys.executable, "-m", "nitro.cli", "run", "app:app", "-p", "0"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert finished.returncode != 0
        assert "bad" in (finished.stdout + finished.stderr)


class TestFileResponses:
    @staticmethod
    def file_app(directory) -> str:
        return f"""
            from nitro import Nitro

            app = Nitro(http="1", log_level="warning")
            root = {str(directory)!r}

            @app.route("/whole")
            async def whole(request):
                scope, protocol = request.scope, request.protocol
                protocol.response_file(200, [], f"{{root}}/data.txt")

            @app.route("/typed")
            async def typed(request):
                scope, protocol = request.scope, request.protocol
                protocol.response_file(200, [], f"{{root}}/page.html")

            @app.route("/overridden")
            async def overridden(request):
                scope, protocol = request.scope, request.protocol
                protocol.response_file(
                    200, [("content-type", "text/plain")], f"{{root}}/page.html"
                )

            @app.route("/missing")
            async def missing(request):
                scope, protocol = request.scope, request.protocol
                protocol.response_file(200, [], f"{{root}}/not-there.txt")

            @app.route("/directory")
            async def directory(request):
                scope, protocol = request.scope, request.protocol
                protocol.response_file(200, [], root)

            @app.route("/range/<int:start>/<int:end>")
            async def ranged(request, start, end):
                scope, protocol = request.scope, request.protocol
                protocol.response_file_range(200, [], f"{{root}}/data.txt", start, end)

            @app.route("/tail/<int:start>")
            async def tail(request, start):
                scope, protocol = request.scope, request.protocol
                protocol.response_file_range(200, [], f"{{root}}/data.txt", start)

            @app.route("/large")
            async def large(request):
                scope, protocol = request.scope, request.protocol
                protocol.response_file(200, [], f"{{root}}/large.bin")
        """

    @pytest.fixture
    def files(self, tmp_path):
        (tmp_path / "data.txt").write_bytes(b"0123456789")
        (tmp_path / "page.html").write_bytes(b"<h1>hi</h1>")
        (tmp_path / "large.bin").write_bytes(bytes(index % 251 for index in range(300_000)))
        return tmp_path

    def test_a_whole_file_is_sent(self, server_factory, files):
        server = server_factory(self.file_app(files))
        response = server.request("/whole")

        assert response.status == 200
        assert response.body == b"0123456789"
        assert response.headers["content-length"] == "10"
        assert response.headers["accept-ranges"] == "bytes"
        assert "last-modified" in response.headers
        server.stop()

    def test_the_content_type_comes_from_the_file(self, server_factory, files):
        server = server_factory(self.file_app(files))
        assert server.request("/typed").headers["content-type"] == "text/html"
        server.stop()

    def test_a_handler_can_override_the_content_type(self, server_factory, files):
        server = server_factory(self.file_app(files))
        assert server.request("/overridden").headers["content-type"] == "text/plain"
        server.stop()

    def test_a_missing_file_is_a_404(self, server_factory, files):
        server = server_factory(self.file_app(files))
        assert server.request("/missing").status == 404
        server.stop()

    def test_a_directory_is_not_served(self, server_factory, files):
        server = server_factory(self.file_app(files))
        assert server.request("/directory").status == 500
        server.stop()

    def test_a_range_is_answered_with_206_and_a_content_range(self, server_factory, files):
        server = server_factory(self.file_app(files))
        response = server.request("/range/2/5")

        assert response.status == 206
        assert response.body == b"2345"
        assert response.headers["content-range"] == "bytes 2-5/10"
        assert response.headers["content-length"] == "4"
        server.stop()

    def test_an_open_ended_range_runs_to_the_last_byte(self, server_factory, files):
        server = server_factory(self.file_app(files))
        response = server.request("/tail/7")

        assert response.status == 206
        assert response.body == b"789"
        assert response.headers["content-range"] == "bytes 7-9/10"
        server.stop()

    def test_the_last_byte_is_reachable(self, server_factory, files):
        server = server_factory(self.file_app(files))
        response = server.request("/range/9/9")

        assert response.status == 206
        assert response.body == b"9"
        server.stop()

    def test_an_end_past_the_file_is_clamped(self, server_factory, files):
        server = server_factory(self.file_app(files))
        response = server.request("/range/8/100")

        assert response.status == 206
        assert response.body == b"89"
        assert response.headers["content-range"] == "bytes 8-9/10"
        server.stop()

    def test_a_start_past_the_file_is_a_416(self, server_factory, files):
        server = server_factory(self.file_app(files))
        response = server.request("/tail/50")

        assert response.status == 416, "a range past the end must not look satisfied"
        assert response.headers["content-range"] == "bytes */10"
        assert response.body == b""
        server.stop()

    def test_an_inverted_range_is_a_416(self, server_factory, files):
        server = server_factory(self.file_app(files))
        assert server.request("/range/8/2").status == 416
        server.stop()

    def test_a_large_file_arrives_intact(self, server_factory, files):
        server = server_factory(self.file_app(files))
        response = server.request("/large")

        assert response.status == 200
        assert len(response.body) == 300_000
        assert response.body == (files / "large.bin").read_bytes()
        server.stop()


class TestStreamingResponses:
    STREAM_APP = """
        import asyncio
        from nitro import Nitro

        app = Nitro(http="1", log_level="warning", stream_queue_capacity=2)

        @app.route("/stream")
        async def stream(request):
            transport = request.protocol.response_stream(
                200, [("content-type", "text/plain")]
            )
            for index in range(5):
                await transport.send_str(f"chunk-{index}\\n")
            transport.close()

        @app.route("/backpressure")
        async def backpressure(request):
            transport = request.protocol.response_stream(200, [])
            for index in range(200):
                await transport.send_bytes(b"x" * 1024)
            transport.close()

        @app.route("/closed-transport")
        async def closed_transport(request):
            transport = request.protocol.response_stream(200, [])
            await transport.send_str("first")
            transport.close()
            try:
                await transport.send_str("second")
            except RuntimeError as error:
                app.last_error = str(error)

        @app.route("/mixed")
        async def mixed(request):
            transport = request.protocol.response_stream(200, [])
            await transport.send_bytes(b"bytes ")
            await transport.send_str("and text")
            transport.close()

        @app.route("/response-class")
        async def response_class(request):
            from nitro.protocols import StreamingResponse

            async def produce():
                for index in range(3):
                    yield f"piece-{index}"

            return StreamingResponse(produce(), content_type="text/plain")
    """

    def test_chunks_arrive_in_order(self, server_factory):
        server = server_factory(self.STREAM_APP)
        response = server.request("/stream")

        assert response.status == 200
        assert response.text == "".join(f"chunk-{index}\n" for index in range(5))
        assert "content-length" not in response.headers
        server.stop()

    def test_bytes_and_text_can_be_mixed(self, server_factory):
        server = server_factory(self.STREAM_APP)
        assert server.request("/mixed").text == "bytes and text"
        server.stop()

    def test_a_producer_faster_than_the_client_still_delivers_everything(self, server_factory):
        server = server_factory(self.STREAM_APP)
        response = server.request("/backpressure")

        assert response.status == 200
        assert len(response.body) == 200 * 1024
        server.stop()

    def test_sending_after_close_is_refused(self, server_factory):
        server = server_factory(self.STREAM_APP)
        assert server.request("/closed-transport").text == "first"
        server.stop()

    def test_a_streaming_response_object_is_written(self, server_factory):
        server = server_factory(self.STREAM_APP)
        assert server.request("/response-class").text == "piece-0piece-1piece-2"
        server.stop()


class TestWebSockets:
    SOCKET_APP = """
        from nitro import Nitro

        app = Nitro(http="1", log_level="warning")

        @app.websocket("/echo")
        async def echo(socket):
            await socket.accept()
            async for message in socket:
                if isinstance(message, str):
                    await socket.send_text(f"echo:{message}")
                else:
                    await socket.send_bytes(message)

        @app.websocket("/rooms/<slug:room>")
        async def show_room(socket, room):
            await socket.accept()
            await socket.send_text(f"room={room} path={socket.path} scheme={socket.scheme}")
            await socket.close()

        @app.websocket("/pick")
        async def pick(socket):
            offered = list(socket.subprotocols)
            await socket.accept(offered[0] if offered else None)
            await socket.send_text(f"offered={offered}")
            await socket.close()

        @app.websocket("/refuse")
        async def refuse(socket):
            await socket.reject(403, "no entry")

        @app.websocket("/boom")
        async def boom(socket):
            raise RuntimeError("handler exploded")

        @app.route("/plain")
        async def plain(request):
            scope, protocol = request.scope, request.protocol
            protocol.response_str(200, [], "plain")
    """

    @staticmethod
    def run_client(coroutine):
        import asyncio

        return asyncio.run(asyncio.wait_for(coroutine, timeout=20))

    def test_messages_round_trip(self, server_factory):
        import websockets

        server = server_factory(self.SOCKET_APP)

        async def exchange():
            async with websockets.connect(f"ws://127.0.0.1:{server.port}/echo") as socket:
                await socket.send("hello")
                text = await socket.recv()
                await socket.send(b"\x01\x02")
                binary = await socket.recv()
                return text, binary

        text, binary = self.run_client(exchange())
        assert text == "echo:hello"
        assert binary == b"\x01\x02"
        server.stop()

    def test_the_scope_carries_path_parameters(self, server_factory):
        import websockets

        server = server_factory(self.SOCKET_APP)

        async def exchange():
            async with websockets.connect(f"ws://127.0.0.1:{server.port}/rooms/lobby") as socket:
                return await socket.recv()

        assert self.run_client(exchange()) == "room=lobby path=/rooms/lobby scheme=ws"
        server.stop()

    def test_a_subprotocol_is_negotiated(self, server_factory):
        import websockets

        server = server_factory(self.SOCKET_APP)

        async def exchange():
            async with websockets.connect(
                f"ws://127.0.0.1:{server.port}/pick", subprotocols=["chat", "superchat"]
            ) as socket:
                return socket.subprotocol, await socket.recv()

        selected, greeting = self.run_client(exchange())
        assert selected == "chat"
        assert greeting == "offered=['chat', 'superchat']"
        server.stop()

    def test_a_refused_upgrade_reports_its_status(self, server_factory):
        import websockets

        server = server_factory(self.SOCKET_APP)

        async def exchange():
            async with websockets.connect(f"ws://127.0.0.1:{server.port}/refuse"):
                pass

        with pytest.raises(websockets.exceptions.InvalidStatus) as failure:
            self.run_client(exchange())
        assert failure.value.response.status_code == 403
        server.stop()

    def test_an_unknown_path_is_refused(self, server_factory):
        import websockets

        server = server_factory(self.SOCKET_APP)

        async def exchange():
            async with websockets.connect(f"ws://127.0.0.1:{server.port}/nowhere"):
                pass

        with pytest.raises(websockets.exceptions.InvalidStatus) as failure:
            self.run_client(exchange())
        assert failure.value.response.status_code == 404
        server.stop()

    def test_a_failing_handler_does_not_leave_the_client_waiting(self, server_factory):
        import websockets

        server = server_factory(self.SOCKET_APP)

        async def exchange():
            async with websockets.connect(f"ws://127.0.0.1:{server.port}/boom"):
                pass

        with pytest.raises(websockets.exceptions.InvalidStatus) as failure:
            self.run_client(exchange())
        assert failure.value.response.status_code == 500
        server.stop()

    def test_a_handler_can_read_and_write_at_the_same_time(self, server_factory):
        """Reading and writing must not wait on each other.

        A handler with one task pushing messages out while another waits for
        them to come in is the usual shape; if the two directions shared a lock
        the pushing task would never get a turn.
        """
        import websockets

        server = server_factory(
            """
            import asyncio
            from nitro import Nitro

            app = Nitro(http="1", log_level="warning")

            @app.websocket("/duplex")
            async def duplex(socket):
                await socket.accept()

                async def heartbeat():
                    for index in range(100):
                        await socket.send_text(f"tick-{index}")
                        await asyncio.sleep(0.01)

                pump = asyncio.create_task(heartbeat())
                try:
                    # This waits for a message that will not come until the
                    # client has seen several ticks.
                    async for incoming in socket:
                        await socket.send_text(f"echo:{incoming}")
                        break
                finally:
                    pump.cancel()
            """
        )

        async def exchange():
            url = f"ws://127.0.0.1:{server.port}/duplex"
            async with websockets.connect(url) as socket:
                ticks = [await asyncio.wait_for(socket.recv(), timeout=5) for _ in range(3)]

                await socket.send("now")
                while True:
                    message = await asyncio.wait_for(socket.recv(), timeout=5)
                    if message.startswith("echo:"):
                        return ticks, message

        ticks, echo = self.run_client(exchange())

        assert ticks == ["tick-0", "tick-1", "tick-2"]
        assert echo == "echo:now"
        server.stop()

    def test_ordinary_requests_still_work(self, server_factory):
        server = server_factory(self.SOCKET_APP)
        assert server.request("/plain").text == "plain"
        server.stop()


class TestIntercomOverWebSocket:
    """The in-process fast path: a socket handler reaching Intercom directly."""

    RELAY_APP = """
        import asyncio
        from nitro import Nitro
        from nitro.intercom import intercom, new_channel
        from nitro.settings import settings

        settings.INTERCOMS = {{
            "default": {{
                "LOCATION": "redis://127.0.0.1:6379",
                "OPTIONS": {{"PREFIX": {prefix!r}}},
            }}
        }}

        app = Nitro(http="1", log_level="warning")

        @app.websocket("/rooms/<slug:room>")
        async def relay(socket, room):
            await socket.accept()
            channel = new_channel("socket")

            async with intercom.listen(channel, groups=[room]) as messages:
                # Let the subscription settle before anyone publishes to it.
                await asyncio.sleep(0.2)
                await socket.send_text("ready")

                async def outbound():
                    async for message in messages:
                        await socket.send_text(message["text"])

                pump = asyncio.create_task(outbound())
                try:
                    async for incoming in socket:
                        await intercom.group_publish(room, {{"text": incoming}})
                finally:
                    pump.cancel()
    """

    def redis_available(self):
        import socket

        with socket.socket() as probe:
            probe.settimeout(1.0)
            try:
                probe.connect(("127.0.0.1", 6379))
            except OSError:
                return False
        return True

    def test_two_sockets_in_a_room_reach_each_other(self, server_factory, tmp_path):
        import asyncio

        import websockets

        if not self.redis_available():
            pytest.skip("no Redis at 127.0.0.1:6379")

        prefix = f"nitro-test:relay:{tmp_path.name}"
        server = server_factory(self.RELAY_APP.format(prefix=prefix))

        async def exchange():
            url = f"ws://127.0.0.1:{server.port}/rooms/lobby"
            async with websockets.connect(url) as first, websockets.connect(url) as second:
                assert await first.recv() == "ready"
                assert await second.recv() == "ready"

                await first.send("hello from first")

                # Both sockets are in the room, so both see it.
                return [
                    await asyncio.wait_for(first.recv(), timeout=10),
                    await asyncio.wait_for(second.recv(), timeout=10),
                ]

        received = asyncio.run(asyncio.wait_for(exchange(), timeout=30))
        assert received == ["hello from first", "hello from first"]
        server.stop()
