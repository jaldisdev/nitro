"""The middleware stack.

Built once from the ``MIDDLEWARE`` setting, or from the paths an application was
constructed with. Which hook each middleware answers a protocol with is decided
here, from the class, and the chain is then a plain series of calls: nothing in
this module catches an exception on a middleware's behalf, so a failure inside
one reaches the application's error handling the way a failure in the handler
does.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from typing import Any

from nitro.di import cache_for, dependencies_for, resolve_dependencies
from nitro.middleware.base import (
    PROTOCOL_HOOKS,
    UNIVERSAL_HOOK,
    Middleware,
    MiddlewareProtocol,
)

__all__ = ["MiddlewareStack"]

Handler = Callable[[Any], Awaitable[Any]]


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
    """The middleware an application runs, in the order it runs them.

    Outermost first: the first entry sees a connection before the second, and
    its answer is the last one out.
    """

    def __init__(self, app: Any, middleware_paths: list[str] | None = None) -> None:
        self.app = app
        self.middleware_instances: list[Middleware] = []

        if middleware_paths is None:
            from nitro.settings import settings

            middleware_paths = list(settings.MIDDLEWARE)

        for path in middleware_paths:
            self.add_middleware(self._build(path))

    def _build(self, path: str) -> Middleware:
        """Import and instantiate the middleware named by `path`.

        A module that cannot be imported and a class that is not in it are
        reported separately, and an exception from the constructor is left
        alone: a middleware that fails to build is a broken configuration, not
        a missing one.
        """
        module_path, separator, class_name = path.rpartition(".")
        if not separator:
            raise ImportError(
                f"middleware {path!r} is not an import path; expected 'module.ClassName'"
            )

        try:
            module = importlib.import_module(module_path)
        except ImportError as error:
            raise ImportError(f"could not import middleware {path!r}: {error}") from error

        try:
            middleware_class = getattr(module, class_name)
        except AttributeError as error:
            raise ImportError(f"{module_path!r} has no {class_name!r}") from error

        return middleware_class(app=self.app)

    def add_middleware(self, middleware: Middleware) -> None:
        """Append `middleware` to the stack, reading its dependency graphs.

        The graphs are read now rather than on the first connection, so a cycle
        or an uninspectable signature stops the application from starting
        instead of failing one request.
        """
        if not isinstance(middleware, Middleware):
            raise TypeError(
                f"{type(middleware).__name__} is not a Middleware; "
                "subclass nitro.middleware.Middleware"
            )

        for protocol in PROTOCOL_HOOKS:
            hook = self._hook_for(middleware, protocol)
            if hook is not None:
                dependencies_for(hook)

        self.middleware_instances.append(middleware)

    @staticmethod
    def _hook_for(middleware: Middleware, protocol: str) -> Callable[..., Any] | None:
        """The bound method `middleware` answers `protocol` with, or `None`.

        Decided from the class rather than by calling anything: the protocol
        hook if it was overridden, the universal one if it was, nothing
        otherwise.
        """
        middleware_class = type(middleware)
        hook_name = PROTOCOL_HOOKS[protocol]

        if middleware_class.implements(hook_name):
            return getattr(middleware, hook_name)
        if middleware_class.implements(UNIVERSAL_HOOK):
            return middleware.__call__
        return None

    async def execute_http(self, request: Any, handler: Handler) -> Any:
        """Run the HTTP chain, ending in `handler`."""
        return await self._execute(MiddlewareProtocol.HTTP, request, handler)

    async def execute_websocket(self, websocket: Any, handler: Handler) -> None:
        """Run the WebSocket chain, ending in `handler`."""
        await self._execute(MiddlewareProtocol.WEBSOCKET, websocket, handler)

    async def execute_webtransport(self, session: Any, handler: Handler) -> None:
        """Run the WebTransport chain, ending in `handler`."""
        await self._execute(MiddlewareProtocol.WEBTRANSPORT, session, handler)

    async def _execute(self, protocol: str, connection: Any, handler: Handler) -> Any:
        chain = handler
        for middleware in reversed(self.middleware_instances):
            hook = self._hook_for(middleware, protocol)
            if hook is not None:
                chain = self._wrap(middleware, hook, chain)
        return await chain(connection)

    @staticmethod
    def _wrap(
        middleware: Middleware,
        hook: Callable[..., Any],
        next_handler: Handler,
    ) -> Handler:
        """`hook` as a handler, with the middleware entered around it."""

        async def call(connection: Any) -> Any:
            async with middleware:
                supplied = await _supplied(hook, connection)
                return await hook(connection, next_handler, **supplied)

        return call

    def clear(self) -> None:
        """Remove every middleware from the stack."""
        self.middleware_instances.clear()

    def __len__(self) -> int:
        return len(self.middleware_instances)

    def __repr__(self) -> str:
        names = ", ".join(type(each).__name__ for each in self.middleware_instances)
        return f"<MiddlewareStack: {names}>"
