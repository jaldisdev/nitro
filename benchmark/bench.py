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


"""What each kind of request costs.

Run: python benchmark/bench.py

One process serves the whole scenario list, and each scenario is warmed up at
the measured concurrency before it is measured, so the numbers are not the
first hundred requests of a cold interpreter. Throughput is what wrk sustained;
the latencies are its own distribution, measured per request rather than
derived from the rate.

Read the table by comparing rows rather than by their absolute values. `b10`
against `b100k` says how much of a request is overhead and how much is the
socket; `sleep0` against `sleep10` says how quickly the server's own cost stops
mattering once a handler does something.
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
    report_failures,
    serving,
)
from payloads import SCENARIOS, SCENARIOS_BY_NAME, Scenario

PORT = 8100


def run(
    scenarios: list[Scenario],
    variant: str,
    connections: int,
    threads: int,
    duration: float,
    warmup: float,
    workers: int,
    runtime_threads: int,
) -> dict[str, Measurement]:
    results: dict[str, Measurement] = {}
    with serving(variant, PORT, workers, runtime_threads) as base_url:
        for scenario in scenarios:
            measure(base_url, scenario, connections, threads, warmup)
            measurement = measure(base_url, scenario, connections, threads, duration)
            report_failures(scenario, measurement)
            results[scenario.name] = measurement
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", default="plain")
    parser.add_argument("--scenarios", default=",".join(scenario.name for scenario in SCENARIOS))
    parser.add_argument("-c", "--connections", type=int, default=64)
    parser.add_argument("-t", "--threads", type=int, default=4)
    parser.add_argument("-d", "--duration", type=float, default=10.0)
    parser.add_argument("--warmup", type=float, default=3.0)
    parser.add_argument("-w", "--workers", type=int, default=1)
    parser.add_argument("--runtime-threads", type=int, default=2)
    parser.add_argument(
        "--no-ceiling",
        action="store_true",
        help="skip measuring how fast this machine can be driven at all",
    )
    arguments = parser.parse_args()

    try:
        scenarios = [SCENARIOS_BY_NAME[name.strip()] for name in arguments.scenarios.split(",")]
    except KeyError as error:
        raise SystemExit(f"unknown scenario {error.args[0]!r}") from error

    print_environment(
        {
            "application": arguments.variant,
            "load": (
                f"wrk, {arguments.connections} connections over {arguments.threads} threads, "
                f"{arguments.duration:g}s measured after {arguments.warmup:g}s warm-up"
            ),
            "server": (
                f"{arguments.workers} worker(s), "
                f"{arguments.runtime_threads} runtime thread(s), HTTP/1.1"
            ),
        }
    )

    ceiling = None
    if not arguments.no_ceiling:
        print("measuring what this machine can be driven at...", file=sys.stderr)
        ceiling = measure_ceiling(arguments.connections, arguments.threads)

    results = run(
        scenarios,
        arguments.variant,
        arguments.connections,
        arguments.threads,
        arguments.duration,
        arguments.warmup,
        arguments.workers,
        arguments.runtime_threads,
    )

    steady = True
    if ceiling is not None:
        steady = report_ceiling_drift(
            ceiling, measure_ceiling(arguments.connections, arguments.threads)
        )
    print()

    header = f"{'scenario':<10}{'throughput':>18}{'p50':>11}{'p99':>11}  what it measures"
    print(header)
    print("-" * (len(header) + 12))

    capped = False
    for scenario in scenarios:
        measurement = results[scenario.name]
        at_ceiling = (
            ceiling is not None and measurement.requests_per_second >= ceiling * CEILING_MARGIN
        )
        capped = capped or at_ceiling
        mark = "*" if at_ceiling else " "
        print(
            f"{scenario.name:<10}{measurement.requests_per_second:>11,.0f} req/s{mark}"
            f"{measurement.latency_p50_ms:>8.2f} ms{measurement.latency_p99_ms:>8.2f} ms"
            f"  {scenario.description}"
        )

    if capped:
        print(
            f"\n* within {(1 - CEILING_MARGIN) * 100:.0f}% of the {ceiling:,.0f} req/s this "
            "machine can be driven\n  at, so the server was not the limit and its real rate is "
            "only known to be\n  at least this. Measure on a quieter machine to find out."
        )
    return 0 if steady else 2


if __name__ == "__main__":
    sys.exit(main())
