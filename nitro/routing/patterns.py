"""The declarative route table.

A project's routes live in a module of their own, named by the ``ROUTES``
setting, which defines a list called ``patterns``:

    # myproject/routes.py
    patterns = [
        HTTPRoute("/", index, name="index"),
        HTTPRoute("/users/<int:user_id>", UserEndpoint, name="user"),
        WebSocketRoute("/rooms/<slug:room>", room),
        Mount("/api", api_patterns, name="api"),
    ]

These classes are descriptions, not registrations: each one says what a route
is, and :meth:`attach` is what turns it into an entry in a :class:`Router`.
Keeping the two apart is what lets a route table be written, imported and
inspected before any application exists to hold it.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nitro.endpoints import HTTP_METHODS
from nitro.routing.router import (
    DEFAULT_METHODS,
    WEBSOCKET_METHOD,
    WEBTRANSPORT_METHOD,
    Router,
)

if TYPE_CHECKING:
    from nitro.routing.mount import Mount

__all__ = [
    "HTTPRoute",
    "WebSocketRoute",
    "WebTransportRoute",
    "load_exception_handlers",
    "load_patterns",
    "qualified",
]

#: What a `patterns` list may contain.
Pattern = "HTTPRoute | WebSocketRoute | WebTransportRoute | Mount"


def qualified(name: str | None, namespace: str | None) -> str | None:
    """A route name as it is registered inside `namespace`."""
    if name is None or not namespace:
        return name
    return f"{namespace}:{name}"


def _dispatcher(endpoint: type) -> Callable[..., Any]:
    """A handler that runs `endpoint` for one request.

    The class is instantiated per request rather than once at registration, so
    an endpoint may keep state on ``self`` for the duration of the call without
    it leaking into the next one.
    """
    if not hasattr(endpoint, "dispatch"):
        raise TypeError(
            f"{endpoint.__name__} is used as a route handler but is not an endpoint class; "
            "subclass HTTPEndpoint, WebSocketEndpoint or WebTransportEndpoint, "
            "or pass an async function"
        )

    async def dispatch(target: Any, **parameters: Any) -> Any:
        return await endpoint().dispatch(target, **parameters)

    dispatch.__name__ = endpoint.__name__
    dispatch.__qualname__ = endpoint.__qualname__
    return dispatch


def _handler_for(handler: Any) -> Callable[..., Any]:
    return _dispatcher(handler) if isinstance(handler, type) else handler


def _methods_for(handler: Any) -> tuple[str, ...]:
    """The methods an endpoint class answers, from the ones it defines.

    Methods are checked by the compiled matcher, so a route that did not
    declare them would answer 405 to a verb its endpoint implements.
    """
    if not isinstance(handler, type):
        return DEFAULT_METHODS

    declared = tuple(
        method.upper() for method in HTTP_METHODS if getattr(handler, method, None) is not None
    )
    if not declared:
        return DEFAULT_METHODS
    if "GET" in declared and "HEAD" not in declared:
        declared += ("HEAD",)
    return declared


@dataclass(frozen=True, slots=True)
class HTTPRoute:
    """An HTTP route: a path, and the handler or endpoint class that answers it.

    ``methods`` defaults to what the handler can answer — ``GET`` and ``HEAD``
    for a function, and for an endpoint class the verbs it defines.
    """

    path: str
    handler: Any
    name: str | None = None
    methods: Sequence[str] | None = None

    def attach(self, router: Router, prefix: str = "", namespace: str | None = None) -> None:
        router.add(
            f"{prefix}{self.path}",
            _handler_for(self.handler),
            methods=self.methods if self.methods is not None else _methods_for(self.handler),
            name=qualified(self.name, namespace),
        )


@dataclass(frozen=True, slots=True)
class WebSocketRoute:
    """A WebSocket route.

    It lives in the same table as the HTTP routes, registered under a method no
    HTTP request can carry, so one path can serve every protocol at once.
    """

    path: str
    handler: Any
    name: str | None = None

    def attach(self, router: Router, prefix: str = "", namespace: str | None = None) -> None:
        router.add(
            f"{prefix}{self.path}",
            _handler_for(self.handler),
            methods=[WEBSOCKET_METHOD],
            name=qualified(self.name, namespace),
        )


@dataclass(frozen=True, slots=True)
class WebTransportRoute:
    """A WebTransport route. Needs HTTP/3, and so a TLS certificate."""

    path: str
    handler: Any
    name: str | None = None

    def attach(self, router: Router, prefix: str = "", namespace: str | None = None) -> None:
        router.add(
            f"{prefix}{self.path}",
            _handler_for(self.handler),
            methods=[WEBTRANSPORT_METHOD],
            name=qualified(self.name, namespace),
        )


def _routes_module(source: str) -> Any:
    from nitro.settings import ImproperlyConfigured

    try:
        return importlib.import_module(source)
    except ImportError as error:
        raise ImproperlyConfigured(
            f"ROUTES names {source!r}, which could not be imported: {error}"
        ) from error


def load_patterns(source: str | Iterable[Any]) -> list[Any]:
    """The route declarations named by `source`.

    A string is the import path of a module defining ``patterns``; anything
    else is already the declarations themselves.
    """
    from nitro.settings import ImproperlyConfigured

    if not isinstance(source, str):
        return list(source)

    module = _routes_module(source)
    patterns = getattr(module, "patterns", None)
    if patterns is None:
        raise ImproperlyConfigured(
            f"the route module {source!r} does not define `patterns`"
        )
    return list(patterns)


def load_exception_handlers(source: str | Iterable[Any]) -> dict[Any, Callable[..., Any]]:
    """The ``exception_handlers`` mapping `source` declares, resolved.

    Only a routes module can carry one; declarations passed directly have no
    module to read it from, and get an empty mapping. Keys are status codes or
    exception classes, values are handlers or the import paths of handlers.
    """
    if not isinstance(source, str):
        return {}

    declared = getattr(_routes_module(source), "exception_handlers", None) or {}
    return normalise_exception_handlers(declared, f"the route module {source!r}")


def normalise_exception_handlers(
    declared: dict[Any, Any], origin: str
) -> dict[Any, Callable[..., Any]]:
    """Check every key and resolve every handler, or say what is wrong.

    Checked when the mapping is read rather than when an exception is raised:
    a misspelled handler is a deployment problem, and finding out at startup
    beats finding out from the one request that needed it.
    """
    from nitro.settings import ImproperlyConfigured

    resolved: dict[Any, Callable[..., Any]] = {}
    for key, handler in declared.items():
        if isinstance(key, bool) or not isinstance(key, (int, type)):
            raise ImproperlyConfigured(
                f"{origin} maps {key!r} to a handler; a key must be a status code "
                "or an exception class"
            )
        if isinstance(key, int) and not 100 <= key <= 599:
            raise ImproperlyConfigured(f"{origin} maps {key!r} to a handler; that is not a status")
        if isinstance(key, type) and not issubclass(key, Exception):
            raise ImproperlyConfigured(
                f"{origin} maps {key.__name__} to a handler; that is not an exception class"
            )
        resolved[key] = _resolve_handler(handler, key, origin)
    return resolved


def _resolve_handler(handler: Any, key: Any, origin: str) -> Callable[..., Any]:
    from nitro.settings import ImproperlyConfigured
    from nitro.utils.modules import import_string

    name = key.__name__ if isinstance(key, type) else key

    if isinstance(handler, str):
        try:
            handler = import_string(handler)
        except ImportError as error:
            raise ImproperlyConfigured(
                f"{origin} names {handler!r} for {name}, which could not be imported: {error}"
            ) from error

    call = getattr(handler, "__call__", None)
    if not (
        inspect.iscoroutinefunction(handler)
        or (call is not None and inspect.iscoroutinefunction(call))
    ):
        raise ImproperlyConfigured(
            f"{origin} gives {name} a handler that is not an async function"
        )
    return handler
