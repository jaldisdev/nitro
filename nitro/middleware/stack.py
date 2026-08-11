import importlib
from typing import Any, Awaitable, Callable

from nitro.di import cache_for, dependencies_for, resolve_dependencies
from nitro.middleware.base import Middleware, MiddlewareProtocol


async def _supplied(hook: Callable[..., Any], connection: Any) -> dict[str, Any]:
    """Values for the parameters `hook` wants injected, or nothing.

    Resolved against the connection's own cache, so a dependency a middleware
    and a handler both name is produced once for the request rather than once
    for each.
    """
    graph = dependencies_for(hook)
    if not graph:
        return {}
    return await resolve_dependencies(graph, connection, cache_for(connection))


class MiddlewareStack:
    """
    Manages a stack of middleware instances.

    Loads middleware from settings.MIDDLEWARE and provides methods to
    execute them in order based on the protocol type.
    """

    def __init__(self, app: Any, middleware_paths: list[str] | None = None):
        """
        Initialize middleware stack.

        Args:
            app: Application instance
            middleware_paths: List of import paths to middleware classes
                            If None, loads from settings.MIDDLEWARE
        """
        self.app = app
        self.middleware_instances: list[Middleware] = []

        if middleware_paths is None:
            from nitro.settings import settings

            middleware_paths = getattr(settings, "MIDDLEWARE", [])

        self._load_middleware(middleware_paths)

    def _load_middleware(self, middleware_paths: list[str]) -> None:
        """
        Load middleware classes from import paths.

        Args:
            middleware_paths: List of dot-separated import paths
        """
        for path in middleware_paths:
            try:
                # Split into module and class name
                module_path, class_name = path.rsplit(".", 1)

                # Import the module
                module = importlib.import_module(module_path)

                # Get the middleware class
                middleware_class = getattr(module, class_name)

                # Instantiate the middleware
                instance = middleware_class(app=self.app)

                if not isinstance(instance, Middleware):
                    raise TypeError(
                        f"Middleware {path} must inherit from nitro.middleware.Middleware"
                    )

                for protocol in (
                    MiddlewareProtocol.HTTP,
                    MiddlewareProtocol.WEBSOCKET,
                    MiddlewareProtocol.WEBTRANSPORT,
                ):
                    hook = getattr(instance, f"__{protocol}__", None)
                    if hook is not None:
                        dependencies_for(hook)

                self.middleware_instances.append(instance)

            except (ImportError, AttributeError) as e:
                raise ImportError(f"Could not import middleware {path}: {e}")

    async def execute_http(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """
        Execute HTTP middleware stack.

        Args:
            request: HTTP request object
            handler: Final route handler

        Returns:
            HttpResponse from handler or middleware
        """
        return await self._execute_stack(MiddlewareProtocol.HTTP, request, handler)

    async def execute_websocket(
        self, websocket: Any, handler: Callable[[Any], Awaitable[None]]
    ) -> None:
        """
        Execute WebSocket middleware stack.

        Args:
            websocket: WebSocket connection
            handler: Final route handler
        """
        await self._execute_stack(MiddlewareProtocol.WEBSOCKET, websocket, handler)

    async def execute_webtransport(
        self, session: Any, handler: Callable[[Any], Awaitable[None]]
    ) -> None:
        """
        Execute WebTransport middleware stack.

        Args:
            session: WebTransport session
            handler: Final route handler
        """
        await self._execute_stack(MiddlewareProtocol.WEBTRANSPORT, session, handler)

    async def _execute_stack(
        self, protocol: str, connection: Any, handler: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """
        Execute middleware stack for a specific protocol.

        Builds a chain of middleware that support the protocol and executes
        them in order, with each middleware calling the next via call_next.

        Args:
            protocol: Protocol type ('http', 'websocket', 'webtransport')
            connection: Connection object
            handler: Final handler to call after all middleware

        Returns:
            Result from handler or middleware
        """
        # Filter middleware that support this protocol
        applicable_middleware = [
            m for m in self.middleware_instances if m.has_protocol_support(protocol)
        ]

        # Build the middleware chain from the end to the beginning
        async def build_chain(index: int) -> Callable:
            """Recursively build the middleware call chain."""
            if index >= len(applicable_middleware):
                # End of chain - call the actual handler
                return handler

            middleware = applicable_middleware[index]
            next_handler = await build_chain(index + 1)

            async def wrapped_handler(conn):
                """Wrapped handler that executes middleware with context manager."""
                # Use async context manager if needed
                async with middleware:
                    # Get the protocol-specific method or fallback to __call__
                    method_name = f"__{protocol}__"

                    if hasattr(middleware, method_name):
                        method = getattr(middleware, method_name)
                        # Check if it's actually implemented
                        try:
                            impl = getattr(middleware.__class__, method_name, None)
                            base_impl = getattr(Middleware, method_name, None)
                            if impl is not None and impl is not base_impl:
                                supplied = await _supplied(method, conn)
                                return await method(conn, next_handler, **supplied)
                        except (AttributeError, NotImplementedError):
                            pass

                    # Try universal __call__
                    if hasattr(middleware, "__call__"):
                        try:
                            impl = getattr(middleware.__class__, "__call__", None)
                            base_impl = getattr(Middleware, "__call__", None)
                            if impl is not None and impl is not base_impl:
                                supplied = await _supplied(middleware, conn)
                                return await middleware(conn, next_handler, **supplied)
                        except (AttributeError, NotImplementedError):
                            pass

                    # If we get here, just call next (shouldn't happen due to filtering)
                    return await next_handler(conn)

            return wrapped_handler

        # Build and execute the chain
        chain = await build_chain(0)
        return await chain(connection)

    def add_middleware(self, middleware: Middleware) -> None:
        """
        Add a middleware instance to the stack.

        Args:
            middleware: Middleware instance to add
        """
        if not isinstance(middleware, Middleware):
            raise TypeError("middleware must be an instance of Middleware")
        self.middleware_instances.append(middleware)

    def clear(self) -> None:
        """Remove all middleware from the stack."""
        self.middleware_instances.clear()

    def __len__(self) -> int:
        """Return the number of middleware in the stack."""
        return len(self.middleware_instances)

    def __repr__(self) -> str:
        """String representation of the middleware stack."""
        middleware_names = [m.__class__.__name__ for m in self.middleware_instances]
        return f"<MiddlewareStack: {', '.join(middleware_names)}>"
