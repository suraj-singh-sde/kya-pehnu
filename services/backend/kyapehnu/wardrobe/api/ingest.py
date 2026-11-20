from typing import Annotated, cast

from fastapi import APIRouter, Query, Response, UploadFile
from pydantic import BaseModel

from kyapehnu.auth.public import CurrentUser
from kyapehnu.core.config import MAX_UPLOAD_FILE_BYTES, MAX_UPLOAD_FILES
from kyapehnu.core.db import get_owned
from kyapehnu.core.deps import CtxDep, SessionDep, SettingsDep, WorkerDep
from kyapehnu.core.errors import PayloadTooLargeError
from kyapehnu.core.schemas import TagsPatch
from kyapehnu.wardrobe import pictures
from kyapehnu.wardrobe.ingest import jobs, pipeline, review
from kyapehnu.wardrobe.ingest.jobs import Worker
from kyapehnu.wardrobe.models import IngestJob, IngestPiece, Item, Photo, PieceState

router = APIRouter(prefix="/api/ingest", tags=["ingest"])

REVIEW_QUEUE_STATES = list(review.REVIEWABLE)
DEFAULT_UPLOAD_NAME = "upload"
BYTES_PER_MB = 1024 * 1024


class AcceptIn(BaseModel):
    overrides: TagsPatch | None = None


class MergeIn(BaseModel):
    other_piece_ids: list[int]


class DuplicateIn(BaseModel):
    item_id: int


def _check_upload_limits(files: list[UploadFile]) -> None:
    if len(files) > MAX_UPLOAD_FILES:
        raise PayloadTooLargeError()
    for f in files:
        size = f.file.seek(0, 2)
        f.file.seek(0)
        if size > MAX_UPLOAD_FILE_BYTES:
            raise PayloadTooLargeError(
                f"{f.filename or DEFAULT_UPLOAD_NAME} is over {MAX_UPLOAD_FILE_BYTES // BYTES_PER_MB} MB"
            )


def _enqueue(job: IngestJob, worker: Worker, response: Response) -> IngestJob:
    job_id = cast(int, job.id)  # the job was committed and refreshed
    worker.submit_job(job_id)
    response.headers["Location"] = f"{router.prefix}/jobs/{job_id}"
    return job


@router.post("", status_code=201)
def upload(
    files: list[UploadFile], response: Response, s: SessionDep, ctx: CtxDep, worker: WorkerDep, user: CurrentUser
) -> IngestJob:
    _check_upload_limits(files)
    job = pipeline.create_upload_job(
        user.id, s, ctx.files, [(f.filename or DEFAULT_UPLOAD_NAME, f.file) for f in files]
    )
    return _enqueue(job, worker, response)


@router.get("/jobs")
def list_jobs(s: SessionDep, user: CurrentUser) -> list[IngestJob]:
    return jobs.recent(user.id, s)


@router.get("/jobs/{job_id}")
def get_job(job_id: int, s: SessionDep, user: CurrentUser) -> IngestJob:
    return get_owned(user.id, s, IngestJob, job_id)


@router.get("/pieces")
def list_pieces(
    s: SessionDep,
    user: CurrentUser,
    state: Annotated[list[PieceState], Query()] = REVIEW_QUEUE_STATES,
    job_id: int | None = None,
) -> list[IngestPiece]:
    return review.queue(user.id, s, state, job_id)


@router.post("/pieces/{piece_id}/accept")
def accept(piece_id: int, body: AcceptIn, s: SessionDep, user: CurrentUser) -> Item:
    overrides = body.overrides.model_dump(exclude_none=True) if body.overrides else None
    return review.accept_piece(s, get_owned(user.id, s, IngestPiece, piece_id), overrides)


@router.post("/pieces/{piece_id}/discard")
def discard(piece_id: int, s: SessionDep, user: CurrentUser) -> IngestPiece:
    return review.discard_piece(s, get_owned(user.id, s, IngestPiece, piece_id))


@router.post("/pieces/{piece_id}/merge")
def merge(piece_id: int, body: MergeIn, s: SessionDep, ctx: CtxDep, user: CurrentUser) -> IngestPiece:
    return review.merge_pieces(ctx, s, get_owned(user.id, s, IngestPiece, piece_id), body.other_piece_ids)


@router.post("/pieces/{piece_id}/mark-duplicate")
def mark_duplicate(piece_id: int, body: DuplicateIn, s: SessionDep, user: CurrentUser) -> IngestPiece:
    return review.mark_duplicate(s, get_owned(user.id, s, IngestPiece, piece_id), body.item_id)


@router.post("/pieces/{piece_id}/clean-picture")
def make_clean_picture(
    piece_id: int, s: SessionDep, ctx: CtxDep, settings: SettingsDep, user: CurrentUser
) -> IngestPiece:
    piece = review.require_pending(get_owned(user.id, s, IngestPiece, piece_id))
    pictures.generate(ctx, settings, s, piece)
    s.refresh(piece)
    return piece


@router.post("/pieces/{piece_id}/clean-picture/switch")
def switch_picture(piece_id: int, s: SessionDep, user: CurrentUser) -> IngestPiece:
    piece = review.require_pending(get_owned(user.id, s, IngestPiece, piece_id))
    pictures.switch(s, piece)
    s.refresh(piece)
    return piece


@router.delete("/pieces/{piece_id}/clean-picture")
def delete_clean_picture(piece_id: int, s: SessionDep, ctx: CtxDep, user: CurrentUser) -> IngestPiece:
    piece = review.require_pending(get_owned(user.id, s, IngestPiece, piece_id))
    pictures.remove(ctx, s, piece)
    s.refresh(piece)
    return piece


@router.post("/photos/{photo_id}/retry", status_code=202)
def retry(photo_id: int, s: SessionDep, worker: WorkerDep, user: CurrentUser) -> dict[str, bool]:
    pipeline.require_retryable(get_owned(user.id, s, Photo, photo_id))
    worker.submit_retry(photo_id)
    return {"queued": True}
