from abc import ABC
from typing import Any, Awaitable, Callable

from nitro.protocols.http import Request, Response
from nitro.protocols.websocket import WebSocket
from nitro.protocols.webtransport import WebTransportSession


class Middleware(ABC):
    """
    Base middleware class for Nitro framework.

    Middleware can implement protocol-specific handlers that are called
    based on the connection type. If a protocol-specific method is not
    implemented, the middleware is skipped for that protocol.

    Example:
        class LoggingMiddleware(Middleware):
            async def __http__(self, request: Request, call_next):
                start_time = time.time()
                response = await call_next(request)
                duration = time.time() - start_time
                logger.info(f'Request to {request.url.path} took {duration}s')
                return response

            async def __websocket__(self, websocket: WebSocket, call_next):
                logger.info(f'WebSocket connection to {websocket.url.path}')
                await call_next(websocket)
    """

    def __init__(self, app: Any | None = None):
        """
        Initialize middleware.

        Args:
            app: Optional application instance for accessing configuration
        """
        self.app = app

    async def __aenter__(self):
        """
        Async context manager entry point.
        Called before processing each request/connection.
        """
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """
        Async context manager exit point.
        Called after processing each request/connection.

        Args:
            exc_type: Exception type if an error occurred
            exc_val: Exception value if an error occurred
            exc_tb: Exception traceback if an error occurred

        Returns:
            None to propagate exceptions, True to suppress them
        """
        return None

    async def __http__(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """
        Handle HTTP request/response cycle.

        Args:
            request: The incoming HTTP request
            call_next: Callable to invoke the next middleware or route handler

        Returns:
            HTTP response

        Note:
            This method is optional. If not implemented, the middleware
            will be skipped for HTTP requests.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement __http__"
        )

    async def __websocket__(
        self, websocket: WebSocket, call_next: Callable[[WebSocket], Awaitable[None]]
    ) -> None:
        """
        Handle WebSocket connection lifecycle.

        Args:
            websocket: The WebSocket connection
            call_next: Callable to invoke the next middleware or route handler

        Note:
            This method is optional. If not implemented, the middleware
            will be skipped for WebSocket connections.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement __websocket__"
        )

    async def __webtransport__(
        self,
        session: WebTransportSession,
        call_next: Callable[[WebTransportSession], Awaitable[None]],
    ) -> None:
        """
        Handle WebTransport session lifecycle.

        Args:
            session: The WebTransport session
            call_next: Callable to invoke the next middleware or route handler

        Note:
            This method is optional. If not implemented, the middleware
            will be skipped for WebTransport sessions.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement __webtransport__"
        )

    async def __call__(
        self, connection: Any, call_next: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """
        Universal handler for all protocols.

        This method is used as a fallback if a protocol-specific method
        is not implemented. It receives a generic connection object and
        must handle it appropriately.

        Args:
            connection: The connection object (Request, WebSocket, or WebTransportSession)
            call_next: Callable to invoke the next middleware or route handler

        Returns:
            The result of the next middleware/handler

        Note:
            This method is optional. If neither this nor a protocol-specific
            method is implemented, the middleware will be skipped.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement __call__"
        )

    def has_protocol_support(self, protocol: str) -> bool:
        """
        Check if this middleware supports a specific protocol.

        Args:
            protocol: Protocol name ('http', 'websocket', 'webtransport')

        Returns:
            True if the middleware implements a handler for this protocol
        """
        method_name = f"__{protocol}__"

        # Check if protocol-specific method is implemented
        if hasattr(self, method_name):
            method = getattr(self, method_name)
            # Check if it's actually implemented (not just the base class version)
            try:
                # Get the implementation from the instance's class
                impl = getattr(self.__class__, method_name, None)
                if impl is not None and impl is not getattr(
                    Middleware, method_name, None
                ):
                    return True
            except AttributeError:
                pass

        # Check if universal __call__ is implemented
        if hasattr(self, "__call__"):
            try:
                impl = getattr(self.__class__, "__call__", None)
                if impl is not None and impl is not getattr(
                    Middleware, "__call__", None
                ):
                    return True
            except AttributeError:
                pass

        return False


class MiddlewareProtocol:
    """Protocol identifier for middleware handlers."""

    HTTP = "http"
    WEBSOCKET = "websocket"
    WEBTRANSPORT = "webtransport"
