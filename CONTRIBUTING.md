# Contributing to Nitro

Thanks for wanting to help.

## Getting set up

```sh
python -m venv .venv && . .venv/bin/activate
pip install maturin
maturin develop --extras dev          # builds the extension into the venv
```

You need a Rust toolchain (stable) and Python 3.13 or newer.

```sh
cargo test --workspace                # Rust
pytest                                # Python
(cd nitro-intercom && pytest)         # the standalone package
```

Tests that need Redis or Memcached skip when there is not one reachable at
`127.0.0.1:6379` / `127.0.0.1:11211`, so a checkout without them still runs
green. To run them:

```sh
docker run -d -p 6379:6379 redis:8
docker run -d -p 11211:11211 memcached:alpine
```

`emcache` publishes nothing for Python 3.14, so the Memcached tests can only
run on 3.13. CI covers that leg.

## Before you open a pull request

```sh
cargo fmt --all --check
cargo clippy --locked --workspace --all-targets -- -D warnings
ruff check nitro tests
ruff format --check nitro tests
cargo test --workspace
pytest
```

CI runs exactly these.

## How the code is written

`.rules` (symlinked as `CLAUDE.md` and `AGENTS.md`) holds the conventions, and
they are worth reading before a first change. The two that surprise people:

**Comments explain why, not what.** A comment that restates the code is noise
that goes stale. A comment that explains a decision — why the lock is taken
here, why this is a copy rather than shared — is the thing a reader cannot
recover from the code.

**Errors propagate or are handled, never swallowed.** No bare `except:`, no
`except Exception: pass`, no `let _ =` on a fallible call. If something must
carry on past a failure, log it and say why in a comment.

## Where things live

| Path | Contents |
|---|---|
| `nitro/` | The framework |
| `nitro-intercom/` | Standalone publish/subscribe client for non-Nitro services |
| `crates/nitro-core/` | Transport, lifecycle and routing — pure Rust, no interpreter |
| `crates/nitro-observability/` | Prometheus metrics and the exporter |
| `crates/nitro-py/` | Python bindings for the server (`nitro._nitro`) |
| `crates/intercom-core/` | Publish/subscribe channels — pure Rust, no interpreter |
| `crates/intercom-py/` | Python bindings for Intercom |

The `-core` crates know nothing about Python, and it should stay that way:
everything needing an interpreter goes through a trait the binding crate
implements. A `use pyo3` in `nitro-core` or `intercom-core` is a bug.

## Tests

A change that fixes a bug should come with a test that fails without it. Name
tests for the behaviour they pin rather than the function they call —
`a_refused_upgrade_reports_its_status` rather than `test_reject`.

## Reporting a security issue

Please do not open a public issue. See [SECURITY.md](SECURITY.md).

## Licence

By contributing you agree that your contribution is dual licensed under the MIT
licence and the Apache Licence 2.0, as the rest of the project is.
