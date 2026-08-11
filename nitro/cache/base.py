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

import json
import pickle
from abc import ABC, abstractmethod
from typing import Any

__all__ = [
    "DEFAULT_TIMEOUT",
    "BaseCache",
    "CacheSerializer",
    "JsonSerializer",
    "PickleSerializer",
    "serializer_for",
]

DEFAULT_TIMEOUT = 300  # 5 minutes


class CacheSerializer(ABC):
    """How a value is turned into bytes for a store outside this process."""

    @abstractmethod
    def dumps(self, value: Any) -> bytes:
        """The bytes to store for `value`."""

    @abstractmethod
    def loads(self, data: bytes) -> Any:
        """The value `data` was made from."""


class JsonSerializer(CacheSerializer):
    """JSON. The default, because reading it cannot execute anything.

    It carries what JSON carries: `None`, booleans, numbers, strings, lists and
    dictionaries with string keys. A tuple comes back as a list, and `bytes`,
    `set`, `datetime` or an arbitrary object is refused rather than silently
    turned into something else — cache a representation you chose instead.
    """

    def dumps(self, value: Any) -> bytes:
        try:
            return json.dumps(value).encode("utf-8")
        except TypeError as error:
            raise TypeError(
                f"{type(value).__name__} cannot be cached as JSON: {error}. "
                "Cache a JSON-compatible representation, or configure "
                "OPTIONS={'SERIALIZER': 'pickle'} for this cache."
            ) from error

    def loads(self, data: bytes) -> Any:
        return json.loads(data)


class PickleSerializer(CacheSerializer):
    """Pickle. Carries almost any Python object, and trusts what it reads.

    SECURITY WARNING: unpickling runs code contained in the data. Anyone who
    can write to the cache store — a shared Redis, a Memcached on a network
    somebody else can reach, an operator who can set a key — can therefore run
    code in every process that reads it. Choose this only for a store nothing
    else can write to.
    """

    def dumps(self, value: Any) -> bytes:
        return pickle.dumps(value)

    def loads(self, data: bytes) -> Any:
        return pickle.loads(data)


#: The serializers a cache may be configured with, by ``SERIALIZER`` option.
SERIALIZERS: dict[str, type[CacheSerializer]] = {
    "json": JsonSerializer,
    "pickle": PickleSerializer,
}

DEFAULT_SERIALIZER = "json"


def serializer_for(name: str) -> CacheSerializer:
    """The serializer named by a cache's ``SERIALIZER`` option."""
    try:
        return SERIALIZERS[name]()
    except KeyError:
        known = ", ".join(sorted(SERIALIZERS))
        raise ValueError(f"{name!r} is not a cache serializer; expected one of {known}") from None


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
                - OPTIONS: Backend-specific options, including SERIALIZER
                - KEY_PREFIX: Prefix for all cache keys
                - VERSION: Default version number for keys
        """
        self.location = location
        self.default_timeout = params.get("TIMEOUT", DEFAULT_TIMEOUT)
        self.key_prefix = params.get("KEY_PREFIX", "")
        self.version = params.get("VERSION", 1)
        self.options = params.get("OPTIONS", {})
        #: How values are encoded for a store outside this process. Backends
        #: that keep Python objects in memory have no use for it.
        self.serializer = serializer_for(self.options.get("SERIALIZER", DEFAULT_SERIALIZER))

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

    @abstractmethod
    async def clear(self) -> bool:
        """
        Remove all keys from the cache.

        Returns True if successful.
        """

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

    @abstractmethod
    async def has_key(
        self,
        key: str,
        version: int | None = None,
    ) -> bool:
        """
        Check if a key exists in the cache.
        """

    @abstractmethod
    async def close(self) -> None:
        """
        Close any connections to the cache backend.
        """
