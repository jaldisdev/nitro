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

import pytest

from nitro.storage.backends.memory import MemoryStorage
from nitro.storage.handler import DEFAULT_STORAGE_ALIAS, StorageHandler


def _make_config(**extra):
    config = {
        "default": {
            "BACKEND": MemoryStorage,
            "LOCATION": "",
        }
    }
    config.update(extra)
    return config


# ---------------------------------------------------------------------------
# __getitem__
# ---------------------------------------------------------------------------


def test_getitem_returns_correct_type():
    handler = StorageHandler(_make_config())
    assert isinstance(handler["default"], MemoryStorage)


def test_getitem_caches_instance():
    handler = StorageHandler(_make_config())
    assert handler["default"] is handler["default"]


def test_getitem_unknown_alias_raises_key_error():
    handler = StorageHandler(_make_config())
    with pytest.raises(KeyError):
        _ = handler["unknown"]


def test_getitem_error_message_lists_available_aliases():
    handler = StorageHandler(_make_config())
    with pytest.raises(KeyError) as exc_info:
        _ = handler["nonexistent"]
    assert "default" in str(exc_info.value)


def test_default_alias_constant():
    assert DEFAULT_STORAGE_ALIAS == "default"


# ---------------------------------------------------------------------------
# _create_storage — backend resolution
# ---------------------------------------------------------------------------


def test_create_storage_accepts_class_directly():
    handler = StorageHandler(_make_config())
    storage = handler._create_storage("default")
    assert isinstance(storage, MemoryStorage)


def test_create_storage_accepts_dotted_string_path():
    config = {
        "default": {
            "BACKEND": "nitro.storage.backends.memory.MemoryStorage",
            "LOCATION": "",
        }
    }
    handler = StorageHandler(config)
    assert isinstance(handler["default"], MemoryStorage)


def test_create_storage_passes_location():
    config = {
        "default": {
            "BACKEND": MemoryStorage,
            "LOCATION": "custom-location",
        }
    }
    handler = StorageHandler(config)
    assert handler["default"].location == "custom-location"


def test_create_storage_passes_options():
    config = {
        "default": {
            "BACKEND": MemoryStorage,
            "LOCATION": "",
            "OPTIONS": {"key": "value"},
        }
    }
    handler = StorageHandler(config)
    assert handler["default"].options == {"key": "value"}


def test_create_storage_passes_base_url():
    config = {
        "default": {
            "BACKEND": MemoryStorage,
            "LOCATION": "",
            "BASE_URL": "https://cdn.example.com",
        }
    }
    handler = StorageHandler(config)
    assert handler["default"].base_url == "https://cdn.example.com"


def test_create_storage_missing_options_defaults_to_empty():
    handler = StorageHandler(_make_config())
    assert handler["default"].options == {}


def test_create_storage_missing_base_url_defaults_to_none():
    handler = StorageHandler(_make_config())
    assert handler["default"].base_url is None


# ---------------------------------------------------------------------------
# all
# ---------------------------------------------------------------------------


def test_all_returns_all_configured_aliases():
    config = _make_config(
        uploads={"BACKEND": MemoryStorage, "LOCATION": ""},
    )
    handler = StorageHandler(config)
    result = handler.all()
    assert set(result.keys()) == {"default", "uploads"}


def test_all_returns_correct_types():
    config = _make_config(
        uploads={"BACKEND": MemoryStorage, "LOCATION": ""},
    )
    handler = StorageHandler(config)
    assert all(isinstance(s, MemoryStorage) for s in handler.all().values())


def test_all_populates_instance_cache():
    handler = StorageHandler(_make_config())
    assert "default" not in handler._storages
    handler.all()
    assert "default" in handler._storages


def test_all_on_empty_config():
    handler = StorageHandler({})
    assert handler.all() == {}


# ---------------------------------------------------------------------------
# multiple aliases are independent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_storages_do_not_share_state():
    config = {
        "a": {"BACKEND": MemoryStorage, "LOCATION": ""},
        "b": {"BACKEND": MemoryStorage, "LOCATION": ""},
    }
    handler = StorageHandler(config)
    await handler["a"].save("shared_name.txt", b"in a")
    assert not await handler["b"].exists("shared_name.txt")


@pytest.mark.asyncio
async def test_multiple_storages_each_hold_own_data():
    config = {
        "a": {"BACKEND": MemoryStorage, "LOCATION": ""},
        "b": {"BACKEND": MemoryStorage, "LOCATION": ""},
    }
    handler = StorageHandler(config)
    await handler["a"].save("file.txt", b"from a")
    await handler["b"].save("file.txt", b"from b")
    assert await handler["a"].read("file.txt") == b"from a"
    assert await handler["b"].read("file.txt") == b"from b"


# ---------------------------------------------------------------------------
# close_all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_all_invokes_close_on_each_storage():
    handler = StorageHandler(_make_config())
    storage = handler["default"]
    await storage.save("file.txt", b"data")
    await handler.close_all()
    assert not await storage.exists("file.txt")


@pytest.mark.asyncio
async def test_close_all_clears_instance_cache():
    handler = StorageHandler(_make_config())
    _ = handler["default"]
    await handler.close_all()
    assert handler._storages == {}


@pytest.mark.asyncio
async def test_close_all_only_closes_instantiated_storages():
    config = _make_config(
        uploads={"BACKEND": MemoryStorage, "LOCATION": ""},
    )
    handler = StorageHandler(config)
    _ = handler["default"]  # instantiate only 'default'
    await handler.close_all()  # 'uploads' was never created; must not raise


@pytest.mark.asyncio
async def test_close_all_reinitialises_on_next_access():
    handler = StorageHandler(_make_config())
    first = handler["default"]
    await handler.close_all()
    second = handler["default"]
    assert first is not second


@pytest.mark.asyncio
async def test_close_all_on_empty_handler_does_not_raise():
    handler = StorageHandler(_make_config())
    await handler.close_all()
