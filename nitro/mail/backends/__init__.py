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

from nitro.mail.backends.base import BaseEmailBackend
from nitro.mail.backends.console import ConsoleBackend
from nitro.mail.backends.oauth_smtp import OAuth2SMTPBackend
from nitro.mail.backends.sendgrid import SendGridBackend
from nitro.mail.backends.ses import SESBackend
from nitro.mail.backends.smtp import SMTPBackend

__all__ = [
    "BaseEmailBackend",
    "ConsoleBackend",
    "OAuth2SMTPBackend",
    "SESBackend",
    "SMTPBackend",
    "SendGridBackend",
]
