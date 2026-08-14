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


"""How the server answers as the load and the process count change.

Run: python benchmark/sweep.py

`bench.py` fixes the load and varies the work. This does the opposite: one
scenario, swept over client concurrency, then over runtime threads, then over
worker processes.

The three sweeps answer different questions. Concurrency says where a single
worker stops climbing, which is where its event loop is full. Runtime threads
say how much of the socket work can be moved off that loop's way. Workers say
what the machine will do, since each one is a separate process with an
interpreter of its own.
"""

from __future__ import annotations

import argparse
import sys

from harness import (
    CEILING_MARGIN,
    Measurement,
    measure,
    measure_ceiling,
    print_environment,
    report_ceiling_drift,
    serving,
)
from payloads import SCENARIOS_BY_NAME, Scenario

PORT = 8140

CONNECTION_LEVELS = [1, 8, 32, 64, 128, 256]
THREAD_LEVELS = [1, 2, 4]
WORKER_LEVELS = [1, 2, 4]


def sweep_connections(
    scenario: Scenario, levels: list[int], threads: int, duration: float
) -> dict[int, Measurement]:
    results: dict[int, Measurement] = {}
    with serving("plain", PORT) as base_url:
        for level in levels:
            measure(base_url, scenario, level, threads, 2.0)
            results[level] = measure(base_url, scenario, level, threads, duration)
    return results


def sweep_servers(
    scenario: Scenario,
    levels: list[int],
    connections: int,
    threads: int,
    duration: float,
    workers: bool,
) -> dict[int, Measurement]:
    results: dict[int, Measurement] = {}
    for level in levels:
        arguments = {"workers": level} if workers else {"runtime_threads": level}
        with serving("plain", PORT, **arguments) as base_url:
            measure(base_url, scenario, connections, threads, 2.0)
            results[level] = measure(base_url, scenario, connections, threads, duration)
    return results


def print_sweep(
    title: str, column: str, results: dict[int, Measurement], ceiling: float | None
) -> bool:
    print(title)
    header = f"{column:<10}{'throughput':>18}{'p50':>11}{'p99':>11}"
    print(header)
    print("-" * len(header))
    capped = False
    for level, measurement in results.items():
        at_ceiling = (
            ceiling is not None and measurement.requests_per_second >= ceiling * CEILING_MARGIN
        )
        capped = capped or at_ceiling
        print(
            f"{level:<10}{measurement.requests_per_second:>11,.0f} req/s"
            f"{'*' if at_ceiling else ' '}"
            f"{measurement.latency_p50_ms:>8.2f} ms{measurement.latency_p99_ms:>8.2f} ms"
        )
    print()
    return capped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="json")
    parser.add_argument("-t", "--threads", type=int, default=4)
    parser.add_argument("-d", "--duration", type=float, default=8.0)
    parser.add_argument("-c", "--connections", type=int, default=128)
    parser.add_argument("--no-ceiling", action="store_true")
    arguments = parser.parse_args()

    if arguments.scenario not in SCENARIOS_BY_NAME:
        raise SystemExit(f"unknown scenario {arguments.scenario!r}")
    scenario = SCENARIOS_BY_NAME[arguments.scenario]

    print_environment(
        {
            "scenario": f"{scenario.method} {scenario.path} — {scenario.description}",
            "load": f"wrk over {arguments.threads} threads, {arguments.duration:g}s per point",
        }
    )

    ceiling = None
    if not arguments.no_ceiling:
        print("measuring what this machine can be driven at...", file=sys.stderr)
        ceiling = measure_ceiling(arguments.connections, arguments.threads)

    print("sweeping client concurrency...", file=sys.stderr)
    by_connections = sweep_connections(
        scenario, CONNECTION_LEVELS, arguments.threads, arguments.duration
    )
    print("sweeping runtime threads...", file=sys.stderr)
    by_threads = sweep_servers(
        scenario, THREAD_LEVELS, arguments.connections, arguments.threads, arguments.duration, False
    )
    print("sweeping workers...", file=sys.stderr)
    by_workers = sweep_servers(
        scenario, WORKER_LEVELS, arguments.connections, arguments.threads, arguments.duration, True
    )

    steady = True
    if ceiling is not None:
        steady = report_ceiling_drift(
            ceiling, measure_ceiling(arguments.connections, arguments.threads)
        )
    print()

    capped = print_sweep("Client concurrency, one worker", "conns", by_connections, ceiling)
    capped |= print_sweep(
        f"Runtime threads, {arguments.connections} connections", "threads", by_threads, ceiling
    )
    capped |= print_sweep(
        f"Worker processes, {arguments.connections} connections", "workers", by_workers, ceiling
    )

    if capped:
        print(
            f"* at the {ceiling:,.0f} req/s this machine can be driven at, so the server was\n"
            "  not the limit and the figure is a lower bound on what it would sustain."
        )
    return 0 if steady else 2


if __name__ == "__main__":
    sys.exit(main())
