"""The store-down rule (work plan, rules for every package) for the commands WP-2 owns: with the picture store
unreachable `seed` and `import-folder` print one fixed line on stderr, exit 1 and leave nothing half-written: no
rows, no pictures in the bucket, and not a key, address or bucket name in what they print."""

import pytest
from sqlmodel import Session, func, select

from kyapehnu.auth.accounts import upsert_google_user
from kyapehnu.core.cli import main
from kyapehnu.core.config import get_settings
from kyapehnu.core.db import prepare_database, utcnow
from kyapehnu.wardrobe.models import IngestJob, Item, Photo, PhotoStatus
from kyapehnu.wardrobe.seed import SEED_ITEMS
from tests.conftest import TEST_BUCKET, noise_image_bytes
from tests.ingest_store_helpers import bucket_keys, put_fails_after

EMAIL = "store.down@example.com"
MESSAGE = "files_unavailable: Pictures are unavailable right now. Retry shortly.\n"


@pytest.fixture
def stack(tmp_path, monkeypatch, s3):
    """The CLI pointed at a throwaway data dir and the moto bucket, with one account."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("KYAPEHNU_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KYAPEHNU_CONFIG_FILE", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("KYAPEHNU_FILES_BUCKET", TEST_BUCKET)
    get_settings.cache_clear()
    engine = prepare_database(data_dir)
    with Session(engine) as s:
        upsert_google_user(s, sub="sub-cli", email=EMAIL, name="cli", now=utcnow())
    engine.dispose()
    yield data_dir
    get_settings.cache_clear()


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "photos"
    path.mkdir()
    for i in range(2):
        (path / f"p{i}.jpg").write_bytes(noise_image_bytes(i))
    return path


def _count(data_dir, model) -> int:
    engine = prepare_database(data_dir)
    try:
        with Session(engine) as s:
            return s.exec(select(func.count()).select_from(model)).one()
    finally:
        engine.dispose()


def _run_and_expect_the_store_down_line(argv, capsys):
    with pytest.raises(SystemExit) as stop:
        main(argv)
    out, err = capsys.readouterr()
    assert stop.value.code == 1
    assert err == MESSAGE
    assert out == ""
    for secret in (TEST_BUCKET, "amazonaws", "images/", EMAIL):
        assert secret not in err


def test_seed_with_the_store_down_prints_the_line_exits_1_and_adds_nothing(stack, s3, s3_down, capsys):
    _run_and_expect_the_store_down_line(["seed", "--email", EMAIL], capsys)

    assert _count(stack, Item) == 0
    s3_down.heal()
    assert bucket_keys(s3) == []


def test_seed_that_loses_the_store_half_way_discards_what_it_put_and_adds_nothing(stack, s3, monkeypatch, capsys):
    stored = put_fails_after(monkeypatch, 5)

    _run_and_expect_the_store_down_line(["seed", "--email", EMAIL], capsys)

    assert len(stored) == 5
    assert _count(stack, Item) == 0
    assert bucket_keys(s3) == []  # the five that went in were taken back


def test_seed_still_works_on_the_bucket(stack, s3, capsys):
    main(["seed", "--email", EMAIL])

    assert capsys.readouterr().out == f"added {len(SEED_ITEMS)} seed items\n"
    assert len(bucket_keys(s3)) == 2 * len(SEED_ITEMS)


def test_import_folder_with_the_store_down_prints_the_line_exits_1_and_creates_no_job(
    stack, s3, s3_down, folder, capsys
):
    _run_and_expect_the_store_down_line(["import-folder", str(folder), "--email", EMAIL], capsys)

    assert (_count(stack, IngestJob), _count(stack, Photo)) == (0, 0)
    s3_down.heal()
    assert bucket_keys(s3) == []


def test_import_folder_that_loses_the_store_after_the_first_copy_discards_it(stack, s3, folder, monkeypatch, capsys):
    stored = put_fails_after(monkeypatch, 1)

    _run_and_expect_the_store_down_line(["import-folder", str(folder), "--email", EMAIL], capsys)

    assert len(stored) == 1
    assert (_count(stack, IngestJob), _count(stack, Photo)) == (0, 0)
    assert bucket_keys(s3) == []


def test_import_folder_that_loses_the_store_while_processing_ends_with_failed_photos_not_a_crash(
    stack, s3, folder, monkeypatch, capsys
):
    put_fails_after(monkeypatch, 2)  # the two originals go in; the normalized copies cannot

    main(["import-folder", str(folder), "--email", EMAIL])

    out = capsys.readouterr()
    assert out.out == "import_done user_id=1 job=1 processed=2 failed=2\n"
    assert out.err == ""
    engine = prepare_database(stack)
    try:
        with Session(engine) as s:
            photos = s.exec(select(Photo)).all()
        assert [p.status for p in photos] == [PhotoStatus.failed] * 2  # retryable
        assert bucket_keys(s3) == sorted(p.original_path for p in photos)
    finally:
        engine.dispose()
