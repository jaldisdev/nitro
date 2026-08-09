import asyncio

import pytest

from nitro.di import (
    DependencyCache,
    DependencyCycle,
    DependencyError,
    Depends,
    extract_dependencies,
    resolve_dependencies,
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
        async def handler(request, websocket, session, scope): ...

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
