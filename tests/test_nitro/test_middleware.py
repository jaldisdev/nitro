import pytest

from nitro.middleware.base import Middleware
from nitro.middleware.stack import MiddlewareStack
from nitro.protocols.http import Request, Response

# ── Helpers ──────────────────────────────────────────────────────────────────


class FakeHeaders(dict):
    def get(self, name, default=None):
        return super().get(name.lower(), default)


class FakeScope:
    """Stands in for the compiled scope object."""

    def __init__(self, method="GET", path="/", headers=None, query_string=""):
        self.method = method
        self.path = path
        self.query_string = query_string
        self.scheme = "http"
        self.authority = "localhost:8000"
        self.http_version = "1.1"
        self.headers = FakeHeaders({name.lower(): value for name, value in (headers or {}).items()})
        self.client = ("127.0.0.1", 9000)
        self.server = ("localhost", 8000)
        self.path_params = {}


class FakeProtocol:
    disconnected = False

    async def __call__(self):
        return b""


def make_request(method="GET", path="/", headers=None):
    return Request(FakeScope(method=method, path=path, headers=headers), FakeProtocol())


# ── Base middleware ───────────────────────────────────────────────────────────


class TestMiddlewareProtocolSupport:
    def test_has_support_for_implemented_http(self):
        class MyMiddleware(Middleware):
            async def __http__(self, request, call_next):
                return await call_next(request)

        m = MyMiddleware()
        assert m.has_protocol_support("http") is True

    def test_no_support_for_unimplemented(self):
        class HttpOnly(Middleware):
            async def __http__(self, request, call_next):
                return await call_next(request)

        m = HttpOnly()
        assert m.has_protocol_support("websocket") is False

    def test_universal_call_counts_as_all_protocols(self):
        class Universal(Middleware):
            async def __call__(self, conn, call_next):
                return await call_next(conn)

        m = Universal()
        assert m.has_protocol_support("http") is True
        assert m.has_protocol_support("websocket") is True


# ── Middleware stack ──────────────────────────────────────────────────────────


class TestMiddlewareStack:
    def test_empty_stack(self):
        stack = MiddlewareStack(app=None, middleware_paths=[])
        assert len(stack) == 0

    def test_add_middleware(self):
        class DummyMiddleware(Middleware):
            async def __call__(self, conn, call_next):
                return await call_next(conn)

        stack = MiddlewareStack(app=None, middleware_paths=[])
        stack.add_middleware(DummyMiddleware())
        assert len(stack) == 1

    def test_clear(self):
        class DummyMiddleware(Middleware):
            async def __call__(self, conn, call_next):
                return await call_next(conn)

        stack = MiddlewareStack(app=None, middleware_paths=[])
        stack.add_middleware(DummyMiddleware())
        stack.clear()
        assert len(stack) == 0

    def test_add_non_middleware_raises(self):
        stack = MiddlewareStack(app=None, middleware_paths=[])
        with pytest.raises(TypeError):
            stack.add_middleware(object())

    def test_load_from_path(self):
        # Use a built-in middleware class that has a fully-qualified path
        stack = MiddlewareStack(
            app=None,
            middleware_paths=["nitro.middleware.common.LoggingMiddleware"],
        )
        assert len(stack) == 1

    def test_invalid_path_raises(self):
        with pytest.raises(ImportError):
            MiddlewareStack(
                app=None, middleware_paths=["nitro.does.not.exist.Middleware"]
            )

    @pytest.mark.asyncio
    async def test_execute_http_passes_through_empty_stack(self):
        stack = MiddlewareStack(app=None, middleware_paths=[])
        req = make_request()

        async def handler(request):
            return Response(content="direct", status_code=200)

        response = await stack.execute_http(req, handler)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_middleware_executes_in_order(self):
        order = []

        class FirstMiddleware(Middleware):
            async def __http__(self, request, call_next):
                order.append("first-before")
                response = await call_next(request)
                order.append("first-after")
                return response

        class SecondMiddleware(Middleware):
            async def __http__(self, request, call_next):
                order.append("second-before")
                response = await call_next(request)
                order.append("second-after")
                return response

        stack = MiddlewareStack(app=None, middleware_paths=[])
        stack.add_middleware(FirstMiddleware())
        stack.add_middleware(SecondMiddleware())

        async def handler(request):
            order.append("handler")
            return Response(content="ok")

        await stack.execute_http(make_request(), handler)
        assert order == [
            "first-before",
            "second-before",
            "handler",
            "second-after",
            "first-after",
        ]

    @pytest.mark.asyncio
    async def test_http_only_middleware_skipped_for_websocket(self):
        executed = []

        class HttpOnlyMiddleware(Middleware):
            async def __http__(self, request, call_next):
                executed.append("http")
                return await call_next(request)

        stack = MiddlewareStack(app=None, middleware_paths=[])
        stack.add_middleware(HttpOnlyMiddleware())

        async def ws_handler(websocket):
            executed.append("ws_handler")

        # Create a mock websocket-like object
        class MockWS:
            pass

        await stack.execute_websocket(MockWS(), ws_handler)
        assert executed == ["ws_handler"]  # middleware was skipped


# ── Common middleware ─────────────────────────────────────────────────────────


async def async_ok_handler(r):
    return Response(content="ok")


async def async_empty_handler(r):
    return Response()


class TestCORSMiddleware:
    def make_cors_middleware(self, allow_origins=None, allow_all=False):
        import os

        os.environ.setdefault("NITRO_CONFIG_MODULE", "")
        from nitro.middleware.common import CORSMiddleware

        mw = CORSMiddleware.__new__(CORSMiddleware)
        mw.app = None
        mw.allow_origins = allow_origins or ["https://example.com"]
        mw.allow_all = allow_all
        mw.allow_credentials = False
        mw.allow_methods = ["GET", "POST"]
        mw.allow_headers = ["*"]
        return mw

    @pytest.mark.asyncio
    async def test_adds_cors_header_for_allowed_origin(self):
        mw = self.make_cors_middleware(allow_origins=["https://example.com"])

        request = make_request(headers={"origin": "https://example.com"})
        response = await mw.__http__(request, async_ok_handler)
        assert (
            response.headers.get("Access-Control-Allow-Origin") == "https://example.com"
        )

    @pytest.mark.asyncio
    async def test_no_cors_header_for_disallowed_origin(self):
        mw = self.make_cors_middleware(allow_origins=["https://allowed.com"])

        request = make_request(headers={"origin": "https://evil.com"})
        response = await mw.__http__(request, async_ok_handler)
        assert "Access-Control-Allow-Origin" not in response.headers

    @pytest.mark.asyncio
    async def test_preflight_returns_200(self):
        mw = self.make_cors_middleware(allow_origins=["https://example.com"])

        request = make_request()
        response = await mw.__http__(request, async_empty_handler)
        assert response.status_code == 200


class TestRateLimitMiddleware:
    def make_rate_limit_middleware(self, max_requests=10):
        from nitro.middleware.common import RateLimitMiddleware

        mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
        mw.app = None
        mw.requests = {}
        mw.max_requests = max_requests
        mw.window = 60
        return mw

    def test_allows_requests_under_limit(self):
        mw = self.make_rate_limit_middleware(max_requests=5)
        for _ in range(5):
            assert mw._check_rate_limit("127.0.0.1") is True

    def test_blocks_requests_over_limit(self):
        mw = self.make_rate_limit_middleware(max_requests=3)
        for _ in range(3):
            mw._check_rate_limit("127.0.0.1")
        assert mw._check_rate_limit("127.0.0.1") is False

    def test_different_ips_independent(self):
        mw = self.make_rate_limit_middleware(max_requests=1)
        assert mw._check_rate_limit("1.1.1.1") is True
        assert mw._check_rate_limit("2.2.2.2") is True
        assert mw._check_rate_limit("1.1.1.1") is False

    @pytest.mark.asyncio
    async def test_rate_limited_returns_429(self):
        mw = self.make_rate_limit_middleware(max_requests=0)
        request = make_request()
        response = await mw.__http__(request, async_ok_handler)
        assert response.status_code == 429


class TestSecurityHeadersMiddleware:
    def make_security_middleware(self, hsts=0, nosniff=True, frame_deny=True):
        from nitro.middleware.common import SecurityHeadersMiddleware

        mw = SecurityHeadersMiddleware.__new__(SecurityHeadersMiddleware)
        mw.app = None
        mw.hsts_seconds = hsts
        mw.hsts_include_subdomains = False
        mw.content_type_nosniff = nosniff
        mw.frame_deny = frame_deny
        return mw

    @pytest.mark.asyncio
    async def test_adds_x_content_type_options(self):
        mw = self.make_security_middleware(nosniff=True)
        response = await mw.__http__(make_request(), async_empty_handler)
        assert response.headers.get("X-Content-Type-Options") == "nosniff"

    @pytest.mark.asyncio
    async def test_adds_x_frame_options(self):
        mw = self.make_security_middleware(frame_deny=True)
        response = await mw.__http__(make_request(), async_empty_handler)
        assert response.headers.get("X-Frame-Options") == "DENY"

    @pytest.mark.asyncio
    async def test_adds_hsts_when_configured(self):
        mw = self.make_security_middleware(hsts=31536000)
        response = await mw.__http__(make_request(), async_empty_handler)
        hsts = response.headers.get("Strict-Transport-Security", "")
        assert "31536000" in hsts

    @pytest.mark.asyncio
    async def test_no_hsts_when_zero(self):
        mw = self.make_security_middleware(hsts=0)
        response = await mw.__http__(make_request(), async_empty_handler)
        assert "Strict-Transport-Security" not in response.headers


class TestExceptionMiddlewareStatuses:
    """An HttpException is an answer, not a failure."""

    async def test_an_http_exception_keeps_its_status(self):
        from nitro.middleware.common import ExceptionMiddleware
        from nitro.protocols.exceptions import Http404

        async def raises(request):
            raise Http404()

        response = await ExceptionMiddleware().__http__(make_request(), raises)
        assert response.status_code == 404

    async def test_another_exception_still_becomes_a_500(self):
        from nitro.middleware.common import ExceptionMiddleware

        async def raises(request):
            raise RuntimeError("deliberate")

        response = await ExceptionMiddleware().__http__(make_request(), raises)
        assert response.status_code == 500
