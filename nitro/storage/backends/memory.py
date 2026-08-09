import asyncio
from datetime import datetime

from nitro.storage.base import BaseStorage, StorageFile


class MemoryFile(StorageFile):
    """File object for memory storage."""
    
    def __init__(self, content: bytes):
        self.content = content
        self.position = 0
    
    async def read(self, size: int = -1) -> bytes:
        """Read file content."""
        if size == -1:
            result = self.content[self.position:]
            self.position = len(self.content)
            return result
        else:
            result = self.content[self.position:self.position + size]
            self.position += len(result)
            return result
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """No-op for memory file."""
        pass


class MemoryStorage(BaseStorage):
    """
    In-memory storage backend.
    
    Stores files in memory. Not persistent across restarts.
    Use only for testing or temporary storage.
    """
    
    def __init__(self, location: str, params: dict) -> None:
        super().__init__(location, params)
        self._storage: dict[str, dict] = {}
        self._lock = asyncio.Lock()
    
    def _get_file_info(self, name: str) -> dict:
        """Get file metadata."""
        if name not in self._storage:
            raise FileNotFoundError(f'File not found: {name}')
        return self._storage[name]
    
    async def save(self, name: str, content: bytes) -> str:
        now = datetime.now()
        
        async with self._lock:
            self._storage[name] = {
                'content': content,
                'created': now,
                'modified': now,
                'accessed': now,
            }
        
        return name
    
    def open(self, name: str, mode: str = 'rb') -> StorageFile:
        """
        Open a file and return a file object.
        """
        # Get content synchronously since it's already in memory
        if name not in self._storage:
            raise FileNotFoundError(f'File not found: {name}')
        
        file_info = self._storage[name]
        file_info['accessed'] = datetime.now()
        
        return MemoryFile(file_info['content'])
    
    async def read(self, name: str) -> bytes:
        async with self._lock:
            file_info = self._get_file_info(name)
            file_info['accessed'] = datetime.now()
            return file_info['content']
    
    async def delete(self, name: str) -> bool:
        async with self._lock:
            if name not in self._storage:
                return False
            del self._storage[name]
        
        return True
    
    async def exists(self, name: str) -> bool:
        async with self._lock:
            return name in self._storage
    
    async def listdir(self, path: str = '') -> tuple[list[str], list[str]]:
        """
        List files in a virtual directory.
        
        Since memory storage is flat, we simulate directories
        by splitting on '/' and checking prefixes.
        """
        async with self._lock:
            prefix = path.rstrip('/') + '/' if path else ''
            
            directories_set = set()
            files = []
            
            for name in self._storage.keys():
                if not name.startswith(prefix):
                    continue
                
                relative_name = name[len(prefix):]
                
                if '/' in relative_name:
                    # This is in a subdirectory
                    subdir = relative_name.split('/')[0]
                    directories_set.add(prefix + subdir)
                else:
                    # This is a file at this level
                    files.append(name)
            
            directories = sorted(directories_set)
        
        return directories, files
    
    async def size(self, name: str) -> int:
        async with self._lock:
            file_info = self._get_file_info(name)
            return len(file_info['content'])
    
    async def url(self, name: str) -> str:
        if self.base_url:
            return f'{self.base_url.rstrip("/")}/{name.lstrip("/")}'
        
        # Return a memory:// URL
        return f'memory://{name}'
    
    async def get_accessed_time(self, name: str) -> datetime:
        async with self._lock:
            file_info = self._get_file_info(name)
            return file_info['accessed']
    
    async def get_created_time(self, name: str) -> datetime:
        async with self._lock:
            file_info = self._get_file_info(name)
            return file_info['created']
    
    async def get_modified_time(self, name: str) -> datetime:
        async with self._lock:
            file_info = self._get_file_info(name)
            return file_info['modified']
    
    async def copy(self, source: str, destination: str) -> str:
        async with self._lock:
            source_info = self._get_file_info(source)
            now = datetime.now()
            
            self._storage[destination] = {
                'content': source_info['content'],  # Share the same bytes object
                'created': now,
                'modified': now,
                'accessed': now,
            }
        
        return destination
    
    async def move(self, source: str, destination: str) -> str:
        async with self._lock:
            source_info = self._get_file_info(source)
            
            self._storage[destination] = source_info
            del self._storage[source]
        
        return destination
    
    async def close(self) -> None:
        """Clear all stored files."""
        async with self._lock:
            self._storage.clear()
