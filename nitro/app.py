#
# This source file is part of the Nitro open source project.
#
# Copyright (c) 2026 Jaldis B.V.
#
# Licensed under the MIT OR Apache-2.0 license (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://opensource.org/licenses/MIT
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""The application object.

A Nitro application is what the bundled server calls into. It exposes the
protocol entry points the server looks for — ``__handle_http__`` for requests,
``__startup__`` and ``__shutdown__`` for the worker lifetime — and holds the
route table those entry points consult.

Matching itself happens in the compiled matcher, which is given the route table
at startup. By the time a request reaches :meth:`Nitro.__handle_http__` the
route is already known; what is left is turning the captured text into Python
values and calling the handler.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any

from nitro.di import cache_for, existing_cache, resolve_dependencies
from nitro.middleware.stack import MiddlewareStack
from nitro.protocols.exceptions import (
    ExceptionHandlerRegistry,
    Http404,
    HttpException,
    HttpMethodNotAllowed,
)
from nitro.protocols.http import HttpRequest
from nitro.protocols.websocket import WebSocket
from nitro.protocols.webtransport import WebTransportSession
from nitro.routing.mount import Mount
from nitro.routing.patterns import (
    load_exception_handlers,
    load_patterns,
    normalise_exception_handlers,
)
from nitro.routing.reverse import set_active_router
from nitro.routing.router import (
    DEFAULT_METHODS,
    WEBSOCKET_METHOD,
    WEBTRANSPORT_METHOD,
    Route,
    Router,
    RouteTable,
)
from nitro.settings import ServerOptions

logger = logging.getLogger("nitro")

Handler = Callable[..., Awaitable[None]]
LifecycleCallback = Callable[[], Any]

_PLAIN_TEXT = ("content-type", "text/plain; charset=utf-8")


async def serve_http(application: Any, scope: Any, protocol: Any) -> None:
    """Run `application`'s HTTP entry point and say how it ended.

    The server schedules this on the event loop as one ordinary task, and hears
    back through `protocol` — the response the handler sent, or the fact that it
    returned or raised without sending one. That is the whole conversation, and
    it is why the server does not also need a future for the handler itself.

    The entry point answers its own exceptions; anything reaching here escaped
    that, so there is nothing left to do but report it and let the transport
    send a 500.
    """
    try:
        await application.__handle_http__(scope, protocol)
    except BaseException as error:
        logger.exception("serving a request failed")
        protocol._handler_failed(f"{type(error).__name__}: {error}")
    else:
        protocol._handler_ended()


def _full_path(scope: Any) -> str:
    """The path a request asked for, query string included."""
    path = getattr(scope, "path", "/")
    query = getattr(scope, "query_string", "") or ""
    if isinstance(query, bytes):
        query = query.decode("utf-8", errors="replace")
    return f"{path}?{query}" if query else path


def build_server(application: Any, **overrides: Any) -> tuple[Any, ServerOptions]:
    """The compiled server for `application`, and the options it was built with.

    Shared by :meth:`Nitro.serve` and the command line so the two cannot drift.
    `application` is duck-typed rather than required to be a `Nitro`, since the
    command line will serve anything that answers the protocol entry points.
    The options come back because the server does not hand them out again, and
    the caller needs them to describe what it is serving.
    """
    from nitro._nitro import Server

    resolver = getattr(application, "server_options", None)
    options = resolver(**overrides) if callable(resolver) else ServerOptions.resolve(**overrides)

    table = getattr(application, "route_table", None)
    return Server(application, options, table() if callable(table) else []), options


def served_addresses(server: Any, options: ServerOptions) -> list[str]:
    """The URLs `server` answers on, as a person would type them.

    The scheme follows whether TLS is terminated on the TCP socket: a
    certificate configured only for HTTP/3 leaves the TCP listener plaintext,
    and reporting `https` for it would send the reader somewhere that does not
    answer.
    """
    scheme = "https" if options.tls_cert and options.tls_tcp else "http"
    return [f"{scheme}://{host}:{port}" for host, port in server.addresses]


async def _dependencies(route: Route, context: Any) -> dict[str, Any]:
    """Values for the parameters `route`'s handler wants injected.

    The graph was read when the route was registered, so nothing is inspected
    here. The cache spans one request, which is what makes a dependency named
    twice in one request resolve once — and, because it belongs to the call
    rather than to the route, what stops one request seeing another's values.
    It also holds whatever the dependencies left open, so the caller drains it
    when the work it was made for is over.

    A route with nothing to inject never asks for a cache, so it never makes
    one: the common request pays for none of this.
    """
    if not route.dependencies:
        return {}
    return await resolve_dependencies(route.dependencies, context, cache_for(context))


async def _served(connection: Any, work: Any) -> Any:
    """Await `work`, then release whatever it resolved on the way.

    Outside the middleware stack rather than inside it: middleware resolves
    into the same cache the handler does, so a dependency a middleware opened
    is still open while the handler runs and is released once, here, when the
    connection has been served. The failure, if there was one, is what each
    dependency is told at its `yield`.
    """
    failure: BaseException | None = None
    try:
        return await work
    except BaseException as error:
        failure = error
        raise
    finally:
        cache = existing_cache(connection)
        if cache is not None:
            await cache.aclose(failure)


def _is_async_callable(handler: Any) -> bool:
    """Whether `handler` can be awaited when called.

    An endpoint instance is a callable object rather than a function, so asking
    only about the object itself would reject the class-based handlers the route
    table is free to hold.
    """
    if inspect.iscoroutinefunction(handler):
        return True
    # Not `callable()`: what matters is whether the *bound* `__call__` is a
    # coroutine function, which `callable` cannot tell you.
    call = getattr(handler, "__call__", None)  # noqa: B004
    return call is not None and inspect.iscoroutinefunction(call)


class Nitro:
    """A Nitro application.

    Keyword arguments are server options and take precedence over the project's
    ``SERVER`` setting.
    """

    def __init__(
        self,
        *,
        routes: str | Iterable[Any] | None = None,
        middleware: list[str] | None = None,
        exception_handlers: dict[type[Exception] | int, Any] | None = None,
        debug: bool | None = None,
        **options: Any,
    ) -> None:
        self.router = Router()
        set_active_router(self.router)
        self._startup_callbacks: list[LifecycleCallback] = []
        self._shutdown_callbacks: list[LifecycleCallback] = []
        self._option_overrides = options
        self._middleware_paths = middleware
        self._middleware: MiddlewareStack | None = None
        self._debug = debug
        self._exception_handlers = ExceptionHandlerRegistry()
        self._load_routes(routes, exception_handlers)

    @property
    def debug(self) -> bool:
        """Whether this application reports failures in detail.

        Read from the ``DEBUG`` setting unless the application was told
        directly. Resolved on each read rather than at construction, so a test
        that changes the setting does not have to rebuild the application.
        """
        if self._debug is not None:
            return self._debug

        from nitro.settings import settings

        return bool(settings.DEBUG)

    def _load_routes(
        self,
        routes: str | Iterable[Any] | None,
        exception_handlers: dict[type[Exception] | int, Any] | None,
    ) -> None:
        """Register the project's route table and its exception handlers.

        `routes` names a module defining ``patterns``, or is the declarations
        themselves; left out, it comes from the ``ROUTES`` setting. A project
        that configures neither is not an error — its routes are the ones its
        decorators register.

        The route module's handlers are registered before the ones given here,
        so a constructor argument overrides the project's own — the same
        direction as the server options.
        """
        if routes is None:
            from nitro.settings import settings

            routes = settings.ROUTES

        if routes:
            self.include(load_patterns(routes))
            self._register_handlers(load_exception_handlers(routes))

        if exception_handlers:
            self._register_handlers(
                normalise_exception_handlers(exception_handlers, "Nitro(exception_handlers=...)")
            )

    def _register_handlers(self, handlers: dict[Any, Any]) -> None:
        for key, handler in handlers.items():
            self._exception_handlers.add_handler(key, handler)

    @property
    def exception_handlers(self) -> ExceptionHandlerRegistry:
        """The handlers answering for particular statuses and exceptions."""
        return self._exception_handlers

    @property
    def middleware(self) -> MiddlewareStack:
        """The middleware stack, built from settings on first use."""
        if self._middleware is None:
            self._middleware = MiddlewareStack(self, self._middleware_paths)
        return self._middleware

    # ── route registration ───────────────────────────────────────────────────

    def add_route(
        self,
        path: str,
        handler: Handler,
        *,
        methods: Sequence[str] = DEFAULT_METHODS,
        name: str | None = None,
    ) -> Route:
        """Register `handler` for `path`.

        The handler is called with the request scope, the protocol object, and
        any captured path parameters as keyword arguments. Sending a response
        through the protocol is the handler's job.
        """
        if not _is_async_callable(handler):
            raise TypeError(f"handler for {path!r} must be an async function")
        return self.router.add(path, handler, methods=methods, name=name)

    def route(
        self,
        path: str,
        *,
        methods: Sequence[str] = DEFAULT_METHODS,
        name: str | None = None,
    ) -> Callable[[Handler], Handler]:
        """Decorator form of :meth:`add_route`."""

        def register(handler: Handler) -> Handler:
            self.add_route(path, handler, methods=methods, name=name)
            return handler

        return register

    def websocket(self, path: str, *, name: str | None = None) -> Callable[[Handler], Handler]:
        """Register a WebSocket handler for `path`.

        The handler is called with the connection scope and a transport it must
        either accept or reject before any messages can pass.
        """

        def register(handler: Handler) -> Handler:
            self.add_websocket_route(path, handler, name=name)
            return handler

        return register

    def add_websocket_route(self, path: str, handler: Handler, *, name: str | None = None) -> Route:
        if not _is_async_callable(handler):
            raise TypeError(f"WebSocket handler for {path!r} must be an async function")
        return self.router.add(path, handler, methods=[WEBSOCKET_METHOD], name=name)

    def webtransport(self, path: str, *, name: str | None = None) -> Callable[[Handler], Handler]:
        """Register a WebTransport handler for `path`.

        The handler is called with the session scope and a session it must
        either accept or reject before any traffic can pass.
        """

        def register(handler: Handler) -> Handler:
            self.add_webtransport_route(path, handler, name=name)
            return handler

        return register

    def add_webtransport_route(
        self, path: str, handler: Handler, *, name: str | None = None
    ) -> Route:
        if not _is_async_callable(handler):
            raise TypeError(f"WebTransport handler for {path!r} must be an async function")
        return self.router.add(path, handler, methods=[WEBTRANSPORT_METHOD], name=name)

    def mount(self, mount: Mount) -> None:
        """Attach a sub-router's routes under its prefix."""
        mount.attach(self.router)

    def include(self, routes: Iterable[Any] | Router) -> None:
        """Register route declarations, or another router's routes."""
        self.router.include(routes)

    @property
    def routes(self) -> list[Route]:
        return self.router.routes

    def route_table(self) -> RouteTable:
        """The route table, described for the compiled matcher."""
        return self.router.table()

    def url_for(self, name: str, **values: Any) -> str:
        return self.router.url_for(name, **values)

    # ── lifecycle registration ───────────────────────────────────────────────

    def on_startup(self, callback: LifecycleCallback) -> LifecycleCallback:
        self._startup_callbacks.append(callback)
        return callback

    def on_shutdown(self, callback: LifecycleCallback) -> LifecycleCallback:
        self._shutdown_callbacks.append(callback)
        return callback

    # ── server configuration ─────────────────────────────────────────────────

    def serve(self, **overrides: Any) -> None:
        """Start the bundled server and block until it stops.

        This is what `nitro app:app` does, reachable from Python so a script can
        be the entry point instead — a container's `CMD`, most often, where the
        application file being executable is worth more than a command line.

        Keyword arguments override the server options the same way the command
        line flags do.

            if __name__ == "__main__":
                app.serve()
        """
        server, options = build_server(self, **overrides)
        for address in served_addresses(server, options):
            # Flushed because stdout is a pipe under a process supervisor, and
            # an address that appears after the first request is no use to
            # whatever is waiting to send it.
            print(f"Serving on {address}", flush=True)
        if options.uds:
            print(f"Serving on {options.uds}", flush=True)
        server.serve()

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
        route = self.router.get(scope.route_id) if scope.route_id is not None else None

        if route is None:
            if scope.allowed_methods:
                allow = ", ".join(scope.allowed_methods)
                if not await self._answered(
                    scope, protocol, HttpMethodNotAllowed(headers={"Allow": allow})
                ):
                    protocol.response_str(
                        405,
                        [_PLAIN_TEXT, ("allow", allow)],
                        "Method Not Allowed",
                    )
            else:
                await self._not_found(scope, protocol)
            return

        try:
            parameters = route.convert(scope.path_params)
        except (ValueError, TypeError):
            # The matcher accepted the text but the converter could not turn it
            # into a value, so as far as the application is concerned the path
            # does not name anything.
            logger.debug("path parameters for %s could not be converted", scope.path, exc_info=True)
            await self._not_found(scope, protocol)
            return

        request = HttpRequest(scope, protocol, parameters)

        # With nothing to wrap the handler in and nothing to inject, the chain
        # the middleware stack builds is the handler itself and the dependency
        # resolution has nothing to resolve. Calling it directly is the same
        # work with three fewer coroutines to get there, and most requests take
        # this way. What still has to happen afterwards is the release, because
        # a handler can resolve dependencies of its own — an endpoint's method
        # does — so it keeps the same wrapper the long way round uses.
        direct = not self.middleware and not route.dependencies

        try:
            if direct:
                result = await _served(request, route.handler(request, **parameters))
            else:

                async def call_handler(request: HttpRequest) -> Any:
                    supplied = await _dependencies(route, request)
                    return await route.handler(request, **supplied, **parameters)

                result = await _served(request, self.middleware.execute_http(request, call_handler))
        except HttpException as exception:
            if not await self._answered(scope, protocol, exception, request):
                page = self._debug_page(scope, exception.status_code, exception)
                await (page or exception.as_response()).__http__(protocol)
        except Exception as exception:
            logger.exception("handler for %s %s failed", scope.method, scope.path)
            if not await self._answered(scope, protocol, exception, request):
                page = self._debug_page(scope, 500, exception)
                if page is not None:
                    await page.__http__(protocol)
                else:
                    protocol.response_str(500, [_PLAIN_TEXT], "Internal Server Error")
        else:
            # A handler may answer through the protocol itself and return
            # nothing; only a returned response has to be written here.
            if result is not None:
                await self._write(result, protocol)

    async def _not_found(self, scope: Any, protocol: Any) -> None:
        if await self._answered(scope, protocol, Http404()):
            return
        page = self._debug_page(scope, 404)
        if page is None:
            protocol.response_str(404, [_PLAIN_TEXT], "Not Found")
            return
        await page.__http__(protocol)

    async def _answered(
        self,
        scope: Any,
        protocol: Any,
        exception: BaseException,
        request: HttpRequest | None = None,
    ) -> bool:
        """Whether a registered handler answered `exception`.

        A path that matched nothing has no request of its own, so one is built
        here: a handler for a 404 is exactly the one most likely to want the
        path it was asked for.
        """
        handled, answer = await self._dispatch_exception(
            request if request is not None else HttpRequest(scope, protocol, {}), exception
        )
        if handled and answer is not None:
            await self._write(answer, protocol)
        return handled

    async def _dispatch_exception(self, target: Any, exception: BaseException) -> tuple[bool, Any]:
        """Run the handler registered for `exception`, if there is one.

        A handler that fails is logged and reported as absent, so the client
        still gets an answer for the original exception rather than for the one
        raised while describing it.
        """
        handler = self._exception_handlers.get_handler(exception)
        if handler is None and not isinstance(exception, HttpException):
            # An ordinary exception carries no status of its own, and the
            # answer it becomes is a 500, so that is the key it should reach.
            handler = self._exception_handlers.get_status_handler(500)
        if handler is None:
            return False, None
        try:
            return True, await handler(target, exception)
        except Exception:
            logger.exception("the handler for %s failed", type(exception).__name__)
            return False, None

    def _debug_page(
        self, scope: Any, status_code: int, exception: BaseException | None = None
    ) -> Any:
        """The debug page for `status_code`, or `None` when there is not one.

        Imported here rather than at module level: the pages pull in a template
        environment of their own, which a production application never needs.
        """
        from nitro.views.debug import debug_response

        try:
            return debug_response(
                status_code,
                getattr(scope, "method", "GET"),
                _full_path(scope),
                debug=self.debug,
                exception=exception,
                routes=[route.path for route in self.router],
            )
        except Exception:
            # The page is a convenience; failing to build it must not replace
            # the status the client is owed with a different one.
            logger.exception("the debug page for %s could not be rendered", status_code)
            return None

    async def _write(self, result: Any, protocol: Any) -> None:
        writer = getattr(result, "__http__", None)
        if writer is None:
            raise TypeError(
                f"a handler returned {type(result).__name__}, which is not a response; "
                "return a HttpResponse, or answer through request.protocol and return None"
            )
        await writer(protocol)

    async def __handle_ws__(self, scope: Any, transport: Any) -> None:
        route = self.router.get(scope.route_id) if scope.route_id is not None else None

        if route is None:
            await transport.reject(404, "Not Found")
            return

        try:
            parameters = route.convert(scope.path_params)
        except (ValueError, TypeError):
            logger.debug("path parameters for %s could not be converted", scope.path, exc_info=True)
            await transport.reject(404, "Not Found")
            return

        socket = WebSocket(scope, transport, parameters)

        async def call_handler(socket: WebSocket) -> None:
            supplied = await _dependencies(route, socket)
            await route.handler(socket, **supplied, **parameters)

        try:
            await _served(socket, self.middleware.execute_websocket(socket, call_handler))
        except Exception as exception:
            handled, _ = await self._dispatch_exception(socket, exception)
            if handled:
                return
            logger.exception("WebSocket handler for %s failed", scope.path)
            # Whether this refuses the upgrade or closes an open connection
            # depends on how far the handler got; both are the right answer at
            # their respective stage, and neither should raise here.
            try:
                if socket.connected:
                    await socket.close(1011, "handler failed")
                else:
                    await socket.reject(500, "Internal Server Error")
            except RuntimeError:
                logger.debug("the WebSocket was already finished", exc_info=True)

    async def __handle_wt__(self, scope: Any, session: Any) -> None:
        route = self.router.get(scope.route_id) if scope.route_id is not None else None

        if route is None:
            await session.reject(404)
            return

        try:
            parameters = route.convert(scope.path_params)
        except (ValueError, TypeError):
            logger.debug("path parameters for %s could not be converted", scope.path, exc_info=True)
            await session.reject(404)
            return

        connection = WebTransportSession(scope, session, parameters)

        async def call_handler(connection: WebTransportSession) -> None:
            supplied = await _dependencies(route, connection)
            await route.handler(connection, **supplied, **parameters)

        try:
            await _served(
                connection, self.middleware.execute_webtransport(connection, call_handler)
            )
        except Exception as exception:
            handled, _ = await self._dispatch_exception(connection, exception)
            if handled:
                return
            logger.exception("WebTransport handler for %s failed", scope.path)
            try:
                if connection.connected:
                    await connection.close()
                else:
                    await connection.reject(500)
            except RuntimeError:
                logger.debug("the WebTransport session was already finished", exc_info=True)

    def __startup__(self, loop: asyncio.AbstractEventLoop) -> None:
        """Run startup callbacks before the worker accepts anything.

        The loop exists but is not running yet, so an asynchronous callback is
        driven to completion here rather than scheduled.
        """
        # A worker is forked from a process that may have opened connections of
        # its own. Sharing one across a fork means two processes reading the
        # same socket, so each worker starts with none.
        from nitro.cache import reset_caches
        from nitro.di import open_worker_dependencies, reset_worker_dependencies
        from nitro.intercom import reset_connections
        from nitro.storage import reset_storages

        reset_connections()
        reset_caches()
        reset_storages()
        reset_worker_dependencies()

        # Worker-scoped dependencies are built before anything is served, so a
        # pool that cannot connect stops this worker instead of failing the
        # first request that asked for it.
        loop.run_until_complete(
            open_worker_dependencies(route.dependencies for route in self.routes)
        )
        self._run_callbacks(loop, self._startup_callbacks, "startup")

    def __shutdown__(self, loop: asyncio.AbstractEventLoop) -> None:
        """Run shutdown callbacks after the worker has stopped serving."""
        from nitro.di import close_worker_dependencies

        self._run_callbacks(loop, self._shutdown_callbacks, "shutdown")
        # After the callbacks: one of them may still want what a worker-scoped
        # dependency is holding.
        loop.run_until_complete(close_worker_dependencies())

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
        return f"Nitro(routes={len(self.router)})"
