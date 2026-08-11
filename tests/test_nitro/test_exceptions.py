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

import pytest

from nitro.protocols.exceptions import (
    BadRequest,
    ExceptionHandlerRegistry,
    Http404,
    HttpBadRequest,
    HttpException,
    HttpForbidden,
    HttpInternalServerError,
    HttpMethodNotAllowed,
    HttpNotFound,
    HttpServiceUnavailable,
    HttpTooManyRequests,
    HttpUnauthorized,
    HttpUnprocessableEntity,
    PermissionDenied,
)


class TestHttpException:
    def test_default_detail(self):
        exc = Http404()
        assert exc.detail == "Not found"
        assert exc.status_code == 404

    def test_custom_detail(self):
        exc = Http404(detail="Page gone")
        assert exc.detail == "Page gone"

    def test_custom_headers(self):
        exc = HttpMethodNotAllowed(headers={"Allow": "GET, POST"})
        assert exc.headers == {"Allow": "GET, POST"}

    def test_repr(self):
        exc = HttpBadRequest()
        assert "400" in repr(exc)

    def test_is_exception(self):
        assert isinstance(HttpBadRequest(), Exception)


class TestHttpExceptionStatusCodes:
    @pytest.mark.parametrize(
        "exc_class,expected_status",
        [
            (HttpBadRequest, 400),
            (HttpUnauthorized, 401),
            (HttpForbidden, 403),
            (Http404, 404),
            (HttpMethodNotAllowed, 405),
            (HttpUnprocessableEntity, 422),
            (HttpTooManyRequests, 429),
            (HttpInternalServerError, 500),
            (HttpServiceUnavailable, 503),
        ],
    )
    def test_status_code(self, exc_class, expected_status):
        assert exc_class.status_code == expected_status


class TestAliases:
    def test_httpnotfound_is_http404(self):
        assert HttpNotFound is Http404

    def test_permissiondenied_is_httpforbidden(self):
        assert PermissionDenied is HttpForbidden

    def test_badrequest_is_httpbadrequest(self):
        assert BadRequest is HttpBadRequest


class TestExceptionHandlerRegistry:
    @pytest.mark.asyncio
    async def test_exact_type_match(self):
        registry = ExceptionHandlerRegistry()

        async def handler(req, exc):
            return "exact"

        registry.add_handler(Http404, handler)
        assert registry.get_handler(Http404()) is handler

    @pytest.mark.asyncio
    async def test_status_code_match(self):
        registry = ExceptionHandlerRegistry()

        async def handler(req, exc):
            return "404_handler"

        registry.add_handler(404, handler)
        assert registry.get_handler(Http404()) is handler

    @pytest.mark.asyncio
    async def test_parent_class_mro_match(self):
        registry = ExceptionHandlerRegistry()

        async def handler(req, exc):
            return "base_handler"

        registry.add_handler(HttpException, handler)
        # HttpBadRequest inherits from HttpException
        assert registry.get_handler(HttpBadRequest()) is handler

    def test_no_handler_returns_none(self):
        registry = ExceptionHandlerRegistry()
        assert registry.get_handler(Http404()) is None

    def test_exact_match_beats_parent(self):
        registry = ExceptionHandlerRegistry()

        async def base_handler(req, exc):
            pass

        async def specific_handler(req, exc):
            pass

        registry.add_handler(HttpException, base_handler)
        registry.add_handler(Http404, specific_handler)
        assert registry.get_handler(Http404()) is specific_handler

    def test_remove_handler(self):
        registry = ExceptionHandlerRegistry()

        async def handler(req, exc):
            pass

        registry.add_handler(Http404, handler)
        registry.remove_handler(Http404)
        assert registry.get_handler(Http404()) is None

    def test_clear(self):
        registry = ExceptionHandlerRegistry()

        async def handler(req, exc):
            pass

        registry.add_handler(Http404, handler)
        registry.add_handler(500, handler)
        registry.clear()
        assert registry.get_handler(Http404()) is None

    def test_non_http_exception_no_status_match(self):
        registry = ExceptionHandlerRegistry()

        async def handler(req, exc):
            pass

        registry.add_handler(404, handler)
        # Plain ValueError has no status_code
        assert registry.get_handler(ValueError("something")) is None
