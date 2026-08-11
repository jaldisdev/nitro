import mimetypes
from dataclasses import dataclass
from email.message import EmailMessage as StdlibEmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path


@dataclass
class EmailRecipient:
    """
    Represents an email recipient with optional display name.

    Example:
        recipient = EmailRecipient("user@example.com", "John Doe")
    """

    email: str
    name: str | None = None

    def __str__(self) -> str:
        if self.name:
            return formataddr((self.name, self.email))
        return self.email


@dataclass
class EmailHeader:
    """
    Represents a custom email header.

    Example:
        header = EmailHeader("X-Priority", "1")
    """

    name: str
    value: str


@dataclass
class EmailAttachment:
    """
    Represents an email attachment.

    Example:
        # From file
        attachment = EmailAttachment.from_file("report.pdf")

        # From bytes
        attachment = EmailAttachment(
            filename="data.json",
            content=b'{"key": "value"}',
            mimetype="application/json"
        )
    """

    filename: str
    content: bytes
    mimetype: str | None = None

    @classmethod
    def from_file(cls, filepath: str | Path) -> "EmailAttachment":
        """Create an attachment from a file path."""
        path = Path(filepath)

        if not path.exists():
            raise FileNotFoundError(f"File not found: {filepath}")

        content = path.read_bytes()
        mimetype = mimetypes.guess_type(str(path))[0]

        return cls(
            filename=path.name,
            content=content,
            mimetype=mimetype,
        )


RecipientType = str | EmailRecipient | tuple[str, str]


class EmailMessage:
    """
    Container for constructing email messages with support for:
    - Plain text and HTML content
    - Multiple recipients (to, cc, bcc)
    - Attachments
    - Custom headers
    - Reply-to addresses

    Example:
        message = EmailMessage(
            subject="Hello",
            body="Plain text content",
            from_email="sender@example.com",
            to=["recipient@example.com"],
        )

        # Add HTML alternative
        message.html = "<p>HTML content</p>"

        # Add attachment
        message.attach(EmailAttachment.from_file("report.pdf"))

        # Send
        await message.send()
    """

    def __init__(
        self,
        subject: str = "",
        body: str = "",
        from_email: str | None = None,
        to: list[RecipientType] | None = None,
        cc: list[RecipientType] | None = None,
        bcc: list[RecipientType] | None = None,
        reply_to: list[RecipientType] | None = None,
        headers: dict[str, str] | list[EmailHeader] | None = None,
        attachments: list[EmailAttachment] | None = None,
    ) -> None:
        """
        Initialize an email message.

        Args:
            subject: Email subject
            body: Plain text body content
            from_email: Sender email address
            to: List of recipient addresses
            cc: List of CC recipient addresses
            bcc: List of BCC recipient addresses
            reply_to: List of reply-to addresses
            headers: Custom headers as dict or list of EmailHeader
            attachments: List of EmailAttachment objects
        """
        self.subject = subject
        self.body = body
        self.from_email = from_email
        self.to = to or []
        self.cc = cc or []
        self.bcc = bcc or []
        self.reply_to = reply_to or []
        self.attachments = attachments or []
        self.html: str | None = None

        # Process headers
        self.extra_headers: dict[str, str] = {}
        if headers:
            if isinstance(headers, dict):
                self.extra_headers = headers
            else:
                self.extra_headers = {h.name: h.value for h in headers}

    def attach(self, attachment: EmailAttachment) -> None:
        """Add an attachment to the message."""
        self.attachments.append(attachment)

    def attach_file(self, filepath: str | Path) -> None:
        """Add a file attachment from a file path."""
        self.attach(EmailAttachment.from_file(filepath))

    def _normalize_recipient(self, recipient: RecipientType) -> str:
        """Convert various recipient formats to email address string."""
        if isinstance(recipient, str):
            return recipient
        elif isinstance(recipient, EmailRecipient):
            return str(recipient)
        elif isinstance(recipient, tuple):
            return formataddr(recipient)
        return str(recipient)

    def _normalize_recipients(self, recipients: list[RecipientType]) -> list[str]:
        """Normalize a list of recipients to email address strings."""
        return [self._normalize_recipient(r) for r in recipients]

    def get_from_email(self) -> str:
        """Get the from email, using default from settings if not set."""
        if self.from_email:
            return self.from_email

        from nitro.settings import settings

        return settings.DEFAULT_FROM_EMAIL

    def recipients(self) -> list[str]:
        """Get all recipients (to, cc, bcc)."""
        return (
            self._normalize_recipients(self.to)
            + self._normalize_recipients(self.cc)
            + self._normalize_recipients(self.bcc)
        )

    def message(self) -> StdlibEmailMessage:
        """
        Build and return the email message object using stdlib EmailMessage.
        """
        msg = StdlibEmailMessage()

        # Set headers
        msg["Subject"] = self.subject
        msg["From"] = self.get_from_email()
        msg["To"] = ", ".join(self._normalize_recipients(self.to))

        if self.cc:
            msg["Cc"] = ", ".join(self._normalize_recipients(self.cc))

        if self.reply_to:
            msg["Reply-To"] = ", ".join(self._normalize_recipients(self.reply_to))

        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()

        # Add custom headers
        for key, value in self.extra_headers.items():
            msg[key] = value

        # Set content
        msg.set_content(self.body)

        # Add HTML alternative if present
        if self.html:
            msg.add_alternative(self.html, subtype="html")

        # Add attachments
        for attachment in self.attachments:
            self._attach_file(msg, attachment)

        return msg

    def _attach_file(self, msg: StdlibEmailMessage, attachment: EmailAttachment) -> None:
        """Attach a file to the message."""
        if attachment.mimetype:
            maintype, subtype = attachment.mimetype.split("/", 1)
        else:
            # Unknown type, use application/octet-stream
            maintype, subtype = "application", "octet-stream"

        msg.add_attachment(
            attachment.content,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.filename,
        )

    async def send(self, fail_silently: bool = False) -> int:
        """
        Send this email message.

        Args:
            fail_silently: If True, suppress exceptions

        Returns:
            Number of messages sent (0 or 1)
        """
        from nitro.mail import get_connection

        async with get_connection() as connection:
            return await connection.send_messages([self], fail_silently=fail_silently)
