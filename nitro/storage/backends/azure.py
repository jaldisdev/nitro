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

from datetime import datetime

try:
    from azure.storage.blob.aio import BlobServiceClient, ContainerClient
except ImportError:
    BlobServiceClient = None  # type: ignore
    ContainerClient = None  # type: ignore

from nitro.storage.base import BaseStorage, StorageFile
from nitro.utils.content import Content, file_object_for, read_content


class AzureBlobFile(StorageFile):
    """File object for Azure Blob storage."""

    def __init__(self, blob_client):
        self.blob_client = blob_client
        self._content = None

    async def read(self, size: int = -1) -> bytes:
        """Read file content."""
        if self._content is None:
            # Read entire file on first read
            try:
                downloader = await self.blob_client.download_blob()
                self._content = await downloader.readall()
            except Exception as e:
                if "BlobNotFound" in str(e):
                    raise FileNotFoundError(f"File not found: {self.blob_client.blob_name}") from e
                raise

        if size == -1:
            return self._content
        else:
            return self._content[:size]

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """No-op for Azure file."""


class AzureBlobStorage(BaseStorage):
    """
    Azure Blob Storage backend using azure-storage-blob async client.

    Requires: pip install azure-storage-blob

    Example configuration:
        STORAGES = {
            'default': {
                'BACKEND': 'nitro.storage.backends.azure.AzureBlobStorage',
                'LOCATION': 'my-container-name',
                'OPTIONS': {
                    'account_name': 'myaccount',
                    'account_key': 'YOUR_ACCOUNT_KEY',
                    # Or use connection_string instead
                    'connection_string': 'DefaultEndpointsProtocol=https;...',
                    # Or use SAS token
                    'sas_token': 'YOUR_SAS_TOKEN',
                },
                'BASE_URL': 'https://myaccount.blob.core.windows.net/my-container',
            }
        }
    """

    def __init__(self, location: str, params: dict) -> None:
        if BlobServiceClient is None:
            raise ImportError(
                "AzureBlobStorage requires azure-storage-blob package. "
                "Install it with: pip install azure-storage-blob"
            )

        super().__init__(location, params)
        self.container_name = location

        # Get authentication method
        connection_string = self.options.get("connection_string")
        account_name = self.options.get("account_name")
        account_key = self.options.get("account_key")
        sas_token = self.options.get("sas_token")

        # Create blob service client
        if connection_string:
            self.blob_service_client = BlobServiceClient.from_connection_string(connection_string)
        elif account_name and account_key:
            account_url = f"https://{account_name}.blob.core.windows.net"
            self.blob_service_client = BlobServiceClient(
                account_url=account_url,
                credential=account_key,
            )
        elif account_name and sas_token:
            account_url = f"https://{account_name}.blob.core.windows.net"
            self.blob_service_client = BlobServiceClient(
                account_url=account_url,
                credential=sas_token,
            )
        else:
            raise ValueError(
                "Azure storage requires either connection_string, "
                "or account_name with account_key/sas_token"
            )

        self.container_client: ContainerClient = self.blob_service_client.get_container_client(
            self.container_name
        )

    async def save(self, name: str, content: Content) -> str:
        # As in the S3 backend: a file is handed over as a file so the client
        # reads it from disk itself, and anything else is collected first.
        data = file_object_for(content)
        if data is None:
            data = await read_content(content)

        blob_client = self.container_client.get_blob_client(name)
        await blob_client.upload_blob(data, overwrite=True)
        return name

    def open(self, name: str, mode: str = "rb") -> StorageFile:
        """
        Open a file and return a file object.
        """
        blob_client = self.container_client.get_blob_client(name)
        return AzureBlobFile(blob_client)

    async def read(self, name: str) -> bytes:
        blob_client = self.container_client.get_blob_client(name)

        try:
            downloader = await blob_client.download_blob()
            return await downloader.readall()
        except Exception as e:
            if "BlobNotFound" in str(e):
                raise FileNotFoundError(f"File not found: {name}") from e
            raise

    async def delete(self, name: str) -> bool:
        blob_client = self.container_client.get_blob_client(name)

        try:
            await blob_client.delete_blob()
            return True
        except Exception as e:
            if "BlobNotFound" in str(e):
                return False
            raise

    async def exists(self, name: str) -> bool:
        blob_client = self.container_client.get_blob_client(name)

        try:
            await blob_client.get_blob_properties()
            return True
        except Exception as e:
            if "BlobNotFound" in str(e):
                return False
            raise

    async def listdir(self, path: str = "") -> tuple[list[str], list[str]]:
        """
        List blobs with a given prefix.

        Azure Blob Storage doesn't have true directories,
        but we simulate them using name prefixes and delimiters.
        """
        prefix = path.rstrip("/") + "/" if path else ""

        # List with delimiter to get "virtual directories"
        directories = set()
        files = []

        async for blob in self.container_client.walk_blobs(
            name_starts_with=prefix,
            delimiter="/",
        ):
            # Check if this is a "directory" (prefix)
            if hasattr(blob, "name"):
                # It's a blob
                if blob.name != prefix:  # Skip the prefix itself
                    files.append(blob.name)
            else:
                # It's a prefix (virtual directory)
                directories.add(blob.prefix.rstrip("/"))

        return sorted(directories), files

    async def size(self, name: str) -> int:
        blob_client = self.container_client.get_blob_client(name)

        try:
            properties = await blob_client.get_blob_properties()
            return properties.size
        except Exception as e:
            if "BlobNotFound" in str(e):
                raise FileNotFoundError(f"File not found: {name}") from e
            raise

    async def url(self, name: str) -> str:
        if self.base_url:
            return f"{self.base_url.rstrip('/')}/{name.lstrip('/')}"

        # Generate default Azure Blob URL
        blob_client = self.container_client.get_blob_client(name)
        return blob_client.url

    async def get_accessed_time(self, name: str) -> datetime:
        # Azure Blob Storage tracks last access time
        blob_client = self.container_client.get_blob_client(name)

        try:
            properties = await blob_client.get_blob_properties()
            return properties.last_accessed_on or properties.last_modified
        except Exception as e:
            if "BlobNotFound" in str(e):
                raise FileNotFoundError(f"File not found: {name}") from e
            raise

    async def get_created_time(self, name: str) -> datetime:
        blob_client = self.container_client.get_blob_client(name)

        try:
            properties = await blob_client.get_blob_properties()
            return properties.creation_time
        except Exception as e:
            if "BlobNotFound" in str(e):
                raise FileNotFoundError(f"File not found: {name}") from e
            raise

    async def get_modified_time(self, name: str) -> datetime:
        blob_client = self.container_client.get_blob_client(name)

        try:
            properties = await blob_client.get_blob_properties()
            return properties.last_modified
        except Exception as e:
            if "BlobNotFound" in str(e):
                raise FileNotFoundError(f"File not found: {name}") from e
            raise

    async def copy(self, source: str, destination: str) -> str:
        """Efficient Azure server-side copy."""
        source_blob = self.container_client.get_blob_client(source)
        dest_blob = self.container_client.get_blob_client(destination)

        # Start the copy operation
        await dest_blob.start_copy_from_url(source_blob.url)

        return destination

    async def close(self) -> None:
        """Close the clients."""
        await self.container_client.close()
        await self.blob_service_client.close()
