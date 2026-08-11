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

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("nitro.di")

__all__ = [
    "DependencyCache",
    "DependencyCycle",
    "DependencyError",
    "DependencyScope",
    "Depends",
    "cache_for",
    "close_worker_dependencies",
    "dependencies_for",
    "extract_dependencies",
    "open_worker_dependencies",
    "reset_worker_dependencies",
    "resolve_dependencies",
    "worker_scoped",
]

#: Set on a provider by :func:`worker_scoped`.
WORKER_SCOPED = "__nitro_worker_scoped__"

#: Parameter names that receive the request, socket or session itself rather
#: than a dependency.
CONTEXT_PARAMETERS: frozenset[str] = frozenset({"request", "websocket", "session", "scope"})


class DependencyError(Exception):
    """A dependency could not be resolved."""


class DependencyCycle(DependencyError):
    """A dependency depends on itself, directly or through others."""


class DependencyScope(DependencyError):
    """A dependency asks for something that does not live as long as it does."""


def worker_scoped(provider: Callable[..., Any]) -> Callable[..., Any]:
    """Marks `provider` as producing one value for the whole worker.

    Lifetime belongs to the resource rather than to whoever asks for it: a
    connection pool is worker-lifetime whether a handler, a middleware or
    another dependency wants it, so it is declared once here instead of at
    every use.

        @worker_scoped
        async def get_pool() -> AsyncIterator[Pool]:
            pool = await create_pool(...)
            try:
                yield pool
            finally:
                await pool.close()

    A worker is a forked process, so this is one value per worker rather than
    one for the deployment — with ``WORKERS = 4`` there are four pools, in the
    same way caches and storages are rebuilt per worker because a connection
    cannot cross a fork.
    """
    setattr(provider, WORKER_SCOPED, True)
    return provider


def is_worker_scoped(provider: Callable[..., Any]) -> bool:
    return bool(getattr(provider, WORKER_SCOPED, False))


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
    """Resolved values for the span of one request, and what they left open.

    A miss is distinguished from a cached `None`, so a dependency that legitimately
    produces `None` is not called again every time it is asked for.

    A dependency that yields its value is left suspended at the ``yield`` until
    :meth:`aclose` finishes it. The cache owns those because it already spans
    exactly the right stretch of time: everything resolved for one request is
    released at the end of that request, in the reverse of the order it was
    acquired.
    """

    __slots__ = ("_open", "_values")

    _MISSING = object()

    def __init__(self) -> None:
        self._values: dict[Callable[..., Any], Any] = {}
        self._open: list[Any] = []

    def opened(self, generator: Any) -> None:
        """Remember a dependency suspended at its `yield`, to be finished by
        :meth:`aclose`."""
        self._open.append(generator)

    async def aclose(self, exception: BaseException | None = None) -> None:
        """Finish every dependency still suspended, most recent first.

        `exception` is whatever the request failed with, and is raised at the
        ``yield`` so a dependency can tell a failure from a success — a
        transaction has to know which of commit and roll back it is doing.
        Without one the dependency is resumed normally, which is the difference
        between running the code after its ``yield`` and running only its
        ``finally``.

        A dependency that fails to release is logged and the rest are still
        released: one that cannot close is not a reason to leak the others, nor
        to lose the failure that is already on its way to the client.
        """
        while self._open:
            generator = self._open.pop()
            try:
                await _finish(generator, exception)
            except Exception:
                logger.exception("a dependency failed while being released")

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


#: Values that live as long as the worker does, and whatever they left open.
#: Reset per worker, because a value built before a fork would be shared by
#: processes that must not share it.
_worker_cache = DependencyCache()


#: Where a connection keeps its cache. Underscored because a request's state is
#: the application's to write to, and this is not the application's.
_CACHE_ATTRIBUTE = "_dependency_cache"


def cache_for(connection: Any) -> DependencyCache:
    """The cache belonging to `connection`, created on first use.

    Kept on the connection's own state so that everything resolved while it is
    being served — middleware, the handler, and anything either depends on —
    shares one. A cache per layer would call a dependency once for the
    middleware that authenticated and again for the handler that answered,
    which is the thing caching exists to prevent.

    Something with no state of its own gets a cache of its own, which shares
    nothing and is released by whoever made it.
    """
    state = getattr(connection, "state", None)
    if state is None:
        return DependencyCache()
    if _CACHE_ATTRIBUTE not in state:
        setattr(state, _CACHE_ATTRIBUTE, DependencyCache())
    return getattr(state, _CACHE_ATTRIBUTE)


def reset_worker_dependencies() -> None:
    """Forget everything worker-scoped, without releasing it.

    For a worker that has just been forked: whatever the parent resolved
    belongs to the parent, and closing it here would close it there too.
    """
    global _worker_cache
    _worker_cache = DependencyCache()


async def close_worker_dependencies() -> None:
    """Release everything worker-scoped. For a worker that is shutting down."""
    await _worker_cache.aclose()


async def open_worker_dependencies(graphs: Iterable[dict[str, DependencyParam]]) -> None:
    """Build everything worker-scoped that `graphs` can reach.

    Done before the worker serves anything, so a pool that cannot connect stops
    the worker rather than failing the first request that wanted it — and so
    that two requests arriving together do not both find it missing and build
    it twice.

    Nothing has to be registered for this: a provider is reachable only if some
    handler depends on it, and every handler's graph was read when its route
    was.
    """
    for graph in graphs:
        for parameter in _worker_scoped_in(graph):
            await _resolve_one(parameter, None, _worker_cache)


def _worker_scoped_in(graph: dict[str, DependencyParam]) -> Iterator[DependencyParam]:
    for parameter in graph.values():
        if is_worker_scoped(parameter.depends.dependency):
            yield parameter
        else:
            yield from _worker_scoped_in(parameter.sub_dependencies)


def extract_dependencies(function: Callable[..., Any]) -> dict[str, DependencyParam]:
    """Every parameter of `function` supplied by a dependency, with its own.

    A cycle is reported here rather than at resolution time, so a broken graph
    is found when routes are registered instead of when a request arrives.
    """
    return _extract(function, ())


#: Graphs already read, keyed by the callable they were read from. Extraction
#: walks signatures recursively, which is worth doing once per handler rather
#: than once per request; the result depends only on the callable itself.
_CACHED_GRAPHS: dict[Any, dict[str, DependencyParam]] = {}


def dependencies_for(function: Callable[..., Any]) -> dict[str, DependencyParam]:
    """:func:`extract_dependencies`, remembered per callable.

    For a handler whose graph is read when its route is registered this is
    already known; it exists for the ones that cannot be — an endpoint's verb
    methods are reached through ``dispatch`` rather than being the registered
    handler themselves.
    """
    key = getattr(function, "__func__", function)
    try:
        return _CACHED_GRAPHS[key]
    except KeyError:
        pass
    except TypeError:
        # Unhashable, so it cannot be remembered. Reading it every time is
        # slower but still correct.
        return extract_dependencies(function)

    graph = extract_dependencies(function)
    _CACHED_GRAPHS[key] = graph
    return graph


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
        if name in CONTEXT_PARAMETERS:
            if is_worker_scoped(function):
                raise DependencyScope(
                    f"{_name(function)} lives for the worker but asks for {name!r}; "
                    "it is built before any of them exist"
                )
            continue
        if not isinstance(parameter.default, Depends):
            continue

        depends = parameter.default
        if is_worker_scoped(function) and not is_worker_scoped(depends.dependency):
            raise DependencyScope(
                f"{_name(function)} lives for the worker but depends on "
                f"{_name(depends.dependency)}, which lives for one request; "
                "the request's value would be held for every later one"
            )
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

    # Where the value is kept, and so how long it and whatever it opened live.
    # A worker-scoped provider resolves into the cache that outlives the
    # request, which is what makes it one value per worker rather than one per
    # request that happens to be built the same way.
    if is_worker_scoped(depends.dependency):
        cache = _worker_cache

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

    # A provider that yields hands over a value and stays suspended, holding
    # whatever it opened until the cache releases it. One that returns is done
    # the moment it answers.
    if inspect.isasyncgen(result):
        value = await _start_async(result, depends.dependency)
        cache.opened(result)
    elif inspect.isgenerator(result):
        value = await _start_sync(result, depends.dependency)
        cache.opened(result)
    elif inspect.isawaitable(result):
        value = await result
    else:
        value = result

    if depends.use_cache:
        cache.set(depends.dependency, value)
    return value


async def _start_async(generator: Any, dependency: Callable[..., Any]) -> Any:
    try:
        return await anext(generator)
    except StopAsyncIteration:
        raise DependencyError(f"{_name(dependency)} yielded nothing") from None


async def _start_sync(generator: Any, dependency: Callable[..., Any]) -> Any:
    try:
        return await asyncio.to_thread(next, generator)
    except StopIteration:
        raise DependencyError(f"{_name(dependency)} yielded nothing") from None


async def _finish(generator: Any, exception: BaseException | None) -> None:
    """Resume `generator` past its `yield` so the rest of it runs.

    Resumed rather than closed, because closing raises `GeneratorExit` at the
    yield and a dependency written as `async with transaction():` would read
    that as a failure and roll back a request that had succeeded.
    """
    asynchronous = inspect.isasyncgen(generator)

    try:
        if exception is None:
            if asynchronous:
                await anext(generator)
            else:
                await asyncio.to_thread(next, generator)
        else:
            if asynchronous:
                await generator.athrow(exception)
            else:
                await asyncio.to_thread(generator.throw, exception)
    except (StopIteration, StopAsyncIteration):
        return
    except BaseException as raised:
        # The dependency let the failure it was told about carry on, which is
        # what not catching it looks like from here.
        if raised is exception:
            return
        raise

    raise DependencyError(f"a dependency yielded more than once: {_name(generator)}")


def _context_parameters(function: Callable[..., Any]) -> list[str]:
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return []
    return [name for name in signature.parameters if name in CONTEXT_PARAMETERS]


def _name(function: Callable[..., Any]) -> str:
    return getattr(function, "__name__", repr(function))
