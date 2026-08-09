"""Dependency injection.

A handler declares what it needs by giving a parameter a :class:`Depends`
default; the value is produced by calling what that names, recursively, before
the handler runs.

Caching is per request and nothing else. A dependency that opens a database
transaction, reads the signed-in user or generates a request identifier must
produce a fresh value for every request, and must produce the *same* value
everywhere within one — so the cache is created per request and passed down,
never held on the resolver. A resolver is shared across requests and across
tasks; a cache on it would hand one request's values to another.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DependencyCache",
    "DependencyCycle",
    "DependencyError",
    "Depends",
    "extract_dependencies",
    "resolve_dependencies",
]

#: Parameter names that receive the request, socket or session itself rather
#: than a dependency.
CONTEXT_PARAMETERS: frozenset[str] = frozenset({"request", "websocket", "session", "scope"})


class DependencyError(Exception):
    """A dependency could not be resolved."""


class DependencyCycle(DependencyError):
    """A dependency depends on itself, directly or through others."""


class Depends:
    """Marks a parameter as supplied by calling `dependency`.

        async def get_database() -> Database: ...

        async def handler(request, database=Depends(get_database)): ...

    With `use_cache` left on, a dependency named more than once in a request is
    called once and shared. Turn it off for something that must be produced
    afresh each time it is asked for.
    """

    __slots__ = ("dependency", "use_cache")

    def __init__(
        self,
        dependency: Callable[..., Awaitable[Any]] | Callable[..., Any],
        *,
        use_cache: bool = True,
    ) -> None:
        if not callable(dependency):
            raise TypeError("a dependency must be callable")
        self.dependency = dependency
        self.use_cache = use_cache

    def __repr__(self) -> str:
        name = getattr(self.dependency, "__name__", repr(self.dependency))
        return f"Depends({name}{'' if self.use_cache else ', use_cache=False'})"


@dataclass(slots=True)
class DependencyParam:
    """One parameter that is supplied by a dependency."""

    name: str
    depends: Depends
    sub_dependencies: dict[str, DependencyParam] = field(default_factory=dict)


class DependencyCache:
    """Resolved values for the span of one request.

    A miss is distinguished from a cached `None`, so a dependency that legitimately
    produces `None` is not called again every time it is asked for.
    """

    __slots__ = ("_values",)

    _MISSING = object()

    def __init__(self) -> None:
        self._values: dict[Callable[..., Any], Any] = {}

    def get(self, dependency: Callable[..., Any]) -> Any:
        """The cached value, or :data:`DependencyCache._MISSING`."""
        return self._values.get(dependency, self._MISSING)

    def set(self, dependency: Callable[..., Any], value: Any) -> None:
        self._values[dependency] = value

    def __contains__(self, dependency: Callable[..., Any]) -> bool:
        return dependency in self._values

    def __len__(self) -> int:
        return len(self._values)

    def clear(self) -> None:
        self._values.clear()


def extract_dependencies(function: Callable[..., Any]) -> dict[str, DependencyParam]:
    """Every parameter of `function` supplied by a dependency, with its own.

    A cycle is reported here rather than at resolution time, so a broken graph
    is found when routes are registered instead of when a request arrives.
    """
    return _extract(function, ())


def _extract(
    function: Callable[..., Any],
    ancestry: tuple[Callable[..., Any], ...],
) -> dict[str, DependencyParam]:
    if function in ancestry:
        chain = " -> ".join(_name(step) for step in (*ancestry, function))
        raise DependencyCycle(f"dependencies form a cycle: {chain}")

    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError) as error:
        raise DependencyError(f"{_name(function)} cannot be inspected: {error}") from error

    found: dict[str, DependencyParam] = {}
    for name, parameter in signature.parameters.items():
        if name in CONTEXT_PARAMETERS or not isinstance(parameter.default, Depends):
            continue

        depends = parameter.default
        found[name] = DependencyParam(
            name=name,
            depends=depends,
            sub_dependencies=_extract(depends.dependency, (*ancestry, function)),
        )
    return found


async def resolve_dependencies(
    dependencies: dict[str, DependencyParam],
    context: Any = None,
    cache: DependencyCache | None = None,
) -> dict[str, Any]:
    """Resolve `dependencies` into values for a handler's keyword arguments.

    `cache` spans one request. Leaving it out gives each call a cache of its
    own, which is right for a one-off resolution and wrong for a request —
    within a request the same cache must be passed throughout, or two parameters
    naming one dependency would each get their own value.
    """
    cache = DependencyCache() if cache is None else cache
    return {
        name: await _resolve_one(parameter, context, cache)
        for name, parameter in dependencies.items()
    }


async def _resolve_one(
    parameter: DependencyParam,
    context: Any,
    cache: DependencyCache,
) -> Any:
    depends = parameter.depends

    if depends.use_cache:
        cached = cache.get(depends.dependency)
        if cached is not DependencyCache._MISSING:
            return cached

    arguments: dict[str, Any] = {}
    for name, sub in parameter.sub_dependencies.items():
        arguments[name] = await _resolve_one(sub, context, cache)

    # A dependency may ask for the request, socket or session by naming it.
    for name in _context_parameters(depends.dependency):
        arguments[name] = context

    result = depends.dependency(**arguments)
    if inspect.isawaitable(result):
        result = await result

    if depends.use_cache:
        cache.set(depends.dependency, result)
    return result


def _context_parameters(function: Callable[..., Any]) -> list[str]:
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return []
    return [name for name in signature.parameters if name in CONTEXT_PARAMETERS]


def _name(function: Callable[..., Any]) -> str:
    return getattr(function, "__name__", repr(function))
