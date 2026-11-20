"""AC-43 (NFR-12): 5 people write at the same time for 2 minutes (uploads, chat turns, wear logs, item edits). No
response is a 500, contention answers only 503 `db_busy` with Retry-After (a writer waits that long and tries again, and
counts it as retryable, not failed), and every write that got a success response is in the database afterwards.

Slow: 2 minutes (set KYAPEHNU_QA_WRITE_SECONDS to shorten it while developing). The second test needs no waiting: it makes
SQLite refuse a write at once (a transaction on a stale snapshot) and checks the real error text becomes a 503.
"""

import logging
import os
import threading
import time
from collections import Counter

import pytest
from sqlalchemy import text
from sqlmodel import Session, func, select

from kyapehnu.agent.models import ChatMessage
from kyapehnu.wardrobe.models import IngestJob, Item, WearLog
from tests.chat_fakes import say
from tests.chat_helpers import new_chat
from tests.conftest import noise_image_bytes, sign_in
from tests.fakes import add_item, analysis, garment

SECONDS = float(os.environ.get("KYAPEHNU_QA_WRITE_SECONDS", "120"))
USERS = 5
THINK_S = 0.02
OPS = ("upload", "chat", "wear", "edit")


class Endless(list):
    """A scripted-replies list that never runs out: every `pop(0)` makes a fresh reply."""

    def __init__(self, make):
        super().__init__()
        self._make = make

    def __len__(self) -> int:
        return 1

    def pop(self, index=-1):
        return self._make()


@pytest.mark.slow
@pytest.mark.timeout(int(SECONDS) + 120)  # the suite-wide 60 s limit is shorter than the run
def test_five_writers_for_two_minutes_get_no_500_only_retryable_503s_and_lose_no_write(app, fake, caplog):
    caplog.set_level(logging.WARNING)
    fake.replies = Endless(lambda: analysis(garment()))
    fake.chat_replies = Endless(lambda: say("ok"))
    engine = app.state.ctx.engine
    people = []
    for n in range(USERS):
        uid, c = sign_in(app, f"writer-{n}")
        with Session(engine) as s:
            items = [add_item(s, user_id=uid, name=f"Shirt {k}").id for k in range(3)]
        people.append((uid, c, items, new_chat(c)))
    images = [noise_image_bytes(seed, size=(160, 120)) for seed in range(40)]

    ok: Counter[tuple[int, str]] = Counter()
    retried: Counter[str] = Counter()
    bad: list[str] = []
    last_note: dict[int, str] = {}
    app.state.worker.start()
    deadline = time.monotonic() + SECONDS
    barrier = threading.Barrier(USERS)

    def write(op, c, uid, items, chat, n):
        if op == "upload":
            return c.post("/api/ingest", files=[("files", (f"p{n}.jpg", images[n % len(images)], "image/jpeg"))])
        if op == "chat":
            return c.post(f"/api/chat/sessions/{chat}/messages", json={"text": f"hello {n}"})
        if op == "wear":
            return c.post("/api/outfits/wear", json={"item_ids": [items[n % 3]]})
        return c.patch(f"/api/items/{items[0]}", json={"notes": f"edit {n}"})

    def person(uid, c, items, chat):
        barrier.wait()
        n = 0
        while time.monotonic() < deadline:
            op = OPS[n % len(OPS)]
            n += 1
            for _attempt in range(5):
                r = write(op, c, uid, items, chat, n)
                if r.status_code // 100 == 2:
                    ok[uid, op] += 1
                    if op == "edit":
                        last_note[uid] = f"edit {n}"
                    break
                if r.status_code == 503 and r.json().get("code") == "db_busy" and "Retry-After" in r.headers:
                    retried[op] += 1
                    time.sleep(min(int(r.headers["Retry-After"]), 2))
                    continue
                bad.append(f"{op} {r.status_code} {r.text[:200]}")
                break
            time.sleep(THINK_S)

    threads = [threading.Thread(target=person, args=p) for p in people]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    app.state.worker.drain()
    app.state.worker.stop()

    print(f"succeeded {sum(ok.values())} writes in {SECONDS:.0f}s, retryable 503s by op: {dict(retried)}")
    assert bad == []  # no 500, no 503 without Retry-After, nothing else
    assert "event=db_error" not in caplog.text
    for uid, _c, items, _chat in people:
        for op in OPS:
            assert ok[uid, op] >= 3, f"user {uid} made no progress with {op}: {ok}"
        with Session(engine) as s:
            jobs = s.exec(select(func.count()).select_from(IngestJob).where(IngestJob.user_id == uid)).one()
            asked = s.exec(
                select(func.count())
                .select_from(ChatMessage)
                .where(ChatMessage.user_id == uid, ChatMessage.role == "user")
            ).one()
            wears = s.exec(select(func.count()).select_from(WearLog).where(WearLog.user_id == uid)).one()
            note = s.get(Item, items[0]).notes
        assert jobs >= ok[uid, "upload"]
        assert asked >= ok[uid, "chat"]
        assert wears >= ok[uid, "wear"]
        assert note == last_note[uid]  # the last edit that was acknowledged is the one stored


def test_a_write_sqlite_refuses_at_once_is_a_503_with_retry_after_not_a_500(client, add_route, app):
    """A transaction that read, then lost its snapshot to another writer's commit, gets SQLITE_BUSY without waiting."""
    from kyapehnu.core.deps import SessionDep

    engine = app.state.ctx.engine

    def stale(s: SessionDep):
        conn = s.connection()
        conn.exec_driver_sql("BEGIN")
        conn.execute(text("SELECT count(*) FROM item")).all()  # the snapshot is taken here
        with Session(engine) as other:  # another request writes and commits
            add_item(other, name="Written meanwhile")
        conn.execute(text("UPDATE item SET notes = 'late'"))  # a write on a stale snapshot: refused at once
        return {"ok": True}

    add_route("/qa/stale", stale, methods=("POST",))
    start = time.monotonic()

    r = client.post("/qa/stale")

    assert time.monotonic() - start < 3  # refused, not waited out (the busy timeout is 5 s)
    assert (r.status_code, r.json()["code"]) == (503, "db_busy")
    assert int(r.headers["Retry-After"]) >= 1


@pytest.mark.slow
def test_a_write_lock_held_past_the_busy_timeout_gives_503_and_the_next_write_after_it_succeeds(client, app):
    """Another process (a backup, a stuck writer) holds the database's write lock for longer than the 5 s timeout."""
    import sqlite3

    from kyapehnu.core.db import BUSY_TIMEOUT_S, DB_FILE_NAME

    holder = sqlite3.connect(app.state.settings.data_dir / DB_FILE_NAME, timeout=0, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        start = time.monotonic()
        blocked = client.post("/api/events", json={"type": "party", "title": "x", "starts_at": "2099-01-01T10:00"})
        waited = time.monotonic() - start
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert (blocked.status_code, blocked.json()["code"]) == (503, "db_busy")
    assert int(blocked.headers["Retry-After"]) >= 1
    assert BUSY_TIMEOUT_S - 1 <= waited < BUSY_TIMEOUT_S + 3
    retry = client.post("/api/events", json={"type": "party", "title": "x", "starts_at": "2099-01-01T10:00"})
    assert retry.status_code == 201  # and nothing was left stuck
