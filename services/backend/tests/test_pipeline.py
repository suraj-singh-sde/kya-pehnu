"""Ingestion pipeline: job creation (validate first, compensate on failure), worker, failure isolation, dedupe flags."""

import errno
import io
import logging
import os
import re
import shutil
import sys
from pathlib import Path

import pytest
from PIL import Image
from sqlmodel import Session, select

from kyapehnu.agent.llm.base import LLMUnavailableError
from kyapehnu.core.errors import HeicExtraMissingError, InvalidInputError, NotFoundError, UnsupportedFileError
from kyapehnu.wardrobe.ingest import images, pipeline, review, screening
from kyapehnu.wardrobe.ingest.jobs import Worker
from kyapehnu.wardrobe.models import IngestJob, IngestPiece, Item, Photo
from tests.conftest import noise_image_bytes
from tests.fakes import OWNER_ID, analysis, ensure_owner, garment, stored_files
from tests.ingest_store_helpers import make_store_ctx

SECRET_REPLY = "SECRETREPLY123"


@pytest.fixture(autouse=True)
def _owner(engine):
    with Session(engine) as s:
        ensure_owner(s)
        s.commit()


def _upload(ctx, *blobs):
    files = [(f"p{i}.jpg", io.BytesIO(b)) for i, b in enumerate(blobs)]
    with Session(ctx.engine) as s:
        return pipeline.create_upload_job(OWNER_ID, s, ctx.files, files).id


def _pieces(ctx):
    with Session(ctx.engine) as s:
        return s.exec(select(IngestPiece).order_by(IngestPiece.id)).all()


def _photos(ctx):
    with Session(ctx.engine) as s:
        return s.exec(select(Photo).order_by(Photo.id)).all()


def _job(ctx, job_id):
    with Session(ctx.engine) as s:
        return s.get(IngestJob, job_id)


def _assert_nothing_stored(ctx):
    assert stored_files(ctx.data_dir, "original") == []
    with Session(ctx.engine) as s:
        assert s.exec(select(IngestJob)).all() == []
        assert s.exec(select(Photo)).all() == []


def _events(caplog, event):
    return [m for m in caplog.messages if f"event={event}" in m]


def _make_folder(tmp_path, name="photos"):
    folder = tmp_path / name
    folder.mkdir()
    return folder


# --- processing: pieces, cutouts, bbox (AC-16, AC-17) ---


def test_photo_with_two_garments_makes_two_pending_pieces(make_ctx):
    ctx = make_ctx([analysis(garment((0.0, 0.0, 0.5, 1.0)), garment((0.5, 0.0, 1.0, 1.0), category="bottom"))])
    job_id = _upload(ctx, noise_image_bytes(1))

    pipeline.run_job(OWNER_ID, ctx, job_id)

    pieces = _pieces(ctx)
    assert [p.state for p in pieces] == ["pending_review", "pending_review"]
    assert all((ctx.data_dir / p.cutout_path).exists() and (ctx.data_dir / p.thumb_path).exists() for p in pieces)
    assert all(max(Image.open(ctx.data_dir / p.thumb_path).size) <= images.THUMB_SIDE for p in pieces)
    assert pieces[1].proposed_tags["category"] == "bottom"
    job = _job(ctx, job_id)
    assert (job.status, job.total, job.processed, job.failed) == ("done", 1, 1, 0)
    [photo] = _photos(ctx)
    assert photo.status == "done" and photo.photo_type == "flat_lay" and photo.phash
    assert (ctx.data_dir / photo.normalized_path).exists()


def _answer(choice):
    return [{"type": "choice", "name": "shows", "choice": choice, "confidence": 0.9}]


def test_a_photo_that_is_not_clothing_or_is_innerwear_is_turned_away_before_tagging(make_ctx):
    ctx = make_ctx([analysis(garment())])
    fake = ctx.provider.transport
    fake.decisions = [_answer("other"), _answer("innerwear")]
    job_id = _upload(ctx, noise_image_bytes(1), noise_image_bytes(2))

    pipeline.run_job(OWNER_ID, ctx, job_id)

    pieces = _pieces(ctx)
    assert [(p.state, p.error) for p in pieces] == [
        ("rejected", screening.NOT_CLOTHING),
        ("rejected", screening.INNERWEAR),
    ]
    assert fake.calls == []  # neither photo was tagged
    assert len(fake.decide_calls) == 2 and fake.decide_calls[0]["questions"][0]["name"] == "shows"
    assert fake.decide_calls[0]["image"][:3] == b"\xff\xd8\xff"  # the normalized JPEG, the same the tagger would get
    job = _job(ctx, job_id)
    assert (job.status, job.processed, job.failed) == ("done", 2, 2)
    with Session(ctx.engine) as s:  # a rejected piece is in the review queue and can be removed
        assert [p.id for p in review.queue(OWNER_ID, s)] == [p.id for p in pieces]
        review.discard_piece(s, s.get(IngestPiece, pieces[0].id))
        s.commit()
    assert _pieces(ctx)[0].state == "discarded"


def test_a_refusal_or_a_provider_without_a_decision_model_lets_the_photo_through(make_ctx):
    ctx = make_ctx([analysis(garment()), analysis(garment())])
    fake = ctx.provider.transport
    fake.decisions = [[{"type": "refusal", "name": "shows"}]]
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(1)))
    assert [p.state for p in _pieces(ctx)] == ["pending_review"]
    fake.decide_image = None  # a provider that cannot decide: no question asked, straight to tagging
    assert not ctx.provider.can_decide
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(2)))
    assert [p.state for p in _pieces(ctx)] == ["pending_review", "pending_review"]
    assert len(fake.decide_calls) == 1


def test_screening_off_or_failing(make_ctx):
    ctx = make_ctx([analysis(garment()), analysis(garment())])
    fake = ctx.provider.transport
    fake.decisions = [LLMUnavailableError("down")]
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(1)))
    [p] = _pieces(ctx)
    assert p.state == "error" and p.error == "checking the photo failed: down"  # retryable, like a tagging failure
    ctx.prefs.ingest.screen_photos = False
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(2)))
    assert [p.state for p in _pieces(ctx)] == ["error", "pending_review"]
    assert len(fake.decide_calls) == 1


def test_piece_bbox_is_normalized_and_padded(make_ctx):
    # pixel coordinates on an 800x600 photo, with reversed corners: (200,150)-(600,450) = (0.25,0.25)-(0.75,0.75)
    ctx = make_ctx([analysis(garment((600.0, 450.0, 200.0, 150.0)))])
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(1)))

    [piece] = _pieces(ctx)
    pad = ctx.prefs.ingest.bbox_padding * 0.5
    assert piece.bbox == pytest.approx([0.25 - pad, 0.25 - pad, 0.75 + pad, 0.75 + pad])


def test_tiny_bbox_dropped(make_ctx):
    ctx = make_ctx([analysis(garment((0.1, 0.1, 0.9, 0.9)), garment((0.0, 0.0, 0.1, 0.1)))])
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(1)))
    assert len(_pieces(ctx)) == 1


def test_only_tiny_bboxes_is_a_no_garments_error_piece(make_ctx):
    ctx = make_ctx([analysis(garment((0.0, 0.0, 0.1, 0.1)))])
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(1)))
    [p] = _pieces(ctx)
    assert p.state == "error" and p.error == "No garments detected"


def test_worn_photo_skips_rembg(make_ctx, monkeypatch):
    called = []
    monkeypatch.setattr(images, "remove_background", lambda im: called.append(1) or im.convert("RGBA"))
    ctx = make_ctx([analysis(garment(), photo_type="worn")])
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(1)))
    assert called == []


def test_flat_lay_photo_uses_rembg(make_ctx, monkeypatch):
    called = []
    monkeypatch.setattr(images, "remove_background", lambda im: called.append(1) or im.convert("RGBA"))
    ctx = make_ctx([analysis(garment(), photo_type="flat_lay")])
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(1)))
    assert called == [1]


def test_rembg_failure_keeps_plain_crop_and_piece(make_ctx, monkeypatch):
    def boom(_im):
        raise RuntimeError("model missing")

    monkeypatch.setattr(images, "remove_background", boom)
    ctx = make_ctx([analysis(garment())])
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(1)))
    [piece] = _pieces(ctx)
    assert piece.state == "pending_review" and (ctx.data_dir / piece.cutout_path).exists()


# --- failure isolation (AC-18, AC-19, NFR-9) ---


def test_llm_down_and_no_garments_make_error_pieces_while_other_photos_succeed(make_ctx):
    ctx = make_ctx([LLMUnavailableError("no key"), analysis(garment()), analysis()])
    job_id = _upload(ctx, noise_image_bytes(1), noise_image_bytes(2), noise_image_bytes(3))

    pipeline.run_job(OWNER_ID, ctx, job_id)

    first, second, third = _pieces(ctx)
    assert (first.state, second.state, third.state) == ("error", "pending_review", "error")
    assert "no key" in first.error and first.bbox == [0.0, 0.0, 1.0, 1.0]
    assert third.error == "No garments detected"
    job = _job(ctx, job_id)
    assert (job.status, job.total, job.processed, job.failed) == ("done", 3, 3, 2)


def test_retry_recovers_only_that_photo(make_ctx):
    ctx = make_ctx([LLMUnavailableError("no key"), analysis(garment()), analysis(garment(name="Retried"))])
    job_id = _upload(ctx, noise_image_bytes(1), noise_image_bytes(2))
    pipeline.run_job(OWNER_ID, ctx, job_id)
    err, ok = _pieces(ctx)
    assert (err.state, ok.state) == ("error", "pending_review")

    pipeline.retry_photo(OWNER_ID, ctx, err.photo_id)

    pieces = _pieces(ctx)
    assert [p.state for p in pieces] == ["pending_review", "pending_review"]
    assert ok.id in [p.id for p in pieces] and err.id not in [p.id for p in pieces]
    assert len(ctx.provider.transport.calls) == 3  # two photos, then one retry: the other photo is not re-analysed
    assert (_job(ctx, job_id).processed, _job(ctx, job_id).failed) == (2, 0)


def test_retry_of_a_photo_that_is_not_failed_is_skipped_and_logged(make_ctx, caplog):
    ctx = make_ctx([analysis(garment())])
    job_id = _upload(ctx, noise_image_bytes(1))
    pipeline.run_job(OWNER_ID, ctx, job_id)
    [piece] = _pieces(ctx)
    caplog.set_level(logging.INFO)

    pipeline.retry_photo(OWNER_ID, ctx, piece.photo_id)  # a second queued retry arrives after the first one finished

    assert [p.id for p in _pieces(ctx)] == [piece.id]
    assert len(ctx.provider.transport.calls) == 1
    assert len(_events(caplog, "retry_skipped")) == 1


def test_llm_output_error_piece_and_logs_never_carry_the_raw_reply(make_ctx, caplog):
    bad = {"photo_type": SECRET_REPLY, "garments": []}  # invalid twice: the validation error echoes the value
    ctx = make_ctx([bad, bad])
    caplog.set_level(logging.DEBUG)

    job_id = _upload(ctx, noise_image_bytes(1))
    pipeline.run_job(OWNER_ID, ctx, job_id)

    [piece] = _pieces(ctx)
    [photo] = _photos(ctx)
    assert piece.state == "error" and piece.error
    assert SECRET_REPLY not in piece.error
    assert SECRET_REPLY not in (photo.error or "")
    assert SECRET_REPLY not in caplog.text
    [event] = _events(caplog, "photo_failed")
    assert f"photo_id={photo.id}" in event and "error=LLMOutputError" in event


def test_corrupt_image_fails_photo_and_job_continues(make_ctx, caplog):
    ctx = make_ctx([analysis(garment())])
    caplog.set_level(logging.INFO)
    job_id = _upload(ctx, b"garbage", noise_image_bytes(2))

    pipeline.run_job(OWNER_ID, ctx, job_id)

    job = _job(ctx, job_id)
    assert (job.status, job.processed, job.failed) == ("done", 2, 1)
    bad, good = _photos(ctx)
    assert (bad.status, good.status) == ("failed", "done") and bad.error
    assert len([p for p in _pieces(ctx) if p.state == "pending_review"]) == 1
    [event] = _events(caplog, "photo_failed")
    assert f"photo_id={bad.id}" in event and "error=UnidentifiedImageError" in event


def test_photo_over_pixel_limit_fails_and_job_continues(make_ctx, monkeypatch, caplog):
    monkeypatch.setattr(images, "MAX_PIXELS", 800 * 600 - 1)
    ctx = make_ctx([analysis(garment())])
    caplog.set_level(logging.INFO)
    big = noise_image_bytes(1, size=(800, 600))
    small = noise_image_bytes(2, size=(400, 300))
    job_id = _upload(ctx, big, small)

    pipeline.run_job(OWNER_ID, ctx, job_id)

    job = _job(ctx, job_id)
    assert (job.status, job.processed, job.failed) == ("done", 2, 1)
    [too_big, _ok] = _photos(ctx)
    assert too_big.status == "failed" and "too large" in too_big.error
    [event] = _events(caplog, "photo_failed")
    assert "error=ImageTooLargeError" in event


def test_unexpected_exception_fails_photo_not_the_job(make_ctx, monkeypatch, caplog):
    real = pipeline.process_photo
    calls = []

    def flaky(user_id, ctx, photo_id):
        calls.append(photo_id)
        if len(calls) == 1:
            raise RuntimeError("disk exploded SECRETDETAIL")
        return real(user_id, ctx, photo_id)

    monkeypatch.setattr(pipeline, "process_photo", flaky)
    ctx = make_ctx([analysis(garment())])
    caplog.set_level(logging.INFO)
    job_id = _upload(ctx, noise_image_bytes(1), noise_image_bytes(2))

    pipeline.run_job(OWNER_ID, ctx, job_id)

    job = _job(ctx, job_id)
    assert (job.status, job.processed, job.failed) == ("done", 2, 1)
    first, second = _photos(ctx)
    assert (first.status, second.status) == ("failed", "done")
    [event] = _events(caplog, "photo_failed")
    assert f"photo_id={first.id}" in event and "error=RuntimeError" in event
    assert "SECRETDETAIL" not in event


@pytest.mark.parametrize("operation", [pipeline.run_job, pipeline.retry_photo])
def test_unknown_id_raises_not_found(make_ctx, operation):
    ctx = make_ctx([])
    with pytest.raises(NotFoundError):
        operation(OWNER_ID, ctx, 9999)


# --- job lifecycle logs (D17) ---


def test_job_logs_started_and_done_with_counts(make_ctx, caplog):
    ctx = make_ctx([analysis(garment())])
    caplog.set_level(logging.INFO)
    job_id = _upload(ctx, b"garbage", noise_image_bytes(2))

    pipeline.run_job(OWNER_ID, ctx, job_id)

    [started] = _events(caplog, "job_started")
    [done] = _events(caplog, "job_done")
    assert f"job_id={job_id}" in started
    assert f"job_id={job_id}" in done and "processed=2" in done and "failed=1" in done


def test_folder_ingest_logs_no_paths_above_debug(make_ctx, tmp_path, caplog):
    ctx = make_ctx([analysis(garment())])
    src = _make_folder(tmp_path, "my private photos")
    (src / "shirt.jpg").write_bytes(noise_image_bytes(1))
    caplog.set_level(logging.INFO)

    with Session(ctx.engine) as s:
        job = pipeline.create_folder_job(OWNER_ID, s, ctx.files, src)
    pipeline.run_job(OWNER_ID, ctx, job.id)

    assert str(tmp_path) not in caplog.text
    assert "my private photos" not in caplog.text


# --- resume (AC-20) ---


def test_worker_resumes_only_pending_photos(make_ctx):
    ctx = make_ctx([analysis(garment())])  # exactly one LLM reply available
    job_id = _upload(ctx, noise_image_bytes(1), noise_image_bytes(2))
    with Session(ctx.engine) as s:  # simulate a crash after the first photo finished
        first = s.exec(select(Photo).where(Photo.job_id == job_id).order_by(Photo.id)).first()
        first.status = "done"
        s.get(IngestJob, job_id).status = "running"
        s.commit()

    w = Worker(ctx)
    w.start()
    w.drain()
    w.stop()

    job = _job(ctx, job_id)
    assert (job.status, job.processed, job.failed) == ("done", 2, 0)
    assert len(_pieces(ctx)) == 1


def test_worker_start_picks_up_queued_job(make_ctx):
    ctx = make_ctx([analysis(garment())])
    job_id = _upload(ctx, noise_image_bytes(1))
    assert _job(ctx, job_id).status == "queued"

    w = Worker(ctx)
    w.start()
    w.drain()
    w.stop()

    assert _job(ctx, job_id).status == "done" and len(_pieces(ctx)) == 1


def test_run_job_twice_does_not_reprocess(make_ctx):
    ctx = make_ctx([analysis(garment())])
    job_id = _upload(ctx, noise_image_bytes(1))
    pipeline.run_job(OWNER_ID, ctx, job_id)

    pipeline.run_job(OWNER_ID, ctx, job_id)  # a second LLM call would raise: the fake has no more replies

    assert len(_pieces(ctx)) == 1 and _job(ctx, job_id).status == "done"


def test_worker_survives_a_failing_task_and_runs_the_next(make_ctx):
    ctx = make_ctx([analysis(garment())])
    job_id = _upload(ctx, noise_image_bytes(1))
    w = Worker(ctx)
    w.start()
    try:
        w.submit_job(9999)  # unknown id raises inside the worker thread
        w.submit_job(job_id)
        w.drain()
    finally:
        w.stop()
    assert _job(ctx, job_id).status == "done"


def test_worker_retry_task_recovers_photo(make_ctx):
    ctx = make_ctx([LLMUnavailableError("no key"), analysis(garment())])
    job_id = _upload(ctx, noise_image_bytes(1))
    pipeline.run_job(OWNER_ID, ctx, job_id)
    [err] = _pieces(ctx)
    w = Worker(ctx)
    w.start()
    try:
        w.submit_retry(err.photo_id)
        w.drain()
    finally:
        w.stop()
    assert [p.state for p in _pieces(ctx)] == ["pending_review"]


def test_worker_start_reprocesses_photo_left_pending_by_a_crash_during_retry(make_ctx, monkeypatch):
    class Crash(BaseException):  # a process death: not caught by `except Exception`
        pass

    ctx = make_ctx([LLMUnavailableError("no key"), analysis(garment())])
    job_id = _upload(ctx, noise_image_bytes(1))
    pipeline.run_job(OWNER_ID, ctx, job_id)
    [err] = _pieces(ctx)
    real_process_photo = pipeline.process_photo

    def crash(*_args, **_kwargs):
        raise Crash

    monkeypatch.setattr(pipeline, "process_photo", crash)
    with pytest.raises(Crash):
        pipeline.retry_photo(OWNER_ID, ctx, err.photo_id)  # dies after its first commit, before processing
    monkeypatch.setattr(pipeline, "process_photo", real_process_photo)
    assert [p.status for p in _photos(ctx)] == ["pending"] and _pieces(ctx) == []  # the stuck state

    w = Worker(ctx)  # restart
    w.start()
    try:
        w.drain()
    finally:
        w.stop()

    assert [p.status for p in _photos(ctx)] == ["done"]
    assert [p.state for p in _pieces(ctx)] == ["pending_review"]
    job = _job(ctx, job_id)
    assert (job.status, job.total, job.processed, job.failed) == ("done", 1, 1, 0)


# --- duplicates are flagged, never merged (AC-21, AC-22, AC-23) ---


def test_reupload_flagged_and_still_reviewable(make_ctx):
    ctx = make_ctx([analysis(garment()), analysis(garment())])
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(7), noise_image_bytes(7)))
    first, second = _pieces(ctx)
    assert first.dup_reason is None
    assert second.dup_reason == "same photo uploaded before"
    assert second.state == "pending_review" and second.duplicate_of_item_id is None


@pytest.mark.parametrize(
    ("item_status", "flagged"), [("available", True), ("retired", False)], ids=["active_item", "retired_item"]
)
def test_near_identical_cutout_flags_existing_item_without_llm_call(make_ctx, item_status, flagged):
    ctx = make_ctx([analysis(garment()), analysis(garment())])
    blob = noise_image_bytes(5)
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, blob))
    with Session(ctx.engine) as s:
        item = review.accept_piece(s, s.exec(select(IngestPiece)).one())
        item.status = item_status
        s.commit()
        item_id = item.id

    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, blob))

    second = _pieces(ctx)[-1]
    assert second.state == "pending_review"  # flagged only: never auto-discarded or merged
    if flagged:
        assert (second.duplicate_of_item_id, second.dup_reason) == (item_id, "near-identical image")
    else:
        assert second.duplicate_of_item_id is None and second.dup_reason == "same photo uploaded before"
    assert len(ctx.provider.transport.calls) == 2  # no dedupe LLM call
    with Session(ctx.engine) as s:
        assert len(s.exec(select(Item)).all()) == 1


@pytest.mark.parametrize(
    ("verdict", "flagged", "skip_logged"),
    [
        ({"match_index": 1, "confidence": 0.9}, True, False),
        ({"match_index": 1, "confidence": 0.69}, False, False),
        (LLMUnavailableError("down"), False, True),
    ],
    ids=["confident", "below_threshold", "dedupe_call_fails"],
)
def test_llm_dedupe_verdict_decides_the_flag(make_ctx, caplog, verdict, flagged, skip_logged):
    ctx = make_ctx([analysis(garment()), verdict])
    thumb = images.save_cutout(Image.new("RGBA", (40, 40), "white"), ctx.files, OWNER_ID, "candidate")[1]
    with Session(ctx.engine) as s:  # similar colour/category/pattern, but no phash: only the LLM can match it
        item = Item(
            user_id=OWNER_ID,
            name="Cream shirt",
            category="top",
            pattern="solid",
            thumb_path=thumb,
            colors=[{"hex": "#fafafa", "name": "white", "role": "primary"}],
        )
        s.add(item)
        s.commit()
        item_id = item.id
    caplog.set_level(logging.INFO)

    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(1)))

    [piece] = _pieces(ctx)
    assert piece.state == "pending_review"
    assert piece.duplicate_of_item_id == (item_id if flagged else None)
    assert piece.dup_reason == ("looks like 'Cream shirt'" if flagged else None)
    assert bool(_events(caplog, "dedupe_skipped")) is skip_logged


# --- upload job creation (AC-12, AC-13, D6) ---


def test_upload_creates_queued_job_and_streams_originals_to_disk(make_ctx):
    ctx = make_ctx([])
    blobs = [noise_image_bytes(1), noise_image_bytes(2)]
    files = [("Shirt.JPG", io.BytesIO(blobs[0])), ("b.png", io.BytesIO(blobs[1]))]

    with Session(ctx.engine) as s:
        job = pipeline.create_upload_job(OWNER_ID, s, ctx.files, files)

    assert (job.source, job.status, job.total) == ("upload", "queued", 2)
    photos = _photos(ctx)
    assert [p.status for p in photos] == ["pending", "pending"]
    assert re.fullmatch(r"images/1/original/[0-9a-f]{32}\.jpg", photos[0].original_path)
    assert photos[1].original_path.endswith(".png")
    assert [(ctx.data_dir / p.original_path).read_bytes() for p in photos] == blobs


@pytest.mark.parametrize(
    ("files", "error", "code"),
    [
        ([], InvalidInputError, "invalid_input"),
        ([("notes.txt", b"hi")], UnsupportedFileError, "unsupported_file"),
        ([("noextension", b"hi")], UnsupportedFileError, "unsupported_file"),
        ([("ok.jpg", b"x"), ("notes.txt", b"hi")], UnsupportedFileError, "unsupported_file"),
    ],
    ids=["no_files", "unsupported", "no_suffix", "one_bad_among_good"],
)
def test_upload_rejected_before_anything_is_written(make_ctx, files, error, code):
    ctx = make_ctx([])
    streams = [(name, io.BytesIO(data)) for name, data in files]

    with Session(ctx.engine) as s, pytest.raises(error) as exc:
        pipeline.create_upload_job(OWNER_ID, s, ctx.files, streams)

    assert exc.value.code == code and isinstance(exc.value, ValueError)
    _assert_nothing_stored(ctx)


def test_unsupported_upload_message_names_the_file(make_ctx):
    ctx = make_ctx([])
    with Session(ctx.engine) as s, pytest.raises(UnsupportedFileError, match="notes.txt"):
        pipeline.create_upload_job(OWNER_ID, s, ctx.files, [("notes.txt", io.BytesIO(b"hi"))])


def test_upload_heic_without_extra_rejected_nothing_stored(make_ctx, monkeypatch):
    monkeypatch.setattr(images, "HEIC_AVAILABLE", False)
    ctx = make_ctx([])
    files = [("ok.jpg", io.BytesIO(noise_image_bytes(1))), ("IMG_1.HEIC", io.BytesIO(b"x"))]

    with Session(ctx.engine) as s, pytest.raises(HeicExtraMissingError, match=re.escape("Export them as JPEG")) as exc:
        pipeline.create_upload_job(OWNER_ID, s, ctx.files, files)

    assert exc.value.code == "heic_extra_missing"
    _assert_nothing_stored(ctx)


def test_upload_accepts_heic_when_extra_present(make_ctx, monkeypatch):
    monkeypatch.setattr(images, "HEIC_AVAILABLE", True)
    ctx = make_ctx([])
    with Session(ctx.engine) as s:
        job = pipeline.create_upload_job(OWNER_ID, s, ctx.files, [("IMG_1.HEIC", io.BytesIO(b"x"))])
    assert job.total == 1


def test_upload_compensates_when_commit_fails(make_ctx, monkeypatch, caplog):
    def boom(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(pipeline, "_new_job", boom)
    ctx = make_ctx([])
    caplog.set_level(logging.INFO)
    files = [("a.jpg", io.BytesIO(noise_image_bytes(1))), ("b.jpg", io.BytesIO(noise_image_bytes(2)))]

    with Session(ctx.engine) as s, pytest.raises(OSError) as exc:
        pipeline.create_upload_job(OWNER_ID, s, ctx.files, files)

    assert exc.value.errno == errno.ENOSPC  # re-raised unchanged, so the API can answer 507
    assert stored_files(ctx.data_dir) == []
    [event] = _events(caplog, "upload_compensated")
    assert "files=2" in event


def test_upload_compensates_partial_copy(make_ctx, monkeypatch):
    def partial_copy(_src, dst, *_a, **_k):
        dst.write(b"partial")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copyfileobj", partial_copy)
    ctx = make_ctx([])

    with Session(ctx.engine) as s, pytest.raises(OSError) as exc:
        pipeline.create_upload_job(OWNER_ID, s, ctx.files, [("a.jpg", io.BytesIO(b"data"))])

    assert exc.value.errno == errno.ENOSPC
    _assert_nothing_stored(ctx)


# --- folder job creation (AC-14, AC-43, D7) ---


def test_folder_job_copies_top_level_images_only(make_ctx, tmp_path):
    ctx = make_ctx([])
    src = _make_folder(tmp_path, "my photos")
    (src / "Shirt ä.JPG").write_bytes(noise_image_bytes(1))
    (src / "readme.txt").write_text("x")
    (src / "sub").mkdir()
    (src / "sub" / "nested.jpg").write_bytes(noise_image_bytes(2))

    with Session(ctx.engine) as s:
        job = pipeline.create_folder_job(OWNER_ID, s, ctx.files, src)
        assert job.total == 1 and job.source == "folder"
        [photo] = s.exec(select(Photo)).all()
    assert (ctx.data_dir / photo.original_path).read_bytes() == (src / "Shirt ä.JPG").read_bytes()
    assert photo.original_path.endswith(".jpg")
    assert (src / "Shirt ä.JPG").exists()  # copied, not moved


def test_folder_job_skips_symlinks(make_ctx, tmp_path):
    ctx = make_ctx([])
    src = _make_folder(tmp_path)
    (src / "real.jpg").write_bytes(noise_image_bytes(1))
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(noise_image_bytes(2))
    try:
        (src / "link.jpg").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported here")

    with Session(ctx.engine) as s:
        job = pipeline.create_folder_job(OWNER_ID, s, ctx.files, src)

    assert job.total == 1


@pytest.mark.parametrize("kind", ["relative", "missing", "file", "empty", "only_non_images"])
def test_folder_job_invalid_inputs(make_ctx, tmp_path, kind):
    ctx = make_ctx([])
    folder = {
        "relative": Path("relative/path"),
        "missing": tmp_path / "does-not-exist",
        "file": tmp_path / "a-file.txt",
        "empty": tmp_path / "empty",
        "only_non_images": tmp_path / "docs",
    }[kind]
    if kind == "file":
        folder.write_text("x")
    elif kind in ("empty", "only_non_images"):
        folder.mkdir()
    if kind == "only_non_images":
        (folder / "notes.txt").write_text("x")

    with Session(ctx.engine) as s, pytest.raises(InvalidInputError) as exc:
        pipeline.create_folder_job(OWNER_ID, s, ctx.files, folder)

    assert exc.value.code == "invalid_input"
    _assert_nothing_stored(ctx)


def _skip_without_posix_permissions():
    if sys.platform == "win32" or os.geteuid() == 0:
        pytest.skip("permission bits are not enforced here")


def test_folder_job_unreadable_folder(make_ctx, tmp_path):
    _skip_without_posix_permissions()
    ctx = make_ctx([])
    src = _make_folder(tmp_path)
    (src / "a.jpg").write_bytes(noise_image_bytes(1))
    src.chmod(0o000)
    try:
        with Session(ctx.engine) as s, pytest.raises(InvalidInputError, match="cannot read folder"):
            pipeline.create_folder_job(OWNER_ID, s, ctx.files, src)
    finally:
        src.chmod(0o755)
    _assert_nothing_stored(ctx)


def test_folder_job_unreadable_source_file_compensates_earlier_copies(make_ctx, tmp_path):
    _skip_without_posix_permissions()
    ctx = make_ctx([])
    src = _make_folder(tmp_path)
    (src / "a.jpg").write_bytes(noise_image_bytes(1))
    locked = src / "b.jpg"
    locked.write_bytes(noise_image_bytes(2))
    locked.chmod(0o000)
    try:
        with Session(ctx.engine) as s, pytest.raises(InvalidInputError, match=re.escape("cannot read b.jpg")):
            pipeline.create_folder_job(OWNER_ID, s, ctx.files, src)
    finally:
        locked.chmod(0o644)
    _assert_nothing_stored(ctx)


def test_folder_job_compensates_when_commit_fails(make_ctx, tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(pipeline, "_new_job", boom)
    ctx = make_ctx([])
    src = _make_folder(tmp_path)
    (src / "a.jpg").write_bytes(noise_image_bytes(1))

    with Session(ctx.engine) as s, pytest.raises(OSError) as exc:
        pipeline.create_folder_job(OWNER_ID, s, ctx.files, src)

    assert exc.value.errno == errno.ENOSPC
    assert stored_files(ctx.data_dir) == []


def test_folder_heic_without_extra_fails_photo_and_job_continues(make_ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(images, "HEIC_AVAILABLE", False)
    ctx = make_ctx([analysis(garment())])
    src = _make_folder(tmp_path)
    (src / "a.jpg").write_bytes(noise_image_bytes(1))
    (src / "b.heic").write_bytes(b"not really heic")

    with Session(ctx.engine) as s:
        job = pipeline.create_folder_job(OWNER_ID, s, ctx.files, src)
    assert job.total == 2  # HEIC files are included and fail in the worker
    pipeline.run_job(OWNER_ID, ctx, job.id)

    done = _job(ctx, job.id)
    assert (done.status, done.processed, done.failed) == ("done", 2, 1)
    good, heic = _photos(ctx)
    assert (good.status, heic.status) == ("done", "failed")
    assert "Export them as JPEG" in heic.error


# --- the main item of a photo --------------------------------------------------------------------------------

JACKET = (0.05, 0.3, 0.95, 0.95)
TEE = (0.35, 0.35, 0.65, 0.9)
JEANS = (0.2, 0.9, 0.8, 1.0)


def _worn_outfit(main=None):
    """A person wearing a jacket, a t-shirt and jeans. `main` names the item the model marks as the main one."""
    items = [("Olive jacket", "outerwear", JACKET), ("White tee", "top", TEE), ("Blue jeans", "bottom", JEANS)]
    return analysis(
        *[{**garment(box, name=name, category=cat), "main": name == main} for name, cat, box in items],
        photo_type="worn",
    )


def _names(ctx):
    with Session(ctx.engine) as s:
        return [p.proposed_tags["name"] for p in s.exec(select(IngestPiece).order_by(IngestPiece.id)).all()]


def test_a_worn_photo_gives_only_the_item_it_is_mainly_showing(make_ctx):
    ctx = make_ctx([_worn_outfit(main="Olive jacket")])
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(1)))

    assert _names(ctx) == ["Olive jacket"]


def test_the_models_choice_of_main_item_wins_over_size(make_ctx):
    ctx = make_ctx([_worn_outfit(main="White tee")])
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(1)))

    assert _names(ctx) == ["White tee"]


def test_without_a_marked_main_item_the_largest_one_is_taken(make_ctx):
    ctx = make_ctx([_worn_outfit(main=None)])
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(1)))

    assert _names(ctx) == ["Olive jacket"]


def test_laid_out_photos_still_give_every_item(make_ctx):
    reply = _worn_outfit(main="Olive jacket") | {"photo_type": "flat_lay"}
    ctx = make_ctx([reply])
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(1)))

    assert _names(ctx) == ["Olive jacket", "White tee", "Blue jeans"]


@pytest.mark.parametrize(
    ("setting", "photo_type", "expected"),
    [("always", "flat_lay", 1), ("never", "worn", 3), ("worn", "hanger", 3)],
)
def test_main_item_only_setting(make_ctx, setting, photo_type, expected):
    ctx = make_ctx([_worn_outfit(main="Olive jacket") | {"photo_type": photo_type}])
    ctx.prefs.ingest.main_item_only = setting
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(1)))

    assert len(_names(ctx)) == expected


def _worn_kurta_set(main="Cream kurta", in_set=("Cream kurta", "White pyjama")):
    """A person in a kurta and pyjama, with a jacket over them and shoes."""
    items = [
        ("Black nehru jacket", "ethnic", JACKET),
        ("Cream kurta", "ethnic", TEE),
        ("White pyjama", "bottom", JEANS),
        ("Tan mojari", "footwear", (0.4, 0.95, 0.6, 1.0)),
    ]
    return analysis(
        *[
            {**garment(box, name=name, category=cat), "main": name == main, "in_set": name in in_set}
            for name, cat, box in items
        ],
        photo_type="worn",
    )


def test_a_worn_traditional_set_gives_every_piece_of_the_set(make_ctx):
    ctx = make_ctx([_worn_kurta_set()])
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(1)))

    assert _names(ctx) == ["Cream kurta", "White pyjama"]


def test_set_pieces_stay_out_when_the_main_item_is_not_one_of_them(make_ctx):
    ctx = make_ctx([_worn_kurta_set(main="Black nehru jacket")])
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(1)))

    assert _names(ctx) == ["Black nehru jacket"]


def test_main_item_default_and_prompt():
    from kyapehnu.core.config import Prefs
    from kyapehnu.wardrobe.ingest import tagging

    assert Prefs().ingest.main_item_only == "worn"
    assert "main:" in tagging.SYSTEM
    assert "in_set:" in tagging.SYSTEM
    parsed = tagging.DetectedGarment.model_validate(garment())
    assert (parsed.main, parsed.in_set) == (False, False)


# --- "same photo uploaded before" only when the earlier upload still matters ---------------------------------


def test_reuploading_a_photo_that_failed_before_is_not_called_a_duplicate(make_ctx):
    ctx = make_ctx([LLMUnavailableError("down"), analysis(garment())])
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(7)))

    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(7)))

    assert [p.dup_reason for p in _pieces(ctx) if p.state == "pending_review"] == [None]


def test_reuploading_a_photo_whose_pieces_were_discarded_is_not_called_a_duplicate(make_ctx):
    ctx = make_ctx([analysis(garment()), analysis(garment())])
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(7)))
    with Session(ctx.engine) as s:
        review.discard_piece(s, s.exec(select(IngestPiece)).one())

    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(7)))

    assert [p.dup_reason for p in _pieces(ctx) if p.state == "pending_review"] == [None]


def test_reuploading_a_photo_already_in_the_wardrobe_is_still_flagged(make_ctx):
    ctx = make_ctx([analysis(garment()), analysis(garment())])
    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(7)))
    with Session(ctx.engine) as s:
        review.accept_piece(s, s.exec(select(IngestPiece)).one())

    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(7)))

    [new] = [p for p in _pieces(ctx) if p.state == "pending_review"]
    assert new.dup_reason is not None


# --- on every store (AC-13) -----------------------------------------------------------------------------------------


@pytest.fixture
def store_ctx(engine, store, tmp_path):
    return lambda replies: make_store_ctx(engine, store, tmp_path, replies)


def test_upload_and_processing_keep_every_key_a_row_names_in_the_store(store_ctx, store):
    ctx = store_ctx([analysis(garment((0.0, 0.0, 1.0, 0.5)), garment((0.0, 0.5, 1.0, 1.0)))])

    pipeline.run_job(OWNER_ID, ctx, _upload(ctx, noise_image_bytes(1)))

    [photo] = _photos(ctx)
    pieces = _pieces(ctx)
    keys = [photo.original_path, photo.normalized_path, *(k for p in pieces for k in (p.cutout_path, p.thumb_path))]
    assert len(pieces) == 2
    assert sorted(f.key for f in store.list("images/")) == sorted(keys)
    assert all(store.exists(key) for key in keys)
    assert Image.open(io.BytesIO(store.read_bytes(photo.normalized_path))).format == "JPEG"


def test_folder_job_copies_the_sources_into_the_store_and_leaves_the_folder_alone(store_ctx, store, tmp_path):
    ctx = store_ctx([])
    src = tmp_path / "photos"
    src.mkdir()
    (src / "a.JPG").write_bytes(noise_image_bytes(1))

    with Session(ctx.engine) as s:
        job = pipeline.create_folder_job(OWNER_ID, s, ctx.files, src)
        assert job.total == 1

    [photo] = _photos(ctx)
    assert photo.original_path.endswith(".jpg")
    assert store.read_bytes(photo.original_path) == (src / "a.JPG").read_bytes()
    assert (src / "a.JPG").is_file()


def test_a_photo_whose_original_is_missing_from_the_store_fails_on_its_own(store_ctx, store):
    ctx = store_ctx([analysis(garment())])
    job_id = _upload(ctx, noise_image_bytes(1))
    [photo] = _photos(ctx)
    store.delete(photo.original_path)

    pipeline.run_job(OWNER_ID, ctx, job_id)

    [photo] = _photos(ctx)
    assert (photo.status, photo.error) == ("failed", "Image not found")  # as for a picture that cannot be decoded
    assert _pieces(ctx) == []
    assert _job(ctx, job_id).failed == 1


# --- the photos of one job are processed a few at a time --------------------------------------------------------------


def test_the_photos_of_one_job_are_processed_a_few_at_a_time(make_ctx):
    from dataclasses import replace

    from kyapehnu.core.config import Prefs

    ctx = replace(make_ctx([analysis(garment())] * 3), prefs=Prefs(ingest={"workers": 3}))
    job_id = _upload(ctx, noise_image_bytes(1), noise_image_bytes(2), noise_image_bytes(3))

    pipeline.run_job(OWNER_ID, ctx, job_id)

    assert [p.status for p in _photos(ctx)] == ["done"] * 3
    assert len(_pieces(ctx)) == 3
    job = _job(ctx, job_id)
    assert (job.status, job.total, job.processed, job.failed) == ("done", 3, 3, 0)
