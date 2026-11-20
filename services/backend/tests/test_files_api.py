"""GET|HEAD /images/{owner_id}/{kind}/{name} (LLD 3.1, 3.3, 3.8, 4.2): owner check, validation, symlinks, Range, ETag.
Moved from test_images_route.py. Every test runs on the local store and on S3 (moto), unless it is about the disk."""

from datetime import timedelta
from email.utils import format_datetime

import pytest
from fastapi.testclient import TestClient

from kyapehnu.core.db import utcnow
from kyapehnu.core.schemas import MAX_ROW_ID
from tests.conftest import LOOPBACK_PEER, SESSION_COOKIE, TEST_BUCKET, sign_in
from tests.files_helpers import picture, problem
from tests.owner_helpers import me

NAME = "pic1.png"


@pytest.fixture(params=["local", "s3"])
def app(request, make_app):
    """Overrides the app of conftest: the same app on each store, so every fixture built on it runs twice."""
    return make_app() if request.param == "local" else request.getfixturevalue("s3_app")


def write_image(app, rel, rgb=(200, 30, 30)):
    """A small picture at key `rel` of the app's own store (format by extension). Returns the bytes stored."""
    data = picture(rel[rel.rindex(".") :], rgb)
    app.state.ctx.files.put(rel, data)
    return data


@pytest.fixture
def a_id(client_a):
    return me(client_a)


@pytest.fixture
def b_id(client_b):
    return me(client_b)


@pytest.fixture
def stored(app, a_id):
    """A's picture at images/<a>/cutout/pic1.png. Returns (url, bytes)."""
    data = write_image(app, f"images/{a_id}/cutout/{NAME}")
    return f"/images/{a_id}/cutout/{NAME}", data


# --- who may read ----------------------------------------------------------------------------------------------------


def test_the_owner_gets_the_bytes_with_the_right_content_type(client_a, stored):
    url, data = stored

    r = client_a.get(url)

    assert r.status_code == 200
    assert r.content == data
    assert r.headers["content-type"] == "image/png"


@pytest.mark.parametrize(
    ("kind", "name", "media"),
    [
        ("original", "o.jpg", "image/jpeg"),
        ("cutout", "c.png", "image/png"),
        ("thumb", "t.webp", "image/webp"),
        ("collage", "k.webp", "image/webp"),
        ("mannequin", "1-2.png", "image/png"),
    ],
)
def test_every_kind_is_served_from_the_owners_directory(client_a, app, a_id, kind, name, media):
    data = write_image(app, f"images/{a_id}/{kind}/{name}")

    r = client_a.get(f"/images/{a_id}/{kind}/{name}")

    assert (r.status_code, r.content, r.headers["content-type"]) == (200, data, media)


def test_a_signed_out_client_gets_401_and_no_bytes(anon_client, stored):
    url, data = stored

    r = anon_client.get(url)

    assert r.status_code == 401
    assert r.json()["code"] == "not_authenticated"
    assert data not in r.content


def test_another_user_gets_404_not_found_and_no_bytes(client_b, stored):
    url, data = stored

    r = client_b.get(url)

    assert r.status_code == 404
    assert r.json()["code"] == "not_found"
    assert data not in r.content


def test_a_missing_file_of_the_own_directory_is_the_same_404(client_a, a_id):
    r = client_a.get(f"/images/{a_id}/cutout/nothing.png")

    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


def test_an_owner_id_that_is_nobodys_is_a_404(client_a, stored):
    r = client_a.get("/images/999999/cutout/pic1.png")

    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


def test_the_same_name_in_two_directories_gives_each_user_their_own_file(client_a, client_b, app, a_id, b_id):
    data_a = write_image(app, f"images/{a_id}/thumb/same.webp", (250, 0, 0))
    data_b = write_image(app, f"images/{b_id}/thumb/same.webp", (0, 0, 250))

    assert client_a.get(f"/images/{a_id}/thumb/same.webp").content == data_a
    assert client_b.get(f"/images/{b_id}/thumb/same.webp").content == data_b
    assert client_b.get(f"/images/{a_id}/thumb/same.webp").status_code == 404


def test_a_file_in_the_old_flat_layout_is_no_longer_served(client_a, app):
    flat = app.state.ctx.data_dir / "images" / "cutout"  # the static mount served this before
    flat.mkdir(parents=True, exist_ok=True)
    (flat / "old.png").write_bytes(picture(".png"))

    r = client_a.get("/images/cutout/old.png")

    assert r.status_code == 404


def test_the_route_is_not_part_of_the_api_schema(app):
    assert not [p for p in app.openapi()["paths"] if p.startswith("/images")]


def test_post_to_an_image_url_is_405(client_a, stored):
    assert client_a.post(stored[0]).status_code == 405


# --- validation: 422 -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["secrets", "Cutout", "ORIGINAL", "thumbs", "mannequins", "images", "x"])
def test_an_unknown_kind_is_422(client_a, a_id, kind):
    r = client_a.get(f"/images/{a_id}/{kind}/{NAME}")

    assert r.status_code == 422


@pytest.mark.parametrize(
    "name",
    [
        "pic.gif",
        "pic.txt",
        "pic",
        ".png",
        "pic.png.exe",
        "a.b.png",
        "a b.png",
        "a%00.png",
        "a%20.png",
        "a$.png",
        "ä.png",
        ("x" * 201) + ".png",
    ],
)
def test_a_name_outside_the_pattern_is_422(client_a, a_id, name):
    r = client_a.get(f"/images/{a_id}/cutout/{name}")

    assert r.status_code == 422


@pytest.mark.parametrize("ext", ["jpg", "jpeg", "png", "webp", "heic", "heif"])
def test_each_allowed_extension_passes_validation(client_a, a_id, ext):
    r = client_a.get(f"/images/{a_id}/original/x.{ext}")

    assert r.status_code == 404  # valid shape, nothing there


@pytest.mark.parametrize("stem", ["a", "A-b_9", "x" * 200])
def test_stems_of_one_to_200_allowed_characters_pass_validation(client_a, a_id, stem):
    assert client_a.get(f"/images/{a_id}/original/{stem}.jpg").status_code == 404


@pytest.mark.parametrize("owner", ["abc", "-", "1.5", str(MAX_ROW_ID + 1), "99999999999999999999999"])
def test_an_owner_id_that_is_not_a_row_id_is_422(client_a, owner):
    r = client_a.get(f"/images/{owner}/cutout/{NAME}")

    assert r.status_code == 422


def test_the_largest_row_id_is_valid_and_not_the_callers(client_a):
    assert client_a.get(f"/images/{MAX_ROW_ID}/cutout/{NAME}").status_code == 404


# --- symlinks and folders: the disk only (the local store) ------------------------------------------------------------


@pytest.fixture
def local_pair(make_app):
    """A local-store app with user A and user B signed in, whatever store the module's `app` fixture is on."""
    app = make_app()
    a_id, a = sign_in(app, "sub-a")
    b_id, b = sign_in(app, "sub-b")
    return app, a_id, a, b_id, b


def test_a_symlink_is_a_404_even_for_the_owner(local_pair, tmp_path):
    app, a_id, client, _, _ = local_pair
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"outside the data dir")
    folder = app.state.ctx.data_dir / "images" / str(a_id) / "cutout"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "link.png").symlink_to(secret)

    r = client.get(f"/images/{a_id}/cutout/link.png")

    assert r.status_code == 404
    assert b"outside the data dir" not in r.content


def test_a_symlink_to_another_users_file_is_a_404(local_pair):
    app, a_id, _, b_id, client_b = local_pair
    write_image(app, f"images/{a_id}/cutout/{NAME}")
    data_dir = app.state.ctx.data_dir
    folder = data_dir / "images" / str(b_id) / "cutout"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "stolen.png").symlink_to(data_dir / "images" / str(a_id) / "cutout" / NAME)

    r = client_b.get(f"/images/{b_id}/cutout/stolen.png")

    assert r.status_code == 404


def test_a_directory_with_an_image_name_is_a_404(local_pair):
    app, a_id, client, _, _ = local_pair
    (app.state.ctx.data_dir / "images" / str(a_id) / "cutout" / "dir.png").mkdir(parents=True)

    assert client.get(f"/images/{a_id}/cutout/dir.png").status_code == 404


# --- Range, ETag, HEAD -----------------------------------------------------------------------------------------------


def test_a_range_request_gets_206_with_exactly_those_bytes(client_a, stored):
    url, data = stored

    r = client_a.get(url, headers={"Range": "bytes=2-9"})

    assert r.status_code == 206
    assert r.content == data[2:10]
    assert r.headers["content-range"] == f"bytes 2-9/{len(data)}"
    assert r.headers["accept-ranges"] == "bytes"


def test_a_range_beyond_the_file_is_416(client_a, stored):
    url, data = stored

    r = client_a.get(url, headers={"Range": f"bytes={len(data) + 10}-"})

    assert r.status_code == 416


def test_a_range_request_of_another_users_image_is_a_404_not_a_206(client_b, stored):
    r = client_b.get(stored[0], headers={"Range": "bytes=0-9"})

    assert r.status_code == 404


def test_the_etag_is_sent_and_a_matching_conditional_request_gets_304(client_a, stored):
    url, data = stored
    first = client_a.get(url)
    etag = first.headers["etag"]

    again = client_a.get(url, headers={"If-None-Match": etag})

    assert etag
    assert again.status_code == 304
    assert again.content == b""


def test_every_answer_for_the_owner_is_private_and_must_revalidate(client_a, stored):
    url, _ = stored
    etag = client_a.get(url).headers["etag"]

    answers = [
        client_a.get(url),
        client_a.head(url),
        client_a.get(url, headers={"Range": "bytes=0-3"}),
        client_a.get(url, headers={"If-None-Match": etag}),
    ]

    assert [r.status_code for r in answers] == [200, 200, 206, 304]
    assert {r.headers["cache-control"] for r in answers} == {"private, no-cache"}


def test_a_changed_file_gets_a_different_etag(client_a, app, a_id, stored):
    url, _ = stored
    before = client_a.get(url).headers["etag"]

    write_image(app, f"images/{a_id}/cutout/{NAME}", (1, 2, 3))

    assert client_a.get(url).headers["etag"] != before


def test_a_conditional_request_cannot_confirm_another_users_file(client_a, client_b, stored):
    etag = client_a.get(stored[0]).headers["etag"]

    r = client_b.get(stored[0], headers={"If-None-Match": etag})

    assert r.status_code == 404


def test_head_of_the_own_image_has_the_headers_and_no_body(client_a, stored):
    url, data = stored

    r = client_a.head(url)

    assert r.status_code == 200
    assert r.content == b""
    assert r.headers["content-length"] == str(len(data))
    assert r.headers["content-type"] == "image/png"
    assert r.headers["etag"] == client_a.get(url).headers["etag"]


def test_head_of_another_users_image_is_404_and_signed_out_is_401(client_b, anon_client, stored):
    assert client_b.head(stored[0]).status_code == 404
    assert anon_client.head(stored[0]).status_code == 401


def test_head_with_a_bad_kind_is_422(client_a, a_id):
    assert client_a.head(f"/images/{a_id}/nope/{NAME}").status_code == 422


# --- AC-14: one answer for everything that is not the caller's picture -----------------------------------------------


def test_other_user_missing_file_and_nobodys_owner_id_all_get_the_same_404(client_a, client_b, a_id, b_id, stored):
    url, _ = stored
    answers = [
        client_b.get(url),  # A's picture, asked by B
        client_a.get(f"/images/{a_id}/cutout/nothing.png"),  # A's own folder, no such file
        client_a.get(f"/images/{b_id}/cutout/{NAME}"),  # B's folder, asked by A
        client_a.get(f"/images/999999/cutout/{NAME}"),  # nobody's
        client_a.get(url.replace("pic1", "pic2")),  # an edited name
    ]

    assert {problem(r) for r in answers} == {(404, "Image not found", "not_found", "application/json")}


def test_the_404_of_a_missing_file_never_says_what_exists(client_a, a_id, stored):
    r = client_a.get(f"/images/{a_id}/cutout/nothing.png")

    assert set(r.json()) == {"detail", "code", "request_id"}
    assert NAME not in r.text


def test_after_sign_out_the_same_address_returns_no_bytes(app, client_a, stored):
    url, data = stored
    cookie = client_a.cookies[SESSION_COOKIE]
    assert client_a.get(url).content == data
    assert client_a.post("/api/auth/logout").status_code == 204

    replay = TestClient(app, base_url="http://localhost", client=LOOPBACK_PEER)
    replay.cookies.set(SESSION_COOKIE, cookie)
    r = replay.get(url)

    assert r.status_code == 401
    assert data not in r.content


# --- AC-15: nothing of S3 reaches the browser ----------------------------------------------------------


def _leaks(response) -> list[str]:
    found = [k for k in response.headers if k.lower().startswith("x-amz")]
    text = response.text if response.status_code >= 400 else ""
    values = " ".join([*response.headers.values(), text]).lower()
    return found + [w for w in ("amazonaws", TEST_BUCKET, "x-amz-", "signature=") if w in values]


def test_no_answer_carries_an_s3_host_a_bucket_name_or_a_signed_query(client_a, client_b, stored):
    url, data = stored
    etag = client_a.get(url).headers["etag"]
    answers = [
        client_a.get(url),
        client_a.head(url),
        client_a.get(url, headers={"Range": "bytes=0-3"}),
        client_a.get(url, headers={"If-None-Match": etag}),
        client_a.get(url, headers={"Range": f"bytes={len(data) + 10}-"}),
        client_a.get(url.replace("pic1", "nothing")),
        client_b.get(url),
    ]

    assert [r.status_code for r in answers] == [200, 200, 206, 304, 416, 404, 404]
    assert [_leaks(r) for r in answers] == [[]] * len(answers)
    assert not [r for r in answers if "location" in r.headers]  # the bytes come from this address, never a redirect


# --- more of Range and the validators --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "span"),
    [
        ("bytes=0-0", lambda n: (0, 0)),
        ("bytes=-4", lambda n: (n - 4, n - 1)),
        ("bytes=5-", lambda n: (5, n - 1)),
        ("bytes=0-99999", lambda n: (0, n - 1)),
    ],
    ids=["first-byte", "last-four", "open-ended", "past-the-end"],
)
def test_a_range_gets_exactly_those_bytes(client_a, stored, header, span):
    url, data = stored
    first, last = span(len(data))

    r = client_a.get(url, headers={"Range": header})

    assert r.status_code == 206
    assert r.content == data[first : last + 1]
    assert r.headers["content-range"] == f"bytes {first}-{last}/{len(data)}"


def test_a_range_answer_is_not_the_whole_file_in_length(client_a, stored):
    url, _ = stored

    r = client_a.get(url, headers={"Range": "bytes=2-9"})

    assert r.headers["content-length"] == "8"


def test_a_416_has_no_body_of_picture_bytes(client_a, stored):
    url, data = stored

    r = client_a.get(url, headers={"Range": f"bytes={len(data) + 10}-"})

    assert r.status_code == 416
    assert data not in r.content


def test_the_last_modified_date_gives_304_to_a_later_if_modified_since(client_a, stored):
    url, _ = stored
    first = client_a.get(url)
    later = format_datetime(utcnow() + timedelta(days=1), usegmt=True)

    r = client_a.get(url, headers={"If-Modified-Since": later})

    assert "last-modified" in first.headers
    assert r.status_code == 304
    assert r.content == b""


def test_an_unparsable_if_modified_since_is_ignored(client_a, stored):
    url, data = stored

    r = client_a.get(url, headers={"If-Modified-Since": "yesterday-ish"})

    assert r.status_code == 200
    assert r.content == data


def test_a_stale_etag_gets_the_whole_picture(client_a, stored):
    url, data = stored

    r = client_a.get(url, headers={"If-None-Match": '"not-the-etag"'})

    assert (r.status_code, r.content) == (200, data)


def test_head_has_the_same_validators_as_get(client_a, stored):
    url, _ = stored
    got, head = client_a.get(url), client_a.head(url)

    assert head.headers["etag"] == got.headers["etag"]
    assert head.headers["accept-ranges"] == "bytes"
    assert head.headers["cache-control"] == "private, no-cache"
