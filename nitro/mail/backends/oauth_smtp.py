import base64
import logging
from typing import TYPE_CHECKING

from aiosmtplib import SMTP, SMTPException

from nitro.mail.backends.base import BaseEmailBackend

if TYPE_CHECKING:
    from nitro.mail.message import EmailMessage


logger = logging.getLogger(__name__)


class OAuth2SMTPBackend(BaseEmailBackend):
    """
    Email backend using SMTP with OAuth2 authentication.

    Supports Microsoft 365 and other OAuth2-enabled SMTP servers.

    Requires: pip install aiosmtplib

    Example configuration:
        EMAIL_BACKEND = 'nitro.mail.backends.oauth_smtp.OAuth2SMTPBackend'
        EMAIL_HOST = 'smtp.office365.com'
        EMAIL_PORT = 587
        EMAIL_HOST_USER = 'your-email@company.com'
        EMAIL_USE_TLS = True
        EMAIL_OAUTH2_TOKEN = 'your-oauth2-access-token'

    Or use EMAIL_OAUTH2_TOKEN_CALLBACK for dynamic token retrieval:
        EMAIL_OAUTH2_TOKEN_CALLBACK = 'myapp.auth.get_smtp_token'
    """

    def __init__(
        self,
        oauth2_token: str | None = None,
        oauth2_token_callback: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._connection: SMTP | None = None
        self.oauth2_token = oauth2_token
        self.oauth2_token_callback = oauth2_token_callback

        # Set default port for OAuth SMTP
        if self.port is None:
            self.port = 587

        # OAuth typically requires TLS
        if not self.use_tls and not self.use_ssl:
            self.use_tls = True

    async def _get_oauth_token(self) -> str:
        """
        Get OAuth2 token either from configuration or callback.
        """
        if self.oauth2_token:
            return self.oauth2_token

        if self.oauth2_token_callback:
            # Import and call the callback function
            import importlib

            module_path, func_name = self.oauth2_token_callback.rsplit(".", 1)
            module = importlib.import_module(module_path)
            callback = getattr(module, func_name)

            # Call the callback (it may be async or sync)
            import inspect

            if inspect.iscoroutinefunction(callback):
                return await callback()
            return callback()

        raise ValueError(
            "OAuth2 token not configured. Set EMAIL_OAUTH2_TOKEN or "
            "EMAIL_OAUTH2_TOKEN_CALLBACK in settings."
        )

    def _build_oauth_string(self, user: str, token: str) -> str:
        """
        Build OAuth2 authentication string.

        Format: base64(user=<user>\x01auth=Bearer <token>\x01\x01)
        """
        auth_string = f"user={user}\x01auth=Bearer {token}\x01\x01"
        return base64.b64encode(auth_string.encode()).decode()

    async def open(self) -> bool:
        """
        Open a connection to the SMTP server with OAuth2 authentication.
        """
        if self._connection is not None:
            return False

        try:
            self._connection = SMTP(
                hostname=self.host,
                port=self.port,
                timeout=self.timeout,
                use_tls=self.use_ssl,
            )

            await self._connection.connect()

            # STARTTLS if requested
            if self.use_tls and not self.use_ssl:
                await self._connection.starttls()

            # Authenticate with OAuth2
            token = await self._get_oauth_token()
            oauth_string = self._build_oauth_string(self.username, token)

            # Send AUTH XOAUTH2 command
            await self._connection.execute_command(
                "AUTH",
                "XOAUTH2",
                oauth_string,
            )

            return True

        except SMTPException as e:
            if not self.fail_silently:
                raise
            logger.error(f"Failed to connect to SMTP server with OAuth2: {e}")
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
