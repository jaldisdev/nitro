"""Mounting a sub-router under a path prefix.

A mount is a grouping device, not a hosting one: it takes a router's routes and
re-registers them under a prefix in the parent. There is no notion of handing a
request off to a separate application — everything a Nitro project serves is
served by the same application object.
"""

from __future__ import annotations

from collections.abc import Iterable

from nitro.routing.router import Route, Router

__all__ = ["Mount"]


class Mount:
    """A router's routes, to be attached under `prefix`."""

    def __init__(self, prefix: str, routes: Router | Iterable[Route], *, name: str | None = None):
        if not prefix.startswith("/"):
            raise ValueError(f"mount prefix must start with '/', got {prefix!r}")
        # A trailing slash would double up against the mounted paths, which
        # start with one of their own.
        self.prefix = prefix.rstrip("/")
        self.name = name
        self.routes: list[Route] = list(routes.routes if isinstance(routes, Router) else routes)

    def attach(self, router: Router) -> None:
        """Register every mounted route on `router` under this mount's prefix."""
        for route in self.routes:
            qualified = f"{self.name}:{route.name}" if self.name and route.name else route.name
            router.add(
                f"{self.prefix}{route.path}",
                route.handler,
                methods=route.methods,
                name=qualified,
            )

    def __len__(self) -> int:
        return len(self.routes)

    def __repr__(self) -> str:
        return f"Mount({self.prefix!r}, routes={len(self.routes)})"
