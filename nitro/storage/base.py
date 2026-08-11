"""The storage backend interface."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from nitro.utils.content import Content


class StorageFile(ABC):
    """
    Abstract base class for file objects returned by storage.open().

    Can be used as an async context manager.
    """

    @abstractmethod
    async def read(self, size: int = -1) -> bytes:
        """
        Read file content.

        Args:
            size: Number of bytes to read. -1 means read all.

        Returns:
            File content as bytes
        """
        pass

    async def __aenter__(self) -> "StorageFile":
        """Enter async context manager."""
        return self

    @abstractmethod
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit async context manager."""
        pass


class BaseStorage(ABC):
    """
    Abstract base class for all storage backends.
    """

    def __init__(
        self,
        location: str,
        params: dict[str, Any],
    ) -> None:
        """
        Initialize the storage backend.

        Args:
            location: Backend-specific location (path, bucket name, etc.)
            params: Additional parameters including OPTIONS
        """
        self.location = location
        self.options = params.get("OPTIONS", {})
        self.base_url = params.get("BASE_URL", None)

    @abstractmethod
    async def save(self, name: str, content: Content) -> str:
        """
        Save content to the storage.

        Args:
            name: The name/path for the file
            content: Bytes, something with a read() — an open file, an
                UploadFile, a StorageFile — or an async iterator of chunks.
                Anything but bytes is moved a chunk at a time where the backend
                can, so a large upload never has to be held whole.

        Returns:
            The name where the file was saved
        """
        pass

    @abstractmethod
    def open(self, name: str, mode: str = "rb") -> "StorageFile":
        """
        Open a file from storage.

        Args:
            name: The name/path of the file
            mode: The mode to open the file (only 'rb' is required)

        Returns:
            A StorageFile object that can be used as an async context manager

        Usage:
            async with storage.open('file.txt') as f:
                content = await f.read()
        """
        pass

    @abstractmethod
    async def read(self, name: str) -> bytes:
        """
        Read entire file content.

        Args:
            name: The name/path of the file

        Returns:
            Complete file content as bytes
        """
        pass

    @abstractmethod
    async def delete(self, name: str) -> bool:
        """
        Delete a file from storage.

        Args:
            name: The name/path of the file

        Returns:
            True if the file was deleted, False if it didn't exist
        """
        pass

    @abstractmethod
    async def exists(self, name: str) -> bool:
        """
        Check if a file exists in storage.

        Args:
            name: The name/path of the file

        Returns:
            True if the file exists
        """
        pass

    @abstractmethod
    async def listdir(self, path: str = "") -> tuple[list[str], list[str]]:
        """
        List the contents of a directory.

        Args:
            path: The directory path to list

        Returns:
            Tuple of (directories, files) lists
        """
        pass

    @abstractmethod
    async def size(self, name: str) -> int:
        """
        Get the size of a file in bytes.

        Args:
            name: The name/path of the file

        Returns:
            File size in bytes
        """
        pass

    @abstractmethod
    async def url(self, name: str) -> str:
        """
        Get the URL to access a file.

        Args:
            name: The name/path of the file

        Returns:
            URL string to access the file
        """
        pass

    @abstractmethod
    async def get_accessed_time(self, name: str) -> datetime:
        """
        Get the last accessed time of a file.

        Args:
            name: The name/path of the file

        Returns:
            Last accessed datetime
        """
        pass

    @abstractmethod
    async def get_created_time(self, name: str) -> datetime:
        """
        Get the creation time of a file.

        Args:
            name: The name/path of the file

        Returns:
            Creation datetime
        """
        pass

    @abstractmethod
    async def get_modified_time(self, name: str) -> datetime:
        """
        Get the last modified time of a file.

        Args:
            name: The name/path of the file

        Returns:
            Last modified datetime
        """
        pass

    async def copy(self, source: str, destination: str) -> str:
        """
        Copy a file within storage.

        Default implementation reads and writes.
        Backends can override for more efficient copying.

        Args:
            source: Source file name/path
            destination: Destination file name/path

        Returns:
            The destination name where file was saved
        """
        content = await self.read(source)
        return await self.save(destination, content)

    async def move(self, source: str, destination: str) -> str:
        """
        Move a file within storage.

        Default implementation copies then deletes.
        Backends can override for more efficient moving.

        Args:
            source: Source file name/path
            destination: Destination file name/path

        Returns:
            The destination name where file was saved
        """
        result = await self.copy(source, destination)
        await self.delete(source)
        return result

    @abstractmethod
    async def close(self) -> None:
        """
        Close any connections to the storage backend.
        """
        pass
