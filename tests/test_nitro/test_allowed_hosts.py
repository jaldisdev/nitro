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

"""Host header validation, against a real server.

`ALLOWED_HOSTS` was declared, documented and checked by `nitro check` while
being read by nothing at all. These drive the enforcement through the compiled
server, which is where it happens: a refused request never reaches a handler.
"""

from __future__ import annotations

from nitro.settings import ServerOptions


class TestResolution:
    """The setting is top level, not a SERVER key."""

    def test_it_is_read_from_the_top_level(self):
        class Source:
            ALLOWED_HOSTS = ["example.test"]

        assert ServerOptions.resolve(Source()).allowed_hosts == ["example.test"]

    def test_it_defaults_to_answering_for_anything(self):
        class Source:
            pass

        assert ServerOptions.resolve(Source()).allowed_hosts == []


HOSTED_APP = """
    from nitro import Nitro
    from nitro.protocols import PlainTextResponse

    app = Nitro(
        http="1",
        log_level="warning",
        allowed_hosts=["allowed.test", ".sub.test"],
    )

    @app.route("/")
    async def index(request):
        return PlainTextResponse("served")
"""

OPEN_APP = """
    from nitro import Nitro
    from nitro.protocols import PlainTextResponse

    app = Nitro(http="1", log_level="warning")

    @app.route("/")
    async def index(request):
        return PlainTextResponse("served")
"""


class TestEnforcement:
    def test_a_configured_host_is_served(self, server_factory):
        server = server_factory(HOSTED_APP)
        answer = server.request("/", headers={"Host": "allowed.test"})
        assert answer.status == 200
        assert answer.text == "served"

    def test_an_unconfigured_host_is_refused(self, server_factory):
        server = server_factory(HOSTED_APP)
        answer = server.request("/", headers={"Host": "attacker.test"})
        assert answer.status == 400
        assert b"Host" in answer.body

    def test_the_refusal_does_not_name_the_configured_hosts(self, server_factory):
        server = server_factory(HOSTED_APP)
        answer = server.request("/", headers={"Host": "attacker.test"})
        assert b"allowed.test" not in answer.body

    def test_a_subdomain_of_a_dotted_entry_is_served(self, server_factory):
        server = server_factory(HOSTED_APP)
        assert server.request("/", headers={"Host": "any.sub.test"}).status == 200
        assert server.request("/", headers={"Host": "sub.test"}).status == 200

    def test_a_name_merely_ending_the_same_way_is_refused(self, server_factory):
        server = server_factory(HOSTED_APP)
        assert server.request("/", headers={"Host": "evilsub.test"}).status == 400

    def test_the_port_is_not_part_of_the_name(self, server_factory):
        server = server_factory(HOSTED_APP)
        answer = server.request("/", headers={"Host": f"allowed.test:{server.port}"})
        assert answer.status == 200

    def test_matching_ignores_case(self, server_factory):
        server = server_factory(HOSTED_APP)
        assert server.request("/", headers={"Host": "ALLOWED.test"}).status == 200

    def test_an_unconfigured_list_serves_any_host(self, server_factory):
        server = server_factory(OPEN_APP)
        assert server.request("/", headers={"Host": "anything.test"}).status == 200

    def test_a_refused_request_never_reaches_the_application(self, server_factory):
        server = server_factory(
            """
            from nitro import Nitro
            from nitro.protocols import PlainTextResponse

            app = Nitro(http="1", log_level="warning", allowed_hosts=["allowed.test"])

            @app.route("/")
            async def index(request):
                print("HANDLER RAN", flush=True)
                return PlainTextResponse("served")
            """
        )
        assert server.request("/", headers={"Host": "attacker.test"}).status == 400
        server.stop()
        assert "HANDLER RAN" not in server.output
