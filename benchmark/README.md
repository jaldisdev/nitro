# Benchmarking Nitro

Three tools, one application, and a habit of not believing the machine.

```sh
pip install -e ".[benchmark]"   # uvloop and httpx
brew install wrk                # or apt-get install wrk

python benchmark/bench.py       # what each kind of request costs
python benchmark/probe.py       # where that cost is, layer by layer
python benchmark/sweep.py       # how it answers as load and processes change
```

The load is driven by [wrk](https://github.com/wg/wrk), out of process and
written in C, because at these rates a Python client is the first thing to
become the bottleneck.

## What it measures

`bench.py` serves every scenario from one process and reports what each
sustained. The scenarios are chosen to separate costs rather than to produce a
large number:

| Scenario | What it isolates |
|---|---|
| `b10` | Almost entirely per-request overhead: ten bytes of body. |
| `b1k`, `b10k`, `b100k` | Where the socket starts to dominate the request. |
| `json` | Serialising a document on every request. |
| `route` | Matching a path parameter and converting it. |
| `echo` | Reading a request body and sending it back. |
| `file` | Serving a file from disk. |
| `sleep0` … `sleep10` | A handler that awaits 0, 1, 5 or 10 ms. |

The `sleep` family is the most useful row in the table and the least
interesting number. A server's own cost is the whole cost only when the handler
does nothing; the moment one awaits a database, a cache or another service, that
cost is a rounding error on top. Comparing `sleep0` with `sleep1` says how
quickly that happens.

`probe.py` answers a different question: not what a request costs, but where.
It serves the same request through applications that differ by one layer —
nothing, then routing and responses, then middleware, then injected
dependencies — and reports what each layer adds. The first row answers before
any of the framework runs, so it is this server's floor: no work on the
framework will go below it.

`sweep.py` varies the load instead of the work. Client concurrency finds where
one worker stops climbing, which is where its event loop is full. Runtime
threads say how much socket work can be kept out of that loop's way. Workers say
what the machine will do, since each is a separate process with its own
interpreter.

## Reading the numbers honestly

**A result at the machine's ceiling is not a result.** Every tool measures how
fast this machine can be driven at all — four workers answering ten bytes before
the framework runs — and marks any figure within 10% of it with a `*`. Those
rows measured the machine and the load generator, not the server. This is not a
hypothetical: on a laptop also running an editor and a container stack, every
scenario in this suite reported the same number, and that number was the
laptop's.

**A machine that moves invalidates the run.** The ceiling is measured again
afterwards, and if it moved more than 10% the tools say so and exit non-zero.
Numbers taken while something else started or stopped do not compare with each
other, however tidy the table looks.

**Compare rows, not runs.** Absolute figures depend on the machine, the kernel
and what else is on it; on the same machine on the same day they have varied by
20% between runs. The relationship between rows is far more stable than any row
is.

For a measurement worth quoting: a quiet machine with nothing else running,
enough cores that the load generator is not competing with the server for them,
and the server pinned away from the client — `taskset -c 0-3` for one and
`-c 4-7` for the other on an eight-core box — with the results confirmed by
running twice.

## Changing what is served

Scenarios live in `payloads.py` and the application in `app.py`, which builds
five variants from the same routes. Adding a scenario means adding a `Scenario`
to the list and a route that answers it; both tools pick it up from there.
