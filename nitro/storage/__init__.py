"""File storage.

Backends are declared in the ``STORAGES`` setting and reached by alias:

    from nitro.storage import storages

    await storages["default"].save("uploads/photo.jpg", data)

Each backend is built the first time its alias is used, so a project that
configures storage it never touches never connects to it.
"""

from nitro.storage.base import BaseStorage, StorageFile, StorageOperationUnsupported
from nitro.storage.handler import DEFAULT_STORAGE_ALIAS, StorageHandler

__all__ = [
    "DEFAULT_STORAGE_ALIAS",
    "BaseStorage",
    "StorageFile",
    "StorageHandler",
    "StorageOperationUnsupported",
    "reset_storages",
    "storage",
    "storages",
]


class _Storages:
    """The project's storage backends, built from settings on first use."""

    __slots__ = ("_handler",)

    def __init__(self) -> None:
        self._handler: StorageHandler | None = None

    def _resolve(self) -> StorageHandler:
        if self._handler is None:
            from nitro.settings import settings

            self._handler = StorageHandler(settings.STORAGES)
        return self._handler

    def __getitem__(self, alias: str) -> BaseStorage:
        return self._resolve()[alias]

    def __contains__(self, alias: str) -> bool:
        return alias in self._resolve()

    def __iter__(self):
        return iter(self._resolve())

    def all(self) -> dict[str, BaseStorage]:
        return self._resolve().all()

    def reset(self) -> None:
        """Forget every backend, so the next use rebuilds it."""
        self._handler = None

    def __repr__(self) -> str:
        state = "built" if self._handler is not None else "not built"
        return f"<storages [{state}]>"


class _DefaultStorage:
    """The ``default`` backend, so the common case reads as one object."""

    def __getattr__(self, name: str):
        return getattr(storages[DEFAULT_STORAGE_ALIAS], name)

    def __repr__(self) -> str:
        return f"<storage {DEFAULT_STORAGE_ALIAS!r}>"


storages = _Storages()

#: Shorthand for ``storages["default"]``.
storage = _DefaultStorage()


def reset_storages() -> None:
    """Forget every backend. Called after a worker forks."""
    storages.reset()
