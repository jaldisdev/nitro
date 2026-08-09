import logging
from typing import TYPE_CHECKING

from aiosmtplib import SMTP, SMTPException

from nitro.mail.backends.base import BaseEmailBackend

if TYPE_CHECKING:
    from nitro.mail.message import EmailMessage


logger = logging.getLogger(__name__)


class SMTPBackend(BaseEmailBackend):
    """
    Email backend using SMTP protocol.

    Supports both TLS (STARTTLS) and SSL connections.

    Requires: pip install aiosmtplib

    Example configuration:
        EMAIL_BACKEND = 'nitro.mail.backends.smtp.SMTPBackend'
        EMAIL_HOST = 'smtp.gmail.com'
        EMAIL_PORT = 587
        EMAIL_HOST_USER = 'your-email@gmail.com'
        EMAIL_HOST_PASSWORD = 'your-app-password'
        EMAIL_USE_TLS = True
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._connection: SMTP | None = None

        # Set default port based on SSL/TLS
        if self.port is None:
            if self.use_ssl:
                self.port = 465
            elif self.use_tls:
                self.port = 587
            else:
                self.port = 25

    async def open(self) -> bool:
        """
        Open a connection to the SMTP server.
        """
        if self._connection is not None:
            return False

        try:
            self._connection = SMTP(
                hostname=self.host,
                port=self.port,
                timeout=self.timeout,
                use_tls=self.use_ssl,  # Direct SSL connection
            )

            await self._connection.connect()

            # STARTTLS if requested (and not using direct SSL)
            if self.use_tls and not self.use_ssl:
                await self._connection.starttls()

            # Authenticate if credentials provided
            if self.username and self.password:
                await self._connection.login(self.username, self.password)

            return True

        except SMTPException as e:
            if not self.fail_silently:
                raise
            logger.error(f"Failed to connect to SMTP server: {e}")
            return False

    async def close(self) -> None:
        """
        Close the connection to the SMTP server.
        """
        if self._connection is not None:
            try:
                await self._connection.quit()
            except SMTPException:
                pass
            finally:
                self._connection = None

    async def send_messages(
        self,
        email_messages: list["EmailMessage"],
        fail_silently: bool = False,
    ) -> int:
        """
        Send one or more EmailMessage objects.
        """
        if not email_messages:
            return 0

        self.fail_silently = fail_silently

        # Open connection if not already open
        connection_opened = await self.open()

        if self._connection is None:
            if not fail_silently:
                raise ConnectionError("Failed to connect to SMTP server")
            return 0

        num_sent = 0

        try:
            for message in email_messages:
                try:
                    await self._send(message)
                    num_sent += 1
                except Exception as e:
                    if not fail_silently:
                        raise
                    logger.error(f"Failed to send email: {e}")

        finally:
            # Close connection if we opened it
            if connection_opened:
                await self.close()

        return num_sent

    async def _send(self, email_message: "EmailMessage") -> None:
        """
        Send a single email message.
        """
        if self._connection is None:
            raise ConnectionError("No SMTP connection available")

        mime_message = email_message.message()
        from_addr = email_message.get_from_email()
        recipients = email_message.recipients()

        await self._connection.send_message(
            mime_message,
            sender=from_addr,
            recipients=recipients,
        )
