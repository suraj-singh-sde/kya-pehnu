"""Ingestion belongs to the account that started it (AC-11, AC-17, AC-21, AC-49; LLD 2.2, 3.6, 3.8).

The worker reads the owner from the job and the photo, duplicate checks look at the owner's wardrobe only, every image
path is `images/<user_id>/<kind>/<name>`, and `kyapehnu import-folder` works for one named account.
"""

import re

import pytest
from PIL import Image
from sqlmodel import Session, select

from kyapehnu.agent.llm.base import LLMUnavailableError
from kyapehnu.auth.accounts import upsert_google_user
from kyapehnu.core import app as core_app
from kyapehnu.core.cli import main
from kyapehnu.core.config import MAX_UPLOAD_FILE_BYTES, MAX_UPLOAD_FILES, get_settings
from kyapehnu.core.db import make_engine, prepare_database, utcnow
from kyapehnu.core.errors import NotFoundError
from kyapehnu.files.local import LocalStore
from kyapehnu.wardrobe import public as wardrobe
from kyapehnu.wardrobe.ingest import images
from kyapehnu.wardrobe.models import IngestJob, IngestPiece, Item, Photo, PhotoStatus
from kyapehnu.wardrobe.outfits import mannequin
from kyapehnu.wardrobe.seed import SEED_ITEMS
from tests.conftest import noise_image_bytes
from tests.fakes import FakeTransport, analysis, garment
from tests.owner_helpers import image_files, me, rows_of

EMAIL_A = "asha.zq9@example.com"
EMAIL_B = "bela.zq9@example.com"
HEX_NAME = re.compile(r"[0-9a-f]{32}\.jpg")


# --- the path helpers (LLD 3.8) -------------------------------------------------------------------------------------


def test_save_cutout_writes_png_and_thumb_into_the_owners_directories(tmp_path):
    cut, thumb = images.save_cutout(Image.new("RGBA", (50, 40), "red"), LocalStore(tmp_path), 7, "stem")

    assert (cut, thumb) == ("images/7/cutout/stem.png", "images/7/thumb/stem.webp")
    assert (tmp_path / cut).is_file()
    assert (tmp_path / thumb).is_file()


def test_the_mannequin_path_is_owner_keyed_and_differs_per_user():
    mine = mannequin.rel_for(7, [2, 1])

    assert mine == "images/7/mannequin/1-2.png"
    assert mannequin.rel_for(8, [2, 1]) == "images/8/mannequin/1-2.png"
    assert mannequin.rel_for(7, [1, 2], "tall").startswith("images/7/mannequin/1-2-p")


# --- the worker takes the owner from the job and the photo ----------------------------------------------------------


@pytest.fixture
def ctx(app):
    return app.state.ctx


def queue_photo(ctx, user_id, seed, name="queued"):
    """A queued job of one photo for `user_id`, written the way an upload would. Returns (job_id, photo_id)."""
    rel = f"images/{user_id}/original/{name}.jpg"
    path = ctx.data_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(noise_image_bytes(seed))
    with Session(ctx.engine) as s:
        job = IngestJob(user_id=user_id, source="upload", total=1)
        s.add(job)
        s.flush()
        photo = Photo(user_id=user_id, job_id=job.id, original_path=rel)
        s.add(photo)
        s.commit()
        return job.id, photo.id


def run_worker(app, *, retry=None):
    worker = app.state.worker
    worker.start()
    if retry is not None:
        worker.submit_retry(retry)
    worker.drain()
    worker.stop()


def test_the_worker_files_pieces_cutouts_and_the_normalized_copy_under_the_jobs_owner(
    app, client_a, client_b, fake, ctx
):
    me(client_a)
    b_id = me(client_b)
    fake.replies.append(analysis(garment()))
    _, photo_id = queue_photo(ctx, b_id, 41)

    run_worker(app)

    [piece] = rows_of(ctx, IngestPiece, b_id)
    with Session(ctx.engine) as s:
        photo = s.get(Photo, photo_id)
        assert photo.status == PhotoStatus.done
        assert photo.normalized_path == f"images/{b_id}/original/queued_norm.jpg"
    assert piece.cutout_path.startswith(f"images/{b_id}/cutout/")
    assert piece.thumb_path.startswith(f"images/{b_id}/thumb/")
    assert (ctx.data_dir / piece.cutout_path).is_file()
    assert (ctx.data_dir / piece.thumb_path).is_file()
    assert (ctx.data_dir / photo.normalized_path).is_file()
    assert all(f.startswith(f"images/{b_id}/") for f in image_files(ctx.data_dir))


def test_a_tagging_failure_leaves_an_error_piece_owned_by_the_photos_owner(app, client_a, client_b, fake, ctx):
    me(client_a)
    b_id = me(client_b)
    _, photo_id = queue_photo(ctx, b_id, 42)
    fake.replies.append(LLMUnavailableError("no key"))

    run_worker(app)

    [piece] = rows_of(ctx, IngestPiece, b_id)
    assert piece.state == "error"
    assert piece.photo_id == photo_id
    assert piece.user_id == b_id
    assert rows_of(ctx, IngestPiece, 1) == []


def test_an_undecodable_original_fails_the_photo_and_files_no_piece_for_anyone(app, client_a, client_b, fake, ctx):
    a_id = me(client_a)
    b_id = me(client_b)
    _, photo_id = queue_photo(ctx, b_id, 44)
    (ctx.data_dir / f"images/{b_id}/original/queued.jpg").write_bytes(b"not an image")

    run_worker(app)

    with Session(ctx.engine) as s:
        assert s.get(Photo, photo_id).status == PhotoStatus.failed
        assert s.exec(select(IngestPiece)).all() == []
    assert rows_of(ctx, IngestPiece, a_id) == [] and rows_of(ctx, IngestPiece, b_id) == []


def test_a_retry_files_the_new_pieces_under_the_photos_owner(app, client_a, client_b, fake, ctx):
    me(client_a)
    b_id = me(client_b)
    _, photo_id = queue_photo(ctx, b_id, 43)
    original = ctx.data_dir / f"images/{b_id}/original/queued.jpg"
    good = original.read_bytes()
    original.write_bytes(b"not an image")
    run_worker(app)
    original.write_bytes(good)
    fake.replies.append(analysis(garment()))

    run_worker(app, retry=photo_id)

    pieces = rows_of(ctx, IngestPiece, b_id)
    assert [p.state for p in pieces] == ["pending_review"]
    assert pieces[0].cutout_path.startswith(f"images/{b_id}/cutout/")
    assert rows_of(ctx, IngestPiece, 1) == []


def test_an_upload_creates_a_job_photos_and_originals_owned_by_the_uploader(client_a, client_b, ctx):
    me(client_a)
    b_id = me(client_b)
    files = [("files", (f"p{i}.jpg", noise_image_bytes(50 + i), "image/jpeg")) for i in range(2)]

    r = client_b.post("/api/ingest", files=files)

    assert r.status_code == 201
    assert r.json()["user_id"] == b_id
    photos = rows_of(ctx, Photo, b_id)
    assert len(photos) == 2
    for photo in photos:
        folder, name = photo.original_path.rsplit("/", 1)
        assert folder == f"images/{b_id}/original"
        assert HEX_NAME.fullmatch(name)
        assert (ctx.data_dir / photo.original_path).is_file()
    assert [j.id for j in rows_of(ctx, IngestJob, b_id)] == [r.json()["id"]]


def test_a_failed_upload_leaves_no_file_in_the_uploaders_directory(client_a, client_b, ctx):
    me(client_a)
    b_id = me(client_b)

    r = client_b.post("/api/ingest", files=[("files", ("x.txt", b"text", "text/plain"))])

    assert r.status_code == 400
    assert r.json()["code"] == "unsupported_file"
    assert image_files(ctx.data_dir) == []
    assert rows_of(ctx, Photo, b_id) == []


# --- duplicate checks are per account -------------------------------------------------------------------------------


def ingest(app, client, fake, seed, *, scripted=True):
    """Upload one photo as `client`, let the worker finish, and return that job's single piece."""
    if scripted:
        fake.replies.append(analysis(garment()))
    r = client.post("/api/ingest", files=[("files", ("p.jpg", noise_image_bytes(seed), "image/jpeg"))])
    assert r.status_code == 201, r.text
    run_worker(app)
    pieces = client.get(f"/api/ingest/pieces?job_id={r.json()['id']}").json()
    assert len(pieces) == 1, pieces
    return pieces[0]


def accept(client, piece):
    r = client.post(f"/api/ingest/pieces/{piece['id']}/accept", json={})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("accepted", [True, False], ids=["item-in-wardrobe", "piece-in-review"])
def test_the_same_photo_uploaded_by_two_users_is_not_a_duplicate_for_the_second(
    app, client_a, client_b, fake, accepted
):
    first = ingest(app, client_a, fake, 60)
    if accepted:
        accept(client_a, first)

    second = ingest(app, client_b, fake, 60)

    assert second["duplicate_of_item_id"] is None
    assert second["dup_reason"] is None
    assert second["state"] == "pending_review"


def test_the_same_photo_uploaded_twice_by_one_user_is_flagged_as_a_reupload(app, client_a, fake):
    ingest(app, client_a, fake, 61)

    again = ingest(app, client_a, fake, 61)

    assert again["dup_reason"] == "same photo uploaded before"


def test_a_garment_already_in_the_users_wardrobe_is_flagged_as_a_duplicate_of_that_item(app, client_a, fake):
    item = accept(client_a, ingest(app, client_a, fake, 62))

    again = ingest(app, client_a, fake, 62)

    assert again["duplicate_of_item_id"] == item["id"]


def test_a_duplicate_never_points_at_another_users_item(app, client_a, client_b, fake):
    a_item = accept(client_a, ingest(app, client_a, fake, 63))
    accept(client_b, ingest(app, client_b, fake, 64))  # B has no item yet, so the model is not asked about A's
    fake.replies.append(analysis(garment()))
    fake.replies.append({"match_index": None, "confidence": 0.0})  # B's own item is a candidate: the model is asked

    again = ingest(app, client_b, fake, 63, scripted=False)

    assert again["duplicate_of_item_id"] != a_item["id"]
    assert again["duplicate_of_item_id"] is None


def test_mark_duplicate_accepts_only_an_item_of_the_same_user(app, client_a, client_b, fake):
    a_item = accept(client_a, ingest(app, client_a, fake, 65))
    piece = ingest(app, client_b, fake, 66)

    r = client_b.post(f"/api/ingest/pieces/{piece['id']}/mark-duplicate", json={"item_id": a_item["id"]})

    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


def test_an_accepted_piece_becomes_an_item_of_its_owner_with_the_owner_keyed_pictures(
    app, client_a, client_b, fake, ctx
):
    b_id = me(client_b)
    item = accept(client_b, ingest(app, client_b, fake, 67))

    [row] = rows_of(ctx, Item, b_id)
    assert item["id"] == row.id
    assert row.user_id == b_id
    assert row.cutout_path.startswith(f"images/{b_id}/cutout/")
    assert row.thumb_path.startswith(f"images/{b_id}/thumb/")
    assert rows_of(ctx, Item, 1) == []


# --- folder import through the port ---------------------------------------------------------------------------------


def make_folder(tmp_path, seeds, name="photos"):
    folder = tmp_path / name
    folder.mkdir()
    for seed in seeds:
        (folder / f"p{seed}.jpg").write_bytes(noise_image_bytes(seed))
    return folder


def test_a_folder_job_copies_the_photos_into_the_owners_directory_and_runs_for_the_owner(
    client_a, client_b, fake, ctx, tmp_path
):
    me(client_a)
    b_id = me(client_b)
    folder = make_folder(tmp_path, [71, 72])
    fake.replies.extend([analysis(garment()), analysis(garment())])

    with Session(ctx.engine) as s:
        job = wardrobe.create_folder_job(b_id, s, ctx.files, folder)
        job_id = job.id
        assert job.user_id == b_id
    wardrobe.run_job(b_id, ctx, job_id)

    assert len(rows_of(ctx, Photo, b_id)) == 2
    assert len(rows_of(ctx, IngestPiece, b_id)) == 2
    assert rows_of(ctx, IngestJob, 1) == []
    assert all(f.startswith(f"images/{b_id}/") for f in image_files(ctx.data_dir))
    with Session(ctx.engine) as s:
        assert wardrobe.get_job(b_id, s, job_id).processed == 2


def test_run_job_for_another_users_job_is_refused(client_a, client_b, ctx, tmp_path):
    a_id = me(client_a)
    b_id = me(client_b)
    with Session(ctx.engine) as s:
        job_id = wardrobe.create_folder_job(a_id, s, ctx.files, make_folder(tmp_path, [73])).id
    before = image_files(ctx.data_dir)

    with pytest.raises(NotFoundError):
        wardrobe.run_job(b_id, ctx, job_id)

    assert image_files(ctx.data_dir) == before
    with Session(ctx.engine) as s:
        assert s.exec(select(Photo.status).where(Photo.user_id == a_id)).all() == [PhotoStatus.pending]


# --- kyapehnu import-folder for a named account (AC-11, AC-49) ---------------------------------------------------------


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """A CLI pointed at a throwaway data dir with two accounts (ids 1 and 2) and a scripted model. Returns the fake."""
    data = tmp_path / "data"
    monkeypatch.setenv("KYAPEHNU_DATA_DIR", str(data))
    monkeypatch.setenv("KYAPEHNU_CONFIG_FILE", str(tmp_path / "config.yaml"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    engine = prepare_database(data)
    with Session(engine) as s:
        for sub, email in (("sub-a", EMAIL_A), ("sub-b", EMAIL_B)):
            upsert_google_user(s, sub=sub, email=email, name=sub, now=utcnow())
    engine.dispose()
    fake = FakeTransport()
    build = core_app.build_ctx
    monkeypatch.setattr(core_app, "build_ctx", lambda settings, kv, **kw: build(settings, kv, transport=fake))
    yield fake
    get_settings.cache_clear()


def owned_counts(data_dir, model):
    engine = make_engine(data_dir)
    try:
        with Session(engine) as s:
            return {
                uid: len(rows) for uid in (1, 2) if (rows := s.exec(select(model).where(model.user_id == uid)).all())
            }
    finally:
        engine.dispose()


def test_import_folder_ingests_the_photos_for_the_named_account_only(cli, tmp_path, capsys):
    data = tmp_path / "data"
    folder = make_folder(tmp_path, [81, 82])
    cli.replies.extend([analysis(garment()), analysis(garment())])

    main(["import-folder", str(folder), "--email", EMAIL_B])

    out = capsys.readouterr()
    assert out.out == "import_done user_id=2 job=1 processed=2 failed=0\n"
    assert owned_counts(data, IngestJob) == {2: 1}
    assert owned_counts(data, Photo) == {2: 2}
    assert owned_counts(data, IngestPiece) == {2: 2}
    files = image_files(data)
    assert files
    assert all(f.startswith("images/2/") for f in files)


def test_import_folder_output_and_logs_never_carry_the_email(cli, tmp_path, capsys, caplog):
    folder = make_folder(tmp_path, [83])
    cli.replies.append(analysis(garment()))

    with caplog.at_level("DEBUG"):
        main(["import-folder", str(folder), "--email", EMAIL_A])

    out = capsys.readouterr()
    assert "user_id=1" in out.out
    assert EMAIL_A not in out.out + out.err + caplog.text
    assert "zq9" not in out.out + out.err + caplog.text


def test_import_folder_leaves_the_other_accounts_wardrobe_untouched(cli, tmp_path, capsys):
    data = tmp_path / "data"
    cli.replies.append(analysis(garment()))
    main(["import-folder", str(make_folder(tmp_path, [84], "first")), "--email", EMAIL_A])
    before = {f: (data / f).read_bytes() for f in image_files(data)}
    cli.replies.append(analysis(garment()))

    main(["import-folder", str(make_folder(tmp_path, [85], "second")), "--email", EMAIL_B])

    assert owned_counts(data, Photo) == {1: 1, 2: 1}
    assert {f: (data / f).read_bytes() for f in before} == before


def test_a_folder_of_more_than_200_images_is_refused_and_nothing_is_written(cli, tmp_path, capsys):
    data = tmp_path / "data"
    folder = tmp_path / "many"
    folder.mkdir()
    for i in range(MAX_UPLOAD_FILES + 1):
        (folder / f"p{i}.jpg").write_bytes(b"x")

    with pytest.raises(SystemExit) as exc:
        main(["import-folder", str(folder), "--email", EMAIL_A])

    assert exc.value.code == 2
    assert capsys.readouterr().err.strip()
    assert image_files(data) == []
    assert owned_counts(data, IngestJob) == {}


def test_a_folder_of_exactly_200_images_is_accepted(cli, tmp_path, capsys):
    folder = tmp_path / "limit"
    folder.mkdir()
    for i in range(MAX_UPLOAD_FILES):
        (folder / f"p{i}.jpg").write_bytes(b"x")  # not images: each fails on its own, no model call is made

    main(["import-folder", str(folder), "--email", EMAIL_A])

    assert (
        capsys.readouterr().out
        == f"import_done user_id=1 job=1 processed={MAX_UPLOAD_FILES} failed={MAX_UPLOAD_FILES}\n"
    )


def test_a_file_over_25_mb_is_refused_and_nothing_is_written(cli, tmp_path, capsys):
    data = tmp_path / "data"
    folder = tmp_path / "big"
    folder.mkdir()
    (folder / "ok.jpg").write_bytes(b"x")
    with (folder / "big.jpg").open("wb") as f:
        f.truncate(MAX_UPLOAD_FILE_BYTES + 1)

    with pytest.raises(SystemExit) as exc:
        main(["import-folder", str(folder), "--email", EMAIL_A])

    assert exc.value.code == 2
    assert image_files(data) == []
    assert owned_counts(data, Photo) == {}


def test_a_file_of_exactly_25_mb_is_accepted(cli, tmp_path, capsys):
    folder = tmp_path / "edge"
    folder.mkdir()
    with (folder / "edge.jpg").open("wb") as f:
        f.truncate(MAX_UPLOAD_FILE_BYTES)

    main(["import-folder", str(folder), "--email", EMAIL_A])

    assert capsys.readouterr().out == "import_done user_id=1 job=1 processed=1 failed=1\n"


def test_import_folder_for_an_unknown_account_writes_nothing_and_does_not_echo_the_email(cli, tmp_path, capsys):
    data = tmp_path / "data"
    folder = make_folder(tmp_path, [86])

    with pytest.raises(SystemExit) as exc:
        main(["import-folder", str(folder), "--email", "nobody.zq9@example.com"])

    out = capsys.readouterr()
    assert exc.value.code == 2
    assert "nobody" not in out.out + out.err
    assert image_files(data) == []
    assert owned_counts(data, IngestJob) == {}
    assert [p.name for p in folder.iterdir()] == ["p86.jpg"]


def test_seed_with_an_email_adds_the_demo_wardrobe_to_that_account_only(cli, tmp_path, capsys):
    data = tmp_path / "data"

    main(["seed", "--email", EMAIL_B])

    out = capsys.readouterr()
    assert out.out == f"added {len(SEED_ITEMS)} seed items\n"
    assert "zq9" not in out.out + out.err
    assert owned_counts(data, Item) == {2: len(SEED_ITEMS)}
    assert all(f.startswith("images/2/") for f in image_files(data))
