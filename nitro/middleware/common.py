import logging
import time

from nitro.middleware.base import Middleware
from nitro.protocols.exceptions import HttpException
from nitro.protocols.http import Request, Response
from nitro.protocols.websocket import WebSocket

logger = logging.getLogger("nitro.middleware")


class LoggingMiddleware(Middleware):
    """
    Universal logging middleware that works for all protocols.

    Uses __call__ to handle all connection types.
    """

    async def __call__(self, connection, call_next):
        """Log all requests/connections with timing."""
        protocol = getattr(connection, "scope", {}).get("type", "unknown")
        path = getattr(connection, "url", None) or getattr(
            connection, "path", "unknown"
        )

        start_time = time.time()
        logger.info(f"[{protocol.upper()}] Starting: {path}")

        try:
            result = await call_next(connection)
            duration = time.time() - start_time
            logger.info(f"[{protocol.upper()}] Completed: {path} ({duration:.3f}s)")
            return result
        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"[{protocol.upper()}] Failed: {path} ({duration:.3f}s) - {e}")
            raise


class CORSMiddleware(Middleware):
    """
    CORS middleware for HTTP requests only.

    Adds CORS headers based on settings.CORS_* configuration.
    """

    def __init__(self, app=None):
        super().__init__(app)

        # Load CORS settings
        from nitro.settings import settings

        self.allow_origins = getattr(settings, "CORS_ALLOWED_ORIGINS", [])
        self.allow_all = getattr(settings, "CORS_ALLOW_ALL_ORIGINS", False)
        self.allow_credentials = getattr(settings, "CORS_ALLOW_CREDENTIALS", False)
        self.allow_methods = getattr(settings, "CORS_ALLOW_METHODS", ["*"])
        self.allow_headers = getattr(settings, "CORS_ALLOW_HEADERS", ["*"])

    async def __http__(self, request: Request, call_next):
        """Add CORS headers to HTTP responses."""
        # Handle preflight requests
        if request.method == "OPTIONS":
            return self._build_preflight_response(request)

        # Process normal request
        response = await call_next(request)

        # Add CORS headers
        origin = request.headers.get("origin")
        if origin and self._is_allowed_origin(origin):
            response.headers["Access-Control-Allow-Origin"] = origin

            if self.allow_credentials:
                response.headers["Access-Control-Allow-Credentials"] = "true"

        return response

    def _is_allowed_origin(self, origin: str) -> bool:
        """Check if origin is allowed."""
        if self.allow_all:
            return True
        return origin in self.allow_origins

    def _build_preflight_response(self, request: Request) -> Response:
        """Build response for OPTIONS preflight request."""
        headers = {}

        origin = request.headers.get("origin")
        if origin and self._is_allowed_origin(origin):
            headers["Access-Control-Allow-Origin"] = origin

            if self.allow_credentials:
                headers["Access-Control-Allow-Credentials"] = "true"

            headers["Access-Control-Allow-Methods"] = ", ".join(self.allow_methods)
            headers["Access-Control-Allow-Headers"] = ", ".join(self.allow_headers)
            headers["Access-Control-Max-Age"] = "600"

        return Response(status_code=200, headers=headers)


class RateLimitMiddleware(Middleware):
    """
    Rate limiting middleware for HTTP and WebSocket.

    Implements simple in-memory rate limiting per IP address.
    """

    def __init__(self, app=None):
        super().__init__(app)
        self.requests: dict[str, list] = {}
        self.max_requests = 100  # requests per window
        self.window = 60  # seconds

    async def __aenter__(self):
        """Clean up old entries on each request."""
        self._cleanup()
        return self

    def _cleanup(self):
        """Remove expired entries."""
        now = time.time()
        for ip in list(self.requests.keys()):
            self.requests[ip] = [
                timestamp
                for timestamp in self.requests[ip]
                if now - timestamp < self.window
            ]
            if not self.requests[ip]:
                del self.requests[ip]

    def _check_rate_limit(self, ip: str) -> bool:
        """
        Check if request is within rate limit.

        Returns:
            True if allowed, False if rate limited
        """
        now = time.time()

        if ip not in self.requests:
            self.requests[ip] = []

        # Remove old requests
        self.requests[ip] = [
            timestamp
            for timestamp in self.requests[ip]
            if now - timestamp < self.window
        ]

        # Check limit
        if len(self.requests[ip]) >= self.max_requests:
            return False

        # Add current request
        self.requests[ip].append(now)
        return True

    async def __http__(self, request: Request, call_next):
        """Rate limit HTTP requests."""
        client_ip = request.client.host if request.client else "unknown"

        if not self._check_rate_limit(client_ip):
            return Response(
                content={"error": "Rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(self.window)},
            )

        return await call_next(request)

    async def __websocket__(self, websocket: WebSocket, call_next):
        """Rate limit WebSocket connections."""
        client_ip = websocket.client.host if websocket.client else "unknown"

        if not self._check_rate_limit(client_ip):
            await websocket.close(code=1008, reason="Rate limit exceeded")
            return

        await call_next(websocket)


class ExceptionMiddleware(Middleware):
    """
    Exception handling middleware for all protocols.

    Catches exceptions and returns appropriate error responses.
    """

    async def __http__(self, request: Request, call_next):
        """Handle HTTP exceptions."""
        try:
            return await call_next(request)
        except HttpException as e:
            # Raising one of these is how a handler asks for a particular
            # status, so it is an answer rather than a failure and must not be
            # flattened into a 500.
            return e.as_response()
        except Exception as e:
            logger.exception(f"Unhandled exception in HTTP handler: {e}")

            from nitro.settings import settings

            debug = getattr(settings, "DEBUG", False)

            if debug:
                content = {
                    "error": str(e),
                    "type": type(e).__name__,
                }
            else:
                content = {"error": "Internal server error"}

            return Response(content=content, status_code=500)

    async def __websocket__(self, websocket: WebSocket, call_next):
        """Handle WebSocket exceptions."""
        try:
            await call_next(websocket)
        except Exception as e:
            logger.exception(f"Unhandled exception in WebSocket handler: {e}")

            try:
                await websocket.close(code=1011, reason="Internal server error")
            except RuntimeError:
                logger.debug("the WebSocket was already closed", exc_info=True)


class SecurityHeadersMiddleware(Middleware):
    """
    Security headers middleware for HTTP only.

    Adds security-related headers based on settings.
    """

    def __init__(self, app=None):
        super().__init__(app)

        from nitro.settings import settings

        self.hsts_seconds = getattr(settings, "SECURE_HSTS_SECONDS", 0)
        self.hsts_include_subdomains = getattr(
            settings, "SECURE_HSTS_INCLUDE_SUBDOMAINS", False
        )
        self.content_type_nosniff = getattr(
            settings, "SECURE_CONTENT_TYPE_NOSNIFF", True
        )
        self.frame_deny = getattr(settings, "SECURE_FRAME_DENY", True)

    async def __http__(self, request: Request, call_next):
        """Add security headers to response."""
        response = await call_next(request)

        # HSTS
        if self.hsts_seconds > 0:
            hsts_value = f"max-age={self.hsts_seconds}"
            if self.hsts_include_subdomains:
                hsts_value += "; includeSubDomains"
            response.headers["Strict-Transport-Security"] = hsts_value

        # X-Content-Type-Options
        if self.content_type_nosniff:
            response.headers["X-Content-Type-Options"] = "nosniff"

        # X-Frame-Options
        if self.frame_deny:
            response.headers["X-Frame-Options"] = "DENY"

        return response
