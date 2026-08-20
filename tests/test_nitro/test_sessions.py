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

"""The session store."""

from __future__ import annotations

import os
import time
from typing import Any
from unittest.mock import patch

import pytest
from test_middleware import make_request

from nitro.cache.backends.memory import MemoryCache
from nitro.protocols.http import HttpResponse
from nitro.sessions import (
    KEY_ATTEMPTS,
    CacheSessionStore,
    Session,
    SessionError,
    SessionMiddleware,
    SessionStore,
    open_session,
)

pytestmark = pytest.mark.asyncio

TIMEOUT = 600


class CountingCache(MemoryCache):
    """A cache that records what was asked of it."""

    def __init__(self) -> None:
        super().__init__("", {"KEY_PREFIX": "", "VERSION": 1, "TIMEOUT": TIMEOUT})
        self.calls: list[str] = []

    async def get(self, key: str, default: Any = None, version: int | None = None) -> Any:
        self.calls.append("get")
        return await super().get(key, default, version)

    async def set(self, key, value, timeout=None, version=None) -> bool:
        self.calls.append("set")
        return await super().set(key, value, timeout, version)

    async def add(self, key, value, timeout=None, version=None) -> bool:
        self.calls.append("add")
        return await super().add(key, value, timeout, version)

    async def delete(self, key, version=None) -> bool:
        self.calls.append("delete")
        return await super().delete(key, version)


class DirectStore(CacheSessionStore):
    """A CacheSessionStore over a cache handed to it.

    The alias is never resolved, so a test needs no CACHES setting and no
    handler — and, unlike patching the property onto the class, two stores in
    one test do not share a cache.
    """

    def __init__(self, cache: MemoryCache) -> None:
        super().__init__(alias="unused", prefix="session")
        self._backing = cache

    @property
    def cache(self) -> MemoryCache:
        return self._backing


def make_store(cache: MemoryCache | None = None) -> DirectStore:
    return DirectStore(cache if cache is not None else CountingCache())


def make_session(key: str | None = None, store: SessionStore | None = None) -> Session:
    return Session(key, store or make_store(), TIMEOUT)


class TestReadingAndWriting:
    async def test_a_new_session_starts_empty(self):
        session = make_session()
        assert await session.items() == {}
        assert session.key is None

    async def test_a_saved_session_is_readable_under_its_key(self):
        store = make_store()
        session = make_session(store=store)
        await session.set("region", "eu")
        await session.save()

        assert session.key is not None
        assert await make_session(session.key, store).get("region") == "eu"

    async def test_the_bag_is_read_once_however_many_gets(self):
        cache = CountingCache()
        store = make_store(cache)
        session = make_session(store=store)
        await session.set("a", 1)
        await session.save()

        cache.calls.clear()
        reopened = make_session(session.key, store)
        for _ in range(5):
            await reopened.get("a")

        assert cache.calls == ["get"]

    async def test_an_untouched_session_reads_and_writes_nothing(self):
        cache = CountingCache()
        session = make_session("whatever", make_store(cache))
        await session.save()

        assert cache.calls == []
        assert not session.loaded

    async def test_an_unchanged_session_is_not_rewritten(self):
        cache = CountingCache()
        store = make_store(cache)
        session = make_session(store=store)
        await session.set("a", 1)
        await session.save()

        reopened = make_session(session.key, store)
        await reopened.get("a")
        cache.calls.clear()
        await reopened.save()

        assert cache.calls == []

    async def test_force_rewrites_an_unchanged_session(self):
        cache = CountingCache()
        store = make_store(cache)
        session = make_session(store=store)
        await session.set("a", 1)
        await session.save()

        reopened = make_session(session.key, store)
        await reopened.get("a")
        cache.calls.clear()
        await reopened.save(force=True)

        assert cache.calls == ["set"]

    async def test_pop_removes_and_returns(self):
        session = make_session()
        await session.set("a", 1)
        assert await session.pop("a") == 1
        assert await session.pop("a", "gone") == "gone"
        assert not await session.has("a")

    async def test_items_is_a_copy(self):
        session = make_session()
        await session.set("a", 1)
        (await session.items())["a"] = 2
        assert await session.get("a") == 1

    async def test_clear_empties_the_bag_but_keeps_the_key(self):
        store = make_store()
        session = make_session(store=store)
        await session.set("a", 1)
        await session.save()
        key = session.key

        await session.clear()
        await session.save()

        assert session.key == key
        assert await make_session(key, store).items() == {}


class TestKeys:
    async def test_a_key_is_allocated_only_when_there_is_something_to_keep(self):
        session = make_session()
        await session.set("a", 1)
        assert session.key is None
        await session.save()
        assert session.key is not None

    async def test_a_session_emptied_before_saving_leaves_nothing_behind(self):
        cache = CountingCache()
        session = make_session(store=make_store(cache))
        await session.set("a", 1)
        await session.pop("a")
        await session.save()

        assert session.key is None
        assert "add" not in cache.calls

    async def test_an_unknown_key_is_dropped_rather_than_adopted(self):
        # Session fixation: a key nothing is stored under must not become the
        # key a real session is written to, or one planted by an attacker would
        # be a session they can already read.
        store = make_store()
        session = make_session("planted-by-an-attacker", store)
        await session.set("signed_in", True)
        await session.save()

        assert session.key is not None
        assert session.key != "planted-by-an-attacker"
        assert await make_session("planted-by-an-attacker", store).items() == {}

    async def test_allocation_gives_up_rather_than_spinning(self):
        class RefusingCache(CountingCache):
            async def add(self, key, value, timeout=None, version=None) -> bool:
                self.calls.append("add")
                return False

        cache = RefusingCache()
        session = make_session(store=make_store(cache))
        await session.set("a", 1)

        with pytest.raises(SessionError, match="refusing writes"):
            await session.save()
        assert cache.calls.count("add") == KEY_ATTEMPTS

    async def test_keys_differ_between_sessions(self):
        store = make_store()
        keys = set()
        for index in range(20):
            session = make_session(store=store)
            await session.set("n", index)
            await session.save()
            keys.add(session.key)
        assert len(keys) == 20


class TestCycle:
    async def test_cycle_keeps_the_data_under_a_new_key(self):
        store = make_store()
        session = make_session(store=store)
        await session.set("signed_in", True)
        await session.save()
        before = session.key

        await session.cycle()

        assert session.key != before
        assert await session.get("signed_in") is True
        assert await make_session(session.key, store).get("signed_in") is True

    async def test_cycle_removes_the_old_key(self):
        store = make_store()
        session = make_session(store=store)
        await session.set("a", 1)
        await session.save()
        before = session.key

        await session.cycle()

        assert await make_session(before, store).items() == {}

    async def test_cycle_works_before_a_key_exists(self):
        session = make_session()
        await session.set("a", 1)
        await session.cycle()
        assert session.key is not None
        assert await session.get("a") == 1


class TestFlush:
    async def test_flush_deletes_the_session_and_drops_the_key(self):
        store = make_store()
        session = make_session(store=store)
        await session.set("a", 1)
        await session.save()
        before = session.key

        await session.flush()

        assert session.key is None
        assert await session.items() == {}
        assert await make_session(before, store).items() == {}

    async def test_saving_after_a_flush_stores_nothing(self):
        cache = CountingCache()
        session = make_session(store=make_store(cache))
        await session.set("a", 1)
        await session.save()

        await session.flush()
        cache.calls.clear()
        await session.save()

        assert cache.calls == []

    async def test_storing_after_a_flush_starts_a_new_session(self):
        session = make_session()
        await session.set("a", 1)
        await session.save()
        before = session.key

        await session.flush()
        await session.set("b", 2)
        await session.save()

        assert session.key is not None
        assert session.key != before


class TestExpiry:
    async def test_a_session_expires(self):
        store = make_store(MemoryCache("", {"KEY_PREFIX": "", "VERSION": 1, "TIMEOUT": TIMEOUT}))
        session = Session(None, store, timeout=10)
        await session.set("a", 1)
        await session.save()

        with patch("nitro.cache.backends.memory.time") as clock:
            clock.time.return_value = time.time() + 20
            assert await make_session(session.key, store).items() == {}

    async def test_touch_reports_whether_there_was_a_session(self):
        store = make_store()
        session = make_session(store=store)
        assert await session.touch() is False

        await session.set("a", 1)
        await session.save()
        assert await session.touch() is True


class TestOpenSession:
    async def test_open_session_reads_nothing_until_it_is_used(self):
        cache = CountingCache()
        store = make_store(cache)
        await open_session("some-key", store=store, timeout=TIMEOUT)
        assert cache.calls == []

    async def test_open_session_round_trips(self):
        store = make_store()
        session = await open_session(store=store, timeout=TIMEOUT)
        await session.set("a", 1)
        await session.save()

        reopened = await open_session(session.key, store=store, timeout=TIMEOUT)
        assert await reopened.get("a") == 1


class TestStoreProtocol:
    async def test_a_store_of_your_own_is_enough(self):
        class DictStore:
            def __init__(self) -> None:
                self.bags: dict[str, dict[str, Any]] = {}
                self.next = 0

            async def read(self, key):
                return self.bags.get(key)

            async def create(self, data, timeout):
                self.next += 1
                key = f"k{self.next}"
                self.bags[key] = data
                return key

            async def write(self, key, data, timeout):
                self.bags[key] = data

            async def remove(self, key):
                self.bags.pop(key, None)

            async def touch(self, key, timeout):
                return key in self.bags

        store = DictStore()
        assert isinstance(store, SessionStore)

        session = Session(None, store, TIMEOUT)
        await session.set("a", 1)
        await session.save()

        assert session.key == "k1"
        assert store.bags == {"k1": {"a": 1}}


class TestAgainstRedis:
    """The store over a real Redis, when there is one.

    Skipped otherwise, the way the cache and Intercom tests are, so a checkout
    without Redis still runs green. Worth having despite the store being
    backend-agnostic by construction: `add` carrying the atomicity that key
    allocation depends on is a promise of the backend, not of this module.
    """

    @pytest.fixture
    async def store(self):
        pytest.importorskip("redis", reason="the redis package is not installed")
        from nitro.cache.backends.redis import RedisCache

        url = os.environ.get("NITRO_TEST_REDIS", "redis://127.0.0.1:6379/15")
        cache = RedisCache(
            url, {"TIMEOUT": TIMEOUT, "KEY_PREFIX": "nitro-session-test", "OPTIONS": {}}
        )
        try:
            await cache._client.ping()
        except Exception as error:
            pytest.skip(f"no Redis at {url}: {error}")

        yield DirectStore(cache)

        await cache.clear()
        await cache.close()

    async def test_a_session_round_trips(self, store):
        session = Session(None, store, TIMEOUT)
        await session.set("region", "eu")
        await session.save()

        assert await Session(session.key, store, TIMEOUT).get("region") == "eu"

    async def test_allocation_does_not_hand_out_a_key_twice(self, store):
        first = Session(None, store, TIMEOUT)
        await first.set("a", 1)
        await first.save()

        second = Session(None, store, TIMEOUT)
        await second.set("a", 2)
        await second.save()

        assert first.key != second.key
        assert await Session(first.key, store, TIMEOUT).get("a") == 1

    async def test_cycle_moves_the_bag(self, store):
        session = Session(None, store, TIMEOUT)
        await session.set("a", 1)
        await session.save()
        before = session.key

        await session.cycle()

        assert await Session(session.key, store, TIMEOUT).get("a") == 1
        assert await Session(before, store, TIMEOUT).items() == {}


# ── The middleware ───────────────────────────────────────────────────────────


def make_middleware(store: SessionStore, **overrides: Any) -> SessionMiddleware:
    """The real middleware over a store handed to it.

    Injecting rather than subclassing on purpose: an earlier version of this
    file reimplemented `__http__` to get a store in, and the copy carried the
    same bug the original did, so the two agreed and the test proved nothing.
    """
    middleware = SessionMiddleware(store=store)
    for name, value in overrides.items():
        setattr(middleware, name, value)
    return middleware


def cookies_of(response: HttpResponse) -> list[str]:
    return [value for name, value in response.header_pairs() if name == "set-cookie"]


def session_cookie(response: HttpResponse) -> str | None:
    for value in cookies_of(response):
        if value.startswith("sessionid="):
            return value
    return None


def key_from(response: HttpResponse) -> str | None:
    cookie = session_cookie(response)
    if cookie is None:
        return None
    return cookie.split("=", 1)[1].split(";", 1)[0]


class TestMiddleware:
    async def test_a_request_that_ignores_the_session_writes_nothing(self):
        cache = CountingCache()
        middleware = make_middleware(make_store(cache))

        async def handler(request):
            return HttpResponse("hello")

        response = await middleware.__http__(make_request(), handler)

        assert cache.calls == []
        assert cookies_of(response) == []

    async def test_storing_something_sets_the_cookie(self):
        store = make_store()
        middleware = make_middleware(store)

        async def handler(request):
            await request.state.session.set("region", "eu")
            return HttpResponse("hello")

        response = await middleware.__http__(make_request(), handler)

        key = key_from(response)
        assert key is not None
        assert await Session(key, store, TIMEOUT).get("region") == "eu"

    async def test_the_cookie_carries_the_configured_attributes(self):
        middleware = make_middleware(make_store())

        async def handler(request):
            await request.state.session.set("a", 1)
            return HttpResponse("hello")

        cookie = session_cookie(await middleware.__http__(make_request(), handler))

        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=lax" in cookie
        assert "Path=/" in cookie

    async def test_a_returning_visitor_keeps_their_key_and_gets_no_new_cookie(self):
        store = make_store()
        middleware = make_middleware(store)

        session = Session(None, store, TIMEOUT)
        await session.set("region", "eu")
        await session.save()

        seen = {}

        async def handler(request):
            seen["region"] = await request.state.session.get("region")
            return HttpResponse("hello")

        request = make_request(headers={"cookie": f"sessionid={session.key}"})
        response = await middleware.__http__(request, handler)

        assert seen["region"] == "eu"
        assert cookies_of(response) == []

    async def test_a_flushed_session_clears_the_cookie(self):
        store = make_store()
        middleware = make_middleware(store)

        session = Session(None, store, TIMEOUT)
        await session.set("a", 1)
        await session.save()

        async def handler(request):
            await request.state.session.flush()
            return HttpResponse("bye")

        request = make_request(headers={"cookie": f"sessionid={session.key}"})
        response = await middleware.__http__(request, handler)

        assert "Max-Age=0" in session_cookie(response)
        assert await Session(session.key, store, TIMEOUT).items() == {}

    async def test_a_cycled_session_sets_the_new_key(self):
        store = make_store()
        middleware = make_middleware(store)

        session = Session(None, store, TIMEOUT)
        await session.set("a", 1)
        await session.save()
        before = session.key

        async def handler(request):
            await request.state.session.cycle()
            return HttpResponse("in")

        request = make_request(headers={"cookie": f"sessionid={before}"})
        response = await middleware.__http__(request, handler)

        assert key_from(response) not in (None, before)

    async def test_an_unmodified_session_is_not_rewritten(self):
        cache = CountingCache()
        store = make_store(cache)
        middleware = make_middleware(store)

        session = Session(None, store, TIMEOUT)
        await session.set("a", 1)
        await session.save()
        cache.calls.clear()

        async def handler(request):
            await request.state.session.get("a")
            return HttpResponse("hello")

        request = make_request(headers={"cookie": f"sessionid={session.key}"})
        await middleware.__http__(request, handler)

        assert cache.calls == ["get"]

    async def test_refresh_on_access_rewrites_and_resets_the_cookie(self):
        cache = CountingCache()
        store = make_store(cache)
        middleware = make_middleware(store, refresh_on_access=True)

        session = Session(None, store, TIMEOUT)
        await session.set("a", 1)
        await session.save()
        cache.calls.clear()

        async def handler(request):
            await request.state.session.get("a")
            return HttpResponse("hello")

        request = make_request(headers={"cookie": f"sessionid={session.key}"})
        response = await middleware.__http__(request, handler)

        assert cache.calls == ["get", "set"]
        assert key_from(response) == session.key

    async def test_refresh_on_access_still_ignores_an_untouched_session(self):
        cache = CountingCache()
        middleware = make_middleware(make_store(cache), refresh_on_access=True)

        async def handler(request):
            return HttpResponse("hello")

        response = await middleware.__http__(
            make_request(headers={"cookie": "sessionid=whatever"}), handler
        )

        assert cache.calls == []
        assert cookies_of(response) == []

    async def test_a_key_that_names_nothing_yields_a_fresh_session(self):
        store = make_store()
        middleware = make_middleware(store)

        async def handler(request):
            await request.state.session.set("a", 1)
            return HttpResponse("hello")

        request = make_request(headers={"cookie": "sessionid=planted-by-an-attacker"})
        response = await middleware.__http__(request, handler)

        assert key_from(response) not in (None, "planted-by-an-attacker")


class TestOverridingTheTransport:
    """The Jaldis case: the key rides in a token, not a cookie."""

    async def test_a_subclass_can_carry_the_key_anywhere(self):
        store = make_store()

        class TokenSessions(SessionMiddleware):
            def read_key(self, connection):
                return connection.headers.get("x-session-id")

            def write_key(self, response, key):
                response.headers["x-session-id"] = key

        middleware = TokenSessions(store=store)

        session = Session(None, store, TIMEOUT)
        await session.set("region", "eu")
        await session.save()

        seen = {}

        async def handler(request):
            seen["region"] = await request.state.session.get("region")
            return HttpResponse("hello")

        request = make_request(headers={"x-session-id": session.key})
        response = await middleware.__http__(request, handler)

        assert seen["region"] == "eu"
        assert cookies_of(response) == []

    async def test_a_no_op_write_key_sets_nothing(self):
        class TokenSessions(SessionMiddleware):
            def read_key(self, connection):
                return None

            def write_key(self, response, key):
                pass

        middleware = TokenSessions(store=make_store())

        async def handler(request):
            await request.state.session.set("a", 1)
            return HttpResponse("hello")

        response = await middleware.__http__(make_request(), handler)
        assert cookies_of(response) == []
