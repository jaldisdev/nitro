# Caching

Caches are declared in `CACHES` and reached by alias.

```python
CACHES = {
    "default": {
        "BACKEND": "nitro.cache.backends.RedisCache",
        "LOCATION": "redis://localhost:6379/0",
        "TIMEOUT": 300,
        "KEY_PREFIX": "myapp",
        "VERSION": 1,
        "OPTIONS": {},
    }
}
```

```python
from nitro.cache import cache, caches

await cache.set("key", {"a": 1}, timeout=60)
value = await cache.get("key", default=None)

await caches["sessions"].set("key", "value")
```

`cache` is shorthand for `caches["default"]`. Each backend is built the first
time its alias is used, so a cache a project configures but never touches is
never connected to.

## Backends

| Backend | Needs |
|---|---|
| `nitro.cache.backends.MemoryCache` | nothing |
| `nitro.cache.backends.RedisCache` | `nitro[redis]` |
| `nitro.cache.backends.MemcachedCache` | `nitro[memcached]` |

`MemoryCache` is per process. With more than one worker each has its own, so it
suits caching that can differ between workers and not sessions.

## What a cache does

```python
await cache.set("key", value, timeout=60)     # timeout=0 never expires
await cache.add("key", value)                 # only if absent
await cache.get_or_set("key", produce)        # default may be a callable

await cache.get_many(["a", "b"])
await cache.set_many({"a": 1, "b": 2})
await cache.delete_many(["a", "b"])

await cache.incr("counter")
await cache.decr("counter")
await cache.touch("key", timeout=120)         # extend without rewriting

await cache.has_key("key")
await cache.clear()
```

`KEY_PREFIX` and `VERSION` are both part of the stored key, so bumping
`VERSION` invalidates everything without touching the server.

## Across a fork

Backends are rebuilt in each worker as it starts. A connection opened before the
fork would be shared by every worker at once, which is why they are built on
first use inside the worker rather than at import time.
