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
import zipfile

import pytest

from nitro.protocols import UploadFile
from nitro.utils.archives import zip_stream
from nitro.utils.content import CHUNK_SIZE

pytestmark = pytest.mark.asyncio


async def collect(stream) -> bytes:
    return b"".join([chunk async for chunk in stream])


def opened(blob: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(blob))


async def chunks_of(*chunks: bytes):
    for chunk in chunks:
        yield chunk


async def test_a_mapping_becomes_a_readable_archive():
    blob = await collect(zip_stream({"a.txt": b"first", "b/c.txt": b"second"}))

    with opened(blob) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == ["a.txt", "b/c.txt"]
        assert archive.read("a.txt") == b"first"
        assert archive.read("b/c.txt") == b"second"


async def test_pairs_are_accepted_as_well_as_a_mapping():
    blob = await collect(zip_stream([("one.txt", b"1"), ("two.txt", b"2")]))

    with opened(blob) as archive:
        assert archive.namelist() == ["one.txt", "two.txt"]


async def test_a_member_can_be_any_kind_of_content():
    upload = UploadFile(filename="up.txt", file=io.BytesIO(b"uploaded"), size=8)
    members = [
        ("bytes.txt", b"plain"),
        ("file.txt", io.BytesIO(b"out of a file")),
        ("upload.txt", upload),
        ("streamed.txt", chunks_of(b"one ", b"two")),
    ]

    blob = await collect(zip_stream(members))

    with opened(blob) as archive:
        assert archive.read("bytes.txt") == b"plain"
        assert archive.read("file.txt") == b"out of a file"
        assert archive.read("upload.txt") == b"uploaded"
        assert archive.read("streamed.txt") == b"one two"


async def test_an_empty_archive_is_still_a_valid_one():
    blob = await collect(zip_stream({}))

    with opened(blob) as archive:
        assert archive.namelist() == []


async def test_an_empty_member_is_kept():
    blob = await collect(zip_stream({"empty.txt": b""}))

    with opened(blob) as archive:
        assert archive.read("empty.txt") == b""


async def test_the_archive_is_yielded_in_pieces_rather_than_at_the_end():
    # Compressible content, so the pieces are only produced as members are
    # written rather than all falling out of the final close.
    members = {f"{index}.txt": bytes(range(256)) * 40 for index in range(4)}

    chunks = [chunk async for chunk in zip_stream(members)]

    assert len(chunks) > 1
    with opened(b"".join(chunks)) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) == 4


async def test_a_member_larger_than_a_chunk_survives_intact():
    content = bytes(range(256)) * (CHUNK_SIZE // 64)

    blob = await collect(zip_stream({"big.bin": content}))

    with opened(blob) as archive:
        assert archive.read("big.bin") == content


async def test_stored_rather_than_deflated_when_asked():
    blob = await collect(zip_stream({"a.txt": b"x" * 500}, compression=zipfile.ZIP_STORED))

    with opened(blob) as archive:
        assert archive.getinfo("a.txt").compress_type == zipfile.ZIP_STORED
        assert archive.read("a.txt") == b"x" * 500


async def test_deflated_content_is_actually_smaller():
    content = b"y" * 10_000

    deflated = await collect(zip_stream({"a.txt": content}))
    stored = await collect(zip_stream({"a.txt": content}, compression=zipfile.ZIP_STORED))

    assert len(deflated) < len(stored)


async def test_members_are_not_all_dated_1980():
    blob = await collect(zip_stream({"a.txt": b"x"}))

    with opened(blob) as archive:
        assert archive.getinfo("a.txt").date_time[0] > 1980


async def test_zip64_can_be_forced_for_a_member_of_unknown_size():
    blob = await collect(zip_stream({"a.txt": chunks_of(b"x")}, force_zip64=True))

    with opened(blob) as archive:
        assert archive.read("a.txt") == b"x"


async def test_a_member_that_cannot_be_read_says_so():
    with pytest.raises(TypeError, match="cannot read content from"):
        await collect(zip_stream({"a.txt": 12345}))
