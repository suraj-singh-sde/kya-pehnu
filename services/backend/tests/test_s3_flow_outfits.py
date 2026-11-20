"""AC-13 for the outfit side: wear, save an outfit (thumbs up), the mannequin picture and the hard delete of an item
run on the S3 store. Every key a row names is in the bucket, `GET /images/...` answers 200, and nothing is written to
the disk. The pictures of the items exist only in the bucket, so a collage or a mannequin that shows them was read
from there."""

import io
import logging
import re

import pytest
from botocore.exceptions import EndpointConnectionError
from PIL import Image

from kyapehnu.wardrobe.outfits import wear
from tests.conftest import TEST_BUCKET, sign_in
from tests.files_helpers import picture
from tests.owner_helpers import add_item, image_files

RED, BLUE, GREEN = (220, 30, 30), (30, 30, 220), (30, 160, 30)
OPENAI = {
    "llm_provider": "openai_compatible",
    "openai_base_url": "https://api.openai.com/v1",
    "openai_api_key": "sk-oai-FLOW-KEY-123456",
}


@pytest.fixture
def app(s3, make_app):
    """The S3 app with an image model, so the mannequin can be made (the model itself is the scripted `fake`)."""
    return make_app(files_bucket=TEST_BUCKET, **OPENAI)


@pytest.fixture
def owner(app):
    return sign_in(app, "sub-a")


@pytest.fixture
def uid(owner):
    return owner[0]


@pytest.fixture
def client(owner):
    return owner[1]


@pytest.fixture
def ids(app, uid):
    """A top, a bottom and footwear whose thumbnails are in the bucket and nowhere else."""
    out = []
    for name, category, rgb in (("Shirt", "top", RED), ("Chinos", "bottom", BLUE), ("Loafers", "footwear", GREEN)):
        key = f"images/{uid}/thumb/{name.lower()}.webp"
        app.state.ctx.files.put(key, picture(".webp", rgb))
        out.append(add_item(app.state.ctx, uid, name, category=category, thumb_path=key).id)
    return out


def keys_in_bucket(s3) -> set[str]:
    return {o["Key"] for o in s3.list_objects_v2(Bucket=TEST_BUCKET).get("Contents", [])}


def open_picture(response) -> Image.Image:
    return Image.open(io.BytesIO(response.content))


def test_a_wear_draws_its_collage_from_the_bucket_into_the_bucket(app, client, uid, ids, s3):
    r = client.post("/api/outfits/wear", json={"item_ids": ids})

    assert r.status_code == 201
    key = r.json()["collage_path"]
    assert re.fullmatch(rf"images/{uid}/collage/[0-9a-f]{{32}}\.webp", key)
    assert s3.head_object(Bucket=TEST_BUCKET, Key=key)["ContentType"] == "image/webp"
    served = client.get(f"/{key}")
    assert (served.status_code, served.headers["content-type"]) == (200, "image/webp")
    assert open_picture(served).size == (800, 800)
    assert open_picture(served).convert("RGB").getpixel((200, 400)) == pytest.approx(RED, abs=14)  # the shirt's thumb
    assert image_files(app.state.ctx.data_dir) == []


def test_a_thumbs_up_on_a_new_set_saves_the_outfit_with_its_collage_in_the_bucket(client, ids, s3):
    r = client.post("/api/feedback", json={"item_ids": ids, "rating": 1, "reason": ""})

    assert r.status_code == 201, r.text
    [outfit] = client.get("/api/outfits").json()
    assert outfit["collage_path"]
    assert s3.head_object(Bucket=TEST_BUCKET, Key=outfit["collage_path"])["ContentLength"] > 0
    assert client.get(f"/{outfit['collage_path']}").status_code == 200


def test_the_mannequin_is_made_once_into_the_bucket_and_served(app, client, uid, ids, fake, s3):
    fake.edits = [picture(".png", (10, 10, 10))]

    first = client.post("/api/outfits/mannequin", json={"item_ids": ids})
    again = client.post("/api/outfits/mannequin", json={"item_ids": list(reversed(ids))})

    assert (first.status_code, again.status_code) == (200, 200), first.text
    key = first.json()["path"]
    assert key == again.json()["path"] == f"images/{uid}/mannequin/{'-'.join(map(str, ids))}.png"
    assert len(fake.edit_calls) == 1  # the second request found the picture in the bucket
    assert Image.open(io.BytesIO(fake.edit_calls[0]["image"])).size == (800, 800)  # the collage, from bucket thumbs
    assert s3.head_object(Bucket=TEST_BUCKET, Key=key)["ContentType"] == "image/png"
    served = client.get(f"/{key}")
    assert (served.status_code, served.headers["content-type"]) == (200, "image/png")
    assert image_files(app.state.ctx.data_dir) == []


def test_a_provider_failure_stores_no_mannequin(client, uid, ids, fake, s3):
    fake.edits = [b"not a picture"]

    r = client.post("/api/outfits/mannequin", json={"item_ids": ids})

    assert (r.status_code, r.json()["code"]) == (502, "provider_error")
    assert not [k for k in keys_in_bucket(s3) if "/mannequin/" in k]


def test_a_hard_delete_removes_the_item_its_collages_and_its_mannequins_from_the_bucket(client, uid, ids, fake, s3):
    shirt, chinos, loafers = ids
    fake.edits = [picture(".png"), picture(".png")]
    with_shirt = client.post("/api/outfits/mannequin", json={"item_ids": ids}).json()["path"]
    without_shirt = client.post("/api/outfits/mannequin", json={"item_ids": [chinos, loafers]}).json()["path"]
    collage_with = client.post("/api/outfits/wear", json={"item_ids": ids}).json()["collage_path"]
    collage_without = client.post("/api/outfits/wear", json={"item_ids": [chinos, loafers]}).json()["collage_path"]

    r = client.delete(f"/api/items/{shirt}", params={"hard": True})

    assert r.status_code == 204
    now = keys_in_bucket(s3)
    assert not {f"images/{uid}/thumb/shirt.webp", with_shirt, collage_with} & now
    assert {
        f"images/{uid}/thumb/chinos.webp",
        f"images/{uid}/thumb/loafers.webp",
        without_shirt,
        collage_without,
    } <= now
    assert client.get(f"/{with_shirt}").status_code == 404
    assert client.get(f"/{without_shirt}").status_code == 200


def test_an_item_whose_object_is_not_in_the_bucket_still_hard_deletes(app, client, uid, s3_calls):
    item = add_item(app.state.ctx, uid, thumb_path=f"images/{uid}/thumb/never-stored.webp")

    r = client.delete(f"/api/items/{item.id}", params={"hard": True})  # a missing key is not an error

    assert r.status_code == 204
    assert client.get(f"/api/items/{item.id}").status_code == 404
    assert "DeleteObject" in s3_calls  # the delete went to the bucket, not to a folder


# --- discard_collage, on both stores ---


def test_discarding_a_collage_removes_it_from_the_store(store, plant):
    key = "images/1/collage/c.webp"
    plant(key)

    wear.discard_collage(store, key)

    assert not store.exists(key)


def test_discarding_no_collage_or_a_missing_one_is_not_an_error(store):
    wear.discard_collage(store, None)
    wear.discard_collage(store, "images/1/collage/never-was.webp")


def test_a_collage_that_cannot_be_discarded_is_logged_by_class_and_left_for_the_sweep(s3_store, s3_fail, s3, caplog):
    key = "images/1/collage/c.webp"
    s3.put_object(Bucket=TEST_BUCKET, Key=key, Body=b"x")
    s3_fail(EndpointConnectionError(endpoint_url="https://s3.example"))
    caplog.set_level(logging.INFO)

    wear.discard_collage(s3_store, key)  # does not raise

    s3_fail.heal()
    assert s3_store.exists(key)
    [event] = [m for m in caplog.messages if "event=files_delete_failed" in m]
    assert "error=StoreUnavailableError" in event
    assert key not in caplog.text and TEST_BUCKET not in caplog.text
