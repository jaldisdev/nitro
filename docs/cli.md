# The command line

```sh
nitro --help
```

| Command | Does |
|---|---|
| `nitro APPLICATION` | Serves an application. |
| `nitro check` | Reports configuration problems; exits non-zero when it finds any. |
| `nitro shell` | An interactive shell with the project loaded. |
| `nitro version` | The installed version. |

## Serving

```sh
nitro app:app
nitro myproject.entry:application --workers 4 --host 0.0.0.0 --port 8000
```

Serving is what the root command does; there is no word in front of it. Anything
that is not one of the commands above is taken as an application.

The argument is `module:attribute`, defaulting to `app:app`. The working
directory is put on the import path, so a project's own modules are importable
without installing it.

| Flag | Setting it overrides |
|---|---|
| `-H`, `--host` | `SERVER_HOST` |
| `-p`, `--port` | `SERVER_PORT` |
| `--uds` | `SERVER_UDS` |
| `-w`, `--workers` | `SERVER_WORKERS` |
| `--runtime-threads` | `SERVER_RUNTIME_THREADS` |
| `--http` | `SERVER_HTTP` |
| `--tls-cert`, `--tls-key` | the TLS pair |
| `--access-log` / `--no-access-log` | `SERVER_ACCESS_LOG` |
| `--reload` | — (development only, see below) |
| `-l`, `--log-level` | `SERVER_LOG_LEVEL` |

A flag that is not given is dropped rather than applied, so it cannot erase
something the application set for itself.

Sockets are bound before any worker exists, so a port already in use is one
clear error at startup rather than one per worker after the process appears to
have started.

An application can also start the server itself, which is what a container
entry point usually wants. See [deployment](deployment.md#serving-from-the-application-itself).

## Reloading during development

```sh
nitro --reload app:app
```

`--reload` restarts the server whenever a `.py` file under the working
directory changes. It is off unless asked for, and is not tied to `DEBUG`: a
deployment that ships debug pages by mistake should not also acquire a file
watcher and a supervising process on top of it.

A parent process watches the tree and a child runs the server, which is
replaced wholesale on a change. Reloading a module in place would not work
here — the route table is compiled into the matcher at startup, the middleware
stack is built once from settings, and the listening sockets belong to the
server — so a full restart is what makes an edit take effect.

Nothing about this reaches a server started without the flag. The reloader is
imported only when it is asked for, the child is started from the same command
line and reaches the same server the flag-less run would have, and no watching
happens inside it.

A child that exits with an error is not restarted on a timer. The supervisor
prints the traceback and waits for the next edit, so a syntax error stays on
screen instead of scrolling past in a restart loop.

Two things are worth knowing. The watch is rooted at the working directory, so
running from somewhere that does not contain the project watches the wrong
tree. And changes are noticed by polling, within roughly half a second rather
than instantly — the trade for having nothing to install and no per-platform
notification backend to differ.

An application that starts the server itself takes the same option:

```python
if __name__ == "__main__":
    app.serve(reload=True)
```

## Commands of your own

Any `click.Command` defined at module level in a package listed in
`COMMAND_MODULES` is registered automatically.

```python
COMMAND_MODULES = ["myproject.commands"]
```

```python
# myproject/commands/backfill.py
import click


@click.command("backfill")
@click.option("--since", required=True)
def backfill(since: str) -> None:
    """Backfill records changed since a date."""
    click.echo(f"backfilling from {since}")
```

```sh
nitro backfill --since 2026-01-01
```

A package that cannot be imported is logged and skipped rather than taking the
whole command line down with it — a broken command should not stop you running
the one that fixes it.

Both `-h` and `--help` open help, so a command cannot use `-h` for anything
else. `-H` is the convention for a host option.
