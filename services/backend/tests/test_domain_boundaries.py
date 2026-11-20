"""AC-1: the five domains meet only through `<domain>.public`. A stdlib `ast` check over the real tree, plus a
self-test on bad snippets so the checker cannot silently go blind (LLD 9.2)."""

import ast
import importlib
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlmodel import SQLModel

import kyapehnu

PACKAGE_DIR = Path(kyapehnu.__file__).parent
DOMAINS = ("auth", "user", "agent", "wardrobe", "files")
# The composition-root modules: only `core` itself may import these.
ROOT_MODULES = tuple(f"kyapehnu.core.{m}" for m in ("app", "cli", "housekeeping", "backup", "import_phase3", "health"))
SQL_VERBS = r"(?:from|join|into|update|table)"


def _under(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def _domain_of(name: str) -> str | None:
    parts = name.split(".")
    return parts[1] if len(parts) > 1 and parts[0] == "kyapehnu" and parts[1] in DOMAINS else None


def _resolve(node: ast.ImportFrom, package: str) -> str:
    """The absolute module a `from` statement names (level 1 is the file's own package)."""
    if node.level == 0:
        return node.module or ""
    parts = package.split(".")
    return ".".join([*parts[: len(parts) - (node.level - 1)], *([node.module] if node.module else [])])


def _imported_names(tree: ast.AST, package: str) -> Iterator[tuple[str, bool]]:
    """Every absolute dotted name an import statement can reach, with whether it is a `from` base (not a name)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, False
        elif isinstance(node, ast.ImportFrom):
            base = _resolve(node, package)
            yield base, True
            for alias in node.names:
                yield f"{base}.{alias.name}", False


def _is_public(name: str, other: str) -> bool:
    return _under(name, f"kyapehnu.{other}.public")


class _OutsideAnnotations(ast.NodeVisitor):
    """Collect Name loads and Attribute chains found anywhere except inside an annotation."""

    def __init__(self) -> None:
        self.names: list[ast.Name] = []
        self.attributes: list[ast.Attribute] = []

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if isinstance(node.ctx, ast.Load):
            self.names.append(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        self.attributes.append(node)
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        pass  # the annotation is the only child

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        self.visit(node.target)
        if node.value is not None:
            self.visit(node.value)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for child in (*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None), *node.body):
            self.visit(child)
        for decorator in node.decorator_list:
            self.visit(decorator)

    visit_FunctionDef = _function  # noqa: N815
    visit_AsyncFunctionDef = _function  # noqa: N815


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _string_constants(tree: ast.AST) -> Iterator[str]:
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.value


def violations(
    source: str,
    *,
    domain: str,
    package: str,
    filename: str = "module.py",
    row_classes: dict[str, set[str]],
    tables: dict[str, set[str]],
) -> list[str]:
    """Rule violations of one file. `domain` is a domain name or "weather" (which may import no domain at all)."""
    tree = ast.parse(source)
    found: list[str] = []
    others = [d for d in DOMAINS if d != domain]

    # Rule 1: imports.
    for name, is_base in _imported_names(tree, package):
        if _under(name, "authlib.jose"):
            found.append(f"authlib.jose: {name}")
        if any(_under(name, m) for m in ROOT_MODULES):
            found.append(f"composition-root module: {name}")
        target = _domain_of(name)
        if target is None or (domain != "weather" and target == domain):
            continue
        if domain == "weather":
            if not is_base or name.count(".") > 1:
                found.append(f"weather imports a domain: {name}")
            continue
        if _is_public(name, target):
            continue
        if is_base and name.count(".") == 1:
            continue  # `from kyapehnu.wardrobe import public`: the base is only the package
        found.append(f"cross-domain import: {name}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called = _dotted(node.func) or ""
            if called.rsplit(".", 1)[-1] in {"import_module", "__import__"}:
                found.append(f"dynamic import: {called}")

    # Rule 2: row classes of another domain, outside annotations.
    row_names: set[str] = set()
    module_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = _resolve(node, package)
            for alias in node.names:
                full = f"{base}.{alias.name}"
                for other in others:
                    if _under(base, f"kyapehnu.{other}.public") and alias.name in row_classes.get(other, set()):
                        row_names.add(alias.asname or alias.name)
                    if full == f"kyapehnu.{other}.public":
                        module_aliases[alias.asname or alias.name] = other
        elif isinstance(node, ast.Import):
            for alias in node.names:
                for other in others:
                    if alias.name == f"kyapehnu.{other}.public":
                        module_aliases[alias.asname or alias.name] = other
    uses = _OutsideAnnotations()
    uses.visit(tree)
    found += [f"row class outside an annotation: {n.id}" for n in uses.names if n.id in row_names]
    for attr in uses.attributes:
        base = _dotted(attr.value)
        other = module_aliases.get(base or "")
        if other is not None and attr.attr in row_classes.get(other, set()):
            found.append(f"row class outside an annotation: {base}.{attr.attr}")

    # Rule 3: raw SQL naming another domain's table.
    foreign = {t for d in others for t in tables.get(d, set())}
    if foreign:
        pattern = re.compile(rf"\b{SQL_VERBS}\s+\"?({'|'.join(sorted(map(re.escape, foreign)))})\b", re.IGNORECASE)
        found += [f"raw SQL names a foreign table: {s[:60]!r}" for s in _string_constants(tree) if pattern.search(s)]

    # Rule 4: public.py shape.
    if filename == "public.py" and domain in DOMAINS:
        for name, _ in _imported_names(tree, package):
            if _under(name, f"kyapehnu.{domain}.api"):
                found.append(f"public.py imports its own api: {name}")
            if domain == "agent" and _under(name, "kyapehnu.agent.chat"):
                found.append(f"agent.public imports agent.chat: {name}")
    return sorted(set(found))


def _package_of(path: Path) -> str:
    parts = ["kyapehnu", *path.relative_to(PACKAGE_DIR).with_suffix("").parts]
    return ".".join(parts if path.name == "__init__.py" else parts[:-1]).removesuffix(".__init__")


def _public_exports() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    rows: dict[str, set[str]] = {}
    for d in DOMAINS:
        public = importlib.import_module(f"kyapehnu.{d}.public")
        rows[d] = {n for n, obj in vars(public).items() if hasattr(obj, "__table__")}
        if (PACKAGE_DIR / d / "models.py").exists():
            importlib.import_module(f"kyapehnu.{d}.models")
    tables: dict[str, set[str]] = {d: set() for d in DOMAINS}
    for mapper in SQLModel._sa_registry.mappers:
        domain = _domain_of(mapper.class_.__module__)
        if domain is not None:
            tables[domain].add(str(mapper.local_table.name))
    return rows, tables


def test_the_five_domain_packages_exist_with_a_public_module():
    for d in DOMAINS:
        assert (PACKAGE_DIR / d / "__init__.py").is_file(), d
        assert (PACKAGE_DIR / d / "public.py").is_file(), d


def test_the_domains_meet_only_through_their_public_modules():
    rows, tables = _public_exports()
    problems: list[str] = []
    files = [(d, p) for d in (*DOMAINS, "weather") for p in sorted((PACKAGE_DIR / d).rglob("*.py"))]
    for d, path in files:
        for v in violations(
            path.read_text(encoding="utf-8"),
            domain=d,
            package=_package_of(path),
            filename=path.name,
            row_classes=rows,
            tables=tables,
        ):
            problems.append(f"{path.relative_to(PACKAGE_DIR)}: {v}")
    assert problems == []


def test_authlib_jose_is_imported_nowhere():
    for path in PACKAGE_DIR.rglob("*.py"):
        names = [n for n, _ in _imported_names(ast.parse(path.read_text(encoding="utf-8")), _package_of(path))]
        assert not [n for n in names if _under(n, "authlib.jose")], path


ROWS = {"wardrobe": {"Item"}, "agent": {"LlmCall"}, "user": {"Profile"}, "auth": set(), "files": set()}
TABLES = {"wardrobe": {"item", "outfit"}, "agent": {"llmcall"}, "user": {"userprofile"}, "auth": set(), "files": set()}

# (id, snippet, domain, package, filename)
BAD_SNIPPETS = [
    ("absolute-models", "from kyapehnu.wardrobe.models import Item", "agent", "kyapehnu.agent.chat", "m.py"),
    ("module-of-a-domain", "from kyapehnu.wardrobe import items", "agent", "kyapehnu.agent.chat", "m.py"),
    ("from-kyapehnu-import-domain", "from kyapehnu import wardrobe", "agent", "kyapehnu.agent", "m.py"),
    ("import-as", "import kyapehnu.wardrobe.items as y", "agent", "kyapehnu.agent", "m.py"),
    ("relative", "from ..wardrobe import items", "agent", "kyapehnu.agent", "m.py"),
    ("relative-models", "from ...wardrobe.models import Item", "agent", "kyapehnu.agent.chat", "m.py"),
    (
        "type-checking",
        "if TYPE_CHECKING:\n    from kyapehnu.wardrobe.items import Thing",
        "agent",
        "kyapehnu.agent",
        "m.py",
    ),
    ("inside-a-function", "def f():\n    from kyapehnu.user.profile import get", "agent", "kyapehnu.agent", "m.py"),
    ("composition-root", "from kyapehnu.core.app import create_app", "wardrobe", "kyapehnu.wardrobe", "m.py"),
    ("composition-root-housekeeping", "from kyapehnu.core import housekeeping", "agent", "kyapehnu.agent", "m.py"),
    (
        "import-module",
        "import importlib\nimportlib.import_module('kyapehnu.wardrobe.items')",
        "agent",
        "kyapehnu.agent",
        "m.py",
    ),
    ("dunder-import", "__import__('kyapehnu.wardrobe.items')", "agent", "kyapehnu.agent", "m.py"),
    ("authlib-jose", "from authlib.jose import JsonWebToken", "auth", "kyapehnu.auth", "m.py"),
    (
        "select-row-class",
        "from kyapehnu.wardrobe.public import Item\n\ndef f():\n    return select(Item.id)",
        "agent",
        "kyapehnu.agent",
        "m.py",
    ),
    (
        "where-row-class",
        "from kyapehnu.wardrobe.public import Item\n\ndef f(u):\n    return q.where(Item.user_id == u)",
        "agent",
        "kyapehnu.agent",
        "m.py",
    ),
    (
        "construct-row-class",
        "from kyapehnu.wardrobe.public import Item\n\ndef f(s):\n    s.add(Item(name='x'))",
        "agent",
        "kyapehnu.agent",
        "m.py",
    ),
    (
        "get-owned-row-class",
        "from kyapehnu.wardrobe.public import Item\n\ndef f(s):\n    return get_owned(1, s, Item, 2)",
        "agent",
        "kyapehnu.agent",
        "m.py",
    ),
    (
        "row-class-through-from-alias",
        "from kyapehnu.wardrobe import public as w\n\ndef f():\n    return w.Item(name='x')",
        "agent",
        "kyapehnu.agent",
        "m.py",
    ),
    (
        "row-class-through-import-alias",
        "import kyapehnu.wardrobe.public as w\n\ndef f():\n    return select(w.Item.id)",
        "agent",
        "kyapehnu.agent",
        "m.py",
    ),
    (
        "row-class-through-dotted-name",
        "import kyapehnu.wardrobe.public\n\ndef f():\n    return kyapehnu.wardrobe.public.Item()",
        "agent",
        "kyapehnu.agent",
        "m.py",
    ),
    (
        "raw-sql-select",
        "from sqlalchemy import text\nq = text('SELECT * FROM item')",
        "agent",
        "kyapehnu.agent",
        "m.py",
    ),
    ("raw-sql-quoted-update", "q = 'update \"outfit\" set x = 1'", "agent", "kyapehnu.agent", "m.py"),
    (
        "weather-imports-a-domain",
        "from kyapehnu.wardrobe.public import list_items",
        "weather",
        "kyapehnu.weather",
        "m.py",
    ),
    ("weather-imports-a-domain-package", "from kyapehnu import wardrobe", "weather", "kyapehnu.weather", "m.py"),
    ("public-imports-its-api", "from kyapehnu.wardrobe.api import items", "wardrobe", "kyapehnu.wardrobe", "public.py"),
    ("agent-public-imports-chat", "from kyapehnu.agent.chat import store", "agent", "kyapehnu.agent", "public.py"),
    (
        "files-adapter-from-another-domain",
        "from kyapehnu.files.local import LocalStore",
        "wardrobe",
        "kyapehnu.wardrobe",
        "m.py",
    ),
    ("files-module-from-another-domain", "from kyapehnu.files import keys", "agent", "kyapehnu.agent", "m.py"),
    ("files-public-imports-its-api", "from kyapehnu.files.api import router", "files", "kyapehnu.files", "public.py"),
]

GOOD_SNIPPETS = [
    (
        "public-function",
        "from kyapehnu.wardrobe.public import list_items\n\nlist_items(1, None)",
        "agent",
        "kyapehnu.agent",
    ),
    ("public-module", "from kyapehnu.wardrobe import public\n\npublic.list_items(1, None)", "agent", "kyapehnu.agent"),
    (
        "core",
        "from kyapehnu.core.db import get_owned\nfrom kyapehnu.core.errors import NotFoundError",
        "wardrobe",
        "kyapehnu.wardrobe",
    ),
    ("own-domain", "from kyapehnu.agent.chat import store\nfrom ..llm import base", "agent", "kyapehnu.agent"),
    (
        "annotation-only",
        "from kyapehnu.wardrobe.public import Item\n\ndef f(x: Item) -> list[Item]:\n    y: Item = x\n    return [y]",
        "agent",
        "kyapehnu.agent",
    ),
    (
        "annotation-only-through-alias",
        "from kyapehnu.wardrobe import public as w\n\ndef f(x: w.Item) -> w.Item:\n    return x",
        "agent",
        "kyapehnu.agent",
    ),
    ("sql-prose-in-a-docstring", '"""Rows from item are listed."""\n\nx = 1', "agent", "kyapehnu.agent"),
    ("weather-core", "from kyapehnu.core.config import Settings", "weather", "kyapehnu.weather"),
    ("files-public", "from kyapehnu.files import public as files", "wardrobe", "kyapehnu.wardrobe"),
    ("files-public-function", "from kyapehnu.files.public import key", "wardrobe", "kyapehnu.wardrobe"),
]


@pytest.mark.parametrize(
    ("snippet", "domain", "package", "filename"), [b[1:] for b in BAD_SNIPPETS], ids=[b[0] for b in BAD_SNIPPETS]
)
def test_the_checker_flags_each_bad_snippet(snippet, domain, package, filename):
    assert violations(snippet, domain=domain, package=package, filename=filename, row_classes=ROWS, tables=TABLES)


@pytest.mark.parametrize(
    ("snippet", "domain", "package"), [g[1:] for g in GOOD_SNIPPETS], ids=[g[0] for g in GOOD_SNIPPETS]
)
def test_the_checker_accepts_each_allowed_snippet(snippet, domain, package):
    assert violations(snippet, domain=domain, package=package, row_classes=ROWS, tables=TABLES) == []


# --- the files domain's own rules (LLD 7) ----------------------------------------------------------------------------


def files_imports(source: str, *, package: str, filename: str) -> list[str]:
    """`files_imports`: a module of `kyapehnu/files/` other than `api.py` imports no other domain and nothing of
    `kyapehnu.core` except `kyapehnu.core.errors` (so the store stays a leaf that any domain may call)."""
    if filename == "api.py":
        return []
    found: list[str] = []
    for name, is_base in _imported_names(ast.parse(source), package):
        if is_base and name in {"kyapehnu", "kyapehnu.core"}:
            continue  # `from kyapehnu.core import errors`: the base is only the package
        if _under(name, "kyapehnu.core") and not _under(name, "kyapehnu.core.errors"):
            found.append(f"files imports core: {name}")
        target = _domain_of(name)
        if target not in (None, "files"):
            found.append(f"files imports a domain: {name}")
    return sorted(set(found))


FILES_BAD = [
    ("wardrobe-public", "from kyapehnu.wardrobe.public import Item", "s3.py"),
    ("wardrobe-module", "from kyapehnu.wardrobe import public", "s3.py"),
    ("auth-public", "from kyapehnu.auth.public import CurrentUser", "keys.py"),
    ("core-config", "from kyapehnu.core.config import Settings", "local.py"),
    ("core-deps", "from kyapehnu.core import deps", "store.py"),
    ("core-db", "import kyapehnu.core.db", "public.py"),
    ("relative-core", "from ..core.context import Ctx", "s3.py"),
    ("inside-a-function", "def f():\n    from kyapehnu.wardrobe.public import Item", "local.py"),
]
FILES_GOOD = [
    ("core-errors", "from kyapehnu.core.errors import NotFoundError", "errors.py"),
    ("core-errors-module", "from kyapehnu.core import errors", "errors.py"),
    ("own-domain", "from kyapehnu.files import keys\nfrom kyapehnu.files.keys import key", "public.py"),
    ("relative-own-domain", "from .keys import key\nfrom . import store", "local.py"),
    ("third-party", "import boto3\nfrom botocore.exceptions import ClientError", "s3.py"),
    (
        "api-imports-what-the-route-needs",
        "from kyapehnu.auth.public import CurrentUser\nfrom kyapehnu.core.deps import CtxDep",
        "api.py",
    ),
]


@pytest.mark.parametrize(("snippet", "filename"), [b[1:] for b in FILES_BAD], ids=[b[0] for b in FILES_BAD])
def test_files_imports_flags_each_bad_snippet(snippet, filename):
    assert files_imports(snippet, package="kyapehnu.files", filename=filename)


@pytest.mark.parametrize(("snippet", "filename"), [g[1:] for g in FILES_GOOD], ids=[g[0] for g in FILES_GOOD])
def test_files_imports_accepts_each_allowed_snippet(snippet, filename):
    assert files_imports(snippet, package="kyapehnu.files", filename=filename) == []


def test_the_files_domain_imports_no_other_domain_and_only_core_errors():
    problems = [
        f"{path.relative_to(PACKAGE_DIR)}: {v}"
        for path in sorted((PACKAGE_DIR / "files").rglob("*.py"))
        for v in files_imports(path.read_text(encoding="utf-8"), package=_package_of(path), filename=path.name)
    ]

    assert problems == []


def test_only_the_s3_store_imports_boto3():
    importers = {
        path.relative_to(PACKAGE_DIR).as_posix()
        for path in (PACKAGE_DIR / "files").rglob("*.py")
        for name, _ in _imported_names(ast.parse(path.read_text(encoding="utf-8")), _package_of(path))
        if _under(name, "boto3") or _under(name, "botocore")
    }

    assert importers == {"files/s3.py"}


def picture_io_violations(source: str) -> list[str]:
    """AC-10: picture bytes move only through `kyapehnu.files`. A string constant (docstrings excluded, the literal
    parts of an f-string included) equal to "images" or starting with "images/", or an import of boto3/botocore."""
    tree = ast.parse(source)
    found = [f"images path: {s[:40]!r}" for s in _string_constants(tree) if s == "images" or s.startswith("images/")]
    found += [
        f"s3 client: {n}" for n, _ in _imported_names(tree, "kyapehnu.x") if _under(n, "boto3") or _under(n, "botocore")
    ]
    return sorted(set(found))


PICTURE_IO_BAD = [
    ("path-join", 'p = data_dir / "images" / str(uid)'),
    ("f-string-key", 'k = f"images/{uid}/thumb/x.webp"'),
    ("plain-key", 'k = "images/1/thumb/x.webp"'),
    ("import-boto3", "import boto3"),
    ("from-botocore", "from botocore.exceptions import ClientError"),
    ("inside-a-function", 'def f(d):\n    return d.joinpath("images")'),
]
PICTURE_IO_GOOD = [
    ("files-public", "from kyapehnu.files.public import key"),
    ("files-module", "from kyapehnu.files import public as files\n\nfiles.key(1, 'thumb', 'x.webp')"),
    ("prose-in-a-docstring", '"""Pictures live in images/ and are served from images."""\n\nx = 1'),
    ("similar-word", 'x = "imagery/1"\ny = "my_images"'),
]


@pytest.mark.parametrize(("snippet",), [(b[1],) for b in PICTURE_IO_BAD], ids=[b[0] for b in PICTURE_IO_BAD])
def test_picture_io_violations_flags_each_bad_snippet(snippet):
    assert picture_io_violations(snippet)


@pytest.mark.parametrize(("snippet",), [(g[1],) for g in PICTURE_IO_GOOD], ids=[g[0] for g in PICTURE_IO_GOOD])
def test_picture_io_violations_accepts_each_allowed_snippet(snippet):
    assert picture_io_violations(snippet) == []


# AC-10 on the real tree: every kyapehnu/**/*.py outside kyapehnu/files/. Each exception names its reason; any other hit is
# moved behind `files.public`.
PICTURE_IO_ALLOWED = {
    # The Phase 3 source folder layout (images/<kind>/<name>, read as plain files: an input, not the store) and the SQL
    # that rewrites those row paths to images/<user_id>/<kind>/<name>.
    "core/import_phase3.py",
    # Alembic revisions are frozen history: they name the paths and tables of their own day.
    "migrations/",
}


def _is_allowed_picture_io(relative: str) -> bool:
    return any(relative == a or (a.endswith("/") and relative.startswith(a)) for a in PICTURE_IO_ALLOWED)


def test_no_module_outside_the_files_domain_reads_or_writes_pictures():
    problems = [
        f"{relative}: {v}"
        for path in sorted(PACKAGE_DIR.rglob("*.py"))
        if (relative := path.relative_to(PACKAGE_DIR).as_posix())
        and not relative.startswith("files/")
        and not _is_allowed_picture_io(relative)
        for v in picture_io_violations(path.read_text(encoding="utf-8"))
    ]

    assert problems == []


def test_every_allow_listed_picture_io_path_exists():
    for allowed in PICTURE_IO_ALLOWED:
        assert (PACKAGE_DIR / allowed).exists(), allowed


def test_the_allow_list_holds_exactly_what_the_design_names():
    assert PICTURE_IO_ALLOWED == {"core/import_phase3.py", "migrations/"}


# The legacy picture helpers are gone: one way to name and make a picture key, the files domain.


@pytest.mark.parametrize("name", ["rel", "user_dir", "ensure_dirs", "KINDS"])
def test_the_ingest_images_module_no_longer_has_the_legacy_path_helper(name):
    from kyapehnu.wardrobe.ingest import images

    assert not hasattr(images, name)


@pytest.mark.parametrize("name", ["ensure_image_dirs", "IMAGE_KINDS"])
def test_the_wardrobe_public_module_no_longer_exports_the_legacy_picture_helper(name):
    from kyapehnu.wardrobe import public

    assert not hasattr(public, name)
    assert name not in public.__all__


def test_the_backup_module_has_no_local_file_discard():
    from kyapehnu.core import backup

    assert not hasattr(backup, "discard_files")
