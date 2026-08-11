"""The email backend interface.

A backend is a way of getting a message to somebody: an SMTP server, an HTTP
API, a stream on the console. What they have in common is only that: open,
send, close. Nothing about connections, hosts or credentials belongs here,
because half of the backends have no such thing — an API key is not a password
and an endpoint is not a host, and a base class that insisted otherwise would
be describing SMTP rather than email.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from nitro.mail.message import EmailMessage

__all__ = ["BaseEmailBackend"]


class BaseEmailBackend(ABC):
    """What every email backend answers.

    Usable as an async context manager, which opens on entry and closes on
    exit — the way to send several messages over one connection.

        async with get_connection() as connection:
            await connection.send_messages([first, second])
    """

    #: The settings this backend is built from, as constructor keyword to
    #: setting name. `get_connection` reads it to decide what to pass, so a
    #: backend declares what it wants rather than being guessed at by the
    #: shape of its import path.
    settings_map: ClassVar[dict[str, str]] = {}

    def __init__(self, *, fail_silently: bool = False, **options: Any) -> None:
        #: Whether failures are logged rather than raised, when a caller does
        #: not say. Only ever read: `send_messages` takes its own, and writing
        #: this per call would let one caller's choice leak into another's.
        self.fail_silently = fail_silently
        self.options = options

    def _silence(self, fail_silently: bool | None) -> bool:
        """Whether to swallow failures for this call."""
        return self.fail_silently if fail_silently is None else fail_silently

    async def __aenter__(self) -> BaseEmailBackend:
        await self.open()
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    @abstractmethod
    async def open(self) -> bool:
        """Make the backend ready to send.

        Returns whether this call is what opened it, so a caller knows whether
        closing it again is its business. A backend that cannot open raises —
        whether that is fatal is :meth:`send_messages`'s decision, not this
        one's.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release whatever :meth:`open` acquired. Safe to call twice."""

    @abstractmethod
    async def send_messages(
        self,
        email_messages: list[EmailMessage],
        fail_silently: bool | None = None,
    ) -> int:
        """Send `email_messages`, returning how many were sent.

        `fail_silently` overrides the backend's own setting for this call.
        """
