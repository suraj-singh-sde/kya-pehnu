"""Review actions, the items service (list, update with corrections) and the hard-delete cascade (D14, D16)."""

import io
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, select

from kyapehnu.core import errors
from kyapehnu.core.errors import AppError
from kyapehnu.files.local import LocalStore
from kyapehnu.files.public import StoreUnavailableError
from kyapehnu.wardrobe import items
from kyapehnu.wardrobe.ingest import images, pipeline, review
from kyapehnu.wardrobe.models import IngestJob, IngestPiece, Item, ItemCorrection, Photo
from tests.conftest import noise_image_bytes
from tests.fakes import OWNER_ID, analysis, ensure_owner, garment


@pytest.fixture(autouse=True)
def _owner(engine):
    with Session(engine) as s:
        ensure_owner(s)
        s.commit()


@pytest.fixture
def ctx(make_ctx):
    return make_ctx(
        [
            analysis(
                garment((0.0, 0.0, 0.5, 0.5), name="Kurta", category="ethnic"),
                garment((0.0, 0.5, 0.5, 1.0), name="Pyjama", category="bottom"),
                garment((0.5, 0.0, 1.0, 1.0), name="Shoes", category="footwear"),
            ),
            analysis(garment()),
        ]
    )


@pytest.fixture
def pieces(ctx):
    """Piece ids in order: Kurta, Pyjama, Shoes (photo a), White shirt (photo b)."""
    files = [("a.jpg", io.BytesIO(noise_image_bytes(1))), ("b.jpg", io.BytesIO(noise_image_bytes(2)))]
    with Session(ctx.engine) as s:
        job = pipeline.create_upload_job(OWNER_ID, s, ctx.files, files)
    pipeline.run_job(OWNER_ID, ctx, job.id)
    with Session(ctx.engine) as s:
        return [p.id for p in s.exec(select(IngestPiece).order_by(IngestPiece.id)).all()]


def _accept(s, piece_id, overrides=None):
    return review.accept_piece(s, s.get(IngestPiece, piece_id), overrides)


def _set_state(ctx, piece_id, state):
    with Session(ctx.engine) as s:
        s.get(IngestPiece, piece_id).state = state
        s.commit()


# --- review actions (AC-24, AC-25, AC-26, D16) ---


def test_review_error_is_the_shared_conflict_error():
    assert review.ReviewError is errors.ReviewError
    assert (errors.ReviewError.status, errors.ReviewError.code) == (409, "review_conflict")


def test_accept_creates_item_with_ai_tags(ctx, pieces):
    with Session(ctx.engine) as s:
        piece = s.get(IngestPiece, pieces[2])
        item = review.accept_piece(s, piece)
        assert item.name == "Shoes" and item.category == "footwear"
        assert item.ai_tags == piece.proposed_tags and item.cutout_path and item.thumb_path
        assert (item.photo_id, item.bbox, item.phash) == (piece.photo_id, piece.bbox, piece.phash)
        assert (piece.state, piece.item_id) == ("accepted", item.id)
        assert s.exec(select(ItemCorrection)).all() == []


def test_accept_with_overrides_records_corrections_only_for_changed_tag_fields(ctx, pieces):
    with Session(ctx.engine) as s:
        item = _accept(s, pieces[2], {"name": "Brown loafers", "formality": 3, "notes": "resoled"})
        fields = [c.field for c in s.exec(select(ItemCorrection)).all()]
        # formality 3 equals the AI value, notes is not a tag field: exactly one row
        assert item.name == "Brown loafers" and item.notes == "resoled" and fields == ["name"]


@pytest.mark.parametrize("state", ["accepted", "discarded", "merged", "error"])
def test_accept_requires_pending_review(ctx, pieces, state):
    _set_state(ctx, pieces[2], state)
    with Session(ctx.engine) as s, pytest.raises(review.ReviewError) as exc:
        _accept(s, pieces[2])
    assert (exc.value.status, exc.value.code) == (409, "review_conflict")
    with Session(ctx.engine) as s:
        assert s.exec(select(Item)).all() == []


@pytest.mark.parametrize("state", ["pending_review", "error"])
def test_discard_pending_or_error_piece_creates_no_item(ctx, pieces, state):
    _set_state(ctx, pieces[2], state)
    with Session(ctx.engine) as s:
        piece = review.discard_piece(s, s.get(IngestPiece, pieces[2]))
        assert piece.state == "discarded"
        assert s.exec(select(Item)).all() == []


@pytest.mark.parametrize("state", ["accepted", "discarded", "merged"])
def test_discard_finished_piece_rejected(ctx, pieces, state):
    _set_state(ctx, pieces[2], state)
    with Session(ctx.engine) as s, pytest.raises(review.ReviewError):
        review.discard_piece(s, s.get(IngestPiece, pieces[2]))


def test_mark_duplicate_discards_piece_and_creates_no_item(ctx, pieces):
    with Session(ctx.engine) as s:
        item = _accept(s, pieces[3])
        piece = review.mark_duplicate(s, s.get(IngestPiece, pieces[2]), item.id)
        assert (piece.state, piece.duplicate_of_item_id) == ("discarded", item.id)
        assert len(s.exec(select(Item)).all()) == 1
        with pytest.raises(review.ReviewError):  # a finished piece cannot be reviewed again
            _accept(s, pieces[2])


def test_mark_duplicate_of_unknown_item_is_rejected_and_piece_unchanged(ctx, pieces):
    with Session(ctx.engine) as s, pytest.raises(AppError):
        review.mark_duplicate(s, s.get(IngestPiece, pieces[2]), 9999)
    with Session(ctx.engine) as s:
        assert s.get(IngestPiece, pieces[2]).state == "pending_review"


def test_mark_duplicate_requires_pending_review(ctx, pieces):
    with Session(ctx.engine) as s:
        item = _accept(s, pieces[3])
        _accept(s, pieces[2])
        with pytest.raises(review.ReviewError):
            review.mark_duplicate(s, s.get(IngestPiece, pieces[2]), item.id)


@pytest.mark.parametrize(
    ("primary", "others", "name", "category"),
    [
        (0, [1], "Kurta", "ethnic"),
        (1, [0], "Pyjama", "ethnic"),  # the primary's tags win, but an ethnic member makes the merge ethnic
        (1, [2], "Pyjama", "bottom"),
    ],
    ids=["ethnic_primary", "ethnic_other", "no_ethnic"],
)
def test_merge_same_photo_unions_bbox_recuts_and_prefers_ethnic(ctx, pieces, primary, others, name, category):
    with Session(ctx.engine) as s:
        group = [s.get(IngestPiece, pieces[i]) for i in [primary, *others]]
        expected_box = images.union_bbox([p.bbox for p in group])
        old_files = [rel for p in group for rel in (p.cutout_path, p.thumb_path)]

        merged = review.merge_pieces(ctx, s, group[0], [pieces[i] for i in others])

        assert merged.id == pieces[primary] and merged.state == "pending_review"
        assert (merged.proposed_tags["name"], merged.proposed_tags["category"]) == (name, category)
        assert merged.bbox == pytest.approx(expected_box)
        assert (ctx.data_dir / merged.cutout_path).exists() and (ctx.data_dir / merged.thumb_path).exists()
        assert all(not (ctx.data_dir / rel).exists() for rel in old_files)  # replaced files do not leak
        assert [s.get(IngestPiece, pieces[i]).state for i in others] == ["merged"] * len(others)


def test_merge_three_pieces_marks_both_others_merged(ctx, pieces):
    with Session(ctx.engine) as s:
        review.merge_pieces(ctx, s, s.get(IngestPiece, pieces[0]), [pieces[1], pieces[2]])
        assert [s.get(IngestPiece, i).state for i in pieces[:3]] == ["pending_review", "merged", "merged"]


def test_merge_across_photos_rejected_and_nothing_changes(ctx, pieces):
    with Session(ctx.engine) as s:
        before = [s.get(IngestPiece, i).state for i in pieces]
        with pytest.raises(review.ReviewError, match="same photo"):
            review.merge_pieces(ctx, s, s.get(IngestPiece, pieces[0]), [pieces[1], pieces[3]])
        s.rollback()
        assert [s.get(IngestPiece, i).state for i in pieces] == before


@pytest.mark.parametrize("others", [[], "self"], ids=["no_others", "only_itself"])
def test_merge_needs_another_piece(ctx, pieces, others):
    with Session(ctx.engine) as s, pytest.raises(review.ReviewError):
        review.merge_pieces(ctx, s, s.get(IngestPiece, pieces[0]), [pieces[0]] if others == "self" else others)


def test_merge_with_unknown_piece_is_rejected(ctx, pieces):
    with Session(ctx.engine) as s, pytest.raises(AppError):
        review.merge_pieces(ctx, s, s.get(IngestPiece, pieces[0]), [9999])


@pytest.mark.parametrize("which", ["primary", "other"])
def test_merge_requires_pending_pieces(ctx, pieces, which):
    with Session(ctx.engine) as s:
        _accept(s, pieces[0] if which == "primary" else pieces[1])
        with pytest.raises(review.ReviewError):
            review.merge_pieces(ctx, s, s.get(IngestPiece, pieces[0]), [pieces[1]])


# --- list_items (AC-27) ---


def _wardrobe(s):
    s.add_all(
        [
            Item(
                user_id=OWNER_ID,
                name="Navy polo",
                category="top",
                colors=[{"hex": "#1F2A44", "name": "navy", "role": "primary"}],
                seasons=["summer"],
                tags=["office"],
                formality=2,
            ),
            Item(user_id=OWNER_ID, name="Old tee", category="top", status="retired"),
            Item(user_id=OWNER_ID, name="Chinos", category="bottom", formality=3, seasons=["winter"]),
            Item(
                user_id=OWNER_ID,
                name="Silk kurta",
                category="ethnic",
                formality=4,
                brand="Manyavar",
                seasons=["winter", "autumn"],
                colors=[{"hex": "#800020", "name": "maroon", "role": "primary"}],
            ),
        ]
    )
    s.commit()


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({}, ["Chinos", "Navy polo", "Silk kurta"]),  # retired items are hidden by default
        ({"category": "top"}, ["Navy polo"]),
        ({"color": "NAVY"}, ["Navy polo"]),
        ({"season": "winter"}, ["Chinos", "Silk kurta"]),
        ({"formality_min": 3}, ["Chinos", "Silk kurta"]),
        ({"formality_max": 2}, ["Navy polo"]),
        ({"formality_min": 3, "formality_max": 3}, ["Chinos"]),
        ({"q": "office"}, ["Navy polo"]),
        ({"q": "MANYAVAR"}, ["Silk kurta"]),
        ({"q": "chin"}, ["Chinos"]),
        ({"status": "retired"}, ["Old tee"]),
        ({"season": "winter", "category": "ethnic"}, ["Silk kurta"]),
        ({"category": "footwear"}, []),
    ],
    ids=lambda v: str(v) if isinstance(v, dict) else None,
)
def test_list_items_filters(engine, filters, expected):
    with Session(engine) as s:
        _wardrobe(s)
        assert sorted(i.name for i in items.list_items(OWNER_ID, s, **filters)) == expected


def test_list_items_newest_first(engine):
    now = datetime.now(UTC)
    with Session(engine) as s:
        for age_days, name in [(2, "oldest"), (0, "newest"), (1, "middle")]:
            s.add(Item(user_id=OWNER_ID, name=name, category="top", created_at=now - timedelta(days=age_days)))
        s.commit()
        assert [i.name for i in items.list_items(OWNER_ID, s)] == ["newest", "middle", "oldest"]


# --- update_item and corrections (AC-28) ---


def test_update_item_corrections_only_on_real_changes(ctx, pieces):
    with Session(ctx.engine) as s:
        item = _accept(s, pieces[2])
        items.update_item(s, item, {"category": "accessory"})
        items.update_item(s, item, {"category": "accessory"})  # unchanged: nothing
        items.update_item(s, item, {"notes": "resoled"})  # not a tag field: nothing
        s.commit()
        [c] = s.exec(select(ItemCorrection)).all()
        assert (c.item_id, c.field, c.ai_value, c.user_value) == (item.id, "category", "footwear", "accessory")


def test_setting_a_field_back_to_the_ai_value_records_nothing(ctx, pieces):
    with Session(ctx.engine) as s:
        item = _accept(s, pieces[2])
        items.update_item(s, item, {"category": "accessory"})
        items.update_item(s, item, {"category": "footwear"})
        s.commit()
        assert len(s.exec(select(ItemCorrection)).all()) == 1  # no second row for the revert
        assert item.category == "footwear"


def test_update_item_records_list_valued_correction(ctx, pieces):
    new_colors = [{"hex": "#000000", "name": "black", "role": "primary"}]
    with Session(ctx.engine) as s:
        item = _accept(s, pieces[2])
        items.update_item(s, item, {"colors": new_colors})
        s.commit()
        [c] = s.exec(select(ItemCorrection)).all()
        assert (c.field, c.user_value) == ("colors", new_colors) and c.ai_value != new_colors


def test_seed_item_without_ai_tags_records_nothing(engine):
    with Session(engine) as s:
        it = Item(user_id=OWNER_ID, name="Seed", category="top")
        s.add(it)
        s.commit()
        items.update_item(s, it, {"name": "Renamed"})
        s.commit()
        assert it.name == "Renamed" and s.exec(select(ItemCorrection)).all() == []


def test_update_item_does_not_commit_and_bumps_updated_at(engine):
    with Session(engine) as s:
        it = Item(user_id=OWNER_ID, name="Tee", category="top")
        s.add(it)
        s.commit()
        before = it.updated_at
        items.update_item(s, it, {"name": "Renamed"})
        assert it.updated_at != before
        s.rollback()
        s.refresh(it)
        assert it.name == "Tee"


def test_retired_item_leaves_the_default_list(engine):
    with Session(engine) as s:
        it = Item(user_id=OWNER_ID, name="Tee", category="top")
        s.add(it)
        s.commit()
        items.update_item(s, it, {"status": "retired"})
        s.commit()
        assert items.list_items(OWNER_ID, s) == [] and [
            i.name for i in items.list_items(OWNER_ID, s, status="retired")
        ] == ["Tee"]


# --- hard delete (AC-29, AC-47, D14) ---
# Photo a (pieces 0, 1, 2) and photo b (piece 3) come from the `pieces` fixture.


def _file(ctx, rel):
    return ctx.data_dir / rel


def _photo_files(ctx, s, photo_id):
    """Every file the photo owns: original, normalized, and all its pieces' cutouts and thumbs."""
    photo = s.get(Photo, photo_id)
    rels = [photo.original_path, photo.normalized_path]
    for p in s.exec(select(IngestPiece).where(IngestPiece.photo_id == photo_id)).all():
        rels += [p.cutout_path, p.thumb_path]
    return [r for r in rels if r]


def test_hard_delete_removes_item_corrections_files_and_nulls_references(ctx, pieces):
    with Session(ctx.engine) as s:
        item = _accept(s, pieces[2])
        items.update_item(s, item, {"name": "x"})
        s.commit()
        review.mark_duplicate(s, s.get(IngestPiece, pieces[1]), item.id)  # discarded piece pointing at the item
        flagged = s.get(IngestPiece, pieces[0])  # pending piece flagged as a likely duplicate of the item
        flagged.duplicate_of_item_id, flagged.dup_reason = item.id, "looks like 'Shoes'"
        s.commit()
        item_id, cut, thumb = item.id, _file(ctx, item.cutout_path), _file(ctx, item.thumb_path)
        assert cut.exists() and thumb.exists()

        items.hard_delete(s, item, ctx.files)

        assert not cut.exists() and not thumb.exists()
        assert s.get(Item, item_id) is None
        assert s.exec(select(ItemCorrection)).all() == []
        accepted = s.get(IngestPiece, pieces[2])
        assert (accepted.item_id, accepted.cutout_path, accepted.thumb_path) == (None, None, None)
        pending, discarded = s.get(IngestPiece, pieces[0]), s.get(IngestPiece, pieces[1])
        assert pending.state == "pending_review" and pending.duplicate_of_item_id is None
        assert pending.dup_reason == "looks like 'Shoes'"  # the warning text survives
        assert discarded.state == "discarded" and discarded.duplicate_of_item_id is None


def test_hard_delete_last_item_removes_photo_pieces_and_all_files(ctx, pieces):
    with Session(ctx.engine) as s:
        first, second = _accept(s, pieces[0]), _accept(s, pieces[2])
        review.discard_piece(s, s.get(IngestPiece, pieces[1]))
        photo_id, first_files = first.photo_id, [first.cutout_path, first.thumb_path]
        owned = _photo_files(ctx, s, photo_id)
        assert all(_file(ctx, r).exists() for r in owned)
        other_photo = s.get(IngestPiece, pieces[3]).photo_id
        other_files = _photo_files(ctx, s, other_photo)

        items.hard_delete(s, first, ctx.files)  # a sibling item remains: nothing is orphaned yet
        assert s.get(Photo, photo_id) is not None
        assert all(_file(ctx, r).exists() for r in owned if r not in first_files)

        items.hard_delete(s, second, ctx.files)

        assert s.get(Photo, photo_id) is None
        assert s.exec(select(IngestPiece).where(IngestPiece.photo_id == photo_id)).all() == []
        assert all(not _file(ctx, r).exists() for r in owned)  # original, normalized, discarded piece's files too
        assert s.get(Photo, other_photo) is not None and all(_file(ctx, r).exists() for r in other_files)
        assert len(s.exec(select(IngestJob)).all()) == 1  # the job row stays as history


def test_hard_delete_keeps_photo_with_sibling_item(ctx, pieces):
    with Session(ctx.engine) as s:
        doomed, sibling = _accept(s, pieces[0]), _accept(s, pieces[2])
        photo_id, doomed_files = doomed.photo_id, [doomed.cutout_path, doomed.thumb_path]
        keep = _photo_files(ctx, s, photo_id)
        keep = [r for r in keep if r not in doomed_files]

        items.hard_delete(s, doomed, ctx.files)

        assert s.get(Photo, photo_id) is not None and s.get(Item, sibling.id) is not None
        assert all(_file(ctx, r).exists() for r in keep)
        assert all(not _file(ctx, r).exists() for r in doomed_files)
        gone = s.get(IngestPiece, pieces[0])
        assert (gone.item_id, gone.cutout_path, gone.thumb_path) == (None, None, None)


def test_hard_delete_keeps_photo_with_retired_sibling_item(ctx, pieces):
    with Session(ctx.engine) as s:
        doomed, sibling = _accept(s, pieces[0]), _accept(s, pieces[2])
        items.update_item(s, sibling, {"status": "retired"})
        s.commit()
        photo_id = doomed.photo_id

        items.hard_delete(s, doomed, ctx.files)

        assert s.get(Photo, photo_id) is not None  # an item of any status keeps the photo


@pytest.mark.parametrize("other_state", ["pending_review", "error"])
def test_hard_delete_keeps_photo_with_unfinished_piece(ctx, pieces, other_state):
    _set_state(ctx, pieces[0], other_state)
    with Session(ctx.engine) as s:
        doomed = _accept(s, pieces[2])
        review.discard_piece(s, s.get(IngestPiece, pieces[1]))
        photo_id = doomed.photo_id
        unfinished = s.get(IngestPiece, pieces[0])
        files = [unfinished.cutout_path, unfinished.thumb_path, s.get(Photo, photo_id).original_path]

        items.hard_delete(s, doomed, ctx.files)

        assert s.get(Photo, photo_id) is not None
        assert s.get(IngestPiece, pieces[0]).state == other_state
        assert all(_file(ctx, r).exists() for r in files)


def test_hard_delete_seed_item_without_photo_or_files(engine, tmp_path):
    with Session(engine) as s:
        it = Item(
            user_id=OWNER_ID,
            name="Seed",
            category="top",
            cutout_path=f"images/{OWNER_ID}/cutout/gone.png",
            thumb_path=f"images/{OWNER_ID}/thumb/gone.webp",
        )
        s.add(it)
        s.commit()
        item_id = it.id

        items.hard_delete(s, it, LocalStore(tmp_path))  # files already missing, no photo: no error

        assert s.get(Item, item_id) is None


def _spy_delete(monkeypatch, engine, item_id, fail_for=None):
    """Replace LocalStore.delete; record whether the item row was already gone from the DB at each call."""
    real, seen = LocalStore.delete, []

    def delete(self, key):
        with Session(engine) as other:
            seen.append(other.get(Item, item_id) is None)
        if fail_for and key.rsplit("/", 1)[-1] == fail_for:
            raise PermissionError("locked by another process")
        real(self, key)

    monkeypatch.setattr(LocalStore, "delete", delete)
    return seen


def test_hard_delete_commits_before_deleting_files(ctx, pieces, monkeypatch):
    with Session(ctx.engine) as s:
        item = _accept(s, pieces[2])
        item_id = item.id
        with monkeypatch.context() as m:
            seen = _spy_delete(m, ctx.engine, item_id)
            items.hard_delete(s, item, ctx.files)
    assert seen and all(seen)


def test_hard_delete_survives_a_file_that_will_not_go_and_logs_the_class_only(ctx, pieces, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    with Session(ctx.engine) as s:
        item = _accept(s, pieces[2])
        item_id, cut_name, thumb = item.id, Path(item.cutout_path).name, _file(ctx, item.thumb_path)
        with monkeypatch.context() as m:
            _spy_delete(m, ctx.engine, item_id, fail_for=cut_name)
            items.hard_delete(s, item, ctx.files)  # does not raise: the sweep removes the file later
        assert s.get(Item, item_id) is None
    assert not thumb.exists()  # the failure of one file does not stop the others
    [event] = [msg for msg in caplog.messages if "event=files_delete_failed" in msg]
    assert "error=PermissionError" in event
    assert cut_name not in caplog.text and "images/" not in caplog.text


def test_hard_delete_with_the_store_down_still_removes_the_row_and_keeps_going(ctx, pieces, monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    def down(self, key):
        raise StoreUnavailableError

    with Session(ctx.engine) as s:
        item = _accept(s, pieces[2])
        item_id, cut = item.id, _file(ctx, item.cutout_path)
        monkeypatch.setattr(LocalStore, "delete", down)
        items.hard_delete(s, item, ctx.files)
        assert s.get(Item, item_id) is None
    assert cut.exists()  # left for the sweep
    assert any("event=files_delete_failed" in m and "StoreUnavailableError" in m for m in caplog.messages)
