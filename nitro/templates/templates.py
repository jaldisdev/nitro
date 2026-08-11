"""The project's template engines, reached by name.

Several can be configured — one for pages, one for email, one for anything else
with its own directories or filters — and each entry of the ``TEMPLATES``
setting builds one. The first is the default.

    from nitro.templates import templates

    html = await templates.render_to_string("index.html", {"title": "Home"})
    body = await templates.render_to_string("welcome.txt", context, using="email")

Engines are built on first use, because settings are not necessarily resolved
when this module is imported.
"""

from __future__ import annotations

from typing import Any

from nitro.templates.engine import Jinja2, Template

__all__ = ["Templates", "templates"]

#: The only backend there is. `BACKEND` is still written in each entry because
#: an engine has to say what it is, and because a second one would be added
#: here rather than by changing every project's settings.
SUPPORTED_BACKENDS: dict[str, type[Jinja2]] = {
    "nitro.templates.engine.Jinja2": Jinja2,
}

DEFAULT_BACKEND = "nitro.templates.engine.Jinja2"


class Templates:
    """Every configured engine, built from ``TEMPLATES`` on first use."""

    def __init__(self) -> None:
        self._engines: dict[str, Jinja2] = {}
        self._built = False

    def _build(self) -> None:
        from nitro.settings import ImproperlyConfigured, settings

        if self._built:
            return

        for index, configuration in enumerate(settings.TEMPLATES):
            backend = configuration.get("BACKEND", DEFAULT_BACKEND)
            engine_class = SUPPORTED_BACKENDS.get(backend)
            if engine_class is None:
                known = ", ".join(sorted(SUPPORTED_BACKENDS))
                raise ImproperlyConfigured(
                    f"TEMPLATES[{index}] names the backend {backend!r}. "
                    f"Nitro ships one template backend; BACKEND must be {known}."
                )
            name = configuration.get("NAME", "default")
            self._engines[name] = engine_class(configuration)

        self._built = True

    def reset(self) -> None:
        """Forget every engine, so the next use rebuilds it from settings."""
        self._engines = {}
        self._built = False

    def __getitem__(self, name: str) -> Jinja2:
        self._build()
        try:
            return self._engines[name]
        except KeyError:
            known = ", ".join(sorted(self._engines)) or "none"
            raise KeyError(
                f"no template engine is named {name!r}; configured engines are {known}"
            ) from None

    def __contains__(self, name: str) -> bool:
        self._build()
        return name in self._engines

    def __iter__(self):
        self._build()
        return iter(self._engines)

    @property
    def default(self) -> Jinja2:
        """The first configured engine."""
        self._build()
        if not self._engines:
            raise RuntimeError(
                "no template engine is configured; add one to the TEMPLATES setting"
            )
        return next(iter(self._engines.values()))

    def _engine(self, using: str | None) -> Jinja2:
        return self[using] if using else self.default

    def get_template(self, template_name: str, using: str | None = None) -> Template:
        """The template `template_name`, from `using` or the default engine."""
        return self._engine(using).get_template(template_name)

    async def render_to_string(
        self,
        template_name: str,
        context: dict[str, Any] | None = None,
        using: str | None = None,
    ) -> str:
        """Render `template_name`, awaiting anything its context needs."""
        return await self._engine(using).render_to_string(template_name, context)

    def render_to_string_sync(
        self,
        template_name: str,
        context: dict[str, Any] | None = None,
        using: str | None = None,
    ) -> str:
        """Render `template_name` without a running loop.

        For a template whose context processors are all synchronous — a
        management command, or the debug pages.
        """
        return self._engine(using).render_to_string_sync(template_name, context)

    @property
    def engines(self) -> dict[str, Jinja2]:
        self._build()
        return self._engines

    def __repr__(self) -> str:
        state = ", ".join(self._engines) if self._built else "not built"
        return f"<Templates [{state}]>"


#: The project's engines.
templates = Templates()
