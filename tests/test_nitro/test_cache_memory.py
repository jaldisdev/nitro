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

import time
from unittest.mock import patch

import pytest

from nitro.cache.backends.memory import MemoryCache

pytestmark = pytest.mark.asyncio


def make_memory_cache(
    prefix: str = "",
    version: int = 1,
    timeout: int = 300,
) -> MemoryCache:
    return MemoryCache("", {"KEY_PREFIX": prefix, "VERSION": version, "TIMEOUT": timeout})


@pytest.fixture
def cache() -> MemoryCache:
    return make_memory_cache()


# ---------------------------------------------------------------------------
# get / set
# ---------------------------------------------------------------------------


class TestGetSet:
    async def test_set_and_get(self, cache: MemoryCache) -> None:
        await cache.set("key", "value")
        assert await cache.get("key") == "value"

    async def test_get_missing_key_returns_none(self, cache: MemoryCache) -> None:
        assert await cache.get("nonexistent") is None

    async def test_get_missing_key_returns_custom_default(self, cache: MemoryCache) -> None:
        assert await cache.get("nonexistent", default="fallback") == "fallback"

    async def test_set_returns_true(self, cache: MemoryCache) -> None:
        result = await cache.set("key", "value")
        assert result is True

    async def test_set_overwrites_existing(self, cache: MemoryCache) -> None:
        await cache.set("key", "old")
        await cache.set("key", "new")
        assert await cache.get("key") == "new"

    async def test_set_various_value_types(self, cache: MemoryCache) -> None:
        cases = [
            ("int", 42),
            ("float", 3.14),
            ("list", [1, 2, 3]),
            ("dict", {"a": 1}),
            ("none", None),
            ("bool", False),
        ]
        for key, value in cases:
            await cache.set(key, value)
            assert await cache.get(key) == value

    async def test_get_expired_key_returns_default(self, cache: MemoryCache) -> None:
        await cache.set("key", "value", timeout=10)
        with patch("nitro.cache.backends.memory.time") as mock_time:
            mock_time.time.return_value = time.time() + 20
            assert await cache.get("key") is None

    async def test_get_expired_key_removes_it(self, cache: MemoryCache) -> None:
        await cache.set("key", "value", timeout=10)
        with patch("nitro.cache.backends.memory.time") as mock_time:
            mock_time.time.return_value = time.time() + 20
            await cache.get("key")
        # After expiry removal, the internal dict should not hold the stale entry
        assert cache.make_key("key") not in cache._cache

    async def test_set_zero_timeout_never_expires(self, cache: MemoryCache) -> None:
        await cache.set("key", "value", timeout=0)
        with patch("nitro.cache.backends.memory.time") as mock_time:
            mock_time.time.return_value = time.time() + 999_999
            assert await cache.get("key") == "value"


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


class TestAdd:
    async def test_add_new_key_returns_true(self, cache: MemoryCache) -> None:
        assert await cache.add("key", "value") is True

    async def test_add_new_key_stores_value(self, cache: MemoryCache) -> None:
        await cache.add("key", "value")
        assert await cache.get("key") == "value"

    async def test_add_existing_key_returns_false(self, cache: MemoryCache) -> None:
        await cache.set("key", "original")
        assert await cache.add("key", "new") is False

    async def test_add_does_not_overwrite_existing(self, cache: MemoryCache) -> None:
        await cache.set("key", "original")
        await cache.add("key", "new")
        assert await cache.get("key") == "original"

    async def test_add_on_expired_key_succeeds(self, cache: MemoryCache) -> None:
        await cache.set("key", "old", timeout=10)
        with patch("nitro.cache.backends.memory.time") as mock_time:
            mock_time.time.return_value = time.time() + 20
            result = await cache.add("key", "new")
        assert result is True


# ---------------------------------------------------------------------------
# get_or_set
# ---------------------------------------------------------------------------


class TestGetOrSet:
    async def test_returns_existing_value(self, cache: MemoryCache) -> None:
        await cache.set("key", "existing")
        assert await cache.get_or_set("key", "default") == "existing"

    async def test_sets_and_returns_default_when_missing(self, cache: MemoryCache) -> None:
        result = await cache.get_or_set("key", "default")
        assert result == "default"
        assert await cache.get("key") == "default"

    async def test_accepts_callable_default(self, cache: MemoryCache) -> None:
        result = await cache.get_or_set("key", lambda: "computed")
        assert result == "computed"
        assert await cache.get("key") == "computed"

    async def test_callable_not_called_when_key_exists(self, cache: MemoryCache) -> None:
        await cache.set("key", "existing")
        called = []
        await cache.get_or_set("key", lambda: called.append(True) or "computed")
        assert not called

    async def test_none_value_treated_as_missing(self, cache: MemoryCache) -> None:
        # get_or_set uses `if val is None` so a stored None re-triggers the default
        await cache.set("key", None)
        result = await cache.get_or_set("key", "replacement")
        assert result == "replacement"


# ---------------------------------------------------------------------------
# get_many / set_many
# ---------------------------------------------------------------------------


class TestBatchOperations:
    async def test_get_many_returns_found_keys(self, cache: MemoryCache) -> None:
        await cache.set("a", 1)
        await cache.set("b", 2)
        result = await cache.get_many(["a", "b", "c"])
        assert result == {"a": 1, "b": 2}

    async def test_get_many_excludes_expired(self, cache: MemoryCache) -> None:
        await cache.set("a", 1, timeout=10)
        await cache.set("b", 2)
        with patch("nitro.cache.backends.memory.time") as mock_time:
            mock_time.time.return_value = time.time() + 20
            result = await cache.get_many(["a", "b"])
        assert result == {"b": 2}

    async def test_get_many_empty_list(self, cache: MemoryCache) -> None:
        assert await cache.get_many([]) == {}

    async def test_set_many_stores_all_values(self, cache: MemoryCache) -> None:
        await cache.set_many({"x": 10, "y": 20, "z": 30})
        assert await cache.get("x") == 10
        assert await cache.get("y") == 20
        assert await cache.get("z") == 30

    async def test_set_many_returns_empty_failed_list(self, cache: MemoryCache) -> None:
        failed = await cache.set_many({"a": 1, "b": 2})
        assert failed == []

    async def test_set_many_empty_dict(self, cache: MemoryCache) -> None:
        failed = await cache.set_many({})
        assert failed == []


# ---------------------------------------------------------------------------
# delete / delete_many
# ---------------------------------------------------------------------------


class TestDelete:
    async def test_delete_existing_key_returns_true(self, cache: MemoryCache) -> None:
        await cache.set("key", "value")
        assert await cache.delete("key") is True

    async def test_delete_removes_the_key(self, cache: MemoryCache) -> None:
        await cache.set("key", "value")
        await cache.delete("key")
        assert await cache.get("key") is None

    async def test_delete_missing_key_returns_false(self, cache: MemoryCache) -> None:
        assert await cache.delete("nonexistent") is False

    async def test_delete_many_returns_count(self, cache: MemoryCache) -> None:
        await cache.set("a", 1)
        await cache.set("b", 2)
        count = await cache.delete_many(["a", "b", "c"])
        assert count == 2

    async def test_delete_many_removes_keys(self, cache: MemoryCache) -> None:
        await cache.set("a", 1)
        await cache.set("b", 2)
        await cache.delete_many(["a", "b"])
        assert await cache.get("a") is None
        assert await cache.get("b") is None

    async def test_delete_many_empty_list(self, cache: MemoryCache) -> None:
        assert await cache.delete_many([]) == 0


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


class TestClear:
    async def test_clear_removes_all_keys(self, cache: MemoryCache) -> None:
        await cache.set("a", 1)
        await cache.set("b", 2)
        await cache.clear()
        assert await cache.get("a") is None
        assert await cache.get("b") is None

    async def test_clear_returns_true(self, cache: MemoryCache) -> None:
        assert await cache.clear() is True

    async def test_clear_empty_cache(self, cache: MemoryCache) -> None:
        assert await cache.clear() is True


# ---------------------------------------------------------------------------
# touch
# ---------------------------------------------------------------------------


class TestTouch:
    async def test_touch_existing_key_returns_true(self, cache: MemoryCache) -> None:
        await cache.set("key", "value")
        assert await cache.touch("key", timeout=600) is True

    async def test_touch_missing_key_returns_false(self, cache: MemoryCache) -> None:
        assert await cache.touch("nonexistent", timeout=600) is False

    async def test_touch_extends_expiry(self, cache: MemoryCache) -> None:
        now = time.time()
        await cache.set("key", "value", timeout=10)

        with patch("nitro.cache.backends.memory.time") as mock_time:
            # Simulate 5 seconds later — key still alive, then touch it for 600s
            mock_time.time.return_value = now + 5
            await cache.touch("key", timeout=600)

        _, expire_time = cache._cache[cache.make_key("key")]
        assert expire_time > now + 600

    async def test_touch_expired_key_returns_false(self, cache: MemoryCache) -> None:
        await cache.set("key", "value", timeout=10)
        with patch("nitro.cache.backends.memory.time") as mock_time:
            mock_time.time.return_value = time.time() + 20
            result = await cache.touch("key", timeout=600)
        assert result is False

    async def test_touch_preserves_value(self, cache: MemoryCache) -> None:
        await cache.set("key", "original")
        await cache.touch("key", timeout=600)
        assert await cache.get("key") == "original"


# ---------------------------------------------------------------------------
# incr / decr
# ---------------------------------------------------------------------------


class TestIncrDecr:
    async def test_incr_by_default_delta(self, cache: MemoryCache) -> None:
        await cache.set("counter", 5)
        assert await cache.incr("counter") == 6

    async def test_incr_by_custom_delta(self, cache: MemoryCache) -> None:
        await cache.set("counter", 0)
        assert await cache.incr("counter", delta=10) == 10

    async def test_incr_missing_key_raises(self, cache: MemoryCache) -> None:
        with pytest.raises(ValueError, match="not found"):
            await cache.incr("nonexistent")

    async def test_incr_non_integer_raises(self, cache: MemoryCache) -> None:
        await cache.set("key", "string")
        with pytest.raises(ValueError):
            await cache.incr("key")

    async def test_incr_expired_key_raises(self, cache: MemoryCache) -> None:
        await cache.set("key", 1, timeout=10)
        with patch("nitro.cache.backends.memory.time") as mock_time:
            mock_time.time.return_value = time.time() + 20
            with pytest.raises(ValueError, match="not found"):
                await cache.incr("key")

    async def test_decr_by_default_delta(self, cache: MemoryCache) -> None:
        await cache.set("counter", 5)
        assert await cache.decr("counter") == 4

    async def test_decr_by_custom_delta(self, cache: MemoryCache) -> None:
        await cache.set("counter", 10)
        assert await cache.decr("counter", delta=3) == 7

    async def test_decr_missing_key_raises(self, cache: MemoryCache) -> None:
        with pytest.raises(ValueError, match="not found"):
            await cache.decr("nonexistent")

    async def test_incr_decr_sequence(self, cache: MemoryCache) -> None:
        await cache.set("counter", 0)
        await cache.incr("counter", delta=5)
        await cache.decr("counter", delta=2)
        assert await cache.get("counter") == 3


# ---------------------------------------------------------------------------
# has_key
# ---------------------------------------------------------------------------


class TestHasKey:
    async def test_existing_key_returns_true(self, cache: MemoryCache) -> None:
        await cache.set("key", "value")
        assert await cache.has_key("key") is True

    async def test_missing_key_returns_false(self, cache: MemoryCache) -> None:
        assert await cache.has_key("nonexistent") is False

    async def test_expired_key_returns_false(self, cache: MemoryCache) -> None:
        await cache.set("key", "value", timeout=10)
        with patch("nitro.cache.backends.memory.time") as mock_time:
            mock_time.time.return_value = time.time() + 20
            assert await cache.has_key("key") is False

    async def test_expired_key_cleaned_up(self, cache: MemoryCache) -> None:
        await cache.set("key", "value", timeout=10)
        with patch("nitro.cache.backends.memory.time") as mock_time:
            mock_time.time.return_value = time.time() + 20
            await cache.has_key("key")
        assert cache.make_key("key") not in cache._cache


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    async def test_close_is_no_op(self, cache: MemoryCache) -> None:
        await cache.set("key", "value")
        await cache.close()
        # Cache should still be intact after close (no-op for memory backend)
        assert await cache.get("key") == "value"


# ---------------------------------------------------------------------------
# key versioning
# ---------------------------------------------------------------------------


class TestVersioning:
    async def test_different_versions_are_independent(self) -> None:
        cache = make_memory_cache(version=1)
        await cache.set("key", "v1", version=1)
        await cache.set("key", "v2", version=2)
        assert await cache.get("key", version=1) == "v1"
        assert await cache.get("key", version=2) == "v2"

    async def test_delete_specific_version(self) -> None:
        cache = make_memory_cache(version=1)
        await cache.set("key", "v1", version=1)
        await cache.set("key", "v2", version=2)
        await cache.delete("key", version=1)
        assert await cache.get("key", version=1) is None
        assert await cache.get("key", version=2) == "v2"

    async def test_uses_instance_version_by_default(self) -> None:
        cache = make_memory_cache(version=3)
        await cache.set("key", "val")
        # Should be stored under version 3
        assert await cache.get("key") == "val"
        assert await cache.get("key", version=3) == "val"
        assert await cache.get("key", version=1) is None


# ---------------------------------------------------------------------------
# key prefix isolation
# ---------------------------------------------------------------------------


class TestKeyPrefix:
    async def test_prefixed_caches_are_isolated(self) -> None:
        cache_a = make_memory_cache(prefix="app_a")
        cache_b = make_memory_cache(prefix="app_b")
        await cache_a.set("key", "from_a")
        assert await cache_b.get("key") is None

    async def test_prefix_included_in_stored_key(self) -> None:
        cache = make_memory_cache(prefix="myapp")
        await cache.set("foo", "bar")
        stored_key = cache.make_key("foo")
        assert stored_key.startswith("myapp:")
        assert stored_key in cache._cache
