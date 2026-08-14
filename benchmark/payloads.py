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


"""What the benchmark asks the server for.

Every scenario is defined once, here, so that the application under test and the
tools that drive it cannot drift apart on what a request is meant to produce.
The set is chosen to separate costs rather than to flatter: a ten-byte response
is almost entirely per-request overhead, a hundred-kilobyte one is almost
entirely the socket, and the sleeping ones are neither.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import orjson

BASE_DIR = Path(__file__).resolve().parent

PLAINTEXT_TYPE = "text/plain; charset=utf-8"
ASSET_TYPE = "image/jpeg"

PLAINTEXT_SIZES: dict[str, int] = {
    "b10": 10,
    "b1k": 1024,
    "b10k": 10 * 1024,
    "b100k": 100 * 1024,
}

PLAINTEXT_BODIES: dict[str, bytes] = {name: b"x" * size for name, size in PLAINTEXT_SIZES.items()}

JSON_DOCUMENT: dict[str, object] = {
    "message": "Hello, World!",
    "id": 4711,
    "active": True,
    "score": 12.5,
    "tags": ["alpha", "beta", "gamma"],
    "author": {"id": 1, "name": "Ada Lovelace", "email": "ada@example.com"},
}

JSON_BYTES = orjson.dumps(JSON_DOCUMENT)

ECHO_BODY = b"x" * 1024

#: Awaited work, added to an otherwise empty handler. A handler that awaits
#: nothing is the only place where the server's own cost is the whole cost, and
#: these say how quickly that stops being true.
SLEEP_STEPS: dict[str, float] = {
    "sleep0": 0.0,
    "sleep1": 0.001,
    "sleep5": 0.005,
    "sleep10": 0.010,
}

ASSET_PATH = BASE_DIR / "assets" / "media.bin"
ASSET_SIZE = 50 * 1024


def user_document(user_id: int) -> dict[str, object]:
    """The body of the path-parameter route.

    Takes the converted integer rather than the captured text, so a conversion
    that silently failed would show up as `"4711"` in the response rather than
    passing for the same work.
    """
    return {"id": user_id, "name": "Ada Lovelace", "email": "ada@example.com"}


@dataclasses.dataclass(frozen=True)
class Scenario:
    name: str
    path: str
    description: str
    method: str = "GET"
    body: bytes | None = None


SCENARIOS: list[Scenario] = [
    Scenario("b10", "/b10", "10 B of text: almost entirely per-request cost"),
    Scenario("b1k", "/b1k", "1 KiB of text"),
    Scenario("b10k", "/b10k", "10 KiB of text"),
    Scenario("b100k", "/b100k", "100 KiB of text: almost entirely the socket"),
    Scenario("json", "/json", "a document serialised on every request"),
    Scenario("route", "/users/4711", "a path parameter, matched and converted"),
    Scenario("echo", "/echo", "a 1 KiB request body read and sent back", "POST", ECHO_BODY),
    Scenario("file", "/file", f"a {ASSET_SIZE // 1024} KiB file from disk"),
    *(
        Scenario(name, f"/{name}", f"{seconds * 1000:.0f} ms of awaited nothing")
        for name, seconds in SLEEP_STEPS.items()
    ),
]

SCENARIOS_BY_NAME: dict[str, Scenario] = {scenario.name: scenario for scenario in SCENARIOS}


def ensure_asset() -> Path:
    """The file the file-serving scenario sends, written on first use.

    Generated rather than committed: nothing about its contents matters except
    its length, and a repository is no place for fifty kilobytes of that.
    """
    if ASSET_PATH.exists() and ASSET_PATH.stat().st_size == ASSET_SIZE:
        return ASSET_PATH
    ASSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    pattern = bytes(range(256))
    ASSET_PATH.write_bytes((pattern * (ASSET_SIZE // len(pattern) + 1))[:ASSET_SIZE])
    return ASSET_PATH
