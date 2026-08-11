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

"""SMTP authenticated with an OAuth2 bearer token.

What Microsoft 365 and Google Workspace want in place of a password. Only the
authentication step differs from ordinary SMTP, so that is all this changes.
"""

from __future__ import annotations

import base64
import importlib
import inspect
import logging
from typing import TYPE_CHECKING, ClassVar

from aiosmtplib import SMTP

from nitro.mail.backends.smtp import STARTTLS_PORT, SMTPBackend

if TYPE_CHECKING:
    pass

__all__ = ["OAuth2SMTPBackend"]

logger = logging.getLogger(__name__)


class OAuth2SMTPBackend(SMTPBackend):
    """Send through an SMTP server, authenticating with an OAuth2 token.

        EMAIL_BACKEND = "nitro.mail.backends.oauth_smtp.OAuth2SMTPBackend"
        EMAIL_HOST = "smtp.office365.com"
        EMAIL_HOST_USER = "you@company.com"
        EMAIL_OAUTH2_TOKEN = "..."

    A token expires, so a long-running process usually names a callback that
    fetches a current one instead:

        EMAIL_OAUTH2_TOKEN_CALLBACK = "myapp.auth.get_smtp_token"

    The callback may be synchronous or a coroutine function, and is called once
    per connection.
    """

    settings_map: ClassVar[dict[str, str]] = {
        **SMTPBackend.settings_map,
        "oauth2_token": "EMAIL_OAUTH2_TOKEN",
        "oauth2_token_callback": "EMAIL_OAUTH2_TOKEN_CALLBACK",
    }

    def __init__(
        self,
        oauth2_token: str | None = None,
        oauth2_token_callback: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.oauth2_token = oauth2_token
        self.oauth2_token_callback = oauth2_token_callback

        # OAuth2 is only offered over an encrypted connection, so a server that
        # was configured with neither gets STARTTLS rather than a login that
        # would be refused.
        if not self.use_tls and not self.use_ssl:
            self.use_tls = True
            if self.port == self.default_port():
                self.port = STARTTLS_PORT

    def default_port(self) -> int:
        return STARTTLS_PORT if not self.use_ssl else super().default_port()

    async def _get_oauth_token(self) -> str:
        """The token to authenticate with, configured or fetched."""
        if self.oauth2_token:
            return self.oauth2_token

        if self.oauth2_token_callback:
            module_path, function_name = self.oauth2_token_callback.rsplit(".", 1)
            module = importlib.import_module(module_path)
            callback = getattr(module, function_name)

            if inspect.iscoroutinefunction(callback):
                return await callback()
            return callback()

        raise ValueError(
            "no OAuth2 token is configured; set EMAIL_OAUTH2_TOKEN or EMAIL_OAUTH2_TOKEN_CALLBACK"
        )

    def _build_oauth_string(self, user: str, token: str) -> str:
        """The XOAUTH2 argument: ``base64(user=<user>^Aauth=Bearer <token>^A^A)``."""
        authentication = f"user={user}\x01auth=Bearer {token}\x01\x01"
        return base64.b64encode(authentication.encode()).decode()

    async def _authenticate(self, connection: SMTP) -> None:
        token = await self._get_oauth_token()
        await connection.execute_command(
            "AUTH",
            "XOAUTH2",
            self._build_oauth_string(self.username, token),
        )
