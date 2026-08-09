# Storage

Storage backends are declared in `STORAGES` and reached by alias.

```python
STORAGES = {
    "default": {
        "BACKEND": "nitro.storage.backends.FileSystemStorage",
        "LOCATION": "/var/data/uploads",
        "OPTIONS": {},
    },
    "media": {
        "BACKEND": "nitro.storage.backends.S3Storage",
        "LOCATION": "my-bucket",
        "OPTIONS": {"region_name": "eu-central-1"},
    },
}
```

```python
from nitro.storage import storage, storages

name: str = await storage.save("photos/one.jpg", data)
exists: bool = await storage.exists(name)
url: str = await storages["media"].url(name)
await storage.delete(name)
```

`storage` is shorthand for `storages["default"]`. Each backend is built the
first time its alias is used, and rebuilt in each worker after a fork.

## Backends

| Backend | Needs | `LOCATION` |
|---|---|---|
| `FileSystemStorage` | nothing | a directory |
| `MemoryStorage` | nothing | ignored |
| `S3Storage` | `nitro[aws]` | the bucket |
| `AzureStorage` | `nitro[azure]` | the container |

`MemoryStorage` is per process and does not survive a restart. It is for tests.
