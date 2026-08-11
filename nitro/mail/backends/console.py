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

"""Writing messages to a stream instead of sending them."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, TextIO

from nitro.mail.backends.base import BaseEmailBackend

if TYPE_CHECKING:
    from nitro.mail.message import EmailMessage

__all__ = ["ConsoleBackend"]

RULE = "-" * 79


class ConsoleBackend(BaseEmailBackend):
    """Write each message to a stream. The default while developing.

    EMAIL_BACKEND = "nitro.mail.backends.console.ConsoleBackend"
    """

    def __init__(self, stream: TextIO | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.stream = stream or sys.stdout

    async def open(self) -> bool:
        """Nothing to open; the stream is already there."""
        return True

    async def close(self) -> None:
        """Nothing to close: the stream is not this backend's to shut."""

    async def send_messages(
        self,
        email_messages: list[EmailMessage],
        fail_silently: bool | None = None,
    ) -> int:
        sent = 0
        for message in email_messages:
            self.stream.write(RULE)
            self.stream.write("\n")
            self.stream.write(message.message().as_string())
            self.stream.write("\n")
            self.stream.write(RULE)
            self.stream.write("\n")
            sent += 1
        return sent
