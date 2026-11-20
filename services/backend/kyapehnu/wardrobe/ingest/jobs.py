import logging
import queue
import threading
from typing import Final, cast

from sqlmodel import Session, col, select

from kyapehnu.core.context import Ctx
from kyapehnu.core.db import get_one
from kyapehnu.wardrobe.ingest.pipeline import retry_photo, run_job
from kyapehnu.wardrobe.models import IngestJob, JobStatus, Photo, PhotoStatus

log = logging.getLogger(__name__)

JOB: Final = "job"
RETRY: Final = "retry"
STOP_JOIN_SECONDS: Final = 10
RECENT_JOBS_LIMIT: Final = 20


def recent(user_id: int, s: Session) -> list[IngestJob]:
    stmt = (
        select(IngestJob)
        .where(IngestJob.user_id == user_id)
        .order_by(col(IngestJob.id).desc())
        .limit(RECENT_JOBS_LIMIT)
    )
    return list(s.exec(stmt).all())


class Worker:
    """Single background thread that processes ingestion jobs in order.

    ponytail: one photo at a time with sequential LLM calls; add a thread pool if bulk ingest is too slow.
    """

    def __init__(self, ctx: Ctx) -> None:
        self.ctx = ctx
        self._queue: queue.Queue[tuple[str, int] | None] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Re-enqueue jobs a crash left queued, running or holding a pending photo (retry), then start the thread."""
        with Session(self.ctx.engine) as s:
            unfinished = s.exec(
                select(IngestJob.id)
                .where(
                    col(IngestJob.status).in_([JobStatus.queued, JobStatus.running])
                    | col(IngestJob.id).in_(select(Photo.job_id).where(col(Photo.status) == PhotoStatus.pending))
                )
                .order_by(col(IngestJob.id))
            ).all()
        for job_id in cast(list[int], unfinished):
            self.submit_job(job_id)
        self._thread = threading.Thread(target=self._loop, name="kyapehnu-ingest", daemon=True)
        self._thread.start()

    def submit_job(self, job_id: int) -> None:
        self._queue.put((JOB, job_id))

    def submit_retry(self, photo_id: int) -> None:
        self._queue.put((RETRY, photo_id))

    def drain(self) -> None:
        """Block until all queued work is done (used by tests)."""
        self._queue.join()

    def stop(self) -> None:
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=STOP_JOIN_SECONDS)

    def _owner(self, model: type[IngestJob] | type[Photo], ident: int) -> int:
        """Whose job or photo it is: the worker serves every account, the row says which."""
        with Session(self.ctx.engine) as s:
            return get_one(s, model, ident).user_id

    def _loop(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if task is None:
                    return
                kind, ident = task
                if kind == JOB:
                    run_job(self._owner(IngestJob, ident), self.ctx, ident)
                else:
                    retry_photo(self._owner(Photo, ident), self.ctx, ident)
            except Exception as e:  # one failing task must not kill the worker
                log.error("event=ingest_task_failed task=%s error=%s", task, type(e).__name__)
                log.debug("event=ingest_task_failed_trace", exc_info=True)
            finally:
                self._queue.task_done()
