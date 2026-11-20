"""AC-24 (the `/images` side; `/readyz` is WP-3) and AC-14, AC-15 under failure: with S3 unreachable, refusing or
gone, a picture request is a retryable 503 `files_unavailable`, never a 404 (a 404 would tell the browser the picture
is gone) and never a 500, and it recovers without a restart (LLD 4.1, 4.2, 8)."""

import logging

import pytest
from botocore.exceptions import ConnectTimeoutError, NoCredentialsError, ReadTimeoutError

from tests.conftest import TEST_BUCKET
from tests.files_helpers import picture
from tests.owner_helpers import me

NAME = "pic1.png"
UNAVAILABLE = {
    "detail": "Pictures are unavailable right now. Retry shortly.",
    "code": "files_unavailable",
}


@pytest.fixture
def app(s3_app):
    return s3_app


@pytest.fixture
def a_id(client_a):
    return me(client_a)


@pytest.fixture
def url(a_id):
    return f"/images/{a_id}/cutout/{NAME}"


def _assert_unavailable(r):
    assert r.status_code == 503
    assert {k: r.json()[k] for k in UNAVAILABLE} == UNAVAILABLE
    assert r.headers["retry-after"] == "1"
    assert r.headers["content-type"] == "application/json"
    assert r.json()["request_id"]


def _assert_no_s3_in(r):
    assert TEST_BUCKET not in r.text and "amazonaws" not in r.text.lower()
    assert not [k for k in r.headers if k.lower().startswith("x-amz")]


def test_an_unreachable_s3_makes_a_picture_request_a_retryable_503(client_a, url, s3_down):
    r = client_a.get(url)

    _assert_unavailable(r)
    _assert_no_s3_in(r)


def test_head_is_a_503_too(client_a, url, s3_down):
    r = client_a.head(url)

    assert (r.status_code, r.headers["retry-after"]) == (503, "1")


def test_a_range_request_is_a_503_too(client_a, url, s3_down):
    _assert_unavailable(client_a.get(url, headers={"Range": "bytes=0-9"}))


def test_the_pictures_come_back_when_s3_does_with_no_restart(client_a, app, a_id, url, s3_down):
    assert client_a.get(url).status_code == 503

    s3_down.heal()
    assert client_a.get(url).status_code == 404  # reachable again, and the picture was never stored
    data = picture(".png")
    app.state.ctx.files.put(f"images/{a_id}/cutout/{NAME}", data)
    r = client_a.get(url)

    assert (r.status_code, r.content) == (200, data)


def test_an_outage_does_not_turn_another_users_request_into_a_503(client_b, a_id, url, s3_down):
    r = client_b.get(url)  # the owner check comes first: B learns nothing, not even that S3 is down

    assert (r.status_code, r.json()["code"]) == (404, "not_found")


def test_an_outage_does_not_turn_a_signed_out_request_into_a_503(anon_client, url, s3_down):
    r = anon_client.get(url)

    assert (r.status_code, r.json()["code"]) == (401, "not_authenticated")


def test_an_invalid_name_is_still_a_422_in_an_outage(client_a, a_id, s3_down):
    assert client_a.get(f"/images/{a_id}/cutout/bad.gif").status_code == 422


@pytest.mark.parametrize("code", ["AccessDenied", "SlowDown", "InternalError", "503"])
def test_an_s3_error_answer_is_a_503_not_a_404_or_a_500(client_a, url, s3_fail, code):
    s3_fail(code)

    _assert_unavailable(client_a.get(url))


def test_a_refused_head_is_a_503(client_a, url, s3_fail):
    s3_fail("AccessDenied")

    assert client_a.head(url).status_code == 503


@pytest.mark.parametrize(
    "error",
    [
        ConnectTimeoutError(endpoint_url="https://s3.example"),
        ReadTimeoutError(endpoint_url="https://s3.example"),
        NoCredentialsError(),
    ],
    ids=["connect-timeout", "read-timeout", "no-credentials"],
)
def test_a_timeout_or_missing_credentials_is_a_503(client_a, url, s3_fail, error):
    s3_fail(error)

    _assert_unavailable(client_a.get(url))


def test_a_deleted_bucket_is_a_503_not_a_404(client_a, url, s3):
    s3.delete_bucket(Bucket=TEST_BUCKET)

    r = client_a.get(url)

    _assert_unavailable(r)
    _assert_no_s3_in(r)


def test_a_missing_key_in_a_working_bucket_is_still_the_plain_404(client_a, url):
    r = client_a.get(url)

    assert (r.status_code, r.json()["code"], r.json()["detail"]) == (404, "not_found", "Image not found")
    assert "retry-after" not in r.headers


def test_the_outage_log_has_no_key_address_bucket_or_email(client_a, url, s3_fail, caplog):
    s3_fail("AccessDenied")

    with caplog.at_level(logging.INFO):
        client_a.get(url)

    assert "files_store_unavailable" in caplog.text
    for secret in (TEST_BUCKET, "images/", "amazonaws", "@example.com", NAME):
        assert secret not in caplog.text
