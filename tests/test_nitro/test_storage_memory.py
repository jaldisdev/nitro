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
import io
from datetime import datetime

import pytest

from nitro.protocols import UploadFile
from nitro.storage.backends.memory import MemoryStorage

pytestmark = pytest.mark.asyncio


@pytest.fixture
def storage():
    return MemoryStorage("", {})


@pytest.fixture
def storage_with_base_url():
    return MemoryStorage("", {"BASE_URL": "https://cdn.example.com"})


# ---------------------------------------------------------------------------
# save / read
# ---------------------------------------------------------------------------


async def test_save_returns_name(storage):
    name = await storage.save("file.txt", b"hello")
    assert name == "file.txt"


async def test_save_and_read(storage):
    await storage.save("file.txt", b"hello world")
    assert await storage.read("file.txt") == b"hello world"


async def test_save_overwrites_existing_file(storage):
    await storage.save("file.txt", b"original")
    await storage.save("file.txt", b"updated")
    assert await storage.read("file.txt") == b"updated"


async def test_read_missing_file_raises(storage):
    with pytest.raises(FileNotFoundError):
        await storage.read("missing.txt")


async def test_save_binary_content(storage):
    data = bytes(range(256))
    await storage.save("binary.bin", data)
    assert await storage.read("binary.bin") == data


async def test_save_empty_content(storage):
    await storage.save("empty.txt", b"")
    assert await storage.read("empty.txt") == b""


async def test_save_nested_path(storage):
    await storage.save("docs/reports/q1.txt", b"report")
    assert await storage.read("docs/reports/q1.txt") == b"report"


# ---------------------------------------------------------------------------
# exists
# ---------------------------------------------------------------------------


async def test_exists_true_after_save(storage):
    await storage.save("file.txt", b"data")
    assert await storage.exists("file.txt") is True


async def test_exists_false_for_missing(storage):
    assert await storage.exists("missing.txt") is False


async def test_exists_false_after_delete(storage):
    await storage.save("file.txt", b"data")
    await storage.delete("file.txt")
    assert await storage.exists("file.txt") is False


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_returns_true(storage):
    await storage.save("file.txt", b"data")
    assert await storage.delete("file.txt") is True


async def test_delete_returns_false_for_missing(storage):
    assert await storage.delete("missing.txt") is False


async def test_deleted_file_is_unreadable(storage):
    await storage.save("file.txt", b"data")
    await storage.delete("file.txt")
    with pytest.raises(FileNotFoundError):
        await storage.read("file.txt")


# ---------------------------------------------------------------------------
# size
# ---------------------------------------------------------------------------


async def test_size_returns_byte_count(storage):
    await storage.save("file.txt", b"hello")
    assert await storage.size("file.txt") == 5


async def test_size_empty_file(storage):
    await storage.save("empty.txt", b"")
    assert await storage.size("empty.txt") == 0


async def test_size_updates_after_overwrite(storage):
    await storage.save("file.txt", b"short")
    await storage.save("file.txt", b"much longer content")
    assert await storage.size("file.txt") == len(b"much longer content")


async def test_size_missing_raises(storage):
    with pytest.raises(FileNotFoundError):
        await storage.size("missing.txt")


# ---------------------------------------------------------------------------
# url
# ---------------------------------------------------------------------------


async def test_url_default_returns_memory_scheme(storage):
    url = await storage.url("file.txt")
    assert url == "memory://file.txt"


async def test_url_with_base_url(storage_with_base_url):
    url = await storage_with_base_url.url("file.txt")
    assert url == "https://cdn.example.com/file.txt"


async def test_url_with_base_url_strips_leading_slash(storage_with_base_url):
    url = await storage_with_base_url.url("/docs/report.pdf")
    assert url == "https://cdn.example.com/docs/report.pdf"


async def test_url_with_base_url_no_double_slash(storage_with_base_url):
    url = await storage_with_base_url.url("nested/file.txt")
    assert "//" not in url.replace("https://", "")


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------


async def test_open_as_context_manager(storage):
    await storage.save("file.txt", b"content")
    async with storage.open("file.txt") as f:
        data = await f.read()
    assert data == b"content"


async def test_open_full_read_without_size(storage):
    await storage.save("file.txt", b"hello world")
    async with storage.open("file.txt") as f:
        data = await f.read(-1)
    assert data == b"hello world"


async def test_open_chunked_read(storage):
    await storage.save("file.txt", b"0123456789")
    async with storage.open("file.txt") as f:
        first = await f.read(5)
        second = await f.read(5)
    assert first == b"01234"
    assert second == b"56789"


async def test_open_partial_then_remainder(storage):
    await storage.save("file.txt", b"abcdefgh")
    async with storage.open("file.txt") as f:
        await f.read(3)
        rest = await f.read()
    assert rest == b"defgh"


async def test_open_read_past_end_returns_empty(storage):
    await storage.save("file.txt", b"hi")
    async with storage.open("file.txt") as f:
        await f.read(100)
        tail = await f.read()
    assert tail == b""


async def test_open_missing_raises(storage):
    with pytest.raises(FileNotFoundError):
        storage.open("missing.txt")


# ---------------------------------------------------------------------------
# listdir
# ---------------------------------------------------------------------------


async def test_listdir_root_flat_files(storage):
    await storage.save("a.txt", b"")
    await storage.save("b.txt", b"")
    dirs, files = await storage.listdir("")
    assert sorted(files) == ["a.txt", "b.txt"]
    assert dirs == []


async def test_listdir_root_shows_virtual_subdirectory(storage):
    await storage.save("file.txt", b"")
    await storage.save("docs/report.txt", b"")
    dirs, files = await storage.listdir("")
    assert "file.txt" in files
    assert "docs" in dirs


async def test_listdir_subdirectory_files(storage):
    await storage.save("docs/a.txt", b"")
    await storage.save("docs/b.txt", b"")
    await storage.save("other/c.txt", b"")
    dirs, files = await storage.listdir("docs")
    assert sorted(files) == ["docs/a.txt", "docs/b.txt"]
    assert dirs == []


async def test_listdir_subdirectory_shows_nested_dir(storage):
    await storage.save("docs/sub/deep.txt", b"")
    dirs, files = await storage.listdir("docs")
    assert "docs/sub" in dirs
    assert files == []


async def test_listdir_empty_storage(storage):
    dirs, files = await storage.listdir("")
    assert dirs == []
    assert files == []


async def test_listdir_excludes_files_outside_path(storage):
    await storage.save("other/file.txt", b"")
    dirs, files = await storage.listdir("docs")
    assert files == []
    assert dirs == []


# ---------------------------------------------------------------------------
# timestamps
# ---------------------------------------------------------------------------


async def test_get_created_time_returns_datetime(storage):
    await storage.save("file.txt", b"data")
    assert isinstance(await storage.get_created_time("file.txt"), datetime)


async def test_get_modified_time_returns_datetime(storage):
    await storage.save("file.txt", b"data")
    assert isinstance(await storage.get_modified_time("file.txt"), datetime)


async def test_get_accessed_time_returns_datetime(storage):
    await storage.save("file.txt", b"data")
    assert isinstance(await storage.get_accessed_time("file.txt"), datetime)


async def test_overwrite_resets_modified_time(storage):
    await storage.save("file.txt", b"v1")
    t1 = await storage.get_modified_time("file.txt")
    await asyncio.sleep(0.01)
    await storage.save("file.txt", b"v2")
    t2 = await storage.get_modified_time("file.txt")
    assert t2 >= t1


async def test_timestamps_missing_raises(storage):
    with pytest.raises(FileNotFoundError):
        await storage.get_created_time("missing.txt")


async def test_accessed_time_missing_raises(storage):
    with pytest.raises(FileNotFoundError):
        await storage.get_accessed_time("missing.txt")


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------


async def test_copy_creates_destination(storage):
    await storage.save("src.txt", b"data")
    await storage.copy("src.txt", "dst.txt")
    assert await storage.read("dst.txt") == b"data"


async def test_copy_leaves_source_intact(storage):
    await storage.save("src.txt", b"data")
    await storage.copy("src.txt", "dst.txt")
    assert await storage.exists("src.txt") is True


async def test_copy_destination_is_independent(storage):
    await storage.save("src.txt", b"original")
    await storage.copy("src.txt", "dst.txt")
    await storage.save("src.txt", b"changed")
    assert await storage.read("dst.txt") == b"original"


async def test_copy_missing_source_raises(storage):
    with pytest.raises(FileNotFoundError):
        await storage.copy("missing.txt", "dst.txt")


# ---------------------------------------------------------------------------
# move
# ---------------------------------------------------------------------------


async def test_move_creates_destination(storage):
    await storage.save("src.txt", b"data")
    await storage.move("src.txt", "dst.txt")
    assert await storage.read("dst.txt") == b"data"


async def test_move_removes_source(storage):
    await storage.save("src.txt", b"data")
    await storage.move("src.txt", "dst.txt")
    assert await storage.exists("src.txt") is False


async def test_move_missing_source_raises(storage):
    with pytest.raises(FileNotFoundError):
        await storage.move("missing.txt", "dst.txt")


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


async def test_close_clears_all_files(storage):
    await storage.save("a.txt", b"")
    await storage.save("b.txt", b"")
    await storage.close()
    assert await storage.exists("a.txt") is False
    assert await storage.exists("b.txt") is False


async def test_close_allows_saving_new_files_after(storage):
    await storage.save("file.txt", b"old")
    await storage.close()
    await storage.save("file.txt", b"new")
    assert await storage.read("file.txt") == b"new"


# ---------------------------------------------------------------------------
# save from something other than bytes
# ---------------------------------------------------------------------------


async def chunks_of(*chunks: bytes):
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_save_from_a_file_object(storage):
    await storage.save("from_file.txt", io.BytesIO(b"out of a file"))
    assert await storage.read("from_file.txt") == b"out of a file"


@pytest.mark.asyncio
async def test_save_from_an_upload(storage):
    upload = UploadFile(filename="notes.txt", file=io.BytesIO(b"uploaded bytes"), size=14)
    await storage.save("upload.txt", upload)
    assert await storage.read("upload.txt") == b"uploaded bytes"
    assert await storage.size("upload.txt") == 14


@pytest.mark.asyncio
async def test_save_from_an_async_iterator(storage):
    await storage.save("streamed.txt", chunks_of(b"one ", b"two ", b"three"))
    assert await storage.read("streamed.txt") == b"one two three"


@pytest.mark.asyncio
async def test_save_from_another_backends_file(storage):
    await storage.save("source.txt", b"copied across")
    await storage.save("destination.txt", storage.open("source.txt"))
    assert await storage.read("destination.txt") == b"copied across"
