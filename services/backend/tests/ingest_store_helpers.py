"""Helpers of the WP-2 tests: ingest on a picture store, and picture stores that fail on purpose."""

from sqlmodel import Session, select

from kyapehnu.agent.llm.base import LLMProvider
from kyapehnu.core.config import Prefs
from kyapehnu.core.context import Ctx
from kyapehnu.files.errors import StoreUnavailableError
from kyapehnu.files.s3 import S3Store
from kyapehnu.wardrobe.models import IngestPiece
from tests.conftest import TEST_BUCKET
from tests.fakes import FakeTransport


def bucket_keys(s3) -> list[str]:
    """Every key in the test bucket, sorted (read with the test's own client, behind the app's back)."""
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=TEST_BUCKET)
    return sorted(obj["Key"] for page in pages for obj in page.get("Contents", []))


def make_store_ctx(engine, store, tmp_path, replies=()) -> Ctx:
    """A pipeline Ctx on `store` whose LLM replies are scripted (the transport is ctx.provider.transport)."""
    return Ctx(
        engine=engine,
        provider=LLMProvider(FakeTransport(list(replies))),
        prefs=Prefs(ingest={"workers": 1}),  # scripted replies are in photo order
        data_dir=tmp_path,
        file_store=store,
    )


def put_fails_after(monkeypatch, allowed: int, store_class: type = S3Store) -> list[str]:
    """Let `allowed` more puts through, then every put of `store_class` is an outage. Returns the keys put."""
    original = store_class.put
    stored: list[str] = []

    def put(self, key, body):
        if len(stored) >= allowed:
            raise StoreUnavailableError
        original(self, key, body)
        stored.append(key)

    monkeypatch.setattr(store_class, "put", put)
    return stored


def fail_nth_commit(monkeypatch, nth: int, error: Exception) -> None:
    """The `nth` commit (1 is the first) from now on raises `error` instead of committing."""
    original = Session.commit
    seen = {"n": 0}

    def commit(self):
        seen["n"] += 1
        if seen["n"] == nth:
            raise error
        original(self)

    monkeypatch.setattr(Session, "commit", commit)


def pieces_of(engine) -> list[IngestPiece]:
    with Session(engine) as s:
        return list(s.exec(select(IngestPiece).order_by(IngestPiece.id)).all())
