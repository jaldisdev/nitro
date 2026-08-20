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

import asyncio
import contextlib

import pytest

from nitro.di import (
    DependencyCache,
    DependencyCycle,
    DependencyError,
    DependencyParam,
    DependencyScope,
    Depends,
    close_worker_dependencies,
    extract_dependencies,
    open_worker_dependencies,
    reset_worker_dependencies,
    resolve_dependencies,
    worker_scoped,
)


class TestDepends:
    def test_a_dependency_must_be_callable(self):
        with pytest.raises(TypeError, match="callable"):
            Depends("not callable")

    def test_the_representation_names_the_dependency(self):
        async def get_database(): ...

        assert repr(Depends(get_database)) == "Depends(get_database)"
        assert "use_cache=False" in repr(Depends(get_database, use_cache=False))


class TestExtraction:
    def test_a_handler_without_dependencies_has_none(self):
        async def handler(request): ...

        assert extract_dependencies(handler) == {}

    def test_a_dependency_parameter_is_found(self):
        async def get_database(): ...

        async def handler(request, database=Depends(get_database)): ...

        found = extract_dependencies(handler)
        assert list(found) == ["database"]
        assert found["database"].depends.dependency is get_database

    def test_context_parameters_are_not_dependencies(self):
        async def handler(request, websocket, transport, scope): ...

        assert extract_dependencies(handler) == {}

    def test_ordinary_defaults_are_not_dependencies(self):
        async def handler(request, page=1, sort="name"): ...

        assert extract_dependencies(handler) == {}

    def test_dependencies_of_dependencies_are_found(self):
        async def get_settings(): ...

        async def get_database(settings=Depends(get_settings)): ...

        async def handler(request, database=Depends(get_database)): ...

        found = extract_dependencies(handler)
        assert list(found["database"].sub_dependencies) == ["settings"]

    def test_a_direct_cycle_is_reported(self):
        def placeholder(): ...

        async def loops(inner=Depends(placeholder)): ...

        # Rebind so the dependency names itself.
        loops.__defaults__ = (Depends(loops),)

        async def handler(request, value=Depends(loops)): ...

        with pytest.raises(DependencyCycle, match="cycle"):
            extract_dependencies(handler)

    def test_an_indirect_cycle_is_reported(self):
        def placeholder(): ...

        async def first(second=Depends(placeholder)): ...

        async def second(first_value=Depends(first)): ...

        first.__defaults__ = (Depends(second),)

        async def handler(request, value=Depends(first)): ...

        with pytest.raises(DependencyCycle, match="cycle"):
            extract_dependencies(handler)

    def test_something_uninspectable_is_reported(self):
        class Uninspectable:
            def __call__(self): ...

            @property
            def __signature__(self):
                raise ValueError("this cannot be inspected")

        async def handler(request, value=Depends(Uninspectable())): ...

        with pytest.raises(DependencyError, match="cannot be inspected"):
            extract_dependencies(handler)


class TestResolution:
    async def test_a_dependency_is_called_and_its_value_supplied(self):
        async def get_database():
            return "database"

        async def handler(request, database=Depends(get_database)): ...

        resolved = await resolve_dependencies(extract_dependencies(handler))
        assert resolved == {"database": "database"}

    async def test_a_synchronous_dependency_is_supported(self):
        def get_setting():
            return 42

        async def handler(request, setting=Depends(get_setting)): ...

        assert await resolve_dependencies(extract_dependencies(handler)) == {"setting": 42}

    async def test_sub_dependencies_are_supplied_to_their_dependency(self):
        async def get_settings():
            return {"url": "postgres://"}

        async def get_database(settings=Depends(get_settings)):
            return f"connected to {settings['url']}"

        async def handler(request, database=Depends(get_database)): ...

        resolved = await resolve_dependencies(extract_dependencies(handler))
        assert resolved == {"database": "connected to postgres://"}

    async def test_the_context_is_supplied_to_a_dependency_that_asks(self):
        async def get_user(request):
            return f"user of {request}"

        async def handler(request, user=Depends(get_user)): ...

        resolved = await resolve_dependencies(extract_dependencies(handler), "the request")
        assert resolved == {"user": "user of the request"}


class TestCaching:
    async def test_a_dependency_named_twice_is_called_once_per_request(self):
        calls = []

        async def get_connection():
            calls.append(1)
            return len(calls)

        async def get_repository(connection=Depends(get_connection)):
            return connection

        async def handler(
            request,
            connection=Depends(get_connection),
            repository=Depends(get_repository),
        ): ...

        cache = DependencyCache()
        resolved = await resolve_dependencies(extract_dependencies(handler), None, cache)

        assert len(calls) == 1, "one request means one call"
        assert resolved["connection"] == resolved["repository"] == 1

    async def test_each_request_gets_its_own_values(self):
        calls = []

        async def get_request_id():
            calls.append(1)
            return len(calls)

        async def handler(request, request_id=Depends(get_request_id)): ...

        dependencies = extract_dependencies(handler)
        first = await resolve_dependencies(dependencies, None, DependencyCache())
        second = await resolve_dependencies(dependencies, None, DependencyCache())

        assert first["request_id"] == 1
        assert second["request_id"] == 2, "a value must not carry over between requests"

    async def test_concurrent_requests_do_not_share_values(self):
        counter = 0

        async def get_request_id():
            nonlocal counter
            counter += 1
            mine = counter
            # Yield so the other request interleaves here.
            await asyncio.sleep(0.01)
            return mine

        async def handler(request, request_id=Depends(get_request_id)): ...

        dependencies = extract_dependencies(handler)

        async def one_request():
            return await resolve_dependencies(dependencies, None, DependencyCache())

        first, second = await asyncio.gather(one_request(), one_request())
        assert {first["request_id"], second["request_id"]} == {1, 2}

    async def test_caching_can_be_turned_off(self):
        calls = []

        async def get_token():
            calls.append(1)
            return len(calls)

        async def get_first(token=Depends(get_token, use_cache=False)):
            return token

        async def handler(
            request,
            token=Depends(get_token, use_cache=False),
            first=Depends(get_first),
        ): ...

        await resolve_dependencies(extract_dependencies(handler), None, DependencyCache())
        assert len(calls) == 2, "an uncached dependency is called every time"

    async def test_a_dependency_returning_none_is_not_called_again(self):
        calls = []

        async def get_optional():
            calls.append(1)
            return None

        async def get_wrapper(value=Depends(get_optional)):
            return value

        async def handler(
            request,
            value=Depends(get_optional),
            wrapper=Depends(get_wrapper),
        ): ...

        resolved = await resolve_dependencies(
            extract_dependencies(handler), None, DependencyCache()
        )

        assert len(calls) == 1, "None is a value, and caches like any other"
        assert resolved == {"value": None, "wrapper": None}

    async def test_a_cache_is_created_when_one_is_not_supplied(self):
        calls = []

        async def get_value():
            calls.append(1)
            return len(calls)

        async def handler(request, value=Depends(get_value)): ...

        dependencies = extract_dependencies(handler)
        assert (await resolve_dependencies(dependencies))["value"] == 1
        assert (await resolve_dependencies(dependencies))["value"] == 2


class TestDependencyCache:
    def test_a_miss_is_distinguishable_from_a_cached_none(self):
        def dependency(): ...

        cache = DependencyCache()
        assert cache.get(dependency) is DependencyCache._MISSING
        assert dependency not in cache

        cache.set(dependency, None)
        assert cache.get(dependency) is None
        assert dependency in cache

    def test_clearing_empties_it(self):
        def dependency(): ...

        cache = DependencyCache()
        cache.set(dependency, 1)
        assert len(cache) == 1

        cache.clear()
        assert len(cache) == 0


class TestTeardown:
    async def test_a_yielded_value_is_supplied_like_any_other(self):
        async def get_thing():
            yield "value"

        cache = DependencyCache()
        values = await resolve_dependencies(
            {"thing": DependencyParam("thing", Depends(get_thing))}, None, cache
        )
        assert values == {"thing": "value"}

    async def test_the_rest_of_the_dependency_runs_when_the_cache_closes(self):
        trail = []

        async def get_thing():
            trail.append("acquired")
            yield "value"
            trail.append("released")

        cache = DependencyCache()
        await resolve_dependencies({"t": DependencyParam("t", Depends(get_thing))}, None, cache)
        assert trail == ["acquired"]

        await cache.aclose()
        assert trail == ["acquired", "released"]

    async def test_a_success_is_not_reported_as_a_failure(self):
        """A dependency written as a context manager must commit, not roll back:
        closing the generator instead of resuming it would raise GeneratorExit
        at the yield and every `async with` inside would see a failure."""
        outcome = []

        async def get_transaction():
            try:
                yield "transaction"
            except BaseException:
                outcome.append("rolled back")
                raise
            else:
                outcome.append("committed")

        cache = DependencyCache()
        await resolve_dependencies(
            {"t": DependencyParam("t", Depends(get_transaction))}, None, cache
        )
        await cache.aclose()

        assert outcome == ["committed"]

    async def test_a_failure_is_raised_at_the_yield(self):
        outcome = []

        async def get_transaction():
            try:
                yield "transaction"
            except RuntimeError:
                outcome.append("rolled back")
                raise

        cache = DependencyCache()
        await resolve_dependencies(
            {"t": DependencyParam("t", Depends(get_transaction))}, None, cache
        )
        await cache.aclose(RuntimeError("the handler failed"))

        assert outcome == ["rolled back"]

    async def test_a_dependency_may_swallow_the_failure(self):
        async def get_thing():
            # Swallowing it is what is under test: a dependency that handles
            # the failure it is told about stops it there.
            with contextlib.suppress(RuntimeError):
                yield "value"

        cache = DependencyCache()
        await resolve_dependencies({"t": DependencyParam("t", Depends(get_thing))}, None, cache)
        await cache.aclose(RuntimeError("boom"))  # must not raise

    async def test_they_are_released_in_reverse_order(self):
        trail = []

        async def outer():
            trail.append("outer acquired")
            yield "outer"
            trail.append("outer released")

        async def inner(value=Depends(outer)):
            trail.append("inner acquired")
            yield "inner"
            trail.append("inner released")

        cache = DependencyCache()
        await resolve_dependencies(
            extract_dependencies(lambda thing=Depends(inner): None), None, cache
        )
        await cache.aclose()

        assert trail == [
            "outer acquired",
            "inner acquired",
            "inner released",
            "outer released",
        ]

    async def test_one_that_fails_to_release_does_not_strand_the_others(self, caplog):
        trail = []

        async def breaks():
            yield "value"
            raise RuntimeError("cannot close")

        async def works():
            yield "value"
            trail.append("released")

        cache = DependencyCache()
        await resolve_dependencies({"a": DependencyParam("a", Depends(works))}, None, cache)
        await resolve_dependencies({"b": DependencyParam("b", Depends(breaks))}, None, cache)

        await cache.aclose()  # must not raise

        assert trail == ["released"]
        assert "failed while being released" in caplog.text

    async def test_a_synchronous_generator_works_too(self):
        trail = []

        def get_thing():
            trail.append("acquired")
            yield "value"
            trail.append("released")

        cache = DependencyCache()
        values = await resolve_dependencies(
            {"t": DependencyParam("t", Depends(get_thing))}, None, cache
        )
        assert values == {"t": "value"}

        await cache.aclose()
        assert trail == ["acquired", "released"]

    async def test_a_dependency_that_yields_nothing_says_so(self):
        async def get_nothing():
            if False:
                yield "never"

        cache = DependencyCache()
        with pytest.raises(DependencyError, match="yielded nothing"):
            await resolve_dependencies(
                {"t": DependencyParam("t", Depends(get_nothing))}, None, cache
            )

    async def test_a_dependency_that_yields_twice_is_reported(self, caplog):
        async def get_two():
            yield "first"
            yield "second"

        cache = DependencyCache()
        await resolve_dependencies({"t": DependencyParam("t", Depends(get_two))}, None, cache)
        await cache.aclose()

        assert "yielded more than once" in caplog.text

    async def test_a_cached_dependency_is_released_once(self):
        trail = []

        async def get_thing():
            yield "value"
            trail.append("released")

        async def first(thing=Depends(get_thing)):
            return thing

        async def second(thing=Depends(get_thing)):
            return thing

        cache = DependencyCache()
        await resolve_dependencies(
            extract_dependencies(lambda a=Depends(first), b=Depends(second): None), None, cache
        )
        await cache.aclose()

        assert trail == ["released"]

    async def test_closing_twice_releases_nothing_further(self):
        trail = []

        async def get_thing():
            yield "value"
            trail.append("released")

        cache = DependencyCache()
        await resolve_dependencies({"t": DependencyParam("t", Depends(get_thing))}, None, cache)
        await cache.aclose()
        await cache.aclose()

        assert trail == ["released"]


class TestSuppliedCache:
    """`_supplied` is what endpoint hooks resolve through."""

    async def test_a_cache_that_is_empty_is_still_the_cache(self):
        """An empty cache is falsy, so `cache or DependencyCache()` quietly
        replaced it — and a cache is empty exactly when it is first used, so
        nothing a socket's hooks resolved was ever shared between them."""
        from nitro.endpoints import _supplied

        calls = []

        async def get_thing():
            calls.append("called")
            return "value"

        async def first(thing=Depends(get_thing)):
            return thing

        async def second(thing=Depends(get_thing)):
            return thing

        shared = DependencyCache()
        await _supplied(first, None, shared)
        await _supplied(second, None, shared)

        assert calls == ["called"]
        assert len(shared) > 0

    async def test_what_hooks_open_is_released_once_for_the_connection(self):
        from nitro.endpoints import _supplied

        trail = []

        async def get_connection():
            trail.append("acquired")
            yield "connection"
            trail.append("released")

        async def on_connect(connection=Depends(get_connection)):
            return connection

        async def on_receive(connection=Depends(get_connection)):
            return connection

        shared = DependencyCache()
        await _supplied(on_connect, None, shared)
        await _supplied(on_receive, None, shared)
        await shared.aclose()

        assert trail == ["acquired", "released"]


class TestWorkerScope:
    def setup_method(self):
        reset_worker_dependencies()

    async def test_one_value_serves_every_request(self):
        built = []

        @worker_scoped
        async def get_pool():
            built.append("built")
            return "pool"

        graph = extract_dependencies(lambda pool=Depends(get_pool): None)

        first = await resolve_dependencies(graph, None, DependencyCache())
        second = await resolve_dependencies(graph, None, DependencyCache())

        assert first == second == {"pool": "pool"}
        assert built == ["built"]  # not once per request

    async def test_it_is_not_released_with_the_request(self):
        trail = []

        @worker_scoped
        async def get_pool():
            yield "pool"
            trail.append("closed")

        graph = extract_dependencies(lambda pool=Depends(get_pool): None)
        cache = DependencyCache()

        await resolve_dependencies(graph, None, cache)
        await cache.aclose()

        assert trail == []  # the request ending is not the worker ending

        await close_worker_dependencies()
        assert trail == ["closed"]

    async def test_a_request_scoped_dependency_still_belongs_to_the_request(self):
        trail = []

        @worker_scoped
        async def get_pool():
            yield "pool"
            trail.append("pool closed")

        async def get_connection(pool=Depends(get_pool)):
            yield f"connection from {pool}"
            trail.append("connection closed")

        graph = extract_dependencies(lambda connection=Depends(get_connection): None)
        cache = DependencyCache()

        values = await resolve_dependencies(graph, None, cache)
        assert values == {"connection": "connection from pool"}

        await cache.aclose()
        assert trail == ["connection closed"]

        await close_worker_dependencies()
        assert trail == ["connection closed", "pool closed"]

    async def test_forgetting_does_not_close_what_the_parent_holds(self):
        trail = []

        @worker_scoped
        async def get_pool():
            yield "pool"
            trail.append("closed")

        graph = extract_dependencies(lambda pool=Depends(get_pool): None)
        await resolve_dependencies(graph, None, DependencyCache())

        reset_worker_dependencies()  # as a freshly forked worker does

        assert trail == []  # the parent's pool is the parent's to close

    def test_it_may_not_depend_on_something_shorter_lived(self):
        async def get_current_user():
            return "user"

        @worker_scoped
        async def get_pool(user=Depends(get_current_user)):
            return "pool"

        with pytest.raises(DependencyScope, match="lives for the worker"):
            extract_dependencies(lambda pool=Depends(get_pool): None)

    def test_it_may_not_ask_for_the_request(self):
        @worker_scoped
        async def get_pool(request):
            return "pool"

        with pytest.raises(DependencyScope, match="asks for 'request'"):
            extract_dependencies(lambda pool=Depends(get_pool): None)

    def test_it_may_depend_on_another_of_its_own_kind(self):
        @worker_scoped
        async def get_settings():
            return "settings"

        @worker_scoped
        async def get_pool(settings=Depends(get_settings)):
            return "pool"

        graph = extract_dependencies(lambda pool=Depends(get_pool): None)
        assert graph["pool"].sub_dependencies["settings"] is not None

    async def test_they_are_built_before_anything_is_served(self):
        built = []

        @worker_scoped
        async def get_pool():
            built.append("built")
            return "pool"

        graph = extract_dependencies(lambda pool=Depends(get_pool): None)
        await open_worker_dependencies([graph])

        assert built == ["built"]

        # And the request that follows finds it already there.
        await resolve_dependencies(graph, None, DependencyCache())
        assert built == ["built"]

    async def test_one_that_cannot_be_built_stops_the_worker(self):
        @worker_scoped
        async def get_pool():
            raise RuntimeError("cannot connect")

        graph = extract_dependencies(lambda pool=Depends(get_pool): None)

        with pytest.raises(RuntimeError, match="cannot connect"):
            await open_worker_dependencies([graph])
