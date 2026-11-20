"""Security-review fixes: body cap chosen by route (M1), provider error text redacted, no server path in settings."""

import io

import pytest
from sqlmodel import Session

from kyapehnu.agent.llm.base import LLMUnavailableError, redact
from kyapehnu.core.config import MAX_JSON_BODY_BYTES
from kyapehnu.wardrobe.ingest import pipeline
from tests.conftest import noise_image_bytes
from tests.fakes import OWNER_ID, ensure_owner
from tests.test_pipeline import _photos, _pieces

MULTIPART = {"content-type": "multipart/form-data; boundary=x"}
LEAKED_KEY = "sk-LEAKED-KEY-123"


@pytest.mark.parametrize(
    ("method", "path"),
    [("PUT", "/api/profile"), ("POST", "/api/ingest/pieces/1/accept")],
)
def test_multipart_label_does_not_lift_the_cap_on_a_json_route(client, method, path):
    r = client.request(method, path, content=b" " * (MAX_JSON_BODY_BYTES + 1), headers=MULTIPART)

    assert (r.status_code, r.json()["code"]) == (413, "payload_too_large")


def test_multipart_label_without_content_length_is_still_capped_on_a_json_route(client):
    def chunks():
        yield b" " * MAX_JSON_BODY_BYTES
        yield b" "

    r = client.put("/api/profile", content=chunks(), headers=MULTIPART)

    assert (r.status_code, r.json()["code"]) == (413, "http_413")


def test_real_upload_over_1_mib_still_passes_the_guard(client):
    big = noise_image_bytes(1, size=(1600, 1200))
    assert len(big) > MAX_JSON_BODY_BYTES

    r = client.post("/api/ingest", files=[("files", ("big.jpg", big, "image/jpeg"))])

    assert r.status_code == 201


def test_redact_blanks_every_configured_key_and_ignores_empty_ones():
    assert redact("a K1 b K2 c", ["K1", None, "", "K2"]) == "a *** b *** c"


def test_tagging_failure_text_stored_on_photo_and_piece_has_no_api_key(make_ctx):
    ctx = make_ctx([LLMUnavailableError(f"401 invalid key {LEAKED_KEY}")])
    ctx.secrets = (LEAKED_KEY,)
    with Session(ctx.engine) as s:
        ensure_owner(s)
        s.commit()
        job_id = pipeline.create_upload_job(OWNER_ID, s, ctx.files, [("p.jpg", io.BytesIO(noise_image_bytes(1)))]).id

    pipeline.run_job(OWNER_ID, ctx, job_id)

    [piece] = _pieces(ctx)
    [photo] = _photos(ctx)
    assert piece.error == photo.error == "tagging failed: 401 invalid key ***"


def test_app_context_carries_the_configured_keys(client, settings):
    assert client.app.state.ctx.secrets == (settings.anthropic_api_key, settings.openai_api_key)


def test_settings_response_does_not_expose_the_server_path(client, settings):
    r = client.get("/api/settings")

    assert r.status_code == 200
    assert "data_dir" not in r.json()
    assert str(settings.data_dir) not in r.text
