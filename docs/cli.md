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
| `-H`, `--host` | `SERVER["HOST"]` |
| `-p`, `--port` | `SERVER["PORT"]` |
| `--uds` | `SERVER["UDS"]` |
| `-w`, `--workers` | `SERVER["WORKERS"]` |
| `--runtime-threads` | `SERVER["RUNTIME_THREADS"]` |
| `--http` | `SERVER["HTTP"]` |
| `--tls-cert`, `--tls-key` | the TLS pair |
| `--access-log` / `--no-access-log` | `SERVER["ACCESS_LOG"]` |
| `-l`, `--log-level` | `SERVER["LOG_LEVEL"]` |

A flag that is not given is dropped rather than applied, so it cannot erase
something the application set for itself.

Sockets are bound before any worker exists, so a port already in use is one
clear error at startup rather than one per worker after the process appears to
have started.

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
