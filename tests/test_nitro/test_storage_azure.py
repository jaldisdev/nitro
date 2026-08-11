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

"""The Azure Blob storage backend.

Driven against a stand-in for the Azure SDK rather than a real account: what is
worth pinning here is the shape of the calls the backend makes and how it
translates the SDK's answers, both of which a double captures. The S3 backend
is tested the same way for the same reason.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


class FakeBlobClient:
    def __init__(self, container, name):
        self.container = container
        self.name = name

    async def upload_blob(self, data, overwrite=False):
        self.container.blobs[self.name] = data if isinstance(data, bytes) else data.read()

    async def download_blob(self):
        if self.name not in self.container.blobs:
            raise RuntimeError("BlobNotFound")
        content = self.container.blobs[self.name]
        return SimpleNamespace(readall=AsyncMock(return_value=content))

    async def delete_blob(self):
        if self.name not in self.container.blobs:
            raise RuntimeError("BlobNotFound")
        del self.container.blobs[self.name]

    async def get_blob_properties(self):
        if self.name not in self.container.blobs:
            raise RuntimeError("BlobNotFound")
        return SimpleNamespace(
            size=len(self.container.blobs[self.name]),
            creation_time=datetime(2026, 1, 1, tzinfo=UTC),
            last_modified=datetime(2026, 1, 2, tzinfo=UTC),
            # Only populated when the account has access tracking switched on;
            # the backend falls back to the modification time without it.
            last_accessed_on=None,
        )

    async def exists(self):
        return self.name in self.container.blobs

    async def start_copy_from_url(self, url):
        return {}

    @property
    def url(self):
        return f"https://account.blob.core.windows.net/{self.container.name}/{self.name}"

    @property
    def blob_name(self):
        return self.name


class FakeContainerClient:
    def __init__(self, name):
        self.name = name
        self.blobs: dict[str, bytes] = {}

    def get_blob_client(self, name):
        return FakeBlobClient(self, name)

    def walk_blobs(self, name_starts_with="", delimiter="/"):
        """Blobs directly under the prefix, and the prefixes below it.

        The real client yields a BlobPrefix for a virtual directory and a
        BlobProperties for a blob, and the backend tells them apart by whether
        the item has a `name`.
        """
        container = self

        async def generate():
            seen_prefixes = set()
            for blob in sorted(container.blobs):
                if not blob.startswith(name_starts_with):
                    continue
                rest = blob[len(name_starts_with) :]
                head, separator, _ = rest.partition(delimiter)
                if separator:
                    if head not in seen_prefixes:
                        seen_prefixes.add(head)
                        yield SimpleNamespace(prefix=f"{name_starts_with}{head}/")
                else:
                    yield SimpleNamespace(name=blob)

        class Listing:
            def __aiter__(self):
                return generate()

        return Listing()


@pytest.fixture
def azure(monkeypatch):
    """A backend wired to the fake SDK above."""
    container = FakeContainerClient("uploads")
    service = MagicMock()
    service.get_container_client.return_value = container

    module = MagicMock()
    module.BlobServiceClient.from_connection_string.return_value = service
    module.BlobServiceClient.return_value = service
    monkeypatch.setitem(sys.modules, "azure.storage.blob.aio", module)

    from nitro.storage.backends import azure as azure_module

    monkeypatch.setattr(azure_module, "BlobServiceClient", module.BlobServiceClient)
    monkeypatch.setattr(azure_module, "ContainerClient", MagicMock())

    backend = azure_module.AzureBlobStorage(
        "uploads", {"OPTIONS": {"connection_string": "UseDevelopmentStorage=true"}}
    )
    backend.container_client = container
    return backend


class TestConstruction:
    def test_it_needs_a_way_to_authenticate(self, monkeypatch):
        from nitro.storage.backends import azure as azure_module

        monkeypatch.setattr(azure_module, "BlobServiceClient", MagicMock())
        with pytest.raises(ValueError, match="connection_string"):
            azure_module.AzureBlobStorage("uploads", {"OPTIONS": {}})

    def test_the_package_is_required(self, monkeypatch):
        from nitro.storage.backends import azure as azure_module

        monkeypatch.setattr(azure_module, "BlobServiceClient", None)
        with pytest.raises(ImportError, match="azure-storage-blob"):
            azure_module.AzureBlobStorage("uploads", {"OPTIONS": {}})


class TestFiles:
    async def test_saving_and_reading(self, azure):
        assert await azure.save("photo.jpg", b"bytes") == "photo.jpg"
        assert await azure.read("photo.jpg") == b"bytes"

    async def test_reading_something_absent(self, azure):
        with pytest.raises(FileNotFoundError):
            await azure.read("absent.jpg")

    async def test_existence(self, azure):
        await azure.save("photo.jpg", b"bytes")
        assert await azure.exists("photo.jpg") is True
        assert await azure.exists("absent.jpg") is False

    async def test_deleting_reports_what_it_found(self, azure):
        await azure.save("photo.jpg", b"bytes")
        assert await azure.delete("photo.jpg") is True
        assert await azure.delete("photo.jpg") is False

    async def test_size(self, azure):
        await azure.save("photo.jpg", b"12345")
        assert await azure.size("photo.jpg") == 5

    async def test_size_of_something_absent(self, azure):
        with pytest.raises(FileNotFoundError):
            await azure.size("absent.jpg")

    async def test_opening_reads_through_a_storage_file(self, azure):
        await azure.save("photo.jpg", b"bytes")
        async with azure.open("photo.jpg") as handle:
            assert await handle.read() == b"bytes"

    async def test_listing_separates_directories_from_files(self, azure):
        await azure.save("a.txt", b"a")
        await azure.save("nested/b.txt", b"b")

        directories, files = await azure.listdir("")
        assert "a.txt" in files
        assert "nested" in directories


class TestTimestamps:
    async def test_created_and_modified(self, azure):
        await azure.save("photo.jpg", b"bytes")
        assert (await azure.get_created_time("photo.jpg")).year == 2026
        assert (await azure.get_modified_time("photo.jpg")).day == 2

    async def test_access_time_falls_back_to_modification_time(self, azure):
        # Blob storage reports a last access time only when the account has
        # access tracking on; the backend answers with the modification time
        # rather than refusing, which is why it overrides the base class.
        await azure.save("photo.jpg", b"bytes")
        assert (await azure.get_accessed_time("photo.jpg")).day == 2


class TestUrls:
    async def test_a_base_url_is_used_when_configured(self, monkeypatch, azure):
        azure.base_url = "https://cdn.example.com/"
        assert await azure.url("photo.jpg") == "https://cdn.example.com/photo.jpg"
