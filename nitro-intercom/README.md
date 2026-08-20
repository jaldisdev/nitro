# nitro-intercom

Publish/subscribe channels for Python services, backed by the same Rust core
the Nitro framework uses internally.

## Installation

```sh
pip install nitro-intercom
```

Python 3.13 or newer. The distribution is named `nitro-intercom`; the package
it installs is `nitro_intercom`, which is what you import. The wheel carries the
compiled core, so there is nothing to build. Redis is what it connects to, not
something it installs.

Install this only if you are **not** running on Nitro. Nitro projects should
import `nitro.intercom`, which reaches the same core in-process and takes its
configuration from the project's settings object instead of a separate config
surface.

## Connecting

```python
from nitro_intercom import Intercom

intercom = await Intercom.connect("redis://localhost:6379", prefix="myapp")
```

`prefix` keeps several applications apart on one server. `capacity` and
`expiry` apply to queued channels.

## Two ways to deliver

**`publish` / `subscribe` is push delivery.** A subscriber connected now gets
the message immediately; one that is not never learns of it.

```python
reached: int = await intercom.publish("room:42", {"event": "joined", "user": "ada"})

listener = await intercom.subscribe("room:42")
async for message in listener:
    print(message)
await listener.close()
```

**`send` / `receive` is a bounded queue.** A message waits until it is read or
the channel expires, and the oldest is discarded once the channel is full.

```python
await intercom.send("jobs", {"task": "resize", "id": 7})

message = await intercom.receive("jobs")        # None when empty

reader = await intercom.reader("jobs")
message = await reader.receive(timeout=30)      # waits; 0 waits indefinitely
```

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

## What crosses the wire

`None`, `bool`, `int`, `float`, `str`, `bytes`, `list`, `tuple` and `dict`,
encoded as MessagePack so a service written in another language can read them.
Tuples arrive as lists. Anything else is refused rather than coerced, because
guessing would mean the receiver gets something the sender did not mean to
send.

## License

Licensed under either of [Apache License, Version 2.0](LICENSE-APACHE) or the
[MIT license](LICENSE-MIT), at your option.
