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

import logging
from typing import TYPE_CHECKING, Any, ClassVar

try:
    from aiobotocore.session import get_session
except ImportError:
    get_session = None  # type: ignore

from nitro.mail.backends.base import BaseEmailBackend

if TYPE_CHECKING:
    from aiobotocore.session import AioSession

    from nitro.mail.message import EmailMessage


logger = logging.getLogger(__name__)


class SESBackend(BaseEmailBackend):
    """
    Email backend using the AWS Simple Email Service (SES) v2 API.

    Requires: pip install aiobotocore, and the `ses:SendEmail` IAM permission
    rather than the `ses:SendRawEmail` the v1 API needed.

    Example configuration:
        EMAIL_BACKEND = 'nitro.mail.backends.ses.SESBackend'
        EMAIL_AWS_REGION = 'us-east-1'
        EMAIL_AWS_ACCESS_KEY_ID = 'your-access-key'  # Optional
        EMAIL_AWS_SECRET_ACCESS_KEY = 'your-secret-key'  # Optional
        EMAIL_AWS_SESSION_TOKEN = 'your-session-token'  # Optional
        EMAIL_SES_CONFIGURATION_SET = 'my-config-set'  # Optional

    Note: If AWS credentials are not provided, boto3 will use the
    default credential chain (environment variables, ~/.aws/config, IAM role, etc.)
    """

    settings_map: ClassVar[dict[str, str]] = {
        "region_name": "EMAIL_AWS_REGION",
        "aws_access_key_id": "EMAIL_AWS_ACCESS_KEY_ID",
        "aws_secret_access_key": "EMAIL_AWS_SECRET_ACCESS_KEY",
        "aws_session_token": "EMAIL_AWS_SESSION_TOKEN",
        "configuration_set_name": "EMAIL_SES_CONFIGURATION_SET",
    }

    def __init__(
        self,
        region_name: str = "us-east-1",
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        aws_session_token: str | None = None,
        configuration_set_name: str | None = None,
        **kwargs,
    ) -> None:
        if get_session is None:
            raise ImportError(
                "SESBackend requires aiobotocore package. Install it with: pip install aiobotocore"
            )

        super().__init__(**kwargs)

        self.region_name = region_name
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key
        self.aws_session_token = aws_session_token
        self.configuration_set_name = configuration_set_name

        self._session: AioSession | None = None

    async def open(self) -> bool:
        """
        Initialize AWS session.
        """
        if self._session is not None:
            return False

        self._session = get_session()
        return True

    async def close(self) -> None:
        """
        Close AWS session.
        """
        self._session = None

    async def send_messages(
        self,
        email_messages: list["EmailMessage"],
        fail_silently: bool | None = None,
    ) -> int:
        """
        Send one or more EmailMessage objects via AWS SES.
        """
        if not email_messages:
            return 0

        silence = self._silence(fail_silently)
        await self.open()

        if self._session is None:
            if not silence:
                raise ConnectionError("the AWS session could not be built")
            return 0

        client_kwargs = {"region_name": self.region_name}

        if self.aws_access_key_id:
            client_kwargs["aws_access_key_id"] = self.aws_access_key_id

        if self.aws_secret_access_key:
            client_kwargs["aws_secret_access_key"] = self.aws_secret_access_key

        if self.aws_session_token:
            client_kwargs["aws_session_token"] = self.aws_session_token

        sent = 0
        async with self._session.create_client("sesv2", **client_kwargs) as client:
            for message in email_messages:
                try:
                    await self._send(client, message)
                    sent += 1
                except Exception:
                    # Broad, but only reached when a caller asked for silence.
                    if not silence:
                        raise
                    logger.exception("a message could not be sent via SES")

        return sent

    async def _send(self, ses_client, email_message: "EmailMessage") -> None:
        """
        Send a single email message via SES.
        """
        mime_message = email_message.message()
        from_addr = email_message.get_from_email()
        recipients = email_message.recipients()

        # The envelope, so a Bcc is addressed here and named in no header.
        request: dict[str, Any] = {
            "FromEmailAddress": from_addr,
            "Destination": {"ToAddresses": recipients},
            "Content": {
                "Raw": {
                    "Data": mime_message.as_bytes(),
                },
            },
        }

        if self.configuration_set_name:
            request["ConfigurationSetName"] = self.configuration_set_name

        response = await ses_client.send_email(**request)

        logger.debug("a message was accepted by SES as %s", response.get("MessageId"))
