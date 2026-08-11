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

import pytest

from nitro.cache.backends.memory import MemoryCache
from nitro.cache.handler import DEFAULT_CACHE_ALIAS, CacheHandler


def make_handler(config: dict | None = None) -> CacheHandler:
    if config is None:
        config = {
            "default": {
                "BACKEND": "nitro.cache.backends.memory.MemoryCache",
                "TIMEOUT": 300,
            }
        }
    return CacheHandler(config)


@pytest.fixture
def handler() -> CacheHandler:
    return make_handler()


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_default_cache_alias(self) -> None:
        assert DEFAULT_CACHE_ALIAS == "default"


# ---------------------------------------------------------------------------
# CacheHandler.__getitem__
# ---------------------------------------------------------------------------


class TestGetItem:
    def test_returns_cache_for_known_alias(self, handler: CacheHandler) -> None:
        cache = handler["default"]
        assert isinstance(cache, MemoryCache)

    def test_raises_for_unknown_alias(self, handler: CacheHandler) -> None:
        with pytest.raises(KeyError, match="'missing'"):
            _ = handler["missing"]

    def test_same_instance_returned_on_repeated_access(self, handler: CacheHandler) -> None:
        first = handler["default"]
        second = handler["default"]
        assert first is second

    def test_multiple_aliases_resolved_independently(self) -> None:
        handler = make_handler(
            {
                "default": {
                    "BACKEND": "nitro.cache.backends.memory.MemoryCache",
                    "TIMEOUT": 300,
                },
                "sessions": {
                    "BACKEND": "nitro.cache.backends.memory.MemoryCache",
                    "TIMEOUT": 3600,
                },
            }
        )
        default_cache = handler["default"]
        session_cache = handler["sessions"]
        assert default_cache is not session_cache
        assert isinstance(default_cache, MemoryCache)
        assert isinstance(session_cache, MemoryCache)


# ---------------------------------------------------------------------------
# CacheHandler._create_cache (backend instantiation)
# ---------------------------------------------------------------------------


class TestCreateCache:
    def test_creates_memory_cache_from_string_backend(self) -> None:
        handler = make_handler(
            {
                "default": {
                    "BACKEND": "nitro.cache.backends.memory.MemoryCache",
                    "TIMEOUT": 120,
                    "KEY_PREFIX": "test",
                    "VERSION": 2,
                }
            }
        )
        cache = handler["default"]
        assert isinstance(cache, MemoryCache)
        assert cache.default_timeout == 120
        assert cache.key_prefix == "test"
        assert cache.version == 2

    def test_creates_cache_from_class_backend(self) -> None:
        handler = make_handler(
            {
                "default": {
                    "BACKEND": MemoryCache,
                    "TIMEOUT": 60,
                }
            }
        )
        cache = handler["default"]
        assert isinstance(cache, MemoryCache)
        assert cache.default_timeout == 60

    def test_location_passed_to_backend(self) -> None:
        handler = make_handler(
            {
                "default": {
                    "BACKEND": MemoryCache,
                    "LOCATION": "some-location",
                }
            }
        )
        cache = handler["default"]
        assert cache.location == "some-location"

    def test_missing_location_defaults_to_empty_string(self) -> None:
        handler = make_handler({"default": {"BACKEND": MemoryCache}})
        cache = handler["default"]
        assert cache.location == ""

    def test_options_forwarded_to_backend(self) -> None:
        handler = make_handler(
            {
                "default": {
                    "BACKEND": MemoryCache,
                    "OPTIONS": {"custom_opt": True},
                }
            }
        )
        cache = handler["default"]
        assert cache.options == {"custom_opt": True}


# ---------------------------------------------------------------------------
# CacheHandler.all()
# ---------------------------------------------------------------------------


class TestAll:
    def test_all_returns_all_configured_caches(self) -> None:
        handler = make_handler(
            {
                "default": {"BACKEND": MemoryCache},
                "sessions": {"BACKEND": MemoryCache},
                "api": {"BACKEND": MemoryCache},
            }
        )
        all_caches = handler.all()
        assert set(all_caches.keys()) == {"default", "sessions", "api"}
        assert all(isinstance(c, MemoryCache) for c in all_caches.values())

    def test_all_instantiates_unaccesssed_caches(self) -> None:
        handler = make_handler(
            {
                "default": {"BACKEND": MemoryCache},
                "other": {"BACKEND": MemoryCache},
            }
        )
        # Access only 'default' directly
        _ = handler["default"]
        all_caches = handler.all()
        # 'other' was never accessed but all() should still return it
        assert "other" in all_caches

    def test_all_returns_same_instances_as_getitem(self) -> None:
        handler = make_handler(
            {
                "default": {"BACKEND": MemoryCache},
            }
        )
        direct = handler["default"]
        all_caches = handler.all()
        assert all_caches["default"] is direct


# ---------------------------------------------------------------------------
# CacheHandler.close_all()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCloseAll:
    async def test_close_all_clears_instance_cache(self, handler: CacheHandler) -> None:
        _ = handler["default"]
        assert "default" in handler._caches
        await handler.close_all()
        assert handler._caches == {}

    async def test_close_all_on_empty_handler(self) -> None:
        handler = make_handler()
        # Should not raise even if no caches have been accessed
        await handler.close_all()

    async def test_caches_reusable_after_close_all(self, handler: CacheHandler) -> None:
        cache = handler["default"]
        await cache.set("key", "value")
        await handler.close_all()
        # After close_all, accessing the alias creates a fresh instance
        new_cache = handler["default"]
        assert new_cache is not cache


# ---------------------------------------------------------------------------
# Cache isolation via handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestHandlerIsolation:
    async def test_separate_caches_do_not_share_keys(self) -> None:
        handler = make_handler(
            {
                "a": {"BACKEND": MemoryCache},
                "b": {"BACKEND": MemoryCache},
            }
        )
        await handler["a"].set("shared_key", "from_a")
        assert await handler["b"].get("shared_key") is None

    async def test_separate_handlers_do_not_share_state(self) -> None:
        h1 = make_handler()
        h2 = make_handler()
        await h1["default"].set("key", "from_h1")
        assert await h2["default"].get("key") is None
