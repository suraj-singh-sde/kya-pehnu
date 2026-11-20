"""An in-process Google for the sign-in tests: OIDC discovery, token endpoint, JWKS and userinfo over a mock
transport, with a test RSA key. No network is used.

`install` points every `httpx` / `httpx2` client that is built without an explicit transport at the fake. authlib 1.8
builds its clients from `httpx2` (a separate package: its errors are NOT `httpx.HTTPError` subclasses), so both
libraries are served and each raises its own `ConnectError`. The real authlib flow runs: state, nonce and PKCE in the
redirect, ID-token signature, `iss`, `azp`, `exp` and `nonce` checks. A test reaches a failure by setting a mode or a
claim override on the fake, not by patching the code under test."""

import base64
import hashlib
import time
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

ISSUER = "https://accounts.google.com"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"

CLIENT_ID = "kyapehnu-test.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-test-client-secret-marker"
KID = "test-key-1"

# One key pair for the module: generating RSA keys is slow and nothing needs a fresh one per test.
SIGNING_KEY = RSAKey.generate_key(2048, parameters={"kid": KID, "use": "sig", "alg": "RS256"})
OTHER_KEY = RSAKey.generate_key(2048, parameters={"kid": KID, "use": "sig", "alg": "RS256"})  # same kid, other key


@dataclass(frozen=True)
class Person:
    sub: str = "google-sub-1"
    email: str = "ada@example.com"
    name: str = "Ada Lovelace"
    email_verified: bool = True


@dataclass
class Grant:
    person: Person
    nonce: str
    challenge: str
    redirect_uri: str
    claims: dict[str, Any]


@dataclass
class FakeGoogle:
    redirect_uri: str
    client_id: str = CLIENT_ID
    client_secret: str = CLIENT_SECRET
    discovery: str = "ok"  # ok | connect_error | http_500 | not_json
    token: str = "ok"  # ok | invalid_grant | connect_error | http_500
    jwks: str = "ok"  # ok | connect_error | http_500 | other_key (serves a key that did not sign the token)
    sign_with: RSAKey = field(default_factory=lambda: SIGNING_KEY)
    grants: dict[str, Grant] = field(default_factory=dict)
    requests: list[Any] = field(default_factory=list)
    issued: list[dict[str, str]] = field(default_factory=list)  # every token response body, for the secret sweeps
    served: int = 0

    # --- the browser side -------------------------------------------------------------------------------------
    def approve(
        self,
        authorize_url: str,
        person: Person | None = None,
        *,
        nonce: str | None = None,
        claims: dict[str, Any] | None = None,
        drop: tuple[str, ...] = (),
    ) -> str:
        """The person approves at Google. Returns the callback URL Google redirects the browser to.

        `nonce` replaces the nonce signed into the ID token (a wrong-nonce attack), `claims` overrides or adds ID-token
        claims, `drop` removes claims."""
        parts = urlsplit(authorize_url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTHORIZE_URL
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert q["client_id"] == self.client_id
        assert q["redirect_uri"] == self.redirect_uri
        assert q["response_type"] == "code"
        assert q["code_challenge_method"] == "S256"
        code = f"code-{len(self.grants)}-{hashlib.sha256(q['state'].encode()).hexdigest()[:12]}"
        who = person or Person()
        wanted = dict(claims or {})
        for name in drop:
            wanted[name] = None  # None means: remove the claim
        self.grants[code] = Grant(
            who, q["nonce"] if nonce is None else nonce, q["code_challenge"], q["redirect_uri"], wanted
        )
        return f"{q['redirect_uri']}?code={code}&state={q['state']}"

    # --- the server side --------------------------------------------------------------------------------------
    def handle(self, request: Any, lib: ModuleType = httpx) -> Any:
        """Answer one request; `lib` is the httpx flavour that sent it (it builds the response, raises the errors)."""
        self.requests.append(request)
        url = str(request.url).split("?")[0]
        if url == DISCOVERY_URL:
            return self._discovery(lib)
        if url == TOKEN_URL:
            return self._token(request, lib)
        if url == JWKS_URL:
            return self._jwks(lib)
        if url == USERINFO_URL:
            return lib.Response(401, json={"error": "invalid_token"})  # the flow reads the ID token, never userinfo
        return lib.Response(404, json={"error": f"unknown test url {url}"})

    def _discovery(self, lib: ModuleType) -> Any:
        if self.discovery == "connect_error":
            raise lib.ConnectError("discovery unreachable")
        if self.discovery == "http_500":
            return lib.Response(500, text="boom")
        if self.discovery == "not_json":
            return lib.Response(200, text="<html>captive portal</html>")
        return lib.Response(
            200,
            json={
                "issuer": ISSUER,
                "authorization_endpoint": AUTHORIZE_URL,
                "token_endpoint": TOKEN_URL,
                "jwks_uri": JWKS_URL,
                "userinfo_endpoint": USERINFO_URL,
                "response_types_supported": ["code"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "code_challenge_methods_supported": ["S256", "plain"],
                "scopes_supported": ["openid", "email", "profile"],
            },
        )

    def _jwks(self, lib: ModuleType) -> Any:
        if self.jwks == "connect_error":
            raise lib.ConnectError("jwks unreachable")
        if self.jwks == "http_500":
            return lib.Response(500, text="boom")
        key = OTHER_KEY if self.jwks == "other_key" else SIGNING_KEY
        return lib.Response(200, json={"keys": [key.as_dict(private=False)]})

    def _token(self, request: Any, lib: ModuleType) -> Any:
        if self.token == "connect_error":
            raise lib.ConnectError("token endpoint unreachable")
        if self.token == "http_500":
            return lib.Response(500, text="boom")
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if not self._client_authenticates(request, form):
            return lib.Response(401, json={"error": "invalid_client"})
        grant = self.grants.pop(form.get("code", ""), None)  # a code is single use
        if self.token == "invalid_grant" or grant is None or form.get("redirect_uri") != grant.redirect_uri:
            return lib.Response(400, json={"error": "invalid_grant", "error_description": "Bad Request"})
        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != grant.challenge:
            return lib.Response(400, json={"error": "invalid_grant", "error_description": "Bad PKCE"})
        self.served += 1
        body = {
            "access_token": f"ya29.access-token-marker-{self.served}",
            "refresh_token": f"1//refresh-token-marker-{self.served}",
            "id_token": self.id_token(grant),
            "token_type": "Bearer",
            "expires_in": 3599,
            "scope": "openid https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile",
        }
        self.issued.append(body)
        return lib.Response(200, json=body)

    def _client_authenticates(self, request: Any, form: dict[str, str]) -> bool:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            raw = base64.b64decode(header[6:]).decode()
            return raw == f"{self.client_id}:{self.client_secret}"
        return form.get("client_id") == self.client_id and form.get("client_secret") == self.client_secret

    def id_token(self, grant: Grant) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": self.client_id,
            "azp": self.client_id,
            "sub": grant.person.sub,
            "email": grant.person.email,
            "email_verified": grant.person.email_verified,
            "name": grant.person.name,
            "nonce": grant.nonce,
            "iat": now,
            "exp": now + 3600,
        }
        for name, value in grant.claims.items():
            if value is None:
                claims.pop(name, None)
            else:
                claims[name] = value
        return jwt.encode({"alg": "RS256", "kid": KID}, claims, self.sign_with)

    # --- wiring -----------------------------------------------------------------------------------------------
    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every httpx / httpx2 client built without its own transport (authlib's) talks to this fake."""
        for lib in (httpx, httpx2):
            transport = lib.MockTransport(lambda request, lib=lib: self.handle(request, lib))
            for cls in (lib.AsyncClient, lib.Client):
                original = cls.__init__

                def patched(
                    self_: Any, *args: Any, _original: Any = original, _transport: Any = transport, **kwargs: Any
                ) -> None:
                    if kwargs.get("transport") is None:
                        kwargs["transport"] = _transport
                    _original(self_, *args, **kwargs)

                monkeypatch.setattr(cls, "__init__", patched)

    @property
    def secrets(self) -> list[str]:
        """Everything Google issued or shared that must never be stored or logged (NFR-1, AC-13)."""
        out = [self.client_secret]
        for body in self.issued:
            out += [body["access_token"], body["refresh_token"], body["id_token"]]
        return out


def complete_sign_in(
    client: Any, google: FakeGoogle, person: Person | None = None, *, next: str | None = None, **grant: Any
) -> Any:
    """Sign-in start, approval at Google, callback: the callback response (a redirect, not followed)."""
    params = {} if next is None else {"next": next}
    start = client.get("/api/auth/google/login", params=params, follow_redirects=False)
    assert start.status_code == 302, start.text
    callback = google.approve(start.headers["location"], person, **grant)
    return client.get(callback, follow_redirects=False)
