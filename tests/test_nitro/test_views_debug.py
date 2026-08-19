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

import re
import sys

import pytest

from nitro.routing.router import WEBSOCKET_METHOD, WEBTRANSPORT_METHOD, Router
from nitro.views.debug import (
    RoutePattern,
    debug_response,
    render_404_page,
    render_500_page,
    route_patterns,
)


async def handler(request):
    return None


@pytest.fixture
def router() -> Router:
    return Router()


def raised(message: str = "a distinctive message") -> ValueError:
    """A `ValueError` carrying a real traceback, as the page is given one."""
    try:
        raise ValueError(message)
    except ValueError as error:
        return error


def badge_classes(page: str, method: str) -> list[str]:
    """The `class` of every badge on `page` showing `method`."""
    pattern = re.compile(r'class="(method-badge[^"]*)"\s*>' + re.escape(method) + r"<")
    return pattern.findall(page)


class TestRoutePatterns:
    def test_no_routes_are_no_patterns(self):
        assert route_patterns([]) == []

    def test_a_route_carries_its_path_and_methods(self, router):
        router.add("/things", handler, methods=["GET", "POST"])

        assert route_patterns(router.routes) == [RoutePattern("/things", ("GET", "POST"))]

    def test_head_is_hidden_beside_get(self, router):
        router.add("/things", handler, methods=["GET", "HEAD"])

        assert route_patterns(router.routes) == [RoutePattern("/things", ("GET",))]

    def test_head_alone_is_still_listed(self, router):
        router.add("/things", handler, methods=["HEAD"])

        assert route_patterns(router.routes) == [RoutePattern("/things", ("HEAD",))]

    def test_a_path_registered_twice_is_listed_once(self, router):
        router.add("/thing", handler, methods=["GET"])
        router.add("/thing", handler, methods=[WEBSOCKET_METHOD])

        assert route_patterns(router.routes) == [RoutePattern("/thing", ("GET", WEBSOCKET_METHOD))]

    def test_a_method_registered_twice_is_listed_once(self, router):
        router.add("/thing", handler, methods=["GET"])
        router.add("/thing", handler, methods=["POST"])
        router.add("/thing", handler, methods=[WEBSOCKET_METHOD])

        assert route_patterns(router.routes) == [
            RoutePattern("/thing", ("GET", "POST", WEBSOCKET_METHOD))
        ]

    def test_paths_keep_the_order_they_were_registered_in(self, router):
        router.add("/second", handler)
        router.add("/first", handler)

        assert [pattern.path for pattern in route_patterns(router.routes)] == [
            "/second",
            "/first",
        ]


class TestRender404Page:
    def test_the_request_is_shown(self):
        page = render_404_page(method="POST", path="/missing")

        assert "POST" in page
        assert "/missing" in page

    def test_every_pattern_is_listed_with_its_methods(self):
        page = render_404_page(
            method="GET",
            path="/missing",
            url_patterns=[
                RoutePattern("/things", ("GET", "POST")),
                RoutePattern("/other", ("DELETE",)),
            ],
        )

        assert "/things" in page
        assert "/other" in page
        assert badge_classes(page, "GET") == ["method-badge"]
        assert badge_classes(page, "POST") == ["method-badge"]
        assert badge_classes(page, "DELETE") == ["method-badge"]

    @pytest.mark.parametrize("method", [WEBSOCKET_METHOD, WEBTRANSPORT_METHOD])
    def test_a_protocol_method_is_marked_as_one(self, method):
        page = render_404_page(
            method="GET", path="/missing", url_patterns=[RoutePattern("/live", (method,))]
        )

        assert badge_classes(page, method) == ["method-badge protocol"]

    def test_a_page_without_patterns_says_so(self):
        page = render_404_page(method="GET", path="/missing")

        assert "No URL patterns are registered." in page

    def test_a_pattern_is_escaped(self):
        page = render_404_page(
            method="GET",
            path="/missing",
            url_patterns=[RoutePattern("/things/<int:identifier>", ("GET",))],
        )

        assert "/things/&lt;int:identifier&gt;" in page
        assert "<int:identifier>" not in page

    def test_the_requested_path_is_escaped(self):
        page = render_404_page(method="GET", path="/<script>alert(1)</script>")

        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page


class TestRender500Page:
    def test_the_exception_is_shown(self):
        page = render_500_page(method="GET", path="/boom", exc=raised())

        assert "ValueError" in page
        assert "a distinctive message" in page

    def test_the_request_is_shown(self):
        page = render_500_page(method="DELETE", path="/boom", exc=raised())

        assert "DELETE" in page
        assert "/boom" in page

    def test_the_failing_frame_is_shown(self):
        page = render_500_page(method="GET", path="/boom", exc=raised())

        assert __file__ in page
        assert "raise ValueError(message)" in page

    def test_the_python_version_is_shown(self):
        page = render_500_page(method="GET", path="/boom", exc=raised())

        assert sys.version.split()[0] in page

    def test_the_message_is_escaped(self):
        page = render_500_page(method="GET", path="/boom", exc=raised("<script>alert(1)</script>"))

        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page

    def test_an_exception_without_a_traceback_still_renders(self):
        page = render_500_page(method="GET", path="/boom", exc=ValueError("never raised"))

        assert "never raised" in page


class TestDebugResponse:
    def test_debug_off_has_no_page(self):
        assert debug_response(404, "GET", "/missing", debug=False) is None

    def test_a_status_without_a_page_has_none(self):
        assert debug_response(403, "GET", "/private", debug=True) is None

    def test_a_404_is_an_html_response(self, router):
        router.add("/things", handler)

        response = debug_response(404, "GET", "/missing", debug=True, routes=router.routes)

        assert response is not None
        assert response.status_code == 404
        assert response.content_type == "text/html; charset=utf-8"
        assert b"/things" in response.body

    def test_a_500_carries_the_traceback(self):
        response = debug_response(500, "GET", "/boom", debug=True, exception=raised())

        assert response is not None
        assert response.status_code == 500
        assert response.content_type == "text/html; charset=utf-8"
        assert b"a distinctive message" in response.body

    def test_a_500_without_an_exception_has_no_page(self):
        assert debug_response(500, "GET", "/boom", debug=True) is None
