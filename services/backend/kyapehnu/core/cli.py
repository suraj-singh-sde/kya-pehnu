"""`kyapehnu` command line: migrate, openapi, seed, backup, restore, import-phase3, import-folder, copy-pictures and
retrim-thumbs."""

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import NoReturn, cast

from sqlalchemy import Engine
from sqlmodel import Session

from kyapehnu import __version__
from kyapehnu.auth.public import user_id_for_email
from kyapehnu.core import backup as backup_module
from kyapehnu.core import import_phase3
from kyapehnu.core.config import get_settings
from kyapehnu.core.db import prepare_database
from kyapehnu.core.errors import AppError, ImportRefusedError, StartupRefusedError
from kyapehnu.files import public as files
from kyapehnu.wardrobe.public import SeedError, create_folder_job, get_job, retrim_thumbs, run_job, seed

BYTES_PER_MB = 1024 * 1024
REFUSED_EXIT_CODE = 2
STORE_DOWN_EXIT_CODE = 1
COPY_FAILED_EXIT_CODE = 1
NOTHING_CHANGED = "Nothing was changed."
NO_ACCOUNT = f"No account matches that email. {NOTHING_CHANGED}"
BUCKET_VARIABLE = "KYAPEHNU_FILES_BUCKET"


def _store() -> files.FileStore:
    settings = get_settings()
    return files.build_store(settings.data_dir, settings.files_bucket, settings.files_region)


def _engine() -> tuple[Engine, files.FileStore]:
    engine = prepare_database(get_settings().data_dir)
    return engine, _store()


def _refuse(message: str) -> NoReturn:
    """Stop before anything is changed. The text never carries the email: the CLI prints ids only."""
    print(message, file=sys.stderr)
    sys.exit(REFUSED_EXIT_CODE)


def _account_id(engine: Engine, email: str) -> int:
    try:
        with Session(engine) as s:
            return user_id_for_email(s, email)
    except LookupError:
        _refuse(NO_ACCOUNT)


def cmd_migrate(_args: argparse.Namespace) -> None:
    _engine()
    print("database is up to date")


def cmd_seed(args: argparse.Namespace) -> None:
    engine, store = _engine()
    user_id = _account_id(engine, args.email)
    try:
        print(f"added {seed(user_id, engine, store, force=args.force)} seed items")
    except SeedError as e:
        sys.exit(str(e))


def cmd_backup(_args: argparse.Namespace) -> None:
    _, store = _engine()
    out = backup_module.backup(get_settings().data_dir, store)
    print(f"backup written: {out} ({out.stat().st_size / BYTES_PER_MB:.1f} MB)")


def cmd_restore(args: argparse.Namespace) -> None:
    try:
        backup_module.restore(get_settings().data_dir, _store(), args.archive)
    except ImportRefusedError as e:
        _refuse(str(e))
    print("restore complete")


def cmd_import_phase3(args: argparse.Namespace) -> None:
    try:
        counts = import_phase3.run(get_settings(), _store(), args.source, args.email)
    except ImportRefusedError as e:
        _refuse(str(e))
    print(counts.line())


def cmd_copy_pictures(args: argparse.Namespace) -> None:
    """Move the pictures between the disk and the bucket. It never opens the database."""
    settings = get_settings()
    if settings.files_bucket is None:
        _refuse(f"{BUCKET_VARIABLE} is not set: copy-pictures needs the bucket. {NOTHING_CHANGED}")
    if args.delete_source and args.to == "local":
        _refuse(
            f"--delete-source only applies to --to s3: pictures are never deleted from the bucket. {NOTHING_CHANGED}"
        )
    disk = files.LocalStore(settings.data_dir)
    bucket = files.build_store(settings.data_dir, settings.files_bucket, settings.files_region)
    source, target = (disk, bucket) if args.to == "s3" else (bucket, disk)
    source.ping()  # a store that is down from the start is the store-down line; later outages are per-picture failures
    target.ping()
    counts = files.copy_all(source, target, delete_source=args.delete_source)
    print(
        f"pictures_copied copied={counts.copied} present={counts.present} failed={counts.failed}"
        f" deleted={counts.deleted} direction={args.to}"
    )
    if counts.failed:
        sys.exit(COPY_FAILED_EXIT_CODE)


def cmd_retrim_thumbs(_args: argparse.Namespace) -> None:
    """Remake every thumb from its cutout, trimmed to the garment: once, for thumbs made before trimming."""
    engine, store = _engine()
    print(f"thumbs_retrimmed count={retrim_thumbs(engine, store)}")


def _folder(path: Path) -> Path:
    try:
        return path.expanduser()
    except RuntimeError:  # "~name" of an unknown user
        _refuse("Cannot resolve the home directory in the folder path. Nothing was changed.")


def cmd_import_folder(args: argparse.Namespace) -> None:
    from kyapehnu.core.app import build_ctx
    from kyapehnu.core.valkey import make_client

    settings = get_settings()
    kv = make_client(settings)
    try:
        ctx = build_ctx(settings, kv)
        user_id = _account_id(ctx.engine, args.email)
        try:
            with Session(ctx.engine) as s:
                job_id = cast(
                    int, create_folder_job(user_id, s, ctx.files, _folder(args.folder)).id
                )  # committed: has an id
            run_job(user_id, ctx, job_id)  # in this process: no worker thread
        except files.StoreUnavailableError:
            raise  # an outage is not a refusal: main prints the store-down line and exits 1
        except AppError as e:
            _refuse(str(e))
        with Session(ctx.engine) as s:
            job = get_job(user_id, s, job_id)
            print(f"import_done user_id={user_id} job={job_id} processed={job.processed} failed={job.failed}")
    finally:
        kv.close()


def cmd_openapi(_args: argparse.Namespace) -> None:
    """The schema needs no data directory and no Valkey: the app is built on a throwaway directory."""
    from kyapehnu.core.app import create_app

    with tempfile.TemporaryDirectory() as tmp:
        scratch = get_settings().model_copy(
            update={"data_dir": Path(tmp), "config_file": Path(tmp) / "config.yaml", "files_bucket": None}
        )
        app = create_app(scratch, start_worker=False)
        print(json.dumps(app.openapi()))
        app.state.ctx.engine.dispose()
        app.state.kv.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kyapehnu", description="Kya Pehnu personal stylist")
    parser.add_argument("--version", action="version", version=f"kyapehnu {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="upgrade the database").set_defaults(fn=cmd_migrate)
    p = sub.add_parser("seed", help="add ~30 demo items")
    p.add_argument("--email", required=True, help="the account that gets the items")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_seed)
    sub.add_parser("backup", help="zip the DB and images into data/backups").set_defaults(fn=cmd_backup)
    p = sub.add_parser("restore", help="restore a backup archive into an empty stack")
    p.add_argument("archive", type=Path)
    p.set_defaults(fn=cmd_restore)
    p = sub.add_parser("import-phase3", help="import a Phase 3 data directory into one account")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--email", required=True)
    p.set_defaults(fn=cmd_import_phase3)
    p = sub.add_parser("import-folder", help="import the photos of a folder into one account")
    p.add_argument("folder", type=Path)
    p.add_argument("--email", required=True)
    p.set_defaults(fn=cmd_import_folder)
    p = sub.add_parser("copy-pictures", help="copy the pictures between the disk and the bucket")
    p.add_argument("--to", choices=("s3", "local"), required=True)
    p.add_argument("--delete-source", action="store_true", help="after the copy was verified (--to s3 only)")
    p.set_defaults(fn=cmd_copy_pictures)
    sub.add_parser("retrim-thumbs", help="remake every thumb from its cutout, trimmed to the garment").set_defaults(
        fn=cmd_retrim_thumbs
    )
    sub.add_parser("openapi", help="print the OpenAPI schema (used to generate frontend types)").set_defaults(
        fn=cmd_openapi
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        args.fn(args)
    except StartupRefusedError as e:
        _refuse(str(e))
    except files.StoreUnavailableError:
        # Fixed text: the error's own may name a bucket or a key. Commands undo their own writes before it gets here.
        print(f"{files.StoreUnavailableError.code}: {files.StoreUnavailableError.message}", file=sys.stderr)
        sys.exit(STORE_DOWN_EXIT_CODE)
