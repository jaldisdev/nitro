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

"""Caching.

Caches are declared in the ``CACHES`` setting and reached by alias:

    from nitro.cache import caches

    await caches["default"].set("key", "value", timeout=60)

Each backend is built the first time its alias is used, so a project that
configures a cache it never touches never connects to it.
"""

from nitro.cache.base import DEFAULT_TIMEOUT, BaseCache
from nitro.cache.handler import DEFAULT_CACHE_ALIAS, CacheHandler

__all__ = [
    "DEFAULT_CACHE_ALIAS",
    "DEFAULT_TIMEOUT",
    "BaseCache",
    "CacheHandler",
    "cache",
    "caches",
    "reset_caches",
]


class _Caches:
    """The project's caches, built from settings on first use.

    Resolution is deferred because settings are not necessarily configured when
    this module is imported.
    """

    __slots__ = ("_handler",)

    def __init__(self) -> None:
        self._handler: CacheHandler | None = None

    def _resolve(self) -> CacheHandler:
        if self._handler is None:
            from nitro.settings import settings

            self._handler = CacheHandler(settings.CACHES)
        return self._handler

    def __getitem__(self, alias: str) -> BaseCache:
        return self._resolve()[alias]

    def __contains__(self, alias: str) -> bool:
        return alias in self._resolve()

    def __iter__(self):
        return iter(self._resolve())

    def all(self) -> dict[str, BaseCache]:
        return self._resolve().all()

    async def close_all(self) -> None:
        if self._handler is not None:
            await self._handler.close_all()

    def reset(self) -> None:
        """Forget every backend, so the next use rebuilds it.

        A worker is forked from a process that may already have opened
        connections; sharing one across a fork means two processes reading the
        same socket.
        """
        self._handler = None

    def __repr__(self) -> str:
        state = "built" if self._handler is not None else "not built"
        return f"<caches [{state}]>"


class _DefaultCache:
    """The ``default`` cache, so the common case reads as one object."""

    def __getattr__(self, name: str):
        return getattr(caches[DEFAULT_CACHE_ALIAS], name)

    def __repr__(self) -> str:
        return f"<cache {DEFAULT_CACHE_ALIAS!r}>"


caches = _Caches()

#: Shorthand for ``caches["default"]``.
cache = _DefaultCache()


def reset_caches() -> None:
    """Forget every backend. Called after a worker forks."""
    caches.reset()
