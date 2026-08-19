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

"""Default values for every Nitro setting.

Override any of these in the module named by ``NITRO_SETTINGS_MODULE``. Only
names in upper case are read.
"""

import os
from typing import Any

# SECURITY WARNING: don't run with debug turned on in production.
DEBUG: bool = False

# Restart the server when a Python file changes. Development only; `--reload`
# turns it on regardless.
RELOAD: bool = False

# SECURITY WARNING: keep the secret key used in production secret.
SECRET_KEY: str = ""

# Keys accepted for signatures that were made with an earlier SECRET_KEY, so a
# key can be rotated without invalidating everything signed under the old one.
SECRET_KEY_FALLBACKS: list[str] = []

# Host names this site answers for. "*" matches anything, and a leading dot
# matches a domain and all of its subdomains.
#
# The server checks every request against this before the application sees it,
# and answers 400 to a name that is not here. An empty list means "not
# configured" and answers for any name, which is right while developing and
# wrong in production — `nitro check` refuses to pass a deployment that left it
# empty with DEBUG off.
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

# Cross-origin requests, read by nitro.middleware.common.CORSMiddleware. They
# do nothing unless that middleware is installed.
#
# SECURITY WARNING: CORS_ALLOW_ALL_ORIGINS answers for any site that asks.
# Combined with CORS_ALLOW_CREDENTIALS it hands a visitor's cookies to it.
CORS_ALLOWED_ORIGINS: list[str] = []
CORS_ALLOW_ALL_ORIGINS: bool = False
CORS_ALLOW_CREDENTIALS: bool = False
CORS_ALLOW_METHODS: list[str] = ["*"]
CORS_ALLOW_HEADERS: list[str] = ["*"]

# Security headers, read by nitro.middleware.common.SecurityHeadersMiddleware.
# They do nothing unless that middleware is installed.
#
# SECURE_HSTS_SECONDS is zero because HSTS is hard to undo: a browser told to
# use HTTPS for a year will refuse plain HTTP for that long, including on
# subdomains once SECURE_HSTS_INCLUDE_SUBDOMAINS is on.
SECURE_HSTS_SECONDS: int = 0
SECURE_HSTS_INCLUDE_SUBDOMAINS: bool = False
SECURE_CONTENT_TYPE_NOSNIFF: bool = True
SECURE_FRAME_DENY: bool = True

# Import path of the module holding the route table, or the routes themselves.
ROUTES: str | list[Any] = []

# Uploaded files. A file part larger than MAX_UPLOAD_MEMORY is written to disk
# as it arrives rather than held in memory, so a large upload costs a file
# descriptor instead of resident memory in the process serving every other
# connection. UPLOAD_DIR names where those files are written; None uses the
# system temporary directory, and either way they are deleted when closed.
MAX_UPLOAD_MEMORY: int = 1024 * 1024
UPLOAD_DIR: str | os.PathLike[str] | None = None

# ── The bundled server ───────────────────────────────────────────────────────
#
# Flat rather than nested under a SERVER mapping, for the same reason the
# observability options are: there is exactly one server to configure, and a
# mapping would suggest several named ones can be.
#
# Prefixed the way every other subsystem's flat settings are — EMAIL_HOST for
# mail, SECURE_HSTS_SECONDS for the security headers — because a settings
# module is one namespace shared with everything a project configures for
# itself, and a bare PORT or WORKERS in it belongs to whoever thought of it
# first.
#
# Each name is also the keyword argument `Nitro(...)` takes, without the prefix
# and in lower case, and most are a command line flag as well; the three
# override one another in that order.

# Network
SERVER_HOST: str = "localhost"
SERVER_PORT: int = 8000
SERVER_UDS: str | None = None

# TLS. QUIC always terminates its own TLS because the protocol requires it, so
# a certificate is mandatory whenever HTTP/3 is active.
SERVER_TLS_CERT: str | None = None
SERVER_TLS_KEY: str | None = None
SERVER_TLS_CA: str | None = None
SERVER_TLS_CLIENT_AUTH: str = "none"
SERVER_TLS_TCP: bool = True
SERVER_TLS_RELOAD_INTERVAL: float = 10.0

# Protocols. "auto" is the highest available, currently HTTP/3.
SERVER_HTTP: str = "auto"
SERVER_WEBSOCKETS: bool = True
SERVER_WEBTRANSPORT: bool = True

# Processes
SERVER_WORKERS: int = 1
SERVER_RUNTIME_THREADS: int = 2

# Backpressure
SERVER_BACKLOG: int = 1024
SERVER_MAX_CONCURRENT_CONNECTIONS: int | None = None
SERVER_DATAGRAM_QUEUE_CAPACITY: int = 64
SERVER_STREAM_QUEUE_CAPACITY: int = 16

# Advertising HTTP/3: "auto", "off", or a verbatim header value.
SERVER_ALT_SVC: str = "auto"

# Seconds in-flight work is given to finish once shutdown starts.
SERVER_DRAIN_TIMEOUT: float = 30.0

# Value for the Server response header. None omits it. Not SERVER_SERVER_HEADER:
# the name already reads as the server's setting for the Server header.
SERVER_HEADER: str | None = "nitro"

# Server log
SERVER_LOG_LEVEL: str = "info"
SERVER_LOG_DESTINATION: str = "stderr"
SERVER_LOG_FORMAT: str = "pretty"

# Access log
SERVER_ACCESS_LOG: bool = False
SERVER_ACCESS_LOG_DESTINATION: str = "stdout"
SERVER_ACCESS_LOG_FORMAT: str = "combined"

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

# Publish/subscribe channels. The default backend keeps everything in this
# process, so Intercom works before a project has a Redis and its tests need
# nothing running. A deployment with more than one worker has to name
# RedisIntercom instead: separate processes share nothing, so a message
# published in one worker would not reach a socket held by another.
INTERCOMS: dict[str, dict[str, Any]] = {
    "default": {
        "BACKEND": "nitro.intercom.backends.MemoryIntercom",
        "LOCATION": "",
        "OPTIONS": {},
    }
}
