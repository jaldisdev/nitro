"""
Redis cache backend.
"""

from typing import Any

try:
    import redis.asyncio as redis
except ImportError:
    redis = None  # type: ignore

from nitro.cache.base import BaseCache

#: Options that configure this backend rather than the connection under it.
_NOT_CONNECTION_OPTIONS = frozenset({"CLIENT_CLASS", "SERIALIZER"})


class RedisCache(BaseCache):
    """
    Redis cache backend using redis-py async client.

    Requires: pip install redis

    Example configuration:
        CACHES = {
            'default': {
                'BACKEND': 'nitro.cache.backends.redis.RedisCache',
                'LOCATION': 'redis://localhost:6379/0',
                'OPTIONS': {
                    'SERIALIZER': 'json',
                    'CLIENT_CLASS': 'redis.asyncio.Redis',
                },
            }
        }

    Values are encoded with the SERIALIZER option, which is JSON unless it
    says otherwise. See `nitro.cache.base` for what that means and for why
    'pickle' is a decision rather than a default.
    """

    def __init__(self, location: str, params: dict[str, Any]) -> None:
        if redis is None:
            raise ImportError(
                "RedisCache requires redis package. Install it with: pip install redis"
            )

        super().__init__(location, params)

        # Parse connection options
        client_class = self.options.get("CLIENT_CLASS", redis.Redis)
        if isinstance(client_class, str):
            # Import the class if given as string
            module_path, class_name = client_class.rsplit(".", 1)
            import importlib

            module = importlib.import_module(module_path)
            client_class = getattr(module, class_name)

        self._client: redis.Redis = client_class.from_url(
            location,
            decode_responses=False,  # We handle serialization
            **{
                name: value
                for name, value in self.options.items()
                if name not in _NOT_CONNECTION_OPTIONS
            },
        )

    def _serialize(self, value: Any) -> bytes:
        """Encode a value for the store, as SERIALIZER says to."""
        return self.serializer.dumps(value)

    def _deserialize(self, value: bytes | None) -> Any:
        """Decode a value from the store."""
        if value is None:
            return None
        return self.serializer.loads(value)

    async def get(
        self,
        key: str,
        default: Any = None,
        version: int | None = None,
    ) -> Any:
        cache_key = self.make_key(key, version)
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
        cache_key = self.make_key(key, version)
        timeout = self.get_backend_timeout(timeout)

        serialized = self._serialize(value)

        if timeout is None:
            await self._client.set(cache_key, serialized)
        else:
            await self._client.set(cache_key, serialized, ex=timeout)

        return True

    async def add(
        self,
        key: str,
        value: Any,
        timeout: int | None = None,
        version: int | None = None,
    ) -> bool:
        cache_key = self.make_key(key, version)
        timeout = self.get_backend_timeout(timeout)

        serialized = self._serialize(value)

        # `set(nx=True)` answers None when the key was already there and True
        # when it was not, so the result is coerced rather than returned: this
        # method is declared to say whether it added, in a bool.
        if timeout is None:
            stored = await self._client.set(cache_key, serialized, nx=True)
        else:
            stored = await self._client.set(cache_key, serialized, ex=timeout, nx=True)

        return bool(stored)

    async def get_many(
        self,
        keys: list[str],
        version: int | None = None,
    ) -> dict[str, Any]:
        if not keys:
            return {}

        cache_keys = [self.make_key(key, version) for key in keys]
        values = await self._client.mget(cache_keys)

        result = {}
        for key, value in zip(keys, values):
            if value is not None:
                result[key] = self._deserialize(value)

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

        pipeline = self._client.pipeline()

        for key, value in data.items():
            cache_key = self.make_key(key, version)
            serialized = self._serialize(value)

            if timeout is None:
                pipeline.set(cache_key, serialized)
            else:
                pipeline.set(cache_key, serialized, ex=timeout)

        await pipeline.execute()
        return []

    async def delete(
        self,
        key: str,
        version: int | None = None,
    ) -> bool:
        cache_key = self.make_key(key, version)
        result = await self._client.delete(cache_key)
        return result > 0

    async def delete_many(
        self,
        keys: list[str],
        version: int | None = None,
    ) -> int:
        if not keys:
            return 0

        cache_keys = [self.make_key(key, version) for key in keys]
        return await self._client.delete(*cache_keys)

    async def clear(self) -> bool:
        await self._client.flushdb()
        return True

    async def touch(
        self,
        key: str,
        timeout: int | None = None,
        version: int | None = None,
    ) -> bool:
        cache_key = self.make_key(key, version)
        timeout = self.get_backend_timeout(timeout)

        if timeout is None:
            return await self._client.persist(cache_key)
        else:
            return await self._client.expire(cache_key, timeout)

    async def incr(
        self,
        key: str,
        delta: int = 1,
        version: int | None = None,
    ) -> int:
        cache_key = self.make_key(key, version)

        try:
            return await self._client.incr(cache_key, delta)
        except redis.ResponseError as e:
            raise ValueError(f"Key {key!r} value is not an integer") from e

    async def decr(
        self,
        key: str,
        delta: int = 1,
        version: int | None = None,
    ) -> int:
        cache_key = self.make_key(key, version)

        try:
            return await self._client.decr(cache_key, delta)
        except redis.ResponseError as e:
            raise ValueError(f"Key {key!r} value is not an integer") from e

    async def has_key(
        self,
        key: str,
        version: int | None = None,
    ) -> bool:
        cache_key = self.make_key(key, version)
        return await self._client.exists(cache_key) > 0

    async def close(self) -> None:
        await self._client.aclose()
