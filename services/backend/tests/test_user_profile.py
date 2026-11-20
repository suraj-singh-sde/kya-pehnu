"""One profile per account (LLD 3.4, 5.2; AC-12): a new account starts at onboarding and each person reads and changes
only their own row."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from kyapehnu.core.config import Prefs
from kyapehnu.user.models import UserProfile
from kyapehnu.user.public import ensure_profile, get_profile
from tests.conftest import sign_in

A_PROFILE = {"name": "Asha", "gender": "female", "home_city": "Pune", "style_prefs": "minimal", "budget_max": 9000}


def row_of(app, user_id):
    with app.state.ctx.engine.connect() as conn:
        return conn.exec_driver_sql("SELECT * FROM userprofile WHERE user_id = ?", (user_id,)).one()


def test_each_user_reads_their_own_profile(app, client_a, client_b):
    client_a.put("/api/profile", json=A_PROFILE)

    mine, theirs = client_a.get("/api/profile").json(), client_b.get("/api/profile").json()

    assert mine["name"] == "Asha" and mine["home_city"] == "Pune"
    assert theirs["name"] == "" and theirs["gender"] == ""
    assert mine["user_id"] == client_a.get("/api/auth/me").json()["id"]
    assert theirs["user_id"] == client_b.get("/api/auth/me").json()["id"]
    assert mine["id"] != theirs["id"]


def test_b_changing_the_profile_changes_only_bs_row(app, client_a, client_b):
    client_a.put("/api/profile", json=A_PROFILE)
    a_id, b_id = (c.get("/api/auth/me").json()["id"] for c in (client_a, client_b))
    a_before, b_before = row_of(app, a_id), row_of(app, b_id)

    r = client_b.put("/api/profile", json={"name": "Bela", "gender": "male", "home_city": "Delhi"})

    assert r.status_code == 200
    assert row_of(app, a_id) == a_before  # A's row is byte-equal
    assert row_of(app, b_id) != b_before
    assert client_b.get("/api/profile").json()["name"] == "Bela"
    assert client_a.get("/api/profile").json()["name"] == "Asha"


def test_the_body_cannot_name_another_owner(app, client_a, client_b):
    a_id, b_id = (c.get("/api/auth/me").json()["id"] for c in (client_a, client_b))
    b_before = row_of(app, b_id)

    r = client_a.put("/api/profile", json={**A_PROFILE, "user_id": b_id})

    assert r.status_code == 200
    assert r.json()["user_id"] == a_id
    assert row_of(app, b_id) == b_before


def test_a_new_account_has_the_default_profile_and_is_at_onboarding(app):
    user_id, c = sign_in(app, "sub-new")

    profile = c.get("/api/profile").json()

    assert profile["gender"] == "" and profile["name"] == ""
    assert profile["home_city"] == Prefs().home_city
    assert (profile["budget_min"], profile["budget_max"]) == (Prefs().profile.budget_min, Prefs().profile.budget_max)
    assert profile["user_id"] == user_id


def test_ensure_profile_makes_one_row_per_user_and_keeps_changes(app):
    user_id, _ = sign_in(app, "sub-x")
    prefs = app.state.ctx.prefs

    with Session(app.state.ctx.engine) as s:
        first = ensure_profile(user_id, s, prefs)
        first.name = "Changed"
        s.add(first)
        s.commit()
        again = ensure_profile(user_id, s, prefs)
        rows = s.exec(select(UserProfile).where(UserProfile.user_id == user_id)).all()
        assert get_profile(user_id, s).name == "Changed"

    assert len(rows) == 1 and again.name == "Changed"


def test_a_profile_made_by_a_concurrent_request_is_used_not_a_500(app):
    user_id, _ = sign_in(app, "sub-z")
    engine = app.state.ctx.engine
    with Session(engine) as s:
        s.delete(get_profile(user_id, s))
        s.commit()

    with Session(engine) as s:
        add = s.add

        def add_after_a_rival_wins(row):
            with Session(engine) as rival:  # the other tab commits between our read and our insert
                rival.add(UserProfile(user_id=user_id, name="Rival"))
                rival.commit()
            add(row)

        s.add = add_after_a_rival_wins
        profile = ensure_profile(user_id, s, app.state.ctx.prefs)

        assert profile.name == "Rival"
        assert len(s.exec(select(UserProfile).where(UserProfile.user_id == user_id)).all()) == 1


def test_two_profiles_for_one_user_are_refused_by_the_database(app):
    user_id, _ = sign_in(app, "sub-y")

    with Session(app.state.ctx.engine) as s, pytest.raises(IntegrityError):
        s.add(UserProfile(user_id=user_id))
        s.commit()


# ---- almirah finish and shelf layout (saved on the profile) --------------------------------------------------------


def _profile(c):
    return c.get("/api/profile").json()


def test_the_finish_defaults_to_teal_and_accepts_a_preset(client_a):
    p = _profile(client_a)
    assert p["theme"] == "teal" and p["layout"] is None

    assert client_a.put("/api/profile", json={**p, "theme": "walnut"}).json()["theme"] == "walnut"
    assert client_a.put("/api/profile", json={**p, "theme": "neon"}).status_code == 422


def test_a_shelf_layout_round_trips_and_is_validated(client_a):
    p = _profile(client_a)
    layout = {
        "shelves": [
            {"name": "Bags", "kind": "shelf", "width": "full", "cats": ["bag"]},
            {"name": "Caps", "kind": "shelf", "width": "full", "cats": ["accessory"], "match": "caps_glasses"},
            {"name": "Sarees", "kind": "shelf", "width": "half", "cats": ["ethnic"], "match": "sarees"},
            {"name": "Jewellery", "kind": "locker", "width": "half", "cats": ["accessory"], "match": "jewellery"},
            {"name": "Everyday", "kind": "rail", "width": "half", "cats": ["top", "outerwear"]},
            {"name": "Bottoms", "kind": "drawer", "width": "half", "cats": ["bottom"]},
        ]
    }

    saved = client_a.put("/api/profile", json={**p, "layout": layout}).json()["layout"]
    assert saved == layout
    assert client_a.put("/api/profile", json={**p, "layout": None}).json()["layout"] is None

    bad = [
        {"shelves": []},  # at least one shelf
        {"shelves": [{"name": "", "kind": "rail", "width": "full", "cats": ["top"]}]},  # a name
        {"shelves": [{"name": "x" * 31, "kind": "rail", "width": "full", "cats": ["top"]}]},  # short name
        {"shelves": [{"name": "A", "kind": "cupboard", "width": "full", "cats": ["top"]}]},  # known kind
        {"shelves": [{"name": "A", "kind": "rail", "width": "wide", "cats": ["top"]}]},  # known width
        {"shelves": [{"name": "A", "kind": "rail", "width": "full", "cats": ["hats"]}]},  # known category
        {"shelves": [{"name": "A", "kind": "rail", "width": "full", "cats": []}]},  # holds something
        {"shelves": [{"name": "A", "kind": "shelf", "width": "full", "cats": ["top"], "match": "hats"}]},  # known list
        {
            "shelves": [{"name": f"S{i}", "kind": "shelf", "width": "full", "cats": ["top"]} for i in range(13)]
        },  # at most 12
        {"rows": []},  # the shape
        [],
    ]
    for layout in bad:
        assert client_a.put("/api/profile", json={**p, "layout": layout}).status_code == 422, layout


def test_the_shelf_categories_are_the_wardrobe_categories():
    from kyapehnu.user.models import SHELF_CATEGORIES
    from kyapehnu.wardrobe.models import Category

    assert SHELF_CATEGORIES == frozenset(c.value for c in Category)
