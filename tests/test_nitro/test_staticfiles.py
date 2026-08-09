import os
from email.utils import formatdate

import pytest

from nitro.protocols.exceptions import Http404, HttpForbidden, HttpMethodNotAllowed
from nitro.staticfiles import StaticFiles


class Request:
    """The part of a request StaticFiles reads."""

    def __init__(self, method: str = "GET", headers: dict[str, str] | None = None):
        self.method = method
        self.headers = headers or {}


@pytest.fixture
def served(tmp_path):
    """A directory with a file in it, and the handler serving it."""
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "app.css").write_text("body {}")
    (tmp_path / "site" / "nested").mkdir()
    (tmp_path / "site" / "nested" / "deep.txt").write_text("deep")
    (tmp_path / "secret.txt").write_text("not yours")
    return tmp_path


class TestServing:
    async def test_a_file_is_answered(self, served):
        files = StaticFiles(served / "site")
        response = await files(Request(), path="app.css")

        assert response.status_code == 200
        assert response.path == str(served / "site" / "app.css")

    async def test_a_nested_file_is_answered(self, served):
        files = StaticFiles(served / "site")
        response = await files(Request(), path="nested/deep.txt")
        assert response.path == str(served / "site" / "nested" / "deep.txt")

    async def test_a_missing_file_is_a_404(self, served):
        files = StaticFiles(served / "site")
        with pytest.raises(Http404):
            await files(Request(), path="absent.css")

    async def test_caching_headers_describe_the_file(self, served):
        files = StaticFiles(served / "site")
        response = await files(Request(), path="app.css")

        assert response.headers["cache-control"] == "public, max-age=3600"
        assert response.headers["etag"].startswith('"')
        assert "last-modified" in response.headers

    async def test_only_read_methods_are_served(self, served):
        files = StaticFiles(served / "site")
        with pytest.raises(HttpMethodNotAllowed):
            await files(Request(method="POST"), path="app.css")

    async def test_the_route_must_capture_a_path(self, served):
        files = StaticFiles(served / "site")
        with pytest.raises(TypeError, match="exactly one path parameter"):
            await files(Request())


class TestContainment:
    async def test_climbing_out_of_the_directory_is_a_404(self, served):
        files = StaticFiles(served / "site")
        with pytest.raises(Http404):
            await files(Request(), path="../secret.txt")

    async def test_an_absolute_path_does_not_escape(self, served):
        files = StaticFiles(served / "site")
        with pytest.raises(Http404):
            await files(Request(), path="/etc/hosts")

    async def test_a_sibling_directory_sharing_a_prefix_is_not_reachable(self, tmp_path):
        (tmp_path / "site").mkdir()
        (tmp_path / "site-private").mkdir()
        (tmp_path / "site-private" / "secret.txt").write_text("not yours")

        files = StaticFiles(tmp_path / "site")
        with pytest.raises(Http404):
            await files(Request(), path="../site-private/secret.txt")

    async def test_a_symlink_out_of_the_directory_is_refused_by_default(self, served):
        link = served / "site" / "escape.txt"
        link.symlink_to(served / "secret.txt")

        files = StaticFiles(served / "site")
        with pytest.raises(Http404):
            await files(Request(), path="escape.txt")

    async def test_a_symlink_can_be_followed_deliberately(self, served):
        link = served / "site" / "escape.txt"
        link.symlink_to(served / "secret.txt")

        files = StaticFiles(served / "site", follow_symlink=True)
        response = await files(Request(), path="escape.txt")
        assert response.status_code == 200


class TestDirectories:
    async def test_a_directory_is_refused(self, served):
        files = StaticFiles(served / "site")
        with pytest.raises(HttpForbidden):
            await files(Request(), path="nested")

    async def test_html_mode_serves_an_index(self, served):
        (served / "site" / "nested" / "index.html").write_text("<p>index</p>")

        files = StaticFiles(served / "site", html=True)
        response = await files(Request(), path="nested")
        assert response.path == str(served / "site" / "nested" / "index.html")

    async def test_html_mode_still_refuses_a_directory_without_an_index(self, served):
        files = StaticFiles(served / "site", html=True)
        with pytest.raises(HttpForbidden):
            await files(Request(), path="nested")

    async def test_html_mode_falls_back_to_a_404_page(self, served):
        (served / "site" / "404.html").write_text("<p>gone</p>")

        files = StaticFiles(served / "site", html=True)
        response = await files(Request(), path="absent.css")

        assert response.status_code == 404
        assert response.path == str(served / "site" / "404.html")


class TestConditionalRequests:
    async def test_a_matching_etag_is_a_304(self, served):
        files = StaticFiles(served / "site")
        first = await files(Request(), path="app.css")

        response = await files(
            Request(headers={"if-none-match": first.headers["etag"]}), path="app.css"
        )
        assert response.status_code == 304
        assert response.body == b""

    async def test_a_different_etag_sends_the_file(self, served):
        files = StaticFiles(served / "site")
        response = await files(Request(headers={"if-none-match": '"stale"'}), path="app.css")
        assert response.status_code == 200

    async def test_an_unmodified_file_is_a_304(self, served):
        files = StaticFiles(served / "site")
        modified = os.stat(served / "site" / "app.css").st_mtime

        response = await files(
            Request(headers={"if-modified-since": formatdate(modified + 10, usegmt=True)}),
            path="app.css",
        )
        assert response.status_code == 304

    async def test_a_file_changed_since_is_sent(self, served):
        files = StaticFiles(served / "site")
        modified = os.stat(served / "site" / "app.css").st_mtime

        response = await files(
            Request(headers={"if-modified-since": formatdate(modified - 10, usegmt=True)}),
            path="app.css",
        )
        assert response.status_code == 200

    async def test_an_unparseable_date_sends_the_file(self, served):
        files = StaticFiles(served / "site")
        response = await files(Request(headers={"if-modified-since": "yesterday"}), path="app.css")
        assert response.status_code == 200


class TestConfiguration:
    def test_a_missing_directory_is_reported(self, tmp_path):
        with pytest.raises(RuntimeError, match="does not exist"):
            StaticFiles(tmp_path / "absent")

    def test_the_check_can_be_skipped(self, tmp_path):
        StaticFiles(tmp_path / "absent", check_dir=False)
