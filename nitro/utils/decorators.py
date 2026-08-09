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

"""Turning a decorator written for functions into one that can wrap a method."""

from collections.abc import Callable, Iterable
from functools import partial, update_wrapper, wraps
from inspect import iscoroutinefunction, markcoroutinefunction
from typing import Any

__all__ = ["method_decorator"]

type Decoration[F] = Callable[[F], F]


def _listed[F](decorators: Decoration[F] | Iterable[Decoration[F]]) -> tuple[Decoration[F], ...]:
    """One decorator or several, always a tuple, in the order they were written."""
    if isinstance(decorators, Iterable):
        return tuple(decorators)
    return (decorators,)


def _stack[F](decorators: tuple[Decoration[F], ...], function: F) -> Any:
    """Apply `decorators` to `function`, first written ending up outermost.

    Applied back to front, so the last one written wraps the function directly
    and the first one written wraps everything.
    """
    for decorator in reversed(decorators):
        function = decorator(function)
    return function


def _without_self(method: Any, instance: Any) -> Any:
    """`method` bound to `instance`, as something a decorator will accept.

    A decorator written for functions takes no `self`, so it has to be handed
    the bound form. That is a bound method, which refuses new attributes — and a
    decorator setting one on what it wraps is ordinary — so it goes through a
    `partial`, which does not.
    """
    return wraps(method)(partial(method.__get__(instance, type(instance))))


def _tagged[F](decorators: tuple[Decoration[F], ...]) -> Any:
    """What the decorators leave on whatever they wrap.

    A decorator that marks its wrapper is telling later code something, and the
    mark has to survive. Nothing can ask a decorator what it sets, so it is run
    over a function that exists only to be inspected and never called.
    """

    def sacrificial(*arguments: Any, **keywords: Any) -> None:
        raise AssertionError("this exists to be decorated, not to be called")

    return _stack(decorators, sacrificial)


def _wrap_method[F](decorators: Decoration[F] | Iterable[Decoration[F]], method: Any) -> Any:
    listed = _listed(decorators)

    def wrapper(self: Any, *arguments: Any, **keywords: Any) -> Any:
        return _stack(listed, _without_self(method, self))(*arguments, **keywords)

    # Twice, and the order decides the outcome: the decorators' own marks are
    # copied first, then the method's over the top, so the result answers to the
    # method's name rather than to whatever the innermost wrapper was called.
    update_wrapper(wrapper, _tagged(listed))
    update_wrapper(wrapper, method)

    # `wrapper` is an ordinary function that hands back the coroutine the method
    # made, so awaiting it works either way. This is for whatever asks first.
    if iscoroutinefunction(method):
        markcoroutinefunction(wrapper)

    return wrapper


def method_decorator[F, T](
    decorator: Decoration[F] | Iterable[Decoration[F]], name: str = ""
) -> Callable[[type[T] | F], type[T] | Any]:
    """Adapt `decorator` so it can wrap a method, or a named method of a class.

    Given a class, `name` says which method to wrap and the class is returned
    changed in place. Given a method, the wrapped method is returned.
    """

    def decorate(target: type[T] | F) -> type[T] | Any:
        if not isinstance(target, type):
            return _wrap_method(decorator, target)

        if not (name and hasattr(target, name)):
            raise ValueError(
                "The keyword argument `name` must be the name of a method "
                f"of the decorated class: {target}. Got '{name}' instead."
            )
        attribute = getattr(target, name)
        if not callable(attribute):
            raise TypeError(
                f"Cannot decorate '{name}' as it isn't a callable attribute of "
                f"{target} ({attribute})."
            )
        setattr(target, name, _wrap_method(decorator, attribute))
        return target

    # A list has no metadata worth carrying over, so only a lone decorator is
    # copied from.
    if not isinstance(decorator, Iterable):
        update_wrapper(decorate, decorator)

    named = decorator if hasattr(decorator, "__name__") else type(decorator)
    decorate.__name__ = f"method_decorator({named.__name__})"
    return decorate
