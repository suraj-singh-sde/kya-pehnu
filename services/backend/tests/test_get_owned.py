"""`get_owned`: a missing row and another user's row are both a 404 (FR-11)."""

from types import SimpleNamespace

import pytest

from kyapehnu.core.db import get_owned
from kyapehnu.core.errors import NotFoundError


class _Session:
    def __init__(self, rows: dict[int, object]) -> None:
        self.rows = rows

    def get(self, _model: type, ident: int) -> object | None:
        return self.rows.get(ident)


class _Row: ...


def test_the_owner_gets_the_row():
    row = SimpleNamespace(user_id=7)
    assert get_owned(7, _Session({1: row}), _Row, 1) is row


@pytest.mark.parametrize("ident", [1, 2])
def test_another_users_row_and_a_missing_row_look_the_same(ident):
    session = _Session({1: SimpleNamespace(user_id=8)})
    with pytest.raises(NotFoundError) as caught:
        get_owned(7, session, _Row, ident)
    assert str(caught.value) == f"_Row {ident} not found"
