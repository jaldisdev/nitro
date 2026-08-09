"""Default values for every Nitro setting.

Override any of these in the module named by ``NITRO_SETTINGS_MODULE``. Only
names in upper case are read.
"""

from typing import Any

# SECURITY WARNING: don't run with debug turned on in production.
DEBUG: bool = False

# SECURITY WARNING: keep the secret key used in production secret.
SECRET_KEY: str = ""

# Host names this site answers for. "*" matches anything, and a leading dot
# matches a domain and all of its subdomains.
ALLOWED_HOSTS: list[str] = []

TIME_ZONE: str = "Europe/Zurich"
USE_TZ: bool = True

LANGUAGE_CODE: str = "en-us"
LANGUAGES: list[tuple[str, str]] = [("en-us", "English (US)")]

# Import paths searched for additional CLI commands.
COMMAND_MODULES: list[str] = []

# Middleware, outermost first. Each entry is the import path of a Middleware
# subclass; a request passes through them in order on the way in and in reverse
# on the way out.
MIDDLEWARE: list[str] = []

# Import path of the module holding the route table, or the routes themselves.
ROUTES: str | list[Any] = []

# The bundled server. Keys map onto the server's own configuration; anything
# left out keeps the value below.
SERVER: dict[str, Any] = {
    # Network
    "HOST": "localhost",
    "PORT": 8000,
    "UDS": None,
    # TLS. QUIC always terminates its own TLS because the protocol requires it,
    # so a certificate is mandatory whenever HTTP/3 is active.
    "TLS_CERT": None,
    "TLS_KEY": None,
    "TLS_CA": None,
    "TLS_CLIENT_AUTH": "none",
    "TLS_TCP": True,
    "TLS_RELOAD_INTERVAL": 10.0,
    # Protocols. "auto" is the highest available, currently HTTP/3.
    "HTTP": "auto",
    "WEBSOCKETS": True,
    "WEBTRANSPORT": True,
    # Processes
    "WORKERS": 1,
    "RUNTIME_THREADS": 1,
    # Backpressure
    "BACKLOG": 1024,
    "MAX_CONCURRENT_CONNECTIONS": None,
    "DATAGRAM_QUEUE_CAPACITY": 64,
    "STREAM_QUEUE_CAPACITY": 16,
    # Advertising HTTP/3: "auto", "off", or a verbatim header value.
    "ALT_SVC": "auto",
    # Seconds in-flight work is given to finish once shutdown starts.
    "DRAIN_TIMEOUT": 30.0,
    "SERVER_HEADER": "nitro",
    # Server log
    "LOG_LEVEL": "info",
    "LOG_DESTINATION": "stderr",
    "LOG_FORMAT": "pretty",
    # Access log
    "ACCESS_LOG": False,
    "ACCESS_LOG_DESTINATION": "stdout",
    "ACCESS_LOG_FORMAT": "combined",
}

# Prometheus metrics. Flat rather than nested because there is exactly one
# exporter to configure; a mapping would suggest several named ones.
#
# The exporter listens on a port of its own, separate from the application's,
# so it can be firewalled and scraped independently and stays out of the route
# table. The host is loopback on purpose: a metrics endpoint is meant to be read
# by a scraper running alongside the server, not published to the network.
# Setting it to "0.0.0.0" exposes internal counters to anyone who can reach the
# port.
# Each worker takes the next port up from this one, because separate processes
# keep separate counters and cannot share an endpoint.
OBSERVABILITY_ENABLED: bool = False
OBSERVABILITY_HOST: str = "localhost"
OBSERVABILITY_PORT: int = 9464

# Template engines. Each entry configures one engine; DIRS lists the
# directories searched, and APP_DIRS additionally searches a "templates"
# directory inside each installed package.
TEMPLATES: list[dict[str, Any]] = [
    {
        "BACKEND": "nitro.templates.engine.Jinja2",
        "NAME": "default",
        "DIRS": [],
        "APP_DIRS": False,
        "OPTIONS": {},
    }
]

# The cache alias used for compiled template bytecode, when a template engine
# is configured to cache it.
TEMPLATE_CACHE: str = "default"

CACHES: dict[str, dict[str, Any]] = {
    "default": {
        "BACKEND": "nitro.cache.backends.MemoryCache",
        "LOCATION": "",
        "TIMEOUT": 300,
        "OPTIONS": {},
        "KEY_PREFIX": "",
        "VERSION": 1,
    }
}

EMAIL_BACKEND: str = "nitro.mail.backends.console.ConsoleBackend"
DEFAULT_FROM_EMAIL: str = "noreply@example.com"

EMAIL_HOST: str = "localhost"
EMAIL_PORT: int | None = None
EMAIL_HOST_USER: str = ""
EMAIL_HOST_PASSWORD: str = ""
EMAIL_USE_TLS: bool = False
EMAIL_USE_SSL: bool = False
EMAIL_TIMEOUT: int | None = None

EMAIL_OAUTH2_TOKEN: str | None = None
EMAIL_OAUTH2_TOKEN_CALLBACK: str | None = None

EMAIL_AWS_REGION: str = "us-east-1"
EMAIL_AWS_ACCESS_KEY_ID: str | None = None
EMAIL_AWS_SECRET_ACCESS_KEY: str | None = None
EMAIL_AWS_SESSION_TOKEN: str | None = None
EMAIL_SES_CONFIGURATION_SET: str | None = None

EMAIL_SENDGRID_API_KEY: str | None = None
EMAIL_SENDGRID_SANDBOX_MODE: bool = False

STORAGES: dict[str, dict[str, Any]] = {
    "default": {
        "BACKEND": "nitro.storage.backends.FileSystemStorage",
        "LOCATION": "./storage",
        "OPTIONS": {},
    }
}

INTERCOMS: dict[str, dict[str, Any]] = {
    "default": {
        "BACKEND": "nitro.intercom.backends.MemoryIntercom",
        "LOCATION": "",
    }
}
