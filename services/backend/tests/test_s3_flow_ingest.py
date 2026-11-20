"""AC-13 (ingest part) and AC-23 (seed) on the S3 store: upload, review, accept, merge, clean pictures and seed put
their pictures in the bucket (none on the disk), and every key a row names is there and served (LLD 4.4, 9)."""

import pytest
from sqlmodel import Session, select

from kyapehnu.wardrobe import public as wardrobe
from kyapehnu.wardrobe.models import Item, Photo
from kyapehnu.wardrobe.seed import SEED_ITEMS
from tests.conftest import TEST_BUCKET, noise_image_bytes, sign_in
from tests.fakes import analysis, garment
from tests.ingest_store_helpers import bucket_keys
from tests.owner_helpers import image_files
from tests.test_pictures import IMAGE_MODEL, cutout_with_transparency

OPENAI_KEY = "sk-oai-S3-FLOW-123456"


@pytest.fixture
def signed_in(s3_app):
    owner, client = sign_in(s3_app, "sub-a")
    with client:
        yield owner, client


@pytest.fixture
def openai_in(make_app, tmp_path):
    """An app on the bucket with OpenAI as the provider and automatic clean pictures off."""
    config = tmp_path / "openai.yaml"
    config.write_text("ingest:\n  auto_clean_pictures: false\n")
    app = make_app(
        files_bucket=TEST_BUCKET,
        config_file=config,
        llm_provider="openai_compatible",
        openai_base_url="https://api.openai.com/v1",
        openai_api_key=OPENAI_KEY,
    )
    owner, client = sign_in(app, "sub-a")
    with client:
        yield owner, client


def _pieces(client, fake, n=1, seed=1):
    fake.replies.append(analysis(*[garment((0.0, i / n, 1.0, (i + 1) / n)) for i in range(n)]))
    r = client.post("/api/ingest", files=[("files", (f"p{seed}.jpg", noise_image_bytes(seed), "image/jpeg"))])
    assert r.status_code == 201, r.text
    client.app.state.worker.drain()
    return client.get("/api/ingest/pieces").json()


def _photo_keys(client) -> list[str]:
    with Session(client.app.state.ctx.engine) as s:
        return [key for p in s.exec(select(Photo)).all() for key in (p.original_path, p.normalized_path)]


def _assert_in_bucket_and_served(client, s3, keys) -> None:
    assert keys
    for key in keys:
        assert s3.head_object(Bucket=TEST_BUCKET, Key=key)["ContentLength"] > 0
        assert client.get(f"/{key}").status_code == 200, key


def test_upload_puts_the_original_the_normalized_copy_and_every_piece_in_the_bucket(signed_in, s3, fake):
    owner, client = signed_in

    [piece] = _pieces(client, fake)

    keys = [*_photo_keys(client), piece["cutout_path"], piece["thumb_path"]]
    assert all(key.startswith(f"images/{owner}/") for key in keys)
    _assert_in_bucket_and_served(client, s3, keys)
    assert bucket_keys(s3) == sorted(keys)  # nothing else was left behind
    assert image_files(client.app.state.ctx.data_dir) == []


def test_accepting_a_piece_gives_the_item_pictures_that_are_in_the_bucket(signed_in, s3, fake):
    _, client = signed_in
    [piece] = _pieces(client, fake)

    item = client.post(f"/api/ingest/pieces/{piece['id']}/accept", json={}).json()

    _assert_in_bucket_and_served(client, s3, [item["cutout_path"], item["thumb_path"]])


def test_merging_pieces_stores_the_new_picture_and_deletes_the_old_ones_from_the_bucket(signed_in, s3, fake):
    _, client = signed_in
    a, b = _pieces(client, fake, n=2)
    old = {a["cutout_path"], a["thumb_path"], b["cutout_path"], b["thumb_path"]}
    assert old <= set(bucket_keys(s3))

    merged = client.post(f"/api/ingest/pieces/{a['id']}/merge", json={"other_piece_ids": [b["id"]]}).json()

    keys = set(bucket_keys(s3))
    assert {merged["cutout_path"], merged["thumb_path"]} <= keys
    assert not old & keys
    _assert_in_bucket_and_served(client, s3, [merged["cutout_path"], merged["thumb_path"]])


def test_the_clean_picture_is_made_shown_and_removed_in_the_bucket(openai_in, s3, fake):
    _, client = openai_in
    [piece] = _pieces(client, fake)
    fake.edits.append(cutout_with_transparency())

    made = client.post(f"/api/ingest/pieces/{piece['id']}/clean-picture").json()
    alt = [made["alt_cutout_path"], made["alt_thumb_path"]]
    _assert_in_bucket_and_served(client, s3, alt)
    assert fake.edit_calls[0]["model"] == IMAGE_MODEL

    shown = client.post(f"/api/ingest/pieces/{piece['id']}/clean-picture/switch").json()
    assert (shown["cutout_path"], shown["thumb_path"]) == tuple(alt)
    _assert_in_bucket_and_served(client, s3, [shown["cutout_path"], shown["alt_cutout_path"]])

    after = client.delete(f"/api/ingest/pieces/{piece['id']}/clean-picture").json()

    assert (after["cutout_path"], after["thumb_path"]) == (piece["cutout_path"], piece["thumb_path"])
    keys = set(bucket_keys(s3))
    assert not set(alt) & keys  # the clean picture is gone ...
    assert {piece["cutout_path"], piece["thumb_path"]} <= keys  # ... and the original remains
    assert client.get(f"/{alt[0]}").status_code == 404


def test_replacing_a_clean_picture_deletes_the_previous_one_from_the_bucket(openai_in, s3, fake):
    _, client = openai_in
    [piece] = _pieces(client, fake)
    fake.edits += [cutout_with_transparency(), cutout_with_transparency()]
    first = client.post(f"/api/ingest/pieces/{piece['id']}/clean-picture").json()

    second = client.post(f"/api/ingest/pieces/{piece['id']}/clean-picture").json()

    keys = set(bucket_keys(s3))
    assert not {first["alt_cutout_path"], first["alt_thumb_path"]} & keys
    assert {second["alt_cutout_path"], second["alt_thumb_path"], piece["cutout_path"]} <= keys


def test_seed_puts_every_picture_in_the_bucket_and_the_owner_is_served_them(s3_app, s3):
    owner, client = sign_in(s3_app, "sub-a")
    ctx = s3_app.state.ctx

    assert wardrobe.seed(owner, ctx.engine, ctx.files) == len(SEED_ITEMS)

    with Session(ctx.engine) as s:
        rows = s.exec(select(Item).where(Item.user_id == owner)).all()
    keys = [key for row in rows for key in (row.cutout_path, row.thumb_path)]
    assert len(keys) == 2 * len(SEED_ITEMS)
    assert bucket_keys(s3) == sorted(keys)
    _assert_in_bucket_and_served(client, s3, keys[:4])
    assert image_files(ctx.data_dir) == []
