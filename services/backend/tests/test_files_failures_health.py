"""AC-24 (the `/readyz` side; the `/images` side is test_files_failures.py): with the picture store down the wardrobe
still loads, the readiness probe says `files` and recovers without a restart. A healthy ping is cached for 60 s, so
every test here goes down first on a fresh store and never calls a healthy `/readyz` before the down phase."""

import logging
import shutil

import pytest
from fastapi.testclient import TestClient

from tests.conftest import LOOPBACK_PEER, TEST_BUCKET
from tests.files_helpers import picture
from tests.owner_helpers import add_item, me

FILES_DOWN = {"status": "unavailable", "failed": ["files"]}


@pytest.fixture
def app(s3_app):
    return s3_app


def test_a_store_that_is_down_fails_readyz_with_files_while_the_wardrobe_still_loads(client_a, s3_down):
    down = client_a.get("/readyz")

    assert (down.status_code, down.json()) == (503, FILES_DOWN)
    assert client_a.get("/api/items").status_code == 200
    assert client_a.get("/livez").status_code == 200

    s3_down.heal()
    up = client_a.get("/readyz")

    assert (up.status_code, up.json()) == (200, {"status": "ok"})  # no restart


def test_readyz_needs_no_session_and_names_files_for_a_signed_out_caller(anon_client, s3_down):
    r = anon_client.get("/readyz")

    assert (r.status_code, r.json()) == (503, FILES_DOWN)


@pytest.mark.parametrize("code", ["AccessDenied", "SlowDown", "503"])
def test_a_store_that_refuses_fails_readyz_with_files_and_recovers(client_a, s3_fail, code):
    s3_fail(code)

    down = client_a.get("/readyz")

    assert (down.status_code, down.json()) == (503, FILES_DOWN)
    s3_fail.heal()
    assert client_a.get("/readyz").status_code == 200


def test_a_deleted_bucket_fails_readyz_with_files(client_a, s3):
    s3.delete_bucket(Bucket=TEST_BUCKET)

    r = client_a.get("/readyz")

    assert (r.status_code, r.json()) == (503, FILES_DOWN)


def test_files_is_listed_next_to_the_other_dependencies_that_are_down(client_a, valkey_fault, s3_down):
    valkey_fault()

    r = client_a.get("/readyz")

    assert r.status_code == 503
    assert r.json()["status"] == "unavailable"
    assert sorted(r.json()["failed"]) == ["files", "valkey"]


def test_the_not_ready_log_names_files_and_no_bucket_key_or_address(client_a, s3_down, caplog):
    caplog.set_level(logging.INFO)

    client_a.get("/readyz")

    assert any("event=not_ready" in m and "failed=files" in m for m in caplog.messages)
    for secret in (TEST_BUCKET, "amazonaws", "images/"):
        assert secret not in caplog.text


def test_the_disk_store_fails_readyz_with_files_when_its_folder_is_gone(make_app):
    local = make_app()  # no bucket: the disk store
    shutil.rmtree(local.state.ctx.data_dir / "images")

    r = TestClient(local, base_url="http://localhost", client=LOOPBACK_PEER).get("/readyz")

    assert (r.status_code, r.json()) == (503, FILES_DOWN)


# --- what keeps working while the store is down ---


def test_a_wear_is_recorded_without_a_collage_while_the_store_is_down_and_gets_one_later(app, client_a, s3, s3_fail):
    uid = me(client_a)
    ids = []
    for name, cat in (("shirt", "top"), ("chinos", "bottom")):
        key = f"images/{uid}/thumb/{name}.webp"
        s3.put_object(Bucket=TEST_BUCKET, Key=key, Body=picture(".webp"))
        ids.append(add_item(app.state.ctx, uid, name, category=cat, thumb_path=key).id)
    s3_fail("AccessDenied")

    r = client_a.post("/api/outfits/wear", json={"item_ids": ids})  # a wear is never failed by its picture

    assert r.status_code == 201
    assert r.json()["collage_path"] is None
    s3_fail.heal()
    again = client_a.post("/api/outfits/wear", json={"item_ids": ids, "worn_on": "2026-01-01"})
    assert again.status_code == 201
    assert again.json()["collage_path"]
    assert s3.head_object(Bucket=TEST_BUCKET, Key=again.json()["collage_path"])["ContentLength"] > 0


def test_the_mannequin_is_a_retryable_503_while_the_store_is_down(app, client_a, s3_fail):
    uid = me(client_a)
    ids = [add_item(app.state.ctx, uid, thumb_path=f"images/{uid}/thumb/shirt.webp").id]
    s3_fail("AccessDenied")

    r = client_a.post("/api/outfits/mannequin", json={"item_ids": ids})

    assert r.status_code == 503
    assert r.json()["code"] == "files_unavailable"
    assert r.headers["retry-after"] == "1"


def test_a_hard_delete_goes_through_while_the_store_is_down_and_leaves_the_object_for_the_sweep(
    app, client_a, s3, s3_fail, caplog
):
    caplog.set_level(logging.INFO)
    uid = me(client_a)
    key = f"images/{uid}/thumb/shirt.webp"
    s3.put_object(Bucket=TEST_BUCKET, Key=key, Body=b"x")
    item = add_item(app.state.ctx, uid, thumb_path=key)
    s3_fail("AccessDenied", "DeleteObject")

    r = client_a.delete(f"/api/items/{item.id}", params={"hard": True})

    assert r.status_code == 204
    assert client_a.get(f"/api/items/{item.id}").status_code == 404
    assert any("event=files_delete_failed" in m and "error=StoreUnavailableError" in m for m in caplog.messages)
    s3_fail.heal()
    assert s3.head_object(Bucket=TEST_BUCKET, Key=key)["ContentLength"] == 1  # the sweep removes it later
