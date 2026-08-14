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


"""What each layer of the framework costs a request.

Run: python benchmark/probe.py

`bench.py` says what a request costs. This says where the cost is, by serving
the same request through applications that differ by one layer at a time:
nothing, then routing and responses, then middleware, then injected
dependencies. Each row is measured against the one it builds on, so the last
column is what that layer adds rather than what it totals.

The first row is the interesting one to argue with. It answers a request before
any of the framework runs and is therefore the floor for this server: whatever
it costs is the trip in and out of Python, and no amount of work on the
framework will go below it.
"""

from __future__ import annotations

import argparse
import sys

from app import LAYERS, VARIANTS
from harness import (
    CEILING_MARGIN,
    measure,
    measure_ceiling,
    print_environment,
    serving,
)
from payloads import SCENARIOS_BY_NAME

PORT = 8120


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="json")
    parser.add_argument("-c", "--connections", type=int, default=64)
    parser.add_argument("-t", "--threads", type=int, default=4)
    parser.add_argument("-d", "--duration", type=float, default=8.0)
    parser.add_argument("-w", "--workers", type=int, default=1)
    parser.add_argument("--runtime-threads", type=int, default=2)
    parser.add_argument("--no-ceiling", action="store_true")
    arguments = parser.parse_args()

    if arguments.scenario not in SCENARIOS_BY_NAME:
        raise SystemExit(f"unknown scenario {arguments.scenario!r}")
    scenario = SCENARIOS_BY_NAME[arguments.scenario]

    print_environment(
        {
            "probe": "what each layer of the framework costs a request",
            "scenario": f"{scenario.method} {scenario.path} — {scenario.description}",
        }
    )

    ceiling = None
    if not arguments.no_ceiling:
        print("measuring what this machine can be driven at...", file=sys.stderr)
        ceiling = measure_ceiling(arguments.connections, arguments.threads)

    order = list(VARIANTS)
    results: dict[str, float] = {}
    for offset, variant in enumerate(order):
        print(f"serving {variant}...", file=sys.stderr)
        with serving(variant, PORT + offset, arguments.workers, arguments.runtime_threads) as url:
            measure(url, scenario, arguments.connections, arguments.threads, 2.0)
            measurement = measure(
                url, scenario, arguments.connections, arguments.threads, arguments.duration
            )
            results[variant] = measurement.requests_per_second

    def per_request(variant: str) -> float:
        return 1_000_000 / results[variant]

    header = f"{'application':<14}{'throughput':>18}{'per request':>14}{'costs':>10}  what it adds"
    print(header)
    print("-" * (len(header) + 12))
    capped = False
    for variant in order:
        baseline, description = LAYERS[variant]
        added = "" if baseline is None else f"{per_request(variant) - per_request(baseline):>+9.1f}"
        at_ceiling = ceiling is not None and results[variant] >= ceiling * CEILING_MARGIN
        capped = capped or at_ceiling
        print(
            f"{variant:<14}{results[variant]:>11,.0f} req/s{'*' if at_ceiling else ' '}"
            f"{per_request(variant):>11.1f} us{added:>10}  {description}"
        )

    if capped:
        print(
            f"\n* at the {ceiling:,.0f} req/s this machine can be driven at. Every layer\n"
            "  marked this way was waiting for the machine rather than for itself, so the\n"
            "  costs beside them are not what those layers cost. Measure somewhere quieter."
        )
    else:
        print(
            "\n'costs' is what an application adds to the one it builds on, named in the last\n"
            "column. A layer that costs less than the noise between runs is a layer this\n"
            "measurement cannot see; run it twice before believing a small number."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
