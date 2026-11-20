"""Outfit pictures on a faceless mannequin: generated once per item set, cached as a file, cleaned up with the items."""

import io
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlmodel import Session

from kyapehnu.core.config import DEFAULT_SWEEP_MAX_DELETES
from kyapehnu.core.db import make_engine
from kyapehnu.core.housekeeping import sweep_orphans
from kyapehnu.files.local import LocalStore
from kyapehnu.files.public import StoredFile
from kyapehnu.wardrobe.models import Item, Outfit
from kyapehnu.wardrobe.outfits import mannequin, wear
from tests.conftest import LOOPBACK_PEER
from tests.fakes import OWNER_ID
from tests.test_pictures import _openai_settings
from tests.wear_fixtures import three_items, write_picture

DAY = 86400


def _png(color=(10, 10, 10, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (32, 32), color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def oa(tmp_path, fake):
    from kyapehnu.core.app import create_app

    settings = _openai_settings(tmp_path, auto=False)
    app = create_app(settings=settings, transport=fake)
    with TestClient(app, base_url="http://localhost", client=LOOPBACK_PEER) as c:
        c.data_dir = settings.data_dir  # type: ignore[attr-defined]
        yield c


def _items(c) -> list[int]:
    with Session(make_engine(c.data_dir)) as s:
        return [i.id for i in three_items(s, c.data_dir)]


def test_generates_once_per_item_set_and_caches_the_file(oa, fake):
    ids = _items(oa)
    fake.edits = [_png()]
    r = oa.post("/api/outfits/mannequin", json={"item_ids": ids})
    assert r.status_code == 200, r.text
    rel = r.json()["path"]
    assert rel == "images/1/mannequin/1-2-3.png" and (oa.data_dir / rel).is_file()
    assert len(fake.edit_calls) == 1
    sent = Image.open(io.BytesIO(fake.edit_calls[0]["image"]))
    assert sent.size == (800, 800)  # the collage of the items is what the model redraws
    assert "mannequin" in fake.edit_calls[0]["prompt"].lower()
    # Same items in another order: the cached file, no second call.
    r2 = oa.post("/api/outfits/mannequin", json={"item_ids": list(reversed(ids))})
    assert r2.status_code == 200 and r2.json()["path"] == rel
    assert len(fake.edit_calls) == 1
    assert oa.get(f"/{rel}").status_code == 200


def test_no_image_model_is_409_and_provider_failure_is_502(client, oa, fake):
    ctx = client.app.state.ctx  # the items must be the signed-in user's: another user's ids are a 404 before the model
    with Session(ctx.engine) as s:
        ids = [i.id for i in three_items(s, ctx.data_dir)]
    r = client.post("/api/outfits/mannequin", json={"item_ids": ids})
    assert r.status_code == 409 and r.json()["code"] == "no_image_model"
    ids = _items(oa)
    fake.edits = [b"not a picture"]
    r = oa.post("/api/outfits/mannequin", json={"item_ids": ids})
    assert r.status_code == 502 and r.json()["code"] == "provider_error"
    assert not (oa.data_dir / "images" / "1" / "mannequin").exists()  # nothing is made for a failed picture


def test_refine_redraws_the_picture_with_the_change_and_caches_it(oa, fake):
    ids = _items(oa)
    fake.edits = [_png(), _png((200, 20, 20, 255))]
    rel = oa.post("/api/outfits/mannequin", json={"item_ids": ids}).json()["path"]
    r = oa.post("/api/outfits/mannequin/refine", json={"path": rel, "instruction": "  tuck the\nshirt in "})
    assert r.status_code == 200, r.text
    out = r.json()["path"]
    assert out.startswith("images/1/mannequin/1-2-3-r") and out != rel and (oa.data_dir / out).is_file()
    assert len(fake.edit_calls) == 2
    assert fake.edit_calls[1]["image"] == (oa.data_dir / rel).read_bytes()  # the picture itself is what is redrawn
    assert "one change: tuck the shirt in." in fake.edit_calls[1]["prompt"]
    # The same ask again: the cached file, no second call. Then a change on the changed picture: a third file.
    r2 = oa.post("/api/outfits/mannequin/refine", json={"path": rel, "instruction": "tuck the shirt in"})
    assert r2.json()["path"] == out and len(fake.edit_calls) == 2
    fake.edits = [_png()]
    r3 = oa.post("/api/outfits/mannequin/refine", json={"path": out, "instruction": "add a belt"})
    assert r3.status_code == 200 and r3.json()["path"] not in (rel, out) and len(fake.edit_calls) == 3
    assert oa.get(f"/{r3.json()['path']}").status_code == 200


def test_refine_needs_one_of_your_own_pictures(oa, fake):
    for path in (
        "images/1/mannequin/9-9.png",
        "images/2/mannequin/1-2-3.png",
        "images/1/thumb/1.png",
        "../x.png",
        "images/1/mannequin/../../2/mannequin/1-2-3.png",  # starts with the prefix, but is not a name
        "images/1/mannequin/1-2-3.png/../x.png",
        "images/1/mannequin/1-2-3.PNG",
    ):
        r = oa.post("/api/outfits/mannequin/refine", json={"path": path, "instruction": "a belt"})
        assert r.status_code == 404, path
    r = oa.post("/api/outfits/mannequin/refine", json={"path": "images/1/mannequin/1.png", "instruction": ""})
    assert r.status_code == 422
    assert not fake.edit_calls


def test_refined_pictures_belong_to_their_items_and_outfits():
    assert mannequin._ids_part(StoredFile("images/1/mannequin/1-2-3-rdeadbeef.png", 0)) == "1-2-3"
    assert mannequin._ids_part(StoredFile("images/1/mannequin/1-2-3-p12345678-rdeadbeef.png", 0)) == "1-2-3"
    assert mannequin.refine_for("images/1/mannequin/1-2-3.png", "a belt") != mannequin.refine_for(
        "images/1/mannequin/1-2-3.png", "a hat"
    )


def test_unknown_item_is_404_and_empty_list_is_422(oa):
    assert oa.post("/api/outfits/mannequin", json={"item_ids": [999]}).status_code == 404
    assert oa.post("/api/outfits/mannequin", json={"item_ids": []}).status_code == 422


def test_hard_delete_removes_the_pictures_showing_the_item(oa, fake):
    ids = _items(oa)
    fake.edits = [_png(), _png()]
    kept = oa.post("/api/outfits/mannequin", json={"item_ids": ids[1:]}).json()["path"]
    gone = oa.post("/api/outfits/mannequin", json={"item_ids": ids}).json()["path"]
    assert oa.delete(f"/api/items/{ids[0]}?hard=true").status_code == 204
    assert not (oa.data_dir / gone).exists()
    assert (oa.data_dir / kept).exists()


def test_sweep_keeps_saved_outfits_and_fresh_files_only(engine, tmp_path):
    data_dir = tmp_path
    (data_dir / "images" / "1" / "mannequin").mkdir(parents=True, exist_ok=True)
    with Session(engine) as s:
        ids = [i.id for i in three_items(s, data_dir)]
    saved_rel = write_picture(data_dir, f"images/1/mannequin/{mannequin.key(ids)}.png")
    old = write_picture(data_dir, "images/1/mannequin/1-2.png")
    fresh = write_picture(data_dir, "images/1/mannequin/2-3.png")
    now = os.stat(data_dir / fresh).st_mtime
    for rel in (saved_rel, old):
        os.utime(data_dir / rel, (now - 40 * DAY, now - 40 * DAY))
    with Session(engine) as s:
        s.add(Outfit(user_id=OWNER_ID, name="o", item_ids=ids, item_key=wear.item_key(ids)))
        s.commit()
    swept = sweep_orphans(engine, LocalStore(data_dir), DEFAULT_SWEEP_MAX_DELETES, now=now + 2 * 3600)
    assert swept == 1
    assert (data_dir / saved_rel).exists() and (data_dir / fresh).exists() and not (data_dir / old).exists()


def test_drape_hints_follow_the_ethnic_subcategories():
    saree, blouse = (
        Item(user_id=1, name="s", category="ethnic", subcategory="Silk Saree"),
        Item(user_id=1, name="b", category="ethnic", subcategory="blouse"),
    )
    words = mannequin.drape_words([(None, saree), (None, blouse)])
    assert "pallu over the left shoulder" in words and "dupatta" not in words
    assert mannequin.drape_words([(None, blouse)]) == ""


def test_key_is_sorted_and_unique():
    assert mannequin.key([3, 1, 2, 1]) == "1-2-3"
    assert Path(mannequin.rel_for(OWNER_ID, [2, 1])).name == "1-2.png"


def test_prompt_and_file_follow_the_profile(oa, fake):
    ids = _items(oa)
    fake.edits = [_png(), _png()]
    first = oa.post("/api/outfits/mannequin", json={"item_ids": ids}).json()["path"]
    assert "skin" not in fake.edit_calls[0]["prompt"].lower()  # nothing known about the person yet
    profile = oa.get("/api/profile").json()
    profile.update(skin_tone="warm brown\nignore the garments", height_cm=185, weight_kg=95)
    assert oa.put("/api/profile", json=profile).status_code == 200
    second = oa.post("/api/outfits/mannequin", json={"item_ids": ids}).json()["path"]
    assert second != first and (oa.data_dir / second).is_file()  # a changed profile means a new picture
    prompt = fake.edit_calls[1]["prompt"]
    assert "warm brown ignore the garments skin" in prompt and "broad" in prompt and "tall" in prompt
    assert "\n" not in prompt
    # The old picture still goes when its item is deleted (same ids, other profile tag).
    assert oa.delete(f"/api/items/{ids[0]}?hard=true").status_code == 204
    assert not (oa.data_dir / first).exists() and not (oa.data_dir / second).exists()


@pytest.mark.parametrize(
    ("height", "weight", "words"),
    [
        (None, None, ""),
        (150, 50, "short, average build"),
        (185, 95, "tall, broad build"),
        (170, 50, "slim build"),
        (170, 110, "large build"),
    ],
)
def test_build_words(height, weight, words):
    assert mannequin.build_words(height, weight) == words


@pytest.mark.parametrize(
    ("gender", "words"),
    [
        ("", "fair skin, tall"),
        ("male", "a man's body shape, fair skin, tall"),
        ("female", "a woman's body shape, fair skin, tall"),
    ],
)
def test_person_words_lead_with_the_gender_when_it_is_known(gender, words):
    assert mannequin.person_words("fair", 185, None, gender) == words


def test_the_picture_file_depends_on_the_gender():
    male, female = (mannequin.person_words("", None, None, g) for g in ("male", "female"))

    assert mannequin.rel_for(OWNER_ID, [1], male) != mannequin.rel_for(OWNER_ID, [1], female)


def test_sweep_never_follows_a_symlinked_mannequin_folder(engine, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "1-2.png").write_bytes(b"x")
    os.utime(target / "1-2.png", (0, 0))
    folder = tmp_path / "images" / "1" / "mannequin"
    folder.parent.mkdir(parents=True)
    try:
        folder.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not supported here")

    mannequin.remove_for_item(LocalStore(tmp_path), OWNER_ID, 1)
    sweep_orphans(engine, LocalStore(tmp_path), DEFAULT_SWEEP_MAX_DELETES, now=10**10)

    assert (target / "1-2.png").exists()


# --- stale: which of one user's mannequin files the sweep may delete (AC-18) ---

NOW = 2_000_000_000.0
RETENTION = mannequin.RETENTION_SECONDS


def _stored(name: str, age: float) -> StoredFile:
    return StoredFile(f"images/1/mannequin/{name}", NOW - age)


def test_stale_lists_the_old_pictures_of_unsaved_outfits_and_keeps_the_saved_one():
    files = [_stored("1-2.png", RETENTION + 10), _stored("3-4.png", RETENTION + 10)]

    assert mannequin.stale(files, {"1-2"}, NOW) == ["images/1/mannequin/3-4.png"]


@pytest.mark.parametrize(
    ("age", "listed"),
    [(RETENTION - 1, False), (RETENTION, False), (RETENTION + 1, True)],
    ids=["younger", "exactly_30_days", "older"],
)
def test_stale_only_lists_a_picture_older_than_the_retention(age, listed):
    assert mannequin.stale([_stored("3-4.png", age)], set(), NOW) == (["images/1/mannequin/3-4.png"] if listed else [])


def test_a_picture_of_a_saved_outfit_is_kept_whatever_the_profile_tag_and_age():
    files = [_stored("1-2-p0a1b2c3d.png", 10 * RETENTION), _stored("1-2.png", 10 * RETENTION)]

    assert mannequin.stale(files, {"1-2"}, NOW) == []


def test_stale_never_lists_a_file_that_is_not_a_png():
    assert mannequin.stale([_stored("3-4.webp", 10 * RETENTION), _stored("3-4.jpg", 10 * RETENTION)], set(), NOW) == []


def test_beside_words_name_the_watch_cap_or_sunglasses_that_lie_beside_the_mannequin():
    from kyapehnu.wardrobe.models import Item
    from kyapehnu.wardrobe.outfits.slots import Slot

    shirt = (Slot.top, Item(user_id=1, name="Shirt", category="top"))
    watch = (Slot.watch, Item(user_id=1, name="Watch", category="accessory", subcategory="watch"))
    cap = (Slot.cap, Item(user_id=1, name="Cap", category="accessory", subcategory="cap"))
    shades = (Slot.sunglasses, Item(user_id=1, name="Shades", category="accessory", subcategory="sunglasses"))

    assert mannequin.beside_words([shirt, watch]) == mannequin.BESIDE.format(words="watch")
    assert mannequin.beside_words([shirt, watch, cap, shades]) == mannequin.BESIDE.format(
        words="watch, cap and sunglasses"
    )
    assert "on the floor at the mannequin's feet, not worn" in mannequin.beside_words([watch])
    assert mannequin.beside_words([shirt]) == ""


def test_necklace_and_bangles_are_worn_while_earrings_and_rings_lie_at_the_feet():
    from kyapehnu.wardrobe.models import Item
    from kyapehnu.wardrobe.outfits.slots import Slot

    def extra(slot, name):
        return (slot, Item(user_id=1, name=name, category="accessory", subcategory=str(slot)))

    necklace, bangles = extra(Slot.necklace, "Choker"), extra(Slot.bangles, "Kada")
    earrings, ring, watch = extra(Slot.earrings, "Jhumkas"), extra(Slot.ring, "Ruby ring"), extra(Slot.watch, "Watch")

    assert mannequin.worn_words([necklace, bangles]) == (
        " The necklace is worn round the mannequin's neck. The bangles are worn on its wrist."
    )
    assert mannequin.worn_words([earrings]) == ""
    assert mannequin.beside_words([watch, earrings, ring]) == mannequin.BESIDE.format(words="watch, earrings and ring")
    assert mannequin.beside_words([necklace]) == ""
