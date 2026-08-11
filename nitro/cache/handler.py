import importlib
from typing import Any

from nitro.cache.base import BaseCache

DEFAULT_CACHE_ALIAS = "default"


class CacheHandler:
    """
    Handler for managing multiple cache instances.

    Usage:
        from nitro.cache import caches
        cache = caches['default']
        await cache.set('key', 'value')
    """

    def __init__(self, caches_config: dict[str, dict[str, Any]]) -> None:
        """
        Initialize the cache handler.

        Args:
            caches_config: Dictionary of cache configurations from settings.CACHES
        """
        self._config = caches_config
        self._caches: dict[str, BaseCache] = {}

    def __getitem__(self, alias: str) -> BaseCache:
        """
        Get a cache instance by alias.

        Cache instances are lazily created and cached.
        """
        if alias not in self._caches:
            if alias not in self._config:
                raise KeyError(
                    f"Cache alias {alias!r} not found in settings.CACHES. "
                    f"Available aliases: {list(self._config.keys())}"
                )

            self._caches[alias] = self._create_cache(alias)

        return self._caches[alias]

    def _create_cache(self, alias: str) -> BaseCache:
        """Create a cache backend instance from configuration."""
        config = self._config[alias].copy()
        backend = config.pop("BACKEND")
        location = config.pop("LOCATION", "")

        # Import the backend class
        if isinstance(backend, str):
            module_path, class_name = backend.rsplit(".", 1)
            module = importlib.import_module(module_path)
            backend_class = getattr(module, class_name)
        else:
            backend_class = backend

        # Instantiate the backend
        return backend_class(location, config)

    def __contains__(self, alias: str) -> bool:
        return alias in self._config

    def __iter__(self):
        return iter(self._config)

    def all(self) -> dict[str, BaseCache]:
        """
        Get all configured cache instances.

        This will create any caches that haven't been instantiated yet.
        """
        return {alias: self[alias] for alias in self._config}

    async def close_all(self) -> None:
        """Close all cache connections."""
        for cache in self._caches.values():
            await cache.close()
        self._caches.clear()
