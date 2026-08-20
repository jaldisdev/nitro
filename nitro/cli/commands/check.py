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

"""``nitro check`` — look for configuration problems before deploying."""

from __future__ import annotations

import importlib.util
import sys

import click

from nitro.settings import ImproperlyConfigured, ServerOptions, settings

#: Packages a backend needs, by the setting that would select it.
OPTIONAL_PACKAGES: dict[str, str] = {
    "aioboto3": "AWS storage and email",
    "azure.storage.blob": "Azure storage",
    "emcache": "Memcached caching",
    "redis": "Redis caching",
    "sendgrid": "SendGrid email",
    "authlib": "OAuth email",
}


def _report(passed: bool, message: str) -> bool:
    click.echo(
        f"  {click.style('✓', fg='green') if passed else click.style('✗', fg='red')} {message}"
    )
    return passed


def _installed_middleware() -> list[type]:
    """The middleware classes named by ``MIDDLEWARE``, as far as they import.

    A path that cannot be resolved is left out rather than reported: the stack
    raises on it at startup with a better message than this command could, and
    a check that guessed here would report the same problem twice.
    """
    from nitro.utils.modules import import_string

    found: list[type] = []
    for path in settings.MIDDLEWARE:
        try:
            found.append(import_string(path))
        except (ImportError, AttributeError):
            continue
    return found


def _session_problems() -> list[str]:
    """What is wrong with how sessions are configured, if anything."""
    from nitro.middleware.common import OriginMiddleware
    from nitro.sessions import SessionMiddleware

    installed = _installed_middleware()
    sessions = [
        candidate
        for candidate in installed
        if isinstance(candidate, type) and issubclass(candidate, SessionMiddleware)
    ]
    if not sessions:
        return []

    problems: list[str] = []

    entry = settings.CACHES.get(settings.SESSION_CACHE)
    if entry is None:
        problems.append(f"SESSION_CACHE names {settings.SESSION_CACHE!r}, which is not in CACHES")
    elif "MemoryCache" in entry.get("BACKEND", "") and ServerOptions.resolve().workers > 1:
        problems.append(
            f"sessions are kept in {settings.SESSION_CACHE!r}, a MemoryCache, with more than "
            "one worker; each worker has its own, so a session is only found again by the "
            "worker that made it"
        )

    # Only a middleware that still writes the cookie needs the origin check.
    # One that carries the key itself — a claim inside a token, a header — is
    # not sending an ambient credential, and there is nothing to forge.
    carries_cookie = any(
        candidate.write_key is SessionMiddleware.write_key for candidate in sessions
    )
    if carries_cookie and not any(
        isinstance(candidate, type) and issubclass(candidate, OriginMiddleware)
        for candidate in installed
    ):
        problems.append(
            "sessions are carried in a cookie but OriginMiddleware is not installed, so a "
            "state-changing request from another site arrives with the visitor's session"
        )

    return problems


@click.command("check")
@click.option("-v", "--verbose", is_flag=True, help="List optional packages too.")
def check(verbose: bool) -> None:
    """Check the project's configuration.

    Exits non-zero when something is wrong, so it can gate a deployment.
    """
    problems = 0

    click.echo(click.style("Settings", bold=True))
    try:
        # Reading one settles the whole module, which is what is being checked.
        _resolved = settings.DEBUG
        _report(True, f"settings load ({settings.SETTINGS_MODULE or 'defaults only'})")
    except ImproperlyConfigured as error:
        problems += not _report(False, f"settings: {error}")

    try:
        options = ServerOptions.resolve()
        _report(
            True, f"server options ({options.host}:{options.port}, {options.workers} worker(s))"
        )
    except ImproperlyConfigured as error:
        problems += not _report(False, f"server options: {error}")
        options = None

    if options is not None:
        if options.http == "auto" or options.http == "3":
            if not (options.tls_cert and options.tls_key):
                problems += not _report(
                    False, "HTTP/3 is enabled but no TLS certificate is configured"
                )
            else:
                _report(True, "TLS is configured for HTTP/3")

        if not settings.DEBUG and not settings.SECRET_KEY:
            problems += not _report(False, "SECRET_KEY is empty and DEBUG is off")

        if not settings.DEBUG and not options.allowed_hosts:
            problems += not _report(
                False,
                "ALLOWED_HOSTS is empty and DEBUG is off, so the server answers to any Host header",
            )
        elif options.allowed_hosts:
            _report(True, f"answering for {', '.join(options.allowed_hosts)}")

        if options.observability_enabled:
            # Each of these is reported on its own: a configuration can be
            # wrong in both ways, and hearing about one of them and fixing it
            # only to be told about the other wastes a round trip.
            healthy = True
            if options.observability_port == options.port:
                healthy = False
                problems += not _report(
                    False,
                    "the metrics exporter is configured on the application's port",
                )
            if options.observability_host in {"0.0.0.0", "::"}:
                healthy = False
                problems += not _report(
                    False,
                    "OBSERVABILITY_HOST exposes metrics on every interface; "
                    "bind it to a loopback or internal address instead",
                )
            if healthy:
                _report(
                    True,
                    f"metrics on {options.observability_host}:{options.observability_port}",
                )

    try:
        for problem in _session_problems():
            problems += not _report(False, problem)
    except ImproperlyConfigured as error:
        problems += not _report(False, f"sessions: {error}")

    click.echo()
    click.echo(click.style("Runtime", bold=True))
    _report(sys.version_info >= (3, 13), f"Python {sys.version.split()[0]}")

    try:
        import nitro._nitro  # noqa: F401

        _report(True, "the compiled server is available")
    except ImportError as error:
        problems += not _report(False, f"the compiled server is missing: {error}")

    if verbose:
        click.echo()
        click.echo(click.style("Optional packages", bold=True))
        for module, purpose in OPTIONAL_PACKAGES.items():
            installed = importlib.util.find_spec(module.split(".")[0]) is not None
            mark = click.style("✓", fg="green") if installed else "–"
            click.echo(f"  {mark} {module} ({purpose})")

    click.echo()
    if problems:
        raise click.ClickException(f"{problems} problem(s) found")
    click.echo(click.style("No problems found.", fg="green"))
