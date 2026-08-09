from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nitro.mail.message import EmailMessage


class BaseEmailBackend(ABC):
    """
    Abstract base class for all email backends.

    Backends handle the actual sending of email messages through
    various services (SMTP, AWS SES, SendGrid, etc.)
    """

    def __init__(
        self,
        host: str = "",
        port: int | None = None,
        username: str = "",
        password: str = "",
        use_tls: bool = False,
        use_ssl: bool = False,
        timeout: int | None = None,
        **kwargs,
    ) -> None:
        """
        Initialize the email backend.

        Args:
            host: Mail server hostname
            port: Mail server port
            username: Authentication username
            password: Authentication password
            use_tls: Whether to use TLS (STARTTLS)
            use_ssl: Whether to use SSL
            timeout: Connection timeout in seconds
            **kwargs: Additional backend-specific options
        """
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.use_ssl = use_ssl
        self.timeout = timeout
        self.fail_silently = False

        # Store additional options
        self.options = kwargs

    async def __aenter__(self):
        """Async context manager entry."""
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

    @abstractmethod
    async def open(self) -> bool:
        """
        Open a connection to the email server.

        Returns:
            True if connection was opened successfully
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """
        Close the connection to the email server.
        """
        pass

    @abstractmethod
    async def send_messages(
        self,
        email_messages: list["EmailMessage"],
        fail_silently: bool = False,
    ) -> int:
        """
        Send one or more EmailMessage objects.

        Args:
            email_messages: List of EmailMessage objects to send
            fail_silently: If True, suppress exceptions during sending

        Returns:
            Number of messages sent successfully
        """
        pass
