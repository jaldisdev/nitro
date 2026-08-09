from typing import Any

from nitro.templates.engine import Jinja2


class Templates:
    """
    Manager for multiple template engines.

    This allows you to configure multiple template engines (e.g., one for web,
    one for email, one for PDFs) and access them by name.

    Usage:
        from nitro.templates import templates

        # Render with default engine
        html = await templates.render_to_string('index.html', {'title': 'Home'})

        # Render with named engine
        html = await templates['email'].render_to_string('welcome.html', context)

        # Or synchronously if no async context processors
        html = templates.render('error.html', {'code': 404})
    """

    def __init__(self):
        """Initialize template manager."""
        self._engines: dict[str, Jinja2] = {}
        self._initialized = False

    def _ensure_initialized(self):
        """
        Lazily initialize template engines from configuration.
        """
        if self._initialized:
            return

        from nitro.settings import settings

        template_configs = getattr(settings, "TEMPLATES", [])

        if not template_configs:
            # No templates configured
            self._initialized = True
            return

        for config in template_configs:
            backend = config.get("BACKEND", "nitro.templates.engine.Jinja2")

            # For now we only support Jinja2
            if not backend.endswith("Jinja2"):
                raise ValueError(f"Unsupported template backend: {backend}")

            name = config.get("NAME", "default")
            engine = Jinja2(config)
            self._engines[name] = engine

        self._initialized = True

    def __getitem__(self, name: str) -> Jinja2:
        """
        Get template engine by name.

        Args:
            name: Engine name (e.g., 'web', 'email', 'pdf')

        Returns:
            Template engine instance

        Raises:
            KeyError: If engine with given name doesn't exist
        """
        self._ensure_initialized()

        if name not in self._engines:
            raise KeyError(
                f'Template engine "{name}" not found. Available: {list(self._engines.keys())}'
            )

        return self._engines[name]

    @property
    def default(self) -> Jinja2:
        """
        Get the default template engine.

        Returns the first configured engine or raises error if none configured.

        Returns:
            Default template engine

        Raises:
            RuntimeError: If no template engines are configured
        """
        self._ensure_initialized()

        if not self._engines:
            raise RuntimeError("No template engines configured")

        # Return first engine
        return next(iter(self._engines.values()))

    def get_template(self, template_name: str, using: str | None = None):
        """
        Get a template from specified engine or default.

        Args:
            template_name: Template name/path
            using: Optional engine name, uses default if not specified

        Returns:
            Template instance
        """
        if using:
            engine = self[using]
        else:
            engine = self.default

        return engine.get_template(template_name)

    async def render_to_string(
        self,
        template_name: str,
        context: dict[str, Any] | None = None,
        using: str | None = None,
    ) -> str:
        """
        Render a template asynchronously using default or specified engine.

        Args:
            template_name: Template name/path
            context: Context dictionary
            using: Optional engine name, uses default if not specified

        Returns:
            Rendered template string
        """
        if using:
            engine = self[using]
        else:
            engine = self.default

        return await engine.render_to_string(template_name, context)

    def render_to_string_sync(
        self,
        template_name: str,
        context: dict[str, Any] | None = None,
        using: str | None = None,
    ) -> str:
        """
        Render a template synchronously using default or specified engine.

        Args:
            template_name: Template name/path
            context: Context dictionary
            using: Optional engine name, uses default if not specified

        Returns:
            Rendered template string
        """
        if using:
            engine = self[using]
        else:
            engine = self.default

        return engine.render_to_string_sync(template_name, context)

    @property
    def engines(self) -> dict[str, Jinja2]:
        """
        Get all configured template engines.

        Returns:
            Dictionary of engine name -> engine instance
        """
        self._ensure_initialized()
        return self._engines


# Global templates instance
templates = Templates()
