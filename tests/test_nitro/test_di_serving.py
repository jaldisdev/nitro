"""Dependency injection through a real request.

`test_di.py` covers the resolver on its own. These cover the part that had
no test: that the request path actually calls it. Without them, `Depends`
resolved perfectly in isolation while a served handler received the marker
object itself.
"""


class TestHandlerDependencies:
    def test_a_dependency_is_supplied_to_a_handler(self, server_factory):
        server = server_factory(
            """
            from nitro import Nitro
            from nitro.di import Depends
            from nitro.protocols import PlainTextResponse

            app = Nitro(http="1", log_level="warning")

            async def get_greeting() -> str:
                return "from the dependency"

            @app.route("/")
            async def index(request, greeting: str = Depends(get_greeting)):
                return PlainTextResponse(greeting)
            """,
        )

        assert server.request("/").text == "from the dependency"
        server.stop()

    def test_a_synchronous_dependency_is_supplied(self, server_factory):
        server = server_factory(
            """
            from nitro import Nitro
            from nitro.di import Depends
            from nitro.protocols import PlainTextResponse

            app = Nitro(http="1", log_level="warning")

            def get_value() -> str:
                return "plain"

            @app.route("/")
            async def index(request, value: str = Depends(get_value)):
                return PlainTextResponse(value)
            """,
        )

        assert server.request("/").text == "plain"
        server.stop()

    def test_a_dependency_can_ask_for_the_request(self, server_factory):
        server = server_factory(
            """
            from nitro import Nitro
            from nitro.di import Depends
            from nitro.protocols import PlainTextResponse

            app = Nitro(http="1", log_level="warning")

            async def get_agent(request) -> str:
                return request.headers.get("user-agent", "none")

            @app.route("/")
            async def index(request, agent: str = Depends(get_agent)):
                return PlainTextResponse(agent)
            """,
        )

        assert server.request("/").text != "none"
        server.stop()

    def test_dependencies_coexist_with_path_parameters(self, server_factory):
        server = server_factory(
            """
            from nitro import Nitro
            from nitro.di import Depends
            from nitro.protocols import PlainTextResponse

            app = Nitro(http="1", log_level="warning")

            async def get_suffix() -> str:
                return "!"

            @app.route("/users/<int:user_id>")
            async def show(request, user_id: int, suffix: str = Depends(get_suffix)):
                return PlainTextResponse(f"{user_id}{suffix}")
            """,
        )

        assert server.request("/users/42").text == "42!"
        server.stop()

    def test_a_dependency_named_twice_resolves_once_per_request(self, server_factory):
        server = server_factory(
            """
            from nitro import Nitro
            from nitro.di import Depends
            from nitro.protocols import PlainTextResponse

            app = Nitro(http="1", log_level="warning")

            calls = 0

            async def get_connection() -> int:
                global calls
                calls += 1
                return calls

            async def get_repository(connection: int = Depends(get_connection)) -> int:
                return connection

            @app.route("/")
            async def index(
                request,
                connection: int = Depends(get_connection),
                repository: int = Depends(get_repository),
            ):
                # Both reached the same call, so one request opened one.
                return PlainTextResponse(f"{connection}:{repository}")
            """,
        )

        assert server.request("/").text == "1:1"
        # A second request gets its own value rather than the first's.
        assert server.request("/").text == "2:2"
        server.stop()


class TestEndpointDependencies:
    def test_a_verb_method_receives_its_dependency(self, server_factory):
        server = server_factory(
            """
            from nitro import Nitro
            from nitro.di import Depends
            from nitro.endpoints import HTTPEndpoint
            from nitro.protocols import PlainTextResponse
            from nitro.routing import HTTPRoute

            async def get_greeting() -> str:
                return "endpoint dependency"

            class Endpoint(HTTPEndpoint):
                async def get(self, request, greeting: str = Depends(get_greeting)):
                    return PlainTextResponse(greeting)

            app = Nitro(routes=[HTTPRoute("/", Endpoint)], http="1", log_level="warning")
            """,
        )

        assert server.request("/").text == "endpoint dependency"
        server.stop()
