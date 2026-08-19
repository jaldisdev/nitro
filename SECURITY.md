# Security

## Reporting a vulnerability

Please report security issues privately, not as a public issue.

Use GitHub's [private vulnerability reporting][advisory] on this repository, or
email **oss@jaldis.com**.

[advisory]: https://github.com/jaldisdev/nitro/security/advisories/new

Please include what the issue is, how to reproduce it, and what an attacker
could do with it. You will get an acknowledgement within a few days, and we
will tell you what we intend to do and when.

Nitro is pre-1.0 and has no long-term support branches: fixes land on `canary`
and in the next release.

## What is in scope

Nitro is a web server as well as a framework, so the interesting surface is
larger than a library's:

- The compiled server: HTTP/1.1, HTTP/2, HTTP/3, WebSocket and WebTransport
  parsing, TLS handling, and the accept and drain paths.
- Request routing and path parameter conversion.
- `ALLOWED_HOSTS` enforcement, which happens in the server before a request
  reaches the application.
- Anything that crosses the Rust/Python boundary.
- The default configuration. A default that is unsafe is a vulnerability even
  if a safe setting exists.

## What is not

- **Pickle in the cache backends.** `SERIALIZER: "pickle"` deserialises
  arbitrary objects and will run code contained in them. This is documented and
  is not the default; JSON is. Choosing pickle for a store other parties can
  write to is a deployment decision, not a bug in Nitro.
- **`ALLOWED_HOSTS = []`**, which answers to any `Host`. This is the
  unconfigured state, it is documented, and `nitro check` fails a deployment
  that leaves it that way with `DEBUG` off.
- **`DEBUG = True` in production.** The debug pages show tracebacks and the
  route table by design.
- **`OBSERVABILITY_HOST = "0.0.0.0"`**, which publishes internal counters. The
  default is loopback and `nitro check` reports the change.

If one of these is reachable *without* the operator opting in, that is a bug —
please report it.
