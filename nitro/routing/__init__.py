"""Routing: converters, the route table, and sub-router mounting."""

from nitro.routing.converters import (
    Converter,
    IntConverter,
    PathConverter,
    SlugConverter,
    StringConverter,
    UUIDConverter,
    converter_for,
    get_converters,
    register_converter,
)
from nitro.routing.mount import Mount
from nitro.routing.patterns import (
    HTTPRoute,
    WebSocketRoute,
    WebTransportRoute,
    load_exception_handlers,
    load_patterns,
)
from nitro.routing.parameters import (
    Body,
    Cookie,
    File,
    Header,
    Path,
    Query,
    ValidationError,
)
from nitro.routing.reverse import reverse
from nitro.routing.router import ParameterSpec, Route, Router

__all__ = [
    "Body",
    "Converter",
    "Cookie",
    "File",
    "Header",
    "HTTPRoute",
    "Path",
    "Query",
    "ValidationError",
    "WebSocketRoute",
    "WebTransportRoute",
    "load_exception_handlers",
    "load_patterns",
    "reverse",
    "IntConverter",
    "Mount",
    "ParameterSpec",
    "PathConverter",
    "Route",
    "Router",
    "SlugConverter",
    "StringConverter",
    "UUIDConverter",
    "converter_for",
    "get_converters",
    "register_converter",
]
