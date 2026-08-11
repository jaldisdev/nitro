"""Memcached, through emcache.

emcache reports outcomes by raising rather than by returning: a `delete` of a
key that is not there raises, an `add` over a key that is there raises, and a
`get` hands back an `Item` rather than the bytes inside it. `BaseCache` is
declared in booleans and values, so the translation happens here.
"""

from __future__ import annotations

from typing import Any

try:
    import emcache
    from emcache import (
        MemcachedHostAddress,
        NotFoundCommandError,
        NotStoredStorageCommandError,
    )
except ImportError:  # pragma: no cover - exercised by the import guard below
    emcache = None  # type: ignore[assignment]

from nitro.cache.base import BaseCache

__all__ = ["MemcachedCache"]

DEFAULT_PORT = 11211

#: Options that configure this backend rather than the connection under it.
_NOT_CONNECTION_OPTIONS = frozenset({"SERIALIZER"})


def parse_servers(location: str | list[str] | tuple[str, ...]) -> list[Any]:
    """The addresses in `location`, as emcache wants them.

    One server or several, each ``host`` or ``host:port``. A missing port is
    the standard one rather than an error, since that is what anybody writing
    just a host name meant.
    """
    if isinstance(location, str):
        entries = [entry.strip() for entry in location.split(",")]
    else:
        entries = [str(entry).strip() for entry in location]

    addresses = []
    for entry in entries:
        if not entry:
            continue
        host, separator, port = entry.rpartition(":")
        if not separator:
            host, port = entry, str(DEFAULT_PORT)
        try:
            addresses.append(MemcachedHostAddress(host, int(port)))
        except ValueError as error:
            raise ValueError(
                f"{entry!r} is not a memcached address; expected 'host' or 'host:port'"
            ) from error

    if not addresses:
        raise ValueError("a memcached cache needs a LOCATION, for example 'localhost:11211'")
    return addresses


class MemcachedCache(BaseCache):
    """Cache in Memcached.

    Requires: pip install emcache

        CACHES = {
            "default": {
                "BACKEND": "nitro.cache.backends.memcached.MemcachedCache",
                "LOCATION": "127.0.0.1:11211",
                "OPTIONS": {"SERIALIZER": "json", "max_connections": 8},
            }
        }

    Values are encoded with the SERIALIZER option, which is JSON unless it says
    otherwise — see `nitro.cache.base` for why that is the default. `OPTIONS`
    other than SERIALIZER are passed to `emcache.create_client`.

    The client is built on first use rather than in the constructor, because
    building it is asynchronous and a cache is configured from settings long
    before there is a loop to build it on.
    """

    def __init__(self, location: str, params: dict[str, Any]) -> None:
        if emcache is None:
            raise ImportError(
                "MemcachedCache requires the emcache package. Install it with: pip install emcache"
            )

        super().__init__(location, params)
        self.servers = parse_servers(location)
        self._client: Any = None

    async def _connect(self) -> Any:
        if self._client is None:
            self._client = await emcache.create_client(
                self.servers,
                **{
                    name: value
                    for name, value in self.options.items()
                    if name not in _NOT_CONNECTION_OPTIONS
                },
            )
        return self._client

    def _key(self, key: str, version: int | None) -> bytes:
        return self.make_key(key, version).encode("utf-8")

    @staticmethod
    def _expiry(timeout: int | None) -> int:
        """Memcached's expiry, where zero means "never"."""
        return 0 if timeout is None else max(0, int(timeout))

    async def get(self, key: str, default: Any = None, version: int | None = None) -> Any:
        client = await self._connect()
        item = await client.get(self._key(key, version))
        # A miss is None; a hit is an Item, and the bytes are inside it.
        return default if item is None else self.serializer.loads(item.value)

    async def set(
        self,
        key: str,
        value: Any,
        timeout: int | None = None,
        version: int | None = None,
    ) -> bool:
        client = await self._connect()
        await client.set(
            self._key(key, version),
            self.serializer.dumps(value),
            exptime=self._expiry(self.get_backend_timeout(timeout)),
        )
        return True

    async def add(
        self,
        key: str,
        value: Any,
        timeout: int | None = None,
        version: int | None = None,
    ) -> bool:
        client = await self._connect()
        try:
            await client.add(
                self._key(key, version),
                self.serializer.dumps(value),
                exptime=self._expiry(self.get_backend_timeout(timeout)),
            )
        except NotStoredStorageCommandError:
            # The key is already there, which is what `add` asks about.
            return False
        return True

    async def get_many(self, keys: list[str], version: int | None = None) -> dict[str, Any]:
        if not keys:
            return {}

        client = await self._connect()
        wanted = {self._key(key, version): key for key in keys}
        found = await client.get_many(list(wanted))

        return {
            wanted[cache_key]: self.serializer.loads(item.value)
            for cache_key, item in found.items()
            if item is not None
        }

    async def set_many(
        self,
        data: dict[str, Any],
        timeout: int | None = None,
        version: int | None = None,
    ) -> list[str]:
        """Store several values, returning the keys that could not be stored.

        One call each: the protocol has no multi-set, and emcache offers none.
        """
        failed: list[str] = []
        for key, value in data.items():
            if not await self.set(key, value, timeout=timeout, version=version):
                failed.append(key)
        return failed

    async def delete(self, key: str, version: int | None = None) -> bool:
        client = await self._connect()
        try:
            await client.delete(self._key(key, version))
        except NotFoundCommandError:
            return False
        return True

    async def delete_many(self, keys: list[str], version: int | None = None) -> int:
        deleted = 0
        for key in keys:
            if await self.delete(key, version=version):
                deleted += 1
        return deleted

    async def clear(self) -> bool:
        """Empty every node.

        `flush_all` is addressed to one node, so a cluster needs one call per
        node — flushing only the first would leave the rest serving stale keys.
        """
        client = await self._connect()
        for server in self.servers:
            await client.flush_all(server)
        return True

    async def touch(
        self,
        key: str,
        timeout: int | None = None,
        version: int | None = None,
    ) -> bool:
        client = await self._connect()
        try:
            await client.touch(
                self._key(key, version),
                self._expiry(self.get_backend_timeout(timeout)),
            )
        except NotFoundCommandError:
            return False
        return True

    async def incr(self, key: str, delta: int = 1, version: int | None = None) -> int:
        client = await self._connect()
        try:
            result = await client.increment(self._key(key, version), delta)
        except NotFoundCommandError as error:
            raise ValueError(f"cannot increment {key!r}: it is not in the cache") from error
        if result is None:
            raise ValueError(f"cannot increment {key!r}: it is not in the cache")
        return result

    async def decr(self, key: str, delta: int = 1, version: int | None = None) -> int:
        client = await self._connect()
        try:
            result = await client.decrement(self._key(key, version), delta)
        except NotFoundCommandError as error:
            raise ValueError(f"cannot decrement {key!r}: it is not in the cache") from error
        if result is None:
            raise ValueError(f"cannot decrement {key!r}: it is not in the cache")
        return result

    async def has_key(self, key: str, version: int | None = None) -> bool:
        client = await self._connect()
        return await client.get(self._key(key, version)) is not None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
