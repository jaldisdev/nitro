import asyncio
import sys
import types
import uuid

import pytest

from nitro.endpoints import HTTPEndpoint
from nitro.protocols import JSONResponse
from nitro.routing import (
    Converter,
    HTTPRoute,
    Mount,
    Router,
    WebSocketRoute,
    WebTransportRoute,
    converter_for,
    get_converters,
    load_patterns,
    register_converter,
)
from nitro.routing.router import parse_parameters
from nitro.settings import ImproperlyConfigured


async def handler(request):
    return None


class GetRequest:
    """The part of a request an endpoint reads to dispatch."""

    method = "GET"


@pytest.fixture
def patterns_module():
    """Build an importable routes module and yield its name."""
    created: list[str] = []

    def build(source: str, name: str = "test_patterns_module") -> str:
        module = types.ModuleType(name)
        module.__dict__.update(HTTPRoute=HTTPRoute, handler=handler)
        exec(compile(source, name, "exec"), module.__dict__)
        sys.modules[name] = module
        created.append(name)
        return name

    yield build

    for name in created:
        sys.modules.pop(name, None)


class TestConverters:
    def test_the_built_ins_are_registered(self):
        assert set(get_converters()) >= {"str", "int", "slug", "uuid", "path"}

    @pytest.mark.parametrize(
        ("name", "text", "expected"),
        [
            ("str", "anything", "anything"),
            ("int", "42", 42),
            ("slug", "a-slug_1", "a-slug_1"),
            (
                "uuid",
                "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
                uuid.UUID("3f2504e0-4f89-11d3-9a0c-0305e82c3301"),
            ),
            ("path", "deep/nested", "deep/nested"),
        ],
    )
    def test_conversion_to_python(self, name, text, expected):
        assert converter_for(name).to_python(text) == expected

    def test_only_the_path_converter_spans_separators(self):
        assert converter_for("path").spans_separators is True
        for name in ("str", "int", "slug", "uuid"):
            assert converter_for(name).spans_separators is False

    def test_an_unknown_converter_lists_the_known_ones(self):
        with pytest.raises(LookupError, match="slug"):
            converter_for("nonsense")

    def test_an_inline_expression_is_understood(self):
        converter = converter_for('regex("[a-z]{2}")')
        assert converter.regex == "[a-z]{2}"
        assert converter.spans_separators is False

    def test_an_inline_expression_must_be_quoted(self):
        with pytest.raises(ValueError, match="quoted"):
            converter_for("regex([a-z])")

    def test_a_custom_converter_can_be_registered(self):
        class HexConverter(Converter):
            regex = "[0-9a-f]+"

            def to_python(self, value):
                return int(value, 16)

        register_converter("hex", HexConverter)
        try:
            assert converter_for("hex").to_python("ff") == 255
        finally:
            get_converters().pop("hex", None)

    def test_registering_something_that_is_not_a_converter_is_refused(self):
        with pytest.raises(TypeError):
            register_converter("bad", object)


class TestParameterParsing:
    def test_a_static_path_has_no_parameters(self):
        assert parse_parameters("/about/") == {}

    def test_parameters_are_found_in_order(self):
        parsed = parse_parameters("/users/<int:identifier>/posts/<slug:title>")
        assert list(parsed) == ["identifier", "title"]
        assert parsed["identifier"].regex == "[0-9]+"

    def test_a_bare_parameter_uses_the_string_converter(self):
        assert parse_parameters("/users/<name>")["name"].regex == "[^/]+"

    def test_an_expression_containing_a_colon_keeps_its_name(self):
        parsed = parse_parameters('/tags/<regex("[a-z]+:[0-9]+"):tag>')
        assert list(parsed) == ["tag"]
        assert parsed["tag"].regex == "[a-z]+:[0-9]+"

    def test_a_repeated_name_is_refused(self):
        with pytest.raises(ValueError, match="more than once"):
            parse_parameters("/<str:name>/<int:name>")


class TestRouter:
    def test_a_route_is_registered(self):
        router = Router()
        route = router.add("/things", handler, methods=["GET"])
        assert route.id == 0
        assert route.methods == ("GET",)
        assert len(router) == 1

    def test_methods_are_upper_cased_and_deduplicated(self):
        route = Router().add("/things", handler, methods=["get", "GET", "post"])
        assert route.methods == ("GET", "POST")

    def test_a_path_must_be_absolute(self):
        with pytest.raises(ValueError, match="must start with"):
            Router().add("things", handler)

    def test_a_route_must_answer_something(self):
        with pytest.raises(ValueError, match="at least one method"):
            Router().add("/things", handler, methods=[])

    def test_a_clashing_method_on_the_same_path_is_refused(self):
        router = Router()
        router.add("/things", handler, methods=["GET"])
        with pytest.raises(ValueError, match="already registered"):
            router.add("/things", handler, methods=["GET", "POST"])

    def test_a_different_method_on_the_same_path_is_fine(self):
        router = Router()
        router.add("/things", handler, methods=["GET"])
        router.add("/things", handler, methods=["POST"])
        assert len(router) == 2

    def test_a_greedy_parameter_must_end_the_path(self):
        with pytest.raises(ValueError, match="must end the path"):
            Router().add("/files/<path:rest>/info", handler)

    def test_a_greedy_parameter_at_the_end_is_fine(self):
        assert Router().add("/files/<path:rest>", handler).path == "/files/<path:rest>"

    def test_a_repeated_route_name_is_refused(self):
        router = Router()
        router.add("/a", handler, name="thing")
        with pytest.raises(ValueError, match="already used"):
            router.add("/b", handler, name="thing")

    def test_a_prefix_is_applied_to_every_route(self):
        router = Router(prefix="/api")
        assert router.add("/things", handler).path == "/api/things"

    def test_the_decorator_registers_and_returns_the_handler(self):
        router = Router()
        assert router.route("/things")(handler) is handler
        assert len(router) == 1


class TestRouteTable:
    def test_a_static_route_reports_no_parameters(self):
        router = Router()
        router.add("/about", handler, methods=["GET"])
        assert router.table() == [(0, "/about", ("GET",), [])]

    def test_a_parameter_contributes_its_expression(self):
        router = Router()
        router.add("/users/<int:identifier>", handler, methods=["GET"])

        assert router.table() == [
            (0, "/users/<int:identifier>", ("GET",), [("identifier", "[0-9]+", False)])
        ]

    def test_a_greedy_parameter_is_marked(self):
        router = Router()
        router.add("/files/<path:rest>", handler, methods=["GET"])
        assert router.table()[0][3] == [("rest", ".+", True)]

    def test_identifiers_are_unique_and_stable(self):
        router = Router()
        for index in range(3):
            router.add(f"/route-{index}", handler)
        assert [entry[0] for entry in router.table()] == [0, 1, 2]


class TestConversion:
    def test_captured_text_becomes_python_values(self):
        router = Router()
        route = router.add("/users/<int:identifier>/<slug:tab>", handler)

        converted = route.convert({"identifier": "42", "tab": "posts"})
        assert converted == {"identifier": 42, "tab": "posts"}

    def test_an_unconvertible_value_raises(self):
        route = Router().add("/users/<int:identifier>", handler)
        with pytest.raises(ValueError):
            route.convert({"identifier": "not-a-number"})


class TestReverse:
    def test_a_named_route_builds_its_path(self):
        router = Router()
        router.add("/users/<int:identifier>/posts/<slug:title>", handler, name="post")

        assert router.url_for("post", identifier=42, title="hello") == "/users/42/posts/hello"

    def test_a_static_route_builds_its_path(self):
        router = Router()
        router.add("/about", handler, name="about")
        assert router.url_for("about") == "/about"

    def test_a_missing_value_is_reported(self):
        router = Router()
        router.add("/users/<int:identifier>", handler, name="user")
        with pytest.raises(KeyError, match="identifier"):
            router.url_for("user")

    def test_an_unknown_name_is_reported(self):
        with pytest.raises(LookupError, match="nowhere"):
            Router().url_for("nowhere")


class TestMount:
    def test_routes_are_attached_under_the_prefix(self):
        inner = Router()
        inner.add("/things", handler, methods=["GET"])
        inner.add("/things/<int:identifier>", handler, methods=["GET"])

        outer = Router()
        Mount("/api", inner).attach(outer)

        assert [route.path for route in outer] == [
            "/api/things",
            "/api/things/<int:identifier>",
        ]

    def test_a_trailing_slash_on_the_prefix_does_not_double_up(self):
        inner = Router()
        inner.add("/things", handler)

        outer = Router()
        Mount("/api/", inner).attach(outer)

        assert outer.routes[0].path == "/api/things"

    def test_a_prefix_must_be_absolute(self):
        with pytest.raises(ValueError, match="must start with"):
            Mount("api", Router())

    def test_a_mount_name_qualifies_route_names(self):
        inner = Router()
        inner.add("/things", handler, name="list")

        outer = Router()
        Mount("/api", inner, name="api").attach(outer)

        assert outer.url_for("api:list") == "/api/things"

    def test_mounts_can_nest(self):
        leaf = Router()
        leaf.add("/things", handler)

        middle = Router()
        Mount("/v1", leaf).attach(middle)

        outer = Router()
        Mount("/api", middle).attach(outer)

        assert outer.routes[0].path == "/api/v1/things"


class TestDeclarations:
    def test_a_route_declaration_registers_itself(self):
        router = Router()
        HTTPRoute("/things", handler, name="things").attach(router)

        route = router.by_name("things")
        assert route.path == "/things"
        assert route.methods == ("GET", "HEAD")

    def test_declared_methods_are_kept(self):
        router = Router()
        HTTPRoute("/things", handler, methods=["POST"]).attach(router)
        assert router.routes[0].methods == ("POST",)

    def test_an_endpoint_class_answers_the_verbs_it_defines(self):
        class Things(HTTPEndpoint):
            async def get(self, request):
                return None

            async def post(self, request):
                return None

        router = Router()
        HTTPRoute("/things", Things).attach(router)
        assert set(router.routes[0].methods) == {"GET", "POST", "HEAD"}

    def test_an_endpoint_class_is_instantiated_per_request(self):
        seen = []

        class Things(HTTPEndpoint):
            def __init__(self):
                seen.append(self)

            async def get(self, request):
                return JSONResponse({})

        router = Router()
        HTTPRoute("/things", Things).attach(router)
        registered = router.routes[0].handler

        asyncio.run(registered(GetRequest()))
        asyncio.run(registered(GetRequest()))
        assert len(seen) == 2 and seen[0] is not seen[1]

    def test_path_parameters_reach_an_endpoint(self):
        class Things(HTTPEndpoint):
            async def get(self, request, identifier):
                return JSONResponse({"identifier": identifier})

        router = Router()
        HTTPRoute("/things/<int:identifier>", Things).attach(router)
        response = asyncio.run(router.routes[0].handler(GetRequest(), identifier=7))
        assert response.body == b'{"identifier": 7}'

    def test_a_class_that_is_not_an_endpoint_is_refused(self):
        class NotAnEndpoint:
            pass

        with pytest.raises(TypeError, match="not an endpoint class"):
            HTTPRoute("/things", NotAnEndpoint).attach(Router())

    def test_socket_declarations_use_their_own_methods(self):
        router = Router()
        WebSocketRoute("/socket", handler).attach(router)
        WebTransportRoute("/session", handler).attach(router)

        assert [route.methods for route in router] == [("WEBSOCKET",), ("WEBTRANSPORT",)]

    def test_declarations_can_be_mounted(self):
        router = Router()
        Mount(
            "/api",
            [HTTPRoute("/things", handler, name="things")],
            name="api",
        ).attach(router)

        assert router.url_for("api:things") == "/api/things"

    def test_mounted_declarations_nest(self):
        router = Router()
        Mount(
            "/api",
            [Mount("/v1", [HTTPRoute("/things", handler, name="things")], name="v1")],
            name="api",
        ).attach(router)

        assert router.url_for("api:v1:things") == "/api/v1/things"

    def test_a_router_includes_declarations(self):
        router = Router()
        router.include([HTTPRoute("/things", handler, name="things")])
        assert router.by_name("things").path == "/things"

    def test_something_that_is_not_a_route_is_reported(self):
        with pytest.raises(TypeError, match="not a route"):
            Router().include(["/things"])


class TestLoadingPatterns:
    def test_declarations_are_read_from_a_module(self, patterns_module):
        module = patterns_module("patterns = [HTTPRoute('/things', handler, name='things')]")
        loaded = load_patterns(module)
        assert [declaration.name for declaration in loaded] == ["things"]

    def test_a_list_is_taken_as_given(self):
        declarations = [HTTPRoute("/things", handler)]
        assert load_patterns(declarations) == declarations

    def test_a_module_that_cannot_be_imported_is_reported(self):
        with pytest.raises(ImproperlyConfigured, match="could not be imported"):
            load_patterns("no.such.module")

    def test_a_module_without_patterns_is_reported(self, patterns_module):
        module = patterns_module("routes = []")
        with pytest.raises(ImproperlyConfigured, match="does not define `patterns`"):
            load_patterns(module)
