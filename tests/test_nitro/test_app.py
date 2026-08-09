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
    """Stands in for the compiled scope object.

    Matching has already happened by the time an application sees a scope, so a
    scope carries the route it belongs to rather than the app working it out.
    """

    def __init__(self, method="GET", path="/", route_id=None, path_params=None, allowed=()):
        self.method = method
        self.path = path
        self.route_id = route_id
        self.path_params = path_params or {}
        self.allowed_methods = tuple(allowed)


def scope_for(app, method, path, **path_params):
    """Build a scope the way the matcher would for `method` and `path`."""
    for route in app.routes:
        if route.path != path or method not in route.methods:
            continue
        return Scope(method, path, route_id=route.id, path_params=path_params)

    allowed = sorted({m for route in app.routes if route.path == path for m in route.methods})
    return Scope(method, path, allowed=allowed)


async def ok(scope, protocol):
    protocol.response_str(200, [("content-type", "text/plain")], f"handled {scope.path}")


class TestRouteRegistration:
    def test_a_route_records_every_method(self):
        app = Nitro()
        app.add_route("/things", ok, methods=["GET", "POST"])
        assert [(route.path, route.methods) for route in app.routes] == [
            ("/things", ("GET", "POST"))
        ]

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

        await app.__handle_http__(scope_for(app, "GET", "/hello"), protocol)

        assert protocol.status == 200
        assert protocol.body == b"handled /hello"

    async def test_path_parameters_arrive_as_converted_keywords(self):
        app = Nitro()
        seen = {}

        @app.route("/users/<int:user_id>/<slug:tab>")
        async def show(scope, protocol, user_id, tab):
            seen["user_id"] = user_id
            seen["tab"] = tab
            protocol.response_empty(204)

        scope = scope_for(app, "GET", "/users/<int:user_id>/<slug:tab>", user_id="42", tab="posts")
        await app.__handle_http__(scope, protocol := RecordingProtocol())

        assert protocol.status == 204
        assert seen == {"user_id": 42, "tab": "posts"}

    async def test_a_parameter_the_converter_rejects_is_a_404(self):
        app = Nitro()

        @app.route("/items/<uuid:identifier>")
        async def show(scope, protocol, identifier):
            protocol.response_empty(204)

        scope = scope_for(app, "GET", "/items/<uuid:identifier>", identifier="not-a-uuid")
        await app.__handle_http__(scope, protocol := RecordingProtocol())

        assert protocol.status == 404

    async def test_an_unknown_path_is_a_404(self):
        protocol = RecordingProtocol()
        await Nitro().__handle_http__(Scope(path="/missing"), protocol)
        assert protocol.status == 404

    async def test_a_known_path_with_another_method_is_a_405(self):
        app = Nitro()
        app.add_route("/only-post", ok, methods=["POST"])
        protocol = RecordingProtocol()

        await app.__handle_http__(
            Scope(method="DELETE", path="/only-post", allowed=["POST"]), protocol
        )

        assert protocol.status == 405
        assert protocol.header("allow") == "POST"

    async def test_a_failing_handler_becomes_a_500(self, caplog):
        app = Nitro()

        @app.route("/boom")
        async def boom(scope, protocol):
            raise RuntimeError("handler exploded")

        protocol = RecordingProtocol()
        await app.__handle_http__(scope_for(app, "GET", "/boom"), protocol)

        assert protocol.status == 500
        assert "handler exploded" in caplog.text

    async def test_the_request_body_reaches_the_handler(self):
        app = Nitro()

        @app.route("/echo", methods=["POST"])
        async def echo(scope, protocol):
            protocol.response_bytes(200, [], await protocol())

        protocol = RecordingProtocol(body=b"payload")
        await app.__handle_http__(scope_for(app, "POST", "/echo"), protocol)

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


class TestProtocolRoutes:
    async def accepting(self, scope, transport):
        await transport.accept()

    def test_a_websocket_route_uses_its_own_method(self):
        app = Nitro()
        route = app.add_websocket_route("/socket", self.accepting)
        assert route.methods == ("WEBSOCKET",)

    def test_a_webtransport_route_uses_its_own_method(self):
        app = Nitro()
        route = app.add_webtransport_route("/session", self.accepting)
        assert route.methods == ("WEBTRANSPORT",)

    def test_the_three_protocols_can_share_a_path(self):
        app = Nitro()
        app.add_route("/thing", ok)
        app.add_websocket_route("/thing", self.accepting)
        app.add_webtransport_route("/thing", self.accepting)

        assert {route.methods for route in app.routes} == {
            ("GET", "HEAD"),
            ("WEBSOCKET",),
            ("WEBTRANSPORT",),
        }

    def test_a_protocol_handler_must_be_async(self):
        app = Nitro()
        with pytest.raises(TypeError, match="async function"):
            app.add_websocket_route("/socket", lambda scope, transport: None)
        with pytest.raises(TypeError, match="async function"):
            app.add_webtransport_route("/session", lambda scope, session: None)


class RecordingSocket:
    """Stands in for the compiled WebSocket transport."""

    def __init__(self, connected=False):
        self.connected = connected
        self.rejected: tuple[int, str] | None = None
        self.closed: tuple[int, str] | None = None

    async def reject(self, status=403, reason=""):
        self.rejected = (status, reason)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    async def accept(self, subprotocol=None):
        self.connected = True


class WsScope:
    def __init__(self, path="/socket", route_id=None, path_params=None):
        self.path = path
        self.route_id = route_id
        self.path_params = path_params or {}


class TestProtocolDispatch:
    async def test_an_unknown_websocket_path_is_refused(self):
        transport = RecordingSocket()
        await Nitro().__handle_ws__(WsScope(), transport)
        assert transport.rejected == (404, "Not Found")

    async def test_a_websocket_handler_receives_path_parameters(self):
        app = Nitro()
        seen = {}

        @app.websocket("/rooms/<int:room_id>")
        async def room(scope, transport, room_id):
            seen["room_id"] = room_id
            await transport.accept()

        route = app.routes[0]
        transport = RecordingSocket()
        await app.__handle_ws__(
            WsScope(path=route.path, route_id=route.id, path_params={"room_id": "7"}),
            transport,
        )

        assert seen == {"room_id": 7}
        assert transport.connected is True

    async def test_a_failing_websocket_handler_closes_an_open_connection(self):
        app = Nitro()

        @app.websocket("/boom")
        async def boom(scope, transport):
            await transport.accept()
            raise RuntimeError("handler exploded")

        route = app.routes[0]
        transport = RecordingSocket()
        await app.__handle_ws__(WsScope(path=route.path, route_id=route.id), transport)

        assert transport.closed == (1011, "handler failed")

    async def test_a_failing_websocket_handler_refuses_before_accepting(self):
        app = Nitro()

        @app.websocket("/boom")
        async def boom(scope, transport):
            raise RuntimeError("handler exploded")

        route = app.routes[0]
        transport = RecordingSocket()
        await app.__handle_ws__(WsScope(path=route.path, route_id=route.id), transport)

        assert transport.rejected == (500, "Internal Server Error")

    async def test_an_unknown_webtransport_path_is_refused(self):
        class Session(RecordingSocket):
            async def reject(self, status=403):
                self.rejected = status

        session = Session()
        await Nitro().__handle_wt__(WsScope(), session)
        assert session.rejected == 404
