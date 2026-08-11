"""The Redis cache backend, against a real Redis.

It skips when there is not one reachable, the way the Intercom tests do, so a
checkout without Redis still runs green.
"""

from __future__ import annotations

import os

import pytest

redis = pytest.importorskip("redis", reason="the redis package is not installed")

from nitro.cache.backends.redis import RedisCache  # noqa: E402

URL = os.environ.get("NITRO_TEST_REDIS", "redis://127.0.0.1:6379/15")


@pytest.fixture
async def cache():
    backend = RedisCache(URL, {"TIMEOUT": 300, "KEY_PREFIX": "nitro-test", "OPTIONS": {}})
    try:
        await backend._client.ping()
    except Exception as error:
        pytest.skip(f"no Redis at {URL}: {error}")

    await backend.clear()
    yield backend
    await backend.clear()
    await backend.close()


class TestValues:
    async def test_a_value_survives_the_round_trip(self, cache):
        assert await cache.set("key", {"a": 1}) is True
        assert await cache.get("key") == {"a": 1}

    async def test_a_miss_reads_as_the_default(self, cache):
        assert await cache.get("absent") is None
        assert await cache.get("absent", "fallback") == "fallback"

    @pytest.mark.parametrize(
        "value", [None, True, False, 0, 42, -1, 3.5, "text", [], {}, [1, "two"], {"a": [1]}]
    )
    async def test_json_carries_what_json_carries(self, cache, value):
        await cache.set("key", value)
        assert await cache.get("key") == value

    async def test_something_json_cannot_carry_is_refused(self, cache):
        with pytest.raises(TypeError, match="cannot be cached as JSON"):
            await cache.set("key", {1, 2})

    async def test_the_refusal_says_how_to_allow_it(self, cache):
        with pytest.raises(TypeError, match="SERIALIZER"):
            await cache.set("key", object())

    async def test_pickle_can_be_asked_for(self):
        backend = RedisCache(
            URL, {"KEY_PREFIX": "nitro-test-pickle", "OPTIONS": {"SERIALIZER": "pickle"}}
        )
        try:
            await backend._client.ping()
        except Exception as error:
            pytest.skip(f"no Redis at {URL}: {error}")

        try:
            await backend.set("key", (1, 2))
            assert await backend.get("key") == (1, 2)
        finally:
            await backend.clear()
            await backend.close()

    async def test_the_serializer_option_is_not_passed_to_the_client(self):
        # It configures this backend, not the connection under it; handing it
        # to redis-py would be an unexpected keyword argument.
        backend = RedisCache(URL, {"OPTIONS": {"SERIALIZER": "json"}})
        await backend.close()


class TestExpiry:
    async def test_a_key_can_be_added_only_once(self, cache):
        assert await cache.add("key", "first") is True
        assert await cache.add("key", "second") is False
        assert await cache.get("key") == "first"

    async def test_touch_extends_a_key(self, cache):
        await cache.set("key", "value", timeout=10)
        assert await cache.touch("key", timeout=100) is True

    async def test_touching_something_absent_reports_it(self, cache):
        assert await cache.touch("absent", timeout=100) is False

    async def test_a_zero_timeout_never_expires(self, cache):
        await cache.set("key", "value", timeout=0)
        assert await cache.get("key") == "value"
        assert await cache._client.ttl(cache.make_key("key")) == -1


class TestBulk:
    async def test_many_values_at_once(self, cache):
        await cache.set_many({"a": 1, "b": 2, "c": [3]})
        assert await cache.get_many(["a", "b", "c", "absent"]) == {"a": 1, "b": 2, "c": [3]}

    async def test_empty_reads_and_writes(self, cache):
        assert await cache.get_many([]) == {}
        assert await cache.set_many({}) == []
        assert await cache.delete_many([]) == 0

    async def test_deleting_reports_what_it_found(self, cache):
        await cache.set("a", 1)
        assert await cache.delete("a") is True
        assert await cache.delete("a") is False

    async def test_deleting_many_counts_what_it_removed(self, cache):
        await cache.set_many({"a": 1, "b": 2})
        assert await cache.delete_many(["a", "b", "absent"]) == 2


class TestCounters:
    async def test_incr_and_decr(self, cache):
        await cache._client.set(cache.make_key("counter"), 5)
        assert await cache.incr("counter", 3) == 8
        assert await cache.decr("counter", 2) == 6

    async def test_incrementing_something_that_is_not_a_number(self, cache):
        await cache.set("word", "text")
        with pytest.raises(ValueError, match="not an integer"):
            await cache.incr("word")


class TestKeys:
    async def test_the_prefix_and_version_are_part_of_the_key(self, cache):
        assert cache.make_key("key") == "nitro-test:1:key"
        assert cache.make_key("key", version=2) == "nitro-test:2:key"

    async def test_versions_do_not_see_each_other(self, cache):
        await cache.set("key", "v1", version=1)
        await cache.set("key", "v2", version=2)

        assert await cache.get("key", version=1) == "v1"
        assert await cache.get("key", version=2) == "v2"

    async def test_has_key(self, cache):
        await cache.set("key", "value")
        assert await cache.has_key("key") is True
        assert await cache.has_key("absent") is False
