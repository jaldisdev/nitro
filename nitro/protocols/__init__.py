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

"""Requests, responses, sockets, sessions, and the exceptions that become
responses.

Everything a handler is given or hands back is reachable from here, so a
project imports one name rather than remembering which module each lives in.
"""

from nitro.protocols.exceptions import (
    ExceptionHandlerRegistry,
    Http404,
    HttpBadGateway,
    HttpBadRequest,
    HttpConflict,
    HttpException,
    HttpExpectationFailed,
    HttpForbidden,
    HttpGatewayTimeout,
    HttpGone,
    HttpInternalServerError,
    HttpLengthRequired,
    HttpMethodNotAllowed,
    HttpNotAcceptable,
    HttpNotImplemented,
    HttpPayloadTooLarge,
    HttpPaymentRequired,
    HttpPreconditionFailed,
    HttpProxyAuthenticationRequired,
    HttpRangeNotSatisfiable,
    HttpRequestTimeout,
    HttpServiceUnavailable,
    HttpTooManyRequests,
    HttpUnauthorized,
    HttpUnprocessableEntity,
    HttpUnsupportedMediaType,
    HttpUriTooLong,
    HttpVersionNotSupported,
)
from nitro.protocols.http import (
    URL,
    Address,
    FileResponse,
    FormData,
    HTMLResponse,
    HttpRequest,
    HttpResponse,
    JSONResponse,
    PlainTextResponse,
    QueryParams,
    RedirectResponse,
    State,
    StreamingResponse,
    TemplateResponse,
    UploadFile,
)
from nitro.protocols.websocket import WebSocket, WebSocketDisconnect, WebSocketState
from nitro.protocols.webtransport import (
    WebTransportDisconnect,
    WebTransportSession,
    WebTransportState,
    WebTransportStream,
)

__all__ = [
    "URL",
    "Address",
    "ExceptionHandlerRegistry",
    "FileResponse",
    "FormData",
    "HTMLResponse",
    "Http404",
    "HttpBadGateway",
    "HttpBadRequest",
    "HttpConflict",
    "HttpException",
    "HttpExpectationFailed",
    "HttpForbidden",
    "HttpGatewayTimeout",
    "HttpGone",
    "HttpInternalServerError",
    "HttpLengthRequired",
    "HttpMethodNotAllowed",
    "HttpNotAcceptable",
    "HttpNotImplemented",
    "HttpPayloadTooLarge",
    "HttpPaymentRequired",
    "HttpPreconditionFailed",
    "HttpProxyAuthenticationRequired",
    "HttpRangeNotSatisfiable",
    "HttpRequest",
    "HttpRequestTimeout",
    "HttpResponse",
    "HttpServiceUnavailable",
    "HttpTooManyRequests",
    "HttpUnauthorized",
    "HttpUnprocessableEntity",
    "HttpUnsupportedMediaType",
    "HttpUriTooLong",
    "HttpVersionNotSupported",
    "JSONResponse",
    "PlainTextResponse",
    "QueryParams",
    "RedirectResponse",
    "State",
    "StreamingResponse",
    "TemplateResponse",
    "UploadFile",
    "WebSocket",
    "WebSocketDisconnect",
    "WebSocketState",
    "WebTransportDisconnect",
    "WebTransportSession",
    "WebTransportState",
    "WebTransportStream",
]
