# Dependency injection

A handler declares what it needs by giving a parameter a `Depends` default. The
value is produced by calling what that names, recursively, before the handler
runs.

```python
from nitro.di import Depends
from nitro.protocols import HttpRequest, HttpResponse, JSONResponse


async def get_database() -> Database:
    return await connect()


async def get_current_user(request: HttpRequest, database: Database = Depends(get_database)) -> User:
    return await database.find_user(request.headers.get("authorization"))


async def profile(request: HttpRequest, user: User = Depends(get_current_user)) -> HttpResponse:
    return JSONResponse(user.as_dict())
```

A dependency may be async or plain. It may ask for the request, socket or
session by naming a parameter `request`, `websocket`, `session` or `scope`, and
it may depend on other dependencies.

## Caching is per request

A dependency named more than once in a request is called once and shared:

```python
async def handler(
    request: HttpRequest,
    database: Database = Depends(get_database),
    user: User = Depends(get_current_user),      # also needs the database
) -> HttpResponse:
    ...                                          # one connection, not two
```

and never shared *between* requests. This matters: a dependency that opens a
transaction, reads the signed-in user or generates a request identifier must
produce a fresh value each time, and the same value throughout one request. The
cache lives for the span of a request and is passed down; it is never held on
anything longer-lived.

Turn it off for something that must be produced afresh every time it is asked
for:

```python
async def handler(request: HttpRequest, token: str = Depends(mint_token, use_cache=False)):
    ...
```

A dependency returning `None` caches like any other value, so it is not called
again on the assumption that it failed.

## Cycles

A dependency that depends on itself, directly or through others, is reported
when the graph is read rather than when a request arrives:

```
DependencyCycle: dependencies form a cycle: get_a -> get_b -> get_a
```

## Resolving by hand

```python
from nitro.di import DependencyCache, extract_dependencies, resolve_dependencies

dependencies = extract_dependencies(handler)
values = await resolve_dependencies(dependencies, request, DependencyCache())
```

Pass one cache for the whole of a request. Leaving it out gives each call a
cache of its own, which is right for a one-off resolution and wrong for a
request.
