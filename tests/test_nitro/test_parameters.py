import pytest

from nitro.routing.parameters import (
    Body,
    Cookie,
    Header,
    Path,
    Query,
    ValidationError,
)


class FakeHeaders(dict):
    def get(self, name, default=None):
        return super().get(name.lower(), default)


class FakeScope:
    """Stands in for the compiled scope object."""

    def __init__(self, query_string="", headers=None, path_params=None):
        self.method = "GET"
        self.path = "/"
        self.query_string = query_string
        self.scheme = "http"
        self.authority = "localhost:8000"
        self.http_version = "1.1"
        self.headers = FakeHeaders({name.lower(): value for name, value in (headers or {}).items()})
        self.client = ("127.0.0.1", 9000)
        self.server = ("localhost", 8000)
        self.path_params = path_params or {}


class FakeProtocol:
    def __init__(self, body=b""):
        self._body = body

    async def __call__(self):
        return self._body


def make_request(query_string="", headers=None, path_params=None, cookies_str=None, body=b""):
    from nitro.protocols.http import HttpRequest

    supplied = dict(headers or {})
    if cookies_str:
        supplied["cookie"] = cookies_str

    scope = FakeScope(query_string=query_string, headers=supplied, path_params=path_params)
    return HttpRequest(scope, FakeProtocol(body), path_params)


class TestValidationError:
    def test_message_format(self):
        err = ValidationError("age", "must be positive")
        assert "age" in str(err)
        assert "must be positive" in str(err)
        assert err.param_name == "age"
        assert err.message == "must be positive"


class TestQueryParam:
    @pytest.mark.asyncio
    async def test_extracts_present_param(self):
        req = make_request(query_string="page=3")
        result = await Query(1).extract(req, "page", int)
        assert result == 3

    @pytest.mark.asyncio
    async def test_uses_default_when_absent(self):
        req = make_request()
        result = await Query(1).extract(req, "page", int)
        assert result == 1

    @pytest.mark.asyncio
    async def test_raises_on_missing_required(self):
        req = make_request()
        with pytest.raises(ValidationError):
            await Query(...).extract(req, "page", int)

    @pytest.mark.asyncio
    async def test_bool_conversion(self):
        req = make_request(query_string="active=true")
        result = await Query(False).extract(req, "active", bool)
        assert result is True

    @pytest.mark.asyncio
    async def test_float_conversion(self):
        req = make_request(query_string="price=9.99")
        result = await Query(0.0).extract(req, "price", float)
        assert result == 9.99

    @pytest.mark.asyncio
    async def test_validates_ge_constraint(self):
        req = make_request(query_string="page=0")
        with pytest.raises(ValidationError, match="greater than or equal"):
            await Query(1, ge=1).extract(req, "page", int)

    @pytest.mark.asyncio
    async def test_validates_max_length(self):
        req = make_request(query_string="q=toolongquery")
        with pytest.raises(ValidationError, match="at most"):
            await Query(None, max_length=5).extract(req, "q", str)

    @pytest.mark.asyncio
    async def test_alias(self):
        req = make_request(query_string="per_page=25")
        result = await Query(10, alias="per_page").extract(req, "limit", int)
        assert result == 25


class TestPathParam:
    @pytest.mark.asyncio
    async def test_extracts_path_param(self):
        req = make_request(path_params={"user_id": 42})
        result = await Path(...).extract(req, "user_id", int)
        assert result == 42

    @pytest.mark.asyncio
    async def test_raises_on_missing_required(self):
        req = make_request(path_params={})
        with pytest.raises(ValidationError):
            await Path(...).extract(req, "user_id", int)


class TestHeaderParam:
    @pytest.mark.asyncio
    async def test_extracts_header(self):
        req = make_request(headers={"x-request-id": "abc123"})
        result = await Header(...).extract(req, "x_request_id", str)
        assert result == "abc123"

    @pytest.mark.asyncio
    async def test_snake_to_kebab_conversion(self):
        req = make_request(headers={"user-agent": "TestBot/1.0"})
        result = await Header(None).extract(req, "user_agent", str)
        assert result == "TestBot/1.0"

    @pytest.mark.asyncio
    async def test_raises_on_missing_required(self):
        req = make_request()
        with pytest.raises(ValidationError, match="required"):
            await Header(...).extract(req, "x_api_key", str)

    @pytest.mark.asyncio
    async def test_alias_overrides_name_conversion(self):
        req = make_request(headers={"content-type": "application/json"})
        result = await Header(None, alias="content-type").extract(req, "ct", str)
        assert result == "application/json"


class TestCookieParam:
    @pytest.mark.asyncio
    async def test_extracts_cookie(self):
        req = make_request(cookies_str="session=abc123")
        result = await Cookie(...).extract(req, "session", str)
        assert result == "abc123"

    @pytest.mark.asyncio
    async def test_returns_default_when_absent(self):
        req = make_request()
        result = await Cookie(None).extract(req, "session", str)
        assert result is None

    @pytest.mark.asyncio
    async def test_raises_on_missing_required(self):
        req = make_request()
        with pytest.raises(ValidationError):
            await Cookie(...).extract(req, "token", str)


class TestBodyParam:
    def make_json_request(self, payload: dict):
        import json

        return make_request(
            headers={"content-type": "application/json"},
            body=json.dumps(payload).encode(),
        )

    @pytest.mark.asyncio
    async def test_extracts_body_field(self):
        req = self.make_json_request({"name": "Mario", "age": 30})
        result = await Body(...).extract(req, "name", str)
        assert result == "Mario"

    @pytest.mark.asyncio
    async def test_missing_required_field_raises(self):
        req = self.make_json_request({"name": "Mario"})
        with pytest.raises(ValidationError):
            await Body(...).extract(req, "missing_field", str)

    @pytest.mark.asyncio
    async def test_uses_default_when_absent(self):
        req = self.make_json_request({})
        result = await Body("default_val").extract(req, "optional", str)
        assert result == "default_val"
