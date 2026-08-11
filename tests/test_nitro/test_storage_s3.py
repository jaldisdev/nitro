"""What the S3 backend hands to its client.

aioboto3 is an optional dependency and is not installed for the test run, so
the session is faked. That is enough for what these cover: which of the shapes
`save()` accepts reaches `put_object` as a file to be read, and which has to be
collected into bytes first. Whether aioboto3 then uploads it correctly is
aioboto3's business, and is not tested here.
"""

import io
from types import SimpleNamespace

import pytest

from nitro.protocols import UploadFile
from nitro.storage.backends import s3 as s3_backend
from nitro.storage.backends.s3 import S3Storage


class FakeClient:
    def __init__(self) -> None:
        self.put_calls: list[dict] = []

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *arguments) -> None:
        return None

    async def put_object(self, **kwargs) -> None:
        self.put_calls.append(kwargs)


class FakeSession:
    def __init__(self, **kwargs) -> None:
        self.client_object = FakeClient()

    def client(self, service_name: str, **kwargs) -> FakeClient:
        return self.client_object

    async def close(self) -> None:
        return None


@pytest.fixture
def storage(monkeypatch):
    monkeypatch.setattr(s3_backend, "aioboto3", SimpleNamespace(Session=FakeSession))
    return S3Storage("bucket", {"OPTIONS": {"default_acl": None}})


def last_body(storage):
    return storage.session.client_object.put_calls[-1]["Body"]


async def chunks_of(*chunks: bytes):
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_bytes_are_sent_as_they_are(storage):
    await storage.save("key", b"plain bytes")
    assert last_body(storage) == b"plain bytes"


@pytest.mark.asyncio
async def test_an_upload_is_sent_as_its_own_file(storage):
    upload = UploadFile(filename="big.bin", file=io.BytesIO(b"spooled"), size=7)

    await storage.save("key", upload)

    # The file itself, not its bytes: the client reads it, so nothing here has
    # to hold the upload whole.
    assert last_body(storage) is upload.file
    assert last_body(storage).read() == b"spooled"


@pytest.mark.asyncio
async def test_an_upload_already_read_is_sent_from_its_start(storage):
    upload = UploadFile(filename="big.bin", file=io.BytesIO(b"spooled"), size=7)
    assert await upload.read() == b"spooled"

    await storage.save("key", upload)

    assert last_body(storage).read() == b"spooled"


@pytest.mark.asyncio
async def test_a_chunk_iterator_is_collected_first(storage):
    # Nothing to read from and no length to declare, so it has to be gathered.
    await storage.save("key", chunks_of(b"one ", b"two"))
    assert last_body(storage) == b"one two"


@pytest.mark.asyncio
async def test_the_acl_is_sent_when_one_is_configured(monkeypatch):
    monkeypatch.setattr(s3_backend, "aioboto3", SimpleNamespace(Session=FakeSession))
    storage = S3Storage("bucket", {"OPTIONS": {"default_acl": "private"}})

    await storage.save("key", b"content")

    assert storage.session.client_object.put_calls[-1]["ACL"] == "private"
