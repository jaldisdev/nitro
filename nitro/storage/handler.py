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

import importlib
from typing import Any

from nitro.storage.base import BaseStorage

DEFAULT_STORAGE_ALIAS = "default"


class StorageHandler:
    """
    Handler for managing multiple storage instances.

    Usage:
        from nitro.storage import storages
        storage = storages['default']
        await storage.save('file.txt', b'content')
    """

    def __init__(self, storages_config: dict[str, dict[str, Any]]) -> None:
        """
        Initialize the storage handler.

        Args:
            storages_config: Dictionary of storage configurations from settings.STORAGES
        """
        self._config = storages_config
        self._storages: dict[str, BaseStorage] = {}

    def __getitem__(self, alias: str) -> BaseStorage:
        """
        Get a storage instance by alias.

        Storage instances are lazily created and cached.
        """
        if alias not in self._storages:
            if alias not in self._config:
                raise KeyError(
                    f"Storage alias {alias!r} not found in settings.STORAGES. "
                    f"Available aliases: {list(self._config.keys())}"
                )

            self._storages[alias] = self._create_storage(alias)

        return self._storages[alias]

    def _create_storage(self, alias: str) -> BaseStorage:
        """Create a storage backend instance from configuration."""
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

    def all(self) -> dict[str, BaseStorage]:
        """
        Get all configured storage instances.

        This will create any storages that haven't been instantiated yet.
        """
        return {alias: self[alias] for alias in self._config}

    async def close_all(self) -> None:
        """Close all storage connections."""
        for storage in self._storages.values():
            await storage.close()
        self._storages.clear()
