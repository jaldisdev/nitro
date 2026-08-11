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

import inspect

import pytest

from nitro.endpoints import HTTPEndpoint
from nitro.protocols import JSONResponse
from nitro.routing.patterns import _handler_for
from nitro.utils.decorators import method_decorator


def note(mark: str):
    """A decorator that records that it ran, around whatever it wraps."""

    def decorator(function):
        async def wrapper(*args, **kwargs):
            response = await function(*args, **kwargs)
            response.trail.append(mark)
            return response

        wrapper.applied = mark
        return wrapper

    return decorator


class Trail(JSONResponse):
    def __init__(self):
        super().__init__({})
        self.trail: list[str] = []


class FakeRequest:
    method = "GET"


class TestMethodDecorator:
    async def test_it_decorates_a_single_method(self):
        class Endpoint(HTTPEndpoint):
            @method_decorator(note("outer"))
            async def get(self, request) -> Trail:
                return Trail()

        response = await Endpoint().dispatch(FakeRequest())
        assert response.trail == ["outer"]

    async def test_it_decorates_a_named_method_of_a_class(self):
        @method_decorator(note("on-dispatch"), name="dispatch")
        class Endpoint(HTTPEndpoint):
            async def get(self, request) -> Trail:
                return Trail()

        response = await Endpoint().dispatch(FakeRequest())
        assert response.trail == ["on-dispatch"]

    async def test_a_list_runs_in_the_order_it_is_written(self):
        class Endpoint(HTTPEndpoint):
            @method_decorator([note("first"), note("second")])
            async def get(self, request) -> Trail:
                return Trail()

        response = await Endpoint().dispatch(FakeRequest())
        # Innermost finishes first, so the one written first wrapped last and
        # leaves its mark last.
        assert response.trail == ["second", "first"]

    def test_the_wrapper_is_still_a_coroutine_function(self):
        class Endpoint(HTTPEndpoint):
            @method_decorator(note("x"))
            async def get(self, request) -> Trail:
                return Trail()

        # The wrapper is an ordinary function returning a coroutine; without
        # markcoroutinefunction anything inspecting it would be told otherwise.
        assert inspect.iscoroutinefunction(Endpoint.get)

    def test_it_keeps_the_name_of_what_it_decorated(self):
        class Endpoint(HTTPEndpoint):
            @method_decorator(note("x"))
            async def get(self, request) -> Trail:
                return Trail()

        assert Endpoint.get.__name__ == "get"

    def test_it_carries_attributes_the_decorator_added(self):
        class Endpoint(HTTPEndpoint):
            @method_decorator(note("marked"))
            async def get(self, request) -> Trail:
                return Trail()

        assert Endpoint.get.applied == "marked"

    def test_decorating_a_class_without_a_name_says_so(self):
        with pytest.raises(ValueError, match="must be the name of a method"):

            @method_decorator(note("x"))
            class Endpoint(HTTPEndpoint):
                async def get(self, request) -> Trail:
                    return Trail()

    def test_decorating_a_name_that_is_not_a_method_says_so(self):
        with pytest.raises(ValueError, match="must be the name of a method"):

            @method_decorator(note("x"), name="nope")
            class Endpoint(HTTPEndpoint):
                async def get(self, request) -> Trail:
                    return Trail()

    def test_decorating_something_uncallable_says_so(self):
        with pytest.raises(TypeError, match="isn't a callable attribute"):

            @method_decorator(note("x"), name="setting")
            class Endpoint(HTTPEndpoint):
                setting = "not callable"

    def test_the_decorator_is_named_for_debugging(self):
        def login_required(function):
            return function

        assert method_decorator(login_required).__name__ == "method_decorator(login_required)"


class TestEndpointDecorators:
    async def test_the_list_wraps_the_whole_endpoint(self):
        class Endpoint(HTTPEndpoint):
            decorators = [note("around")]

            async def get(self, request) -> Trail:
                return Trail()

        handler = _handler_for(Endpoint)
        response = await handler(FakeRequest())
        assert response.trail == ["around"]

    async def test_the_first_named_is_the_outermost(self):
        class Endpoint(HTTPEndpoint):
            decorators = [note("first"), note("second")]

            async def get(self, request) -> Trail:
                return Trail()

        handler = _handler_for(Endpoint)
        response = await handler(FakeRequest())
        # Outermost finishes last, so the first named leaves its mark last.
        assert response.trail == ["second", "first"]

    async def test_an_endpoint_without_any_is_untouched(self):
        class Endpoint(HTTPEndpoint):
            async def get(self, request) -> Trail:
                return Trail()

        handler = _handler_for(Endpoint)
        response = await handler(FakeRequest())
        assert response.trail == []

    async def test_the_two_forms_compose(self):
        class Endpoint(HTTPEndpoint):
            decorators = [note("class")]

            @method_decorator(note("method"))
            async def get(self, request) -> Trail:
                return Trail()

        handler = _handler_for(Endpoint)
        response = await handler(FakeRequest())
        assert response.trail == ["method", "class"]
