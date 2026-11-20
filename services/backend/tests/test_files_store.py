"""The files domain below HTTP (LLD 3.1, 3.2, 3.8, 4): keys, the FileStore contract on both stores, the S3 specifics,
error translation, `discard`, `build_store` and `Ctx.files`. AC-11, AC-12 and the store side of AC-14, 15, 24, 27."""

import io
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from botocore.exceptions import ConnectTimeoutError, EndpointConnectionError, NoCredentialsError, ReadTimeoutError

from kyapehnu.core.config import Prefs
from kyapehnu.core.context import Ctx
from kyapehnu.files import keys, public
from kyapehnu.files.errors import InvalidKeyError, MissingFileError, StoreUnavailableError
from kyapehnu.files.local import LocalStore
from kyapehnu.files.s3 import S3Store
from tests.conftest import TEST_BUCKET, TEST_REGION
from tests.files_helpers import Exploding, OneWay, picture, sha256_b64

KEY = "images/1/thumb/a.webp"
UNAVAILABLE = "Pictures are unavailable right now. Retry shortly."
MIB = 1024 * 1024


# --- keys ------------------------------------------------------------------------------------------------------------


def test_the_kinds_are_the_five_picture_kinds():
    assert keys.KINDS == ("original", "cutout", "thumb", "collage", "mannequin")
    assert keys.ROOT == "images/"


def test_key_builds_the_address_of_one_picture():
    assert keys.key(7, "thumb", "x.webp") == "images/7/thumb/x.webp"


@pytest.mark.parametrize(
    ("user_id", "kind", "expected"),
    [(None, None, "images/"), (7, None, "images/7/"), (7, "thumb", "images/7/thumb/")],
)
def test_prefix_narrows_from_the_root_to_one_users_kind(user_id, kind, expected):
    assert keys.prefix(user_id, kind) == expected


@pytest.mark.parametrize(
    ("user_id", "kind", "name"),
    [
        (0, "thumb", "a.webp"),
        (-1, "thumb", "a.webp"),
        (1, "secrets", "a.webp"),
        (1, "Thumb", "a.webp"),
        (1, "thumb", "../a.webp"),
        (1, "thumb", "a/b.webp"),
        (1, "thumb", "a.gif"),
        (1, "thumb", ".webp"),
        (1, "thumb", "x" * 201 + ".webp"),
    ],
)
def test_key_refuses_what_is_not_a_picture_address(user_id, kind, name):
    with pytest.raises(InvalidKeyError):
        keys.key(user_id, kind, name)


def test_key_accepts_the_longest_stem_and_every_extension():
    for ext in ("jpg", "jpeg", "png", "webp", "heic", "heif"):
        assert keys.key(1, "original", f"{'x' * 200}.{ext}").endswith(f".{ext}")


@pytest.mark.parametrize(
    ("address", "parsed"),
    [
        ("images/7/thumb/x.webp", (7, "thumb", "x.webp")),
        ("images/123456/mannequin/1-2.png", (123456, "mannequin", "1-2.png")),
        ("images/0/thumb/x.webp", None),
        ("images/07/thumb/x.webp", None),
        ("images/7/thumb/", None),
        ("images/7/thumb/x.webp/extra", None),
        ("images/7/thumb/.x.webp.abc.tmp", None),
        ("/images/7/thumb/x.webp", None),
        ("other/7/thumb/x.webp", None),
        ("", None),
    ],
)
def test_parse_key_returns_the_parts_or_none(address, parsed):
    assert keys.parse_key(address) == parsed


def test_check_returns_a_good_key_and_refuses_a_bad_one():
    assert keys.check(KEY) == KEY
    with pytest.raises(InvalidKeyError):
        keys.check("images/../../etc/passwd")


def test_the_route_patterns_match_a_kind_and_a_name_whole():
    assert re.search(keys.KIND_PATTERN, "thumb")
    assert not re.search(keys.KIND_PATTERN, "thumbs")
    assert re.search(keys.NAME_PATTERN, "a-b_9.png")
    assert not re.search(keys.NAME_PATTERN, "a.png.exe")
    assert not re.search(keys.NAME_PATTERN, "a b.png")


# --- errors (LLD 4.1) ------------------------------------------------------------------------------------------------


def test_a_missing_file_is_the_not_found_the_route_always_gave():
    e = MissingFileError()

    assert (e.status, e.code, str(e)) == (404, "not_found", "Image not found")


def test_an_unavailable_store_is_a_retryable_503_with_its_own_code():
    e = StoreUnavailableError()

    assert (e.status, e.code, str(e)) == (503, "files_unavailable", UNAVAILABLE)
    assert e.headers() == {"Retry-After": "1"}


def test_an_invalid_key_is_a_value_error():
    assert issubclass(InvalidKeyError, ValueError)


# --- the contract, on both stores ------------------------------------------------------------------------------------


def test_a_put_picture_reads_back_through_every_method(store):
    data = picture(".webp")

    store.put(KEY, data)

    assert store.exists(KEY)
    assert store.read_bytes(KEY) == data
    assert store.checksum(KEY) == sha256_b64(data)
    with store.open(KEY) as f:
        assert f.read() == data


def test_open_gives_a_seekable_file(store):
    data = bytes(range(256)) * 4
    store.put(KEY, data)

    with store.open(KEY) as f:
        f.seek(10)
        part = f.read(5)
        f.seek(0)
        whole = f.read()

    assert (part, whole) == (data[10:15], data)


def test_put_replaces_what_was_there(store):
    store.put(KEY, b"first")

    store.put(KEY, b"second")

    assert store.read_bytes(KEY) == b"second"
    assert [f.key for f in store.list(keys.ROOT)] == [KEY]


@pytest.mark.parametrize("size", [0, 10, 3 * MIB], ids=["empty", "small", "three-mib"])
@pytest.mark.parametrize("kind", ["bytes", "bytesio", "file", "one-way"])
def test_put_takes_bytes_seekable_files_and_streams_that_cannot_seek(store, tmp_path, request, kind, size):
    data = (b"0123456789abcdef" * (size // 16 + 1))[:size]
    if kind == "bytes":
        body = data
    elif kind == "bytesio":
        body = io.BytesIO(data)
    elif kind == "one-way":
        body = OneWay(data)
    else:
        (tmp_path / "body.bin").write_bytes(data)
        body = (tmp_path / "body.bin").open("rb")
        request.addfinalizer(body.close)

    store.put(KEY, body)

    assert store.read_bytes(KEY) == data
    assert store.checksum(KEY) == sha256_b64(data)


def test_a_stream_that_fails_half_way_stores_nothing(store):
    with pytest.raises(OSError, match="connection reset"):
        store.put(KEY, Exploding())

    assert not store.exists(KEY)
    assert list(store.list(keys.ROOT)) == []


def test_a_failed_overwrite_keeps_the_old_picture(store):
    store.put(KEY, b"the original")

    with pytest.raises(OSError, match="connection reset"):
        store.put(KEY, Exploding())

    assert store.read_bytes(KEY) == b"the original"


@pytest.mark.parametrize("read", ["open", "read_bytes", "checksum"])
def test_reading_a_missing_key_is_a_missing_file_error(store, read):
    reader = {"open": lambda: store.open(KEY).__enter__(), "read_bytes": lambda: store.read_bytes(KEY)}
    reader["checksum"] = lambda: store.checksum(KEY)

    with pytest.raises(MissingFileError):
        reader[read]()


def test_exists_is_false_for_a_missing_key(store):
    assert store.exists(KEY) is False


def test_delete_removes_that_key_only_and_a_missing_key_is_not_an_error(store):
    other = "images/1/thumb/b.webp"
    store.put(KEY, b"a")
    store.put(other, b"b")

    store.delete(KEY)
    store.delete(KEY)
    store.delete("images/1/thumb/never-there.webp")

    assert not store.exists(KEY)
    assert store.read_bytes(other) == b"b"


def test_list_shows_each_key_with_its_modified_time(store):
    store.put(KEY, b"a")

    (stored,) = list(store.list(keys.ROOT))

    assert stored.key == KEY
    assert abs(stored.modified - time.time()) < 120


def test_list_is_scoped_by_the_prefix_and_user_1_does_not_match_user_10(store):
    wanted = {"images/1/thumb/a.webp", "images/1/cutout/a.png"}
    unwanted = {"images/10/thumb/a.webp", "images/2/thumb/a.webp"}
    for key in wanted | unwanted:
        store.put(key, b"x")

    def listed(prefix):
        return {f.key for f in store.list(prefix)}

    assert listed(keys.ROOT) == wanted | unwanted
    assert listed(keys.prefix(1)) == wanted
    assert listed(keys.prefix(1, "thumb")) == {"images/1/thumb/a.webp"}
    assert listed(keys.prefix(3)) == set()
    assert listed(keys.prefix(1, "collage")) == set()


def test_list_of_an_empty_store_is_empty(store):
    assert list(store.list(keys.ROOT)) == []


def test_list_shows_only_names_that_are_ours(store, plant):
    plant("images/1/thumb/notes.txt")
    plant("images/1/thumb/.a.webp.0123abcd.tmp")
    plant("images/1/bogus/a.png")
    plant("images/0/thumb/a.png")
    plant("images/01/thumb/a.png")
    plant("images/abc/thumb/a.png")
    plant("images/1/thumb/a.gif")
    plant("readme.txt")
    plant(KEY)

    assert [f.key for f in store.list(keys.ROOT)] == [KEY]


BAD_KEYS = [
    "../etc/passwd",
    "images/../secrets.json",
    "images/1/thumb/../../2/thumb/a.webp",
    "images/1/secrets/a.webp",
    "images/1/Thumb/a.webp",
    "images/0/thumb/a.webp",
    "images/01/thumb/a.webp",
    "images/1/thumb/a.gif",
    "images/1/thumb//a.webp",
    "/images/1/thumb/a.webp",
    "",
]
CALLS = {
    "put": lambda s, k: s.put(k, b"x"),
    "open": lambda s, k: s.open(k).__enter__(),
    "read_bytes": lambda s, k: s.read_bytes(k),
    "exists": lambda s, k: s.exists(k),
    "delete": lambda s, k: s.delete(k),
    "checksum": lambda s, k: s.checksum(k),
}


@pytest.mark.parametrize("call", sorted(CALLS))
@pytest.mark.parametrize("bad", BAD_KEYS)
def test_every_method_refuses_a_key_that_is_not_a_picture_address(store, call, bad):
    with pytest.raises(InvalidKeyError):
        CALLS[call](store, bad)


def test_a_refused_put_writes_nothing_anywhere(store, local_dir):
    with pytest.raises(InvalidKeyError):
        store.put("images/1/thumb/../../escape.webp", b"x")

    assert list(store.list(keys.ROOT)) == []
    assert not (local_dir / "escape.webp").exists() and not (local_dir / "images" / "escape.webp").exists()


# --- LocalStore ------------------------------------------------------------------------------------------------------


@pytest.fixture
def local(local_dir):
    return LocalStore(local_dir)


def test_a_local_store_makes_its_images_folder(local, local_dir):
    assert (local_dir / "images").is_dir()


def test_a_local_put_lands_at_the_key_path_and_leaves_no_temp_file(local, local_dir):
    local.put(KEY, b"data")

    assert [p.name for p in (local_dir / "images" / "1" / "thumb").iterdir()] == ["a.webp"]
    assert (local_dir / KEY).read_bytes() == b"data"


def test_a_temp_file_of_a_write_that_died_is_never_listed(local, local_dir):
    local.put(KEY, b"data")
    (local_dir / "images" / "1" / "thumb" / ".a.webp.deadbeef.tmp").write_bytes(b"half")

    assert [f.key for f in local.list(keys.ROOT)] == [KEY]


def test_a_failed_local_put_leaves_no_temp_file(local, local_dir):
    with pytest.raises(OSError, match="connection reset"):
        local.put(KEY, Exploding())

    folder = local_dir / "images" / "1" / "thumb"
    assert not folder.exists() or list(folder.iterdir()) == []


def test_deleting_the_last_file_removes_the_empty_folders_but_not_the_images_root(local, local_dir):
    local.put(KEY, b"a")

    local.delete(KEY)

    assert not (local_dir / "images" / "1").exists()
    assert (local_dir / "images").is_dir()


def test_deleting_one_file_keeps_the_folder_of_the_others(local, local_dir):
    local.put(KEY, b"a")
    local.put("images/1/thumb/b.webp", b"b")

    local.delete(KEY)

    assert local.exists("images/1/thumb/b.webp")


def _assert_unreadable(local, key):
    assert local.exists(key) is False
    for read in (lambda: local.open(key).__enter__(), lambda: local.read_bytes(key), lambda: local.checksum(key)):
        with pytest.raises(MissingFileError):
            read()


def test_a_symlinked_file_is_missing_for_every_reader_and_not_listed(local, local_dir, tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"outside the data dir")
    folder = local_dir / "images" / "1" / "cutout"
    folder.mkdir(parents=True)
    (folder / "link.png").symlink_to(secret)

    _assert_unreadable(local, "images/1/cutout/link.png")
    assert list(local.list(keys.ROOT)) == []


def test_a_symlinked_user_folder_hides_what_is_behind_it(local, local_dir, tmp_path):
    outside = tmp_path / "outside"
    (outside / "cutout").mkdir(parents=True)
    (outside / "cutout" / "a.png").write_bytes(b"not yours")
    (local_dir / "images" / "1").symlink_to(outside, target_is_directory=True)

    _assert_unreadable(local, "images/1/cutout/a.png")
    assert list(local.list(keys.ROOT)) == []


def test_a_symlinked_kind_folder_hides_what_is_behind_it(local, local_dir, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.png").write_bytes(b"not yours")
    (local_dir / "images" / "1").mkdir()
    (local_dir / "images" / "1" / "cutout").symlink_to(outside, target_is_directory=True)

    _assert_unreadable(local, "images/1/cutout/a.png")
    assert list(local.list(keys.ROOT)) == []


def test_a_directory_with_a_picture_name_is_not_a_file(local, local_dir):
    (local_dir / "images" / "1" / "cutout" / "dir.png").mkdir(parents=True)
    key = "images/1/cutout/dir.png"

    _assert_unreadable(local, key)
    assert list(local.list(keys.ROOT)) == []


def test_local_ping_passes_while_the_images_folder_exists_and_fails_when_it_is_gone(local, local_dir):
    local.ping()

    (local_dir / "images").rmdir()

    with pytest.raises(StoreUnavailableError):
        local.ping()


# --- S3Store ---------------------------------------------------------------------------------------------------------


def test_building_an_s3_store_makes_no_network_call(s3_calls):
    S3Store(TEST_BUCKET, TEST_REGION)

    assert s3_calls == []


@pytest.mark.parametrize(
    ("ext", "media"), [(".png", "image/png"), (".webp", "image/webp"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg")]
)
def test_an_s3_put_sets_the_content_type_and_the_sha256_checksum(s3_store, s3, ext, media):
    data = picture(ext)
    key = f"images/1/original/a{ext}"

    s3_store.put(key, data)

    head = s3.head_object(Bucket=TEST_BUCKET, Key=key, ChecksumMode="ENABLED")
    assert head["ContentType"] == media
    assert head["ChecksumSHA256"] == sha256_b64(data)


def test_checksum_of_an_object_with_no_stored_checksum_is_hashed_from_its_bytes(s3_store, s3):
    s3.put_object(Bucket=TEST_BUCKET, Key=KEY, Body=b"planted by hand")

    assert s3_store.checksum(KEY) == sha256_b64(b"planted by hand")


def test_checksum_of_a_multipart_style_value_is_hashed_from_its_bytes(s3_store, monkeypatch):
    s3_store.put(KEY, b"real bytes")
    head_object = s3_store._client.head_object

    def composite(**kwargs):
        return {**head_object(**kwargs), "ChecksumSHA256": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=-3"}

    monkeypatch.setattr(s3_store._client, "head_object", composite)

    assert s3_store.checksum(KEY) == sha256_b64(b"real bytes")


def test_list_pages_through_more_than_1000_objects(s3_store, s3):
    for i in range(1001):
        s3.put_object(Bucket=TEST_BUCKET, Key=f"images/1/thumb/n{i}.webp", Body=b"x")

    assert len({f.key for f in s3_store.list(keys.ROOT)}) == 1001


@pytest.fixture
def clock(monkeypatch):
    """`clock.advance(seconds)` moves `time.monotonic` forward for the whole process."""
    real = time.monotonic
    offset = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: real() + offset[0])
    return type("Clock", (), {"advance": staticmethod(lambda seconds: offset.__setitem__(0, offset[0] + seconds))})


def _head_buckets(calls):
    return calls.count("HeadBucket")


def test_a_good_ping_is_cached_for_60_seconds(s3_store, s3_calls, clock):
    s3_store.ping()
    s3_store.ping()
    clock.advance(59)
    s3_store.ping()

    assert _head_buckets(s3_calls) == 1

    clock.advance(2)
    s3_store.ping()

    assert _head_buckets(s3_calls) == 2


def test_a_failed_ping_is_not_cached_so_recovery_shows_at_the_next_probe(s3_store, s3_fail, s3_calls):
    s3_fail(EndpointConnectionError(endpoint_url="https://s3.example"))
    for _ in range(2):
        with pytest.raises(StoreUnavailableError):
            s3_store.ping()
    assert _head_buckets(s3_calls) == 2

    s3_fail.heal()
    s3_store.ping()

    assert _head_buckets(s3_calls) == 3


def test_ping_of_a_deleted_bucket_is_store_unavailable(s3_store, s3):
    s3.delete_bucket(Bucket=TEST_BUCKET)

    with pytest.raises(StoreUnavailableError):
        s3_store.ping()


# --- S3 error translation (LLD 4.2) ----------------------------------------------------------------------------------

S3_CALLS = {
    "put": lambda s: s.put(KEY, b"x"),
    "open": lambda s: s.open(KEY).__enter__(),
    "read_bytes": lambda s: s.read_bytes(KEY),
    "exists": lambda s: s.exists(KEY),
    "delete": lambda s: s.delete(KEY),
    "list": lambda s: list(s.list(keys.ROOT)),
    "checksum": lambda s: s.checksum(KEY),
}
CLIENT_ERROR_CODES = ["AccessDenied", "NoSuchBucket", "BadDigest", "SlowDown", "InternalError", "503"]
BOTOCORE_ERRORS = {
    "endpoint": EndpointConnectionError(endpoint_url="https://s3.example"),
    "connect-timeout": ConnectTimeoutError(endpoint_url="https://s3.example"),
    "read-timeout": ReadTimeoutError(endpoint_url="https://s3.example"),
    "no-credentials": NoCredentialsError(),
}


@pytest.mark.parametrize("call", sorted(S3_CALLS))
@pytest.mark.parametrize("code", CLIENT_ERROR_CODES)
def test_any_other_s3_error_is_store_unavailable_with_a_fixed_message(s3_store, s3_fail, call, code):
    s3_fail(code)

    with pytest.raises(StoreUnavailableError) as raised:
        S3_CALLS[call](s3_store)

    assert str(raised.value) == UNAVAILABLE


@pytest.mark.parametrize("call", sorted(S3_CALLS))
@pytest.mark.parametrize("error", BOTOCORE_ERRORS.values(), ids=BOTOCORE_ERRORS.keys())
def test_a_network_or_credential_error_is_store_unavailable(s3_store, s3_fail, call, error):
    s3_fail(error)

    with pytest.raises(StoreUnavailableError) as raised:
        S3_CALLS[call](s3_store)

    assert str(raised.value) == UNAVAILABLE


def test_a_bad_digest_on_put_is_store_unavailable_and_stores_nothing(s3_store, s3_fail, s3):
    s3_fail("BadDigest", operation="PutObject")

    with pytest.raises(StoreUnavailableError):
        s3_store.put(KEY, b"x")

    s3_fail.heal()
    assert s3.list_objects_v2(Bucket=TEST_BUCKET).get("KeyCount") == 0


@pytest.mark.parametrize("code", ["NoSuchKey", "404", "NotFound"])
def test_a_not_found_answer_means_the_file_is_missing_not_that_the_store_is_down(s3_store, s3_fail, code):
    s3_fail(code)

    assert s3_store.exists(KEY) is False
    for read in ("open", "read_bytes", "checksum"):
        with pytest.raises(MissingFileError):
            S3_CALLS[read](s3_store)


def test_an_s3_failure_logs_the_error_code_and_never_a_bucket_key_or_address(s3_store, s3_fail, caplog):
    s3_fail("AccessDenied")

    with caplog.at_level(logging.INFO), pytest.raises(StoreUnavailableError):
        s3_store.put(KEY, b"x")

    assert "files_store_unavailable" in caplog.text
    assert "error=AccessDenied" in caplog.text
    for secret in (TEST_BUCKET, "images/", "amazonaws", KEY):
        assert secret not in caplog.text


def test_a_network_failure_logs_the_exception_class_and_no_endpoint(s3_store, s3_fail, caplog):
    s3_fail(EndpointConnectionError(endpoint_url="https://secret-bucket.s3.example"))

    with caplog.at_level(logging.INFO), pytest.raises(StoreUnavailableError):
        s3_store.exists(KEY)

    assert "files_store_unavailable" in caplog.text
    assert "EndpointConnectionError" in caplog.text
    assert "secret-bucket" not in caplog.text


# --- build_store, public, discard ------------------------------------------------------------------------------------


def test_with_no_bucket_the_store_is_local(tmp_path):
    assert isinstance(public.build_store(tmp_path, None, TEST_REGION), LocalStore)


def test_with_a_bucket_the_store_is_s3(tmp_path, s3):
    assert isinstance(public.build_store(tmp_path, TEST_BUCKET, TEST_REGION), S3Store)


def test_a_local_stack_never_loads_boto3(tmp_path):
    code = (
        "import sys, pathlib\n"
        "from kyapehnu.files import public\n"
        f"public.build_store(pathlib.Path({str(tmp_path)!r}), None, 'ap-south-1')\n"
        "bad = [m for m in ('boto3', 'botocore') if m in sys.modules]\n"
        "sys.exit(f'loaded: {bad}' if bad else 0)\n"
    )

    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False)

    assert (done.returncode, done.stderr) == (0, "")


@pytest.mark.parametrize(
    "name",
    [
        "FileStore",
        "StoredFile",
        "MissingFileError",
        "StoreUnavailableError",
        "InvalidKeyError",
        "KINDS",
        "IMAGE_NAME",
        "KIND_ALTERNATIVES",
        "KEY_PATTERN",
        "ROOT",
        "key",
        "prefix",
        "parse_key",
        "LocalStore",
        "build_store",
        "discard",
    ],
)
def test_public_exports_what_the_other_domains_use(name):
    assert hasattr(public, name)


def test_the_s3_store_stays_behind_build_store():
    assert not hasattr(public, "S3Store")


def test_key_pattern_is_the_whole_address():
    assert public.KEY_PATTERN.fullmatch(KEY)
    assert not public.KEY_PATTERN.fullmatch("images/1/thumb/a.webp/x")


class RecordingStore:
    """Only `delete`: what `discard` is allowed to use."""

    def __init__(self, failures=None):
        self.deleted: list[str] = []
        self.failures = failures or {}

    def delete(self, key):
        if key in self.failures:
            raise self.failures[key]
        self.deleted.append(key)


def test_discard_deletes_every_key_and_skips_none():
    store = RecordingStore()

    public.discard(store, [None, "images/1/thumb/a.webp", None, "images/1/thumb/b.webp"])

    assert store.deleted == ["images/1/thumb/a.webp", "images/1/thumb/b.webp"]


def test_discard_takes_a_generator():
    store = RecordingStore()

    public.discard(store, (k for k in ["images/1/thumb/a.webp"]))

    assert store.deleted == ["images/1/thumb/a.webp"]


def test_discard_logs_a_failure_and_goes_on_with_the_next_key(caplog):
    first, second = "images/1/thumb/a.webp", "images/1/thumb/b.webp"
    store = RecordingStore(failures={first: PermissionError("denied")})

    with caplog.at_level(logging.INFO):
        deleted = public.discard(store, [first, second])

    assert store.deleted == [second]
    assert deleted == 1
    assert "files_delete_failed error=PermissionError" in caplog.text
    assert "kind=" not in caplog.text
    assert "images/" not in caplog.text


def test_discard_stops_at_the_first_key_the_store_cannot_answer_for(caplog):
    first, second, third = (f"images/1/thumb/{n}.webp" for n in "abc")
    store = RecordingStore(failures={second: StoreUnavailableError()})

    with caplog.at_level(logging.INFO):
        deleted = public.discard(store, [first, second, third])

    assert store.deleted == [first]
    assert deleted == 1
    assert "files_delete_failed error=StoreUnavailableError left=2" in caplog.text
    assert "images/" not in caplog.text


def test_discard_removes_real_files_on_each_store_and_ignores_missing_ones(store):
    store.put(KEY, b"x")

    public.discard(store, [KEY, "images/1/thumb/never-there.webp", None])

    assert not store.exists(KEY)


def test_discard_during_an_outage_does_not_raise(s3_store, s3_down, caplog):
    with caplog.at_level(logging.INFO):
        public.discard(s3_store, [KEY])

    assert "files_delete_failed error=StoreUnavailableError" in caplog.text


# --- Ctx.files (LLD 2, A1) -------------------------------------------------------------------------------------------


def test_a_hand_built_ctx_serves_files_from_the_local_store(tmp_path):
    ctx = Ctx(engine=None, provider=None, prefs=Prefs(), data_dir=tmp_path)

    assert isinstance(ctx.files, LocalStore)
    assert ctx.files is ctx.files
    ctx.files.put(KEY, b"x")
    assert (tmp_path / KEY).read_bytes() == b"x"


def test_a_ctx_with_a_store_uses_exactly_that_store(tmp_path):
    chosen = RecordingStore()

    ctx = Ctx(engine=None, provider=None, prefs=Prefs(), data_dir=tmp_path, file_store=chosen)

    assert ctx.files is chosen


def test_the_local_store_lays_files_out_as_data_dir_images(tmp_path):
    store = LocalStore(tmp_path)

    store.put("images/3/cutout/c.png", b"png")

    assert Path(tmp_path / "images" / "3" / "cutout" / "c.png").read_bytes() == b"png"
