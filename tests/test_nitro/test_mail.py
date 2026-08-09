import base64
import io
import sys
from email.utils import formataddr
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nitro.mail.backends.base import BaseEmailBackend
from nitro.mail.backends.console import ConsoleBackend
from nitro.mail.backends.oauth_smtp import OAuth2SMTPBackend
from nitro.mail.backends.sendgrid import SendGridBackend
from nitro.mail.backends.smtp import SMTPBackend
from nitro.mail.message import (
    EmailAttachment,
    EmailHeader,
    EmailMessage,
    EmailRecipient,
)
from nitro.mail.utils import get_connection, send_email, send_mass_email

# ---------------------------------------------------------------------------
# Settings stub
# ---------------------------------------------------------------------------


class _MockSettings:
    """Minimal settings stub.

    Explicit class attributes cover the most common settings.  Any attribute
    not defined here returns None via __getattr__, which causes get_connection()
    to filter it out of the params dict (all None values are dropped there).
    """

    EMAIL_BACKEND = "nitro.mail.backends.console.ConsoleBackend"
    EMAIL_HOST = "localhost"
    EMAIL_PORT = None
    EMAIL_HOST_USER = ""
    EMAIL_HOST_PASSWORD = ""
    EMAIL_USE_TLS = False
    EMAIL_USE_SSL = False
    EMAIL_TIMEOUT = None
    DEFAULT_FROM_EMAIL = "noreply@example.com"

    def __getattr__(self, name: str):
        return None


_SETTINGS = _MockSettings()


# ---------------------------------------------------------------------------
# EmailRecipient
# ---------------------------------------------------------------------------


class TestEmailRecipient:
    def test_str_email_only(self):
        r = EmailRecipient("user@example.com")
        assert str(r) == "user@example.com"

    def test_str_with_name(self):
        r = EmailRecipient("user@example.com", "John Doe")
        assert str(r) == formataddr(("John Doe", "user@example.com"))

    def test_name_defaults_to_none(self):
        r = EmailRecipient("user@example.com")
        assert r.name is None

    def test_fields_accessible(self):
        r = EmailRecipient("a@b.com", "Alice")
        assert r.email == "a@b.com"
        assert r.name == "Alice"


# ---------------------------------------------------------------------------
# EmailHeader
# ---------------------------------------------------------------------------


class TestEmailHeader:
    def test_stores_name_and_value(self):
        h = EmailHeader("X-Priority", "1")
        assert h.name == "X-Priority"
        assert h.value == "1"


# ---------------------------------------------------------------------------
# EmailAttachment
# ---------------------------------------------------------------------------


class TestEmailAttachment:
    def test_constructor_stores_fields(self):
        att = EmailAttachment("file.txt", b"hello", "text/plain")
        assert att.filename == "file.txt"
        assert att.content == b"hello"
        assert att.mimetype == "text/plain"

    def test_mimetype_defaults_to_none(self):
        att = EmailAttachment("file.bin", b"data")
        assert att.mimetype is None

    def test_from_file_reads_content(self, tmp_path):
        p = tmp_path / "report.pdf"
        p.write_bytes(b"%PDF-1.4 content")
        att = EmailAttachment.from_file(p)
        assert att.filename == "report.pdf"
        assert att.content == b"%PDF-1.4 content"

    def test_from_file_detects_pdf_mimetype(self, tmp_path):
        p = tmp_path / "doc.pdf"
        p.write_bytes(b"%PDF")
        att = EmailAttachment.from_file(p)
        assert att.mimetype == "application/pdf"

    def test_from_file_detects_json_mimetype(self, tmp_path):
        p = tmp_path / "data.json"
        p.write_bytes(b"{}")
        att = EmailAttachment.from_file(str(p))  # string path
        assert att.filename == "data.json"
        assert att.mimetype == "application/json"

    def test_from_file_missing_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            EmailAttachment.from_file(tmp_path / "nonexistent.pdf")

    def test_from_file_unknown_extension_yields_none_mimetype(self, tmp_path):
        p = tmp_path / "archive.xyzunknown"
        p.write_bytes(b"raw data")
        att = EmailAttachment.from_file(p)
        assert att.mimetype is None


# ---------------------------------------------------------------------------
# EmailMessage
# ---------------------------------------------------------------------------


class TestEmailMessageConstruction:
    def test_all_defaults(self):
        msg = EmailMessage()
        assert msg.subject == ""
        assert msg.body == ""
        assert msg.from_email is None
        assert msg.to == []
        assert msg.cc == []
        assert msg.bcc == []
        assert msg.reply_to == []
        assert msg.attachments == []
        assert msg.html is None
        assert msg.extra_headers == {}

    def test_extra_headers_via_dict(self):
        msg = EmailMessage(headers={"X-Custom": "value", "X-Batch": "yes"})
        assert msg.extra_headers == {"X-Custom": "value", "X-Batch": "yes"}

    def test_extra_headers_via_list_of_email_header(self):
        msg = EmailMessage(headers=[EmailHeader("X-A", "1"), EmailHeader("X-B", "2")])
        assert msg.extra_headers == {"X-A": "1", "X-B": "2"}

    def test_html_attribute_settable(self):
        msg = EmailMessage()
        msg.html = "<p>Hello</p>"
        assert msg.html == "<p>Hello</p>"

    def test_extra_headers_mutable(self):
        msg = EmailMessage()
        msg.extra_headers["X-Priority"] = "1"
        assert msg.extra_headers["X-Priority"] == "1"


class TestEmailMessageRecipients:
    def _msg(self, **kwargs) -> EmailMessage:
        defaults = {
            "subject": "Test",
            "body": "Body",
            "from_email": "sender@example.com",
            "to": ["to@example.com"],
        }
        defaults.update(kwargs)
        return EmailMessage(**defaults)

    def test_normalize_plain_string(self):
        msg = self._msg()
        assert msg._normalize_recipient("a@b.com") == "a@b.com"

    def test_normalize_email_recipient_with_name(self):
        msg = self._msg()
        r = EmailRecipient("a@b.com", "Alice")
        assert msg._normalize_recipient(r) == formataddr(("Alice", "a@b.com"))

    def test_normalize_email_recipient_without_name(self):
        msg = self._msg()
        r = EmailRecipient("a@b.com")
        assert msg._normalize_recipient(r) == "a@b.com"

    def test_normalize_tuple_format(self):
        # formataddr expects (realname, email), so the tuple format is (name, email)
        msg = self._msg()
        assert msg._normalize_recipient(("Alice", "a@b.com")) == formataddr(
            ("Alice", "a@b.com")
        )

    def test_recipients_combines_to_cc_bcc(self):
        msg = self._msg(
            to=["to@example.com"],
            cc=["cc@example.com"],
            bcc=["bcc@example.com"],
        )
        result = msg.recipients()
        assert "to@example.com" in result
        assert "cc@example.com" in result
        assert "bcc@example.com" in result
        assert len(result) == 3

    def test_recipients_excludes_reply_to(self):
        msg = self._msg(to=["to@example.com"], reply_to=["reply@example.com"])
        assert "reply@example.com" not in msg.recipients()

    def test_mixed_recipient_types_in_to(self):
        msg = self._msg(
            to=[
                "plain@example.com",
                EmailRecipient("named@example.com", "Named"),
                ("tuple@example.com", "Tuple"),
            ],
        )
        result = msg.recipients()
        assert len(result) == 3


class TestEmailMessageGetFromEmail:
    def test_uses_explicitly_set_from_email(self):
        msg = EmailMessage(from_email="me@example.com")
        assert msg.get_from_email() == "me@example.com"

    def test_falls_back_to_settings_default(self):
        msg = EmailMessage(subject="S", body="B", to=["x@x.com"])
        with patch("nitro.settings.settings", _SETTINGS):
            assert msg.get_from_email() == "noreply@example.com"


class TestEmailMessageAttach:
    def test_attach_appends_attachment(self):
        msg = EmailMessage(subject="S", body="B", from_email="a@a.com", to=["b@b.com"])
        att = EmailAttachment("f.txt", b"data", "text/plain")
        msg.attach(att)
        assert att in msg.attachments

    def test_attach_file(self, tmp_path):
        p = tmp_path / "note.txt"
        p.write_bytes(b"hello")
        msg = EmailMessage(subject="S", body="B", from_email="a@a.com", to=["b@b.com"])
        msg.attach_file(p)
        assert len(msg.attachments) == 1
        assert msg.attachments[0].filename == "note.txt"
        assert msg.attachments[0].content == b"hello"

    def test_multiple_attachments(self, tmp_path):
        msg = EmailMessage(subject="S", body="B", from_email="a@a.com", to=["b@b.com"])
        for i in range(3):
            msg.attach(
                EmailAttachment(f"file{i}.bin", bytes([i]), "application/octet-stream")
            )
        assert len(msg.attachments) == 3


class TestEmailMessageMime:
    def _msg(self, **kwargs) -> EmailMessage:
        defaults = {
            "subject": "Test Subject",
            "body": "Test body.",
            "from_email": "sender@example.com",
            "to": ["recipient@example.com"],
        }
        defaults.update(kwargs)
        return EmailMessage(**defaults)

    def test_subject_header(self):
        mime = self._msg(subject="My Subject").message()
        assert mime["Subject"] == "My Subject"

    def test_from_header(self):
        mime = self._msg(from_email="from@example.com").message()
        assert mime["From"] == "from@example.com"

    def test_to_header(self):
        mime = self._msg(to=["a@example.com", "b@example.com"]).message()
        assert "a@example.com" in mime["To"]
        assert "b@example.com" in mime["To"]

    def test_cc_header_present(self):
        mime = self._msg(cc=["cc@example.com"]).message()
        assert "cc@example.com" in mime["Cc"]

    def test_cc_header_absent_when_empty(self):
        mime = self._msg().message()
        assert mime["Cc"] is None

    def test_reply_to_header_present(self):
        mime = self._msg(reply_to=["reply@example.com"]).message()
        assert "reply@example.com" in mime["Reply-To"]

    def test_reply_to_absent_when_empty(self):
        mime = self._msg().message()
        assert mime["Reply-To"] is None

    def test_custom_extra_headers(self):
        msg = self._msg()
        msg.extra_headers["X-Priority"] = "1"
        msg.extra_headers["X-Batch-ID"] = "abc123"
        mime = msg.message()
        assert mime["X-Priority"] == "1"
        assert mime["X-Batch-ID"] == "abc123"

    def test_date_and_message_id_set(self):
        mime = self._msg().message()
        assert mime["Date"] is not None
        assert mime["Message-ID"] is not None

    def test_plain_body_in_content(self):
        mime = self._msg(body="Hello, world!").message()
        assert "Hello, world!" in mime.as_string()

    def test_html_added_as_alternative(self):
        msg = self._msg(body="Plain")
        msg.html = "<p>HTML</p>"
        content = msg.message().as_string()
        assert "<p>HTML</p>" in content
        assert "text/html" in content

    def test_no_html_alternative_when_html_is_none(self):
        msg = self._msg()
        content = msg.message().as_string()
        assert "text/html" not in content

    def test_attachment_present_in_mime(self):
        msg = self._msg()
        msg.attach(EmailAttachment("report.pdf", b"PDF", "application/pdf"))
        content = msg.message().as_string()
        assert "report.pdf" in content

    def test_attachment_without_mimetype_uses_octet_stream(self):
        msg = self._msg()
        msg.attach(EmailAttachment("file.bin", b"\x00\x01\x02"))
        content = msg.message().as_string()
        assert "application/octet-stream" in content


class TestEmailMessageSend:
    pytestmark = pytest.mark.asyncio

    def _conn_mock(self, return_value=1):
        mock = AsyncMock()
        mock.send_messages = AsyncMock(return_value=return_value)
        mock.__aenter__ = AsyncMock(return_value=mock)
        mock.__aexit__ = AsyncMock(return_value=False)
        return mock

    async def test_send_calls_send_messages_with_self(self):
        msg = EmailMessage(
            subject="Hi",
            body="Body",
            from_email="a@a.com",
            to=["b@b.com"],
        )
        conn = self._conn_mock(return_value=1)

        with patch("nitro.mail.get_connection", return_value=conn):
            result = await msg.send()

        conn.send_messages.assert_awaited_once_with([msg], fail_silently=False)
        assert result == 1

    async def test_send_passes_fail_silently(self):
        msg = EmailMessage(
            subject="Hi",
            body="Body",
            from_email="a@a.com",
            to=["b@b.com"],
        )
        conn = self._conn_mock()

        with patch("nitro.mail.get_connection", return_value=conn):
            await msg.send(fail_silently=True)

        conn.send_messages.assert_awaited_once_with([msg], fail_silently=True)


# ---------------------------------------------------------------------------
# BaseEmailBackend
# ---------------------------------------------------------------------------


class TestBaseEmailBackend:
    def test_cannot_instantiate_abstract_class(self):
        with pytest.raises(TypeError):
            BaseEmailBackend()

    def test_concrete_subclass_stores_init_params(self):
        class Concrete(BaseEmailBackend):
            async def open(self):
                return True

            async def close(self):
                pass

            async def send_messages(self, messages, fail_silently=False):
                return 0

        b = Concrete(
            host="smtp.example.com",
            port=587,
            username="user",
            password="pass",
            use_tls=True,
            use_ssl=False,
            timeout=30,
        )
        assert b.host == "smtp.example.com"
        assert b.port == 587
        assert b.username == "user"
        assert b.password == "pass"
        assert b.use_tls is True
        assert b.use_ssl is False
        assert b.timeout == 30

    def test_extra_kwargs_stored_in_options(self):
        class Concrete(BaseEmailBackend):
            async def open(self):
                return True

            async def close(self):
                pass

            async def send_messages(self, messages, fail_silently=False):
                return 0

        b = Concrete(host="h", custom_param="value")
        assert b.options.get("custom_param") == "value"

    @pytest.mark.asyncio
    async def test_context_manager_calls_open_and_close(self):
        class Concrete(BaseEmailBackend):
            async def open(self):
                return True

            async def close(self):
                pass

            async def send_messages(self, messages, fail_silently=False):
                return 0

        b = Concrete()
        b.open = AsyncMock(return_value=True)
        b.close = AsyncMock()

        async with b as entered:
            assert entered is b

        b.open.assert_awaited_once()
        b.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# ConsoleBackend
# ---------------------------------------------------------------------------


class TestConsoleBackend:
    pytestmark = pytest.mark.asyncio

    async def test_open_returns_true(self):
        assert await ConsoleBackend().open() is True

    async def test_close_does_not_raise(self):
        await ConsoleBackend().close()

    async def test_uses_stdout_by_default(self):
        assert ConsoleBackend().stream is sys.stdout

    async def test_send_empty_list_returns_zero(self):
        result = await ConsoleBackend().send_messages([])
        assert result == 0

    async def test_send_writes_separator_and_content_to_stream(self):
        stream = io.StringIO()
        backend = ConsoleBackend(stream=stream)
        msg = EmailMessage(
            subject="Hello",
            body="World",
            from_email="a@example.com",
            to=["b@example.com"],
        )

        result = await backend.send_messages([msg])

        output = stream.getvalue()
        assert result == 1
        assert "Hello" in output
        assert "World" in output
        assert "-" * 79 in output

    async def test_send_counts_multiple_messages(self):
        stream = io.StringIO()
        backend = ConsoleBackend(stream=stream)
        messages = [
            EmailMessage(
                subject=f"Msg {i}", body="b", from_email="a@a.com", to=["b@b.com"]
            )
            for i in range(3)
        ]
        assert await backend.send_messages(messages) == 3

    async def test_context_manager_sends_and_closes(self):
        stream = io.StringIO()
        msg = EmailMessage(
            subject="Hi", body="body", from_email="a@a.com", to=["b@b.com"]
        )

        async with ConsoleBackend(stream=stream) as backend:
            result = await backend.send_messages([msg])

        assert result == 1


# ---------------------------------------------------------------------------
# SMTPBackend
# ---------------------------------------------------------------------------


class TestSMTPBackend:
    def test_default_port_plain(self):
        assert SMTPBackend(host="localhost").port == 25

    def test_default_port_tls(self):
        assert SMTPBackend(host="localhost", use_tls=True).port == 587

    def test_default_port_ssl(self):
        assert SMTPBackend(host="localhost", use_ssl=True).port == 465

    def test_explicit_port_is_not_overridden(self):
        assert SMTPBackend(host="localhost", port=2525, use_tls=True).port == 2525

    @pytest.mark.asyncio
    async def test_send_empty_list_returns_zero(self):
        assert await SMTPBackend(host="localhost").send_messages([]) == 0

    @pytest.mark.asyncio
    async def test_open_returns_false_when_already_connected(self):
        backend = SMTPBackend(host="localhost")
        backend._connection = MagicMock()  # simulate existing connection
        assert await backend.open() is False

    @pytest.mark.asyncio
    async def test_send_messages_plain_success(self):
        backend = SMTPBackend(host="smtp.example.com")
        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock()
        mock_conn.send_message = AsyncMock()
        mock_conn.quit = AsyncMock()

        msg = EmailMessage(
            subject="Test",
            body="Body",
            from_email="a@example.com",
            to=["b@example.com"],
        )

        with patch("nitro.mail.backends.smtp.SMTP", return_value=mock_conn):
            result = await backend.send_messages([msg])

        assert result == 1
        mock_conn.connect.assert_awaited_once()
        mock_conn.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_messages_with_tls_and_auth(self):
        backend = SMTPBackend(
            host="smtp.example.com",
            username="user@example.com",
            password="secret",
            use_tls=True,
        )
        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock()
        mock_conn.starttls = AsyncMock()
        mock_conn.login = AsyncMock()
        mock_conn.send_message = AsyncMock()
        mock_conn.quit = AsyncMock()

        msg = EmailMessage(
            subject="Test",
            body="Body",
            from_email="a@example.com",
            to=["b@example.com"],
        )

        with patch("nitro.mail.backends.smtp.SMTP", return_value=mock_conn):
            result = await backend.send_messages([msg])

        assert result == 1
        mock_conn.starttls.assert_awaited_once()
        mock_conn.login.assert_awaited_once_with("user@example.com", "secret")

    @pytest.mark.asyncio
    async def test_send_messages_connection_failure_raises(self):
        from aiosmtplib import SMTPException

        backend = SMTPBackend(host="smtp.example.com")
        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock(side_effect=SMTPException("refused"))

        msg = EmailMessage(
            subject="T",
            body="B",
            from_email="a@a.com",
            to=["b@b.com"],
        )

        with patch("nitro.mail.backends.smtp.SMTP", return_value=mock_conn):
            with pytest.raises(SMTPException):
                await backend.send_messages([msg])

    @pytest.mark.asyncio
    async def test_send_messages_connection_failure_silenced(self):
        from aiosmtplib import SMTPException

        backend = SMTPBackend(host="smtp.example.com")
        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock(side_effect=SMTPException("refused"))

        msg = EmailMessage(subject="T", body="B", from_email="a@a.com", to=["b@b.com"])

        with patch("nitro.mail.backends.smtp.SMTP", return_value=mock_conn):
            result = await backend.send_messages([msg], fail_silently=True)

        assert result == 0

    @pytest.mark.asyncio
    async def test_send_messages_per_message_failure_silenced(self):
        backend = SMTPBackend(host="smtp.example.com")
        backend._connection = MagicMock()
        backend._connection.send_message = AsyncMock(side_effect=Exception("refused"))

        messages = [
            EmailMessage(
                subject=f"M{i}", body="B", from_email="a@a.com", to=["b@b.com"]
            )
            for i in range(2)
        ]

        with patch.object(backend, "open", AsyncMock(return_value=False)):
            result = await backend.send_messages(messages, fail_silently=True)

        assert result == 0

    @pytest.mark.asyncio
    async def test_close_quits_and_clears_connection(self):
        backend = SMTPBackend(host="smtp.example.com")
        mock_conn = MagicMock()
        mock_conn.quit = AsyncMock()
        backend._connection = mock_conn

        await backend.close()

        mock_conn.quit.assert_awaited_once()
        assert backend._connection is None


# ---------------------------------------------------------------------------
# OAuth2SMTPBackend
# ---------------------------------------------------------------------------


class TestOAuth2SMTPBackend:
    def test_default_port_is_587(self):
        assert OAuth2SMTPBackend(host="h", oauth2_token="tok").port == 587

    def test_use_tls_forced_when_neither_tls_nor_ssl(self):
        backend = OAuth2SMTPBackend(host="h", oauth2_token="tok")
        assert backend.use_tls is True

    def test_explicit_port_respected(self):
        backend = OAuth2SMTPBackend(host="h", port=465, oauth2_token="tok")
        assert backend.port == 465

    def test_build_oauth_string_format(self):
        backend = OAuth2SMTPBackend(host="h", oauth2_token="tok")
        result = backend._build_oauth_string("user@example.com", "my-token")
        expected = base64.b64encode(
            "user=user@example.com\x01auth=Bearer my-token\x01\x01".encode()
        ).decode()
        assert result == expected

    @pytest.mark.asyncio
    async def test_get_token_from_direct_value(self):
        backend = OAuth2SMTPBackend(host="h", oauth2_token="direct-token")
        assert await backend._get_oauth_token() == "direct-token"

    @pytest.mark.asyncio
    async def test_get_token_from_sync_callback(self):
        mock_module = MagicMock()
        mock_module.get_token = lambda: "sync-token"

        with patch.dict("sys.modules", {"myapp.auth": mock_module}):
            backend = OAuth2SMTPBackend(
                host="h",
                oauth2_token_callback="myapp.auth.get_token",
            )
            token = await backend._get_oauth_token()

        assert token == "sync-token"

    @pytest.mark.asyncio
    async def test_get_token_from_async_callback(self):
        async def async_get_token():
            return "async-token"

        mock_module = MagicMock()
        mock_module.get_token = async_get_token

        with patch.dict("sys.modules", {"myapp.auth": mock_module}):
            backend = OAuth2SMTPBackend(
                host="h",
                oauth2_token_callback="myapp.auth.get_token",
            )
            token = await backend._get_oauth_token()

        assert token == "async-token"

    @pytest.mark.asyncio
    async def test_get_token_raises_when_unconfigured(self):
        backend = OAuth2SMTPBackend(host="h")
        with pytest.raises(ValueError, match="OAuth2 token not configured"):
            await backend._get_oauth_token()

    @pytest.mark.asyncio
    async def test_send_empty_list_returns_zero(self):
        backend = OAuth2SMTPBackend(host="h", oauth2_token="tok")
        assert await backend.send_messages([]) == 0

    @pytest.mark.asyncio
    async def test_close_quits_and_clears_connection(self):
        backend = OAuth2SMTPBackend(host="h", oauth2_token="tok")
        mock_conn = MagicMock()
        mock_conn.quit = AsyncMock()
        backend._connection = mock_conn

        await backend.close()

        mock_conn.quit.assert_awaited_once()
        assert backend._connection is None


# ---------------------------------------------------------------------------
# SendGridBackend
# ---------------------------------------------------------------------------


class TestSendGridBackend:
    @pytest.fixture(autouse=True)
    def _httpx_mock(self):
        """Patch httpx at the module level so SendGridBackend.__init__ doesn't raise ImportError."""
        mock_httpx = MagicMock()
        with patch("nitro.mail.backends.sendgrid.httpx", mock_httpx):
            yield mock_httpx

    def _msg(self, **kwargs) -> EmailMessage:
        defaults = {
            "subject": "Hello",
            "body": "Plain text.",
            "from_email": "from@example.com",
            "to": ["to@example.com"],
        }
        defaults.update(kwargs)
        return EmailMessage(**defaults)

    @pytest.mark.asyncio
    async def test_open_raises_without_api_key(self):
        with pytest.raises(ValueError, match="SendGrid API key not configured"):
            await SendGridBackend().open()

    @pytest.mark.asyncio
    async def test_send_empty_list_returns_zero(self):
        assert await SendGridBackend(api_key="key").send_messages([]) == 0

    def test_payload_basic_fields(self):
        backend = SendGridBackend(api_key="key")
        payload = backend._build_sendgrid_payload(self._msg())

        assert payload["subject"] == "Hello"
        assert payload["from"]["email"] == "from@example.com"
        assert payload["personalizations"][0]["to"][0]["email"] == "to@example.com"
        assert payload["content"][0]["type"] == "text/plain"
        assert payload["content"][0]["value"] == "Plain text."

    def test_payload_html_content_block(self):
        backend = SendGridBackend(api_key="key")
        msg = self._msg()
        msg.html = "<p>Hi</p>"
        payload = backend._build_sendgrid_payload(msg)

        content_types = [c["type"] for c in payload["content"]]
        assert "text/html" in content_types
        html_block = next(c for c in payload["content"] if c["type"] == "text/html")
        assert html_block["value"] == "<p>Hi</p>"

    def test_payload_cc_and_bcc(self):
        backend = SendGridBackend(api_key="key")
        payload = backend._build_sendgrid_payload(
            self._msg(cc=["cc@example.com"], bcc=["bcc@example.com"])
        )
        p = payload["personalizations"][0]
        assert p["cc"][0]["email"] == "cc@example.com"
        assert p["bcc"][0]["email"] == "bcc@example.com"

    def test_payload_reply_to(self):
        backend = SendGridBackend(api_key="key")
        payload = backend._build_sendgrid_payload(
            self._msg(reply_to=["reply@example.com"])
        )
        assert payload["reply_to"]["email"] == "reply@example.com"

    def test_payload_attachment_base64_encoded(self):
        backend = SendGridBackend(api_key="key")
        msg = self._msg()
        msg.attach(EmailAttachment("doc.pdf", b"PDF bytes", "application/pdf"))
        payload = backend._build_sendgrid_payload(msg)

        att = payload["attachments"][0]
        assert att["filename"] == "doc.pdf"
        assert att["type"] == "application/pdf"
        assert att["content"] == base64.b64encode(b"PDF bytes").decode()

    def test_payload_custom_headers(self):
        backend = SendGridBackend(api_key="key")
        msg = self._msg()
        msg.extra_headers["X-Custom"] = "header-value"
        payload = backend._build_sendgrid_payload(msg)
        assert payload["headers"]["X-Custom"] == "header-value"

    def test_payload_named_from_address(self):
        backend = SendGridBackend(api_key="key")
        msg = self._msg(from_email="JALDIS <noreply@jaldis.com>")
        payload = backend._build_sendgrid_payload(msg)
        assert payload["from"]["email"].strip() == "noreply@jaldis.com"
        assert payload["from"]["name"] == "JALDIS"

    def test_payload_sandbox_mode_enabled(self):
        backend = SendGridBackend(api_key="key", sandbox_mode=True)
        payload = backend._build_sendgrid_payload(self._msg())
        assert payload["mail_settings"]["sandbox_mode"]["enable"] is True

    def test_payload_no_mail_settings_when_sandbox_off(self):
        backend = SendGridBackend(api_key="key", sandbox_mode=False)
        payload = backend._build_sendgrid_payload(self._msg())
        assert "mail_settings" not in payload

    @pytest.mark.asyncio
    async def test_send_messages_success_202(self):
        backend = SendGridBackend(api_key="test-key")
        msg = self._msg()

        mock_response = MagicMock()
        mock_response.status_code = 202

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch(
            "nitro.mail.backends.sendgrid.httpx.AsyncClient", return_value=mock_client
        ):
            result = await backend.send_messages([msg])

        assert result == 1
        mock_client.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_raises_on_non_202(self):
        backend = SendGridBackend(api_key="test-key")
        msg = self._msg()

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.json.return_value = {"errors": [{"message": "Bad request"}]}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()
        backend._client = mock_client

        with pytest.raises(Exception, match="SendGrid API error: 400"):
            await backend._send(msg)

    @pytest.mark.asyncio
    async def test_send_fail_silently_on_error(self):
        backend = SendGridBackend(api_key="test-key")
        msg = self._msg()

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.json.side_effect = Exception("not json")
        mock_response.text = "Server Error"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch(
            "nitro.mail.backends.sendgrid.httpx.AsyncClient", return_value=mock_client
        ):
            result = await backend.send_messages([msg], fail_silently=True)

        assert result == 0

    @pytest.mark.asyncio
    async def test_close_disposes_client(self):
        backend = SendGridBackend(api_key="test-key")
        mock_client = AsyncMock()
        mock_client.aclose = AsyncMock()
        backend._client = mock_client

        await backend.close()

        mock_client.aclose.assert_awaited_once()
        assert backend._client is None

    @pytest.mark.asyncio
    async def test_open_idempotent(self):
        backend = SendGridBackend(api_key="test-key")
        mock_client = AsyncMock()
        mock_client.aclose = AsyncMock()
        backend._client = mock_client  # already open

        result = await backend.open()

        assert result is False


# ---------------------------------------------------------------------------
# get_connection()
# ---------------------------------------------------------------------------


class TestGetConnection:
    def test_returns_console_backend_from_string_path(self):
        with patch("nitro.settings.settings", _SETTINGS):
            conn = get_connection(backend="nitro.mail.backends.console.ConsoleBackend")
        assert isinstance(conn, ConsoleBackend)

    def test_uses_email_backend_from_settings_when_not_specified(self):
        settings = _MockSettings()
        settings.EMAIL_BACKEND = "nitro.mail.backends.console.ConsoleBackend"
        with patch("nitro.settings.settings", settings):
            conn = get_connection()
        assert isinstance(conn, ConsoleBackend)

    def test_explicit_kwarg_overrides_settings_value(self):
        with patch("nitro.settings.settings", _SETTINGS):
            conn = get_connection(
                backend="nitro.mail.backends.console.ConsoleBackend",
                host="custom.host",
            )
        assert conn.host == "custom.host"

    def test_smtp_backend_receives_host_port_credentials(self):
        settings = _MockSettings()
        settings.EMAIL_HOST = "smtp.example.com"
        settings.EMAIL_PORT = 587
        settings.EMAIL_USE_TLS = True
        settings.EMAIL_HOST_USER = "user@example.com"
        settings.EMAIL_HOST_PASSWORD = "secret"

        with patch("nitro.settings.settings", settings):
            conn = get_connection(backend="nitro.mail.backends.smtp.SMTPBackend")

        assert isinstance(conn, SMTPBackend)
        assert conn.host == "smtp.example.com"
        assert conn.port == 587
        assert conn.use_tls is True
        assert conn.username == "user@example.com"
        assert conn.password == "secret"

    def test_sendgrid_backend_receives_api_key_and_sandbox_mode(self):
        settings = _MockSettings()
        settings.EMAIL_SENDGRID_API_KEY = "SG.test-key"
        settings.EMAIL_SENDGRID_SANDBOX_MODE = True

        with (
            patch("nitro.settings.settings", settings),
            patch("nitro.mail.backends.sendgrid.httpx", MagicMock()),
        ):
            conn = get_connection(
                backend="nitro.mail.backends.sendgrid.SendGridBackend",
            )

        assert isinstance(conn, SendGridBackend)
        assert conn.api_key == "SG.test-key"
        assert conn.sandbox_mode is True

    def test_oauth_backend_receives_token(self):
        settings = _MockSettings()
        settings.EMAIL_OAUTH2_TOKEN = "my-oauth-token"
        settings.EMAIL_HOST = "smtp.office365.com"
        settings.EMAIL_USE_TLS = True

        with patch("nitro.settings.settings", settings):
            conn = get_connection(
                backend="nitro.mail.backends.oauth_smtp.OAuth2SMTPBackend",
            )

        assert isinstance(conn, OAuth2SMTPBackend)
        assert conn.oauth2_token == "my-oauth-token"

    def test_oauth_backend_receives_token_callback(self):
        settings = _MockSettings()
        settings.EMAIL_OAUTH2_TOKEN_CALLBACK = "myapp.auth.get_token"
        settings.EMAIL_HOST = "smtp.office365.com"

        with patch("nitro.settings.settings", settings):
            conn = get_connection(
                backend="nitro.mail.backends.oauth_smtp.OAuth2SMTPBackend",
            )

        assert conn.oauth2_token_callback == "myapp.auth.get_token"

    def test_ses_backend_receives_region_and_credentials(self):
        settings = _MockSettings()
        settings.EMAIL_AWS_REGION = "eu-central-1"
        settings.EMAIL_AWS_ACCESS_KEY_ID = "AKID"
        settings.EMAIL_AWS_SECRET_ACCESS_KEY = "SECRET"

        mock_ses_class = MagicMock()

        with (
            patch("nitro.settings.settings", settings),
            patch("nitro.mail.backends.ses.aioboto3", MagicMock()),
            patch("nitro.mail.backends.ses.SESBackend", mock_ses_class),
        ):
            get_connection(backend="nitro.mail.backends.ses.SESBackend")

        kwargs = mock_ses_class.call_args[1]
        assert kwargs["region_name"] == "eu-central-1"
        assert kwargs["aws_access_key_id"] == "AKID"
        assert kwargs["aws_secret_access_key"] == "SECRET"


# ---------------------------------------------------------------------------
# send_email()
# ---------------------------------------------------------------------------


class TestSendEmail:
    pytestmark = pytest.mark.asyncio

    def _conn_mock(self, return_value=1):
        mock = AsyncMock()
        mock.send_messages = AsyncMock(return_value=return_value)
        mock.__aenter__ = AsyncMock(return_value=mock)
        mock.__aexit__ = AsyncMock(return_value=False)
        return mock

    async def test_sends_plain_email_and_returns_count(self):
        conn = self._conn_mock(return_value=1)

        with (
            patch("nitro.mail.get_connection", return_value=conn),
            patch("nitro.settings.settings", _SETTINGS),
        ):
            result = await send_email(
                subject="Hi",
                message="Hello!",
                from_email="a@example.com",
                recipient_list=["b@example.com"],
            )

        assert result == 1
        sent = conn.send_messages.call_args[0][0]
        assert sent[0].subject == "Hi"
        assert sent[0].body == "Hello!"
        assert sent[0].html is None

    async def test_sets_html_when_provided(self):
        conn = self._conn_mock()

        with (
            patch("nitro.mail.get_connection", return_value=conn),
            patch("nitro.settings.settings", _SETTINGS),
        ):
            await send_email(
                subject="Hi",
                message="Hello!",
                from_email="a@example.com",
                recipient_list=["b@example.com"],
                html_message="<p>Hello!</p>",
            )

        sent = conn.send_messages.call_args[0][0]
        assert sent[0].html == "<p>Hello!</p>"

    async def test_passes_fail_silently(self):
        conn = self._conn_mock(return_value=0)

        with (
            patch("nitro.mail.get_connection", return_value=conn),
            patch("nitro.settings.settings", _SETTINGS),
        ):
            result = await send_email(
                subject="Hi",
                message="Body",
                fail_silently=True,
            )

        assert result == 0
        conn.send_messages.assert_awaited_once_with(
            conn.send_messages.call_args[0][0],
            fail_silently=True,
        )

    async def test_empty_recipient_list_still_sends(self):
        conn = self._conn_mock(return_value=1)

        with (
            patch("nitro.mail.get_connection", return_value=conn),
            patch("nitro.settings.settings", _SETTINGS),
        ):
            await send_email(subject="Hi", message="Body", from_email="a@a.com")

        sent = conn.send_messages.call_args[0][0]
        assert sent[0].to == []


# ---------------------------------------------------------------------------
# send_mass_email()
# ---------------------------------------------------------------------------


class TestSendMassEmail:
    pytestmark = pytest.mark.asyncio

    def _conn_mock(self, return_value=0):
        mock = AsyncMock()
        mock.send_messages = AsyncMock(return_value=return_value)
        mock.__aenter__ = AsyncMock(return_value=mock)
        mock.__aexit__ = AsyncMock(return_value=False)
        return mock

    async def test_four_tuple_no_html(self):
        conn = self._conn_mock(return_value=1)

        with (
            patch("nitro.mail.utils.get_connection", return_value=conn),
            patch("nitro.settings.settings", _SETTINGS),
        ):
            await send_mass_email(
                [
                    ("Subject", "Body", "from@example.com", ["to@example.com"]),
                ]
            )

        sent = conn.send_messages.call_args[0][0]
        assert sent[0].html is None

    async def test_five_tuple_with_html(self):
        conn = self._conn_mock(return_value=1)

        with (
            patch("nitro.mail.utils.get_connection", return_value=conn),
            patch("nitro.settings.settings", _SETTINGS),
        ):
            await send_mass_email(
                [
                    (
                        "Subject",
                        "Body",
                        "from@example.com",
                        ["to@example.com"],
                        "<p>HTML</p>",
                    ),
                ]
            )

        sent = conn.send_messages.call_args[0][0]
        assert sent[0].html == "<p>HTML</p>"

    async def test_five_tuple_html_none_treated_as_no_html(self):
        conn = self._conn_mock(return_value=1)

        with (
            patch("nitro.mail.utils.get_connection", return_value=conn),
            patch("nitro.settings.settings", _SETTINGS),
        ):
            await send_mass_email(
                [
                    ("Subject", "Body", "from@example.com", ["to@example.com"], None),
                ]
            )

        sent = conn.send_messages.call_args[0][0]
        assert sent[0].html is None

    async def test_multiple_messages_batched_in_single_connection(self):
        conn = self._conn_mock(return_value=3)

        data = [
            (f"Subject {i}", f"Body {i}", "from@example.com", [f"to{i}@example.com"])
            for i in range(3)
        ]

        with (
            patch("nitro.mail.utils.get_connection", return_value=conn),
            patch("nitro.settings.settings", _SETTINGS),
        ):
            result = await send_mass_email(data)

        assert result == 3
        conn.send_messages.assert_awaited_once()  # one batch call
        sent = conn.send_messages.call_args[0][0]
        assert len(sent) == 3
        assert sent[0].subject == "Subject 0"
        assert sent[2].subject == "Subject 2"

    async def test_mixed_four_and_five_tuples(self):
        conn = self._conn_mock(return_value=2)

        data = [
            ("S1", "B1", "f@f.com", ["t@t.com"]),
            ("S2", "B2", "f@f.com", ["t@t.com"], "<b>HTML</b>"),
        ]

        with (
            patch("nitro.mail.utils.get_connection", return_value=conn),
            patch("nitro.settings.settings", _SETTINGS),
        ):
            await send_mass_email(data)

        sent = conn.send_messages.call_args[0][0]
        assert sent[0].html is None
        assert sent[1].html == "<b>HTML</b>"

    async def test_empty_datatuple_sends_zero(self):
        conn = self._conn_mock(return_value=0)

        with (
            patch("nitro.mail.utils.get_connection", return_value=conn),
            patch("nitro.settings.settings", _SETTINGS),
        ):
            result = await send_mass_email([])

        assert result == 0
        sent = conn.send_messages.call_args[0][0]
        assert sent == []
