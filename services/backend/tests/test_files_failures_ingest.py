"""AC-16 for ingest: a picture store that is down, refusing or gone never leaves a row that names a picture that was
not stored, and never leaves a picture no row names (LLD 4.4: bytes before the row, compensation, delete after)."""

import io
import logging

import pytest
from botocore.exceptions import EndpointConnectionError
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from kyapehnu.agent.llm.base import LLMUnavailableError
from kyapehnu.core.config import Settings
from kyapehnu.files.errors import StoreUnavailableError
from kyapehnu.files.s3 import S3Store
from kyapehnu.wardrobe.ingest import pipeline, review
from kyapehnu.wardrobe.models import IngestJob, IngestPiece, Photo, PhotoStatus, PieceState
from tests.conftest import TEST_BUCKET, noise_image_bytes, sign_in
from tests.fakes import OWNER_ID, analysis, ensure_owner, garment
from tests.ingest_store_helpers import (
    bucket_keys,
    fail_nth_commit,
    make_store_ctx,
    pieces_of,
    put_fails_after,
)

UNAVAILABLE = "Pictures are unavailable right now. Retry shortly."
OUTAGES = ["NoSuchBucket", "AccessDenied", EndpointConnectionError(endpoint_url="https://s3.test.amazonaws.com")]


@pytest.fixture(autouse=True)
def _owner(engine):
    with Session(engine) as s:
        ensure_owner(s)
        s.commit()


@pytest.fixture
def make_ctx(engine, s3_store, tmp_path):
    return lambda replies=(): make_store_ctx(engine, s3_store, tmp_path, replies)


def _upload(ctx, *blobs) -> int:
    files = [(f"p{i}.jpg", io.BytesIO(b)) for i, b in enumerate(blobs)]
    with Session(ctx.engine) as s:
        return pipeline.create_upload_job(OWNER_ID, s, ctx.files, files).id


def _photo(ctx) -> Photo:
    with Session(ctx.engine) as s:
        return s.exec(select(Photo)).one()


def _count(engine, model) -> int:
    with Session(engine) as s:
        return len(s.exec(select(model)).all())


# --- upload ---------------------------------------------------------------------------------------------------------


def _post_one(client, seed=1):
    return client.post("/api/ingest", files=[("files", (f"p{seed}.jpg", noise_image_bytes(seed), "image/jpeg"))])


def test_an_upload_while_s3_is_down_is_a_503_and_leaves_no_photo_row(s3_app, s3, s3_down):
    _, client = sign_in(s3_app, "sub-a")

    r = _post_one(client)

    assert r.status_code == 503
    assert (r.json()["code"], r.json()["detail"]) == ("files_unavailable", UNAVAILABLE)
    assert r.headers["retry-after"] == "1"
    engine = s3_app.state.ctx.engine
    assert (_count(engine, IngestJob), _count(engine, Photo)) == (0, 0)
    s3_down.heal()
    assert bucket_keys(s3) == []


@pytest.mark.parametrize("outage", OUTAGES, ids=["no_such_bucket", "access_denied", "unreachable"])
def test_an_upload_to_a_refusing_or_gone_bucket_is_a_503_and_leaves_no_photo_row(s3_app, s3, s3_fail, outage):
    _, client = sign_in(s3_app, "sub-a")
    s3_fail(outage)

    r = _post_one(client)

    assert (r.status_code, r.json()["code"]) == (503, "files_unavailable")
    assert TEST_BUCKET not in r.text
    engine = s3_app.state.ctx.engine
    assert (_count(engine, IngestJob), _count(engine, Photo)) == (0, 0)
    s3_fail.heal()
    assert bucket_keys(s3) == []


def test_an_outage_after_the_first_original_discards_it_and_creates_no_job(make_ctx, s3, monkeypatch):
    ctx = make_ctx()
    stored = put_fails_after(monkeypatch, 1)

    with pytest.raises(StoreUnavailableError):
        _upload(ctx, noise_image_bytes(1), noise_image_bytes(2))

    assert len(stored) == 1  # the first original was put ...
    assert bucket_keys(s3) == []  # ... and taken back
    assert (_count(ctx.engine, IngestJob), _count(ctx.engine, Photo)) == (0, 0)


def test_a_failed_commit_after_the_puts_discards_every_original_from_the_bucket(make_ctx, s3, monkeypatch, caplog):
    ctx = make_ctx()
    monkeypatch.setattr(pipeline, "_new_job", lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk I/O error")))
    caplog.set_level(logging.INFO)

    with pytest.raises(OSError, match="disk I/O error"):
        _upload(ctx, noise_image_bytes(1), noise_image_bytes(2))

    assert bucket_keys(s3) == []
    assert any("event=upload_compensated" in m and "files=2" in m for m in caplog.messages)


def test_a_failed_delete_while_compensating_does_not_hide_the_first_error(make_ctx, s3, monkeypatch, caplog):
    ctx = make_ctx()
    put_fails_after(monkeypatch, 1)

    def unreachable_delete(self, key):
        raise StoreUnavailableError

    monkeypatch.setattr(S3Store, "delete", unreachable_delete)
    caplog.set_level(logging.WARNING)

    with pytest.raises(StoreUnavailableError):
        _upload(ctx, noise_image_bytes(1), noise_image_bytes(2))

    assert "event=files_delete_failed error=StoreUnavailableError" in caplog.text  # the sweep removes the leftover


# --- process_photo --------------------------------------------------------------------------------------------------


def _assert_failed_with_one_error_piece(ctx, photo_id: int):
    with Session(ctx.engine) as s:
        photo = s.get(Photo, photo_id)
        [piece] = s.exec(select(IngestPiece)).all()
    assert (photo.status, photo.error) == (PhotoStatus.failed, UNAVAILABLE)
    assert (piece.state, piece.error, piece.bbox) == (PieceState.error, UNAVAILABLE, [0.0, 0.0, 1.0, 1.0])
    assert piece.cutout_path is None and piece.thumb_path is None
    return photo


@pytest.mark.parametrize(
    ("allowed", "committed"),
    [
        (0, ["original"]),
        (1, ["original", "normalized"]),
        (2, ["original", "normalized"]),
        (3, ["original", "normalized"]),
    ],
    ids=["normalized_put_fails", "cutout_put_fails", "thumb_put_fails", "second_piece_fails"],
)
def test_an_outage_during_process_photo_gives_an_error_piece_and_leaves_only_committed_keys(
    make_ctx, s3, monkeypatch, allowed, committed
):
    ctx = make_ctx([analysis(garment((0.0, 0.0, 1.0, 0.5)), garment((0.0, 0.5, 1.0, 1.0)))])
    photo_id = _photo_after_upload(ctx)
    put_fails_after(monkeypatch, allowed)

    assert pipeline.process_photo(OWNER_ID, ctx, photo_id) is False

    photo = _assert_failed_with_one_error_piece(ctx, photo_id)
    row_keys = {"original": photo.original_path, "normalized": photo.normalized_path}
    assert bucket_keys(s3) == sorted(row_keys[name] for name in committed)  # a committed key exists, nothing leaks
    assert (photo.normalized_path is not None) == ("normalized" in committed)


def _photo_after_upload(ctx) -> int:
    _upload(ctx, noise_image_bytes(1))
    return _photo(ctx).id


@pytest.mark.parametrize("outage", OUTAGES, ids=["no_such_bucket", "access_denied", "unreachable"])
def test_the_store_going_away_before_the_photo_is_read_gives_an_error_piece_and_a_retry_recovers(
    make_ctx, s3, s3_fail, outage
):
    ctx = make_ctx([analysis(garment())])
    photo_id = _photo_after_upload(ctx)
    s3_fail(outage)

    assert pipeline.process_photo(OWNER_ID, ctx, photo_id) is False

    _assert_failed_with_one_error_piece(ctx, photo_id)
    s3_fail.heal()
    pipeline.retry_photo(OWNER_ID, ctx, photo_id)
    [piece] = pieces_of(ctx.engine)
    assert piece.state == PieceState.pending_review
    assert {piece.cutout_path, piece.thumb_path} <= set(bucket_keys(s3))
    assert _photo(ctx).status == PhotoStatus.done


def test_a_failed_commit_of_the_pieces_discards_the_cutouts_and_keeps_the_committed_originals(
    make_ctx, s3, monkeypatch
):
    ctx = make_ctx([analysis(garment())])
    photo_id = _photo_after_upload(ctx)
    fail_nth_commit(monkeypatch, 2, OperationalError("COMMIT", {}, Exception("database is locked")))

    with pytest.raises(OperationalError):
        pipeline.process_photo(OWNER_ID, ctx, photo_id)

    photo = _photo(ctx)
    assert bucket_keys(s3) == sorted([photo.original_path, photo.normalized_path])
    assert pieces_of(ctx.engine) == []


def test_a_retry_that_fails_to_commit_never_deletes_the_normalized_copy_the_row_already_names(
    make_ctx, s3, monkeypatch
):
    ctx = make_ctx([LLMUnavailableError("down"), analysis(garment())])
    photo_id = _photo_after_upload(ctx)
    pipeline.process_photo(OWNER_ID, ctx, photo_id)  # the first run commits the normalized copy, then the LLM is down
    normalized = _photo(ctx).normalized_path
    assert normalized in bucket_keys(s3)
    fail_nth_commit(monkeypatch, 2, OperationalError("COMMIT", {}, Exception("database is locked")))

    pipeline.retry_photo(OWNER_ID, ctx, photo_id)  # the retry puts the same key again, then its commit fails

    assert _photo(ctx).normalized_path == normalized
    assert normalized in bucket_keys(s3)


def test_an_outage_in_the_worker_still_ends_with_the_job_done_and_the_photo_failed(make_ctx, s3, monkeypatch):
    ctx = make_ctx([analysis(garment())])
    photo_id = _photo_after_upload(ctx)
    put_fails_after(monkeypatch, 0)

    pipeline.run_job(OWNER_ID, ctx, 1)

    with Session(ctx.engine) as s:
        job = s.get(IngestJob, 1)
        assert (job.status, job.processed, job.failed) == ("done", 1, 1)
        assert s.get(Photo, photo_id).status == PhotoStatus.failed


# --- review: merge --------------------------------------------------------------------------------------------------


@pytest.fixture
def two_pieces(make_ctx):
    ctx = make_ctx([analysis(garment((0.0, 0.0, 1.0, 0.5)), garment((0.0, 0.5, 1.0, 1.0)))])
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(1)))
    return ctx, [p.id for p in pieces_of(ctx.engine)]


def _piece_keys(ctx) -> list[str]:
    return sorted(k for p in pieces_of(ctx.engine) for k in (p.cutout_path, p.thumb_path) if k)


def test_a_merge_during_an_outage_is_a_503_and_changes_nothing(two_pieces, s3, monkeypatch):
    ctx, ids = two_pieces
    before = bucket_keys(s3)
    put_fails_after(monkeypatch, 0)

    with Session(ctx.engine) as s, pytest.raises(StoreUnavailableError):
        review.merge_pieces(ctx, s, s.get(IngestPiece, ids[0]), [ids[1]])

    assert bucket_keys(s3) == before
    assert [p.state for p in pieces_of(ctx.engine)] == [PieceState.pending_review] * 2


def test_a_merge_whose_commit_fails_discards_the_new_picture_and_keeps_the_old_ones(two_pieces, s3, monkeypatch):
    ctx, ids = two_pieces
    before = bucket_keys(s3)
    fail_nth_commit(monkeypatch, 1, OperationalError("COMMIT", {}, Exception("database is locked")))

    with Session(ctx.engine) as s, pytest.raises(OperationalError):
        review.merge_pieces(ctx, s, s.get(IngestPiece, ids[0]), [ids[1]])

    assert bucket_keys(s3) == before
    assert _piece_keys(ctx) == [k for k in before if "/original/" not in k]


def test_a_merge_removes_the_old_pictures_after_the_commit_even_when_a_delete_fails(
    two_pieces, s3, monkeypatch, caplog
):
    ctx, ids = two_pieces
    caplog.set_level(logging.WARNING)

    def unreachable_delete(self, key):
        raise StoreUnavailableError

    monkeypatch.setattr(S3Store, "delete", unreachable_delete)

    with Session(ctx.engine) as s:
        merged = review.merge_pieces(ctx, s, s.get(IngestPiece, ids[0]), [ids[1]])

    assert merged.state == PieceState.pending_review  # the row is committed; the sweep removes the leftovers
    assert "event=files_delete_failed error=StoreUnavailableError" in caplog.text


# --- clean pictures -------------------------------------------------------------------------------------------------

OPENAI_KEY = "sk-oai-FAIL-123456"


@pytest.fixture
def openai_ctx(two_pieces, tmp_path):
    from kyapehnu.agent.llm.base import LLMProvider
    from tests.fakes import FakeTransport

    ctx, ids = two_pieces
    ctx.provider = LLMProvider(FakeTransport())
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        config_file=tmp_path / "missing.yaml",
        llm_provider="openai_compatible",
        openai_base_url="https://api.openai.com/v1",
        openai_api_key=OPENAI_KEY,
    )
    return ctx, settings, ids


def _transparent() -> bytes:
    from tests.test_pictures import cutout_with_transparency

    return cutout_with_transparency()


def test_a_clean_picture_during_an_outage_is_a_503_and_changes_nothing(openai_ctx, s3, monkeypatch):
    from kyapehnu.wardrobe import pictures

    ctx, settings, ids = openai_ctx
    ctx.provider.transport.edits.append(_transparent())
    before = bucket_keys(s3)
    put_fails_after(monkeypatch, 0)

    with Session(ctx.engine) as s, pytest.raises(StoreUnavailableError):
        pictures.generate(ctx, settings, s, s.get(IngestPiece, ids[0]))

    assert bucket_keys(s3) == before
    assert all(p.alt_cutout_path is None for p in pieces_of(ctx.engine))


def test_a_clean_picture_whose_thumb_put_fails_leaves_no_half_picture(openai_ctx, s3, monkeypatch):
    from kyapehnu.wardrobe import pictures

    ctx, settings, ids = openai_ctx
    ctx.provider.transport.edits.append(_transparent())
    before = bucket_keys(s3)
    put_fails_after(monkeypatch, 1)

    with Session(ctx.engine) as s, pytest.raises(StoreUnavailableError):
        pictures.generate(ctx, settings, s, s.get(IngestPiece, ids[0]))

    assert bucket_keys(s3) == before  # the cutout that did go in was taken back


def test_a_clean_picture_whose_commit_fails_discards_the_new_files(openai_ctx, s3, monkeypatch):
    from kyapehnu.wardrobe import pictures

    ctx, settings, ids = openai_ctx
    ctx.provider.transport.edits.append(_transparent())
    before = bucket_keys(s3)
    fail_nth_commit(monkeypatch, 1, OperationalError("COMMIT", {}, Exception("database is locked")))

    with Session(ctx.engine) as s, pytest.raises(OperationalError):
        pictures.generate(ctx, settings, s, s.get(IngestPiece, ids[0]))

    assert bucket_keys(s3) == before
    assert all(p.alt_cutout_path is None for p in pieces_of(ctx.engine))


def test_removing_a_clean_picture_works_even_when_the_delete_fails_after_the_commit(
    openai_ctx, s3, monkeypatch, caplog
):
    from kyapehnu.wardrobe import pictures

    ctx, settings, ids = openai_ctx
    ctx.provider.transport.edits.append(_transparent())
    with Session(ctx.engine) as s:
        pictures.generate(ctx, settings, s, s.get(IngestPiece, ids[0]))
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(S3Store, "delete", lambda self, key: (_ for _ in ()).throw(StoreUnavailableError()))

    with Session(ctx.engine) as s:
        piece = s.get(IngestPiece, ids[0])
        pictures.remove(ctx, s, piece)
        assert (piece.alt_cutout_path, piece.alt_thumb_path) == (None, None)  # committed

    assert "event=files_delete_failed error=StoreUnavailableError" in caplog.text
