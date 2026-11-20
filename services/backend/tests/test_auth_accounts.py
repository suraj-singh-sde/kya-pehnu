"""Accounts (LLD 3.2 `upsert_google_user`, `user_id_for_email`; AC-4, AC-10, L2): one account per Google `sub`, the
first is the admin even when sign-ins race, and Google's text is cleaned before it is stored."""

import threading
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from kyapehnu.auth.accounts import upsert_google_user, user_id_for_email
from kyapehnu.auth.models import User
from kyapehnu.core.db import make_engine, migrate, utcnow


@pytest.fixture
def s(engine):
    with Session(engine) as session:
        yield session


def sign(s, sub, email="a@example.com", name="Ada", now=None):
    return upsert_google_user(s, sub=sub, email=email, name=name, now=utcnow() if now is None else now)


def count(s):
    return len(s.exec(select(User)).all())


def test_the_first_account_is_the_admin_and_the_second_is_not(s):
    first = sign(s, "sub-1")
    second = sign(s, "sub-2")

    assert (first.is_admin, second.is_admin) == (True, False)


def test_signing_in_again_keeps_the_account_and_its_role(s):
    first = sign(s, "sub-1")
    second = sign(s, "sub-2")

    assert sign(s, "sub-1").id == first.id
    assert sign(s, "sub-2").id == second.id
    assert [u.is_admin for u in s.exec(select(User).order_by(User.id)).all()] == [True, False]


def test_a_changed_email_at_google_reaches_the_same_account(s):
    first = sign(s, "sub-1", email="old@example.com", name="Old Name")

    again = sign(s, "sub-1", email="new@example.com", name="New Name")

    assert again.id == first.id
    assert count(s) == 1
    s.expire_all()
    row = s.get(User, first.id)
    assert (row.email, row.display_name) == ("new@example.com", "New Name")


def test_the_sign_in_time_moves_forward_and_the_creation_time_stays(s):
    t0 = utcnow()
    first = sign(s, "sub-1", now=t0)
    created = first.created_at.replace(tzinfo=None)

    sign(s, "sub-1", now=t0 + timedelta(hours=1))

    s.expire_all()
    row = s.get(User, first.id)
    assert row.last_sign_in_at.replace(tzinfo=None) == (t0 + timedelta(hours=1)).replace(tzinfo=None)
    assert row.created_at.replace(tzinfo=None) == created


def test_a_name_loses_its_control_characters_and_is_cut_to_200(s):
    user = sign(s, "sub-1", name="A\x00B\x1bC\nD\t" + "x" * 500)

    assert user.display_name == "ABCD" + "x" * 196


def test_an_email_loses_its_control_characters_and_is_cut_to_320(s):
    user = sign(s, "sub-1", email="a\x00b@" + "x" * 400 + ".com")

    assert user.email == ("ab@" + "x" * 400 + ".com")[:320]
    assert len(user.email) == 320


def test_a_name_loses_its_format_characters_such_as_right_to_left_override(s):
    user = sign(s, "sub-1", name="Ada\u202eEvil\u200b\ufeff")

    assert user.display_name == "AdaEvil"


def test_surrounding_spaces_are_stripped(s):
    user = sign(s, "sub-1", email="  a@example.com ", name="  Ada  ")

    assert (user.email, user.display_name) == ("a@example.com", "Ada")


def test_the_cleaning_applies_to_a_returning_user_too(s):
    sign(s, "sub-1")

    again = sign(s, "sub-1", name="Bad\x00\x1bName" + "y" * 300)

    assert again.display_name == "BadName" + "y" * 193


def test_two_first_sign_ins_at_once_make_exactly_one_admin(tmp_path):
    engine = make_engine(tmp_path)
    migrate(engine)
    workers = 6
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def run(n: int) -> None:
        try:
            with Session(engine) as session:
                barrier.wait()
                upsert_google_user(session, sub=f"racer-{n}", email=f"r{n}@example.com", name="R", now=utcnow())
        except BaseException as e:  # noqa: BLE001 - reported below
            errors.append(e)

    threads = [threading.Thread(target=run, args=(n,)) for n in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert errors == []
    with Session(engine) as session:
        users = session.exec(select(User)).all()
    assert len(users) == workers
    assert sum(u.is_admin for u in users) == 1


def test_the_database_refuses_a_second_admin(s):
    sign(s, "sub-1")  # the admin
    s.add(User(google_sub="sub-2", email="b@example.com", display_name="B", is_admin=True))

    with pytest.raises(IntegrityError):
        s.commit()


def test_the_database_refuses_a_second_account_for_one_google_id(s):
    sign(s, "sub-1")
    s.add(User(google_sub="sub-1", email="b@example.com", display_name="B"))

    with pytest.raises(IntegrityError):
        s.commit()


def test_an_account_is_found_by_its_email(s):
    user = sign(s, "sub-1", email="ada@example.com")
    sign(s, "sub-2", email="bob@example.com")

    assert user_id_for_email(s, "ada@example.com") == user.id


@pytest.mark.parametrize("emails", [[], ["x@example.com", "x@example.com"]])
def test_no_match_or_an_ambiguous_match_is_a_lookup_error_without_the_email(s, emails):
    for n, email in enumerate(emails):
        sign(s, f"sub-{n}", email=email)

    with pytest.raises(LookupError) as raised:
        user_id_for_email(s, "x@example.com")

    assert "x@example.com" not in str(raised.value)


def test_the_user_table_is_called_user(s):
    sign(s, "sub-1")

    assert s.exec(text('SELECT COUNT(*) FROM "user"')).one()[0] == 1


def test_the_google_picture_is_kept_and_refreshed_on_the_next_sign_in(s):
    first = upsert_google_user(
        s, sub="sub-1", email="a@example.com", name="A", now=utcnow(), picture="https://lh3.googleusercontent.com/a/one"
    )
    assert first.avatar_url == "https://lh3.googleusercontent.com/a/one"

    again = upsert_google_user(
        s, sub="sub-1", email="a@example.com", name="A", now=utcnow(), picture="https://lh3.googleusercontent.com/a/two"
    )
    assert again.avatar_url == "https://lh3.googleusercontent.com/a/two"


@pytest.mark.parametrize(
    "picture", [None, "", "http://insecure.example/p.png", "javascript:alert(1)", "https://x/" + "a" * 2100]
)
def test_a_missing_or_unusable_picture_is_stored_as_none(s, picture):
    user = upsert_google_user(s, sub="sub-1", email="a@example.com", name="A", now=utcnow(), picture=picture)
    assert user.avatar_url is None
