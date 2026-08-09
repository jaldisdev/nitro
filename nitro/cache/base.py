from abc import ABC, abstractmethod
from typing import Any

DEFAULT_TIMEOUT = 300  # 5 minutes


class BaseCache(ABC):
    """
    Abstract base class for all cache backends.
    """

    def __init__(
        self,
        location: str,
        params: dict[str, Any],
    ) -> None:
        """
        Initialize the cache backend.

        Args:
            location: Backend-specific location (host, path, etc.)
            params: Additional parameters including:
                - TIMEOUT: Default timeout in seconds
                - OPTIONS: Backend-specific options
                - KEY_PREFIX: Prefix for all cache keys
                - VERSION: Default version number for keys
        """
        self.location = location
        self.default_timeout = params.get("TIMEOUT", DEFAULT_TIMEOUT)
        self.key_prefix = params.get("KEY_PREFIX", "")
        self.version = params.get("VERSION", 1)
        self.options = params.get("OPTIONS", {})

    def make_key(self, key: str, version: int | None = None) -> str:
        """
        Construct the cache key from the provided key and version.
        """
        if version is None:
            version = self.version

        return f"{self.key_prefix}:{version}:{key}"

    def get_backend_timeout(self, timeout: int | None = None) -> int | None:
        """
        Return the timeout value usable by the backend.
        """
        if timeout is None:
            timeout = self.default_timeout
        elif timeout == 0:
            # 0 means never expire
            return None
        return timeout

    @abstractmethod
    async def get(
        self,
        key: str,
        default: Any = None,
        version: int | None = None,
    ) -> Any:
        """
        Fetch a value from the cache.

        Returns default if the key is not found.
        """
        pass

    @abstractmethod
    async def set(
        self,
        key: str,
        value: Any,
        timeout: int | None = None,
        version: int | None = None,
    ) -> bool:
        """
        Set a value in the cache.

        Returns True if successful.
        """
        pass

    @abstractmethod
    async def add(
        self,
        key: str,
        value: Any,
        timeout: int | None = None,
        version: int | None = None,
    ) -> bool:
        """
        Set a value in the cache if the key does not already exist.

        Returns True if the key was added.
        """
        pass

    async def get_or_set(
        self,
        key: str,
        default: Any,
        timeout: int | None = None,
        version: int | None = None,
    ) -> Any:
        """
        Fetch a value from the cache, or set it if it doesn't exist.

        The default can be a callable that returns the value to set.
        """
        val = await self.get(key, version=version)
        if val is None:
            if callable(default):
                default = default()
            await self.set(key, default, timeout=timeout, version=version)
            return default
        return val

    @abstractmethod
    async def get_many(
        self,
        keys: list[str],
        version: int | None = None,
    ) -> dict[str, Any]:
        """
        Fetch multiple values from the cache at once.

        Returns a dict of key/value pairs.
        """
        pass

    @abstractmethod
    async def set_many(
        self,
        data: dict[str, Any],
        timeout: int | None = None,
        version: int | None = None,
    ) -> list[str]:
        """
        Set multiple values in the cache at once.

        Returns a list of keys that failed to be inserted.
        """
        pass

    @abstractmethod
    async def delete(
        self,
        key: str,
        version: int | None = None,
    ) -> bool:
        """
        Delete a key from the cache.

        Returns True if the key existed and was deleted.
        """
        pass

    @abstractmethod
    async def delete_many(
        self,
        keys: list[str],
        version: int | None = None,
    ) -> int:
        """
        Delete multiple keys from the cache.

        Returns the number of keys deleted.
        """
        pass

    @abstractmethod
    async def clear(self) -> bool:
        """
        Remove all keys from the cache.

        Returns True if successful.
        """
        pass

    @abstractmethod
    async def touch(
        self,
        key: str,
        timeout: int | None = None,
        version: int | None = None,
    ) -> bool:
        """
        Update the key's expiry time.

        Returns True if the key was touched.
        """
        pass

    @abstractmethod
    async def incr(
        self,
        key: str,
        delta: int = 1,
        version: int | None = None,
    ) -> int:
        """
        Increment a key's value.

        Returns the new value.
        """
        pass

    @abstractmethod
    async def decr(
        self,
        key: str,
        delta: int = 1,
        version: int | None = None,
    ) -> int:
        """
        Decrement a key's value.

        Returns the new value.
        """
        pass

    @abstractmethod
    async def has_key(
        self,
        key: str,
        version: int | None = None,
    ) -> bool:
        """
        Check if a key exists in the cache.
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """
        Close any connections to the cache backend.
        """
        pass
