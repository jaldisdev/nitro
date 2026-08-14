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


"""The application the benchmark serves.

Five of them, in fact, which is what lets the tools tell the framework's cost
apart from the server's. `plain` is an ordinary Nitro application written the
way the documentation says to write one. `bypass` answers from
`__handle_http__` before any of the framework runs, so the difference between
the two is what routing, requests and responses cost. The rest add middleware
and injected dependencies on top of `plain`.

Run one directly to serve it:

    python benchmark/app.py --variant plain --port 8100
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

if os.environ.get("BENCH_LOOP", "uvloop") == "uvloop":
    import uvloop

    uvloop.install()

import payloads
from payloads import (
    ASSET_TYPE,
    JSON_BYTES,
    JSON_DOCUMENT,
    PLAINTEXT_BODIES,
    PLAINTEXT_TYPE,
    SLEEP_STEPS,
    user_document,
)

from nitro import Nitro
from nitro.di import Depends
from nitro.protocols import (
    FileResponse,
    HttpRequest,
    HttpResponse,
    JSONResponse,
    PlainTextResponse,
)
from nitro.routing import HTTPRoute

ASSET_PATH = payloads.ensure_asset()

PLAINTEXT_HEADERS = [("content-type", PLAINTEXT_TYPE)]
JSON_HEADERS = [("content-type", "application/json")]

#: What `bypass` answers with, built once so that variant does no work at all.
PREBUILT: dict[str, tuple[list[tuple[str, str]], bytes]] = {
    f"/{name}": (PLAINTEXT_HEADERS, body) for name, body in PLAINTEXT_BODIES.items()
}
PREBUILT["/json"] = (JSON_HEADERS, JSON_BYTES)

MIDDLEWARE = [
    "nitro.middleware.common.ExceptionMiddleware",
    "nitro.middleware.common.CORSMiddleware",
    "nitro.middleware.common.SecurityHeadersMiddleware",
]


def plaintext_handler(name: str):
    body = PLAINTEXT_BODIES[name]

    async def handler(request: HttpRequest, body: bytes = body) -> HttpResponse:
        return PlainTextResponse(body)

    return handler


def sleep_handler(seconds: float):
    async def handler(request: HttpRequest, seconds: float = seconds) -> HttpResponse:
        await asyncio.sleep(seconds)
        return PlainTextResponse(PLAINTEXT_BODIES["b10"])

    return handler


async def json_document(request: HttpRequest) -> HttpResponse:
    return JSONResponse(JSON_DOCUMENT)


async def show_user(request: HttpRequest, user_id: int) -> HttpResponse:
    return JSONResponse(user_document(user_id))


async def echo(request: HttpRequest) -> HttpResponse:
    return PlainTextResponse(await request.body())


async def send_file(request: HttpRequest) -> HttpResponse:
    return FileResponse(ASSET_PATH, content_type=ASSET_TYPE)


async def get_configuration() -> dict[str, str]:
    return {"tier": "standard", "region": "eu-central-1"}


async def get_current_user(
    request: HttpRequest, configuration: dict[str, str] = Depends(get_configuration)
) -> dict[str, str]:
    return {"id": "1", "name": "Ada Lovelace", "region": configuration["region"]}


async def json_with_dependencies(
    request: HttpRequest,
    user: dict[str, str] = Depends(get_current_user),
    configuration: dict[str, str] = Depends(get_configuration),
) -> HttpResponse:
    return JSONResponse(JSON_DOCUMENT)


def routes(json_handler=json_document) -> list[HTTPRoute]:
    return [
        *(HTTPRoute(f"/{name}", plaintext_handler(name), name=name) for name in PLAINTEXT_BODIES),
        HTTPRoute("/json", json_handler, name="json"),
        HTTPRoute("/users/<int:user_id>", show_user, name="user"),
        HTTPRoute("/echo", echo, methods=["POST"], name="echo"),
        HTTPRoute("/file", send_file, name="file"),
        *(
            HTTPRoute(f"/{name}", sleep_handler(seconds), name=name)
            for name, seconds in SLEEP_STEPS.items()
        ),
    ]


class Bypass(Nitro):
    """Nitro's server with nothing of Nitro's framework in the way.

    Answering here means no route is matched, no request is built and no
    response class is involved — the floor this stack can serve at, and what
    every other variant is measured against.
    """

    async def __handle_http__(self, scope: Any, protocol: Any) -> None:
        headers, body = PREBUILT.get(scope.path, (PLAINTEXT_HEADERS, b""))
        protocol.response_bytes(200, headers, body)


VARIANTS = {
    "bypass": lambda: Bypass(routes=[]),
    "plain": lambda: Nitro(routes=routes()),
    "middleware": lambda: Nitro(routes=routes(), middleware=MIDDLEWARE),
    "dependencies": lambda: Nitro(routes=routes(json_with_dependencies)),
    "realistic": lambda: Nitro(routes=routes(json_with_dependencies), middleware=MIDDLEWARE),
}

#: What each variant adds, and to what, for reading the probe's table.
LAYERS: dict[str, tuple[str | None, str]] = {
    "bypass": (None, "no framework, but still a full trip into Python"),
    "plain": ("bypass", "routing, HttpRequest, response classes"),
    "middleware": ("plain", "three middleware"),
    "dependencies": ("plain", "two injected dependencies"),
    "realistic": ("plain", "both of the above"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=sorted(VARIANTS), default="plain")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--runtime-threads", type=int, default=2)
    arguments = parser.parse_args()

    VARIANTS[arguments.variant]().serve(
        host=arguments.host,
        port=arguments.port,
        workers=arguments.workers,
        runtime_threads=arguments.runtime_threads,
        http="1",
        websockets=False,
        webtransport=False,
        access_log=False,
        log_level="error",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
