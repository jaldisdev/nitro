"""Intercom for Nitro projects.

This reaches the same publish/subscribe core as the standalone
``nitro-intercom`` package, but in this process and configured from the
project's own ``INTERCOMS`` setting. A Nitro project never installs or
configures that package.

A channel is a plain string agreed on by convention — there is no registry and
no discovery. Messages are ordinary Python values, encoded as MessagePack on
the wire so a service written in another language can read them.

    from nitro.intercom import intercom

    await intercom.publish("room:42", {"event": "joined", "user": "ada"})

    async with intercom.listen("socket:abc", groups=["room:42"]) as messages:
        async for message in messages:
            await transport.send_str(message["event"])

Which backend answers is the ``BACKEND`` key of the alias, and the default is
:class:`~nitro.intercom.backends.MemoryIntercom` — this process only, so a
project can use Intercom before it has a Redis and so its tests need nothing
running. A deployment with more than one worker has to point ``BACKEND`` at
:class:`~nitro.intercom.backends.RedisIntercom`, because separate processes
share nothing.
"""

from __future__ import annotations

import asyncio
from typing import Any

from nitro._nitro import Intercom as _Intercom
from nitro._nitro import IntercomListener, IntercomReader
from nitro.settings import ImproperlyConfigured, settings
from nitro.utils.modules import import_string

__all__ = [
    "ChannelListener",
    "Intercom",
    "IntercomListener",
    "IntercomReader",
    "get_intercom",
    "intercom",
    "new_channel",
]

DEFAULT_ALIAS = "default"

#: Used when an alias does not name one, so an entry that only gives a
#: `LOCATION` still works.
DEFAULT_BACKEND = "nitro.intercom.backends.MemoryIntercom"

Intercom = _Intercom


def new_channel(prefix: str = "channel") -> str:
    """A channel name nothing else is using."""
    return _Intercom.new_channel(prefix)


def _configuration(alias: str) -> dict[str, Any]:
    try:
        configured = settings.INTERCOMS
    except AttributeError as error:
        raise ImproperlyConfigured("no INTERCOMS setting is defined") from error

    try:
        return configured[alias]
    except KeyError:
        known = ", ".join(sorted(configured)) or "none"
        raise ImproperlyConfigured(
            f"INTERCOMS has no {alias!r} entry; configured aliases are {known}"
        ) from None


def _backend(alias: str, entry: dict[str, Any]) -> Any:
    """The backend class named by `alias`, imported."""
    path = entry.get("BACKEND") or DEFAULT_BACKEND
    try:
        return import_string(path)
    except ImportError as error:
        raise ImproperlyConfigured(
            f"INTERCOMS[{alias!r}] names the backend {path!r}, which could not be imported: {error}"
        ) from error


async def connect(alias: str = DEFAULT_ALIAS) -> Any:
    """Connect using the settings for `alias`.

    The client is whatever the backend's `connect` hands back — the compiled
    one for Redis, a Python object for the memory backend — and the two answer
    the same calls.
    """
    entry = _configuration(alias)
    backend = _backend(alias, entry)
    options = entry.get("OPTIONS", {})
    location = entry.get("LOCATION", "")

    if getattr(backend, "requires_location", True) and not location:
        raise ImproperlyConfigured(
            f"INTERCOMS[{alias!r}] uses {backend.__name__} and needs a LOCATION, "
            "for example 'redis://localhost:6379'"
        )

    return await backend.connect(
        location,
        prefix=options.get("PREFIX", ""),
        capacity=options.get("CAPACITY", 100),
        expiry=options.get("EXPIRY", 60),
    )


class _Handle:
    """The project's Intercom, connected on first use.

    Connecting is deferred because settings are not necessarily resolved when
    this module is imported, and a worker that never uses Intercom should not
    open a connection to prove it.
    """

    def __init__(self, alias: str = DEFAULT_ALIAS) -> None:
        self._alias = alias
        self._client: Any = None
        self._connecting: asyncio.Lock | None = None

    async def client(self) -> Any:
        if self._client is not None:
            return self._client

        # The lock is built lazily so that constructing the handle does not
        # require a running loop, and so a forked worker does not inherit one
        # bound to the parent's.
        if self._connecting is None:
            self._connecting = asyncio.Lock()

        async with self._connecting:
            if self._client is None:
                self._client = await connect(self._alias)
        return self._client

    def reset(self) -> None:
        """Forget the connection, so the next use reconnects.

        A forked worker must not keep using a connection opened before the
        fork: the descriptor is shared with its siblings and the parent.
        """
        self._client = None
        self._connecting = None

    async def publish(self, channel: str, message: Any) -> int:
        return await (await self.client()).publish(channel, message)

    async def subscribe(self, channel: str) -> Any:
        return await (await self.client()).subscribe(channel)

    async def send(self, channel: str, message: Any) -> None:
        await (await self.client()).send(channel, message)

    async def receive(self, channel: str) -> Any:
        return await (await self.client()).receive(channel)

    async def reader(self, channel: str) -> Any:
        return await (await self.client()).reader(channel)

    async def group_add(self, group: str, channel: str) -> None:
        await (await self.client()).group_add(group, channel)

    async def group_discard(self, group: str, channel: str) -> None:
        await (await self.client()).group_discard(group, channel)

    async def group_channels(self, group: str) -> list[str]:
        return await (await self.client()).group_channels(group)

    async def group_send(self, group: str, message: Any) -> None:
        await (await self.client()).group_send(group, message)

    async def group_publish(self, group: str, message: Any) -> int:
        return await (await self.client()).group_publish(group, message)

    async def flush(self) -> int:
        return await (await self.client()).flush()

    async def ping(self) -> None:
        await (await self.client()).ping()

    def listen(self, channel: str, groups: list[str] | None = None) -> ChannelListener:
        """Listen to `channel`, joining `groups` for as long as it is open."""
        return ChannelListener(self, channel, groups)

    def __repr__(self) -> str:
        state = "connected" if self._client is not None else "not connected"
        return f"<Intercom {self._alias!r} [{state}]>"


class ChannelListener:
    """A subscription that manages group membership around itself.

    Joining on entry and leaving on exit means a handler cannot leave a channel
    registered in a group after it has gone away — which would send messages to
    a channel nobody reads until it expires.
    """

    def __init__(
        self,
        handle: _Handle,
        channel: str,
        groups: list[str] | None = None,
    ) -> None:
        self._handle = handle
        self.channel = channel
        self.groups = list(groups or [])
        self._listener: Any = None

    async def __aenter__(self) -> ChannelListener:
        # Subscribing before joining any group means nothing published to the
        # group between the two can be missed.
        self._listener = await self._handle.subscribe(self.channel)
        for group in self.groups:
            await self._handle.group_add(group, self.channel)
        return self

    async def __aexit__(self, *_exception: object) -> None:
        for group in self.groups:
            await self._handle.group_discard(group, self.channel)
        if self._listener is not None:
            await self._listener.close()
            self._listener = None

    def __aiter__(self) -> ChannelListener:
        return self

    async def __anext__(self) -> Any:
        if self._listener is None:
            raise RuntimeError("use this listener inside 'async with'")
        return await self._listener.__anext__()

    async def receive(self) -> Any:
        """The next message, or `None` once the subscription has ended."""
        if self._listener is None:
            raise RuntimeError("use this listener inside 'async with'")
        return await self._listener.receive()


_handles: dict[str, _Handle] = {}


def get_intercom(alias: str = DEFAULT_ALIAS) -> _Handle:
    """The project's Intercom for `alias`, connected on first use."""
    if alias not in _handles:
        _handles[alias] = _Handle(alias)
    return _handles[alias]


def reset_connections() -> None:
    """Forget every connection. Called after a worker forks.

    The memory backend's contents go with them: what the parent queued belongs
    to the parent, and a worker that inherited a copy would answer from it.
    """
    from nitro.intercom.backends import reset_store

    for handle in _handles.values():
        handle.reset()
    reset_store()


#: The default Intercom, connected on first use.
intercom = get_intercom()
