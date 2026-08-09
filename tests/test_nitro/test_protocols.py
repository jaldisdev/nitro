import json

import pytest

from nitro.protocols import (
    URL,
    Address,
    HTMLResponse,
    Http404,
    HttpException,
    HttpForbidden,
    JSONResponse,
    PlainTextResponse,
    QueryParams,
    RedirectResponse,
    Request,
    Response,
    State,
)
from nitro.protocols.http import FileResponse, StreamingResponse


class FakeHeaders(dict):
    def get(self, name, default=None):
        return super().get(name.lower(), default)


class FakeScope:
    def __init__(self, **overrides):
        self.method = overrides.get("method", "GET")
        self.path = overrides.get("path", "/")
        self.query_string = overrides.get("query_string", "")
        self.scheme = overrides.get("scheme", "http")
        self.authority = overrides.get("authority", "example.test")
        self.http_version = overrides.get("http_version", "1.1")
        self.headers = FakeHeaders(overrides.get("headers", {}))
        self.client = overrides.get("client", ("203.0.113.7", 51234))
        self.server = overrides.get("server", ("127.0.0.1", 8000))
        self.path_params = overrides.get("path_params", {})


class FakeProtocol:
    def __init__(self, body=b"", chunks=None):
        self._body = body
        self._chunks = chunks or []
        self.status = None
        self.headers = []
        self.written = None
        self.file = None
        self.stream = None
        self.disconnected = False

    async def __call__(self):
        return self._body

    def __aiter__(self):
        self._iterator = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration:
            raise StopAsyncIteration from None

    def response_bytes(self, status, headers=(), body=b""):
        self.status = status
        self.headers = list(headers)
        self.written = bytes(body)

    def response_file(self, status, headers=(), path=""):
        self.status = status
        self.headers = list(headers)
        self.file = (path, None)

    def response_file_range(self, status, headers=(), path="", start=0, end=None):
        self.status = status
        self.headers = list(headers)
        self.file = (path, (start, end))

    def response_stream(self, status, headers=()):
        self.status = status
        self.headers = list(headers)
        self.stream = FakeStream()
        return self.stream

    def header(self, name):
        return [value for key, value in self.headers if key.lower() == name]


class FakeStream:
    def __init__(self):
        self.chunks = []
        self.closed = False

    async def send_bytes(self, data):
        self.chunks.append(bytes(data))

    async def send_str(self, text):
        self.chunks.append(text.encode())

    def close(self):
        self.closed = True


def make_request(**overrides):
    protocol = FakeProtocol(
        body=overrides.pop("body", b""), chunks=overrides.pop("chunks", None)
    )
    return Request(FakeScope(**overrides), protocol), protocol


class TestURL:
    def test_it_reassembles_the_address(self):
        url = URL("https", "example.test:8443", "/path", "a=1")
        assert str(url) == "https://example.test:8443/path?a=1"
        assert url.hostname == "example.test"
        assert url.port == 8443

    def test_a_missing_port_reads_as_none(self):
        assert URL("http", "example.test", "/", "").port is None

    def test_an_unparseable_port_reads_as_none(self):
        assert URL("http", "example.test:nonsense", "/", "").port is None

    def test_parts_can_be_replaced(self):
        url = URL("http", "example.test", "/old", "")
        assert str(url.replace(path="/new")) == "http://example.test/new"


class TestQueryParams:
    def test_values_are_read_by_name(self):
        params = QueryParams("a=1&b=2")
        assert params["a"] == "1"
        assert params.get("missing", "fallback") == "fallback"

    def test_a_repeated_name_keeps_every_value(self):
        params = QueryParams("tag=one&tag=two")
        assert params["tag"] == "one"
        assert params.get_all("tag") == ["one", "two"]
        assert len(params) == 1
        assert params.items() == [("tag", "one"), ("tag", "two")]

    def test_a_blank_value_is_kept(self):
        assert QueryParams("a=")["a"] == ""

    def test_an_unknown_name_raises(self):
        with pytest.raises(KeyError):
            QueryParams("")["missing"]


class TestState:
    def test_values_can_be_set_and_read(self):
        state = State()
        state.user = "ada"
        assert state.user == "ada"
        assert "user" in state

    def test_an_unset_name_says_so(self):
        with pytest.raises(AttributeError, match="user"):
            State().user


class TestRequest:
    def test_the_basics_come_from_the_scope(self):
        request, _ = make_request(method="POST", path="/things", query_string="a=1")

        assert request.method == "POST"
        assert request.path == "/things"
        assert request.query_params["a"] == "1"
        assert str(request.url) == "http://example.test/things?a=1"
        assert request.http_version == "1.1"

    def test_addresses_are_reported_as_pairs(self):
        request, _ = make_request()
        assert request.client == Address("203.0.113.7", 51234)
        assert request.server == Address("127.0.0.1", 8000)

    def test_a_missing_address_reads_as_none(self):
        request, _ = make_request(client=None, server=None)
        assert request.client is None
        assert request.server is None

    def test_cookies_are_parsed(self):
        request, _ = make_request(headers={"cookie": "session=abc; theme=dark"})
        assert request.cookies == {"session": "abc", "theme": "dark"}

    def test_no_cookie_header_reads_as_empty(self):
        request, _ = make_request()
        assert request.cookies == {}

    def test_a_malformed_cookie_header_does_not_fail_the_request(self):
        request, _ = make_request(headers={"cookie": "=broken;;"})
        assert isinstance(request.cookies, dict)

    async def test_the_body_is_read_once_and_remembered(self):
        request, protocol = make_request(body=b"payload")
        assert await request.body() == b"payload"
        assert await request.body() == b"payload"

    async def test_json_is_parsed(self):
        request, _ = make_request(body=json.dumps({"a": 1}).encode())
        assert await request.json() == {"a": 1}

    async def test_a_form_is_parsed(self):
        request, _ = make_request(body=b"a=1&b=two")
        assert await request.form() == {"a": "1", "b": "two"}

    async def test_the_body_can_be_streamed(self):
        request, _ = make_request(chunks=[b"one", b"two"])
        assert [chunk async for chunk in request.stream()] == [b"one", b"two"]

    async def test_streaming_after_reading_yields_what_was_read(self):
        request, _ = make_request(body=b"whole")
        await request.body()
        assert [chunk async for chunk in request.stream()] == [b"whole"]

    def test_path_parameters_are_carried(self):
        request, _ = make_request(path_params={"identifier": "42"})
        assert request.path_params == {"identifier": "42"}

    def test_the_protocol_is_reachable_for_a_direct_answer(self):
        request, protocol = make_request()
        assert request.protocol is protocol


class TestResponse:
    async def test_text_is_encoded(self):
        _, protocol = make_request()
        await Response("hello").__http__(protocol)

        assert protocol.status == 200
        assert protocol.written == b"hello"

    async def test_a_mapping_becomes_json(self):
        _, protocol = make_request()
        await Response({"a": 1}).__http__(protocol)

        assert json.loads(protocol.written) == {"a": 1}
        assert protocol.header("content-type") == ["application/json"]

    async def test_bytes_pass_through(self):
        _, protocol = make_request()
        await Response(b"\x00\x01").__http__(protocol)
        assert protocol.written == b"\x00\x01"

    async def test_none_is_an_empty_body(self):
        _, protocol = make_request()
        await Response(None, status_code=204).__http__(protocol)
        assert protocol.written == b""
        assert protocol.status == 204

    async def test_the_convenience_classes_set_their_type(self):
        for response, expected in [
            (JSONResponse({"a": 1}), "application/json"),
            (PlainTextResponse("hi"), "text/plain; charset=utf-8"),
            (HTMLResponse("<p>hi</p>"), "text/html; charset=utf-8"),
        ]:
            _, protocol = make_request()
            await response.__http__(protocol)
            assert protocol.header("content-type") == [expected]

    async def test_a_redirect_carries_its_location(self):
        _, protocol = make_request()
        await RedirectResponse("/elsewhere").__http__(protocol)

        assert protocol.status == 307
        assert protocol.header("location") == ["/elsewhere"]

    async def test_several_cookies_all_survive(self):
        response = Response("hi")
        response.set_cookie("session", "abc")
        response.set_cookie("theme", "dark", max_age=60, httponly=True)

        _, protocol = make_request()
        await response.__http__(protocol)

        cookies = protocol.header("set-cookie")
        assert len(cookies) == 2, "setting two cookies must send two"
        assert any(cookie.startswith("session=abc") for cookie in cookies)
        assert any("HttpOnly" in cookie for cookie in cookies)

    async def test_deleting_a_cookie_expires_it(self):
        response = Response("hi")
        response.delete_cookie("session")

        _, protocol = make_request()
        await response.__http__(protocol)
        assert "Max-Age=0" in protocol.header("set-cookie")[0]


class TestFileResponse:
    async def test_a_whole_file_is_handed_to_the_transport(self, tmp_path):
        path = tmp_path / "data.txt"
        path.write_bytes(b"contents")

        _, protocol = make_request()
        await FileResponse(path).__http__(protocol)

        assert protocol.file == (str(path), None)

    async def test_a_range_is_handed_to_the_transport(self, tmp_path):
        path = tmp_path / "data.txt"
        path.write_bytes(b"contents")

        _, protocol = make_request()
        await FileResponse(path, range=(2, 5)).__http__(protocol)

        assert protocol.file == (str(path), (2, 5))

    async def test_an_attachment_names_itself(self, tmp_path):
        path = tmp_path / "report.pdf"
        path.write_bytes(b"%PDF")

        _, protocol = make_request()
        await FileResponse(path, as_attachment=True).__http__(protocol)

        assert protocol.header("content-disposition") == ['attachment; filename="report.pdf"']


class TestStreamingResponse:
    async def test_an_async_iterator_is_streamed(self):
        async def produce():
            for index in range(3):
                yield f"chunk-{index}"

        _, protocol = make_request()
        await StreamingResponse(produce()).__http__(protocol)

        assert protocol.stream.chunks == [b"chunk-0", b"chunk-1", b"chunk-2"]
        assert protocol.stream.closed

    async def test_a_plain_iterable_is_streamed(self):
        _, protocol = make_request()
        await StreamingResponse([b"a", b"b"]).__http__(protocol)
        assert protocol.stream.chunks == [b"a", b"b"]

    async def test_the_stream_is_closed_even_when_producing_fails(self):
        async def produce():
            yield b"first"
            raise RuntimeError("deliberate")

        _, protocol = make_request()
        with pytest.raises(RuntimeError, match="deliberate"):
            await StreamingResponse(produce()).__http__(protocol)

        assert protocol.stream.closed, "a failed producer must not leave the stream open"


class TestExceptions:
    async def test_an_exception_becomes_a_response(self):
        _, protocol = make_request()
        await Http404().as_response().__http__(protocol)

        assert protocol.status == 404
        assert protocol.written == b"Not found"

    async def test_a_structured_detail_becomes_json(self):
        _, protocol = make_request()
        await HttpForbidden({"reason": "no"}).as_response().__http__(protocol)

        assert protocol.status == 403
        assert json.loads(protocol.written) == {"reason": "no"}

    async def test_headers_are_carried_through(self):
        _, protocol = make_request()
        exception = HttpException("nope", headers={"x-reason": "testing"})
        await exception.as_response().__http__(protocol)

        assert protocol.header("x-reason") == ["testing"]
