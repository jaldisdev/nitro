from typing import Any, Literal

from nitro.protocols.exceptions import HttpMethodNotAllowed
from nitro.protocols.http import Request, Response
from nitro.protocols.websocket import WebSocket, WebSocketDisconnect
from nitro.protocols.webtransport import WebTransportDisconnect, WebTransportSession


class HTTPEndpoint:
    """
    Base class for HTTP endpoints with method-based dispatch.
    """

    async def __call__(self, request: Request) -> Response:
        """Dispatch to method handler."""
        return await self.dispatch(request)

    async def dispatch(self, request: Request, **params) -> Response:
        """Dispatch request to appropriate method handler."""
        handler = getattr(self, request.method.lower(), None)

        # HEAD falls back to GET per RFC 9110 §9.3.2
        if handler is None and request.method.upper() == "HEAD":
            handler = getattr(self, "get", None)

        if handler is None:
            return await self.method_not_allowed(request)

        response = await handler(request, **params)

        if request.method.upper() == "HEAD":
            if "content-length" not in response._headers:
                response._headers["content-length"] = str(len(response._body))
            response._body = b""

        return response

    async def method_not_allowed(self, request: Request) -> Response:
        """Called when HTTP method is not implemented."""
        allowed = [
            m.upper()
            for m in ("get", "post", "put", "patch", "delete", "head", "options")
            if hasattr(self, m)
        ]
        if "GET" in allowed and "HEAD" not in allowed:
            allowed.insert(allowed.index("GET") + 1, "HEAD")
        raise HttpMethodNotAllowed(
            detail=f"Method {request.method} not allowed",
            headers={"Allow": ", ".join(allowed)} if allowed else {},
        )


class WebSocketEndpoint:
    """
    Base class for WebSocket endpoints with automatic encoding.
    """

    encoding: Literal["text", "bytes", "json"] = "text"

    async def __call__(self, websocket: WebSocket):
        """Handle WebSocket lifecycle."""
        return await self.dispatch(websocket)

    async def dispatch(self, websocket: WebSocket, **params) -> None:
        """Dispatch WebSocket events to lifecycle hooks."""
        await self.on_connect(websocket, **params)

        close_code = 1000
        try:
            async for data in websocket.iter(self.encoding):
                await self.on_receive(websocket, data, **params)
        except WebSocketDisconnect as exc:
            close_code = exc.code
        finally:
            await self.on_disconnect(websocket, close_code, **params)

    async def on_connect(self, websocket: WebSocket, **params) -> None:
        """Override to handle WebSocket connection."""
        await websocket.accept()

    async def on_receive(self, websocket: WebSocket, data: Any, **params) -> None:
        """Override to handle incoming messages."""
        pass

    async def on_disconnect(
        self, websocket: WebSocket, close_code: int, **params
    ) -> None:
        """Override to handle disconnection."""
        pass


class WebTransportEndpoint:
    """
    Base class for WebTransport endpoints with support for datagrams and streams.
    """

    encoding: Literal["bytes", "text", "json"] = "bytes"
    use_streams: bool = False

    async def __call__(self, session: WebTransportSession):
        """Handle WebTransport lifecycle."""
        return await self.dispatch(session)

    async def dispatch(self, session: WebTransportSession, **params) -> None:
        """Dispatch WebTransport events to lifecycle hooks."""
        await self.on_connect(session, **params)

        close_code = 0
        try:
            if self.use_streams:
                import asyncio

                async def handle_datagrams():
                    async for data in session.iter_datagrams(self.encoding):
                        await self.on_datagram(session, data, **params)

                async def handle_streams():
                    while True:
                        stream = await session.receive_stream()
                        asyncio.create_task(self.on_stream(session, stream, **params))

                await asyncio.gather(
                    handle_datagrams(), handle_streams(), return_exceptions=True
                )
            else:
                async for data in session.iter_datagrams(self.encoding):
                    await self.on_datagram(session, data, **params)
        except WebTransportDisconnect as exc:
            close_code = exc.code
        finally:
            await self.on_disconnect(session, close_code, **params)

    async def on_connect(self, session: WebTransportSession, **params) -> None:
        """Override to handle session connection."""
        await session.accept()

    async def on_datagram(
        self, session: WebTransportSession, data: Any, **params
    ) -> None:
        """Override to handle incoming datagrams."""
        pass

    async def on_stream(
        self, session: WebTransportSession, stream: Any, **params
    ) -> None:
        """Override to handle incoming streams (only called if use_streams=True)."""
        pass

    async def on_disconnect(
        self, session: WebTransportSession, close_code: int, **params
    ) -> None:
        """Override to handle disconnection."""
        pass
