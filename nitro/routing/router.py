"""The Python side of routing.

Routes are declared here and matched in the compiled matcher. What crosses the
boundary at startup is a description of each route: its path, the methods it
answers, and for every parameter the expression that recognises it. Converters
themselves never cross — the matcher hands back captured text, and the
converters turn it into Python values on the way to the handler.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from nitro.routing.converters import Converter, converter_for

__all__ = ["ParameterSpec", "Route", "Router", "RouteTable"]

#: A parameter declaration, written ``<converter:name>`` or just ``<name>``.
_PARAMETER = re.compile(r"<([^>]*)>")

DEFAULT_METHODS: tuple[str, ...] = ("GET", "HEAD")


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """What the matcher needs to know about one path parameter."""

    name: str
    pattern: str
    greedy: bool

    def as_tuple(self) -> tuple[str, str, bool]:
        return (self.name, self.pattern, self.greedy)


@dataclass(slots=True)
class Route:
    """A registered route."""

    id: int
    path: str
    handler: Callable[..., Any]
    methods: tuple[str, ...]
    name: str | None = None
    converters: dict[str, Converter] = field(default_factory=dict)

    @property
    def parameters(self) -> list[ParameterSpec]:
        return [
            ParameterSpec(name, converter.regex, converter.spans_separators)
            for name, converter in self.converters.items()
        ]

    def convert(self, captured: dict[str, str]) -> dict[str, Any]:
        """Turn captured text into Python values."""
        converted: dict[str, Any] = {}
        for name, value in captured.items():
            converter = self.converters.get(name)
            converted[name] = converter.to_python(value) if converter else value
        return converted

    def build_path(self, values: dict[str, Any]) -> str:
        """The concrete path for this route with `values` substituted in."""

        def substitute(match: re.Match[str]) -> str:
            name = _parameter_name(match.group(1))
            if name not in values:
                raise KeyError(f"route {self.name or self.path!r} needs a value for {name!r}")
            converter = self.converters.get(name)
            return converter.to_url(values[name]) if converter else str(values[name])

        return _PARAMETER.sub(substitute, self.path)


#: The description of every route, in the shape the compiled matcher reads.
RouteTable = list[tuple[int, str, tuple[str, ...], list[tuple[str, str, bool]]]]


def _parameter_name(declaration: str) -> str:
    """The parameter name from ``converter:name`` or bare ``name``.

    Split from the right, because a converter written as ``regex("a:b")`` may
    contain colons of its own.
    """
    _, _, name = declaration.rpartition(":")
    return name or declaration


def _converter_name(declaration: str) -> str:
    prefix, separator, _ = declaration.rpartition(":")
    return prefix if separator else "str"


def parse_parameters(path: str) -> dict[str, Converter]:
    """The converters for every parameter in `path`, in order of appearance."""
    converters: dict[str, Converter] = {}

    for match in _PARAMETER.finditer(path):
        declaration = match.group(1)
        name = _parameter_name(declaration)
        if not name:
            raise ValueError(f"route {path!r}: a parameter must be named")
        if name in converters:
            raise ValueError(f"route {path!r}: parameter {name!r} appears more than once")
        converters[name] = converter_for(_converter_name(declaration))

    return converters


class Router:
    """A collection of routes."""

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix.rstrip("/")
        self._routes: list[Route] = []
        self._by_id: dict[int, Route] = {}
        self._by_name: dict[str, Route] = {}
        self._next_id = 0

    def __len__(self) -> int:
        return len(self._routes)

    def __iter__(self):
        return iter(self._routes)

    @property
    def routes(self) -> list[Route]:
        return list(self._routes)

    def add(
        self,
        path: str,
        handler: Callable[..., Any],
        *,
        methods: Sequence[str] = DEFAULT_METHODS,
        name: str | None = None,
    ) -> Route:
        """Register `handler` for `path`."""
        full_path = self._join(path)
        converters = parse_parameters(full_path)
        self._reject_misplaced_greedy(full_path, converters)

        normalised = tuple(dict.fromkeys(method.upper() for method in methods))
        if not normalised:
            raise ValueError(f"route {full_path!r} must answer at least one method")

        self._reject_duplicate(full_path, normalised)
        if name is not None and name in self._by_name:
            raise ValueError(f"route name {name!r} is already used by {self._by_name[name].path!r}")

        route = Route(
            id=self._next_id,
            path=full_path,
            handler=handler,
            methods=normalised,
            name=name,
            converters=converters,
        )
        self._next_id += 1
        self._routes.append(route)
        self._by_id[route.id] = route
        if name is not None:
            self._by_name[name] = route
        return route

    def route(
        self,
        path: str,
        *,
        methods: Sequence[str] = DEFAULT_METHODS,
        name: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator form of :meth:`add`."""

        def register(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.add(path, handler, methods=methods, name=name)
            return handler

        return register

    def include(self, routes: Iterable[Route] | Router, prefix: str = "") -> None:
        """Absorb another router's routes, optionally under a further prefix."""
        source = routes.routes if isinstance(routes, Router) else list(routes)
        for route in source:
            self.add(
                f"{prefix.rstrip('/')}{route.path}" if prefix else route.path,
                route.handler,
                methods=route.methods,
                name=route.name,
            )

    def get(self, route_id: int) -> Route | None:
        return self._by_id.get(route_id)

    def by_name(self, name: str) -> Route:
        try:
            return self._by_name[name]
        except KeyError:
            raise LookupError(f"no route is named {name!r}") from None

    def url_for(self, name: str, **values: Any) -> str:
        """The path for the route named `name`."""
        return self.by_name(name).build_path(values)

    def table(self) -> RouteTable:
        """Every route, described for the compiled matcher."""
        return [
            (
                route.id,
                route.path,
                route.methods,
                [parameter.as_tuple() for parameter in route.parameters],
            )
            for route in self._routes
        ]

    def _join(self, path: str) -> str:
        if not path.startswith("/"):
            raise ValueError(f"route path must start with '/', got {path!r}")
        return f"{self.prefix}{path}" if self.prefix else path

    def _reject_duplicate(self, path: str, methods: tuple[str, ...]) -> None:
        for existing in self._routes:
            if existing.path != path:
                continue
            clashing = sorted(set(existing.methods) & set(methods))
            if clashing:
                raise ValueError(
                    f"{', '.join(clashing)} {path} is already registered"
                )

    @staticmethod
    def _reject_misplaced_greedy(path: str, converters: dict[str, Converter]) -> None:
        for match in _PARAMETER.finditer(path):
            name = _parameter_name(match.group(1))
            if converters[name].spans_separators and match.end() != len(path):
                raise ValueError(
                    f"route {path!r}: parameter {name!r} spans separators and must end the path"
                )
