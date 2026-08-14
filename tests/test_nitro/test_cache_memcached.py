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

"""The Memcached cache backend, against a real memcached.

It skips when emcache is not installed or no server is reachable. Note that
emcache publishes nothing for Python 3.14, so on 3.14 these always skip — the
CI matrix runs them on 3.13, which is where the backend can actually be
exercised.

The backend was written against an emcache that does not exist: it built
`emcache.Client(...)` directly, expected `get` to hand back bytes rather than
an `Item`, expected booleans from calls that return `None` and signal by
raising, called a `set_many` the client does not have, and called `flush_all`
without the node it requires. Every one of those is a case below.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("emcache", reason="emcache is not installed (it has no 3.14 wheel)")

from nitro.cache.backends.memcached import MemcachedCache, parse_servers

LOCATION = os.environ.get("NITRO_TEST_MEMCACHED", "127.0.0.1:11211")


@pytest.fixture
async def cache():
    backend = MemcachedCache(LOCATION, {"TIMEOUT": 300, "KEY_PREFIX": "nitro-test", "OPTIONS": {}})
    try:
        await backend._connect()
        # Connecting is not the same as answering: a server that accepts and
        # then says nothing gets past the connect and fails here instead, which
        # is still a server these tests cannot run against.
        await backend.clear()
    except Exception as error:
        pytest.skip(f"no usable memcached at {LOCATION}: {error}")

    yield backend
    await backend.clear()
    await backend.close()


class TestAddresses:
    def test_a_bare_host_takes_the_standard_port(self):
        assert parse_servers("localhost")[0].port == 11211

    def test_a_host_and_port(self):
        address = parse_servers("cache.test:11212")[0]
        assert (address.address, address.port) == ("cache.test", 11212)

    def test_several_servers(self):
        assert len(parse_servers("a:1,b:2")) == 2

    def test_a_list_is_accepted_too(self):
        assert len(parse_servers(["a:1", "b:2"])) == 2

    def test_a_port_that_is_not_a_number_is_reported(self):
        with pytest.raises(ValueError, match="not a memcached address"):
            parse_servers("host:not-a-port")

    def test_nothing_at_all_is_reported(self):
        with pytest.raises(ValueError, match="needs a LOCATION"):
            parse_servers("")


class TestValues:
    async def test_a_value_survives_the_round_trip(self, cache):
        # `get` hands back an Item, not the bytes inside it.
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

    async def test_pickle_can_be_asked_for(self):
        backend = MemcachedCache(LOCATION, {"OPTIONS": {"SERIALIZER": "pickle"}})
        try:
            await backend._connect()
            await backend.clear()
        except Exception as error:
            pytest.skip(f"no usable memcached at {LOCATION}: {error}")

        try:
            await backend.set("key", (1, 2))
            assert await backend.get("key") == (1, 2)
        finally:
            await backend.close()


class TestSignalling:
    """emcache raises where BaseCache is declared in booleans."""

    async def test_a_key_can_be_added_only_once(self, cache):
        assert await cache.add("key", "first") is True
        assert await cache.add("key", "second") is False
        assert await cache.get("key") == "first"

    async def test_deleting_reports_what_it_found(self, cache):
        await cache.set("key", "value")
        assert await cache.delete("key") is True
        assert await cache.delete("key") is False

    async def test_touch_reports_what_it_found(self, cache):
        await cache.set("key", "value")
        assert await cache.touch("key", timeout=60) is True
        assert await cache.touch("absent", timeout=60) is False

    async def test_incrementing_something_absent_is_reported(self, cache):
        with pytest.raises(ValueError, match="not in the cache"):
            await cache.incr("absent")

    async def test_decrementing_something_absent_is_reported(self, cache):
        with pytest.raises(ValueError, match="not in the cache"):
            await cache.decr("absent")


class TestBulk:
    async def test_many_values_at_once(self, cache):
        # There is no multi-set in the protocol, so this is a loop; the point
        # is that it works, not how.
        assert await cache.set_many({"a": 1, "b": 2, "c": [3]}) == []
        assert await cache.get_many(["a", "b", "c", "absent"]) == {"a": 1, "b": 2, "c": [3]}

    async def test_empty_reads_and_writes(self, cache):
        assert await cache.get_many([]) == {}
        assert await cache.set_many({}) == []
        assert await cache.delete_many([]) == 0

    async def test_deleting_many_counts_what_it_removed(self, cache):
        await cache.set_many({"a": 1, "b": 2})
        assert await cache.delete_many(["a", "b", "absent"]) == 2


class TestCounters:
    async def test_incr_and_decr(self, cache):
        await cache.set("counter", 5)
        assert await cache.incr("counter", 3) == 8
        assert await cache.decr("counter", 2) == 6


class TestHousekeeping:
    async def test_clear_empties_the_cache(self, cache):
        # `flush_all` is addressed to a node, and calling it without one is a
        # TypeError rather than an empty cache.
        await cache.set("key", "value")
        assert await cache.clear() is True
        assert await cache.get("key") is None

    async def test_has_key(self, cache):
        await cache.set("key", "value")
        assert await cache.has_key("key") is True
        assert await cache.has_key("absent") is False

    async def test_closing_twice_is_harmless(self, cache):
        await cache.close()
        await cache.close()

    async def test_the_client_is_built_on_first_use(self):
        # Building it is asynchronous, and a cache is configured from settings
        # long before there is a loop to build it on.
        backend = MemcachedCache(LOCATION, {"OPTIONS": {}})
        assert backend._client is None
