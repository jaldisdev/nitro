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

"""Archives built as they are sent.

A ZIP assembled here never exists whole: `zipfile` writes into a sink that
hands each piece straight on, so the archive is streamed to whoever asked for
it while it is still being built.

    return StreamingResponse(zip_stream(members), content_type="application/zip")
    await storage.save("archive.zip", zip_stream(members))
"""

import asyncio
import time
import zipfile
from collections.abc import AsyncIterator, Iterable, Mapping

from nitro.utils.content import CHUNK_SIZE, Content, iter_content

__all__ = ["zip_stream"]


class _Sink:
    """What `zipfile` writes into.

    Holds only what has not been handed on yet. `tell` is how `zipfile` records
    where each member starts, so it has to keep counting past what has already
    been taken away — the count is the position in the archive, not the length
    of the buffer.
    """

    __slots__ = ("_buffer", "_offset")

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._offset = 0

    def write(self, data: bytes) -> int:
        self._buffer += data
        self._offset += len(data)
        return len(data)

    def tell(self) -> int:
        return self._offset

    def flush(self) -> None:
        return None

    def take(self) -> bytes:
        chunk = bytes(self._buffer)
        del self._buffer[:]
        return chunk


async def zip_stream(
    members: Mapping[str, Content] | Iterable[tuple[str, Content]],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
    compresslevel: int | None = None,
    force_zip64: bool = False,
    chunk_size: int = CHUNK_SIZE,
) -> AsyncIterator[bytes]:
    """A ZIP of `members`, a piece at a time.

    Each member is a name and its content — bytes, something with a `read()`,
    or an async iterator of chunks, the same as anything else that takes
    content. A member is read a chunk at a time and compressed a chunk at a
    time, so neither the archive nor any one file in it is ever held whole.

    Compression is handed to a thread. It is the one genuinely expensive thing
    here, and doing it inline would stall the loop that is serving every other
    connection for as long as it took.

    `force_zip64` makes room for a member past 4 GiB. It is off by default
    because the size of a streamed member is not known until it has been read,
    and reserving the space costs a few bytes per member for archives that will
    never need it.
    """
    pairs = members.items() if isinstance(members, Mapping) else members

    sink = _Sink()
    archive = zipfile.ZipFile(sink, "w", compression=compression, compresslevel=compresslevel)

    try:
        for name, content in pairs:
            # As `writestr` does when handed a name rather than a ZipInfo:
            # without this every member is dated 1980.
            info = zipfile.ZipInfo(name, date_time=time.localtime(time.time())[:6])
            info.compress_type = compression

            with archive.open(info, "w", force_zip64=force_zip64) as member:
                async for piece in iter_content(content, chunk_size):
                    await asyncio.to_thread(member.write, piece)
                    if chunk := sink.take():
                        yield chunk

            # Closing the member writes its data descriptor.
            if chunk := sink.take():
                yield chunk
    finally:
        # The central directory, which is what makes the bytes a readable
        # archive rather than a run of compressed files.
        await asyncio.to_thread(archive.close)

    if chunk := sink.take():
        yield chunk
