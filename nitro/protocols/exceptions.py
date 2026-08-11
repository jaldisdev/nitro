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

from collections.abc import Awaitable, Callable
from typing import Any


class ExceptionHandlerRegistry:
    """
    Registry for custom exception handlers.

    Allows mapping exception types or HTTP status codes to handler functions.
    """

    def __init__(self):
        self._handlers: dict[type | int, Callable] = {}

    def add_handler(
        self,
        exc_class_or_status: type[Exception] | int,
        handler: Callable[[Any, Exception], Awaitable[Any]],
    ) -> None:
        """
        Register an exception handler.

        Args:
            exc_class_or_status: Exception class or HTTP status code to handle
            handler: Async handler function that takes (request/websocket/session, exception)
        """
        self._handlers[exc_class_or_status] = handler

    def get_handler(self, exc: Exception) -> Callable[[Any, Exception], Awaitable[Any]] | None:
        """
        Get the appropriate handler for an exception.

        Lookup order:
        1. Exact exception type match
        2. HTTP status code (for HttpException instances)
        3. Parent exception classes (MRO order)

        Args:
            exc: The exception to find a handler for

        Returns:
            Handler function or None if no handler found
        """
        # Try exact type match first
        exc_type = type(exc)
        handler = self._handlers.get(exc_type)
        if handler is not None:
            return handler

        # Try status code if it's an HttpException
        if isinstance(exc, HttpException):
            handler = self._handlers.get(exc.status_code)
            if handler is not None:
                return handler

        # Try parent classes (walking up the MRO)
        for parent_class in exc_type.__mro__[1:]:
            if parent_class is Exception or parent_class is BaseException:
                # Don't match on base Exception/BaseException
                continue
            handler = self._handlers.get(parent_class)
            if handler is not None:
                return handler

        return None

    def get_status_handler(self, status: int) -> Callable[[Any, Exception], Awaitable[Any]] | None:
        """The handler registered for a status code, looked up by the code.

        `get_handler` reaches a status key only through an `HttpException`,
        which carries one. An ordinary exception carries no status, so the
        caller that decides it is a 500 is the one that has to ask.
        """
        return self._handlers.get(status)

    def remove_handler(self, exc_class_or_status: type[Exception] | int) -> None:
        """Remove an exception handler."""
        self._handlers.pop(exc_class_or_status, None)

    def clear(self) -> None:
        """Clear all exception handlers."""
        self._handlers.clear()


class HttpException(Exception):
    """
    Base class for HTTP exceptions.

    Attributes:
        status_code: HTTP status code
        detail: Error message or detail
        headers: Optional additional headers
    """

    status_code: int = 500
    default_detail: str = "An error occurred"

    def __init__(
        self,
        detail: str | dict | None = None,
        headers: dict[str, str] | None = None,
    ):
        if detail is None:
            detail = self.default_detail

        self.detail = detail
        self.headers = headers or {}
        super().__init__(str(detail))

    def as_response(self):
        """The response this exception stands for.

        Raising one of these is how a handler says "answer with this status"
        without having to build the response itself, so the conversion belongs
        with the exception rather than being repeated at every catch site.
        """
        from nitro.protocols.http import JSONResponse, PlainTextResponse

        if isinstance(self.detail, str):
            return PlainTextResponse(
                self.detail, status_code=self.status_code, headers=dict(self.headers)
            )
        return JSONResponse(self.detail, status_code=self.status_code, headers=dict(self.headers))

    def __repr__(self):
        return f"{self.__class__.__name__}(status_code={self.status_code}, detail={self.detail!r})"


# 4xx Client Errors


class HttpBadRequest(HttpException):
    """400 Bad HttpRequest."""

    status_code = 400
    default_detail = "Bad request"


class HttpUnauthorized(HttpException):
    """401 Unauthorized."""

    status_code = 401
    default_detail = "Authentication required"


class HttpPaymentRequired(HttpException):
    """402 Payment Required."""

    status_code = 402
    default_detail = "Payment required"


class HttpForbidden(HttpException):
    """403 Forbidden."""

    status_code = 403
    default_detail = "Permission denied"


class Http404(HttpException):
    """404 Not Found."""

    status_code = 404
    default_detail = "Not found"


class HttpMethodNotAllowed(HttpException):
    """405 Method Not Allowed."""

    status_code = 405
    default_detail = "Method not allowed"


class HttpNotAcceptable(HttpException):
    """406 Not Acceptable."""

    status_code = 406
    default_detail = "Not acceptable"


class HttpProxyAuthenticationRequired(HttpException):
    """407 Proxy Authentication Required."""

    status_code = 407
    default_detail = "Proxy authentication required"


class HttpRequestTimeout(HttpException):
    """408 HttpRequest Timeout."""

    status_code = 408
    default_detail = "HttpRequest timeout"


class HttpConflict(HttpException):
    """409 Conflict."""

    status_code = 409
    default_detail = "Conflict"


class HttpGone(HttpException):
    """410 Gone."""

    status_code = 410
    default_detail = "Gone"


class HttpLengthRequired(HttpException):
    """411 Length Required."""

    status_code = 411
    default_detail = "Length required"


class HttpPreconditionFailed(HttpException):
    """412 Precondition Failed."""

    status_code = 412
    default_detail = "Precondition failed"


class HttpPayloadTooLarge(HttpException):
    """413 Payload Too Large."""

    status_code = 413
    default_detail = "Payload too large"


class HttpUriTooLong(HttpException):
    """414 URI Too Long."""

    status_code = 414
    default_detail = "URI too long"


class HttpUnsupportedMediaType(HttpException):
    """415 Unsupported Media Type."""

    status_code = 415
    default_detail = "Unsupported media type"


class HttpRangeNotSatisfiable(HttpException):
    """416 Range Not Satisfiable."""

    status_code = 416
    default_detail = "Range not satisfiable"


class HttpExpectationFailed(HttpException):
    """417 Expectation Failed."""

    status_code = 417
    default_detail = "Expectation failed"


class HttpUnprocessableEntity(HttpException):
    """422 Unprocessable Entity."""

    status_code = 422
    default_detail = "Unprocessable entity"


class HttpTooManyRequests(HttpException):
    """429 Too Many Requests."""

    status_code = 429
    default_detail = "Too many requests"


# 5xx Server Errors


class HttpInternalServerError(HttpException):
    """500 Internal Server Error."""

    status_code = 500
    default_detail = "Internal server error"


class HttpNotImplemented(HttpException):
    """501 Not Implemented."""

    status_code = 501
    default_detail = "Not implemented"


class HttpBadGateway(HttpException):
    """502 Bad Gateway."""

    status_code = 502
    default_detail = "Bad gateway"


class HttpServiceUnavailable(HttpException):
    """503 Service Unavailable."""

    status_code = 503
    default_detail = "Service unavailable"


class HttpGatewayTimeout(HttpException):
    """504 Gateway Timeout."""

    status_code = 504
    default_detail = "Gateway timeout"


class HttpVersionNotSupported(HttpException):
    """505 HTTP Version Not Supported."""

    status_code = 505
    default_detail = "HTTP version not supported"


# Convenience aliases, for the names these are often known by
HttpNotFound = Http404
PermissionDenied = HttpForbidden
BadRequest = HttpBadRequest
