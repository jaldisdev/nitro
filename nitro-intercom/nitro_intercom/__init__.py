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

"""Publish/subscribe channels for Python services.

Install this package only if you are **not** running on Nitro. A Nitro project
should import ``nitro.intercom``, which reaches the same core in-process and
takes its configuration from the project's settings.

A channel is a plain string agreed on by convention — there is no registry and
no discovery. Messages are ordinary Python values, encoded as MessagePack on
the wire so a service written in another language can read them.

    from nitro_intercom import Intercom

    intercom = await Intercom.connect("redis://localhost:6379")
    await intercom.publish("room:42", {"event": "joined", "user": "ada"})

    listener = await intercom.subscribe("room:42")
    async for message in listener:
        print(message)
"""

from nitro_intercom._intercom import Intercom, Listener, Reader

__version__ = "0.1.0"

__all__ = ["Intercom", "Listener", "Reader", "__version__"]
