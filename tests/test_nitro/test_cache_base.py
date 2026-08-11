from nitro.cache.backends.memory import MemoryCache
from nitro.cache.base import DEFAULT_TIMEOUT


def make_memory_cache(
    prefix: str = "",
    version: int = 1,
    timeout: int = 300,
) -> MemoryCache:
    return MemoryCache("", {"KEY_PREFIX": prefix, "VERSION": version, "TIMEOUT": timeout})


# ---------------------------------------------------------------------------
# make_key
# ---------------------------------------------------------------------------


class TestMakeKey:
    def test_default_prefix_and_version(self) -> None:
        cache = MemoryCache("", {"KEY_PREFIX": "", "VERSION": 1})
        assert cache.make_key("foo") == ":1:foo"

    def test_with_prefix(self) -> None:
        cache = MemoryCache("", {"KEY_PREFIX": "nitro", "VERSION": 1})
        assert cache.make_key("foo") == "nitro:1:foo"

    def test_with_explicit_version(self) -> None:
        cache = MemoryCache("", {"KEY_PREFIX": "", "VERSION": 1})
        assert cache.make_key("foo", version=3) == ":3:foo"

    def test_prefix_and_explicit_version(self) -> None:
        cache = MemoryCache("", {"KEY_PREFIX": "app", "VERSION": 2})
        assert cache.make_key("bar", version=5) == "app:5:bar"

    def test_falls_back_to_instance_version(self) -> None:
        cache = MemoryCache("", {"KEY_PREFIX": "", "VERSION": 7})
        assert cache.make_key("x") == ":7:x"

    def test_empty_key(self) -> None:
        cache = MemoryCache("", {"KEY_PREFIX": "p", "VERSION": 1})
        assert cache.make_key("") == "p:1:"


# ---------------------------------------------------------------------------
# get_backend_timeout
# ---------------------------------------------------------------------------


class TestGetBackendTimeout:
    def test_none_returns_default(self) -> None:
        cache = MemoryCache("", {"TIMEOUT": 60})
        assert cache.get_backend_timeout(None) == 60

    def test_explicit_value_returned_as_is(self) -> None:
        cache = MemoryCache("", {"TIMEOUT": 60})
        assert cache.get_backend_timeout(120) == 120

    def test_zero_means_never_expire(self) -> None:
        cache = MemoryCache("", {"TIMEOUT": 60})
        assert cache.get_backend_timeout(0) is None

    def test_default_timeout_constant(self) -> None:
        cache = MemoryCache("", {})
        assert cache.default_timeout == DEFAULT_TIMEOUT
        assert DEFAULT_TIMEOUT == 300

    def test_params_without_timeout_uses_default(self) -> None:
        cache = MemoryCache("", {})
        assert cache.get_backend_timeout(None) == DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# init params
# ---------------------------------------------------------------------------


class TestInitParams:
    def test_location_stored(self) -> None:
        cache = MemoryCache("some-location", {})
        assert cache.location == "some-location"

    def test_options_stored(self) -> None:
        cache = MemoryCache("", {"OPTIONS": {"max_connections": 10}})
        assert cache.options == {"max_connections": 10}

    def test_empty_options_default(self) -> None:
        cache = MemoryCache("", {})
        assert cache.options == {}

    def test_version_default(self) -> None:
        cache = MemoryCache("", {})
        assert cache.version == 1

    def test_key_prefix_default(self) -> None:
        cache = MemoryCache("", {})
        assert cache.key_prefix == ""
