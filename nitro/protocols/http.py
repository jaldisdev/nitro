"""HTTP requests and responses.

A handler receives the scope and protocol objects the server built, and this
layer puts a comfortable surface on them: :class:`Request` reads the request,
and a :class:`Response` describes what to send back.

There is no dictionary of connection state and no send callable. The scope is
an object with attributes and the protocol is the thing a response is written
through, which is what lets a mistyped field be an ``AttributeError`` where it
is written rather than a missing key somewhere later.
"""

from __future__ import annotations

import http.cookies as http_cookies
import json as json_module
import mimetypes
import os
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode

__all__ = [
    "Address",
    "FileResponse",
    "HTMLResponse",
    "JSONResponse",
    "PlainTextResponse",
    "QueryParams",
    "RedirectResponse",
    "Request",
    "Response",
    "State",
    "StreamingResponse",
    "TemplateResponse",
    "URL",
]


@dataclass(frozen=True, slots=True)
class Address:
    """One end of a connection."""

    host: str
    port: int

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


class URL:
    """The address a request was made to."""

    __slots__ = ("_authority", "_path", "_query", "_scheme")

    def __init__(self, scheme: str, authority: str | None, path: str, query: str) -> None:
        self._scheme = scheme
        self._authority = authority
        self._path = path
        self._query = query

    @property
    def scheme(self) -> str:
        return self._scheme

    @property
    def netloc(self) -> str:
        return self._authority or ""

    @property
    def hostname(self) -> str | None:
        if not self._authority:
            return None
        return self._authority.rsplit(":", 1)[0] if ":" in self._authority else self._authority

    @property
    def port(self) -> int | None:
        if not self._authority or ":" not in self._authority:
            return None
        try:
            return int(self._authority.rsplit(":", 1)[1])
        except ValueError:
            return None

    @property
    def path(self) -> str:
        return self._path

    @property
    def query(self) -> str:
        return self._query

    def replace(self, **parts: Any) -> URL:
        return URL(
            parts.get("scheme", self._scheme),
            parts.get("authority", self._authority),
            parts.get("path", self._path),
            parts.get("query", self._query),
        )

    def __str__(self) -> str:
        base = f"{self._scheme}://{self._authority}" if self._authority else ""
        return f"{base}{self._path}" + (f"?{self._query}" if self._query else "")

    def __repr__(self) -> str:
        return f"URL({str(self)!r})"

    def __eq__(self, other: object) -> bool:
        return str(self) == str(other)


class QueryParams:
    """The query string, parsed. A name may appear more than once."""

    __slots__ = ("_pairs",)

    def __init__(self, query: str = "") -> None:
        self._pairs: list[tuple[str, str]] = parse_qsl(query, keep_blank_values=True)

    def __getitem__(self, name: str) -> str:
        for key, value in self._pairs:
            if key == name:
                return value
        raise KeyError(name)

    def get(self, name: str, default: Any = None) -> Any:
        try:
            return self[name]
        except KeyError:
            return default

    def get_all(self, name: str) -> list[str]:
        return [value for key, value in self._pairs if key == name]

    def keys(self) -> list[str]:
        return list(dict.fromkeys(key for key, _ in self._pairs))

    def values(self) -> list[str]:
        return [value for _, value in self._pairs]

    def items(self) -> list[tuple[str, str]]:
        return list(self._pairs)

    def __contains__(self, name: str) -> bool:
        return any(key == name for key, _ in self._pairs)

    def __iter__(self):
        return iter(self.keys())

    def __len__(self) -> int:
        return len(self.keys())

    def __str__(self) -> str:
        return urlencode(self._pairs)

    def __repr__(self) -> str:
        return f"QueryParams({str(self)!r})"


class State:
    """A place for middleware and handlers to leave things for one another."""

    def __init__(self, **initial: Any) -> None:
        self.__dict__.update(initial)

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"request state has no {name!r}")

    def __contains__(self, name: str) -> bool:
        return name in self.__dict__

    def __repr__(self) -> str:
        return f"State({self.__dict__!r})"


class Request:
    """An HTTP request."""

    __slots__ = (
        "_body",
        "_cookies",
        "_form",
        "_json",
        "_path_params",
        "_protocol",
        "_query_params",
        "_state",
        "_url",
        "scope",
    )

    def __init__(self, scope: Any, protocol: Any, path_params: dict[str, Any] | None = None):
        self.scope = scope
        self._protocol = protocol
        self._path_params = dict(path_params) if path_params else dict(scope.path_params)
        self._state = State()
        self._body: bytes | None = None
        self._form: dict[str, str] | None = None
        self._json: Any = None
        self._cookies: dict[str, str] | None = None
        self._url: URL | None = None
        self._query_params: QueryParams | None = None

    @property
    def method(self) -> str:
        return self.scope.method

    @property
    def path(self) -> str:
        return self.scope.path

    @property
    def http_version(self) -> str:
        return self.scope.http_version

    @property
    def url(self) -> URL:
        if self._url is None:
            self._url = URL(
                self.scope.scheme, self.scope.authority, self.scope.path, self.scope.query_string
            )
        return self._url

    @property
    def headers(self) -> Any:
        """The request headers, as a read-only mapping."""
        return self.scope.headers

    @property
    def query_params(self) -> QueryParams:
        if self._query_params is None:
            self._query_params = QueryParams(self.scope.query_string)
        return self._query_params

    @property
    def path_params(self) -> dict[str, Any]:
        return self._path_params

    @property
    def cookies(self) -> dict[str, str]:
        if self._cookies is None:
            parsed: dict[str, str] = {}
            header = self.scope.headers.get("cookie")
            if header:
                jar = http_cookies.SimpleCookie()
                # A malformed cookie header should not fail the request; the
                # cookies that did parse are still worth having.
                try:
                    jar.load(header)
                except http_cookies.CookieError:
                    pass
                parsed = {name: morsel.value for name, morsel in jar.items()}
            self._cookies = parsed
        return self._cookies

    @property
    def client(self) -> Address | None:
        pair = self.scope.client
        return Address(*pair) if pair else None

    @property
    def server(self) -> Address | None:
        pair = self.scope.server
        return Address(*pair) if pair else None

    @property
    def state(self) -> State:
        return self._state

    @property
    def protocol(self) -> Any:
        """The transport object, for a handler that wants to answer directly
        rather than by returning a response."""
        return self._protocol

    @property
    def disconnected(self) -> bool:
        """Whether the client has already gone away."""
        return self._protocol.disconnected

    async def client_disconnect(self) -> None:
        """Resolve once there is no longer anybody to send to."""
        await self._protocol.client_disconnect()

    async def body(self) -> bytes:
        """The whole request body. Reading it twice gives the same bytes."""
        if self._body is None:
            self._body = await self._protocol()
        return self._body

    async def json(self) -> Any:
        if self._json is None:
            self._json = json_module.loads(await self.body())
        return self._json

    async def form(self) -> dict[str, str]:
        if self._form is None:
            body = await self.body()
            self._form = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
        return self._form

    async def stream(self) -> AsyncIterator[bytes]:
        """The request body as it arrives.

        Once the body has been read whole, streaming yields it in one piece
        rather than nothing — the bytes are already here.
        """
        if self._body is not None:
            yield self._body
            return

        async for chunk in self._protocol:
            if chunk:
                yield chunk

    def __repr__(self) -> str:
        return f"Request(method={self.method!r}, path={self.path!r})"


class Response:
    """What to send back.

    A response is described here and written when the framework hands it to the
    protocol, so a handler can build one, hand it to middleware and have it
    changed before anything reaches the client.
    """

    media_type: str | None = None

    def __init__(
        self,
        content: Any = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        content_type: str | None = None,
    ):
        self.status_code = status_code
        self.content_type = content_type or self.media_type
        self._headers: dict[str, str] = dict(headers or {})
        # Cookies are kept apart from the headers because more than one may be
        # set, and a mapping would keep only the last.
        self._cookies: list[str] = []
        self._body = self.render(content)

    def render(self, content: Any) -> bytes:
        """Turn `content` into the bytes to send."""
        if content is None:
            return b""
        if isinstance(content, bytes):
            return content
        if isinstance(content, str):
            return content.encode("utf-8")
        if isinstance(content, (dict, list, tuple)):
            if self.content_type is None:
                self.content_type = "application/json"
            return json_module.dumps(content).encode("utf-8")
        return str(content).encode("utf-8")

    @property
    def body(self) -> bytes:
        return self._body

    @property
    def headers(self) -> dict[str, str]:
        return self._headers

    def set_cookie(
        self,
        key: str,
        value: str = "",
        max_age: int | None = None,
        expires: str | int | None = None,
        path: str = "/",
        domain: str | None = None,
        secure: bool = False,
        httponly: bool = False,
        samesite: str | None = "lax",
    ) -> None:
        """Add a cookie. Setting several adds several."""
        jar = http_cookies.SimpleCookie()
        jar[key] = value
        morsel = jar[key]
        if max_age is not None:
            morsel["max-age"] = max_age
        if expires is not None:
            morsel["expires"] = expires
        if path is not None:
            morsel["path"] = path
        if domain is not None:
            morsel["domain"] = domain
        if secure:
            morsel["secure"] = True
        if httponly:
            morsel["httponly"] = True
        if samesite:
            morsel["samesite"] = samesite

        self._cookies.append(jar.output(header="").strip())

    def delete_cookie(self, key: str, path: str = "/", domain: str | None = None) -> None:
        """Expire a cookie immediately."""
        self.set_cookie(key, "", max_age=0, path=path, domain=domain)

    def header_pairs(self) -> list[tuple[str, str]]:
        """Every header to send, cookies included."""
        pairs: list[tuple[str, str]] = []
        if self.content_type is not None:
            pairs.append(("content-type", self.content_type))
        pairs.extend(self._headers.items())
        pairs.extend(("set-cookie", cookie) for cookie in self._cookies)
        return pairs

    async def __http__(self, protocol: Any) -> None:
        """Write this response through `protocol`."""
        protocol.response_bytes(self.status_code, self.header_pairs(), self._body)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status_code={self.status_code})"


class JSONResponse(Response):
    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return json_module.dumps(content).encode("utf-8")


class PlainTextResponse(Response):
    media_type = "text/plain; charset=utf-8"


class HTMLResponse(Response):
    media_type = "text/html; charset=utf-8"


class RedirectResponse(Response):
    def __init__(
        self,
        url: str,
        status_code: int = 307,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(b"", status_code=status_code, headers=headers)
        self._headers["location"] = quote(str(url), safe=":/%#?=@[]!$&'()*+,;")


class FileResponse(Response):
    """A file, read as it is sent rather than loaded first.

    The transport handles the reading, so the size of the file does not decide
    what the response costs in memory.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        filename: str | None = None,
        content_type: str | None = None,
        as_attachment: bool = False,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        range: tuple[int, int | None] | None = None,
    ):
        super().__init__(b"", status_code=status_code, headers=headers)
        self.path = os.fspath(path)
        self.range = range

        # Left unset so the transport can name it from the file itself, which
        # knows the actual extension even when a download name differs.
        self.content_type = content_type

        name = filename or os.path.basename(self.path)
        if as_attachment or filename:
            disposition = "attachment" if as_attachment else "inline"
            self._headers.setdefault(
                "content-disposition", f'{disposition}; filename="{name}"'
            )

    async def __http__(self, protocol: Any) -> None:
        if self.range is None:
            protocol.response_file(self.status_code, self.header_pairs(), self.path)
            return

        start, end = self.range
        protocol.response_file_range(
            self.status_code, self.header_pairs(), self.path, start, end
        )


class StreamingResponse(Response):
    """A response produced as it is sent.

    Sending waits when the client is behind, so a producer faster than its
    reader is slowed rather than filling memory.
    """

    def __init__(
        self,
        content: AsyncIterator[bytes | str] | Iterable[bytes | str],
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        content_type: str | None = None,
    ):
        super().__init__(b"", status_code=status_code, headers=headers, content_type=content_type)
        self.content = content

    async def __http__(self, protocol: Any) -> None:
        transport = protocol.response_stream(self.status_code, self.header_pairs())
        try:
            if hasattr(self.content, "__aiter__"):
                async for chunk in self.content:
                    await self._send(transport, chunk)
            else:
                for chunk in self.content:
                    await self._send(transport, chunk)
        finally:
            transport.close()

    @staticmethod
    async def _send(transport: Any, chunk: bytes | str) -> None:
        if isinstance(chunk, str):
            await transport.send_str(chunk)
        else:
            await transport.send_bytes(chunk)


class TemplateResponse(Response):
    """A rendered template.

    Rendering is deferred until the response is written, so middleware can
    still change the context after a handler has returned it.
    """

    media_type = "text/html; charset=utf-8"

    def __init__(
        self,
        template_name: str,
        context: dict[str, Any] | None = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        using: str | None = None,
    ):
        super().__init__(b"", status_code=status_code, headers=headers)
        self.template_name = template_name
        self.context = context or {}
        self.using = using

    async def render_content(self) -> bytes:
        from nitro.templates import templates

        template = templates.get_template(self.template_name, using=self.using)
        return (await template.render_to_string(self.context)).encode("utf-8")

    async def __http__(self, protocol: Any) -> None:
        self._body = await self.render_content()
        protocol.response_bytes(self.status_code, self.header_pairs(), self._body)


def guess_content_type(path: str | os.PathLike[str]) -> str:
    """The content type for `path`, falling back to a generic one."""
    guessed, _encoding = mimetypes.guess_type(os.fspath(path))
    return guessed or "application/octet-stream"
