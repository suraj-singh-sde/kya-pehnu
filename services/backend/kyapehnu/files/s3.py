"""The S3 store: pictures as objects under the same keys as the disk layout. The only module that imports boto3."""

import logging
import mimetypes
import shutil
import time
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from http import HTTPStatus
from tempfile import SpooledTemporaryFile
from typing import IO, TYPE_CHECKING, Any, Final

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from botocore.response import StreamingBody
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from kyapehnu.files.errors import MissingFileError, StoreUnavailableError
from kyapehnu.files.keys import KEY_PATTERN, check
from kyapehnu.files.store import HASH_CHUNK_BYTES, StoredFile, read_chunks, sha256_b64

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S: Final = 2
READ_TIMEOUT_S: Final = 5
MAX_ATTEMPTS: Final = 2  # attempts in total, the first one included
MAX_POOL_CONNECTIONS: Final = 40  # the anyio threadpool that runs the sync routes; fewer and urllib3 logs the host
STREAM_CHUNK_BYTES: Final = 64 * 1024
SPOOL_MAX_BYTES: Final = 1024 * 1024  # in memory up to this, then a temporary file
PING_CACHE_SECONDS: Final = 60
NOT_FOUND_CODES: Final = frozenset({"NoSuchKey", "404", "NotFound"})
NOT_MODIFIED_CODE: Final = "304"
INVALID_RANGE_CODE: Final = "InvalidRange"
OCTET_STREAM: Final = "application/octet-stream"


def _error_code(e: ClientError) -> str:
    return str(e.response.get("Error", {}).get("Code", ""))


def _unavailable(op: str, reason: str) -> StoreUnavailableError:
    """Class or code only: S3's message names the bucket and the key, and neither may reach a log (NFR-6)."""
    log.error("event=files_store_unavailable op=%s error=%s", op, reason)
    return StoreUnavailableError()


@contextmanager
def _translating(op: str) -> Iterator[None]:
    """S3's failures as the store's: not found is MissingFileError, everything else StoreUnavailableError."""
    try:
        yield
    except ClientError as e:
        code = _error_code(e)
        if code in NOT_FOUND_CODES:
            raise MissingFileError from e
        raise _unavailable(op, code) from e
    except BotoCoreError as e:
        raise _unavailable(op, type(e).__name__) from e


@contextmanager
def _seekable(body: IO[bytes]) -> Iterator[IO[bytes]]:
    """The body itself when it can seek, else a copy that can (a zip member is read once, S3 needs the hash first)."""
    seekable = getattr(body, "seekable", None)
    if callable(seekable) and seekable():
        yield body
        return
    with SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES) as spool:
        shutil.copyfileobj(body, spool, STREAM_CHUNK_BYTES)
        spool.seek(0)
        yield spool


def _http_date(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):  # a client that sends garbage gets the picture
        return None


def _conditions(request: Request, *, ranged: bool) -> dict[str, Any]:
    """The request's conditional headers as S3 arguments: S3 decides 200, 206, 304 or 416."""
    conditions: dict[str, Any] = {}
    if ranged and (byte_range := request.headers.get("range")):
        conditions["Range"] = byte_range
    if etags := request.headers.get("if-none-match"):
        conditions["IfNoneMatch"] = etags
    # RFC 9110 13.1.3: If-Modified-Since is ignored when If-None-Match is present.
    elif (since := _http_date(request.headers.get("if-modified-since"))) is not None:
        conditions["IfModifiedSince"] = since
    return conditions


def _headers(found: Mapping[str, Any]) -> dict[str, str]:
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(found["ContentLength"])}
    if etag := found.get("ETag"):
        headers["ETag"] = etag
    if modified := found.get("LastModified"):
        headers["Last-Modified"] = format_datetime(modified.astimezone(UTC), usegmt=True)
    return headers


def _stream(body: StreamingBody) -> Iterator[bytes]:
    try:
        yield from body.iter_chunks(STREAM_CHUNK_BYTES)
    except BotoCoreError as e:  # the headers are out: all that is left is to cut the connection
        log.error("event=files_store_unavailable op=stream error=%s", type(e).__name__)
        raise StoreUnavailableError from None  # the chained urllib3 text names the bucket (NFR-6)
    finally:
        body.close()


def _not_modified(e: ClientError) -> Response:
    sent = e.response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
    return Response(
        status_code=HTTPStatus.NOT_MODIFIED,
        headers={n: sent[n.lower()] for n in ("ETag", "Last-Modified") if n.lower() in sent},
    )


def _get_response(found: Mapping[str, Any]) -> Response:
    headers = _headers(found)
    content_range = found.get("ContentRange")
    if content_range:
        headers["Content-Range"] = content_range
    status = HTTPStatus.PARTIAL_CONTENT if content_range else HTTPStatus.OK
    return StreamingResponse(
        _stream(found["Body"]), status_code=status, headers=headers, media_type=found.get("ContentType")
    )


class S3Store:
    def __init__(self, bucket: str, region: str) -> None:
        self._bucket = bucket
        self._client: S3Client = boto3.session.Session().client(
            "s3",
            region_name=region,
            config=Config(
                connect_timeout=CONNECT_TIMEOUT_S,
                read_timeout=READ_TIMEOUT_S,
                retries={"mode": "standard", "max_attempts": MAX_ATTEMPTS},
                max_pool_connections=MAX_POOL_CONNECTIONS,
            ),
        )
        self._pinged_at: float | None = None

    def put(self, key: str, body: bytes | IO[bytes]) -> None:
        check(key)
        if isinstance(body, bytes):
            self._put_object(key, body, sha256_b64([body]))
            return
        with _seekable(body) as stream:
            start = stream.tell()
            digest = sha256_b64(read_chunks(stream))
            stream.seek(start)
            self._put_object(key, stream, digest)

    def _put_object(self, key: str, body: bytes | IO[bytes], digest: str) -> None:
        with _translating("put"):
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ContentType=mimetypes.guess_type(key)[0] or OCTET_STREAM,
                ChecksumAlgorithm="SHA256",
                ChecksumSHA256=digest,
            )

    def open(self, key: str) -> IO[bytes]:
        check(key)
        with _translating("open"), closing(self._client.get_object(Bucket=self._bucket, Key=key)["Body"]) as body:
            spool: IO[bytes] = SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)
            try:
                for chunk in body.iter_chunks(STREAM_CHUNK_BYTES):
                    spool.write(chunk)
                spool.seek(0)
            except BaseException:
                spool.close()
                raise
            return spool

    def read_bytes(self, key: str) -> bytes:
        with self.open(key) as f:
            return f.read()

    def exists(self, key: str) -> bool:
        check(key)
        try:
            with _translating("exists"):
                self._client.head_object(Bucket=self._bucket, Key=key)
        except MissingFileError:
            return False
        return True

    def delete(self, key: str) -> None:
        check(key)
        with _translating("delete"):
            self._client.delete_object(Bucket=self._bucket, Key=key)

    def list(self, prefix: str) -> Iterator[StoredFile]:
        pages = iter(self._client.get_paginator("list_objects_v2").paginate(Bucket=self._bucket, Prefix=prefix))
        while True:
            with _translating("list"):
                page = next(pages, None)
            if page is None:
                return
            for found in page.get("Contents", []):
                if KEY_PATTERN.fullmatch(found["Key"]):
                    yield StoredFile(found["Key"], found["LastModified"].timestamp())

    def checksum(self, key: str) -> str:
        check(key)
        with _translating("checksum"):
            head = self._client.head_object(Bucket=self._bucket, Key=key, ChecksumMode="ENABLED")
            stored = head.get("ChecksumSHA256")
            if stored and "-" not in stored:  # a "-<parts>" value covers a multipart upload's parts, not the bytes
                return stored
            with closing(self._client.get_object(Bucket=self._bucket, Key=key)["Body"]) as body:
                return sha256_b64(body.iter_chunks(HASH_CHUNK_BYTES))

    def serve(self, key: str, request: Request) -> Response:
        check(key)
        head = request.method == "HEAD"
        conditions = _conditions(request, ranged=not head)
        with _translating("serve"):
            try:
                if head:
                    found = self._client.head_object(Bucket=self._bucket, Key=key, **conditions)
                    return Response(
                        status_code=HTTPStatus.OK, headers=_headers(found), media_type=found.get("ContentType")
                    )
                return _get_response(self._client.get_object(Bucket=self._bucket, Key=key, **conditions))
            except ClientError as e:
                code = _error_code(e)
                if code == NOT_MODIFIED_CODE:
                    return _not_modified(e)
                if code == INVALID_RANGE_CODE:
                    return Response(status_code=HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                raise

    def ping(self) -> None:
        if self._pinged_at is not None and time.monotonic() - self._pinged_at < PING_CACHE_SECONDS:
            return
        try:
            with _translating("ping"):
                self._client.head_bucket(Bucket=self._bucket)
        except MissingFileError as e:  # head_bucket answers a bare 404 for a bucket that is gone
            raise _unavailable("ping", "NoSuchBucket") from e
        self._pinged_at = time.monotonic()
