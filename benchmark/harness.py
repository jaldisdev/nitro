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


"""Running a server, driving it with wrk, and reading the numbers back.

Everything that is not a measurement lives here, so the tools differ only in
what they ask for. Two habits are built in because both were learned the hard
way.

The first is the ceiling. Before and after every run the harness measures how
fast this machine can be driven at all, by loading the cheapest server the
suite has. A result at that ceiling is not a measurement of the server, and is
marked so it cannot be read as one.

The second is drift. If the ceiling moves more than a little across a run, the
machine was doing something else for part of it and the numbers in between do
not compare with each other. The tools say so and exit non-zero rather than
printing a tidy table of nonsense.
"""

from __future__ import annotations

import dataclasses
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from payloads import SCENARIOS_BY_NAME, Scenario

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"

LOOP = os.environ.get("BENCH_LOOP", "uvloop")
WRK = shutil.which("wrk")

#: A measurement this close to the machine's ceiling is a measurement of the
#: machine.
CEILING_MARGIN = 0.9

#: How far the ceiling may move across a run before the run is suspect.
DRIFT_TOLERANCE = 0.1

CEILING_PORT = 8199


@dataclasses.dataclass(frozen=True)
class Measurement:
    requests_per_second: float
    latency_p50_ms: float
    latency_p99_ms: float
    requests: int
    failures: int


@contextmanager
def serving(
    variant: str = "plain",
    port: int = 8100,
    workers: int = 1,
    runtime_threads: int = 2,
) -> Iterator[str]:
    """Run one variant of the application for the duration of the block."""
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"{variant}.log"
    command = [
        sys.executable,
        str(BASE_DIR / "app.py"),
        "--variant",
        variant,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--workers",
        str(workers),
        "--runtime-threads",
        str(runtime_threads),
    ]
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            command,
            cwd=BASE_DIR,
            env={**os.environ, "BENCH_LOOP": LOOP},
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            wait_until_ready(f"http://127.0.0.1:{port}", process, log_path)
            yield f"http://127.0.0.1:{port}"
        finally:
            stop(process)


def wait_until_ready(
    base_url: str,
    process: subprocess.Popen,
    log_path: Path,
    timeout: float = 30.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(
                f"the server exited with {process.returncode} before serving:\n"
                f"{log_path.read_text()[-2000:]}"
            )
        try:
            response = httpx.get(f"{base_url}/b10", timeout=1.0)
        except httpx.TransportError:
            time.sleep(0.05)
            continue
        if response.status_code == 200:
            return
        raise SystemExit(f"{base_url}/b10 answered {response.status_code}")
    raise SystemExit(f"{base_url} never became ready; see {log_path}")


def stop(process: subprocess.Popen) -> None:
    """Stop the whole process group, so no worker outlives its parent.

    A worker that survives keeps the port bound, which the next run would meet
    as an unexplained bind error one measurement later, far from the cause.
    """
    if process.poll() is not None:
        return
    group = os.getpgid(process.pid)
    os.killpg(group, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(group, signal.SIGKILL)
        process.wait(timeout=10)


UNIT_MILLISECONDS = {"us": 0.001, "ms": 1.0, "s": 1000.0, "m": 60_000.0}

REQUESTS_PER_SECOND = re.compile(r"^Requests/sec:\s+([\d.]+)", re.MULTILINE)
PERCENTILE = re.compile(r"^\s+(\d+)%\s+([\d.]+)(us|ms|s|m)\s*$", re.MULTILINE)
TOTAL_REQUESTS = re.compile(r"^\s+(\d+) requests in ", re.MULTILINE)
NON_SUCCESS = re.compile(r"^\s+Non-2xx or 3xx responses: (\d+)", re.MULTILINE)
SOCKET_ERRORS = re.compile(
    r"^\s+Socket errors: connect (\d+), read (\d+), write (\d+), timeout (\d+)", re.MULTILINE
)


def measure(
    base_url: str,
    scenario: Scenario,
    connections: int,
    threads: int,
    duration: float,
) -> Measurement:
    """Drive one scenario and read wrk's report."""
    if WRK is None:
        raise SystemExit("wrk is not installed, and the benchmark needs it")

    command = [
        WRK,
        "--threads",
        str(min(threads, connections)),
        "--connections",
        str(connections),
        "--duration",
        f"{duration:g}s",
        "--latency",
    ]
    script = lua_script(scenario) if scenario.method != "GET" else None
    if script is not None:
        command += ["--script", str(script)]
    command.append(f"{base_url}{scenario.path}")

    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=True)
    finally:
        if script is not None:
            script.unlink(missing_ok=True)
    return parse_wrk(completed.stdout)


def lua_script(scenario: Scenario) -> Path:
    """A wrk script for a scenario that is not a plain GET.

    wrk has no flag for a request body, so the method and body are set from Lua
    instead, once, at the top of each of its threads.
    """
    body = scenario.body or b""
    descriptor, name = tempfile.mkstemp(suffix=".lua")
    os.close(descriptor)
    path = Path(name)
    path.write_text(
        f'wrk.method = "{scenario.method}"\n'
        f'wrk.body = string.rep("x", {len(body)})\n'
        'wrk.headers["Content-Type"] = "application/octet-stream"\n'
    )
    return path


def parse_wrk(output: str) -> Measurement:
    rate = REQUESTS_PER_SECOND.search(output)
    total = TOTAL_REQUESTS.search(output)
    if rate is None or total is None:
        raise SystemExit(f"could not read wrk's output:\n{output}")

    percentiles = {
        int(percent): float(value) * UNIT_MILLISECONDS[unit]
        for percent, value, unit in PERCENTILE.findall(output)
    }

    failures = 0
    non_success = NON_SUCCESS.search(output)
    if non_success is not None:
        failures += int(non_success.group(1))
    socket_errors = SOCKET_ERRORS.search(output)
    if socket_errors is not None:
        failures += sum(int(group) for group in socket_errors.groups())

    return Measurement(
        requests_per_second=float(rate.group(1)),
        latency_p50_ms=percentiles.get(50, float("nan")),
        latency_p99_ms=percentiles.get(99, float("nan")),
        requests=int(total.group(1)),
        failures=failures,
    )


def measure_ceiling(connections: int = 64, threads: int = 4, duration: float = 6.0) -> float:
    """The highest rate this machine can be driven at, whatever is serving.

    Measured against the cheapest server the suite has — four workers answering
    ten bytes from memory before the framework runs — so what it reports is a
    property of the machine and the load generator rather than of an
    application.
    """
    scenario = SCENARIOS_BY_NAME["b10"]
    with serving("bypass", CEILING_PORT, workers=4) as base_url:
        measure(base_url, scenario, connections, threads, 2.0)
        return measure(base_url, scenario, connections, threads, duration).requests_per_second


def report_ceiling_drift(before: float, after: float) -> bool:
    """Say whether the machine held still, and complain loudly when it did not."""
    drift = abs(after - before) / max(before, after)
    print(
        f"{'machine ceiling':<18}{before:,.0f} req/s before the run, "
        f"{after:,.0f} after ({drift * 100:.0f}% drift)"
    )
    if drift <= DRIFT_TOLERANCE:
        return True
    print(
        f"\nthe machine did not hold still: its ceiling moved {drift * 100:.0f}% across the\n"
        "run, so these numbers are not comparable with each other. Close what else is\n"
        "running and measure again.",
        file=sys.stderr,
    )
    return False


def print_environment(extra: dict[str, str] | None = None) -> None:
    import nitro

    details = {
        "nitro": nitro.__version__,
        "python": f"{platform.python_version()} ({platform.machine()})",
        "loop": LOOP,
        "wrk": wrk_version(),
        "load": load_average(),
    }
    for name, value in {**details, **(extra or {})}.items():
        print(f"{name:<18}{value}")
    print()


def wrk_version() -> str:
    if WRK is None:
        return "not installed"
    completed = subprocess.run([WRK, "--version"], capture_output=True, text=True)
    return (completed.stdout or completed.stderr).splitlines()[0].strip()


def load_average() -> str:
    one, five, fifteen = os.getloadavg()
    return f"{one:.2f}, {five:.2f}, {fifteen:.2f} over 1/5/15 min on {os.cpu_count()} cores"


def report_failures(scenario: Scenario, measurement: Measurement) -> None:
    if measurement.failures:
        print(
            f"  warning: {measurement.failures} of "
            f"{measurement.requests + measurement.failures} requests to {scenario.path} "
            "failed or went unanswered",
            file=sys.stderr,
        )
