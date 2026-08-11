import logging
from typing import TYPE_CHECKING, ClassVar

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

from nitro.mail.backends.base import BaseEmailBackend

if TYPE_CHECKING:
    from nitro.mail.message import EmailMessage

__all__ = ["SendGridBackend", "SendGridError"]

logger = logging.getLogger(__name__)


class SendGridError(RuntimeError):
    """SendGrid refused a message.

    Its own type rather than a bare `Exception`, so a caller can catch this
    without also catching every other way sending can go wrong.
    """


class SendGridBackend(BaseEmailBackend):
    """
    Email backend using SendGrid API.

    Requires: pip install httpx

    Example configuration:
        EMAIL_BACKEND = 'nitro.mail.backends.sendgrid.SendGridBackend'
        EMAIL_SENDGRID_API_KEY = 'your-sendgrid-api-key'
        EMAIL_SENDGRID_SANDBOX_MODE = False  # Optional
    """

    settings_map: ClassVar[dict[str, str]] = {
        "api_key": "EMAIL_SENDGRID_API_KEY",
        "sandbox_mode": "EMAIL_SENDGRID_SANDBOX_MODE",
        "timeout": "EMAIL_TIMEOUT",
    }

    def __init__(
        self,
        api_key: str | None = None,
        sandbox_mode: bool = False,
        timeout: int | None = None,
        **kwargs,
    ) -> None:
        if httpx is None:
            raise ImportError(
                "SendGridBackend requires httpx package. "
                "Install it with: pip install httpx"
            )

        super().__init__(**kwargs)

        self.api_key = api_key
        self.sandbox_mode = sandbox_mode
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def open(self) -> bool:
        """
        Initialize HTTP client.
        """
        if self._client is not None:
            return False

        if not self.api_key:
            raise ValueError(
                "SendGrid API key not configured. Set EMAIL_SENDGRID_API_KEY "
                "in settings."
            )

        self._client = httpx.AsyncClient(
            base_url="https://api.sendgrid.com/v3",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout or 30,
        )

        return True

    async def close(self) -> None:
        """
        Close HTTP client.
        """
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send_messages(
        self,
        email_messages: list["EmailMessage"],
        fail_silently: bool | None = None,
    ) -> int:
        """
        Send one or more EmailMessage objects via SendGrid.
        """
        if not email_messages:
            return 0

        silence = self._silence(fail_silently)

        # Broad, but only ever reached when a caller asked for silence; every
        # one of these re-raises otherwise.
        try:
            await self.open()
        except Exception:
            if not silence:
                raise
            logger.exception("the SendGrid client could not be built")
            return 0

        sent = 0
        for message in email_messages:
            try:
                await self._send(message)
                sent += 1
            except Exception:
                if not silence:
                    raise
                logger.exception("a message could not be sent via SendGrid")

        return sent

    def _build_sendgrid_payload(self, email_message: "EmailMessage") -> dict:
        """
        Build SendGrid API payload from EmailMessage.
        """
        from_addr = email_message.get_from_email()

        # Parse from address
        if "<" in from_addr and ">" in from_addr:
            # Format: "Name <email@example.com>"
            name, email = from_addr.split("<")
            name = name.strip().strip('"')
            email = email.strip(">")
            from_email = {"email": email, "name": name}
        else:
            from_email = {"email": from_addr}

        # Build personalizations (recipients)
        personalizations = []

        if email_message.to:
            to_list = []
            for recipient in email_message.to:
                if isinstance(recipient, str):
                    to_list.append({"email": recipient})
                else:
                    to_list.append({"email": str(recipient)})

            personalization = {"to": to_list}

            # Add CC if present
            if email_message.cc:
                cc_list = []
                for recipient in email_message.cc:
                    if isinstance(recipient, str):
                        cc_list.append({"email": recipient})
                    else:
                        cc_list.append({"email": str(recipient)})
                personalization["cc"] = cc_list

            # Add BCC if present
            if email_message.bcc:
                bcc_list = []
                for recipient in email_message.bcc:
                    if isinstance(recipient, str):
                        bcc_list.append({"email": recipient})
                    else:
                        bcc_list.append({"email": str(recipient)})
                personalization["bcc"] = bcc_list

            personalizations.append(personalization)

        # Build content
        content = []

        # Add plain text content
        if email_message.body:
            content.append(
                {
                    "type": "text/plain",
                    "value": email_message.body,
                }
            )

        # Add HTML content
        if email_message.html:
            content.append(
                {
                    "type": "text/html",
                    "value": email_message.html,
                }
            )

        # Build main payload
        payload = {
            "personalizations": personalizations,
            "from": from_email,
            "subject": email_message.subject,
            "content": content,
        }

        # Add reply-to if present
        if email_message.reply_to:
            reply_to_addr = email_message.reply_to[0]
            if isinstance(reply_to_addr, str):
                payload["reply_to"] = {"email": reply_to_addr}
            else:
                payload["reply_to"] = {"email": str(reply_to_addr)}

        # Add attachments if present
        if email_message.attachments:
            import base64

            attachments = []
            for attachment in email_message.attachments:
                encoded_content = base64.b64encode(attachment.content).decode()

                att_data = {
                    "content": encoded_content,
                    "filename": attachment.filename,
                }

                if attachment.mimetype:
                    att_data["type"] = attachment.mimetype

                attachments.append(att_data)

            payload["attachments"] = attachments

        # Add custom headers if present
        if email_message.extra_headers:
            payload["headers"] = email_message.extra_headers

        # Add sandbox mode if enabled
        if self.sandbox_mode:
            payload["mail_settings"] = {"sandbox_mode": {"enable": True}}

        return payload

    async def _send(self, email_message: "EmailMessage") -> None:
        """
        Send a single email message via SendGrid.
        """
        if self._client is None:
            raise ConnectionError("No SendGrid client available")

        payload = self._build_sendgrid_payload(email_message)

        response = await self._client.post("/mail/send", json=payload)

        # SendGrid returns 202 Accepted on success
        if response.status_code not in (200, 202):
            error_msg = f"SendGrid API error: {response.status_code}"
            try:
                error_data = response.json()
                if "errors" in error_data:
                    error_msg += f" - {error_data['errors']}"
            except ValueError:
                # Not JSON, which happens for a gateway error page.
                error_msg += f" - {response.text}"

            raise SendGridError(error_msg)

        logger.debug("a message was accepted by SendGrid with %s", response.status_code)
