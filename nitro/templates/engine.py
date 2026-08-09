import asyncio
from importlib import import_module
from pathlib import Path
from typing import Any

import jinja2
from jinja2 import ChoiceLoader, FileSystemLoader, TemplateNotFound

from nitro.templates.exceptions import TemplateDoesNotExist, TemplateSyntaxError


def import_string(dotted_path: str) -> Any:
    """
    Import a module or attribute by dotted path.

    Example: 'jaldis.template.filters.date' -> function object
    """
    try:
        module_path, class_name = dotted_path.rsplit(".", 1)
    except ValueError as err:
        raise ImportError(f"{dotted_path} doesn't look like a module path") from err

    module = import_module(module_path)

    try:
        return getattr(module, class_name)
    except AttributeError as err:
        raise ImportError(
            f'Module "{module_path}" does not define a "{class_name}" attribute/class'
        ) from err


class Jinja2:
    """
    Jinja2 template engine for Nitro.

    This is the main backend that manages Jinja2 environments and template rendering.
    """

    def __init__(self, params: dict[str, Any]):
        """
        Initialize Jinja2 engine with configuration parameters.

        Args:
            params: Configuration dictionary with DIRS, OPTIONS, NAME, etc.
        """
        self.name = params.get("NAME", "default")
        self.dirs = [Path(d) for d in params.get("DIRS", [])]

        options = params.get("OPTIONS", {})

        # Get environment class
        env_cls = options.get("environment", "jinja2.Environment")
        if isinstance(env_cls, str):
            env_cls = import_string(env_cls)

        # Build loader
        loaders = []
        if self.dirs:
            loaders.append(FileSystemLoader([str(d) for d in self.dirs]))

        loader = (
            ChoiceLoader(loaders)
            if len(loaders) > 1
            else (loaders[0] if loaders else None)
        )

        # Get bytecode cache if specified
        bytecode_cache = None
        if "bytecode_cache" in options:
            cache_cls = options["bytecode_cache"]
            if isinstance(cache_cls, str):
                cache_cls = import_string(cache_cls)
            bytecode_cache = cache_cls()

        # Create environment with basic options
        env_options = {
            "loader": loader,
            "auto_reload": options.get("auto_reload", False),
            "autoescape": True,  # Always autoescape for security
            "enable_async": True,
        }

        if bytecode_cache:
            env_options["bytecode_cache"] = bytecode_cache

        self.env = env_cls(**env_options)

        # Register extensions
        for ext in options.get("extensions", []):
            if isinstance(ext, str):
                ext = import_string(ext)
            self.env.add_extension(ext)

        # Register filters
        for name, filter_path in options.get("filters", {}).items():
            if isinstance(filter_path, str):
                filter_func = import_string(filter_path)
            else:
                filter_func = filter_path
            self.env.filters[name] = filter_func

        # Register globals
        for name, global_path in options.get("globals", {}).items():
            if isinstance(global_path, str):
                global_func = import_string(global_path)
            else:
                global_func = global_path
            self.env.globals[name] = global_func

        # Store context processors
        self.context_processors = []
        for processor_path in options.get("context_processors", []):
            if isinstance(processor_path, str):
                processor = import_string(processor_path)
            else:
                processor = processor_path
            self.context_processors.append(processor)

    def _process_context(self, context: dict[str, Any] | None) -> dict[str, Any]:
        """
        Process context through context processors.

        Args:
            context: Initial context dictionary

        Returns:
            Processed context dictionary
        """
        if context is None:
            context = {}
        else:
            context = dict(context)

        # Run context processors
        for processor in self.context_processors:
            # Context processors can be sync or async
            result = processor(context)

            # Handle async context processors
            if asyncio.iscoroutine(result):
                # We can't await here in sync method, will be handled by render_to_string
                context["_async_processor_results"] = context.get(
                    "_async_processor_results", []
                )
                context["_async_processor_results"].append(result)
            else:
                context.update(result)

        return context

    def get_template(self, template_name: str) -> "Template":
        """
        Get a template by name.

        Args:
            template_name: Name/path of the template

        Returns:
            Template instance

        Raises:
            TemplateDoesNotExist: If template cannot be found
        """
        try:
            jinja_template = self.env.get_template(template_name)
            return Template(jinja_template, self)
        except TemplateNotFound as e:
            tried = [str(d / template_name) for d in self.dirs]
            raise TemplateDoesNotExist(str(e), tried=tried) from e
        except jinja2.TemplateSyntaxError as e:
            raise TemplateSyntaxError(str(e)) from e

    async def render_to_string(
        self, template_name: str, context: dict[str, Any] | None = None
    ) -> str:
        """
        Render a template asynchronously.

        Args:
            template_name: Name/path of the template
            context: Context dictionary

        Returns:
            Rendered template string
        """
        template = self.get_template(template_name)
        return await template.render_to_string(context)

    def render_to_string_sync(
        self, template_name: str, context: dict[str, Any] | None = None
    ) -> str:
        """
        Render a template synchronously.

        Args:
            template_name: Name/path of the template
            context: Context dictionary

        Returns:
            Rendered template string
        """
        template = self.get_template(template_name)
        return template.render_to_string_sync(context)


class Template:
    """
    Wrapper around Jinja2 template that handles rendering.
    """

    def __init__(self, template: jinja2.Template, engine: Jinja2):
        """
        Initialize template wrapper.

        Args:
            template: Jinja2 template instance
            engine: Parent engine instance
        """
        self.template = template
        self.engine = engine

    async def render_to_string(self, context: dict[str, Any] | None = None) -> str:
        """
        Render template asynchronously.

        Args:
            context: Context dictionary

        Returns:
            Rendered template string
        """
        context = self.engine._process_context(context)

        # Handle async context processors
        if "_async_processor_results" in context:
            async_results = context.pop("_async_processor_results")
            for coro in async_results:
                result = await coro
                context.update(result)

        try:
            # Use render_async if available (Jinja2 3.0+)
            if hasattr(self.template, "render_async"):
                return await self.template.render_async(context)
            else:
                # Fall back to sync rendering in executor
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(None, self.template.render, context)
        except jinja2.TemplateError as e:
            raise TemplateSyntaxError(str(e)) from e

    def render_to_string_sync(self, context: dict[str, Any] | None = None) -> str:
        """
        Render template synchronously.

        Args:
            context: Context dictionary

        Returns:
            Rendered template string
        """
        context = self.engine._process_context(context)

        # Handle any async context processors that were deferred
        if "_async_processor_results" in context:
            # Can't handle async processors in sync render
            # This is a limitation - use render_to_string if you have async context processors
            del context["_async_processor_results"]

        try:
            return self.template.render(context)
        except jinja2.TemplateError as e:
            raise TemplateSyntaxError(str(e)) from e


class MemcachedBytecodeCache(jinja2.MemcachedBytecodeCache):
    """
    Caches bytecode of parsed template in memcached.

    This is optional and only used if specified in template configuration.
    """

    def __init__(self):
        """
        Initialize bytecode cache using Nitro's cache system.
        """
        from nitro.cache import DEFAULT_CACHE_ALIAS, caches
        from nitro.settings import settings

        cache = caches[getattr(settings, "TEMPLATE_CACHE", DEFAULT_CACHE_ALIAS)]

        self.client = cache._cache
        self.prefix = "template/"
        self.timeout = None
        self.ignore_memcache_errors = True
