"""Photo to review pieces. Each photo is analysed by one LLM call, then cropped, cut out and dedupe-checked."""

import logging
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Final, cast
from uuid import uuid4

from PIL import Image
from sqlalchemy import ColumnElement, exists, or_
from sqlmodel import Session, col, delete, func, select

from kyapehnu.agent.public import UNUSABLE_REPLY, LLMError, LLMOutputError, redact
from kyapehnu.core.config import MAX_UPLOAD_FILE_BYTES, MAX_UPLOAD_FILES, IngestPrefs
from kyapehnu.core.context import Ctx
from kyapehnu.core.db import get_owned, utcnow
from kyapehnu.core.errors import (
    AppError,
    ConflictError,
    HeicExtraMissingError,
    InvalidInputError,
    PayloadTooLargeError,
    UnsupportedFileError,
)
from kyapehnu.files import public as files
from kyapehnu.wardrobe.ingest import dedupe, images, screening, tagging
from kyapehnu.wardrobe.models import IngestJob, IngestPiece, Item, JobStatus, Photo, PhotoStatus, PhotoType, PieceState

log = logging.getLogger(__name__)

FULL_FRAME: Final = (0.0, 0.0, 1.0, 1.0)
NO_GARMENTS: Final = "No garments detected"
UNREADABLE_IMAGE: Final = "could not read image"


def _original_key(user_id: int, suffix: str) -> str:
    return files.key(user_id, "original", f"{uuid4().hex}{suffix.lower()}")


def _new_job(user_id: int, s: Session, source: str, keys: list[str]) -> IngestJob:
    job = IngestJob(user_id=user_id, source=source, total=len(keys))
    s.add(job)
    s.flush()
    for key in keys:
        s.add(Photo(user_id=user_id, job_id=job.id, original_path=key))
    s.commit()
    return job


def _save_original(src: BinaryIO, store: files.FileStore, user_id: int, suffix: str, written: list[str]) -> None:
    """Put src in the store as one of the owner's originals. The key is recorded first so a partial put is compensated
    too."""
    key = _original_key(user_id, suffix)
    written.append(key)
    store.put(key, src)


@contextmanager
def _compensate(s: Session, store: files.FileStore, source: str, written: list[str]) -> Iterator[None]:
    """On any failure, drop the open transaction and the originals written so far, then re-raise unchanged."""
    try:
        yield
    except Exception:
        s.rollback()
        files.discard(store, written)  # best effort: the orphan sweep removes what is left
        if written:
            log.warning("event=upload_compensated source=%s files=%d", source, len(written))
        raise


def create_upload_job(
    user_id: int, s: Session, store: files.FileStore, uploads: list[tuple[str, BinaryIO]]
) -> IngestJob:
    if not uploads:
        raise InvalidInputError("no files uploaded")
    for name, _ in uploads:
        suffix = Path(name).suffix.lower()
        if suffix not in images.IMAGE_EXTS:
            raise UnsupportedFileError(f"unsupported file type: {name}")
        if suffix in images.HEIC_EXTS and not images.HEIC_AVAILABLE:
            raise HeicExtraMissingError()
    written: list[str] = []
    with _compensate(s, store, "upload", written):
        for name, stream in uploads:
            _save_original(stream, store, user_id, Path(name).suffix, written)
        job = _new_job(user_id, s, "upload", written)
    s.refresh(job)  # outside _compensate: a failure here must not delete the originals of a committed job
    return job


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError as e:
        raise InvalidInputError(f"cannot read {path.name}") from e


def _folder_images(folder: Path) -> list[Path]:
    if not folder.is_absolute():
        raise InvalidInputError("folder path must be absolute")
    try:
        if not folder.is_dir():
            raise InvalidInputError(f"not a directory: {folder}")
        sources = sorted(
            p for p in folder.iterdir() if not p.is_symlink() and p.is_file() and p.suffix.lower() in images.IMAGE_EXTS
        )
    except OSError as e:
        raise InvalidInputError(f"cannot read folder: {folder} ({e.strerror})") from e
    if not sources:
        raise InvalidInputError(f"no images found in {folder}")
    # Same bounds as an upload: one request must not copy and tag an unbounded library.
    if len(sources) > MAX_UPLOAD_FILES or any(_size(p) > MAX_UPLOAD_FILE_BYTES for p in sources):
        raise PayloadTooLargeError()
    return sources


def create_folder_job(user_id: int, s: Session, store: files.FileStore, folder: Path) -> IngestJob:
    sources = _folder_images(folder)
    written: list[str] = []
    with _compensate(s, store, "folder", written):
        for src in sources:
            try:
                handle = src.open("rb")
            except OSError as e:
                raise InvalidInputError(f"cannot read {src.name}") from e
            with handle:
                _save_original(handle, store, user_id, src.suffix, written)
        job = _new_job(user_id, s, "folder", written)
    s.refresh(job)
    return job


def cut_piece(ctx: Ctx, user_id: int, img: Image.Image, box: list[float], use_rembg: bool) -> tuple[str, str, str]:
    """Crop, cut out and store one piece. Returns the cutout key, thumb key and the cutout's perceptual hash."""
    cut = images.make_cutout(images.crop(img, box), use_rembg)
    phash = images.phash(cut)  # before the puts: nothing that can fail is left to run once the pictures are stored
    cut_key, thumb_key = images.save_cutout(cut, ctx.files, user_id, uuid4().hex)
    return cut_key, thumb_key, phash


def _seen_before(s: Session, photo: Photo, phash: str, max_distance: int) -> bool:
    """Was this photo uploaded before and is that upload still around: in the wardrobe or waiting for review?

    An earlier upload that failed, or whose pieces were all discarded, is not a reason to warn.
    """
    in_wardrobe = exists().where(col(Item.photo_id) == Photo.id)
    in_review = exists().where(
        col(IngestPiece.photo_id) == Photo.id, col(IngestPiece.state) == PieceState.pending_review
    )
    others = s.exec(
        select(Photo.phash).where(
            Photo.user_id == photo.user_id,
            Photo.id != photo.id,
            col(Photo.phash).is_not(None),
            or_(in_wardrobe, in_review),
        )
    ).all()
    return any(other and images.hamming(phash, other) <= max_distance for other in others)


def _mark_failed(s: Session, photo: Photo, message: str, cause: str) -> bool:
    photo.status, photo.error = PhotoStatus.failed, message
    s.commit()
    log.warning("event=photo_failed photo_id=%s error=%s", photo.id, cause)  # class only: messages can echo content
    return False


def _new_piece(photo: Photo, **fields: Any) -> IngestPiece:
    # A persisted photo always has an id and a job (see _new_job).
    return IngestPiece(user_id=photo.user_id, job_id=cast(int, photo.job_id), photo_id=cast(int, photo.id), **fields)


def _fail_with_piece(s: Session, photo: Photo, message: str, cause: str, state: str = PieceState.error) -> bool:
    """Leave one full-frame error (or rejected) piece so the failure shows in the review queue and can be retried."""
    s.add(_new_piece(photo, bbox=list(FULL_FRAME), state=state, error=message))
    return _mark_failed(s, photo, message, cause)


def _load_failure_message(e: Exception) -> str:
    # Library messages (PIL) can carry the file path; only our own errors are safe to show.
    return str(e) if isinstance(e, AppError) else UNREADABLE_IMAGE


def _tagging_failure_message(e: LLMError, secrets: Iterable[str | None]) -> str:
    return UNUSABLE_REPLY if isinstance(e, LLMOutputError) else redact(f"tagging failed: {e}", secrets)


def _screening_failure_message(e: LLMError, secrets: Iterable[str | None]) -> str:
    return UNUSABLE_REPLY if isinstance(e, LLMOutputError) else redact(f"checking the photo failed: {e}", secrets)


def _garments_to_keep(
    prefs: IngestPrefs, result: tagging.PhotoAnalysis, size: tuple[int, int]
) -> list[tuple[tagging.DetectedGarment, list[float]]]:
    """The detected garments worth a piece, each with its normalized box.

    Too-small boxes are dropped. For photos of a person wearing clothes (or for every photo, by setting) only the
    item the photo is mainly showing is kept: the one the model marked, else the largest. When that item is one
    piece of a traditional set (a kurta with its pyjama), the set's other pieces are kept with it.
    """
    found = [(g, images.normalize_bbox(g.bbox, *size)) for g in result.garments]
    found = [(g, box) for g, box in found if images.bbox_area(box) >= prefs.min_bbox_area]
    main_only = prefs.main_item_only == "always" or (
        prefs.main_item_only == "worn" and result.photo_type == PhotoType.worn
    )
    if not main_only or len(found) <= 1:
        return found
    marked = [pair for pair in found if pair[0].main]
    main = max(marked or found, key=lambda pair: images.bbox_area(pair[1]))
    return [pair for pair in found if pair is main or (main[0].in_set and pair[0].in_set)]


def process_photo(user_id: int, ctx: Ctx, photo_id: int) -> bool:
    """Turn one photo into review pieces. Returns False if the photo failed.

    Pictures are stored before the rows that name them. If the store goes away the rows are rolled back, what was put
    since the last commit is discarded, and the photo fails with an error piece (retryable). If a commit fails the
    same pictures are discarded and the error goes on to the caller.
    """
    with Session(ctx.engine) as s:
        photo = get_owned(user_id, s, Photo, photo_id)
        uncommitted: list[str] = []
        try:
            return _store_pieces(user_id, ctx, s, photo, uncommitted)
        except files.StoreUnavailableError as e:
            s.rollback()
            files.discard(ctx.files, uncommitted)
            return _fail_with_piece(s, photo, str(e), type(e).__name__)
        except Exception:
            files.discard(ctx.files, uncommitted)
            raise


def _store_pieces(user_id: int, ctx: Ctx, s: Session, photo: Photo, uncommitted: list[str]) -> bool:
    """The body of process_photo: `uncommitted` collects the keys put that no committed row names yet."""
    try:
        img = images.load_normalized(ctx.files, photo.original_path)
    except files.StoreUnavailableError:
        raise
    except Exception as e:  # any decoder failure: a bad photo must not stop the job
        return _mark_failed(s, photo, _load_failure_message(e), type(e).__name__)

    jpeg = images.to_jpeg_bytes(img)
    normalized = files.key(user_id, "original", f"{PurePosixPath(photo.original_path).stem}_norm.jpg")
    ctx.files.put(normalized, jpeg)
    if photo.normalized_path != normalized:  # a retry overwrites the key a row already names: never discard that
        uncommitted.append(normalized)
    photo.normalized_path = normalized
    photo.phash = images.phash(img)
    reupload = _seen_before(s, photo, photo.phash, ctx.prefs.dedupe.photo_phash_distance)
    # The LLM log sink writes on its own connection: an open write transaction here would make it wait out
    # SQLite's busy timeout and lose the row.
    s.commit()
    uncommitted.clear()

    # First: is this a garment at all, and not innerwear? A photo that is not is turned away before any tagging.
    if ctx.prefs.ingest.screen_photos:
        try:
            rejected = screening.check(user_id, ctx.provider, jpeg)
        except LLMError as e:
            return _fail_with_piece(s, photo, _screening_failure_message(e, ctx.secrets), type(e).__name__)
        if rejected:
            return _fail_with_piece(s, photo, rejected, "Rejected", state=PieceState.rejected)
    try:
        result = tagging.analyze_photo(user_id, ctx.provider, jpeg)
    except LLMError as e:
        return _fail_with_piece(s, photo, _tagging_failure_message(e, ctx.secrets), type(e).__name__)
    photo.photo_type = result.photo_type

    made = 0
    for g, box in _garments_to_keep(ctx.prefs.ingest, result, img.size):
        box = images.pad_bbox(box, ctx.prefs.ingest.bbox_padding)
        cut_key, thumb_key, ph = cut_piece(ctx, user_id, img, box, use_rembg=result.photo_type != PhotoType.worn)
        uncommitted += [cut_key, thumb_key]
        with s.no_autoflush:  # same reason as above: the dedupe LLM call must not run under a write lock
            dup_id, reason = dedupe.find_duplicate(
                user_id,
                s,
                cutout_phash=ph,
                tags=g.tags,
                cutout_png=ctx.files.read_bytes(cut_key),
                provider=ctx.provider,
                prefs=ctx.prefs.dedupe,
                store=ctx.files,
            )
        if reupload and not reason:
            reason = "same photo uploaded before"
        s.add(
            _new_piece(
                photo,
                bbox=box,
                cutout_path=cut_key,
                thumb_path=thumb_key,
                phash=ph,
                proposed_tags=g.tags.model_dump(mode="json"),
                duplicate_of_item_id=dup_id,
                dup_reason=reason,
            )
        )
        made += 1
    if not made:
        return _fail_with_piece(s, photo, NO_GARMENTS, "NoGarments")
    photo.status = PhotoStatus.done
    s.commit()
    return True


def refresh_counts(s: Session, job: IngestJob) -> None:
    def count(*where: ColumnElement[bool]) -> int:
        stmt = select(func.count()).select_from(Photo).where(col(Photo.job_id) == job.id, *where)
        return s.exec(stmt).one()

    job.processed = count(col(Photo.status) != PhotoStatus.pending)
    job.failed = count(col(Photo.status) == PhotoStatus.failed)
    job.updated_at = utcnow()


def _process_safely(user_id: int, ctx: Ctx, photo_id: int) -> None:
    try:
        if process_photo(user_id, ctx, photo_id):
            from kyapehnu.wardrobe import pictures  # late: pictures builds on this module

            pictures.auto_for_photo(user_id, ctx, photo_id)  # after the commit, so the slow calls hold no database lock
    except Exception as e:  # disk full, a bug: never leave the photo pending forever
        log.error("event=photo_failed photo_id=%s error=%s", photo_id, type(e).__name__)
        log.debug("event=photo_failed_trace photo_id=%s", photo_id, exc_info=True)
        with Session(ctx.engine) as s:
            photo = get_owned(user_id, s, Photo, photo_id)
            photo.status, photo.error = PhotoStatus.failed, f"unexpected error ({type(e).__name__})"
            s.commit()


def _update_job(user_id: int, ctx: Ctx, job_id: int, status: str | None = None) -> tuple[int, int]:
    """Refresh the job's counts (and status). Returns (processed, failed)."""
    with Session(ctx.engine) as s:
        job = get_owned(user_id, s, IngestJob, job_id)
        refresh_counts(s, job)
        if status:
            job.status = status
        counts = (job.processed, job.failed)
        s.commit()
    return counts


def run_job(user_id: int, ctx: Ctx, job_id: int) -> None:
    """Process the pending photos of one of this user's jobs. Another user's job is NotFoundError, before any write."""
    _update_job(user_id, ctx, job_id, JobStatus.running)
    log.info("event=job_started job_id=%s", job_id)
    with Session(ctx.engine) as s:
        pending = s.exec(
            select(Photo.id)
            .where(col(Photo.user_id) == user_id, col(Photo.job_id) == job_id, col(Photo.status) == PhotoStatus.pending)
            .order_by(col(Photo.id))
        ).all()

    def one(photo_id: int) -> None:
        _process_safely(user_id, ctx, photo_id)
        _update_job(user_id, ctx, job_id)

    # ponytail: a few photos at a time, each with its own session; SQLite's busy timeout covers the commits, and two
    # copies of one photo in the same job may both get through the "seen before" check
    with ThreadPoolExecutor(max_workers=ctx.prefs.ingest.workers, thread_name_prefix="kyapehnu-ingest") as pool:
        list(pool.map(one, cast(list[int], pending)))
    processed, failed = _update_job(user_id, ctx, job_id, JobStatus.done)
    log.info("event=job_done job_id=%s processed=%d failed=%d", job_id, processed, failed)


def require_retryable(photo: Photo) -> None:
    """Only a failed photo can be retried: retrying a done photo would analyse it again and duplicate its pieces."""
    if photo.status != PhotoStatus.failed:
        raise ConflictError(f"photo {photo.id} has not failed")


def retry_photo(user_id: int, ctx: Ctx, photo_id: int) -> None:
    with Session(ctx.engine) as s:
        photo = get_owned(user_id, s, Photo, photo_id)
        if photo.status != PhotoStatus.failed:  # a second queued retry: the first one already handled it
            log.info("event=retry_skipped photo_id=%s status=%s", photo_id, photo.status)
            return
        s.exec(
            delete(IngestPiece).where(
                col(IngestPiece.user_id) == user_id,
                col(IngestPiece.photo_id) == photo_id,
                col(IngestPiece.state).in_([PieceState.error, PieceState.rejected]),
            )
        )
        photo.status, photo.error = PhotoStatus.pending, None
        s.commit()
        job_id = photo.job_id
    _process_safely(user_id, ctx, photo_id)
    if job_id is not None:
        _update_job(user_id, ctx, job_id)
