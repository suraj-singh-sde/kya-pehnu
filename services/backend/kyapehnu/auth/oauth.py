"""Google sign-in through authlib (OpenID Connect, authorization code with PKCE)."""

from typing import Final
from urllib.parse import urlsplit

from authlib.integrations.starlette_client import OAuth

from kyapehnu.core.config import Settings

GOOGLE_DISCOVERY: Final = "https://accounts.google.com/.well-known/openid-configuration"
GOOGLE_SCOPE: Final = "openid email profile"
OAUTH_COOKIE: Final = "kyapehnu_oauth"  # state, nonce and PKCE verifier of a sign-in under way, signed
OAUTH_COOKIE_MAX_AGE: Final = 600
OAUTH_COOKIE_PATH: Final = "/api/auth/google"
MAX_NEXT_LENGTH: Final = 2048
HOME: Final = "/"
ASCII_PRINTABLE_FROM: Final = 0x20
DELETE_CHAR: Final = 0x7F


def missing_settings(settings: Settings) -> list[str]:
    """The environment variables sign-in still needs, by name."""
    secret = settings.google_client_secret
    return [
        name
        for name, present in (
            ("KYAPEHNU_GOOGLE_CLIENT_ID", settings.google_client_id),
            ("KYAPEHNU_GOOGLE_CLIENT_SECRET", secret and secret.get_secret_value()),
        )
        if not present
    ]


def build_oauth(settings: Settings) -> OAuth | None:
    """The registry with Google in it, or None when the client id or secret is missing."""
    if not settings.google_configured or settings.google_client_secret is None:
        return None
    oauth = OAuth()
    oauth.register(
        "google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret.get_secret_value(),
        server_metadata_url=GOOGLE_DISCOVERY,
        client_kwargs={"scope": GOOGLE_SCOPE, "code_challenge_method": "S256"},
    )
    return oauth


def safe_next(raw: str | None) -> str:
    """Where to send the browser after sign-in: a path on this site, otherwise the home page."""
    if (
        not raw
        or len(raw) > MAX_NEXT_LENGTH
        or not raw.startswith("/")
        or raw.startswith("//")
        or "\\" in raw
        or any(ord(c) < ASCII_PRINTABLE_FROM or ord(c) == DELETE_CHAR for c in raw)
    ):
        return HOME
    parts = urlsplit(raw)
    return raw if not parts.scheme and not parts.netloc else HOME
