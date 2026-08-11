#
# This source file is part of the Nitro open source project.
#
# Copyright (c) 2026 Jaldis B.V.
#
# Licensed under the MIT OR Apache-2.0 license (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://opensource.org/licenses/MIT
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""SMTP, with and without authentication."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from aiosmtplib import SMTP, SMTPException

from nitro.mail.backends.base import BaseEmailBackend

if TYPE_CHECKING:
    from nitro.mail.message import EmailMessage

__all__ = ["SMTPBackend"]

logger = logging.getLogger(__name__)

#: The port each way of connecting uses when none is configured.
IMPLICIT_TLS_PORT = 465
STARTTLS_PORT = 587
PLAIN_PORT = 25


class SMTPBackend(BaseEmailBackend):
    """Send through an SMTP server.

        EMAIL_BACKEND = "nitro.mail.backends.smtp.SMTPBackend"
        EMAIL_HOST = "smtp.example.com"
        EMAIL_HOST_USER = "postmaster@example.com"
        EMAIL_HOST_PASSWORD = "..."
        EMAIL_USE_TLS = True

    The port follows from how TLS is configured unless one is given: 465 for a
    connection that is encrypted from the start, 587 for one that upgrades with
    STARTTLS, and 25 for one that does neither.
    """

    settings_map: ClassVar[dict[str, str]] = {
        "host": "EMAIL_HOST",
        "port": "EMAIL_PORT",
        "username": "EMAIL_HOST_USER",
        "password": "EMAIL_HOST_PASSWORD",
        "use_tls": "EMAIL_USE_TLS",
        "use_ssl": "EMAIL_USE_SSL",
        "timeout": "EMAIL_TIMEOUT",
    }

    def __init__(
        self,
        host: str = "localhost",
        port: int | None = None,
        username: str = "",
        password: str = "",
        use_tls: bool = False,
        use_ssl: bool = False,
        timeout: int | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.host = host
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.use_ssl = use_ssl
        self.timeout = timeout
        self.port = port if port is not None else self.default_port()
        self._connection: SMTP | None = None

    def default_port(self) -> int:
        if self.use_ssl:
            return IMPLICIT_TLS_PORT
        if self.use_tls:
            return STARTTLS_PORT
        return PLAIN_PORT

    async def open(self) -> bool:
        """Connect, upgrade and authenticate.

        A failure raises rather than being swallowed here: whether it should be
        fatal is the caller's decision, and `send_messages` makes it.
        """
        if self._connection is not None:
            return False

        connection = SMTP(
            hostname=self.host,
            port=self.port,
            timeout=self.timeout,
            use_tls=self.use_ssl,
        )
        await connection.connect()

        if self.use_tls and not self.use_ssl:
            await connection.starttls()

        self._connection = connection
        await self._authenticate(connection)
        return True

    async def _authenticate(self, connection: SMTP) -> None:
        """Log in, if there is anything to log in with."""
        if self.username and self.password:
            await connection.login(self.username, self.password)

    async def close(self) -> None:
        if self._connection is None:
            return
        try:
            await self._connection.quit()
        except SMTPException as error:
            # The connection is being discarded either way, so this cannot be
            # made to matter — but a server that refuses a QUIT is worth
            # knowing about rather than passing over in silence.
            logger.warning("the SMTP connection did not close cleanly: %s", error)
        finally:
            self._connection = None

    async def send_messages(
        self,
        email_messages: list[EmailMessage],
        fail_silently: bool | None = None,
    ) -> int:
        if not email_messages:
            return 0

        silence = self._silence(fail_silently)

        # Broad, but only ever reached when a caller has explicitly asked for
        # silence: without `fail_silently` every one of these re-raises, and
        # what a failing SMTP server raises is not worth enumerating when the
        # answer either way is "log it and carry on".
        try:
            opened_here = await self.open()
        except Exception:
            if not silence:
                raise
            logger.exception("could not connect to %s", self.host)
            return 0

        sent = 0
        try:
            for message in email_messages:
                try:
                    await self._send(message)
                    sent += 1
                except Exception:
                    if not silence:
                        raise
                    logger.exception("a message could not be sent")
        finally:
            if opened_here:
                await self.close()

        return sent

    async def _send(self, email_message: EmailMessage) -> None:
        if self._connection is None:
            raise ConnectionError("no SMTP connection is open")

        await self._connection.send_message(
            email_message.message(),
            sender=email_message.get_from_email(),
            recipients=email_message.recipients(),
        )
