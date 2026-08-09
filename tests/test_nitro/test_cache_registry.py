import pytest

from nitro.cache import DEFAULT_CACHE_ALIAS, cache, caches, reset_caches
from nitro.settings import settings

MEMORY = {"BACKEND": "nitro.cache.backends.MemoryCache", "LOCATION": "", "TIMEOUT": 300}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(
        settings,
        "CACHES",
        {"default": dict(MEMORY), "sessions": dict(MEMORY, KEY_PREFIX="sessions")},
        raising=False,
    )
    reset_caches()
    yield
    reset_caches()


class TestRegistry:
    def test_backends_are_built_on_first_use(self):
        assert "not built" in repr(caches)
        caches[DEFAULT_CACHE_ALIAS]
        assert "not built" not in repr(caches)

    def test_the_same_alias_gives_the_same_backend(self):
        assert caches["default"] is caches["default"]

    def test_different_aliases_give_different_backends(self):
        assert caches["default"] is not caches["sessions"]

    def test_an_unknown_alias_lists_the_known_ones(self):
        with pytest.raises(KeyError, match="sessions"):
            caches["nowhere"]

    def test_aliases_can_be_tested_and_iterated(self):
        assert "default" in caches
        assert "nowhere" not in caches
        assert sorted(caches) == ["default", "sessions"]

    async def test_the_shorthand_reaches_the_default(self):
        await cache.set("key", "value")
        assert await caches["default"].get("key") == "value"

    async def test_aliases_do_not_share_values(self):
        await caches["default"].set("key", "one")
        await caches["sessions"].set("key", "two")

        assert await caches["default"].get("key") == "one"
        assert await caches["sessions"].get("key") == "two"

    async def test_closing_releases_every_backend(self):
        await caches["default"].set("key", "value")
        await caches.close_all()
        # Rebuilt on next use, so nothing is carried over.
        assert await caches["default"].get("key") is None

    def test_resetting_forgets_the_backends(self):
        first = caches["default"]
        reset_caches()
        assert caches["default"] is not first
