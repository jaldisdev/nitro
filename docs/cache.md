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
| `nitro.cache.backends.MemcachedCache` | `nitro[memcached]`, Python 3.13 |

`MemoryCache` is per process. With more than one worker each has its own, so it
suits caching that can differ between workers and not sessions.

`MemcachedCache` needs emcache, which publishes nothing for Python 3.14 — the
extra is marked accordingly and will not install there. Use `RedisCache` on
3.14 until emcache catches up.

## What is stored

A value has to be turned into bytes for a store outside this process. The
`SERIALIZER` option decides how.

```python
CACHES = {
    "default": {
        "BACKEND": "nitro.cache.backends.RedisCache",
        "LOCATION": "redis://localhost:6379/0",
        "OPTIONS": {"SERIALIZER": "json"},
    }
}
```

| `SERIALIZER` | Carries | |
|---|---|---|
| `"json"` *(default)* | `None`, booleans, numbers, strings, lists, dictionaries with string keys | A tuple comes back as a list. Anything else is refused rather than mangled. |
| `"pickle"` | almost any Python object | **Reading it runs code contained in the data.** |

JSON is the default because reading it cannot execute anything. Pickle is
available, but it is a decision rather than a convenience:

> **Unpickling runs code.** Anyone who can write to the cache store — a shared
> Redis, a Memcached on a network somebody else can reach, an operator who can
> set a key — can run code in every process that reads it. Choose `"pickle"`
> only for a store nothing else can write to, and prefer caching a
> JSON-compatible representation you chose.

`MemoryCache` keeps Python objects as they are and does not serialize at all,
so the option does not apply to it.

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
