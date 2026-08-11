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

import gc
import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

import jinja2
import pytest

from nitro.templates.engine import Jinja2, Template, import_string
from nitro.templates.exceptions import (
    TemplateDoesNotExist,
    TemplateError,
    TemplateSyntaxError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def tdir(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    return d


def make_engine(tdir, name="web", options=None):
    return Jinja2(
        {
            "NAME": name,
            "DIRS": [tdir],
            "OPTIONS": options or {},
        }
    )


# ---------------------------------------------------------------------------
# import_string
# ---------------------------------------------------------------------------


class TestImportString:
    def test_imports_known_attribute(self):
        import os.path

        result = import_string("os.path.join")
        assert result is os.path.join

    def test_imports_class(self):
        result = import_string("pathlib.Path")
        assert result is Path

    def test_imports_callable(self):
        import json

        result = import_string("json.dumps")
        assert result is json.dumps

    def test_raises_on_missing_dot(self):
        with pytest.raises(ImportError, match="doesn't look like a module path"):
            import_string("nodots")

    def test_raises_on_missing_module(self):
        with pytest.raises(ImportError):
            import_string("nitro.does_not_exist.something")

    def test_raises_on_missing_attribute(self):
        with pytest.raises(ImportError, match="does not define"):
            import_string("os.path.totally_fake_function")


# ---------------------------------------------------------------------------
# Jinja2 — initialisation
# ---------------------------------------------------------------------------


class TestJinja2Init:
    def test_name_assigned(self, tdir):
        engine = Jinja2({"NAME": "myengine", "DIRS": [tdir], "OPTIONS": {}})
        assert engine.name == "myengine"

    def test_name_defaults_to_default(self, tdir):
        engine = Jinja2({"DIRS": [tdir], "OPTIONS": {}})
        assert engine.name == "default"

    def test_dirs_converted_to_path_objects(self, tdir):
        engine = Jinja2({"DIRS": [str(tdir)], "OPTIONS": {}})
        assert engine.dirs == [tdir]

    def test_autoescape_always_enabled(self, tdir):
        engine = make_engine(tdir)
        assert engine.env.autoescape is True

    def test_async_mode_enabled(self, tdir):
        engine = make_engine(tdir)
        assert engine.env.is_async is True

    def test_auto_reload_defaults_to_false(self, tdir):
        engine = make_engine(tdir)
        assert engine.env.auto_reload is False

    def test_auto_reload_configurable(self, tdir):
        engine = Jinja2({"DIRS": [tdir], "OPTIONS": {"auto_reload": True}})
        assert engine.env.auto_reload is True

    def test_no_context_processors_by_default(self, tdir):
        engine = make_engine(tdir)
        assert engine.context_processors == []

    def test_registers_callable_filter(self, tdir):
        def my_filter(v):
            return v.upper()

        engine = Jinja2({"DIRS": [tdir], "OPTIONS": {"filters": {"shout": my_filter}}})
        assert engine.env.filters["shout"] is my_filter

    def test_registers_string_filter_path(self, tdir):
        import os.path

        engine = Jinja2({"DIRS": [tdir], "OPTIONS": {"filters": {"join": "os.path.join"}}})
        assert engine.env.filters["join"] is os.path.join

    def test_registers_callable_global(self, tdir):
        def helper():
            return "ok"

        engine = Jinja2({"DIRS": [tdir], "OPTIONS": {"globals": {"helper": helper}}})
        assert engine.env.globals["helper"] is helper

    def test_registers_string_global_path(self, tdir):
        import os

        engine = Jinja2({"DIRS": [tdir], "OPTIONS": {"globals": {"getpid": "os.getpid"}}})
        assert engine.env.globals["getpid"] is os.getpid

    def test_registers_callable_context_processor(self, tdir):
        def proc(ctx):
            return {}

        engine = Jinja2({"DIRS": [tdir], "OPTIONS": {"context_processors": [proc]}})
        assert proc in engine.context_processors

    def test_registers_string_context_processor_path(self, tdir):
        import os

        engine = Jinja2(
            {
                "DIRS": [tdir],
                "OPTIONS": {"context_processors": ["os.getcwd"]},
            }
        )
        assert os.getcwd in engine.context_processors

    def test_registers_jinja2_extension(self, tdir):
        engine = Jinja2(
            {
                "DIRS": [tdir],
                "OPTIONS": {"extensions": ["jinja2.ext.loopcontrols"]},
            }
        )
        from jinja2.ext import LoopControlExtension

        assert LoopControlExtension.identifier in engine.env.extensions

    def test_accepts_callable_environment_class(self, tdir):
        engine = Jinja2(
            {
                "DIRS": [tdir],
                "OPTIONS": {"environment": jinja2.Environment},
            }
        )
        assert isinstance(engine.env, jinja2.Environment)


# ---------------------------------------------------------------------------
# Jinja2 — get_template
# ---------------------------------------------------------------------------


class TestJinja2GetTemplate:
    def test_returns_template_wrapper(self, tdir):
        (tdir / "hello.html").write_text("Hello!")
        engine = make_engine(tdir)
        tmpl = engine.get_template("hello.html")
        assert isinstance(tmpl, Template)

    def test_template_wraps_correct_engine(self, tdir):
        (tdir / "hello.html").write_text("Hello!")
        engine = make_engine(tdir)
        tmpl = engine.get_template("hello.html")
        assert tmpl.engine is engine

    def test_raises_template_does_not_exist(self, tdir):
        engine = make_engine(tdir)
        with pytest.raises(TemplateDoesNotExist):
            engine.get_template("ghost.html")

    def test_tried_paths_included_in_exception(self, tdir):
        engine = make_engine(tdir)
        with pytest.raises(TemplateDoesNotExist) as exc_info:
            engine.get_template("missing.html")
        assert any("missing.html" in p for p in exc_info.value.tried)

    def test_tried_path_includes_configured_dir(self, tdir):
        engine = make_engine(tdir)
        with pytest.raises(TemplateDoesNotExist) as exc_info:
            engine.get_template("nope.html")
        assert any(str(tdir) in p for p in exc_info.value.tried)

    def test_raises_template_syntax_error_for_broken_template(self, tdir):
        (tdir / "broken.html").write_text("{% for x in %}oops{% endfor %}")
        engine = make_engine(tdir)
        with pytest.raises(TemplateSyntaxError):
            engine.get_template("broken.html")


# ---------------------------------------------------------------------------
# Jinja2 — async rendering
# ---------------------------------------------------------------------------


class TestJinja2AsyncRendering:
    pytestmark = pytest.mark.asyncio

    async def test_renders_template_with_context(self, tdir):
        (tdir / "greet.html").write_text("Hello, {{ name }}!")
        engine = make_engine(tdir)
        result = await engine.render_to_string("greet.html", {"name": "World"})
        assert result == "Hello, World!"

    async def test_renders_template_without_context(self, tdir):
        (tdir / "static.html").write_text("Static content")
        engine = make_engine(tdir)
        result = await engine.render_to_string("static.html")
        assert result == "Static content"

    async def test_renders_template_with_none_context(self, tdir):
        (tdir / "none_ctx.html").write_text("No vars")
        engine = make_engine(tdir)
        result = await engine.render_to_string("none_ctx.html", None)
        assert result == "No vars"

    async def test_autoescape_encodes_html_entities(self, tdir):
        (tdir / "escape.html").write_text("{{ value }}")
        engine = make_engine(tdir)
        result = await engine.render_to_string("escape.html", {"value": "<b>bold</b>"})
        assert "<b>" not in result
        assert "&lt;b&gt;" in result


# ---------------------------------------------------------------------------
# Jinja2 — sync rendering
# ---------------------------------------------------------------------------


class TestJinja2SyncRendering:
    def test_renders_template_with_context(self, tdir):
        (tdir / "sync.html").write_text("Hi {{ user }}!")
        engine = make_engine(tdir)
        result = engine.render_to_string_sync("sync.html", {"user": "Mario"})
        assert result == "Hi Mario!"

    def test_renders_template_without_context(self, tdir):
        (tdir / "plain.html").write_text("Plain")
        engine = make_engine(tdir)
        result = engine.render_to_string_sync("plain.html")
        assert result == "Plain"

    def test_renders_integer_context_value(self, tdir):
        (tdir / "count.html").write_text("Count: {{ n }}")
        engine = make_engine(tdir)
        result = engine.render_to_string_sync("count.html", {"n": 42})
        assert result == "Count: 42"


# ---------------------------------------------------------------------------
# Template — async rendering
# ---------------------------------------------------------------------------


class TestTemplateAsyncRendering:
    pytestmark = pytest.mark.asyncio

    async def test_renders_with_context(self, tdir):
        (tdir / "tmpl.html").write_text("{{ val }}")
        engine = make_engine(tdir)
        tmpl = engine.get_template("tmpl.html")
        result = await tmpl.render_to_string({"val": "ok"})
        assert result == "ok"

    async def test_renders_with_none_context(self, tdir):
        (tdir / "empty.html").write_text("empty")
        engine = make_engine(tdir)
        tmpl = engine.get_template("empty.html")
        result = await tmpl.render_to_string(None)
        assert result == "empty"

    async def test_sync_context_processor_injects_data(self, tdir):
        (tdir / "proc.html").write_text("{{ site }}")

        def site_processor(ctx):
            return {"site": "Nitro"}

        engine = Jinja2(
            {
                "DIRS": [tdir],
                "OPTIONS": {"context_processors": [site_processor]},
            }
        )
        result = await engine.get_template("proc.html").render_to_string({})
        assert result == "Nitro"

    async def test_async_context_processor_injects_data(self, tdir):
        (tdir / "async_proc.html").write_text("{{ username }}")

        async def user_processor(ctx):
            return {"username": "alice"}

        engine = Jinja2(
            {
                "DIRS": [tdir],
                "OPTIONS": {"context_processors": [user_processor]},
            }
        )
        result = await engine.get_template("async_proc.html").render_to_string({})
        assert result == "alice"

    async def test_multiple_sync_context_processors_merged(self, tdir):
        (tdir / "multi.html").write_text("{{ a }}-{{ b }}")

        engine = Jinja2(
            {
                "DIRS": [tdir],
                "OPTIONS": {
                    "context_processors": [
                        lambda ctx: {"a": "A"},
                        lambda ctx: {"b": "B"},
                    ],
                },
            }
        )
        result = await engine.get_template("multi.html").render_to_string({})
        assert result == "A-B"

    async def test_multiple_async_context_processors_merged(self, tdir):
        (tdir / "async_multi.html").write_text("{{ x }}-{{ y }}")

        async def proc_x(ctx):
            return {"x": "X"}

        async def proc_y(ctx):
            return {"y": "Y"}

        engine = Jinja2(
            {
                "DIRS": [tdir],
                "OPTIONS": {"context_processors": [proc_x, proc_y]},
            }
        )
        result = await engine.get_template("async_multi.html").render_to_string({})
        assert result == "X-Y"

    async def test_custom_filter_applied_in_template(self, tdir):
        (tdir / "filtered.html").write_text("{{ name|shout }}")

        engine = Jinja2(
            {
                "DIRS": [tdir],
                "OPTIONS": {"filters": {"shout": lambda v: v.upper() + "!"}},
            }
        )
        result = await engine.get_template("filtered.html").render_to_string({"name": "nitro"})
        assert result == "NITRO!"

    async def test_global_function_accessible_in_template(self, tdir):
        (tdir / "global.html").write_text("{{ greeting() }}")

        engine = Jinja2(
            {
                "DIRS": [tdir],
                "OPTIONS": {"globals": {"greeting": lambda: "hi there"}},
            }
        )
        result = await engine.get_template("global.html").render_to_string({})
        assert result == "hi there"


# ---------------------------------------------------------------------------
# Template — sync rendering
# ---------------------------------------------------------------------------


class TestTemplateSyncRendering:
    def test_renders_with_context(self, tdir):
        (tdir / "tmpl.html").write_text("{{ val }}")
        engine = make_engine(tdir)
        result = engine.get_template("tmpl.html").render_to_string_sync({"val": "sync"})
        assert result == "sync"

    def test_renders_with_none_context(self, tdir):
        (tdir / "none.html").write_text("static")
        engine = make_engine(tdir)
        result = engine.get_template("none.html").render_to_string_sync(None)
        assert result == "static"

    def test_sync_context_processor_applied(self, tdir):
        (tdir / "ver.html").write_text("{{ version }}")

        engine = Jinja2(
            {
                "DIRS": [tdir],
                "OPTIONS": {"context_processors": [lambda ctx: {"version": "2.0"}]},
            }
        )
        result = engine.get_template("ver.html").render_to_string_sync({})
        assert result == "2.0"

    def test_async_context_processor_dropped_gracefully(self, tdir):
        (tdir / "dropped.html").write_text("{{ maybe }}")

        async def async_proc(ctx):
            return {"maybe": "never"}

        engine = Jinja2(
            {
                "DIRS": [tdir],
                "OPTIONS": {"context_processors": [async_proc]},
            }
        )
        # The sync path discards the unawaited coroutine. Python emits the
        # "never awaited" warning at GC time, which may fall outside the test
        # frame. Force collection while the filter is still active.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            result = engine.get_template("dropped.html").render_to_string_sync({})
            gc.collect()

        assert result == ""

    def test_raises_template_syntax_error_on_render_error(self, tdir):
        (tdir / "bad_filter.html").write_text("{{ val|explode }}")

        def explode(val):
            raise jinja2.TemplateError("boom")

        engine = Jinja2(
            {
                "DIRS": [tdir],
                "OPTIONS": {"filters": {"explode": explode}},
            }
        )
        with pytest.raises(TemplateSyntaxError):
            engine.get_template("bad_filter.html").render_to_string_sync({"val": "x"})


# ---------------------------------------------------------------------------
# MemcachedBytecodeCache
# ---------------------------------------------------------------------------


class TestMemcachedBytecodeCache:
    def test_initialises_with_mocked_cache(self):
        from nitro.templates.engine import MemcachedBytecodeCache

        mock_inner = MagicMock()
        mock_cache = MagicMock()
        mock_cache._cache = mock_inner

        mock_caches = MagicMock()
        mock_caches.__getitem__ = MagicMock(return_value=mock_cache)

        mock_settings = MagicMock()
        mock_settings.TEMPLATE_CACHE = "default"

        with (
            patch("nitro.cache.caches", mock_caches),
            patch("nitro.cache.DEFAULT_CACHE_ALIAS", "default"),
            patch("nitro.settings.settings", mock_settings),
        ):
            cache = MemcachedBytecodeCache()

        assert cache.client is mock_inner
        assert cache.prefix == "template/"
        assert cache.timeout is None
        assert cache.ignore_memcache_errors is True

    def test_falls_back_to_default_alias_when_setting_absent(self):
        from nitro.templates.engine import MemcachedBytecodeCache

        mock_inner = MagicMock()
        mock_cache = MagicMock()
        mock_cache._cache = mock_inner

        mock_caches = MagicMock()
        mock_caches.__getitem__ = MagicMock(return_value=mock_cache)

        mock_settings = MagicMock(spec=[])  # no TEMPLATE_CACHE attribute

        with (
            patch("nitro.cache.caches", mock_caches),
            patch("nitro.cache.DEFAULT_CACHE_ALIAS", "default"),
            patch("nitro.settings.settings", mock_settings),
        ):
            cache = MemcachedBytecodeCache()

        assert cache.client is mock_inner


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TestExceptions:
    def test_template_does_not_exist_default_tried_is_empty(self):
        exc = TemplateDoesNotExist("not found")
        assert exc.tried == []

    def test_template_does_not_exist_stores_tried_paths(self):
        paths = ["/a/tmpl.html", "/b/tmpl.html"]
        exc = TemplateDoesNotExist("not found", tried=paths)
        assert exc.tried == paths

    def test_template_does_not_exist_message(self):
        exc = TemplateDoesNotExist("my_template.html")
        assert str(exc) == "my_template.html"

    def test_template_does_not_exist_is_exception(self):
        assert issubclass(TemplateDoesNotExist, Exception)

    def test_template_syntax_error_is_subclass_of_template_error(self):
        assert issubclass(TemplateSyntaxError, TemplateError)

    def test_template_error_is_exception(self):
        assert issubclass(TemplateError, Exception)

    def test_template_syntax_error_instance_of_template_error(self):
        exc = TemplateSyntaxError("bad syntax")
        assert isinstance(exc, TemplateError)

    def test_template_syntax_error_message(self):
        exc = TemplateSyntaxError("unexpected token")
        assert str(exc) == "unexpected token"
