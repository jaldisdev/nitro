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

"""What the S3 backend hands to its client.

aiobotocore is an optional dependency and is not installed for the test run, so
the session is faked. That is enough for what these cover: which of the shapes
`save()` accepts reaches `put_object` as a file to be read, and which has to be
collected into bytes first. Whether aiobotocore then uploads it correctly is
aiobotocore's business, and is not tested here.
"""

import io

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
    def __init__(self) -> None:
        self.client_object = FakeClient()
        self.client_calls: list[dict] = []

    def create_client(self, service_name: str, **kwargs) -> FakeClient:
        self.client_calls.append({"service_name": service_name, **kwargs})
        return self.client_object


@pytest.fixture
def storage(monkeypatch):
    monkeypatch.setattr(s3_backend, "get_session", FakeSession)
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
    monkeypatch.setattr(s3_backend, "get_session", FakeSession)
    storage = S3Storage("bucket", {"OPTIONS": {"default_acl": "private"}})

    await storage.save("key", b"content")

    assert storage.session.client_object.put_calls[-1]["ACL"] == "private"


@pytest.mark.asyncio
async def test_credentials_and_endpoint_reach_the_client(monkeypatch):
    monkeypatch.setattr(s3_backend, "get_session", FakeSession)
    storage = S3Storage(
        "bucket",
        {
            "OPTIONS": {
                "region_name": "eu-central-1",
                "aws_access_key_id": "AKID",
                "aws_secret_access_key": "SECRET",
                "endpoint_url": "http://storage:9000",
            }
        },
    )

    await storage.save("key", b"content")

    assert storage.session.client_calls[-1] == {
        "service_name": "s3",
        "region_name": "eu-central-1",
        "aws_access_key_id": "AKID",
        "aws_secret_access_key": "SECRET",
        "endpoint_url": "http://storage:9000",
    }


@pytest.mark.asyncio
async def test_closing_holds_nothing_to_release(storage):
    await storage.close()
