"""Building a path from a route's name.

    from nitro.shortcuts import reverse

    reverse("post", identifier=42)   # "/posts/42"

The name is looked up in the application's route table. A worker serves one
application, so the table is registered when that application is constructed
and reached from anywhere afterwards — which is what lets a template or a model
build a URL without being handed the application first.
"""

from __future__ import annotations

from typing import Any

from nitro.routing.router import Router

__all__ = ["active_router", "reverse", "set_active_router"]

_active: Router | None = None


def set_active_router(router: Router | None) -> None:
    """Make `router` the one :func:`reverse` looks in.

    Called when an application is constructed. A process that builds more than
    one — a test suite, usually — leaves the most recent one active.
    """
    global _active
    _active = router


def active_router() -> Router:
    if _active is None:
        raise LookupError("no application has been constructed, so there are no routes to reverse")
    return _active


def reverse(name: str, **values: Any) -> str:
    """The path for the route named `name`."""
    return active_router().url_for(name, **values)
