"""Content that can be read a chunk at a time.

What a caller hands to something that will move bytes for it — storage backends
write it, archive builders read it. Bytes, anything with a ``read()``, or an
async iterator of chunks, so a large upload can reach its destination without
the whole of it passing through memory on the way.
"""

import asyncio
import inspect
from collections.abc import AsyncIterator
from typing import IO, Any, Protocol, runtime_checkable

#: How much is moved at a time when content is streamed rather than held whole.
CHUNK_SIZE = 64 * 1024


@runtime_checkable
class ReadableContent(Protocol):
    """Anything these helpers can read from: an open file, an
    :class:`~nitro.protocols.http.UploadFile`, a storage backend's own file. The
    ``read`` may be synchronous or awaitable — both are in use, and a caller
    should not have to know which it holds."""

    def read(self, size: int = ...) -> Any: ...


#: Bytes, something to read from, or an
#: async iterator of chunks. Everything but bytes can be moved a chunk at a
#: time, which is the point — an upload spooled to disk should reach its
#: destination without the whole of it passing through memory on the way.
Content = bytes | bytearray | memoryview | ReadableContent | AsyncIterator[bytes]


async def _call(function: Any, *arguments: Any) -> Any:
    """Calls `function`, awaiting it if it is a coroutine and handing it to a
    thread if it is not.

    A synchronous `read` here is a real file — a spooled upload, something
    opened off disk — so calling it inline would block the loop that is serving
    every other connection.

    Called exactly once either way: a read that ran twice would hand back the
    second chunk and lose the first.
    """
    if inspect.iscoroutinefunction(function):
        return await function(*arguments)

    result = await asyncio.to_thread(function, *arguments)
    # A plain method may still hand back something to await — aiofiles and the
    # cloud clients both have shapes like that.
    if inspect.isawaitable(result):
        return await result
    return result


async def _rewind(content: Any) -> None:
    """Positions `content` at its start when it can be, so that content already
    read once still saves whole. It is needed
    for this reason: an `UploadFile` a handler has inspected is left sitting
    at its end."""
    seek = getattr(content, "seek", None)
    if seek is None or not getattr(content, "seekable", lambda: True)():
        return
    result = seek(0)
    if inspect.isawaitable(result):
        await result


async def iter_content(content: SaveContent, chunk_size: int = CHUNK_SIZE) -> AsyncIterator[bytes]:
    """Whatever was handed over, as chunks.

    For a consumer that writes as it goes. The chunks are whatever the source
    gives back, so nothing here holds more than one at a time.
    """
    if isinstance(content, (bytes, bytearray, memoryview)):
        yield bytes(content)
        return

    read = getattr(content, "read", None)
    if read is not None:
        await _rewind(content)
        while True:
            chunk = await _call(read, chunk_size)
            if not chunk:
                return
            yield bytes(chunk)

    if hasattr(content, "__aiter__"):
        async for chunk in content:
            yield bytes(chunk)
        return

    raise TypeError(
        f"cannot read content from {type(content).__name__}: expected bytes, something "
        "with a read(), or an async iterator of chunks"
    )


async def read_content(content: SaveContent) -> bytes:
    """Whatever was handed over, whole.

    For a consumer with nowhere to stream to — one that keeps what it is given
    in memory, or a client that must be told the length up front.
    """
    if isinstance(content, (bytes, bytearray, memoryview)):
        return bytes(content)
    return b"".join([chunk async for chunk in iter_content(content)])


def file_object_for(content: SaveContent) -> IO[bytes] | None:
    """The seekable file behind `content`, or `None` when there is not one.

    For a consumer whose own client takes a file object and reads it itself. An
    upload past ``MAX_UPLOAD_MEMORY`` is already a file on disk, and handing
    that file over lets the client stream from it rather than being given the
    bytes to hold.

    The file comes back positioned at its start, for the same reason
    :func:`iter_content` rewinds what it is given.
    """
    file = getattr(content, "file", content)
    read = getattr(file, "read", None)
    if read is None or inspect.iscoroutinefunction(read):
        return None
    if not hasattr(file, "seek") or not getattr(file, "seekable", lambda: True)():
        return None
    file.seek(0)
    return file
