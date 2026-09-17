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

import asyncio
import base64
import logging
from importlib import import_module
from pathlib import Path
from typing import Any

import jinja2
from jinja2 import ChoiceLoader, FileSystemLoader, TemplateNotFound
from jinja2.bccache import Bucket, BytecodeCache

from nitro.templates.exceptions import TemplateDoesNotExist, TemplateSyntaxError

logger = logging.getLogger(__name__)


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

        loader = ChoiceLoader(loaders) if len(loaders) > 1 else (loaders[0] if loaders else None)

        # An explicit bytecode cache wins over the project-wide TEMPLATE_CACHE
        bytecode_cache = None
        if "bytecode_cache" in options:
            cache_cls = options["bytecode_cache"]
            if isinstance(cache_cls, str):
                cache_cls = import_string(cache_cls)
            bytecode_cache = cache_cls()
        else:
            from nitro.settings import settings

            if settings.TEMPLATE_CACHE is not None:
                bytecode_cache = CacheBytecodeCache(settings.TEMPLATE_CACHE)

        env_options = {
            "loader": loader,
            "auto_reload": options.get("auto_reload", False),
            "autoescape": options.get("autoescape", True),
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
        context = {} if context is None else dict(context)

        # Run context processors
        for processor in self.context_processors:
            # Context processors can be sync or async
            result = processor(context)

            # Handle async context processors
            if asyncio.iscoroutine(result):
                # We can't await here in sync method, will be handled by render_to_string
                context["_async_processor_results"] = context.get("_async_processor_results", [])
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
        await self.warm_bytecode()
        template = self.get_template(template_name)
        return await template.render_to_string(context)

    async def warm_bytecode(self) -> None:
        """Load compiled bytecode from the project's cache, when one is configured."""
        if isinstance(self.env.bytecode_cache, CacheBytecodeCache):
            await self.env.bytecode_cache.warm(self.env)

    async def flush_bytecode(self) -> None:
        """Store what was compiled since the last flush in the project's cache."""
        if isinstance(self.env.bytecode_cache, CacheBytecodeCache):
            await self.env.bytecode_cache.flush()

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

        await self.engine.warm_bytecode()
        try:
            # Use render_async if available (Jinja2 3.0+)
            if hasattr(self.template, "render_async"):
                rendered = await self.template.render_async(context)
            else:
                # Fall back to sync rendering in executor
                loop = asyncio.get_event_loop()
                rendered = await loop.run_in_executor(None, self.template.render, context)
        except jinja2.TemplateError as e:
            raise TemplateSyntaxError(str(e)) from e
        # Templates an include or extends reached for the first time were compiled during the render
        await self.engine.flush_bytecode()
        return rendered

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


class CacheBytecodeCache(BytecodeCache):
    """
    Compiled template bytecode kept in one of the project's caches.

    Jinja loads and stores bytecode synchronously while it loads a template, and
    a project cache can only be reached with an await. So Jinja is served from
    this process's own copy: `warm` fills that copy from the cache before a
    render, and `flush` hands back whatever was compiled in the meantime.
    """

    key_prefix = "nitro.templates.bytecode"

    def __init__(self, alias: str) -> None:
        self.alias = alias
        self._local: dict[str, bytes] = {}
        self._pending: dict[str, bytes] = {}
        self._warmed = False

    def load_bytecode(self, bucket: Bucket) -> None:
        data = self._local.get(bucket.key)
        if data is not None:
            # A checksum that no longer matches the source leaves the bucket empty,
            # so a changed template is compiled afresh
            bucket.bytecode_from_string(data)

    def dump_bytecode(self, bucket: Bucket) -> None:
        data = bucket.bytecode_to_string()
        self._local[bucket.key] = data
        self._pending[bucket.key] = data

    def clear(self) -> None:
        self._local.clear()
        self._pending.clear()

    def _cache_key(self, key: str) -> str:
        return f"{self.key_prefix}:{key}"

    def _template_keys(self, environment: jinja2.Environment) -> list[str]:
        loader = environment.loader
        if loader is None:
            return []
        try:
            names = loader.list_templates()
        except TypeError:
            # A loader that cannot enumerate its templates still writes through;
            # only reading ahead of a render is out of reach
            return []
        keys = []
        for name in names:
            _source, filename, _uptodate = loader.get_source(environment, name)
            keys.append(self.get_cache_key(name, filename))
        return keys

    async def warm(self, environment: jinja2.Environment) -> None:
        """Read the bytecode of every template the loader knows, once per process.

        Every template rather than the one about to render, because the ones it
        includes or extends are loaded in the middle of the render, where there
        is no way to await the cache.
        """
        if self._warmed:
            return

        from nitro.cache import caches

        keys = await asyncio.to_thread(self._template_keys, environment)
        try:
            stored = await caches[self.alias].get_many([self._cache_key(key) for key in keys])
        except Exception:
            # The cache is an optimisation: without it templates are compiled, not lost
            logger.exception("template bytecode could not be read from the %r cache", self.alias)
            return

        for key in keys:
            value = stored.get(self._cache_key(key))
            if value is not None and key not in self._local:
                self._local[key] = base64.b64decode(value)
        self._warmed = True

    async def flush(self) -> None:
        """Store bytecode compiled since the last flush."""
        if not self._pending:
            return

        from nitro.cache import caches

        pending, self._pending = self._pending, {}
        # Base64 so the value survives a JSON serializer as well as pickle
        data = {
            self._cache_key(key): base64.b64encode(value).decode("ascii")
            for key, value in pending.items()
        }
        try:
            await caches[self.alias].set_many(data, timeout=0)
        except Exception:
            logger.exception("template bytecode could not be written to the %r cache", self.alias)
            self._pending = {**pending, **self._pending}
