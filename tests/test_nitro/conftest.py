"""Support for driving a real server process from the tests."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import pytest

STARTUP_TIMEOUT = 30.0
SHUTDOWN_TIMEOUT = 15.0
_ADDRESS = re.compile(r"Serving on https?://(?P<host>[^\s]+):(?P<port>\d+)")


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode()


@dataclass
class RunningServer:
    process: subprocess.Popen[str]
    port: int
    log: list[str]

    def url(self, path: str = "/") -> str:
        # By name, as a client would. The server binds every address the name
        # resolves to, on one port.
        return f"http://localhost:{self.port}{path}"

    def request(
        self,
        path: str = "/",
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> Response:
        request = urllib.request.Request(
            self.url(path), data=data, method=method, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as answer:
                return Response(answer.status, answer.read(), dict(answer.headers))
        except urllib.error.HTTPError as error:
            return Response(error.code, error.read(), dict(error.headers))

    def stop(self, sig: int = signal.SIGTERM) -> int:
        if self.process.poll() is None:
            self.process.send_signal(sig)
        try:
            self.process.wait(timeout=SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=SHUTDOWN_TIMEOUT)
            raise AssertionError("the server did not stop when asked") from None
        self._drain_output()
        return self.process.returncode

    def _drain_output(self) -> None:
        if self.process.stdout is not None:
            self.log.extend(self.process.stdout.read().splitlines())

    @property
    def output(self) -> str:
        return "\n".join(self.log)


def _wait_for_address(process: subprocess.Popen[str], log: list[str]) -> int:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    assert process.stdout is not None

    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            if process.poll() is not None:
                raise AssertionError(f"the server exited during startup:\n{''.join(log)}")
            continue
        log.append(line.rstrip())
        found = _ADDRESS.search(line)
        # A host name resolves to several addresses; the loopback one is the
        # one a test can reliably connect to.
        if found and found.group("host") in {"127.0.0.1", "::1", "localhost"}:
            return int(found.group("port"))

    raise AssertionError(f"the server did not report an address:\n{''.join(log)}")


@pytest.fixture
def server_factory(tmp_path):
    """Start the server on a kernel-chosen port and yield a handle to it.

    `script` runs the written file directly rather than serving it through the
    command line, which is how an application that calls `app.serve()` for
    itself is started.
    """
    running: list[RunningServer] = []

    def start(
        source: str, *arguments: str, module: str = "app", script: bool = False
    ) -> RunningServer:
        (tmp_path / f"{module}.py").write_text(textwrap.dedent(source))

        command = (
            [sys.executable, f"{module}.py"]
            if script
            else [sys.executable, "-m", "nitro.cli", f"{module}:app", "-p", "0"]
        )
        environment = dict(os.environ, PYTHONUNBUFFERED="1")
        process = subprocess.Popen(
            [*command, *arguments],
            cwd=tmp_path,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        log: list[str] = []
        try:
            port = _wait_for_address(process, log)
        except AssertionError:
            process.kill()
            process.wait(timeout=SHUTDOWN_TIMEOUT)
            raise

        handle = RunningServer(process, port, log)
        running.append(handle)
        return handle

    yield start

    for handle in running:
        if handle.process.poll() is None:
            with contextlib.suppress(Exception):
                handle.stop(signal.SIGKILL)


@pytest.fixture
def hello_app() -> str:
    return """
        from nitro import Nitro
        from nitro.protocols import PlainTextResponse

        app = Nitro(http="1", log_level="warning")

        @app.route("/")
        async def index(request):
            return PlainTextResponse("hello")
    """
