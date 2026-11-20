import io
import os
import random
import types

import fakeredis
import httpx
import pytest
import redis
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from limits.storage import MemoryStorage
from PIL import Image
from sqlmodel import Session

from kyapehnu.agent.llm.base import LLMProvider
from kyapehnu.core.config import Prefs, Settings
from kyapehnu.core.db import make_engine, migrate, utcnow
from kyapehnu.wardrobe.ingest import images
from tests._legacy_owner_user import install_legacy_sign_in  # also patches PROFILE_ID at import
from tests.fake_google import CLIENT_ID, CLIENT_SECRET, FakeGoogle
from tests.fake_openmeteo import FakeOpenMeteo
from tests.fakes import FakeTransport


@pytest.fixture(autouse=True)
def legacy_sign_in(request, monkeypatch):
    """Test modules that predate accounts act as the legacy owner (tests/_legacy_owner_user.py)."""
    install_legacy_sign_in(request.module.__name__, monkeypatch)


@pytest.fixture(autouse=True)
def default_valkey(monkeypatch):
    """A `create_app` call that passes no `kv` gets a fresh fake Valkey instead of dialling localhost."""
    monkeypatch.setattr(
        "kyapehnu.core.app.make_client",
        lambda _settings: fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True),
    )


@pytest.fixture(autouse=True)
def no_rembg(monkeypatch):
    """Never load the real rembg model in tests."""
    monkeypatch.setattr(images, "remove_background", lambda im: im.convert("RGBA"))


def pytest_collection_modifyitems(items):
    """Integration tests need a real Valkey: without KYAPEHNU_TEST_VALKEY_URL they show as skipped, not as missing."""
    if os.environ.get("KYAPEHNU_TEST_VALKEY_URL"):
        return
    skip = pytest.mark.skip(reason="integration: no Valkey (set KYAPEHNU_TEST_VALKEY_URL)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def kv(request):
    """A fresh fake Valkey per test; a real one (a database per xdist worker) for a test marked `integration`."""
    url = os.environ.get("KYAPEHNU_TEST_VALKEY_URL")
    if url and request.node.get_closest_marker("integration"):
        worker = os.environ.get("PYTEST_XDIST_WORKER", "gw0").removeprefix("gw")
        client = redis.Redis.from_url(url, db=int(worker) % 16 if worker.isdigit() else 0, decode_responses=True)
        client.flushdb()
        yield client
        client.flushdb()
    else:
        client = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
        yield client
    client.close()


def noise_image_bytes(seed: int, size=(800, 600), fmt="JPEG") -> bytes:
    """Random-noise image: distinct seeds give distinct perceptual hashes."""
    w, h = size
    im = Image.frombytes("RGB", size, random.Random(seed).randbytes(w * h * 3))
    buf = io.BytesIO()
    im.save(buf, fmt)
    return buf.getvalue()


@pytest.fixture
def engine(tmp_path):
    """Migrated SQLite engine in tmp_path, with the image directory created (by the disk store)."""
    from kyapehnu.files.local import LocalStore  # lazy: keeps the other test modules importable on their own

    e = make_engine(tmp_path)
    migrate(e)
    LocalStore(tmp_path)
    return e


@pytest.fixture
def make_ctx(engine, tmp_path):
    """Factory for a pipeline Ctx whose LLM replies are scripted (the transport is ctx.provider.transport)."""

    def _make(replies):
        from kyapehnu.wardrobe.ingest import pipeline  # lazy: keeps the other test modules importable on their own

        return pipeline.Ctx(
            engine=engine,
            provider=LLMProvider(FakeTransport(replies)),
            prefs=Prefs(ingest={"workers": 1}),
            data_dir=tmp_path,
        )

    return _make


LOOPBACK_PEER = ("127.0.0.1", 50000)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        config_file=tmp_path / "missing.yaml",
        public_url="http://localhost",
        google_client_id=CLIENT_ID,
        google_client_secret=CLIENT_SECRET,
        llm_provider="anthropic",
        anthropic_api_key="test-key",
    )


@pytest.fixture
def fake():
    return FakeTransport()


@pytest.fixture
def fake_weather():
    """The scripted Open-Meteo behind the app's weather client (tests/fake_openmeteo.py)."""
    return FakeOpenMeteo()


@pytest.fixture
def app(settings, fake, fake_weather, kv):
    from kyapehnu.core.app import create_app  # lazy: keeps the other test modules importable on their own

    return create_app(
        settings=settings,
        transport=fake,
        weather_transport=httpx.MockTransport(fake_weather),
        kv=kv,
        rate_limit_storage=MemoryStorage(),
    )


SESSION_COOKIE = "kyapehnu_session"
OAUTH_COOKIE = "kyapehnu_oauth"


def sign_in(app, sub, *, email=None, name=None):
    """A person with a live session, made without Google: the account (the first one is the admin), its profile and a
    Valkey session. Returns (user_id, a TestClient that carries the session cookie)."""
    from kyapehnu.auth.accounts import upsert_google_user  # lazy: keeps the other test modules importable on their own
    from kyapehnu.user.public import ensure_profile

    ctx = app.state.ctx
    with Session(ctx.engine) as s:
        user = upsert_google_user(
            s,
            sub=sub,
            email=f"{sub}@example.com" if email is None else email,
            name=sub if name is None else name,
            now=utcnow(),
        )
        ensure_profile(user.id, s, ctx.prefs)
        user_id = user.id
    c = TestClient(app, base_url="http://localhost", client=LOOPBACK_PEER)
    c.cookies.set(SESSION_COOKIE, app.state.sessions.create(user_id))
    return user_id, c


@pytest.fixture
def anon_client(app):
    """Nobody signed in."""
    return TestClient(app, base_url="http://localhost", client=LOOPBACK_PEER)


@pytest.fixture
def client_a(app):
    """User A: the first account on the instance, so the admin."""
    return sign_in(app, "sub-a", name="Asha")[1]


@pytest.fixture
def client_b(app, client_a):
    """User B: the second account, so not an admin. Depends on `client_a` so A is created first."""
    return sign_in(app, "sub-b", name="Bela")[1]


@pytest.fixture
def google(monkeypatch, settings):
    """The in-process Google (tests/fake_google.py) behind every OIDC call authlib makes."""
    g = FakeGoogle(redirect_uri=f"{settings.public_origin}/api/auth/google/callback")
    g.install(monkeypatch)
    return g


@pytest.fixture
def make_app(settings, fake, fake_weather, kv):
    """Factory: an app with settings overrides. `kv_client` replaces the Valkey; `rate_limit_storage=None` makes the
    app build its own (Valkey) limiter storage, otherwise an in-memory one is injected."""

    def _make(*, kv_client=None, rate_limit_storage=..., **overrides):
        from kyapehnu.core.app import create_app  # lazy: keeps the other test modules importable on their own

        return create_app(
            settings=settings.model_copy(update=overrides),
            transport=fake,
            weather_transport=httpx.MockTransport(fake_weather),
            kv=kv if kv_client is None else kv_client,
            rate_limit_storage=MemoryStorage() if rate_limit_storage is ... else rate_limit_storage,
        )

    return _make


@pytest.fixture
def valkey_fault(kv, monkeypatch):
    """`valkey_fault("GETEX")` makes those Valkey commands fail like a dropped connection; no argument fails every
    command; `valkey_fault.heal()` restores it."""
    state: dict = {"fail": None}
    original = type(kv).execute_command

    def execute_command(self, *args, **kwargs):
        names = state["fail"]
        if names is not None and (not names or str(args[0]).upper() in names):
            raise redis.ConnectionError("Valkey went away (injected)")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(kv), "execute_command", execute_command)

    def fault(*commands):
        state["fail"] = {c.upper() for c in commands}

    fault.heal = lambda: state.update(fail=None)
    return fault


@pytest.fixture
def client(app):
    """User A, signed in: the first account, so the admin. One TestClient per app, with its lifespan running."""
    with sign_in(app, "sub-a")[1] as c:
        yield c


@pytest.fixture
def add_route(app):
    """Add a test-only route in front of every mount, so a built SPA at / cannot shadow it."""

    def _add(path, endpoint, methods=("GET",)):
        app.router.routes.insert(0, APIRoute(path, endpoint, methods=list(methods)))

    return _add


@pytest.fixture
def unconfigured_client(tmp_path, monkeypatch, fake, fake_weather, kv):
    """The app with no AI provider (settings with no provider fields, no key in the environment, nothing saved).

    `fake` is the transport behind it, so a test can assert that no call reached it."""
    from kyapehnu.core.app import create_app  # lazy: keeps the other test modules importable on their own

    for name in ("ANTHROPIC_API_KEY", "KYAPEHNU_ANTHROPIC_API_KEY", "KYAPEHNU_LLM_PROVIDER", "KYAPEHNU_OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    bare = Settings(_env_file=None, data_dir=tmp_path / "bare-data", config_file=tmp_path / "bare-missing.yaml")
    app = create_app(settings=bare, transport=fake, weather_transport=httpx.MockTransport(fake_weather), kv=kv)
    with sign_in(app, "sub-a")[1] as c:
        yield c


# --- the files domain: an in-process S3 (moto) and the two stores that pass one contract (LLD 7) -------------------

TEST_BUCKET = "test-pictures"
TEST_REGION = "ap-south-1"


@pytest.fixture(autouse=True)
def no_aws(monkeypatch):
    """No test can reach AWS or pick up a developer's credentials, config, instance role or bucket settings."""
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", TEST_REGION)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    for name in ("KYAPEHNU_FILES_BUCKET", "KYAPEHNU_FILES_REGION", "KYAPEHNU_SWEEP_MAX_DELETES"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def s3(no_aws):
    """An in-process S3 with the empty bucket TEST_BUCKET. Yields a boto3 client for planting and inspecting objects."""
    import boto3  # lazy: keeps the other test modules importable on their own
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("s3", region_name=TEST_REGION)
        client.create_bucket(Bucket=TEST_BUCKET, CreateBucketConfiguration={"LocationConstraint": TEST_REGION})
        yield client


@pytest.fixture
def s3_store(s3):
    from kyapehnu.files.s3 import S3Store  # lazy: the module does not exist before the files domain does

    return S3Store(TEST_BUCKET, TEST_REGION)


@pytest.fixture
def local_dir(tmp_path):
    """The data dir of a LocalStore built by the `store` fixture."""
    path = tmp_path / "store-data"
    path.mkdir()
    return path


@pytest.fixture(params=["local", "s3"])
def store(request, local_dir):
    """Every FileStore adapter: the contract tests run once on each."""
    if request.param == "local":
        from kyapehnu.files.local import LocalStore  # lazy: the module does not exist before the files domain does

        return LocalStore(local_dir)
    return request.getfixturevalue("s3_store")


@pytest.fixture
def plant(store, local_dir, request):
    """`plant(key, data)`: put bytes in the backing storage behind the store's back (no store code involved)."""
    from kyapehnu.files.local import LocalStore  # lazy: the module does not exist before the files domain does

    if isinstance(store, LocalStore):

        def _plant(key, data=b"planted"):
            path = local_dir / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    else:
        client = request.getfixturevalue("s3")

        def _plant(key, data=b"planted"):
            client.put_object(Bucket=TEST_BUCKET, Key=key, Body=data)

    return _plant


@pytest.fixture
def s3_app(s3, make_app):
    """The app on the S3 store: KYAPEHNU_FILES_BUCKET set to the moto bucket."""
    return make_app(files_bucket=TEST_BUCKET)


@pytest.fixture
def s3_calls(s3, monkeypatch):
    """The names of the S3 operations every client makes from now on (the test's own raw client included)."""
    from botocore.client import BaseClient

    calls: list[str] = []
    original = BaseClient._make_api_call

    def _make_api_call(self, operation_name, api_params):
        calls.append(operation_name)
        return original(self, operation_name, api_params)

    monkeypatch.setattr(BaseClient, "_make_api_call", _make_api_call)
    return calls


@pytest.fixture
def s3_fail(s3, monkeypatch):
    """`s3_fail(error, operation=None)`: every S3 call (or only `operation`, like "GetObject") raises `error`, an
    exception instance or a ClientError code. `s3_fail.heal()` restores it. The test's own raw client fails too."""
    from botocore.client import BaseClient
    from botocore.exceptions import ClientError

    state: dict = {"error": None, "operation": None}
    original = BaseClient._make_api_call

    def _make_api_call(self, operation_name, api_params):
        error = state["error"]
        if error is not None and state["operation"] in (None, operation_name):
            raise error
        return original(self, operation_name, api_params)

    monkeypatch.setattr(BaseClient, "_make_api_call", _make_api_call)

    def fail(error, operation=None):
        if isinstance(error, str):
            # The message names the bucket and a key on purpose: neither may come out of the store (NFR-6).
            message = f"leak s3://{TEST_BUCKET}/images/1/thumb/a.webp"
            error = ClientError({"Error": {"Code": error, "Message": message}}, operation or "Unknown")
        state.update(error=error, operation=operation)

    fail.heal = lambda: state.update(error=None, operation=None)
    return fail


@pytest.fixture
def s3_down(s3, monkeypatch):
    """S3 unreachable from the moment the test starts, for every client (the app's store included), until
    `s3_down.heal()`. Like `valkey_fault`, but for the picture store."""
    from botocore.client import BaseClient
    from botocore.exceptions import EndpointConnectionError

    state = {"down": True}
    original = BaseClient._make_api_call

    def _make_api_call(self, operation_name, api_params):
        if state["down"]:
            raise EndpointConnectionError(endpoint_url=f"https://s3.{TEST_REGION}.amazonaws.com")
        return original(self, operation_name, api_params)

    monkeypatch.setattr(BaseClient, "_make_api_call", _make_api_call)
    return types.SimpleNamespace(heal=lambda: state.update(down=False))
