import asyncio

import pytest

from nitro import Nitro


class RecordingProtocol:
    """Stands in for the compiled protocol object."""

    def __init__(self, body: bytes = b""):
        self.status: int | None = None
        self.headers: list[tuple[str, str]] = []
        self.body: bytes | None = None
        self._request_body = body

    async def __call__(self) -> bytes:
        return self._request_body

    def response_str(self, status, headers=(), body=""):
        self.status = status
        self.headers = list(headers)
        self.body = body.encode()

    def response_bytes(self, status, headers=(), body=b""):
        self.status = status
        self.headers = list(headers)
        self.body = bytes(body)

    def response_empty(self, status, headers=()):
        self.status = status
        self.headers = list(headers)
        self.body = b""

    def header(self, name: str) -> str | None:
        for key, value in self.headers:
            if key.lower() == name:
                return value
        return None


class Scope:
    def __init__(self, method="GET", path="/"):
        self.method = method
        self.path = path


async def ok(scope, protocol):
    protocol.response_str(200, [("content-type", "text/plain")], f"handled {scope.path}")


class TestRouteRegistration:
    def test_a_route_is_registered_for_each_method(self):
        app = Nitro()
        app.add_route("/things", ok, methods=["GET", "POST"])
        assert app.routes == [("GET", "/things"), ("POST", "/things")]

    def test_the_decorator_returns_the_handler(self):
        app = Nitro()
        decorated = app.route("/")(ok)
        assert decorated is ok

    def test_a_path_must_be_absolute(self):
        with pytest.raises(ValueError, match="must start with"):
            Nitro().add_route("relative", ok)

    def test_a_handler_must_be_async(self):
        with pytest.raises(TypeError, match="async function"):
            Nitro().add_route("/", lambda scope, protocol: None)

    def test_a_duplicate_route_is_refused(self):
        app = Nitro()
        app.add_route("/", ok)
        with pytest.raises(ValueError, match="already registered"):
            app.add_route("/", ok)


class TestDispatch:
    async def test_a_matching_route_is_called(self):
        app = Nitro()
        app.add_route("/hello", ok)
        protocol = RecordingProtocol()

        await app.__handle_http__(Scope(path="/hello"), protocol)

        assert protocol.status == 200
        assert protocol.body == b"handled /hello"

    async def test_an_unknown_path_is_a_404(self):
        protocol = RecordingProtocol()
        await Nitro().__handle_http__(Scope(path="/missing"), protocol)
        assert protocol.status == 404

    async def test_a_known_path_with_another_method_is_a_405(self):
        app = Nitro()
        app.add_route("/only-post", ok, methods=["POST"])
        protocol = RecordingProtocol()

        await app.__handle_http__(Scope(method="DELETE", path="/only-post"), protocol)

        assert protocol.status == 405
        assert protocol.header("allow") == "POST"

    async def test_head_falls_back_to_get(self):
        app = Nitro()
        app.add_route("/page", ok, methods=["GET"])
        protocol = RecordingProtocol()

        await app.__handle_http__(Scope(method="HEAD", path="/page"), protocol)

        assert protocol.status == 200

    async def test_a_failing_handler_becomes_a_500(self, caplog):
        app = Nitro()

        @app.route("/boom")
        async def boom(scope, protocol):
            raise RuntimeError("handler exploded")

        protocol = RecordingProtocol()
        await app.__handle_http__(Scope(path="/boom"), protocol)

        assert protocol.status == 500
        assert "handler exploded" in caplog.text

    async def test_the_request_body_reaches_the_handler(self):
        app = Nitro()

        @app.route("/echo", methods=["POST"])
        async def echo(scope, protocol):
            protocol.response_bytes(200, [], await protocol())

        protocol = RecordingProtocol(body=b"payload")
        await app.__handle_http__(Scope(method="POST", path="/echo"), protocol)

        assert protocol.body == b"payload"


class TestLifecycle:
    def test_callbacks_run_in_registration_order(self):
        app = Nitro()
        calls: list[str] = []
        app.on_startup(lambda: calls.append("first"))
        app.on_startup(lambda: calls.append("second"))

        loop = asyncio.new_event_loop()
        try:
            app.__startup__(loop)
        finally:
            loop.close()

        assert calls == ["first", "second"]

    def test_an_async_callback_is_driven_to_completion(self):
        app = Nitro()
        finished = []

        @app.on_startup
        async def prepare():
            await asyncio.sleep(0)
            finished.append(True)

        loop = asyncio.new_event_loop()
        try:
            app.__startup__(loop)
        finally:
            loop.close()

        assert finished == [True]

    def test_shutdown_callbacks_run(self):
        app = Nitro()
        calls: list[str] = []
        app.on_shutdown(lambda: calls.append("closed"))

        loop = asyncio.new_event_loop()
        try:
            app.__shutdown__(loop)
        finally:
            loop.close()

        assert calls == ["closed"]


class TestServerOptions:
    def test_constructor_arguments_are_applied(self):
        assert Nitro(port=9100).server_options().port == 9100

    def test_an_explicit_override_beats_a_constructor_argument(self):
        assert Nitro(port=9100).server_options(port=9200).port == 9200

    def test_an_absent_override_leaves_the_constructor_argument(self):
        assert Nitro(port=9100).server_options(port=None).port == 9100
