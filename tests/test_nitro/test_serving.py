"""End-to-end tests against a real `nitro run` process."""

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
            async def echo(scope, protocol):
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
            async def cookies(scope, protocol):
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
            async def boom(scope, protocol):
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
            async def silent(scope, protocol):
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
            async def report(scope, protocol):
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
            async def index(scope, protocol):
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
            async def slow(scope, protocol):
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
            async def pid(scope, protocol):
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
            async def pid(scope, protocol):
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
