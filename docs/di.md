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

## Releasing what a dependency opened

A dependency that `yield`s its value stays suspended there until the work it was
resolved for is over, and the rest of it runs then:

```python
async def get_transaction(pool: Pool = Depends(get_pool)) -> AsyncIterator[Transaction]:
    async with pool.acquire() as connection, connection.transaction() as transaction:
        yield transaction
```

The dependency is *resumed*, not closed, so the code after the `yield` runs as
it normally would — a context manager inside sees an ordinary exit and commits.
Had it been closed, `GeneratorExit` at the `yield` would look like a failure and
roll back a request that had succeeded.

When the work fails, the exception is raised at the `yield`, so a dependency can
tell the two apart:

```python
async def get_transaction() -> AsyncIterator[Transaction]:
    transaction = await begin()
    try:
        yield transaction
    except Exception:
        await transaction.rollback()
        raise
    else:
        await transaction.commit()
```

Ordinary functions are unaffected: a dependency that returns needs no release
and gets none. A plain `def` generator works too, and is resumed in a thread.

**When "over" is depends on the protocol.** For HTTP it is after the response;
for a WebSocket or WebTransport session it is after the disconnect hook, because
the connection is the unit of work — a dependency resolved at connect is held
for as long as the connection is. Releases happen in the reverse of the order
things were acquired, and a dependency named twice is released once. One that
fails to release is logged and the rest still run.

## Something that lives longer than a request

A connection pool is not per request. `worker_scoped` says so on the provider,
which is where lifetime belongs — a pool is worker-lifetime whether a handler, a
middleware or another dependency asks for it:

```python
from nitro.di import Depends, worker_scoped


@worker_scoped
async def get_pool() -> AsyncIterator[Pool]:
    pool = await create_pool(...)
    try:
        yield pool
    finally:
        await pool.close()


async def handler(request: HttpRequest, pool: Pool = Depends(get_pool)) -> HttpResponse:
    ...                                      # the same pool every time
```

Nothing registers it and nothing imports the application to declare it. A
provider is reachable only if something depends on it, and every handler's graph
is read when its route is registered — so the worker already knows what to build
before it serves anything.

It is built at startup rather than on first use, which means a pool that cannot
connect stops the worker instead of failing whichever request happened to arrive
first. It is released at shutdown, after the `on_shutdown` callbacks, since one
of those may still want it.

A worker is a forked process, so this is one value **per worker**: with
`WORKERS = 4` there are four pools, the same way caches and storages are rebuilt
per worker because a connection cannot cross a fork.

Two things it may not do, both refused when the graph is read rather than at
runtime:

```python
@worker_scoped
async def get_pool(request):                       # DependencyScope
    ...                                            # built before any request exists

@worker_scoped
async def get_pool(user: User = Depends(get_current_user)):   # DependencyScope
    ...                                            # one request's user, kept for all of them
```

The other direction is fine and is the usual shape: a request-scoped dependency
built from a worker-scoped one, released with the request while the pool stays.

## Middleware

Middleware declares what it needs the same way a handler does:

```python
from nitro.di import Depends
from nitro.middleware import Middleware


class Auditing(Middleware):
    async def __http__(self, request, call_next, account: Account = Depends(get_account)):
        response = await call_next(request)
        await record(account, request.path)
        return response
```

`__websocket__` and `__webtransport__` take them too, and are given the socket
or session as their context.

**A dependency the middleware and the handler both name is produced once.** The
cache belongs to the connection rather than to whichever layer resolved first,
so authenticating in middleware and reading the same account in the handler is
one lookup, not two. What a middleware opened is released when the connection
has been served — after the response, not when the middleware's own frame ends,
so the handler still has it.

A middleware's graph is read when the stack is loaded, so a cycle or a scope
error stops the process at startup rather than the first request through it.

Bear in mind that middleware runs before routing: a request that matches no
route still resolves what its middleware asks for.

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
