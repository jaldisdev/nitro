"""
Memcached cache backend using emcache.
"""

import pickle
from typing import Any

try:
    import emcache
except ImportError:
    emcache = None  # type: ignore

from nitro.cache.base import BaseCache


class MemcachedCache(BaseCache):
    """
    Memcached cache backend using emcache async client.

    Requires: pip install emcache

    Example configuration:
        CACHES = {
            'default': {
                'BACKEND': 'nitro.cache.backends.memcached.MemcachedCache',
                'LOCATION': '127.0.0.1:11211',  # Or list: ['server1:11211', 'server2:11211']
                'OPTIONS': {
                    'max_connections': 100,
                    'min_connections': 10,
                },
            }
        }
    """

    def __init__(self, location: str, params: dict[str, Any]) -> None:
        if emcache is None:
            raise ImportError(
                "MemcachedCache requires emcache package. "
                "Install it with: pip install emcache"
            )

        super().__init__(location, params)

        # Parse location - can be a single server or list of servers
        if isinstance(location, str):
            servers = [tuple(location.split(":"))]  # type: ignore
        else:
            servers = [tuple(s.split(":")) for s in location]  # type: ignore

        # Convert port strings to integers
        servers = [(host, int(port)) for host, port in servers]

        self._client = emcache.Client(servers, **self.options)

    def _serialize(self, value: Any) -> bytes:
        """Serialize a value for storage."""
        return pickle.dumps(value)

    def _deserialize(self, value: bytes | None) -> Any:
        """Deserialize a value from storage."""
        if value is None:
            return None
        return pickle.loads(value)

    async def get(
        self,
        key: str,
        default: Any = None,
        version: int | None = None,
    ) -> Any:
        cache_key = self.make_key(key, version).encode("utf-8")
        value = await self._client.get(cache_key)

        if value is None:
            return default

        return self._deserialize(value)

    async def set(
        self,
        key: str,
        value: Any,
        timeout: int | None = None,
        version: int | None = None,
    ) -> bool:
        cache_key = self.make_key(key, version).encode("utf-8")
        timeout = self.get_backend_timeout(timeout)

        # Memcached uses 0 for no expiration
        exptime = 0 if timeout is None else timeout

        serialized = self._serialize(value)
        return await self._client.set(cache_key, serialized, exptime=exptime)

    async def add(
        self,
        key: str,
        value: Any,
        timeout: int | None = None,
        version: int | None = None,
    ) -> bool:
        cache_key = self.make_key(key, version).encode("utf-8")
        timeout = self.get_backend_timeout(timeout)

        exptime = 0 if timeout is None else timeout
        serialized = self._serialize(value)

        return await self._client.add(cache_key, serialized, exptime=exptime)

    async def get_many(
        self,
        keys: list[str],
        version: int | None = None,
    ) -> dict[str, Any]:
        if not keys:
            return {}

        cache_keys = [self.make_key(key, version).encode("utf-8") for key in keys]

        values = await self._client.get_many(cache_keys)

        result = {}
        for key, cache_key in zip(keys, cache_keys):
            if cache_key in values:
                result[key] = self._deserialize(values[cache_key])

        return result

    async def set_many(
        self,
        data: dict[str, Any],
        timeout: int | None = None,
        version: int | None = None,
    ) -> list[str]:
        if not data:
            return []

        timeout = self.get_backend_timeout(timeout)
        exptime = 0 if timeout is None else timeout

        items = {
            self.make_key(key, version).encode("utf-8"): self._serialize(value)
            for key, value in data.items()
        }

        await self._client.set_many(items, exptime=exptime)
        return []

    async def delete(
        self,
        key: str,
        version: int | None = None,
    ) -> bool:
        cache_key = self.make_key(key, version).encode("utf-8")
        return await self._client.delete(cache_key)

    async def delete_many(
        self,
        keys: list[str],
        version: int | None = None,
    ) -> int:
        if not keys:
            return 0

        cache_keys = [self.make_key(key, version).encode("utf-8") for key in keys]

        count = 0
        for cache_key in cache_keys:
            if await self._client.delete(cache_key):
                count += 1

        return count

    async def clear(self) -> bool:
        await self._client.flush_all()
        return True

    async def touch(
        self,
        key: str,
        timeout: int | None = None,
        version: int | None = None,
    ) -> bool:
        cache_key = self.make_key(key, version).encode("utf-8")
        timeout = self.get_backend_timeout(timeout)

        exptime = 0 if timeout is None else timeout

        return await self._client.touch(cache_key, exptime=exptime)

    async def incr(
        self,
        key: str,
        delta: int = 1,
        version: int | None = None,
    ) -> int:
        cache_key = self.make_key(key, version).encode("utf-8")

        try:
            result = await self._client.increment(cache_key, delta)
            if result is None:
                raise ValueError(f"Key {key!r} not found")
            return result
        except Exception as e:
            raise ValueError(f"Error incrementing key {key!r}") from e

    async def decr(
        self,
        key: str,
        delta: int = 1,
        version: int | None = None,
    ) -> int:
        cache_key = self.make_key(key, version).encode("utf-8")

        try:
            result = await self._client.decrement(cache_key, delta)
            if result is None:
                raise ValueError(f"Key {key!r} not found")
            return result
        except Exception as e:
            raise ValueError(f"Error decrementing key {key!r}") from e

    async def has_key(
        self,
        key: str,
        version: int | None = None,
    ) -> bool:
        cache_key = self.make_key(key, version).encode("utf-8")
        value = await self._client.get(cache_key)
        return value is not None

    async def close(self) -> None:
        await self._client.close()
