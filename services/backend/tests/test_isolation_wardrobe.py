"""Wardrobe isolation between two accounts (AC-17, AC-18, AC-20, AC-21; LLD 4.2, 9.2).

User A owns a complete wardrobe (a row of every owned table, with image files). User B is signed in too and names A's
ids on every route that takes one: each answers 404 `not_found` and changes nothing, rows and files alike. Lists show
only the caller's records. A's image URLs give B 404 and a signed-out client 401.
"""

import threading
from datetime import date, datetime

import pytest
from sqlmodel import Session

from kyapehnu.core.errors import NotFoundError
from kyapehnu.wardrobe import public as wardrobe
from kyapehnu.wardrobe.models import Event, Feedback, Item, ItemStatus, Outfit, WearLog
from tests.conftest import noise_image_bytes
from tests.fakes import analysis, garment
from tests.owner_helpers import add_item, image_files, me, rows_of, seed_world, snapshot

EVENT = {"type": "party", "starts_at": "2026-10-20T19:00:00", "place": "Pune"}


@pytest.fixture
def ctx(app):
    return app.state.ctx


@pytest.fixture
def a(client_a, ctx):
    """A's complete wardrobe."""
    return seed_world(ctx, me(client_a), "a")


@pytest.fixture
def b(client_b, ctx, a):
    """B's complete wardrobe (B is created after A)."""
    return seed_world(ctx, me(client_b), "b")


# --- routes that take an id: B names A's ----------------------------------------------------------------------------

# name -> f(a, b) -> (method, url, json body). Everything here names an id of A (alone or next to B's own).
FOREIGN = {
    "item-get": lambda a, b: ("GET", f"/api/items/{a.top}", None),
    "item-patch": lambda a, b: ("PATCH", f"/api/items/{a.top}", {"name": "hacked"}),
    "item-status": lambda a, b: ("POST", f"/api/items/{a.top}/status", {"status": "retired"}),
    "item-worn-today": lambda a, b: ("POST", f"/api/items/{a.top}/worn-today", None),
    "item-delete-soft": lambda a, b: ("DELETE", f"/api/items/{a.top}", None),
    "item-delete-hard": lambda a, b: ("DELETE", f"/api/items/{a.top}?hard=true", None),
    "item-clean-picture": lambda a, b: ("POST", f"/api/items/{a.top}/clean-picture", None),
    "item-clean-picture-switch": lambda a, b: ("POST", f"/api/items/{a.top}/clean-picture/switch", None),
    "item-clean-picture-delete": lambda a, b: ("DELETE", f"/api/items/{a.top}/clean-picture", None),
    "items-bulk-status": lambda a, b: (
        "POST",
        "/api/items/status",
        {"item_ids": [b.top, a.top], "status": "retired"},
    ),
    "job-get": lambda a, b: ("GET", f"/api/ingest/jobs/{a.job}", None),
    "piece-accept": lambda a, b: ("POST", f"/api/ingest/pieces/{a.piece}/accept", {}),
    "piece-discard": lambda a, b: ("POST", f"/api/ingest/pieces/{a.piece}/discard", None),
    "piece-merge": lambda a, b: ("POST", f"/api/ingest/pieces/{a.piece}/merge", {"other_piece_ids": [b.piece]}),
    "piece-merge-foreign-other": lambda a, b: (
        "POST",
        f"/api/ingest/pieces/{b.piece}/merge",
        {"other_piece_ids": [a.piece]},
    ),
    "piece-mark-duplicate": lambda a, b: (
        "POST",
        f"/api/ingest/pieces/{a.piece}/mark-duplicate",
        {"item_id": b.top},
    ),
    "piece-mark-duplicate-of-foreign-item": lambda a, b: (
        "POST",
        f"/api/ingest/pieces/{b.piece}/mark-duplicate",
        {"item_id": a.top},
    ),
    "piece-clean-picture": lambda a, b: ("POST", f"/api/ingest/pieces/{a.piece}/clean-picture", None),
    "piece-clean-picture-switch": lambda a, b: ("POST", f"/api/ingest/pieces/{a.piece}/clean-picture/switch", None),
    "piece-clean-picture-delete": lambda a, b: ("DELETE", f"/api/ingest/pieces/{a.piece}/clean-picture", None),
    "photo-retry": lambda a, b: ("POST", f"/api/ingest/photos/{a.photo}/retry", None),
    "wears-of-item": lambda a, b: ("GET", f"/api/wears?item_id={a.top}&days=365", None),
    "wears-of-items": lambda a, b: ("GET", f"/api/wears?item_ids={a.top}&item_ids={a.bottom}&days=365", None),
    "suggest-for-event": lambda a, b: ("POST", "/api/outfits/suggest", {"event_id": a.event}),
    "suggest-include-item": lambda a, b: (
        "POST",
        "/api/outfits/suggest",
        {"event": EVENT, "include": [a.top]},
    ),
    "suggest-own-event-include-foreign-item": lambda a, b: (
        "POST",
        "/api/outfits/suggest",
        {"event_id": b.event, "include": [b.top, a.bottom]},
    ),
    "swap-foreign-event": lambda a, b: (
        "POST",
        "/api/outfits/swap",
        {"event_id": a.event, "item_ids": b.items, "item_id": b.top},
    ),
    "swap-foreign-items": lambda a, b: (
        "POST",
        "/api/outfits/swap",
        {"event_id": b.event, "item_ids": a.items, "item_id": a.top},
    ),
    "swap-one-foreign-item": lambda a, b: (
        "POST",
        "/api/outfits/swap",
        {"event_id": b.event, "item_ids": [b.top, a.bottom], "item_id": b.top},
    ),
    "swap-exclude-is-not-an-escape": lambda a, b: (
        "POST",
        "/api/outfits/swap",
        {"event_id": b.event, "item_ids": [a.top, b.bottom], "item_id": b.bottom, "exclude_item_ids": [b.top]},
    ),
    "wear-foreign-items": lambda a, b: ("POST", "/api/outfits/wear", {"item_ids": a.items}),
    "wear-one-foreign-item": lambda a, b: ("POST", "/api/outfits/wear", {"item_ids": [b.top, a.bottom]}),
    "wear-foreign-event": lambda a, b: ("POST", "/api/outfits/wear", {"item_ids": b.items, "event_id": a.event}),
    "mannequin-foreign-items": lambda a, b: ("POST", "/api/outfits/mannequin", {"item_ids": a.items}),
    "feedback-foreign-outfit": lambda a, b: ("POST", "/api/feedback", {"outfit_id": a.outfit, "rating": 1}),
    "feedback-foreign-items": lambda a, b: ("POST", "/api/feedback", {"item_ids": a.items, "rating": -1}),
}


@pytest.mark.parametrize("case", FOREIGN)
def test_naming_another_users_id_is_a_404_and_changes_nothing(client_b, ctx, a, b, case):
    method, url, body = FOREIGN[case](a, b)
    before = snapshot(ctx)

    r = client_b.request(method, url, json=body)

    assert r.status_code == 404, r.text
    assert r.json()["code"] == "not_found"
    assert snapshot(ctx) == before  # no row and no file of either user changed


def test_the_same_requests_as_the_owner_do_not_fail_with_404(client_a, a, b):
    """Guard against the table above passing for the wrong reason (a route that is 404 for everybody)."""
    for method, url in [
        ("GET", f"/api/items/{a.top}"),
        ("GET", f"/api/ingest/jobs/{a.job}"),
        ("GET", f"/api/wears?item_id={a.top}&days=365"),
    ]:
        assert client_a.request(method, url).status_code == 200
    assert client_a.patch(f"/api/items/{a.top}", json={"name": "renamed"}).json()["name"] == "renamed"
    assert client_a.post(f"/api/ingest/pieces/{a.piece}/discard").status_code == 200
    assert client_a.post("/api/feedback", json={"outfit_id": a.outfit, "rating": -1}).status_code == 201


def test_a_user_still_edits_and_hard_deletes_their_own_item_and_only_their_files_go(client_b, ctx, a, b):
    assert client_b.patch(f"/api/items/{b.top}", json={"name": "mine"}).json()["name"] == "mine"
    assert client_b.delete(f"/api/items/{b.top}?hard=true").status_code == 204

    files = image_files(ctx.data_dir)
    assert not any(f in files for f in (f"images/{b.user_id}/cutout/btop.png", f"images/{b.user_id}/thumb/btop.webp"))
    assert all(rel in files for rel in a.files)  # A's files are all still there
    assert [r.id for r in rows_of(ctx, Item, a.user_id)] == a.items
    assert [r.name for r in rows_of(ctx, Item, a.user_id)] == ["a shirt", "a chinos"]


# --- lists show the caller's records only ---------------------------------------------------------------------------


def _ids(response):
    assert response.status_code == 200, response.text
    return sorted(row["id"] for row in response.json())


def test_item_list_shows_only_the_callers_items_with_their_own_user_id(client_b, a, b):
    r = client_b.get("/api/items")

    assert _ids(r) == sorted(b.items)
    assert {row["user_id"] for row in r.json()} == {b.user_id}


@pytest.mark.parametrize("query", ["category=top", "q=shirt", "q=a", "status=available", "not_worn_days=1"])
def test_item_filters_never_reach_into_another_users_items(client_b, a, b, query):
    ids = _ids(client_b.get(f"/api/items?{query}"))

    assert set(ids) <= set(b.items)
    assert not set(ids) & set(a.items)


def test_not_worn_days_lists_the_callers_idle_items_and_none_of_anyone_elses(client_b, ctx, a, b):
    extra = add_item(ctx, b.user_id, name="idle", category="bottom")
    a_idle = add_item(ctx, a.user_id, name="a idle", category="bottom")  # never worn, so it matches the filter

    ids = _ids(client_b.get("/api/items?not_worn_days=30"))

    assert extra.id in ids
    assert a_idle.id not in ids
    assert not set(ids) & set(a.items)


def test_job_and_piece_lists_show_only_the_callers_records(client_b, a, b):
    jobs = client_b.get("/api/ingest/jobs")
    pieces = client_b.get("/api/ingest/pieces")

    assert _ids(jobs) == [b.job]
    assert _ids(pieces) == [b.piece]
    assert {row["user_id"] for row in jobs.json() + pieces.json()} == {b.user_id}


def test_piece_list_filtered_by_a_foreign_job_is_empty(client_b, a, b):
    r = client_b.get(f"/api/ingest/pieces?job_id={a.job}")

    assert _ids(r) == []


def test_event_list_shows_only_the_callers_events(client_b, a, b):
    r = client_b.get("/api/events")

    assert _ids(r) == [b.event]
    assert {row["user_id"] for row in r.json()} == {b.user_id}


def test_outfit_list_shows_only_the_callers_outfits_and_their_items(client_b, a, b):
    r = client_b.get("/api/outfits")

    assert _ids(r) == [b.outfit]
    assert sorted(i["id"] for i in r.json()[0]["items"]) == sorted(b.items)


def test_wear_history_shows_only_the_callers_wears(client_b, a, b):
    r = client_b.get(f"/api/wears?item_id={b.top}&days=365")

    assert _ids(r) == [b.wear]


def test_a_new_event_belongs_to_the_caller_and_stays_out_of_the_other_list(client_a, client_b, a, b):
    r = client_b.post("/api/events", json=EVENT | {"title": "B only"})

    assert r.status_code == 201
    assert r.json()["user_id"] == b.user_id
    assert r.json()["id"] not in _ids(client_a.get("/api/events"))
    assert [e["title"] for e in client_a.get("/api/events").json()] == ["a dinner"]


def test_a_saved_wear_and_its_outfit_and_feedback_belong_to_the_caller(client_a, client_b, ctx, a, b):
    r = client_b.post("/api/outfits/wear", json={"item_ids": [b.top]})
    assert r.status_code == 201, r.text
    outfit_id = r.json()["id"]

    assert client_b.post("/api/feedback", json={"outfit_id": outfit_id, "rating": 1}).status_code == 201

    assert {o.id for o in rows_of(ctx, Outfit, b.user_id)} >= {outfit_id}
    assert outfit_id not in {o.id for o in rows_of(ctx, Outfit, a.user_id)}
    assert len(rows_of(ctx, WearLog, b.user_id)) == 2
    assert len(rows_of(ctx, Feedback, b.user_id)) == 2
    assert len(rows_of(ctx, WearLog, a.user_id)) == 1
    assert len(rows_of(ctx, Feedback, a.user_id)) == 1
    assert outfit_id not in _ids(client_a.get("/api/outfits"))


def test_a_suggestion_for_b_is_built_from_b_items_only(client_b, ctx, a):
    """B owns a top but no bottom; A owns both. B's outfit cannot borrow A's bottom."""
    b_id = me(client_b)
    add_item(ctx, b_id, name="b top", category="top")
    with Session(ctx.engine) as s:
        event = Event(user_id=b_id, type="party", title="B", starts_at=datetime(2026, 10, 20, 19), place="Pune")
        s.add(event)
        s.commit()
        event_id = event.id

    r = client_b.post("/api/outfits/suggest", json={"event_id": event_id})

    assert r.status_code == 200, r.text
    assert r.json()["suggestions"] == []
    assert r.json()["problem"] is not None


# --- image URLs ------------------------------------------------------------------------------------------------------

A_FILES = [
    "images/{a}/original/aorig.jpg",
    "images/{a}/cutout/atop.png",
    "images/{a}/thumb/atop.webp",
    "images/{a}/collage/acollage.webp",
]


@pytest.mark.parametrize("pattern", A_FILES)
def test_another_users_image_is_a_404_without_bytes(client_a, client_b, a, b, pattern):
    rel = pattern.format(a=a.user_id)

    r_b = client_b.get(f"/{rel}")
    r_a = client_a.get(f"/{rel}")

    assert r_b.status_code == 404
    assert r_b.json()["code"] == "not_found"
    assert a.files[rel] not in r_b.content
    assert r_a.status_code == 200
    assert r_a.content == a.files[rel]


@pytest.mark.parametrize("pattern", A_FILES)
def test_a_signed_out_client_gets_401_for_an_image_that_exists(anon_client, a, pattern):
    rel = pattern.format(a=a.user_id)

    r = anon_client.get(f"/{rel}")

    assert r.status_code == 401
    assert r.json()["code"] == "not_authenticated"
    assert a.files[rel] not in r.content


def test_b_asking_for_a_name_under_their_own_id_does_not_reach_a_files(client_b, a, b):
    r = client_b.get(f"/images/{b.user_id}/cutout/atop.png")  # the name exists only in A's directory

    assert r.status_code == 404


# --- two users working at the same time ------------------------------------------------------------------------------


def _upload(client, seeds):
    files = [("files", (f"p{s}.jpg", noise_image_bytes(s), "image/jpeg")) for s in seeds]
    r = client.post("/api/ingest", files=files)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_two_users_uploading_at_once_keep_separate_jobs_pieces_and_items(app, client_a, client_b, fake, ctx):
    fake.replies.extend([analysis(garment(name=f"shirt {i}")) for i in range(6)])
    a_id, b_id = me(client_a), me(client_b)
    jobs: dict[str, int] = {}
    barrier = threading.Barrier(2)

    def go(key, client, seeds):
        barrier.wait()
        jobs[key] = _upload(client, seeds)

    threads = [
        threading.Thread(target=go, args=("a", client_a, [11, 12, 13])),
        threading.Thread(target=go, args=("b", client_b, [21, 22, 23])),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    worker = app.state.worker
    worker.start()
    worker.drain()
    worker.stop()

    for client, user_id, key in ((client_a, a_id, "a"), (client_b, b_id, "b")):
        pieces = client.get("/api/ingest/pieces").json()
        assert len(pieces) == 3
        assert {p["job_id"] for p in pieces} == {jobs[key]}
        assert {p["user_id"] for p in pieces} == {user_id}
        assert all(p["cutout_path"].startswith(f"images/{user_id}/cutout/") for p in pieces)
        assert [j["id"] for j in client.get("/api/ingest/jobs").json()] == [jobs[key]]
        for p in pieces:
            assert client.post(f"/api/ingest/pieces/{p['id']}/accept", json={}).status_code == 200
        items = client.get("/api/items").json()
        assert len(items) == 3
        assert {i["user_id"] for i in items} == {user_id}
        assert {i["thumb_path"].split("/")[1] for i in items} == {str(user_id)}
    assert len(rows_of(ctx, Item, a_id)) == 3
    assert len(rows_of(ctx, Item, b_id)) == 3
    assert not [f for f in image_files(ctx.data_dir) if f.count("/") != 3]  # images/<id>/<kind>/<name> only


def test_ingest_of_one_user_leaves_the_other_users_files_untouched(client_a, client_b, app, fake, ctx, a):
    fake.replies.append(analysis(garment()))
    before = {rel: (ctx.data_dir / rel).read_bytes() for rel in a.files}

    _upload(client_b, [31])
    app.state.worker.start()
    app.state.worker.drain()
    app.state.worker.stop()

    assert {rel: (ctx.data_dir / rel).read_bytes() for rel in a.files} == before
    new = [f for f in image_files(ctx.data_dir) if f not in a.files]
    assert new
    assert all(f.startswith(f"images/{me(client_b)}/") for f in new)


# --- the public port (what the chat tools and core call) ------------------------------------------------------------

TODAY = date(2026, 10, 12)


@pytest.fixture
def db(ctx):
    with Session(ctx.engine) as s:
        yield s


def test_list_items_returns_only_the_callers_items(db, a, b):
    assert sorted(i.id for i in wardrobe.list_items(b.user_id, db, wardrobe.WardrobeQuery())) == sorted(b.items)
    assert sorted(i.id for i in wardrobe.list_items(a.user_id, db, wardrobe.WardrobeQuery())) == sorted(a.items)


def test_get_item_of_another_user_is_not_found_and_the_owners_own_works(db, a, b):
    with pytest.raises(NotFoundError):
        wardrobe.get_item(b.user_id, db, a.top)

    assert wardrobe.get_item(a.user_id, db, a.top).id == a.top


def test_find_items_leaves_out_another_users_ids(db, a, b):
    assert set(wardrobe.find_items(b.user_id, db, [a.top, b.top])) == {b.top}


@pytest.mark.parametrize("ids", [lambda a, b: [a.top], lambda a, b: [b.top, a.bottom], lambda a, b: [b.top, 987654]])
def test_require_owned_items_refuses_a_foreign_or_missing_id(db, a, b, ids):
    with pytest.raises(NotFoundError):
        wardrobe.require_owned_items(b.user_id, db, ids(a, b))


def test_require_owned_items_returns_the_callers_items(db, a, b):
    assert set(wardrobe.require_owned_items(b.user_id, db, b.items)) == set(b.items)
    assert wardrobe.require_owned_items(b.user_id, db, []) == {}


def test_set_status_of_another_users_item_is_not_found_and_changes_nothing(db, ctx, a, b):
    before = snapshot(ctx)

    with pytest.raises(NotFoundError):
        wardrobe.set_status(b.user_id, db, a.top, ItemStatus.retired)

    assert snapshot(ctx) == before


def test_events_and_jobs_listed_through_the_port_are_the_callers_own(db, a, b):
    assert set(wardrobe.find_events(b.user_id, db, [a.event, b.event])) == {b.event}
    assert [e.id for e in wardrobe.list_events(b.user_id, db)] == [b.event]
    assert wardrobe.get_event(a.user_id, db, a.event).id == a.event
    assert wardrobe.get_job(a.user_id, db, a.job).id == a.job


def test_create_event_stamps_the_caller(db, b):
    event = wardrobe.create_event(b.user_id, db, wardrobe.EventDraft(**EVENT), "Pune")

    assert event.user_id == b.user_id


def test_ratings_by_key_sees_only_the_callers_feedback(db, a, b):
    key = ",".join(map(str, sorted(a.items)))

    assert wardrobe.ratings_by_key(b.user_id, db, [key]) == {}
    assert wardrobe.ratings_by_key(a.user_id, db, [key]) == {key: 1}


# name -> f(db, ctx, a, b): B names A's record through the port.
PORT_REFUSES = {
    "get_event": lambda db, ctx, a, b: wardrobe.get_event(b.user_id, db, a.event),
    "get_job": lambda db, ctx, a, b: wardrobe.get_job(b.user_id, db, a.job),
    "item_history": lambda db, ctx, a, b: wardrobe.item_history(b.user_id, db, a.top),
    "list_wears": lambda db, ctx, a, b: wardrobe.list_wears(
        b.user_id, db, wardrobe.WearsQuery(item_id=a.top, days=365), TODAY
    ),
    "suggest": lambda db, ctx, a, b: wardrobe.suggest(b.user_id, ctx, db, wardrobe.SuggestIn(event_id=a.event), TODAY),
    "swap": lambda db, ctx, a, b: wardrobe.swap(
        b.user_id, ctx, db, wardrobe.SwapIn(event_id=b.event, item_ids=a.items, item_id=a.top), TODAY
    ),
    "wear": lambda db, ctx, a, b: wardrobe.wear(b.user_id, ctx, db, wardrobe.WearIn(item_ids=a.items), TODAY),
    "give_feedback": lambda db, ctx, a, b: wardrobe.give_feedback(
        b.user_id, ctx, db, wardrobe.FeedbackIn(outfit_id=a.outfit, rating=1)
    ),
}


@pytest.mark.parametrize("name", PORT_REFUSES)
def test_the_port_refuses_another_users_record_as_not_found(db, ctx, a, b, name):
    with pytest.raises(NotFoundError):
        PORT_REFUSES[name](db, ctx, a, b)
