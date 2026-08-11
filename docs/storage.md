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

## What can be saved

`save()` takes bytes, anything with a `read()`, or an async iterator of chunks:

```python
await storage.save("one.jpg", data)                       # bytes
await storage.save("two.jpg", open("photo.jpg", "rb"))    # an open file
await storage.save("three.jpg", (await request.form())["photo"])   # an upload
await storage.save("four.jpg", produce_chunks())          # an async iterator
await storage.save("copy.jpg", storages["media"].open("one.jpg"))  # another backend's file
```

Anything but bytes is moved a chunk at a time where the backend can manage it,
which is the point of accepting them: an upload past `MAX_UPLOAD_MEMORY` is
already a file on disk, and saving it should not mean loading it back into this
process first.

What each backend does with that:

| Backend | |
|---|---|
| `FileSystemStorage` | writes as it reads, so a save is a copy between two files |
| `S3Storage`, `AzureStorage` | hand the file to their own client, which reads it |
| `MemoryStorage` | reads it whole, having nowhere to stream it to |

A file that has already been read is rewound first, so an upload a handler
inspected before saving still arrives whole. A synchronous `read()` is handed to
a thread rather than blocking the loop.

## Backends

| Backend | Needs | `LOCATION` |
|---|---|---|
| `FileSystemStorage` | nothing | a directory |
| `MemoryStorage` | nothing | ignored |
| `S3Storage` | `nitro[aws]` | the bucket |
| `AzureStorage` | `nitro[azure]` | the container |

`MemoryStorage` is per process and does not survive a restart. It is for tests.
