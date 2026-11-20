"""AC-27 at the files layer (LLD 6, 9): a 25 MB picture costs a bounded amount of extra memory to write and to read
through the S3 store. The boto3 client is replaced by a fake that streams, so the numbers are the store's own."""

import asyncio
import tracemalloc
from datetime import UTC, datetime

import pytest
from botocore.response import StreamingBody
from starlette.requests import Request

from kyapehnu.files.s3 import S3Store
from tests.conftest import TEST_BUCKET, TEST_REGION

pytestmark = pytest.mark.slow

MIB = 1024 * 1024
SIZE = 25 * MIB
WRITE_PEAK_LIMIT = 32 * MIB
READ_PEAK_LIMIT = 2 * MIB
KEY = "images/1/original/big.jpg"
CHUNK = 64 * 1024


@pytest.fixture
def big_file(tmp_path):
    path = tmp_path / "big.bin"
    block = bytes(range(256)) * 4096  # 1 MiB
    with path.open("wb") as f:
        for _ in range(SIZE // MIB):
            f.write(block)
    return path


@pytest.fixture
def store():
    return S3Store(TEST_BUCKET, TEST_REGION)


def _peak(fn):
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_writing_a_25_mb_picture_stays_under_the_write_bound(store, big_file, monkeypatch, request):
    seen = {"bytes": 0, "checksum": None}

    def put_object(**kwargs):  # the real client streams the body: so does this one
        body = kwargs["Body"]
        seen["checksum"] = kwargs.get("ChecksumSHA256")
        if isinstance(body, bytes):
            seen["bytes"] = len(body)
            return {}
        while chunk := body.read(CHUNK):
            seen["bytes"] += len(chunk)
        return {}

    monkeypatch.setattr(store._client, "put_object", put_object)
    f = big_file.open("rb")
    request.addfinalizer(f.close)

    peak = _peak(lambda: store.put(KEY, f))

    assert seen["bytes"] == SIZE
    assert seen["checksum"]
    assert peak <= WRITE_PEAK_LIMIT


def test_writing_a_25_mb_stream_that_cannot_seek_stays_under_the_write_bound(store, big_file, monkeypatch, request):
    seen = {"bytes": 0}

    def put_object(**kwargs):
        body = kwargs["Body"]
        while chunk := body.read(CHUNK):
            seen["bytes"] += len(chunk)
        return {}

    class FromDisk:
        def __init__(self, handle):
            self._handle = handle

        def read(self, size=-1):
            return self._handle.read(size)

    monkeypatch.setattr(store._client, "put_object", put_object)
    handle = big_file.open("rb")
    request.addfinalizer(handle.close)

    peak = _peak(lambda: store.put(KEY, FromDisk(handle)))

    assert seen["bytes"] == SIZE
    assert peak <= WRITE_PEAK_LIMIT


def test_reading_a_25_mb_picture_through_serve_stays_under_the_read_bound(store, big_file, monkeypatch, request):
    raw = big_file.open("rb")
    request.addfinalizer(raw.close)

    def get_object(**_kwargs):
        return {
            "Body": StreamingBody(raw, SIZE),
            "ContentLength": SIZE,
            "ContentType": "image/jpeg",
            "ETag": '"abc"',
            "LastModified": datetime(2026, 10, 1, tzinfo=UTC),
        }

    monkeypatch.setattr(store._client, "get_object", get_object)
    request_ = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
    received = {"bytes": 0}

    async def drain():
        response = store.serve(KEY, request_)
        async for chunk in response.body_iterator:
            received["bytes"] += len(chunk)

    peak = _peak(lambda: asyncio.run(drain()))

    assert received["bytes"] == SIZE
    assert peak <= READ_PEAK_LIMIT


def test_the_body_is_closed_when_the_stream_has_been_read(store, big_file, monkeypatch):
    closed = []

    class Tracked(StreamingBody):
        def close(self):
            closed.append(True)
            super().close()

    raw = big_file.open("rb")
    monkeypatch.setattr(
        store._client,
        "get_object",
        lambda **_k: {"Body": Tracked(raw, SIZE), "ContentLength": SIZE, "ContentType": "image/jpeg", "ETag": '"abc"'},
    )
    request_ = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})

    async def drain():
        async for _ in store.serve(KEY, request_).body_iterator:
            pass

    asyncio.run(drain())

    assert closed
