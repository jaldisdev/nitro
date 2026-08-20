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

"""Server-side state keyed to a connection.

A session is a bag of JSON-compatible values held under an opaque key, kept in
the cache named by ``SESSION_CACHE``:

    session = await open_session(key)
    await session.set("region", "eu")
    await session.save()

Every operation is a coroutine, and deliberately so. A mapping that reads and
writes a remote store behind ``session["x"]`` hides the one thing worth seeing
in an async application, and the sync interface it needs is what pushes a store
into blocking calls. The bag is read once per session object and held, so ten
`get` calls cost one round trip and the `await`s are honest about where the
trip happens.

What this does not do is decide who the visitor is. There is no login, no
identity, no rotation on sign-in — a session is state, and which state means
"signed in" belongs to the application. The one concession is :meth:`Session.cycle`,
which exists because session fixation has to be defended against at sign-in and
only the application knows when that is.
"""

from __future__ import annotations

import contextlib
import http.cookies as http_cookies
from typing import Any, Protocol, runtime_checkable

from nitro.middleware.base import Middleware
from nitro.protocols.http import HttpRequest, HttpResponse
from nitro.utils.crypto import get_random_string

__all__ = [
    "CacheSessionStore",
    "Session",
    "SessionError",
    "SessionMiddleware",
    "SessionStore",
    "open_session",
]

#: Characters and length of a session key. 32 characters of the default
#: alphabet is about 190 bits, which is the point: a key is guessed or it is
#: not, and nothing else stands between a guess and the state behind it.
KEY_LENGTH = 32

#: How many times a fresh key is tried before giving up. A collision needs two
#: 190-bit draws to match, so more than one attempt is already the impossible
#: case; the loop exists so that a store which is refusing writes for some
#: other reason fails loudly instead of spinning.
KEY_ATTEMPTS = 5


class SessionError(Exception):
    """A session could not be read, written or allocated."""


@runtime_checkable
class SessionStore(Protocol):
    """Where the bags are kept.

    Implement this to hold sessions somewhere the cache cannot — a table that
    outlives an eviction, or one an application already queries for its own
    reasons. :class:`CacheSessionStore` is the default and is enough for state
    that may be lost.
    """

    async def read(self, key: str) -> dict[str, Any] | None:
        """The bag stored under `key`, or `None` if there is none."""
        ...

    async def create(self, data: dict[str, Any], timeout: int) -> str:
        """Store `data` under a key nothing else holds, and return that key."""
        ...

    async def write(self, key: str, data: dict[str, Any], timeout: int) -> None:
        """Replace what is stored under `key`."""
        ...

    async def remove(self, key: str) -> None:
        """Forget `key`. Removing one that does not exist is not an error."""
        ...

    async def touch(self, key: str, timeout: int) -> bool:
        """Extend `key`'s life without rewriting it."""
        ...


class CacheSessionStore:
    """Sessions in the cache named by ``SESSION_CACHE``.

    SECURITY WARNING: a cache may evict. A session held here can disappear
    before it expires — signing a visitor out, or losing a half-finished
    ceremony such as a passkey challenge. Give sessions a cache of their own
    rather than sharing one under memory pressure, and supply a
    :class:`SessionStore` of your own where losing one is not acceptable.
    """

    def __init__(self, alias: str | None = None, prefix: str | None = None) -> None:
        from nitro.settings import settings

        self.alias = settings.SESSION_CACHE if alias is None else alias
        self.prefix = settings.SESSION_KEY_PREFIX if prefix is None else prefix

    @property
    def cache(self) -> Any:
        # Resolved per call rather than held: the cache handler builds a backend
        # on first use, and doing that when the store is constructed would
        # connect for a session nothing has asked for yet.
        from nitro.cache import caches

        return caches[self.alias]

    def _entry(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    async def read(self, key: str) -> dict[str, Any] | None:
        data = await self.cache.get(self._entry(key))
        # A cache holding something that is not a bag is a key collision with
        # whatever else is in there, not a session.
        return data if isinstance(data, dict) else None

    async def create(self, data: dict[str, Any], timeout: int) -> str:
        cache = self.cache
        for _ in range(KEY_ATTEMPTS):
            key = get_random_string(KEY_LENGTH)
            # `add` rather than a read followed by a write: the check and the
            # claim are one operation, so two requests allocating at once
            # cannot be handed the same key.
            if await cache.add(self._entry(key), data, timeout=timeout):
                return key
        raise SessionError(
            f"no session key could be allocated in {KEY_ATTEMPTS} attempts; "
            f"the {self.alias!r} cache is refusing writes"
        )

    async def write(self, key: str, data: dict[str, Any], timeout: int) -> None:
        await self.cache.set(self._entry(key), data, timeout=timeout)

    async def remove(self, key: str) -> None:
        await self.cache.delete(self._entry(key))

    async def touch(self, key: str, timeout: int) -> bool:
        return await self.cache.touch(self._entry(key), timeout=timeout)


class Session:
    """One visitor's bag, read once and written when it has changed."""

    __slots__ = ("_data", "_key", "_modified", "_store", "_timeout")

    def __init__(self, key: str | None, store: SessionStore, timeout: int) -> None:
        self._key = key
        self._store = store
        self._timeout = timeout
        self._data: dict[str, Any] | None = None
        self._modified = False

    @property
    def key(self) -> str | None:
        """The key this bag is held under, or `None` if it has none yet.

        A caller comparing this against the key it opened the session with is
        how it learns that one was allocated, cycled or dropped.
        """
        return self._key

    @property
    def modified(self) -> bool:
        """Whether anything has changed since the bag was read."""
        return self._modified

    @property
    def loaded(self) -> bool:
        """Whether the bag has been read. A session nothing touched has not."""
        return self._data is not None

    async def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data

        if self._key is None:
            self._data = {}
            return self._data

        stored = await self._store.read(self._key)
        if stored is None:
            # The key names nothing: it expired, it was evicted, or it was
            # invented by whoever sent it. Dropping it rather than writing to
            # it is what stops a chosen key from becoming a real session — an
            # attacker who plants `sessionid=known` would otherwise have the
            # visitor fill in a bag they can already read.
            self._key = None
            self._data = {}
            return self._data

        self._data = stored
        return self._data

    async def get(self, name: str, default: Any = None) -> Any:
        """The value stored under `name`, or `default`."""
        data = await self._load()
        return data.get(name, default)

    async def set(self, name: str, value: Any) -> None:
        """Store `value` under `name`."""
        data = await self._load()
        data[name] = value
        self._modified = True

    async def pop(self, name: str, default: Any = None) -> Any:
        """Remove `name` and return what it held, or `default`."""
        data = await self._load()
        if name not in data:
            return default
        self._modified = True
        return data.pop(name)

    async def has(self, name: str) -> bool:
        """Whether `name` is in the bag."""
        return name in await self._load()

    async def items(self) -> dict[str, Any]:
        """The whole bag, as a copy — changing it does not change the session."""
        return dict(await self._load())

    async def clear(self) -> None:
        """Empty the bag, keeping the key."""
        data = await self._load()
        if data:
            data.clear()
            self._modified = True

    async def flush(self) -> None:
        """Delete the session and drop its key.

        The bag is gone and this object is empty; storing something afterwards
        starts a new session under a new key.
        """
        if self._key is not None:
            await self._store.remove(self._key)
        self._key = None
        self._data = {}
        self._modified = False

    async def cycle(self) -> None:
        """Move the bag to a new key, keeping what is in it.

        Call this when a visitor signs in. A key an attacker was able to fix
        beforehand stops being the key the session is held under, which is the
        whole of the defence — and Nitro cannot do it for you, because it does
        not know what signing in means here.
        """
        data = await self._load()
        previous = self._key
        self._key = await self._store.create(dict(data), self._timeout)
        if previous is not None:
            await self._store.remove(previous)
        self._modified = False

    async def touch(self) -> bool:
        """Extend the session's life without rewriting it.

        Returns whether there was a session to extend. Worth calling on a
        connection that stays open longer than ``SESSION_TIMEOUT``, which would
        otherwise outlive its own bag.
        """
        if self._key is None:
            return False
        return await self._store.touch(self._key, self._timeout)

    async def save(self, force: bool = False) -> None:
        """Write the bag back if it changed, allocating a key if it has none.

        `force` writes an unchanged bag as well, which is what extends the life
        of a session that was only read.
        """
        if self._data is None:
            return
        if not self._modified and not force:
            return

        # Nothing to keep and nowhere it is kept: a request that put something
        # in the bag and took it out again should not leave a session behind.
        if self._key is None and not self._data:
            return

        if self._key is None:
            self._key = await self._store.create(dict(self._data), self._timeout)
        else:
            await self._store.write(self._key, dict(self._data), self._timeout)

        self._modified = False

    def __repr__(self) -> str:
        state = "unloaded" if self._data is None else f"{len(self._data)} keys"
        return f"Session(key={self._key!r}, {state})"


_default_store: SessionStore | None = None


def default_store() -> SessionStore:
    """The store built from settings, made once."""
    global _default_store
    if _default_store is None:
        _default_store = CacheSessionStore()
    return _default_store


def reset_default_store() -> None:
    """Forget the store built from settings, so the next call rereads them."""
    global _default_store
    _default_store = None


async def open_session(
    key: str | None = None,
    *,
    store: SessionStore | None = None,
    timeout: int | None = None,
) -> Session:
    """A session for `key`, or a new one when there is no key.

    This is the primitive :class:`~nitro.sessions.SessionMiddleware` is built
    on, and it is public because the middleware cannot reach every case. A
    WebTransport handler authenticating over its own transport, after the
    connection is up, opens the session here instead:

        await transport.accept()
        token = await transport.receive_datagram()
        transport.state.session = await open_session(key_for(token))

    Nothing is read until the session is used, so opening one costs nothing.
    """
    if timeout is None:
        from nitro.settings import settings

        timeout = settings.SESSION_TIMEOUT

    return Session(key, store or default_store(), timeout)


class SessionMiddleware(Middleware):
    """Opens a session for each request and writes it back when it changed.

    The session is left at ``request.state.session``. It is opened but not
    read, so a request that never touches it costs nothing.

    Where the key travels is not this class's decision. :meth:`read_key` and
    :meth:`write_key` carry it in a cookie by default; an application whose key
    lives somewhere else overrides them:

        class TokenSessionMiddleware(SessionMiddleware):
            def read_key(self, connection):
                return getattr(connection.state, "token_claims", {}).get("session")

            def write_key(self, response, key):
                pass  # the token carries it; there is nothing to set

    SECURITY WARNING: the cookie default means the browser sends the key
    whether or not the request came from your own site. Install
    :class:`~nitro.middleware.common.OriginMiddleware` alongside this, or
    carry the key somewhere a browser does not attach on its own.
    """

    def __init__(self, app: Any | None = None, *, store: SessionStore | None = None) -> None:
        super().__init__(app)

        from nitro.settings import settings

        #: Left unset to use the store built from settings. Supplied directly by
        #: an application holding sessions somewhere of its own, and by tests.
        self.store = store
        self.timeout: int = settings.SESSION_TIMEOUT
        self.refresh_on_access: bool = settings.SESSION_REFRESH_ON_ACCESS
        self.cookie_name: str = settings.SESSION_COOKIE_NAME
        self.cookie_path: str = settings.SESSION_COOKIE_PATH
        self.cookie_domain: str | None = settings.SESSION_COOKIE_DOMAIN
        self.cookie_secure: bool = settings.SESSION_COOKIE_SECURE
        self.cookie_httponly: bool = settings.SESSION_COOKIE_HTTPONLY
        self.cookie_samesite: str | None = settings.SESSION_COOKIE_SAMESITE

    def read_key(self, connection: Any) -> str | None:
        """The session key `connection` arrived with, if any.

        Reads ``SESSION_COOKIE_NAME``, from the request's own cookies where
        there are any and from the handshake's ``Cookie`` header otherwise — a
        WebSocket upgrade is an HTTP request, so a browser sends cookies on it.

        WebTransport is the case with no answer here. Its handshake carries no
        cookies at all: the W3C API attaches neither them nor HTTP credentials,
        deliberately, so there is no ambient key to find and this returns
        `None` for every browser connection. A WebTransport application either
        puts the key in the URL and overrides this to read
        ``connection.query_params``, or authenticates over the transport once
        it is open and calls :func:`open_session` itself.
        """
        cookies = getattr(connection, "cookies", None)
        if cookies is None:
            headers = getattr(connection, "headers", None)
            header = headers.get("cookie") if headers is not None else None
            if not header:
                return None
            jar = http_cookies.SimpleCookie()
            # A malformed Cookie header should not refuse the connection; a key
            # that did parse is still worth having.
            with contextlib.suppress(http_cookies.CookieError):
                jar.load(header)
            morsel = jar.get(self.cookie_name)
            return morsel.value if morsel is not None else None

        return cookies.get(self.cookie_name) or None

    def write_key(self, response: HttpResponse, key: str) -> None:
        """Tell the client the key to send next time."""
        response.set_cookie(
            self.cookie_name,
            key,
            max_age=self.timeout,
            path=self.cookie_path,
            domain=self.cookie_domain,
            secure=self.cookie_secure,
            httponly=self.cookie_httponly,
            samesite=self.cookie_samesite,
        )

    def clear_key(self, response: HttpResponse) -> None:
        """Tell the client to forget the key, after a session was flushed."""
        response.delete_cookie(self.cookie_name, path=self.cookie_path, domain=self.cookie_domain)

    async def __http__(self, request: HttpRequest, call_next: Any) -> Any:
        opened_with = self.read_key(request)
        session = await open_session(opened_with, store=self.store, timeout=self.timeout)
        request.state.session = session

        response = await call_next(request)

        await session.save(force=self.refresh_on_access and session.loaded)

        if not isinstance(response, HttpResponse):
            # The handler answered through the protocol itself, so there is no
            # response object left to carry a cookie. The bag is saved either
            # way; only the key cannot be handed back.
            return response

        if session.key != opened_with:
            if session.key is None:
                self.clear_key(response)
            else:
                self.write_key(response, session.key)
        elif self.refresh_on_access and session.loaded and session.key is not None:
            # Same key, but the store's copy was just given a fresh life, so the
            # cookie's should be too — otherwise the browser forgets a session
            # the store still holds. Gated on `loaded` because a session nothing
            # touched was not written either, and re-dating the cookie for one
            # would have the browser keep a key past the store's expiry.
            self.write_key(response, session.key)

        return response

    async def _realtime(self, connection: Any, call_next: Any) -> Any:
        """Attach a session to a long-lived connection and write it back after.

        There is no per-message response to hand a key back on, so nothing is
        set here: a connection either arrived with a key or it did not. What is
        in the bag when the handler finishes is saved, and a connection that was
        given a key it did not have keeps it only for as long as it is open.
        """
        session = await open_session(
            self.read_key(connection), store=self.store, timeout=self.timeout
        )
        connection.state.session = session
        try:
            return await call_next(connection)
        finally:
            # In a `finally` because a socket usually ends by raising — a
            # disconnect is the normal way out, and a bag written during the
            # connection would otherwise be dropped on the way.
            await session.save()

    async def __websocket__(self, websocket: Any, call_next: Any) -> Any:
        return await self._realtime(websocket, call_next)

    async def __webtransport__(self, transport: Any, call_next: Any) -> Any:
        return await self._realtime(transport, call_next)
