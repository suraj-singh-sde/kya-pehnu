"""AC-3, AC-41: the backend runs by itself. No frontend build, no static mount except the image files."""

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount


@pytest.fixture
def alone(app):
    with TestClient(app, base_url="http://localhost") as c:
        yield c


def test_livez_and_readyz_answer_ok(alone):
    assert alone.get("/livez").json() == {"status": "ok"}
    assert alone.get("/readyz").status_code == 200
    assert alone.get("/readyz").json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/", "/index.html", "/wardrobe"])
def test_the_backend_serves_no_single_page_app(alone, path):
    assert alone.get(path).status_code == 404


def test_no_directory_is_mounted_except_the_images(app):
    assert {r.path for r in app.routes if isinstance(r, Mount)} <= {"/images"}


def test_the_api_docs_are_off_by_default(alone):
    assert alone.get("/docs").status_code == 404
    assert alone.get("/openapi.json").status_code == 404


def test_the_api_routes_exist_and_need_a_session(alone):
    r = alone.get("/api/items")  # 401 from the session check: the route exists, so it is not a 404

    assert r.status_code == 401
    assert r.json()["code"] == "not_authenticated"
