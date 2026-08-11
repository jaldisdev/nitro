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

"""
In-memory cache backend.
"""

import asyncio
import time
from typing import Any

from nitro.cache.base import BaseCache


class MemoryCache(BaseCache):
    """
    Simple in-memory cache backend using a dictionary.

    Note: This is not thread-safe and data is not shared between processes.
    Use only for development or single-process deployments.
    """

    def __init__(self, location: str, params: dict[str, Any]) -> None:
        super().__init__(location, params)
        self._cache: dict[str, tuple[Any, float | None]] = {}
        self._lock = asyncio.Lock()

    def _is_expired(self, expire_time: float | None) -> bool:
        """Check if an item has expired."""
        if expire_time is None:
            return False
        return time.time() > expire_time

    async def get(
        self,
        key: str,
        default: Any = None,
        version: int | None = None,
    ) -> Any:
        cache_key = self.make_key(key, version)

        async with self._lock:
            if cache_key not in self._cache:
                return default

            value, expire_time = self._cache[cache_key]

            if self._is_expired(expire_time):
                del self._cache[cache_key]
                return default

            return value

    async def set(
        self,
        key: str,
        value: Any,
        timeout: int | None = None,
        version: int | None = None,
    ) -> bool:
        cache_key = self.make_key(key, version)
        timeout = self.get_backend_timeout(timeout)

        expire_time = None if timeout is None else time.time() + timeout

        async with self._lock:
            self._cache[cache_key] = (value, expire_time)

        return True

    async def add(
        self,
        key: str,
        value: Any,
        timeout: int | None = None,
        version: int | None = None,
    ) -> bool:
        cache_key = self.make_key(key, version)

        async with self._lock:
            if cache_key in self._cache:
                _, expire_time = self._cache[cache_key]
                if not self._is_expired(expire_time):
                    return False

            timeout = self.get_backend_timeout(timeout)
            expire_time = None if timeout is None else time.time() + timeout
            self._cache[cache_key] = (value, expire_time)

        return True

    async def get_many(
        self,
        keys: list[str],
        version: int | None = None,
    ) -> dict[str, Any]:
        result = {}

        async with self._lock:
            for key in keys:
                cache_key = self.make_key(key, version)

                if cache_key in self._cache:
                    value, expire_time = self._cache[cache_key]

                    if not self._is_expired(expire_time):
                        result[key] = value
                    else:
                        del self._cache[cache_key]

        return result

    async def set_many(
        self,
        data: dict[str, Any],
        timeout: int | None = None,
        version: int | None = None,
    ) -> list[str]:
        timeout = self.get_backend_timeout(timeout)
        expire_time = None if timeout is None else time.time() + timeout

        async with self._lock:
            for key, value in data.items():
                cache_key = self.make_key(key, version)
                self._cache[cache_key] = (value, expire_time)

        return []

    async def delete(
        self,
        key: str,
        version: int | None = None,
    ) -> bool:
        cache_key = self.make_key(key, version)

        async with self._lock:
            if cache_key in self._cache:
                del self._cache[cache_key]
                return True

        return False

    async def delete_many(
        self,
        keys: list[str],
        version: int | None = None,
    ) -> int:
        count = 0

        async with self._lock:
            for key in keys:
                cache_key = self.make_key(key, version)
                if cache_key in self._cache:
                    del self._cache[cache_key]
                    count += 1

        return count

    async def clear(self) -> bool:
        async with self._lock:
            self._cache.clear()
        return True

    async def touch(
        self,
        key: str,
        timeout: int | None = None,
        version: int | None = None,
    ) -> bool:
        cache_key = self.make_key(key, version)

        async with self._lock:
            if cache_key not in self._cache:
                return False

            value, expire_time = self._cache[cache_key]

            if self._is_expired(expire_time):
                del self._cache[cache_key]
                return False

            timeout = self.get_backend_timeout(timeout)
            new_expire_time = None if timeout is None else time.time() + timeout
            self._cache[cache_key] = (value, new_expire_time)

        return True

    async def incr(
        self,
        key: str,
        delta: int = 1,
        version: int | None = None,
    ) -> int:
        cache_key = self.make_key(key, version)

        async with self._lock:
            if cache_key not in self._cache:
                raise ValueError(f"Key {key!r} not found")

            value, expire_time = self._cache[cache_key]

            if self._is_expired(expire_time):
                del self._cache[cache_key]
                raise ValueError(f"Key {key!r} not found")

            if not isinstance(value, int):
                raise ValueError(f"Key {key!r} value is not an integer")

            new_value = value + delta
            self._cache[cache_key] = (new_value, expire_time)

        return new_value

    async def decr(
        self,
        key: str,
        delta: int = 1,
        version: int | None = None,
    ) -> int:
        return await self.incr(key, -delta, version)

    async def has_key(
        self,
        key: str,
        version: int | None = None,
    ) -> bool:
        cache_key = self.make_key(key, version)

        async with self._lock:
            if cache_key not in self._cache:
                return False

            _, expire_time = self._cache[cache_key]

            if self._is_expired(expire_time):
                del self._cache[cache_key]
                return False

        return True

    async def close(self) -> None:
        """No-op for memory cache."""
