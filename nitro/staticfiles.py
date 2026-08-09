import mimetypes
import os
import stat
from pathlib import Path
from typing import Callable

from nitro.protocols.exceptions import Http404, HttpForbidden
from nitro.protocols.http import FileResponse, HttpRequest, HttpResponse


class StaticFiles:
    def __init__(
        self,
        directory: str | Path,
        html: bool = False,
        check_dir: bool = True,
        follow_symlink: bool = False,
    ):
        self.directory = Path(directory).resolve()
        self.html = html
        self.follow_symlink = follow_symlink
        self.all_directories = [self.directory]
        self.config_checked = False

        if check_dir and not self.directory.is_dir():
            raise RuntimeError(f"Directory '{self.directory}' does not exist")

    def _get_full_path(self, path: str) -> Path | None:
        for directory in self.all_directories:
            joined_path = directory / path

            if self.follow_symlink:
                full_path = joined_path.resolve()
            else:
                full_path = joined_path.absolute()

            if not str(full_path).startswith(str(directory)):
                continue

            if full_path.is_file():
                return full_path

        return None

    def _get_response(self, path: str, scope: dict) -> HttpResponse:
        if scope["method"] not in (
            "GET",
            "HEAD",
        ):
            return HttpResponse(status_code=405)

        try:
            full_path = self._get_full_path(path)

            if full_path is None or not full_path.exists():
                if self.html:
                    full_path = self._get_full_path("404.html")
                    if full_path:
                        return FileResponse(
                            open(full_path, "rb"),
                            status_code=404,
                        )
                raise Http404

            if full_path.is_dir():
                if self.html:
                    index_path = full_path / "index.html"
                    if index_path.exists():
                        full_path = index_path
                    else:
                        raise HttpForbidden
                else:
                    raise HttpForbidden

            stat_result = os.stat(full_path)

            headers = self._get_headers(stat_result)

            if self._not_modified(scope, stat_result):
                return HttpResponse(
                    status_code=304,
                    headers=headers,
                )

            return FileResponse(
                open(full_path, "rb"),
                headers=headers,
            )

        except PermissionError:
            return HttpResponse(status_code=401)

    def _get_headers(self, stat_result: os.stat_result) -> dict[str, str]:
        headers = {}

        if stat.S_ISREG(stat_result.st_mode):
            headers["last-modified"] = self._format_http_date(stat_result.st_mtime)
            headers["etag"] = f'"{stat_result.st_mtime}-{stat_result.st_size}"'
            headers["cache-control"] = "public, max-age=3600"

        return headers

    def _not_modified(self, scope: dict, stat_result: os.stat_result) -> bool:
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }

        if_none_match = headers.get("if-none-match")
        if_modified_since = headers.get("if-modified-since")

        etag = f'"{stat_result.st_mtime}-{stat_result.st_size}"'

        if if_none_match is not None:
            return etag in [tag.strip() for tag in if_none_match.split(",")]

        if if_modified_since is not None:
            try:
                from email.utils import parsedate_to_datetime

                request_time = parsedate_to_datetime(if_modified_since)
                file_time = parsedate_to_datetime(
                    self._format_http_date(stat_result.st_mtime)
                )
                return file_time <= request_time
            except Exception:
                pass

        return False

    def _format_http_date(self, timestamp: float) -> str:
        from email.utils import formatdate

        return formatdate(timestamp, usegmt=True)

    async def __call__(
        self,
        scope: dict,
        receive: Callable,
        send: Callable,
    ) -> None:
        assert scope["type"] == "http"

        path = scope["path"]

        if path.startswith("/"):
            path = path[1:]

        response = self._get_response(path, scope)
        await response(scope, receive, send)
