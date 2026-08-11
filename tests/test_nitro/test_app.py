import asyncio
import sys
import types

import pytest

from nitro import Nitro
from nitro.di import Depends, reset_worker_dependencies, worker_scoped
from nitro.middleware.base import Middleware
from nitro.endpoints import HTTPEndpoint
from nitro.endpoints import HTTPEndpoint
from nitro.protocols import Http404, HttpForbidden, PlainTextResponse
from nitro.routing import HTTPRoute
from nitro.settings import ImproperlyConfigured, settings


@pytest.fixture
def routes_module():
    """An importable module defining `patterns`, as a project would write it."""
    name = "test_app_routes"
    module = types.ModuleType(name)
    module.patterns = [HTTPRoute("/things", ok, name="things")]
    sys.modules[name] = module
    yield name
    sys.modules.pop(name, None)


@pytest.fixture
def handlers_module():
    """A routes module carrying `exception_handlers` beside its `patterns`."""
    name = "test_app_handlers"
    module = types.ModuleType(name)
    module.patterns = [HTTPRoute("/things", ok, name="things")]
    module.not_found = ok
    module.exception_handlers = {404: ok}
    sys.modules[name] = module
    yield name
    sys.modules.pop(name, None)


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


async def ok(request):
    scope, protocol = request.scope, request.protocol
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

    def test_an_endpoint_instance_is_accepted(self):
        class Things(HTTPEndpoint):
            async def get(self, request):
                return None

        app = Nitro()
        app.add_route("/things", Things())
        assert len(app.routes) == 1


class TestRouteTableFromSettings:
    def test_declarations_are_registered(self):
        app = Nitro(routes=[HTTPRoute("/things", ok, name="things")])
        assert app.url_for("things") == "/things"

    def test_the_routes_module_is_read_from_settings(self, routes_module, monkeypatch):
        monkeypatch.setattr(settings, "ROUTES", routes_module, raising=False)
        assert Nitro().url_for("things") == "/things"

    def test_the_argument_wins_over_the_setting(self, routes_module, monkeypatch):
        monkeypatch.setattr(settings, "ROUTES", routes_module, raising=False)
        app = Nitro(routes=[HTTPRoute("/others", ok, name="others")])

        assert app.url_for("others") == "/others"
        with pytest.raises(LookupError):
            app.url_for("things")

    def test_an_unconfigured_project_still_serves_its_decorators(self, monkeypatch):
        monkeypatch.setattr(settings, "ROUTES", [], raising=False)
        app = Nitro()
        app.route("/", name="index")(ok)

        assert app.url_for("index") == "/"

    def test_decorators_add_to_the_configured_table(self, routes_module, monkeypatch):
        monkeypatch.setattr(settings, "ROUTES", routes_module, raising=False)
        app = Nitro()
        app.route("/extra", name="extra")(ok)

        assert [route.path for route in app.routes] == ["/things", "/extra"]


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
        async def show(request, user_id, tab):
            scope, protocol = request.scope, request.protocol
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
        async def show(request, identifier):
            scope, protocol = request.scope, request.protocol
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
        async def boom(request):
            scope, protocol = request.scope, request.protocol
            raise RuntimeError("handler exploded")

        protocol = RecordingProtocol()
        await app.__handle_http__(scope_for(app, "GET", "/boom"), protocol)

        assert protocol.status == 500
        assert "handler exploded" in caplog.text

    async def test_the_request_body_reaches_the_handler(self):
        app = Nitro()

        @app.route("/echo", methods=["POST"])
        async def echo(request):
            scope, protocol = request.scope, request.protocol
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
        async def room(socket, room_id):
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
        async def boom(socket):
            await transport.accept()
            raise RuntimeError("handler exploded")

        route = app.routes[0]
        transport = RecordingSocket()
        await app.__handle_ws__(WsScope(path=route.path, route_id=route.id), transport)

        assert transport.closed == (1011, "handler failed")

    async def test_a_failing_websocket_handler_refuses_before_accepting(self):
        app = Nitro()

        @app.websocket("/boom")
        async def boom(socket):
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


class TestDebugPages:
    async def test_an_unmatched_path_is_plain_by_default(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", False, raising=False)
        app = Nitro()
        protocol = RecordingProtocol()

        await app.__handle_http__(scope_for(app, "GET", "/missing"), protocol)

        assert protocol.status == 404
        assert protocol.body == b"Not Found"

    async def test_debug_answers_a_404_with_the_routes_it_tried(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", True, raising=False)
        app = Nitro()
        app.add_route("/things/<int:identifier>", ok)
        protocol = RecordingProtocol()

        await app.__handle_http__(scope_for(app, "GET", "/missing"), protocol)

        assert protocol.status == 404
        assert protocol.header("content-type") == "text/html; charset=utf-8"
        page = protocol.body.decode()
        assert "/things/&lt;int:identifier&gt;" in page
        assert "/missing" in page

    async def test_debug_answers_a_500_with_the_traceback(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", True, raising=False)
        app = Nitro()

        @app.route("/boom")
        async def boom(request):
            raise ValueError("a distinctive message")

        protocol = RecordingProtocol()
        await app.__handle_http__(scope_for(app, "GET", "/boom"), protocol)

        assert protocol.status == 500
        page = protocol.body.decode()
        assert "ValueError" in page
        assert "a distinctive message" in page

    async def test_a_500_stays_plain_without_debug(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", False, raising=False)
        app = Nitro()

        @app.route("/boom")
        async def boom(request):
            raise ValueError("a distinctive message")

        protocol = RecordingProtocol()
        await app.__handle_http__(scope_for(app, "GET", "/boom"), protocol)

        assert protocol.status == 500
        assert protocol.body == b"Internal Server Error"
        assert b"distinctive" not in protocol.body

    async def test_a_raised_404_reaches_the_page_too(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", True, raising=False)
        app = Nitro()

        @app.route("/gone")
        async def gone(request):
            raise Http404()

        protocol = RecordingProtocol()
        await app.__handle_http__(scope_for(app, "GET", "/gone"), protocol)

        assert protocol.status == 404
        assert protocol.header("content-type") == "text/html; charset=utf-8"

    async def test_another_status_keeps_its_own_answer(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", True, raising=False)
        app = Nitro()

        @app.route("/nope")
        async def nope(request):
            raise HttpForbidden({"reason": "not yours"})

        protocol = RecordingProtocol()
        await app.__handle_http__(scope_for(app, "GET", "/nope"), protocol)

        assert protocol.status == 403
        assert b"not yours" in protocol.body

    async def test_a_405_is_not_replaced_by_the_404_page(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", True, raising=False)
        app = Nitro()
        app.add_route("/things", ok, methods=["GET"])
        protocol = RecordingProtocol()

        await app.__handle_http__(scope_for(app, "POST", "/things"), protocol)

        assert protocol.status == 405
        assert protocol.body == b"Method Not Allowed"


class TestExceptionHandlers:
    async def test_a_status_handler_answers(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", False, raising=False)

        async def gone(request, exception):
            return PlainTextResponse("custom 404", status_code=404)

        app = Nitro(exception_handlers={404: gone})
        protocol = RecordingProtocol()

        await app.__handle_http__(scope_for(app, "GET", "/missing"), protocol)

        assert protocol.status == 404
        assert protocol.body == b"custom 404"

    async def test_an_exception_class_handler_answers(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", False, raising=False)

        async def forbidden(request, exception):
            return PlainTextResponse("nope", status_code=403)

        app = Nitro(exception_handlers={HttpForbidden: forbidden})

        @app.route("/private")
        async def private(request):
            raise HttpForbidden()

        protocol = RecordingProtocol()
        await app.__handle_http__(scope_for(app, "GET", "/private"), protocol)

        assert protocol.status == 403
        assert protocol.body == b"nope"

    async def test_a_handler_sees_the_path_that_missed(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", False, raising=False)
        seen = {}

        async def gone(request, exception):
            seen["path"] = request.path
            return PlainTextResponse("gone", status_code=404)

        app = Nitro(exception_handlers={404: gone})
        await app.__handle_http__(scope_for(app, "GET", "/nowhere"), RecordingProtocol())

        assert seen["path"] == "/nowhere"

    async def test_a_handler_wins_over_the_debug_page(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", True, raising=False)

        async def gone(request, exception):
            return PlainTextResponse("custom 404", status_code=404)

        app = Nitro(exception_handlers={404: gone})
        protocol = RecordingProtocol()

        await app.__handle_http__(scope_for(app, "GET", "/missing"), protocol)
        assert protocol.body == b"custom 404"

    async def test_a_failing_handler_falls_back(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", False, raising=False)

        async def broken(request, exception):
            raise RuntimeError("the handler itself is broken")

        app = Nitro(exception_handlers={404: broken})
        protocol = RecordingProtocol()

        await app.__handle_http__(scope_for(app, "GET", "/missing"), protocol)

        assert protocol.status == 404
        assert protocol.body == b"Not Found"

    async def test_a_500_handler_answers(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", False, raising=False)

        async def failed(request, exception):
            return PlainTextResponse(f"sorry: {exception}", status_code=500)

        app = Nitro(exception_handlers={500: failed})

        @app.route("/boom")
        async def boom(request):
            raise ValueError("deliberate")

        protocol = RecordingProtocol()
        await app.__handle_http__(scope_for(app, "GET", "/boom"), protocol)

        assert protocol.status == 500
        assert protocol.body == b"sorry: deliberate"

    async def test_a_405_can_be_answered(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", False, raising=False)

        async def wrong_method(request, exception):
            return PlainTextResponse("no such verb", status_code=405)

        app = Nitro(exception_handlers={405: wrong_method})
        app.add_route("/things", ok, methods=["GET"])

        protocol = RecordingProtocol()
        await app.__handle_http__(scope_for(app, "POST", "/things"), protocol)

        assert protocol.status == 405
        assert protocol.body == b"no such verb"

    async def test_an_unhandled_status_keeps_its_answer(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", False, raising=False)

        async def gone(request, exception):
            return PlainTextResponse("custom 404", status_code=404)

        app = Nitro(exception_handlers={404: gone})

        @app.route("/private")
        async def private(request):
            raise HttpForbidden({"reason": "not yours"})

        protocol = RecordingProtocol()
        await app.__handle_http__(scope_for(app, "GET", "/private"), protocol)

        assert protocol.status == 403
        assert b"not yours" in protocol.body

    def test_handlers_come_from_the_routes_module(self, handlers_module):
        app = Nitro(routes=handlers_module)
        assert app.exception_handlers.get_handler(Http404()) is not None

    def test_the_constructor_overrides_the_routes_module(self, handlers_module):
        async def mine(request, exception):
            return None

        app = Nitro(routes=handlers_module, exception_handlers={404: mine})
        assert app.exception_handlers.get_handler(Http404()) is mine

    def test_a_handler_may_be_named_as_an_import_path(self, handlers_module):
        app = Nitro(exception_handlers={404: f"{handlers_module}.not_found"})
        assert app.exception_handlers.get_handler(Http404()) is ok

    def test_a_key_that_is_not_a_status_or_exception_is_refused(self):
        with pytest.raises(ImproperlyConfigured, match="status code or an exception class"):
            Nitro(exception_handlers={"404": ok})

    def test_a_status_outside_the_range_is_refused(self):
        with pytest.raises(ImproperlyConfigured, match="not a status"):
            Nitro(exception_handlers={9000: ok})

    def test_a_class_that_is_not_an_exception_is_refused(self):
        with pytest.raises(ImproperlyConfigured, match="not an exception class"):
            Nitro(exception_handlers={dict: ok})

    def test_an_unimportable_handler_is_refused(self):
        with pytest.raises(ImproperlyConfigured, match="could not be imported"):
            Nitro(exception_handlers={404: "nowhere.at.all"})

    def test_a_handler_that_is_not_async_is_refused(self):
        with pytest.raises(ImproperlyConfigured, match="not an async function"):
            Nitro(exception_handlers={404: lambda request, exception: None})


class TestDebugFlag:
    def test_it_follows_the_setting_by_default(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", True, raising=False)
        assert Nitro().debug is True

        monkeypatch.setattr(settings, "DEBUG", False, raising=False)
        assert Nitro().debug is False

    def test_the_argument_wins_over_the_setting(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", False, raising=False)
        assert Nitro(debug=True).debug is True

        monkeypatch.setattr(settings, "DEBUG", True, raising=False)
        assert Nitro(debug=False).debug is False

    def test_it_is_read_again_rather_than_frozen(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", False, raising=False)
        app = Nitro()

        monkeypatch.setattr(settings, "DEBUG", True, raising=False)
        assert app.debug is True

    async def test_it_decides_whether_the_debug_page_is_shown(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", False, raising=False)
        app = Nitro(debug=True)
        protocol = RecordingProtocol()

        await app.__handle_http__(scope_for(app, "GET", "/missing"), protocol)

        assert protocol.status == 404
        assert protocol.header("content-type") == "text/html; charset=utf-8"

    async def test_it_can_switch_the_page_off_while_the_setting_is_on(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", True, raising=False)
        app = Nitro(debug=False)
        protocol = RecordingProtocol()

        await app.__handle_http__(scope_for(app, "GET", "/missing"), protocol)

        assert protocol.status == 404
        assert protocol.body == b"Not Found"


class TestDependencyRelease:
    """The release wired into dispatch, rather than the cache on its own."""

    async def test_a_dependency_is_released_after_the_response(self):
        app = Nitro()
        trail = []

        async def get_connection():
            trail.append("acquired")
            yield "connection"
            trail.append("released")

        @app.route("/thing")
        async def handler(request, connection=Depends(get_connection)):
            trail.append(f"handler saw {connection}")
            request.protocol.response_empty(204)

        await app.__handle_http__(scope_for(app, "GET", "/thing"), RecordingProtocol())

        assert trail == ["acquired", "handler saw connection", "released"]

    async def test_a_failing_handler_reports_the_failure_to_its_dependency(self):
        app = Nitro()
        outcome = []

        async def get_transaction():
            try:
                yield "transaction"
            except RuntimeError:
                outcome.append("rolled back")
                raise

        @app.route("/boom")
        async def handler(request, transaction=Depends(get_transaction)):
            raise RuntimeError("the handler failed")

        await app.__handle_http__(scope_for(app, "GET", "/boom"), protocol := RecordingProtocol())

        assert outcome == ["rolled back"]
        assert protocol.status == 500

    async def test_an_endpoint_releases_what_its_method_opened(self):
        app = Nitro()
        trail = []

        async def get_connection():
            yield "connection"
            trail.append("released")

        class Endpoint(HTTPEndpoint):
            async def get(self, request, connection=Depends(get_connection)):
                trail.append(f"handler saw {connection}")
                request.protocol.response_empty(204)

        # Through the route table rather than `add_route`, which registers a
        # handler as it is given and so never wraps an endpoint class.
        app = Nitro(routes=[HTTPRoute("/endpoint", Endpoint)])

        await app.__handle_http__(scope_for(app, "GET", "/endpoint"), RecordingProtocol())

        assert trail == ["handler saw connection", "released"]


class TestWorkerScopedDependencies:
    def test_they_are_built_at_startup_and_released_at_shutdown(self):
        trail = []

        @worker_scoped
        async def get_pool():
            trail.append("opened")
            yield "pool"
            trail.append("closed")

        async def handler(request, pool=Depends(get_pool)):
            request.protocol.response_empty(204)

        app = Nitro()
        app.add_route("/thing", handler)

        loop = asyncio.new_event_loop()
        try:
            app.__startup__(loop)
            assert trail == ["opened"]  # before a single request

            loop.run_until_complete(
                app.__handle_http__(scope_for(app, "GET", "/thing"), RecordingProtocol())
            )
            assert trail == ["opened"]  # the request does not release it

            app.__shutdown__(loop)
            assert trail == ["opened", "closed"]
        finally:
            reset_worker_dependencies()
            loop.close()

    def test_one_value_is_shared_across_requests(self):
        seen = []

        @worker_scoped
        async def get_pool():
            return object()

        async def handler(request, pool=Depends(get_pool)):
            seen.append(pool)
            request.protocol.response_empty(204)

        app = Nitro()
        app.add_route("/thing", handler)

        loop = asyncio.new_event_loop()
        try:
            app.__startup__(loop)
            for _ in range(3):
                loop.run_until_complete(
                    app.__handle_http__(scope_for(app, "GET", "/thing"), RecordingProtocol())
                )
        finally:
            reset_worker_dependencies()
            loop.close()

        assert len(seen) == 3
        assert seen[0] is seen[1] is seen[2]


class TestMiddlewareDependencies:
    """What a middleware asks for, and how it relates to what the handler asks for."""

    def make_app(self, middleware, handler, path="/thing"):
        app = Nitro()
        app.middleware.add_middleware(middleware)
        app.add_route(path, handler)
        return app

    async def test_a_middleware_is_supplied_what_it_asks_for(self):
        seen = []

        async def get_account():
            return "account"

        class Auditing(Middleware):
            async def __http__(self, request, call_next, account=Depends(get_account)):
                seen.append(account)
                return await call_next(request)

        async def handler(request):
            request.protocol.response_empty(204)

        app = self.make_app(Auditing(), handler)
        await app.__handle_http__(scope_for(app, "GET", "/thing"), RecordingProtocol())

        assert seen == ["account"]

    async def test_a_dependency_is_produced_once_for_the_request(self):
        """The point of the connection owning its cache: a middleware and a
        handler naming one dependency get one value, not one each."""
        calls = []
        seen = []

        async def get_account():
            calls.append("called")
            return object()

        class Auditing(Middleware):
            async def __http__(self, request, call_next, account=Depends(get_account)):
                seen.append(account)
                return await call_next(request)

        async def handler(request, account=Depends(get_account)):
            seen.append(account)
            request.protocol.response_empty(204)

        app = self.make_app(Auditing(), handler)
        await app.__handle_http__(scope_for(app, "GET", "/thing"), RecordingProtocol())

        assert calls == ["called"]
        assert seen[0] is seen[1]

    async def test_it_is_not_shared_with_the_next_request(self):
        seen = []

        async def get_account():
            return object()

        class Auditing(Middleware):
            async def __http__(self, request, call_next, account=Depends(get_account)):
                seen.append(account)
                return await call_next(request)

        async def handler(request):
            request.protocol.response_empty(204)

        app = self.make_app(Auditing(), handler)
        for _ in range(2):
            await app.__handle_http__(scope_for(app, "GET", "/thing"), RecordingProtocol())

        assert seen[0] is not seen[1]

    async def test_what_a_middleware_opened_is_released_after_the_response(self):
        trail = []

        async def get_connection():
            trail.append("acquired")
            yield "connection"
            trail.append("released")

        class Auditing(Middleware):
            async def __http__(self, request, call_next, connection=Depends(get_connection)):
                response = await call_next(request)
                trail.append("middleware finished")
                return response

        async def handler(request):
            trail.append("handler ran")
            request.protocol.response_empty(204)

        app = self.make_app(Auditing(), handler)
        await app.__handle_http__(scope_for(app, "GET", "/thing"), RecordingProtocol())

        assert trail == ["acquired", "handler ran", "middleware finished", "released"]

    async def test_a_middleware_that_asks_for_nothing_is_untouched(self):
        calls = []

        class Plain(Middleware):
            async def __http__(self, request, call_next):
                calls.append("ran")
                return await call_next(request)

        async def handler(request):
            request.protocol.response_empty(204)

        app = self.make_app(Plain(), handler)
        await app.__handle_http__(scope_for(app, "GET", "/thing"), RecordingProtocol())

        assert calls == ["ran"]
