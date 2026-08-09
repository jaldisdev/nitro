import sys
from typing import TYPE_CHECKING

from nitro.mail.backends.base import BaseEmailBackend

if TYPE_CHECKING:
    from nitro.mail.message import EmailMessage


class ConsoleBackend(BaseEmailBackend):
    """
    Email backend that writes messages to console/stdout.

    Useful for development and testing.

    Example configuration:
        EMAIL_BACKEND = 'nitro.mail.backends.console.ConsoleBackend'
    """

    def __init__(self, stream=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.stream = stream or sys.stdout

    async def open(self) -> bool:
        """No connection needed for console backend."""
        return True

    async def close(self) -> None:
        """No connection to close for console backend."""
        pass

    async def send_messages(
        self,
        email_messages: list["EmailMessage"],
        fail_silently: bool = False,
    ) -> int:
        """
        Write email messages to console.
        """
        num_sent = 0

        for message in email_messages:
            mime_message = message.message()

            self.stream.write("-" * 79)
            self.stream.write("\n")
            self.stream.write(mime_message.as_string())
            self.stream.write("\n")
            self.stream.write("-" * 79)
            self.stream.write("\n")

            num_sent += 1

        return num_sent
