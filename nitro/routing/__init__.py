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
from nitro.routing.parameters import (
    Body,
    Cookie,
    File,
    Header,
    Path,
    Query,
    ValidationError,
)
from nitro.routing.patterns import (
    HTTPRoute,
    WebSocketRoute,
    WebTransportRoute,
    load_exception_handlers,
    load_patterns,
)
from nitro.routing.reverse import reverse
from nitro.routing.router import ParameterSpec, Route, Router

__all__ = [
    "Body",
    "Converter",
    "Cookie",
    "File",
    "HTTPRoute",
    "Header",
    "IntConverter",
    "Mount",
    "ParameterSpec",
    "Path",
    "PathConverter",
    "Query",
    "Route",
    "Router",
    "SlugConverter",
    "StringConverter",
    "UUIDConverter",
    "ValidationError",
    "WebSocketRoute",
    "WebTransportRoute",
    "converter_for",
    "get_converters",
    "load_exception_handlers",
    "load_patterns",
    "register_converter",
    "reverse",
]
