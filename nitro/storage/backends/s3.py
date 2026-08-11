from datetime import datetime

try:
    import aioboto3
except ImportError:
    aioboto3 = None  # type: ignore

from nitro.storage.base import BaseStorage, StorageFile
from nitro.utils.content import Content, file_object_for, read_content


class S3File(StorageFile):
    """File object for S3 storage."""
    
    def __init__(self, session, bucket_name: str, key: str, client_kwargs: dict):
        self.session = session
        self.bucket_name = bucket_name
        self.key = key
        self.client_kwargs = client_kwargs
        self._content = None
    
    async def read(self, size: int = -1) -> bytes:
        """Read file content."""
        if self._content is None:
            # Read entire file on first read
            async with self.session.client('s3', **self.client_kwargs) as s3:
                try:
                    response = await s3.get_object(Bucket=self.bucket_name, Key=self.key)
                    async with response['Body'] as stream:
                        self._content = await stream.read()
                except s3.exceptions.NoSuchKey:
                    raise FileNotFoundError(f'File not found: {self.key}')
        
        if size == -1:
            return self._content
        else:
            return self._content[:size]
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """No-op for S3 file."""
        pass


class S3Storage(BaseStorage):
    """
    AWS S3 storage backend using aioboto3.
    
    Requires: pip install aioboto3
    
    Example configuration:
        STORAGES = {
            'default': {
                'BACKEND': 'nitro.storage.backends.s3.S3Storage',
                'LOCATION': 'my-bucket-name',
                'OPTIONS': {
                    'region_name': 'us-east-1',
                    'aws_access_key_id': 'YOUR_ACCESS_KEY',
                    'aws_secret_access_key': 'YOUR_SECRET_KEY',
                    'endpoint_url': None,  # Optional, for S3-compatible services
                    'default_acl': 'private',  # or 'public-read'
                },
                'BASE_URL': 'https://my-bucket.s3.amazonaws.com',
            }
        }
    """
    
    def __init__(self, location: str, params: dict) -> None:
        if aioboto3 is None:
            raise ImportError(
                'S3Storage requires aioboto3 package. '
                'Install it with: pip install aioboto3'
            )
        
        super().__init__(location, params)
        self.bucket_name = location
        
        # S3 client configuration
        self.region_name = self.options.get('region_name', 'us-east-1')
        self.aws_access_key_id = self.options.get('aws_access_key_id')
        self.aws_secret_access_key = self.options.get('aws_secret_access_key')
        self.endpoint_url = self.options.get('endpoint_url')
        self.default_acl = self.options.get('default_acl', 'private')
        
        # Create session
        self.session = aioboto3.Session(
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
            region_name=self.region_name,
        )
    
    def _get_client_kwargs(self) -> dict:
        """Get kwargs for creating S3 client."""
        kwargs = {}
        if self.endpoint_url:
            kwargs['endpoint_url'] = self.endpoint_url
        return kwargs
    
    async def save(self, name: str, content: Content) -> str:
        # A file goes to the client as a file, which is what lets an upload
        # already spooled to disk be read from there instead of being handed
        # over as bytes this process holds. Anything without a file behind it —
        # bytes, a chunk iterator — has to be collected first, because the
        # request needs a length up front.
        body = file_object_for(content)
        if body is None:
            body = await read_content(content)

        async with self.session.client('s3', **self._get_client_kwargs()) as s3:
            extra_args = {}
            if self.default_acl:
                extra_args['ACL'] = self.default_acl

            await s3.put_object(
                Bucket=self.bucket_name,
                Key=name,
                Body=body,
                **extra_args,
            )

        return name
    
    def open(self, name: str, mode: str = 'rb') -> StorageFile:
        """
        Open a file and return a file object.
        """
        return S3File(self.session, self.bucket_name, name, self._get_client_kwargs())
    
    async def read(self, name: str) -> bytes:
        async with self.session.client('s3', **self._get_client_kwargs()) as s3:
            try:
                response = await s3.get_object(Bucket=self.bucket_name, Key=name)
                async with response['Body'] as stream:
                    return await stream.read()
            except s3.exceptions.NoSuchKey:
                raise FileNotFoundError(f'File not found: {name}')
    
    async def delete(self, name: str) -> bool:
        async with self.session.client('s3', **self._get_client_kwargs()) as s3:
            try:
                await s3.head_object(Bucket=self.bucket_name, Key=name)
                await s3.delete_object(Bucket=self.bucket_name, Key=name)
                return True
            except s3.exceptions.ClientError:
                return False
    
    async def exists(self, name: str) -> bool:
        async with self.session.client('s3', **self._get_client_kwargs()) as s3:
            try:
                await s3.head_object(Bucket=self.bucket_name, Key=name)
                return True
            except s3.exceptions.ClientError:
                return False
    
    async def listdir(self, path: str = '') -> tuple[list[str], list[str]]:
        """
        List objects in S3 bucket with a given prefix.
        
        Note: S3 doesn't have true directories, but we simulate them
        using common prefixes.
        """
        async with self.session.client('s3', **self._get_client_kwargs()) as s3:
            prefix = path.rstrip('/') + '/' if path else ''
            
            paginator = s3.get_paginator('list_objects_v2')
            
            directories = []
            files = []
            
            async for page in paginator.paginate(
                Bucket=self.bucket_name,
                Prefix=prefix,
                Delimiter='/',
            ):
                # Common prefixes are "subdirectories"
                for common_prefix in page.get('CommonPrefixes', []):
                    directories.append(common_prefix['Prefix'].rstrip('/'))
                
                # Contents are files at this level
                for obj in page.get('Contents', []):
                    key = obj['Key']
                    # Skip the prefix itself if it's listed
                    if key != prefix:
                        files.append(key)
        
        return directories, files
    
    async def size(self, name: str) -> int:
        async with self.session.client('s3', **self._get_client_kwargs()) as s3:
            try:
                response = await s3.head_object(Bucket=self.bucket_name, Key=name)
                return response['ContentLength']
            except s3.exceptions.ClientError:
                raise FileNotFoundError(f'File not found: {name}')
    
    async def url(self, name: str) -> str:
        if self.base_url:
            return f'{self.base_url.rstrip("/")}/{name.lstrip("/")}'
        
        # Generate default S3 URL
        if self.endpoint_url:
            return f'{self.endpoint_url.rstrip("/")}/{self.bucket_name}/{name}'
        
        return f'https://{self.bucket_name}.s3.{self.region_name}.amazonaws.com/{name}'
    
    # `get_accessed_time` is left to the base class, which reports that this
    # backend cannot answer it: S3 does not record when an object was read.

    async def get_created_time(self, name: str) -> datetime:
        async with self.session.client('s3', **self._get_client_kwargs()) as s3:
            try:
                response = await s3.head_object(Bucket=self.bucket_name, Key=name)
                return response['LastModified']
            except s3.exceptions.ClientError:
                raise FileNotFoundError(f'File not found: {name}')
    
    async def get_modified_time(self, name: str) -> datetime:
        async with self.session.client('s3', **self._get_client_kwargs()) as s3:
            try:
                response = await s3.head_object(Bucket=self.bucket_name, Key=name)
                return response['LastModified']
            except s3.exceptions.ClientError:
                raise FileNotFoundError(f'File not found: {name}')
    
    async def copy(self, source: str, destination: str) -> str:
        """Efficient S3 server-side copy."""
        async with self.session.client('s3', **self._get_client_kwargs()) as s3:
            copy_source = {'Bucket': self.bucket_name, 'Key': source}
            
            extra_args = {}
            if self.default_acl:
                extra_args['ACL'] = self.default_acl
            
            await s3.copy_object(
                CopySource=copy_source,
                Bucket=self.bucket_name,
                Key=destination,
                **extra_args,
            )
        
        return destination
    
    async def close(self) -> None:
        """Close the session."""
        await self.session.close()
