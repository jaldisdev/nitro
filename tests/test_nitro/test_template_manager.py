from unittest.mock import MagicMock, patch

import pytest

from nitro.templates.engine import Jinja2, Template
from nitro.templates.exceptions import TemplateDoesNotExist
from nitro.templates.templates import Templates

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_engine(tdir, name="web"):
    return Jinja2(
        {
            "NAME": name,
            "DIRS": [tdir],
            "OPTIONS": {},
        }
    )


def seeded_manager(*engines) -> Templates:
    """Return a fully initialised Templates instance with given engines."""
    mgr = Templates()
    mgr._engines = {e.name: e for e in engines}
    mgr._built = True
    return mgr


@pytest.fixture
def tdir(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    (d / "index.html").write_text("Index")
    return d


# ---------------------------------------------------------------------------
# _build
# ---------------------------------------------------------------------------


class TestEnsureInitialized:
    def test_initialises_engine_from_settings(self, tdir):
        mock_settings = MagicMock()
        mock_settings.TEMPLATES = [
            {
                "BACKEND": "nitro.templates.engine.Jinja2",
                "NAME": "web",
                "DIRS": [tdir],
                "OPTIONS": {},
            }
        ]
        with patch("nitro.settings.settings", mock_settings):
            mgr = Templates()
            mgr._build()
        assert "web" in mgr.engines

    def test_initialises_multiple_engines(self, tmp_path):
        web_dir = tmp_path / "web"
        email_dir = tmp_path / "email"
        web_dir.mkdir()
        email_dir.mkdir()

        mock_settings = MagicMock()
        mock_settings.TEMPLATES = [
            {
                "BACKEND": "nitro.templates.engine.Jinja2",
                "NAME": "web",
                "DIRS": [web_dir],
                "OPTIONS": {},
            },
            {
                "BACKEND": "nitro.templates.engine.Jinja2",
                "NAME": "email",
                "DIRS": [email_dir],
                "OPTIONS": {},
            },
        ]
        with patch("nitro.settings.settings", mock_settings):
            mgr = Templates()
            mgr._build()
        assert set(mgr.engines.keys()) == {"web", "email"}

    def test_empty_settings_produces_no_engines(self):
        mock_settings = MagicMock()
        mock_settings.TEMPLATES = []
        with patch("nitro.settings.settings", mock_settings):
            mgr = Templates()
            mgr._build()
        assert mgr.engines == {}

    def test_a_settings_object_without_templates_says_so(self):
        # TEMPLATES is always in the defaults, so a settings object missing it
        # is a broken one. Reporting the name beats reporting "no engines
        # configured" from somewhere else much later.
        mock_settings = MagicMock(spec=[])  # no TEMPLATES attribute
        with patch("nitro.settings.settings", mock_settings):
            mgr = Templates()
            with pytest.raises(AttributeError, match="TEMPLATES"):
                mgr._build()

    def test_sets_the_built_flag(self, tdir):
        mock_settings = MagicMock()
        mock_settings.TEMPLATES = [
            {
                "BACKEND": "nitro.templates.engine.Jinja2",
                "NAME": "web",
                "DIRS": [tdir],
                "OPTIONS": {},
            }
        ]
        with patch("nitro.settings.settings", mock_settings):
            mgr = Templates()
            assert mgr._built is False
            mgr._build()
            assert mgr._built is True

    def test_is_idempotent_once_built(self, tdir):
        engine = make_engine(tdir)
        mgr = seeded_manager(engine)
        original_engines = dict(mgr._engines)
        mgr._build()
        assert mgr._engines == original_engines

    def test_raises_for_unsupported_backend(self, tdir):
        mock_settings = MagicMock()
        mock_settings.TEMPLATES = [
            {
                "BACKEND": "myapp.template.UnknownBackend",
                "NAME": "web",
                "DIRS": [tdir],
                "OPTIONS": {},
            }
        ]
        with patch("nitro.settings.settings", mock_settings):
            mgr = Templates()
            from nitro.settings import ImproperlyConfigured

            with pytest.raises(ImproperlyConfigured, match="ships one template backend"):
                mgr._build()

    def test_default_backend_assumed_when_omitted(self, tdir):
        mock_settings = MagicMock()
        mock_settings.TEMPLATES = [
            {
                "NAME": "web",
                "DIRS": [tdir],
                "OPTIONS": {},
            }
        ]
        with patch("nitro.settings.settings", mock_settings):
            mgr = Templates()
            mgr._build()
        assert "web" in mgr.engines


# ---------------------------------------------------------------------------
# __getitem__
# ---------------------------------------------------------------------------


class TestGetItem:
    def test_returns_engine_by_name(self, tdir):
        engine = make_engine(tdir, "web")
        mgr = seeded_manager(engine)
        assert mgr["web"] is engine

    def test_returns_correct_engine_among_multiple(self, tmp_path):
        web_dir = tmp_path / "web"
        email_dir = tmp_path / "email"
        web_dir.mkdir()
        email_dir.mkdir()
        web_engine = make_engine(web_dir, "web")
        email_engine = make_engine(email_dir, "email")
        mgr = seeded_manager(web_engine, email_engine)
        assert mgr["email"] is email_engine

    def test_raises_key_error_for_unknown_name(self, tdir):
        mgr = seeded_manager(make_engine(tdir, "web"))
        with pytest.raises(KeyError):
            _ = mgr["pdf"]

    def test_key_error_lists_available_engines(self, tdir):
        mgr = seeded_manager(make_engine(tdir, "web"))
        with pytest.raises(KeyError) as exc_info:
            _ = mgr["missing"]
        assert "web" in str(exc_info.value)


# ---------------------------------------------------------------------------
# default property
# ---------------------------------------------------------------------------


class TestDefault:
    def test_returns_first_configured_engine(self, tdir):
        engine = make_engine(tdir, "web")
        mgr = seeded_manager(engine)
        assert mgr.default is engine

    def test_returns_first_when_multiple_engines(self, tmp_path):
        a_dir = tmp_path / "a"
        b_dir = tmp_path / "b"
        a_dir.mkdir()
        b_dir.mkdir()
        a_engine = make_engine(a_dir, "a")
        b_engine = make_engine(b_dir, "b")
        mgr = seeded_manager(a_engine, b_engine)
        assert mgr.default is a_engine

    def test_raises_runtime_error_when_no_engines_configured(self):
        mgr = Templates()
        mgr._engines = {}
        mgr._built = True
        with pytest.raises(RuntimeError, match="no template engine is configured"):
            _ = mgr.default


# ---------------------------------------------------------------------------
# engines property
# ---------------------------------------------------------------------------


class TestEnginesProperty:
    def test_returns_dict_of_all_engines(self, tdir):
        engine = make_engine(tdir, "web")
        mgr = seeded_manager(engine)
        engines = mgr.engines
        assert isinstance(engines, dict)
        assert engines["web"] is engine

    def test_returns_empty_dict_when_none_configured(self):
        mgr = Templates()
        mgr._engines = {}
        mgr._built = True
        assert mgr.engines == {}

    def test_returns_all_named_engines(self, tmp_path):
        a_dir = tmp_path / "a"
        b_dir = tmp_path / "b"
        a_dir.mkdir()
        b_dir.mkdir()
        mgr = seeded_manager(make_engine(a_dir, "a"), make_engine(b_dir, "b"))
        assert set(mgr.engines.keys()) == {"a", "b"}


# ---------------------------------------------------------------------------
# get_template
# ---------------------------------------------------------------------------


class TestGetTemplate:
    def test_returns_template_from_default_engine(self, tdir):
        mgr = seeded_manager(make_engine(tdir, "web"))
        tmpl = mgr.get_template("index.html")
        assert isinstance(tmpl, Template)

    def test_returns_template_from_named_engine(self, tmp_path):
        web_dir = tmp_path / "web"
        email_dir = tmp_path / "email"
        web_dir.mkdir()
        email_dir.mkdir()
        (email_dir / "welcome.html").write_text("Welcome!")
        mgr = seeded_manager(
            make_engine(web_dir, "web"), make_engine(email_dir, "email")
        )
        tmpl = mgr.get_template("welcome.html", using="email")
        assert isinstance(tmpl, Template)

    def test_raises_template_does_not_exist(self, tdir):
        mgr = seeded_manager(make_engine(tdir, "web"))
        with pytest.raises(TemplateDoesNotExist):
            mgr.get_template("ghost.html")

    def test_raises_key_error_for_unknown_engine(self, tdir):
        mgr = seeded_manager(make_engine(tdir, "web"))
        with pytest.raises(KeyError):
            mgr.get_template("index.html", using="pdf")


# ---------------------------------------------------------------------------
# render_to_string (async)
# ---------------------------------------------------------------------------


class TestRenderToString:
    pytestmark = pytest.mark.asyncio

    async def test_renders_via_default_engine(self, tdir):
        (tdir / "page.html").write_text("Hello {{ name }}")
        mgr = seeded_manager(make_engine(tdir, "web"))
        result = await mgr.render_to_string("page.html", {"name": "World"})
        assert result == "Hello World"

    async def test_renders_via_named_engine(self, tmp_path):
        web_dir = tmp_path / "web"
        email_dir = tmp_path / "email"
        web_dir.mkdir()
        email_dir.mkdir()
        (email_dir / "mail.html").write_text("Subject: {{ subject }}")
        mgr = seeded_manager(
            make_engine(web_dir, "web"), make_engine(email_dir, "email")
        )
        result = await mgr.render_to_string(
            "mail.html", {"subject": "Hi"}, using="email"
        )
        assert result == "Subject: Hi"

    async def test_renders_with_none_context(self, tdir):
        (tdir / "static.html").write_text("Static")
        mgr = seeded_manager(make_engine(tdir, "web"))
        result = await mgr.render_to_string("static.html", None)
        assert result == "Static"

    async def test_renders_without_context_argument(self, tdir):
        (tdir / "bare.html").write_text("Bare")
        mgr = seeded_manager(make_engine(tdir, "web"))
        result = await mgr.render_to_string("bare.html")
        assert result == "Bare"


# ---------------------------------------------------------------------------
# render_to_string_sync
# ---------------------------------------------------------------------------


class TestRenderToStringSync:
    def test_renders_via_default_engine(self, tdir):
        (tdir / "sync.html").write_text("Hi {{ user }}")
        mgr = seeded_manager(make_engine(tdir, "web"))
        result = mgr.render_to_string_sync("sync.html", {"user": "Alice"})
        assert result == "Hi Alice"

    def test_renders_via_named_engine(self, tmp_path):
        web_dir = tmp_path / "web"
        email_dir = tmp_path / "email"
        web_dir.mkdir()
        email_dir.mkdir()
        (email_dir / "msg.html").write_text("Body: {{ body }}")
        mgr = seeded_manager(
            make_engine(web_dir, "web"), make_engine(email_dir, "email")
        )
        result = mgr.render_to_string_sync("msg.html", {"body": "test"}, using="email")
        assert result == "Body: test"

    def test_renders_with_none_context(self, tdir):
        (tdir / "plain.html").write_text("Plain")
        mgr = seeded_manager(make_engine(tdir, "web"))
        result = mgr.render_to_string_sync("plain.html", None)
        assert result == "Plain"

    def test_renders_without_context_argument(self, tdir):
        (tdir / "no_args.html").write_text("OK")
        mgr = seeded_manager(make_engine(tdir, "web"))
        result = mgr.render_to_string_sync("no_args.html")
        assert result == "OK"

    def test_raises_runtime_error_when_no_engines(self):
        mgr = Templates()
        mgr._engines = {}
        mgr._built = True
        with pytest.raises(RuntimeError):
            mgr.render_to_string_sync("any.html")
