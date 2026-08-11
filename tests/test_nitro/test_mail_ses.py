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

"""The AWS SES email backend.

Driven against a stand-in for aioboto3, like the SendGrid tests: what is worth
pinning is the request the backend builds and how it treats a failure, neither
of which needs an AWS account.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nitro.mail.backends.ses import SESBackend
from nitro.mail.message import EmailMessage


def message(subject: str = "Subject") -> EmailMessage:
    return EmailMessage(
        subject=subject,
        body="Body",
        from_email="sender@example.com",
        to=["recipient@example.com"],
    )


class FakeSession:
    """Stands in for `aioboto3.Session`, handing out a fake SES client."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.client_calls: list[str] = []
        self.ses = MagicMock()
        self.ses.send_raw_email = AsyncMock(return_value={"MessageId": "abc-123"})

    def client(self, name):
        self.client_calls.append(name)
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=self.ses)
        context.__aexit__ = AsyncMock(return_value=None)
        return context


@pytest.fixture
def backend():
    with patch("nitro.mail.backends.ses.aioboto3", MagicMock()):
        yield SESBackend(region_name="eu-central-1")


class TestConstruction:
    def test_the_package_is_required(self):
        with patch("nitro.mail.backends.ses.aioboto3", None):
            with pytest.raises(ImportError, match="aioboto3"):
                SESBackend()

    def test_it_declares_the_settings_it_wants(self):
        assert set(SESBackend.settings_map) == {
            "region_name",
            "aws_access_key_id",
            "aws_secret_access_key",
            "aws_session_token",
            "configuration_set_name",
        }

    def test_it_does_not_ask_for_smtp_settings(self):
        # The base used to insist on host, port and credentials, none of which
        # mean anything to an HTTP API.
        assert "host" not in SESBackend.settings_map
        assert "password" not in SESBackend.settings_map


class TestSending:
    async def test_nothing_to_send_is_not_a_connection(self, backend):
        assert await backend.send_messages([]) == 0

    async def test_a_message_is_sent_as_a_raw_mime_document(self, backend):
        session = FakeSession()
        with patch.object(backend, "_session", session):
            assert await backend.send_messages([message()]) == 1

        assert session.client_calls == ["ses"]
        request = session.ses.send_raw_email.await_args.kwargs
        assert request["Source"] == "sender@example.com"
        assert request["Destinations"] == ["recipient@example.com"]
        assert b"Subject" in request["RawMessage"]["Data"]

    async def test_several_messages_are_counted(self, backend):
        session = FakeSession()
        with patch.object(backend, "_session", session):
            sent = await backend.send_messages([message("One"), message("Two")])
        assert sent == 2

    async def test_a_configuration_set_is_included_when_given(self):
        with patch("nitro.mail.backends.ses.aioboto3", MagicMock()):
            backend = SESBackend(configuration_set_name="tracked")

        session = FakeSession()
        with patch.object(backend, "_session", session):
            await backend.send_messages([message()])

        assert session.ses.send_raw_email.await_args.kwargs["ConfigurationSetName"] == "tracked"

    async def test_no_configuration_set_when_none_is_configured(self, backend):
        session = FakeSession()
        with patch.object(backend, "_session", session):
            await backend.send_messages([message()])

        assert "ConfigurationSetName" not in session.ses.send_raw_email.await_args.kwargs


class TestFailures:
    async def test_a_failure_is_raised_by_default(self, backend):
        session = FakeSession()
        session.ses.send_raw_email = AsyncMock(side_effect=RuntimeError("rejected"))

        with patch.object(backend, "_session", session):
            with pytest.raises(RuntimeError, match="rejected"):
                await backend.send_messages([message()])

    async def test_a_failure_is_swallowed_when_asked(self, backend, caplog):
        session = FakeSession()
        session.ses.send_raw_email = AsyncMock(side_effect=RuntimeError("rejected"))

        with patch.object(backend, "_session", session):
            assert await backend.send_messages([message()], fail_silently=True) == 0

    async def test_the_flag_is_not_written_onto_the_backend(self, backend):
        # It used to be, so one caller's choice leaked into the next send over
        # the same connection.
        session = FakeSession()
        session.ses.send_raw_email = AsyncMock(side_effect=RuntimeError("rejected"))

        with patch.object(backend, "_session", session):
            await backend.send_messages([message()], fail_silently=True)

        assert backend.fail_silently is False
        with patch.object(backend, "_session", session):
            with pytest.raises(RuntimeError):
                await backend.send_messages([message()])

    async def test_a_configured_default_is_used_when_nothing_is_passed(self):
        with patch("nitro.mail.backends.ses.aioboto3", MagicMock()):
            backend = SESBackend(fail_silently=True)

        session = FakeSession()
        session.ses.send_raw_email = AsyncMock(side_effect=RuntimeError("rejected"))

        with patch.object(backend, "_session", session):
            assert await backend.send_messages([message()]) == 0


class TestLifecycle:
    async def test_opening_builds_a_session_once(self, backend):
        assert await backend.open() is True
        assert await backend.open() is False

    async def test_closing_releases_it(self, backend):
        await backend.open()
        await backend.close()
        assert backend._session is None

    async def test_it_works_as_a_context_manager(self, backend):
        async with backend as opened:
            assert opened is backend
            assert backend._session is not None
        assert backend._session is None
