# Intercom

Intercom moves messages between connections — between two WebSockets, between a
worker and a background job, between one service and another.

A channel is a plain string agreed on by convention. There is no registry, no
schema and no discovery: two parties agree on a name in code and test that
agreement before they deploy. Messages are ordinary Python values, encoded as
MessagePack on the wire, so a service written in another language can read them.

## Configuration

Nothing has to be configured to start. The default backend keeps everything in
this process, so Intercom works on a fresh project and its tests need nothing
running:

```python
INTERCOMS = {
    "default": {
        "BACKEND": "nitro.intercom.backends.MemoryIntercom",
        "LOCATION": "",
        "OPTIONS": {},
    }
}
```

A deployment points it at Redis:

```python
INTERCOMS = {
    "default": {
        "BACKEND": "nitro.intercom.backends.RedisIntercom",
        "LOCATION": "redis://localhost:6379",
        "OPTIONS": {"PREFIX": "myapp", "CAPACITY": 100, "EXPIRY": 60},
    }
}
```

`PREFIX` keeps several applications apart on one server. `CAPACITY` and
`EXPIRY` apply to queued channels, below. `LOCATION` is required by
`RedisIntercom` and ignored by `MemoryIntercom`.

### Which backend

| | `MemoryIntercom` | `RedisIntercom` |
|---|---|---|
| Reaches | this process | every process pointed at the same server |
| Needs | nothing | a Redis |
| For | development, tests, a single-worker deployment | anything else |

**`MemoryIntercom` does not cross workers.** `WORKERS = 2` means two processes:
a message published in one is not seen in the other, and a socket held by the
second will never receive it. If you serve with more than one worker, or more
than one machine, the backend has to be `RedisIntercom`.

The two are interchangeable otherwise — same methods, same delivery semantics,
same bounded queue, same refusal of values that could not cross a wire — so a
project can develop against one and deploy against the other.

A backend is anything importable offering
`async connect(location, *, prefix, capacity, expiry)` that returns a client
with the methods below, so a project can supply its own.

## Two ways to deliver

They answer different questions, and both are offered because neither is right
for everything.

**`publish` / `subscribe` is push delivery.** A subscriber connected now gets
the message immediately; one that is not never learns of it. Right for a live
socket, where a client that has gone away has nothing to catch up on.

```python
from nitro.intercom import intercom

reached: int = await intercom.publish("room:42", {"event": "joined"})

listener = await intercom.subscribe("room:42")
async for message in listener:
    ...
```

**`send` / `receive` is a bounded queue.** A message waits until it is read or
the channel expires, and the oldest is discarded once the channel is full.
Right when a reader may briefly be elsewhere and should not miss what happened.

```python
await intercom.send("jobs", {"task": "resize", "id": 7})

message = await intercom.receive("jobs")        # None when empty

reader = await intercom.reader("jobs")
message = await reader.receive(timeout=30)      # waits; 0 waits indefinitely
```

A reader gets a connection of its own, because a waiting read occupies its
connection and would otherwise stall everything else sharing it.

## Groups

A group is a named set of channels, so a message can be addressed to "everyone
in room 42" without the sender knowing who that is.

```python
await intercom.group_add("room:42", channel)
await intercom.group_discard("room:42", channel)

members: list[str] = await intercom.group_channels("room:42")

await intercom.group_publish("room:42", {"event": "started"})   # to those listening
await intercom.group_send("room:42", {"event": "started"})      # queued for each
```

## Listening around a connection

```python
from nitro.intercom import intercom, new_channel

channel: str = new_channel("socket")

async with intercom.listen(channel, groups=["room:42"]) as messages:
    async for message in messages:
        ...
```

Groups are joined on entry and left on exit, including when the body raises. A
socket that goes away therefore cannot leave its channel registered in a room,
receiving messages nobody reads until it expires.

`new_channel` produces a name nothing else is using — for the per-connection
channel a socket listens on, where the name only has to be unique.

## Outside Nitro

A service that is not a Nitro application installs the standalone package:

```sh
pip install nitro-intercom
```

```python
from nitro_intercom import Intercom

intercom = await Intercom.connect("redis://localhost:6379", prefix="myapp")
await intercom.publish("room:42", {"event": "joined"})
```

The two sit on the same core and interoperate. What differs is configuration: a
Nitro project's Intercom takes it from `INTERCOMS`, and the standalone package
takes it from its own arguments. A Nitro project never installs the standalone
package, and importing it there would give you a second, separately configured
client to the same server.

## What crosses the wire

`None`, `bool`, `int`, `float`, `str`, `bytes`, `list`, `tuple` and `dict`.
Tuples arrive as lists. Anything else is refused rather than coerced, because
guessing would mean the receiver gets something the sender did not mean to send.
