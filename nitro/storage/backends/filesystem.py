import os
from datetime import datetime
from pathlib import Path

import aiofiles
import aiofiles.os

from nitro.storage.base import BaseStorage, StorageFile


class FileSystemFile(StorageFile):
    """File object for filesystem storage."""
    
    def __init__(self, path: Path):
        self.path = path
        self._file = None
    
    async def read(self, size: int = -1) -> bytes:
        """Read file content."""
        if self._file is None:
            self._file = await aiofiles.open(self.path, 'rb')
        
        if size == -1:
            return await self._file.read()
        else:
            return await self._file.read(size)
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Close the file."""
        if self._file is not None:
            await self._file.close()


class FileSystemStorage(BaseStorage):
    """
    Standard filesystem storage backend.
    
    Stores files in the local filesystem.
    """
    
    def __init__(self, location: str, params: dict) -> None:
        super().__init__(location, params)
        self.base_path = Path(location).resolve()
        self.base_path.mkdir(parents=True, exist_ok=True)
    
    def _get_path(self, name: str) -> Path:
        """Get the full filesystem path for a name."""
        path = (self.base_path / name).resolve()
        
        # Ensure the path is within base_path (security check)
        if not str(path).startswith(str(self.base_path)):
            raise ValueError(f'Invalid path: {name}')
        
        return path
    
    async def save(self, name: str, content: bytes) -> str:
        path = self._get_path(name)
        
        # Create parent directories if needed
        path.parent.mkdir(parents=True, exist_ok=True)
        
        async with aiofiles.open(path, 'wb') as f:
            await f.write(content)
        
        return name
    
    def open(self, name: str, mode: str = 'rb') -> StorageFile:
        """
        Open a file and return a file object.
        """
        path = self._get_path(name)
        
        if not path.exists():
            raise FileNotFoundError(f'File not found: {name}')
        
        return FileSystemFile(path)
    
    async def read(self, name: str) -> bytes:
        path = self._get_path(name)
        
        if not path.exists():
            raise FileNotFoundError(f'File not found: {name}')
        
        async with aiofiles.open(path, 'rb') as f:
            return await f.read()
    
    async def delete(self, name: str) -> bool:
        path = self._get_path(name)
        
        if not path.exists():
            return False
        
        await aiofiles.os.remove(path)
        
        # Remove empty parent directories
        try:
            parent = path.parent
            while parent != self.base_path:
                if not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
                else:
                    break
        except (OSError, StopIteration):
            pass
        
        return True
    
    async def exists(self, name: str) -> bool:
        path = self._get_path(name)
        return path.exists()
    
    async def listdir(self, path: str = '') -> tuple[list[str], list[str]]:
        full_path = self._get_path(path) if path else self.base_path
        
        if not full_path.exists():
            raise FileNotFoundError(f'Directory not found: {path}')
        
        if not full_path.is_dir():
            raise NotADirectoryError(f'Not a directory: {path}')
        
        directories = []
        files = []
        
        for item in full_path.iterdir():
            relative_name = str(item.relative_to(self.base_path))
            if item.is_dir():
                directories.append(relative_name)
            else:
                files.append(relative_name)
        
        return directories, files
    
    async def size(self, name: str) -> int:
        path = self._get_path(name)
        
        if not path.exists():
            raise FileNotFoundError(f'File not found: {name}')
        
        stat = await aiofiles.os.stat(path)
        return stat.st_size
    
    async def url(self, name: str) -> str:
        if self.base_url:
            return f'{self.base_url.rstrip("/")}/{name.lstrip("/")}'
        
        # Return file:// URL for local files
        path = self._get_path(name)
        return path.as_uri()
    
    async def get_accessed_time(self, name: str) -> datetime:
        path = self._get_path(name)
        
        if not path.exists():
            raise FileNotFoundError(f'File not found: {name}')
        
        stat = await aiofiles.os.stat(path)
        return datetime.fromtimestamp(stat.st_atime)
    
    async def get_created_time(self, name: str) -> datetime:
        path = self._get_path(name)
        
        if not path.exists():
            raise FileNotFoundError(f'File not found: {name}')
        
        stat = await aiofiles.os.stat(path)
        # On Unix, ctime is last metadata change, not creation time
        # On Windows, ctime is creation time
        return datetime.fromtimestamp(stat.st_ctime)
    
    async def get_modified_time(self, name: str) -> datetime:
        path = self._get_path(name)
        
        if not path.exists():
            raise FileNotFoundError(f'File not found: {name}')
        
        stat = await aiofiles.os.stat(path)
        return datetime.fromtimestamp(stat.st_mtime)
    
    async def copy(self, source: str, destination: str) -> str:
        """Efficient file copy using OS-level operations."""
        source_path = self._get_path(source)
        dest_path = self._get_path(destination)
        
        if not source_path.exists():
            raise FileNotFoundError(f'File not found: {source}')
        
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Use aiofiles for async copy
        async with aiofiles.open(source_path, 'rb') as src:
            async with aiofiles.open(dest_path, 'wb') as dst:
                while True:
                    chunk = await src.read(65536)  # 64KB chunks
                    if not chunk:
                        break
                    await dst.write(chunk)
        
        return destination
    
    async def move(self, source: str, destination: str) -> str:
        """Efficient file move using OS rename when possible."""
        source_path = self._get_path(source)
        dest_path = self._get_path(destination)
        
        if not source_path.exists():
            raise FileNotFoundError(f'File not found: {source}')
        
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            # Try atomic rename first
            await aiofiles.os.rename(source_path, dest_path)
        except OSError:
            # If rename fails (e.g., cross-device), fall back to copy+delete
            await self.copy(source, destination)
            await self.delete(source)
        
        return destination
    
    async def close(self) -> None:
        """No-op for filesystem storage."""
        pass
