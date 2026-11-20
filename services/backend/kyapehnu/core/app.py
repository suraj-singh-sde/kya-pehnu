"""Application factory: wires config, database, LLM provider, worker, error handlers, the request guard and routes."""

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import redis
from fastapi import Depends, FastAPI
from limits.storage import Storage
from starlette.middleware.sessions import SessionMiddleware

from kyapehnu import __version__
from kyapehnu.agent.api import chat, debug
from kyapehnu.agent.api import settings as settings_api
from kyapehnu.agent.public import (
    LLMProvider,
    Transport,
    build_transport,
    db_logger,
    known_secrets,
    load_llm_store,
    make_turns,
)
from kyapehnu.auth import api as auth_api
from kyapehnu.auth.deps import current_user
from kyapehnu.auth.oauth import OAUTH_COOKIE, OAUTH_COOKIE_MAX_AGE, OAUTH_COOKIE_PATH, build_oauth, missing_settings
from kyapehnu.auth.ratelimit import make_limiter
from kyapehnu.auth.sessions import SessionStore
from kyapehnu.core import health, housekeeping
from kyapehnu.core.config import LOOPBACK_HOSTS, MAX_BODY_BYTES, Settings, get_settings, load_prefs
from kyapehnu.core.context import Ctx
from kyapehnu.core.db import prepare_database
from kyapehnu.core.errors import InvalidInputError, install_handlers, request_id_var
from kyapehnu.core.middleware import RequestGuardMiddleware
from kyapehnu.core.valkey import make_client
from kyapehnu.files import api as files_api
from kyapehnu.files import public as files
from kyapehnu.user import api as profile_api
from kyapehnu.wardrobe.api import events, feedback, ingest, items, outfits, weather
from kyapehnu.wardrobe.public import Worker
from kyapehnu.weather.openmeteo import WeatherClient

log = logging.getLogger(__name__)
LOG_FORMAT = "%(levelname)s %(name)s request_id=%(request_id)s: %(message)s"
# They log full request URLs (the place name, the sign-in query) at INFO. authlib and the LLM SDKs use httpx2.
CHATTY_HTTP_LOGGERS = ("httpx", "httpcore", "httpx2", "httpcore2")
SECONDS_PER_DAY = 24 * 3600
SESSION_SECRET_BYTES = 32

_base_record_factory = logging.getLogRecordFactory()


def _record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
    record = _base_record_factory(*args, **kwargs)
    record.request_id = request_id_var.get()  # "-" outside a request (worker thread, startup)
    return record


# Installed at import, before basicConfig, so no record can reach the format without a request_id.
logging.setLogRecordFactory(_record_factory)
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


def build_ctx(
    settings: Settings,
    kv: redis.Redis | None,
    transport: Transport | None = None,
    *,
    weather_transport: httpx.BaseTransport | None = None,
) -> Ctx:
    """The database, LLM provider and clients the services share. Used by `create_app` and by the CLI."""
    prefs = load_prefs(settings.config_file)
    engine = prepare_database(settings.data_dir)  # refuses a Phase 3 or unknown database before it writes anything
    saved = load_llm_store(settings.data_dir)
    provider = LLMProvider(
        build_transport(settings, saved) if transport is None else transport, log=db_logger(engine), kv=kv
    )
    return Ctx(
        engine=engine,
        provider=provider,
        prefs=prefs,
        data_dir=settings.data_dir,
        file_store=files.build_store(settings.data_dir, settings.files_bucket, settings.files_region),
        secrets=known_secrets(settings, saved),
        settings=settings,
        # Redirects off: a weather host must never send a request (carrying the place) somewhere else.
        weather=WeatherClient(httpx.Client(transport=weather_transport, follow_redirects=False), kv),
    )


def create_app(
    settings: Settings | None = None,
    transport: Transport | None = None,
    start_worker: bool = True,
    *,
    weather_transport: httpx.BaseTransport | None = None,
    kv: redis.Redis | None = None,
    rate_limit_storage: Storage | None = None,  # tests pass memory; otherwise the counts live in Valkey
) -> FastAPI:
    if settings is None:
        settings = get_settings()
    if settings.dev_login and settings.public_host not in LOOPBACK_HOSTS:
        raise InvalidInputError("dev_login is for a loopback public_url only")
    for name in CHATTY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    owns_kv = kv is None  # a client built here is closed here; a caller's (tests) is the caller's
    kv = make_client(settings) if kv is None else kv
    ctx = build_ctx(settings, kv, transport, weather_transport=weather_transport)
    worker = Worker(ctx)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if start_worker:
            # before the worker, so it never sweeps a file it just wrote
            housekeeping.run(ctx.engine, ctx.files, settings.sweep_max_deletes)
            worker.start()
        try:
            yield
        finally:
            if start_worker:
                worker.stop()
            if ctx.weather is not None:
                ctx.weather.close()
            ctx.engine.dispose()
            if owns_kv:
                kv.close()

    docs = "/docs" if settings.docs_enabled else None
    app = FastAPI(
        title="Kya Pehnu",
        version=__version__,
        lifespan=lifespan,
        docs_url=docs,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.ctx, app.state.worker, app.state.settings, app.state.kv = ctx, worker, settings, kv
    app.state.turns = make_turns(kv)  # one per app: the chats that have a turn running
    app.state.sessions = SessionStore(kv, settings.session_idle_days * SECONDS_PER_DAY)
    app.state.limiter = make_limiter(settings, kv, rate_limit_storage)
    app.state.oauth = build_oauth(settings)
    if app.state.oauth is None:
        log.warning("event=auth_not_configured missing=%s", ",".join(missing_settings(settings)))
    install_handlers(app)
    # The sign-in state (state, nonce, PKCE verifier) rides in a short-lived signed cookie. The key is random per
    # process: a restart ends sign-ins that are under way, nothing more.
    app.add_middleware(
        SessionMiddleware,
        secret_key=secrets.token_urlsafe(SESSION_SECRET_BYTES),
        session_cookie=OAUTH_COOKIE,
        max_age=OAUTH_COOKIE_MAX_AGE,
        same_site="lax",
        https_only=settings.public_https,
        path=OAUTH_COOKIE_PATH,
    )
    app.add_middleware(
        RequestGuardMiddleware,
        max_body_bytes=MAX_BODY_BYTES,
        allowed_hosts=settings.allowed_hosts,
        public_origin=settings.public_origin,
    )
    # Every router but the two that answer signed-out callers is behind the session check, so no route can be missed.
    for router in (
        items.router,
        ingest.router,
        profile_api.router,
        settings_api.router,
        debug.router,
        outfits.router,
        events.router,
        chat.router,
        feedback.router,
        weather.router,
    ):
        app.include_router(router, dependencies=[Depends(current_user)])
    app.include_router(auth_api.router)
    if settings.dev_login:
        from kyapehnu.auth import dev_login  # noqa: PLC0415  # development only: not imported into a normal instance

        log.warning("event=dev_login_enabled")
        app.include_router(dev_login.router)
    app.include_router(health.router)
    app.include_router(files_api.router)
    return app
