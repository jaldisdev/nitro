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
    """The project's settings, resolved on first use."""

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


@dataclass(slots=True)
class ServerOptions:
    """The bundled server's configuration, in the shape the server reads.

    Field names are lower case here and upper case in the ``SERVER`` setting;
    the translation happens in :meth:`resolve`.
    """

    host: str = "localhost"
    port: int = 8000
    uds: str | None = None

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
    runtime_threads: int = 1

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

    @classmethod
    def resolve(cls, source: Any = None, **overrides: Any) -> ServerOptions:
        """Build options from a settings object, then apply `overrides`.

        An override of ``None`` means "not specified", so a command line flag
        that was not given does not erase a configured value.
        """
        known = {field.name for field in dataclasses.fields(cls)}
        settings_source = settings if source is None else source

        try:
            configured = getattr(settings_source, "SERVER", None) or {}
        except ImproperlyConfigured:
            configured = {}

        values: dict[str, Any] = {}
        for key, value in configured.items():
            name = key.lower()
            if name not in known:
                raise ImproperlyConfigured(
                    f"SERVER setting {key!r} is not a known server option"
                )
            values[name] = value

        for name, value in overrides.items():
            if value is None:
                continue
            if name not in known:
                raise ImproperlyConfigured(f"{name!r} is not a known server option")
            values[name] = value

        return cls(**values)
