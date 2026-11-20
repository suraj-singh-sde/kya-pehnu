"""AC-12 (the settings, not a rebuild, choose the store) and AC-11 (a laptop with no S3 settings keeps the disk):
`build_ctx` picks the store from `Settings`, a picture that is in the bucket is served by the app, and the schema
command never builds an S3 client."""

import json

from kyapehnu.core import cli
from kyapehnu.core.app import build_ctx
from kyapehnu.core.config import get_settings
from kyapehnu.files.local import LocalStore
from kyapehnu.files.s3 import S3Store
from tests.conftest import TEST_BUCKET, TEST_REGION, sign_in
from tests.files_helpers import picture
from tests.owner_helpers import image_files, me


def test_with_no_bucket_the_app_stores_pictures_on_the_disk(settings, kv):
    ctx = build_ctx(settings, kv)

    assert isinstance(ctx.file_store, LocalStore)
    assert ctx.files is ctx.file_store
    ctx.engine.dispose()


def test_with_a_bucket_set_the_same_build_stores_pictures_in_the_bucket(settings, kv, s3):
    ctx = build_ctx(settings.model_copy(update={"files_bucket": TEST_BUCKET}), kv)

    assert isinstance(ctx.file_store, S3Store)
    assert ctx.files is ctx.file_store
    ctx.engine.dispose()


def test_the_bucket_and_region_come_from_the_environment(monkeypatch, tmp_path, kv, s3):
    monkeypatch.setenv("KYAPEHNU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KYAPEHNU_FILES_BUCKET", TEST_BUCKET)
    monkeypatch.setenv("KYAPEHNU_FILES_REGION", TEST_REGION)
    get_settings.cache_clear()
    try:
        ctx = build_ctx(get_settings(), kv)
        ctx.files.put("images/1/thumb/a.webp", b"x")
    finally:
        get_settings.cache_clear()

    assert s3.get_object(Bucket=TEST_BUCKET, Key="images/1/thumb/a.webp")["Body"].read() == b"x"
    ctx.engine.dispose()


def test_a_picture_put_in_the_bucket_by_hand_is_served_to_its_owner(s3_app, s3):
    owner, client = sign_in(s3_app, "sub-a")
    data = picture(".webp")
    s3.put_object(Bucket=TEST_BUCKET, Key=f"images/{owner}/thumb/by-hand.webp", Body=data, ContentType="image/webp")

    r = client.get(f"/images/{owner}/thumb/by-hand.webp")

    assert (r.status_code, r.content, r.headers["content-type"]) == (200, data, "image/webp")


def test_what_the_app_stores_goes_to_the_bucket_and_nothing_to_the_disk(s3_app, s3):
    owner, client = sign_in(s3_app, "sub-a")
    key = f"images/{owner}/cutout/new.png"

    s3_app.state.ctx.files.put(key, picture(".png"))

    assert s3.head_object(Bucket=TEST_BUCKET, Key=key)["ContentLength"] > 0
    assert image_files(s3_app.state.ctx.data_dir) == []
    assert client.get(f"/images/{owner}/cutout/new.png").status_code == 200


def test_the_default_app_without_a_bucket_serves_from_the_disk_with_no_aws_call(app, client_a, s3_calls):
    owner = me(client_a)
    data = picture(".png")
    app.state.ctx.files.put(f"images/{owner}/cutout/x.png", data)

    r = client_a.get(f"/images/{owner}/cutout/x.png")

    assert (r.status_code, r.content) == (200, data)
    assert (app.state.ctx.data_dir / "images" / str(owner) / "cutout" / "x.png").read_bytes() == data
    assert s3_calls == []


def test_the_openapi_command_never_builds_an_s3_client(monkeypatch, tmp_path, capsys):
    def refuse(*_args, **_kwargs):
        raise AssertionError("the schema needs no picture store")

    monkeypatch.setenv("KYAPEHNU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KYAPEHNU_FILES_BUCKET", TEST_BUCKET)
    monkeypatch.setattr(S3Store, "__init__", refuse)
    get_settings.cache_clear()
    try:
        cli.main(["openapi"])
    finally:
        get_settings.cache_clear()

    assert "/api/items" in json.loads(capsys.readouterr().out)["paths"]


def test_the_app_made_with_a_bucket_holds_the_s3_store(s3_app):
    assert isinstance(s3_app.state.ctx.files, S3Store)


def test_the_app_made_without_a_bucket_holds_the_local_store(app):
    assert isinstance(app.state.ctx.files, LocalStore)
