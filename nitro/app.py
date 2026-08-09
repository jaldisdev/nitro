"""The application object.

A Nitro application is what the bundled server calls into. It exposes the
protocol entry points the server looks for — ``__handle_http__`` for requests,
``__startup__`` and ``__shutdown__`` for the worker lifetime — and holds the
route table those entry points consult.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any

from nitro.settings import ServerOptions

logger = logging.getLogger("nitro")

Handler = Callable[..., Awaitable[None]]
LifecycleCallback = Callable[[], Any]

_DEFAULT_METHODS: tuple[str, ...] = ("GET", "HEAD")


class Nitro:
    """A Nitro application.

    Keyword arguments are server options and take precedence over the project's
    ``SERVER`` setting, so a value passed here always wins.
    """

    def __init__(self, **options: Any) -> None:
        self._routes: dict[tuple[str, str], Handler] = {}
        self._startup_callbacks: list[LifecycleCallback] = []
        self._shutdown_callbacks: list[LifecycleCallback] = []
        self._option_overrides = options

    # ── route registration ───────────────────────────────────────────────────

    def add_route(
        self,
        path: str,
        handler: Handler,
        *,
        methods: Sequence[str] = _DEFAULT_METHODS,
    ) -> None:
        """Register `handler` for `path`.

        The handler is called with the request scope and the protocol object,
        and is responsible for sending a response through the latter.
        """
        if not path.startswith("/"):
            raise ValueError(f"route path must start with '/', got {path!r}")
        if not inspect.iscoroutinefunction(handler):
            raise TypeError(f"handler for {path!r} must be an async function")

        for method in methods:
            key = (method.upper(), path)
            if key in self._routes:
                raise ValueError(f"{method.upper()} {path} is already registered")
            self._routes[key] = handler

    def route(
        self, path: str, *, methods: Sequence[str] = _DEFAULT_METHODS
    ) -> Callable[[Handler], Handler]:
        """Decorator form of :meth:`add_route`."""

        def register(handler: Handler) -> Handler:
            self.add_route(path, handler, methods=methods)
            return handler

        return register

    def include(self, routes: Iterable[tuple[str, Handler]]) -> None:
        for path, handler in routes:
            self.add_route(path, handler)

    @property
    def routes(self) -> list[tuple[str, str]]:
        """Every registered route, as ``(method, path)`` pairs."""
        return sorted(self._routes)

    # ── lifecycle registration ───────────────────────────────────────────────

    def on_startup(self, callback: LifecycleCallback) -> LifecycleCallback:
        self._startup_callbacks.append(callback)
        return callback

    def on_shutdown(self, callback: LifecycleCallback) -> LifecycleCallback:
        self._shutdown_callbacks.append(callback)
        return callback

    # ── server configuration ─────────────────────────────────────────────────

    def server_options(self, **overrides: Any) -> ServerOptions:
        """The server configuration for this application.

        Precedence runs from the project's ``SERVER`` setting, through the
        keyword arguments the application was constructed with, to `overrides`.
        An override of ``None`` is dropped rather than applied, so a command
        line flag that was not given cannot erase a constructor argument.
        """
        supplied = {name: value for name, value in overrides.items() if value is not None}
        return ServerOptions.resolve(**{**self._option_overrides, **supplied})

    # ── protocol entry points ────────────────────────────────────────────────

    async def __handle_http__(self, scope: Any, protocol: Any) -> None:
        handler = self._routes.get((scope.method, scope.path))

        if handler is None:
            if scope.method == "HEAD" and ("GET", scope.path) in self._routes:
                handler = self._routes[("GET", scope.path)]
            elif any(path == scope.path for _method, path in self._routes):
                protocol.response_str(
                    405,
                    [("content-type", "text/plain; charset=utf-8"), ("allow", self._allowed(scope.path))],
                    "Method Not Allowed",
                )
                return
            else:
                protocol.response_str(
                    404, [("content-type", "text/plain; charset=utf-8")], "Not Found"
                )
                return

        try:
            await handler(scope, protocol)
        except Exception:
            logger.exception("handler for %s %s failed", scope.method, scope.path)
            protocol.response_str(
                500, [("content-type", "text/plain; charset=utf-8")], "Internal Server Error"
            )

    def _allowed(self, path: str) -> str:
        methods = sorted(method for method, route in self._routes if route == path)
        return ", ".join(methods)

    def __startup__(self, loop: asyncio.AbstractEventLoop) -> None:
        """Run startup callbacks before the worker accepts anything.

        The loop exists but is not running yet, so an asynchronous callback is
        driven to completion here rather than scheduled.
        """
        self._run_callbacks(loop, self._startup_callbacks, "startup")

    def __shutdown__(self, loop: asyncio.AbstractEventLoop) -> None:
        """Run shutdown callbacks after the worker has stopped serving."""
        self._run_callbacks(loop, self._shutdown_callbacks, "shutdown")

    def _run_callbacks(
        self,
        loop: asyncio.AbstractEventLoop,
        callbacks: list[LifecycleCallback],
        stage: str,
    ) -> None:
        for callback in callbacks:
            result = callback()
            if inspect.isawaitable(result):
                loop.run_until_complete(result)
            logger.debug("%s callback %s completed", stage, getattr(callback, "__name__", callback))

    def __repr__(self) -> str:
        return f"Nitro(routes={len(self._routes)})"
