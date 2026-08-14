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

"""Settings resolution.

Defaults are always loaded. The module named by ``NITRO_SETTINGS_MODULE``, if
set, overrides them. Resolution is deferred until the first attribute read so
that importing ``nitro`` never depends on a project being configured.
"""

from __future__ import annotations

import dataclasses
import importlib
import os
from dataclasses import dataclass
from typing import Any

from nitro.settings import defaults

ENVIRONMENT_VARIABLE = "NITRO_SETTINGS_MODULE"


class ImproperlyConfigured(Exception):
    """A setting is missing or cannot be used as given."""


class Settings:
    """A resolved settings namespace: defaults, then project overrides."""

    def __init__(self, settings_module: str | None = None) -> None:
        for name in dir(defaults):
            if name.isupper():
                setattr(self, name, getattr(defaults, name))

        self.SETTINGS_MODULE = settings_module
        if settings_module is None:
            return

        try:
            module = importlib.import_module(settings_module)
        except ImportError as error:
            raise ImproperlyConfigured(
                f"Could not import settings module {settings_module!r}. "
                f"Is it importable from the current directory, and free of import errors? {error}"
            ) from error

        for name in dir(module):
            if name.isupper():
                setattr(self, name, getattr(module, name))

    def as_dict(self) -> dict[str, Any]:
        return {name: value for name, value in vars(self).items() if name.isupper()}


class LazySettings:
    """The project's settings, resolved on first use.

    Not built on :mod:`nitro.utils.lazy`, which offers general-purpose deferred
    objects for application code. This defers one specific thing from one
    specific place, and adds :meth:`reset` and :attr:`configured` for tests that
    change the environment between cases — neither of which a general proxy
    models, and this sits on the import path of nearly everything.
    """

    _wrapped: Settings | None

    def __init__(self) -> None:
        object.__setattr__(self, "_wrapped", None)

    def _setup(self) -> Settings:
        resolved = Settings(os.environ.get(ENVIRONMENT_VARIABLE))
        object.__setattr__(self, "_wrapped", resolved)
        return resolved

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")
        resolved = self._wrapped or self._setup()
        return getattr(resolved, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        resolved = self._wrapped or self._setup()
        setattr(resolved, name, value)

    @property
    def configured(self) -> bool:
        """Whether the settings have been resolved yet."""
        return self._wrapped is not None

    def reset(self) -> None:
        """Forget the resolved settings so the next read reloads them.

        Intended for tests that change the environment between cases.
        """
        object.__setattr__(self, "_wrapped", None)

    def __repr__(self) -> str:
        if self._wrapped is None:
            return "<LazySettings [unresolved]>"
        module = self._wrapped.SETTINGS_MODULE
        return f"<LazySettings {module!r}>" if module else "<LazySettings [defaults]>"


settings = LazySettings()


#: Server options that keep a name of their own instead of taking the server's
#: prefix. ``ALLOWED_HOSTS`` describes the site rather than the socket, and the
#: observability options already carry a prefix naming their own subsystem.
UNPREFIXED_OPTIONS: frozenset[str] = frozenset({"allowed_hosts"})


def setting_name(field: str) -> str:
    """The settings name a :class:`ServerOptions` field is configured under.

    Server options are prefixed, the way every other subsystem's flat settings
    are — ``EMAIL_HOST`` for mail, ``SECURE_HSTS_SECONDS`` for the security
    headers, ``SERVER_PORT`` for the server. A settings module is one namespace
    shared with everything a project configures for itself, and a bare ``PORT``
    or ``WORKERS`` in it belongs to whoever thought of it first.
    """
    if field in UNPREFIXED_OPTIONS or field.startswith("observability_"):
        return field.upper()
    # `server_header` names the header it sets and already reads as a prefixed
    # setting; prefixing again would give SERVER_SERVER_HEADER.
    if field.startswith("server_"):
        return field.upper()
    return f"SERVER_{field.upper()}"


@dataclass(slots=True)
class ServerOptions:
    """The bundled server's configuration, in the shape the server reads.

    Every field is a flat top-level setting under the name
    :func:`setting_name` gives it: ``host`` is ``SERVER_HOST``, ``tls_cert`` is
    ``SERVER_TLS_CERT``. Flat rather than nested for the same reason the
    observability options are — there is exactly one server to configure, and a
    mapping would suggest several named ones can be.
    """

    host: str = "localhost"
    port: int = 8000
    uds: str | None = None

    #: Host names the server answers for, from the ``ALLOWED_HOSTS`` setting.
    #: Empty answers for every name; the server checks each request against
    #: this before the application sees it.
    allowed_hosts: list[str] = dataclasses.field(default_factory=list)

    tls_cert: str | None = None
    tls_key: str | None = None
    tls_ca: str | None = None
    tls_client_auth: str = "none"
    tls_tcp: bool = True
    tls_reload_interval: float = 10.0

    http: str = "auto"
    websockets: bool = True
    webtransport: bool = True

    workers: int = 1
    runtime_threads: int = 2

    backlog: int = 1024
    max_concurrent_connections: int | None = None
    datagram_queue_capacity: int = 64
    stream_queue_capacity: int = 16

    alt_svc: str = "auto"
    drain_timeout: float = 30.0
    server_header: str | None = "nitro"

    log_level: str = "info"
    log_destination: str = "stderr"
    log_format: str = "pretty"

    access_log: bool = False
    access_log_destination: str = "stdout"
    access_log_format: str = "combined"

    observability_enabled: bool = False
    observability_host: str = "localhost"
    observability_port: int = 9464

    @classmethod
    def resolve(cls, source: Any = None, **overrides: Any) -> ServerOptions:
        """Build options from a settings object, then apply `overrides`.

        An override of ``None`` means "not specified", so a command line flag
        that was not given does not erase a configured value.
        """
        known = [field.name for field in dataclasses.fields(cls)]
        settings_source = settings if source is None else source

        values: dict[str, Any] = {}
        for name in known:
            try:
                value = getattr(settings_source, setting_name(name))
            except (AttributeError, ImproperlyConfigured):
                continue
            values[name] = value

        for name, value in overrides.items():
            if value is None:
                continue
            if name not in known:
                raise ImproperlyConfigured(f"{name!r} is not a known server option")
            values[name] = value

        return cls(**values)
