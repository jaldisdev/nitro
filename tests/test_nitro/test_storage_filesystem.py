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

import io
from datetime import datetime

import pytest

from nitro.protocols import UploadFile
from nitro.storage.backends.filesystem import FileSystemStorage
from nitro.utils.content import CHUNK_SIZE


@pytest.fixture
def storage(tmp_path):
    return FileSystemStorage(str(tmp_path / "storage"), {})


@pytest.fixture
def storage_with_base_url(tmp_path):
    return FileSystemStorage(str(tmp_path / "storage"), {"BASE_URL": "/media"})


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_creates_base_directory(tmp_path):
    path = tmp_path / "new_dir"
    assert not path.exists()
    FileSystemStorage(str(path), {})
    assert path.is_dir()


def test_init_creates_nested_base_directory(tmp_path):
    path = tmp_path / "a" / "b" / "c"
    FileSystemStorage(str(path), {})
    assert path.is_dir()


def test_init_accepts_existing_directory(tmp_path):
    path = tmp_path / "existing"
    path.mkdir()
    FileSystemStorage(str(path), {})  # must not raise


# ---------------------------------------------------------------------------
# save / read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_returns_name(storage):
    name = await storage.save("file.txt", b"hello")
    assert name == "file.txt"


@pytest.mark.asyncio
async def test_save_writes_bytes_to_disk(storage, tmp_path):
    await storage.save("file.txt", b"hello")
    assert (tmp_path / "storage" / "file.txt").read_bytes() == b"hello"


@pytest.mark.asyncio
async def test_save_creates_parent_directories(storage, tmp_path):
    await storage.save("docs/reports/q1.txt", b"data")
    assert (tmp_path / "storage" / "docs" / "reports" / "q1.txt").exists()


@pytest.mark.asyncio
async def test_save_and_read_roundtrip(storage):
    await storage.save("file.txt", b"hello world")
    assert await storage.read("file.txt") == b"hello world"


@pytest.mark.asyncio
async def test_save_overwrites_existing_file(storage):
    await storage.save("file.txt", b"original")
    await storage.save("file.txt", b"updated")
    assert await storage.read("file.txt") == b"updated"


@pytest.mark.asyncio
async def test_read_missing_raises(storage):
    with pytest.raises(FileNotFoundError):
        await storage.read("missing.txt")


@pytest.mark.asyncio
async def test_save_binary_content(storage):
    data = bytes(range(256))
    await storage.save("binary.bin", data)
    assert await storage.read("binary.bin") == data


@pytest.mark.asyncio
async def test_save_empty_file(storage):
    await storage.save("empty.txt", b"")
    assert await storage.read("empty.txt") == b""


# ---------------------------------------------------------------------------
# exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exists_true_after_save(storage):
    await storage.save("file.txt", b"data")
    assert await storage.exists("file.txt") is True


@pytest.mark.asyncio
async def test_exists_false_for_missing(storage):
    assert await storage.exists("missing.txt") is False


@pytest.mark.asyncio
async def test_exists_false_after_delete(storage):
    await storage.save("file.txt", b"data")
    await storage.delete("file.txt")
    assert await storage.exists("file.txt") is False


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_returns_true(storage):
    await storage.save("file.txt", b"data")
    assert await storage.delete("file.txt") is True


@pytest.mark.asyncio
async def test_delete_removes_file_from_disk(storage, tmp_path):
    await storage.save("file.txt", b"data")
    await storage.delete("file.txt")
    assert not (tmp_path / "storage" / "file.txt").exists()


@pytest.mark.asyncio
async def test_delete_returns_false_for_missing(storage):
    assert await storage.delete("missing.txt") is False


@pytest.mark.asyncio
async def test_delete_prunes_empty_parent_directories(storage, tmp_path):
    await storage.save("deep/nested/file.txt", b"data")
    await storage.delete("deep/nested/file.txt")
    assert not (tmp_path / "storage" / "deep").exists()


@pytest.mark.asyncio
async def test_delete_preserves_nonempty_parent_directory(storage, tmp_path):
    await storage.save("dir/a.txt", b"")
    await storage.save("dir/b.txt", b"")
    await storage.delete("dir/a.txt")
    assert (tmp_path / "storage" / "dir").is_dir()


@pytest.mark.asyncio
async def test_delete_does_not_prune_base_directory(storage, tmp_path):
    await storage.save("file.txt", b"data")
    await storage.delete("file.txt")
    assert (tmp_path / "storage").is_dir()


# ---------------------------------------------------------------------------
# size
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_size_returns_byte_count(storage):
    await storage.save("file.txt", b"hello")
    assert await storage.size("file.txt") == 5


@pytest.mark.asyncio
async def test_size_empty_file(storage):
    await storage.save("empty.txt", b"")
    assert await storage.size("empty.txt") == 0


@pytest.mark.asyncio
async def test_size_matches_content_length(storage):
    data = b"x" * 1024
    await storage.save("file.bin", data)
    assert await storage.size("file.bin") == 1024


@pytest.mark.asyncio
async def test_size_missing_raises(storage):
    with pytest.raises(FileNotFoundError):
        await storage.size("missing.txt")


# ---------------------------------------------------------------------------
# url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_url_default_returns_file_uri(storage):
    await storage.save("file.txt", b"")
    url = await storage.url("file.txt")
    assert url.startswith("file://")
    assert "file.txt" in url


@pytest.mark.asyncio
async def test_url_with_base_url(storage_with_base_url):
    url = await storage_with_base_url.url("uploads/photo.jpg")
    assert url == "/media/uploads/photo.jpg"


@pytest.mark.asyncio
async def test_url_with_base_url_strips_leading_slash(storage_with_base_url):
    url = await storage_with_base_url.url("/uploads/photo.jpg")
    assert url == "/media/uploads/photo.jpg"


@pytest.mark.asyncio
async def test_url_with_base_url_no_double_slash(storage_with_base_url):
    url = await storage_with_base_url.url("file.txt")
    assert url == "/media/file.txt"


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_as_context_manager(storage):
    await storage.save("file.txt", b"content")
    async with storage.open("file.txt") as f:
        data = await f.read()
    assert data == b"content"


@pytest.mark.asyncio
async def test_open_chunked_read(storage):
    await storage.save("file.txt", b"0123456789")
    async with storage.open("file.txt") as f:
        first = await f.read(5)
        second = await f.read(5)
    assert first == b"01234"
    assert second == b"56789"


@pytest.mark.asyncio
async def test_open_partial_then_remainder(storage):
    await storage.save("file.txt", b"abcdefgh")
    async with storage.open("file.txt") as f:
        await f.read(3)
        rest = await f.read()
    assert rest == b"defgh"


@pytest.mark.asyncio
async def test_open_missing_raises(storage):
    with pytest.raises(FileNotFoundError):
        storage.open("missing.txt")


# ---------------------------------------------------------------------------
# listdir
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listdir_root_flat_files(storage):
    await storage.save("a.txt", b"")
    await storage.save("b.txt", b"")
    dirs, files = await storage.listdir("")
    assert sorted(files) == ["a.txt", "b.txt"]
    assert dirs == []


@pytest.mark.asyncio
async def test_listdir_root_shows_subdirectory(storage):
    await storage.save("file.txt", b"")
    await storage.save("docs/report.txt", b"")
    dirs, files = await storage.listdir("")
    assert "docs" in dirs
    assert "file.txt" in files


@pytest.mark.asyncio
async def test_listdir_subdirectory_returns_relative_paths(storage):
    await storage.save("docs/a.txt", b"")
    await storage.save("docs/b.txt", b"")
    dirs, files = await storage.listdir("docs")
    assert sorted(files) == ["docs/a.txt", "docs/b.txt"]
    assert dirs == []


@pytest.mark.asyncio
async def test_listdir_subdirectory_shows_nested_dir(storage):
    await storage.save("docs/sub/deep.txt", b"")
    dirs, files = await storage.listdir("docs")
    assert any("sub" in d for d in dirs)
    assert files == []


@pytest.mark.asyncio
async def test_listdir_missing_directory_raises(storage):
    with pytest.raises(FileNotFoundError):
        await storage.listdir("nonexistent")


@pytest.mark.asyncio
async def test_listdir_on_file_raises(storage):
    await storage.save("file.txt", b"")
    with pytest.raises(NotADirectoryError):
        await storage.listdir("file.txt")


# ---------------------------------------------------------------------------
# path traversal security
# ---------------------------------------------------------------------------


def test_path_traversal_raises(storage):
    with pytest.raises(ValueError):
        storage._get_path("../outside.txt")


def test_path_traversal_nested_raises(storage):
    with pytest.raises(ValueError):
        storage._get_path("subdir/../../outside.txt")


def test_valid_nested_path_accepted(storage):
    path = storage._get_path("a/b/c.txt")
    assert path.name == "c.txt"


# ---------------------------------------------------------------------------
# timestamps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_modified_time_returns_datetime(storage):
    await storage.save("file.txt", b"data")
    assert isinstance(await storage.get_modified_time("file.txt"), datetime)


@pytest.mark.asyncio
async def test_get_created_time_returns_datetime(storage):
    await storage.save("file.txt", b"data")
    assert isinstance(await storage.get_created_time("file.txt"), datetime)


@pytest.mark.asyncio
async def test_get_accessed_time_returns_datetime(storage):
    await storage.save("file.txt", b"data")
    assert isinstance(await storage.get_accessed_time("file.txt"), datetime)


@pytest.mark.asyncio
async def test_modified_time_missing_raises(storage):
    with pytest.raises(FileNotFoundError):
        await storage.get_modified_time("missing.txt")


@pytest.mark.asyncio
async def test_created_time_missing_raises(storage):
    with pytest.raises(FileNotFoundError):
        await storage.get_created_time("missing.txt")


@pytest.mark.asyncio
async def test_accessed_time_missing_raises(storage):
    with pytest.raises(FileNotFoundError):
        await storage.get_accessed_time("missing.txt")


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copy_creates_destination(storage):
    await storage.save("src.txt", b"data")
    await storage.copy("src.txt", "dst.txt")
    assert await storage.read("dst.txt") == b"data"


@pytest.mark.asyncio
async def test_copy_leaves_source_intact(storage):
    await storage.save("src.txt", b"data")
    await storage.copy("src.txt", "dst.txt")
    assert await storage.exists("src.txt") is True


@pytest.mark.asyncio
async def test_copy_creates_parent_directories(storage):
    await storage.save("src.txt", b"data")
    await storage.copy("src.txt", "nested/dir/dst.txt")
    assert await storage.read("nested/dir/dst.txt") == b"data"


@pytest.mark.asyncio
async def test_copy_missing_source_raises(storage):
    with pytest.raises(FileNotFoundError):
        await storage.copy("missing.txt", "dst.txt")


# ---------------------------------------------------------------------------
# move
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_move_creates_destination(storage):
    await storage.save("src.txt", b"data")
    await storage.move("src.txt", "dst.txt")
    assert await storage.read("dst.txt") == b"data"


@pytest.mark.asyncio
async def test_move_removes_source(storage):
    await storage.save("src.txt", b"data")
    await storage.move("src.txt", "dst.txt")
    assert await storage.exists("src.txt") is False


@pytest.mark.asyncio
async def test_move_creates_parent_directories(storage):
    await storage.save("src.txt", b"data")
    await storage.move("src.txt", "nested/dst.txt")
    assert await storage.read("nested/dst.txt") == b"data"


@pytest.mark.asyncio
async def test_move_missing_source_raises(storage):
    with pytest.raises(FileNotFoundError):
        await storage.move("missing.txt", "dst.txt")


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_is_noop_files_remain(storage):
    await storage.save("file.txt", b"data")
    await storage.close()
    assert await storage.exists("file.txt") is True


@pytest.mark.asyncio
async def test_close_does_not_raise(storage):
    await storage.close()


# ---------------------------------------------------------------------------
# save from something other than bytes
# ---------------------------------------------------------------------------


class CountingFile:
    """A file that records how it was read, to show that a save streams rather
    than asking for everything at once."""

    def __init__(self, content: bytes):
        self._file = io.BytesIO(content)
        self.reads: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.reads.append(size)
        return self._file.read(size)

    def seek(self, offset: int) -> int:
        return self._file.seek(offset)

    def seekable(self) -> bool:
        return True


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


@pytest.mark.asyncio
async def test_save_from_an_upload_already_read(storage):
    upload = UploadFile(filename="notes.txt", file=io.BytesIO(b"uploaded bytes"), size=14)
    assert await upload.read() == b"uploaded bytes"

    # Left sitting at its end by the read above, and saved whole regardless.
    await storage.save("upload.txt", upload)
    assert await storage.read("upload.txt") == b"uploaded bytes"


@pytest.mark.asyncio
async def test_save_from_an_async_iterator(storage):
    await storage.save("streamed.txt", chunks_of(b"one ", b"two ", b"three"))
    assert await storage.read("streamed.txt") == b"one two three"


@pytest.mark.asyncio
async def test_save_from_another_backends_file(storage):
    await storage.save("source.txt", b"copied across")
    await storage.save("destination.txt", storage.open("source.txt"))
    assert await storage.read("destination.txt") == b"copied across"


@pytest.mark.asyncio
async def test_a_large_file_is_read_in_chunks_rather_than_whole(storage):
    content = b"x" * (CHUNK_SIZE * 3)
    source = CountingFile(content)

    await storage.save("large.bin", source)

    assert await storage.read("large.bin") == content
    # One read per chunk, plus the empty one that ends it — never a single
    # read of the whole file.
    assert source.reads == [CHUNK_SIZE] * 4
    assert -1 not in source.reads


@pytest.mark.asyncio
async def test_saving_something_unreadable_says_so(storage):
    with pytest.raises(TypeError, match="cannot read content from"):
        await storage.save("nope.txt", 12345)
