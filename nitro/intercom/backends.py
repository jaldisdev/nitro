"""Intercom backends.

A backend is whatever the ``BACKEND`` key of an ``INTERCOMS`` entry names. It
has to offer an ``async connect(location, *, prefix, capacity, expiry)`` that
returns a client with the methods :mod:`nitro.intercom` calls — which is the
surface the compiled client already has, so a backend is free to be that client
or to be a Python object shaped like it.

Two ship with Nitro:

:class:`MemoryIntercom`
    Everything in this process. The default, so a project can use Intercom
    before it has a Redis to point at.

:class:`RedisIntercom`
    The compiled client, backed by Redis. What a deployment uses.

Both keep the same semantics, described once here because the memory backend
exists to stand in for the other:

* ``publish`` reaches whoever is subscribed at that moment and nobody else, and
  reports how many that was.
* ``send`` appends to a queue holding at most ``CAPACITY`` messages, discarding
  the oldest when it is full, and ``receive`` takes the oldest. An idle channel
  or group is forgotten after ``EXPIRY`` seconds.
* A message is restricted to the types that cross the wire unambiguously, so a
  value that works against the memory backend works against Redis. A tuple
  arrives as a list, as it does through MessagePack.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Final

from nitro._nitro import Intercom as _CompiledIntercom

__all__ = ["MemoryIntercom", "MemoryListener", "MemoryReader", "RedisIntercom"]

#: What a message may be built from. Anything else is refused rather than
#: coerced, matching the compiled client, because guessing would mean the
#: receiver gets something the sender did not mean to send.
_SENDABLE: Final = (bool, int, float, str, bytes)


def _sendable(value: Any) -> Any:
    """`value` as it would arrive at the other end, or a `TypeError`.

    Copied on the way in, so a sender that goes on to mutate what it sent does
    not change what a reader will see — which is what crossing a wire would
    have given it.
    """
    if value is None or isinstance(value, _SENDABLE):
        return value
    if isinstance(value, dict):
        return {_sendable(key): _sendable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sendable(item) for item in value]

    raise TypeError(
        f"{type(value).__name__} cannot be sent through a channel; "
        "use None, bool, int, float, str, bytes, list, tuple or dict"
    )


class _Entry:
    """One channel's queue, or one group's membership, and when it expires."""

    __slots__ = ("expires_at", "messages", "members")

    def __init__(self) -> None:
        self.messages: deque[Any] = deque()
        self.members: set[str] = set()
        self.expires_at: float = 0.0

    def touch(self, expiry: float) -> None:
        self.expires_at = time.monotonic() + expiry

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at


class _Store:
    """The messages, groups and subscribers held in this process.

    One store for the whole process rather than one per client, so two clients
    built from the same settings reach each other the way two connections to
    one Redis would. Keys are namespaced exactly as the Redis backend does, so
    a prefix keeps two applications apart here too.
    """

    def __init__(self) -> None:
        self.entries: dict[str, _Entry] = {}
        self.subscribers: dict[str, list[MemoryListener]] = {}
        self.waiters: dict[str, list[asyncio.Future[None]]] = {}

    def entry(self, key: str, expiry: float) -> _Entry:
        """The entry for `key`, created or renewed."""
        found = self.entries.get(key)
        if found is None or found.expired:
            found = _Entry()
            self.entries[key] = found
        found.touch(expiry)
        return found

    def live(self, key: str) -> _Entry | None:
        """The entry for `key` if it exists and has not expired."""
        found = self.entries.get(key)
        if found is None:
            return None
        if found.expired:
            del self.entries[key]
            return None
        return found

    def wake_one(self, key: str) -> None:
        """Wake a single reader waiting on `key`, if any is."""
        waiting = self.waiters.get(key)
        while waiting:
            future = waiting.pop(0)
            if not future.done():
                future.set_result(None)
                return

    def waiter(self, key: str) -> asyncio.Future[None]:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waiters.setdefault(key, []).append(future)
        return future

    def forget(self, key: str, future: asyncio.Future[None]) -> None:
        waiting = self.waiters.get(key)
        if waiting is None:
            return
        if future in waiting:
            waiting.remove(future)
        if not waiting:
            del self.waiters[key]


#: The process-wide store. Rebuilt per worker by `reset_connections`, since a
#: worker is forked and what the parent held is not its to serve.
_store = _Store()


def reset_store() -> None:
    """Forget everything the memory backend holds. Called after a worker forks."""
    global _store
    _store = _Store()


class MemoryListener:
    """A live subscription to one channel, iterable with `async for`."""

    def __init__(self, store: _Store, key: str) -> None:
        self._store = store
        self._key = key
        self._messages: asyncio.Queue[Any] = asyncio.Queue()
        self._closed = False

    def deliver(self, message: Any) -> None:
        self._messages.put_nowait(message)

    def __aiter__(self) -> MemoryListener:
        return self

    async def __anext__(self) -> Any:
        message = await self.receive()
        if message is None and self._closed:
            raise StopAsyncIteration
        return message

    async def receive(self) -> Any:
        """The next message, or `None` once the subscription has ended."""
        if self._closed and self._messages.empty():
            return None
        return await self._messages.get()

    async def close(self) -> None:
        """Stop listening."""
        if self._closed:
            return
        self._closed = True
        listeners = self._store.subscribers.get(self._key)
        if listeners is not None and self in listeners:
            listeners.remove(self)
            if not listeners:
                del self._store.subscribers[self._key]
        # Releases anyone parked in `receive`, which then reports the end.
        self._messages.put_nowait(None)


#: Distinguishes "no message" from a message that is legitimately `None`.
_MISSING: Final = object()


class MemoryReader:
    """A queued-channel reader that can wait for a message."""

    def __init__(self, store: _Store, key: str) -> None:
        self._store = store
        self._key = key

    async def receive(self, timeout: float = 0.0) -> Any:
        """The oldest queued message, waiting up to `timeout` seconds.

        Zero waits indefinitely, which is what the compiled reader means by
        zero and what a reader with nothing else to do usually wants.
        """
        deadline = None if timeout <= 0 else time.monotonic() + timeout

        while True:
            message = self._take()
            if message is not _MISSING:
                return message

            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return None

            future = self._store.waiter(self._key)
            try:
                await asyncio.wait_for(future, remaining)
            except TimeoutError:
                return None
            finally:
                self._store.forget(self._key, future)

    async def try_receive(self) -> Any:
        """A queued message if one is already there."""
        message = self._take()
        return None if message is _MISSING else message

    def _take(self) -> Any:
        entry = self._store.live(self._key)
        if entry is None or not entry.messages:
            return _MISSING
        return entry.messages.pop()


class MemoryIntercom:
    """Intercom within one process.

    Everything published, queued or grouped stays in this interpreter. That is
    what makes it the right default — a project can write against Intercom
    before it has a Redis to point at, and its tests need nothing running.

    It is equally what makes it wrong for a deployment with more than one
    worker: `WORKERS = 2` means two processes, and a message published in one
    is not seen in the other. Point ``BACKEND`` at :class:`RedisIntercom` for
    anything that runs more than a single process.
    """

    #: Nothing to address, so a `LOCATION` is neither needed nor read.
    requires_location: Final = False

    def __init__(self, prefix: str = "", capacity: int = 100, expiry: float = 60.0) -> None:
        self.prefix = prefix
        self.capacity = max(1, capacity)
        self.expiry = max(1.0, expiry)

    @classmethod
    async def connect(
        cls,
        location: str = "",
        *,
        prefix: str = "",
        capacity: int = 100,
        expiry: float = 60.0,
    ) -> MemoryIntercom:
        """Build a client. `location` is accepted and ignored."""
        return cls(prefix=prefix, capacity=capacity, expiry=expiry)

    @staticmethod
    def new_channel(prefix: str = "channel") -> str:
        """A channel name nothing else is using."""
        return _CompiledIntercom.new_channel(prefix)

    # ── keys ─────────────────────────────────────────────────────────────────

    def _channel_key(self, channel: str) -> str:
        return self._key(f"channel:{channel}")

    def _group_key(self, group: str) -> str:
        return self._key(f"group:{group}")

    def _key(self, suffix: str) -> str:
        return f"{self.prefix}:{suffix}" if self.prefix else suffix

    # ── push delivery ────────────────────────────────────────────────────────

    async def publish(self, channel: str, message: Any) -> int:
        """Deliver to whoever is listening right now, returning how many."""
        payload = _sendable(message)
        listeners = list(_store.subscribers.get(self._channel_key(channel), ()))
        for listener in listeners:
            listener.deliver(payload)
        return len(listeners)

    async def subscribe(self, channel: str) -> MemoryListener:
        """Listen to a channel."""
        key = self._channel_key(channel)
        listener = MemoryListener(_store, key)
        _store.subscribers.setdefault(key, []).append(listener)
        return listener

    async def group_publish(self, group: str, message: Any) -> int:
        reached = 0
        for channel in await self.group_channels(group):
            reached += await self.publish(channel, message)
        return reached

    # ── queued delivery ──────────────────────────────────────────────────────

    async def send(self, channel: str, message: Any) -> None:
        """Queue a message for whoever reads the channel next."""
        self._queue(self._channel_key(channel), _sendable(message))

    def _queue(self, key: str, payload: Any) -> None:
        entry = _store.entry(key, self.expiry)
        entry.messages.appendleft(payload)
        while len(entry.messages) > self.capacity:
            entry.messages.pop()
        _store.wake_one(key)

    async def receive(self, channel: str) -> Any:
        """The oldest queued message, or `None`."""
        entry = _store.live(self._channel_key(channel))
        if entry is None or not entry.messages:
            return None
        return entry.messages.pop()

    async def reader(self, channel: str) -> MemoryReader:
        """A reader that can wait for messages."""
        return MemoryReader(_store, self._channel_key(channel))

    async def group_send(self, group: str, message: Any) -> None:
        payload = _sendable(message)
        for channel in await self.group_channels(group):
            self._queue(self._channel_key(channel), payload)

    # ── groups ───────────────────────────────────────────────────────────────

    async def group_add(self, group: str, channel: str) -> None:
        _store.entry(self._group_key(group), self.expiry).members.add(channel)

    async def group_discard(self, group: str, channel: str) -> bool:
        entry = _store.live(self._group_key(group))
        if entry is None or channel not in entry.members:
            return False
        entry.members.discard(channel)
        return True

    async def group_channels(self, group: str) -> list[str]:
        entry = _store.live(self._group_key(group))
        return sorted(entry.members) if entry is not None else []

    async def group_size(self, group: str) -> int:
        entry = _store.live(self._group_key(group))
        return len(entry.members) if entry is not None else 0

    # ── housekeeping ─────────────────────────────────────────────────────────

    async def flush(self) -> int:
        """Remove every channel and group under this client's prefix."""
        owned = f"{self.prefix}:" if self.prefix else ""
        keys = [key for key in _store.entries if key.startswith(owned)]
        for key in keys:
            del _store.entries[key]
        return len(keys)

    async def ping(self) -> None:
        """Always reachable: it is this process."""

    def __repr__(self) -> str:
        return f"MemoryIntercom(prefix={self.prefix!r})"


class RedisIntercom:
    """The compiled Redis-backed client.

    A thin name for :class:`nitro._nitro.Intercom`, so ``BACKEND`` can point at
    something importable from Python and both backends are reached the same
    way. :meth:`connect` hands back the compiled client itself.
    """

    #: Redis has to be addressed, so a `LOCATION` is required.
    requires_location: Final = True

    @staticmethod
    async def connect(
        location: str,
        *,
        prefix: str = "",
        capacity: int = 100,
        expiry: float = 60.0,
    ) -> Any:
        return await _CompiledIntercom.connect(
            location, prefix=prefix, capacity=capacity, expiry=expiry
        )

    @staticmethod
    def new_channel(prefix: str = "channel") -> str:
        return _CompiledIntercom.new_channel(prefix)
