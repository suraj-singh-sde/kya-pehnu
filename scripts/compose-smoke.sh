#!/usr/bin/env bash
# Smoke test of the running Kya Pehnu stack. Start it first:  docker compose up -d --build --wait
# Needs a .env with a Valkey password and (fake) Google client values, and internet access: the login step makes the
# backend fetch Google's discovery document. Steps 1-7 are the ones of the design (LLD 9.3); 8 guards the baked model.
set -euo pipefail

BASE="http://127.0.0.1:${KYAPEHNU_PORT:-8080}"
M="mk${RANDOM}${RANDOM}" # a marker no log line may ever carry
CURL=(curl -s --max-time 30)
WAIT_SECONDS=120
MAX_LOGINS=40 # the limit is 20 a minute; earlier steps spent a few of them

trap 'echo "SMOKE FAIL: command failed at line $LINENO" >&2' ERR

fail() {
  echo "SMOKE FAIL: $*" >&2
  exit 1
}

status() { "${CURL[@]}" -o /dev/null -w '%{http_code}' "$@" || true; } # 000 when curl got no answer
location() { "${CURL[@]}" -o /dev/null -D - "$@" | tr -d '\r' | sed -n 's/^[Ll]ocation: //p'; }
sha256_hex() { if command -v sha256sum >/dev/null; then sha256sum; else shasum -a 256; fi | cut -d' ' -f1; }
valkey() {
  # valkey-cli 8.1 reads REDISCLI_AUTH; VALKEYCLI_AUTH is the name newer versions use
  docker compose exec -T valkey sh -c 'p=$(cat /run/secrets/valkey_password); export REDISCLI_AUTH=$p VALKEYCLI_AUTH=$p; '"$1"
}
no_marker_in_logs() {
  local logs
  logs=$(docker compose logs backend frontend 2>&1)
  if grep -qF "$M" <<<"$logs"; then fail "the marker $M is in the logs ($1)"; fi
}

echo "1. healthy, and the app is served"
"${CURL[@]}" -f "$BASE/readyz" >/dev/null || fail "/readyz is not 200"
"${CURL[@]}" -f "$BASE/" | grep -q '<div id="root">' || fail "/ does not serve the app"
"${CURL[@]}" -f "$BASE/wardrobe" | grep -q '<div id="root">' || fail "/wardrobe does not fall back to the app"

echo "2. signed out: 401, and the login redirects to Google"
[ "$(status "$BASE/api/auth/me")" = 401 ] || fail "/api/auth/me is not 401 without a session"
[[ "$(location "$BASE/api/auth/google/login?next=/$M")" == *accounts.google.com* ]] ||
  fail "the login does not redirect to accounts.google.com (fake Google client values in .env? internet?)"

echo "3. a session without Google; Valkey holds only its hash"
SESSION=$(docker compose exec -T backend python - <<'PY'
from sqlmodel import Session

from kyapehnu.auth.accounts import upsert_google_user
from kyapehnu.auth.sessions import SessionStore
from kyapehnu.core.config import Prefs, get_settings
from kyapehnu.core.db import make_engine, utcnow
from kyapehnu.core.valkey import make_client
from kyapehnu.user.public import ensure_profile

settings = get_settings()
with Session(make_engine(settings.data_dir)) as s:
    user = upsert_google_user(s, sub="smoke", email="smoke@example.invalid", name="Smoke", now=utcnow())
    ensure_profile(user.id, s, Prefs())
    user_id = user.id
print(SessionStore(make_client(settings), settings.session_idle_days * 86400).create(user_id))
PY
)
SESSION=$(tr -d '\r\n' <<<"$SESSION")
[ -n "$SESSION" ] || fail "no session was created"
COOKIE="Cookie: kyapehnu_session=$SESSION"
[ "$(status -H "$COOKIE" "$BASE/api/auth/me")" = 200 ] || fail "/api/auth/me is not 200 with the session"
KEYS=$(valkey 'valkey-cli --scan --pattern "session:*"')
grep -qx "session:$(printf %s "$SESSION" | sha256_hex)" <<<"$KEYS" || fail "Valkey has no key with the sha256 of the session"
DUMP=$(valkey 'valkey-cli --scan | while read -r k; do echo "$k"; valkey-cli GET "$k"; done')
if grep -qF "$SESSION" <<<"$DUMP"; then fail "the session value is stored in Valkey"; fi

echo "4. markers reach no log"
[[ "$(location "$BASE/api/auth/google/callback?code=$M&state=$M")" == */signin?error=state ]] ||
  fail "the callback without a state cookie is not refused with error=state"
status -H "$COOKIE" "$BASE/api/weather?place=$M" >/dev/null || true # any answer: the place must not be logged
no_marker_in_logs "after the callback and a weather request"

echo "5. the backend is down: the frontend is unhealthy and answers 502, and still logs nothing"
docker compose stop backend >/dev/null
CODE=$(status "$BASE/readyz")
[[ "$CODE" != 2* ]] || fail "/readyz answers $CODE with the backend stopped"
[ "$(status "$BASE/api/auth/google/callback?code=$M&state=$M")" = 502 ] || fail "the callback is not 502 with the backend stopped"
no_marker_in_logs "with the backend stopped"
docker compose start backend >/dev/null
for _ in $(seq $((WAIT_SECONDS / 2))); do
  "${CURL[@]}" -f "$BASE/readyz" >/dev/null && break
  sleep 2
done
"${CURL[@]}" -f "$BASE/readyz" >/dev/null || fail "the stack did not become ready again"

echo "6. a forged X-Forwarded-For does not lift the sign-in rate limit"
LIMITED=0
for i in $(seq "$MAX_LOGINS"); do
  HEADERS=$("${CURL[@]}" -o /dev/null -D - -H "X-Forwarded-For: 10.0.0.$i" "$BASE/api/auth/google/login" | tr -d '\r')
  if head -n 1 <<<"$HEADERS" | grep -q ' 429'; then
    grep -qi '^Retry-After:' <<<"$HEADERS" || fail "the 429 has no Retry-After"
    LIMITED=$i
    break
  fi
done
[ "$LIMITED" -gt 0 ] || fail "$MAX_LOGINS logins with different forged addresses were never limited"

echo "7. Valkey and the backend are closed to the host, and Valkey wants a password"
# An unpublished port prints "invalid IP:0" (or nothing, or fails, depending on the Compose version): a host port above 0
# is what counts as published.
if docker compose port backend 8000 2>/dev/null | grep -Eq ':[1-9][0-9]*$'; then fail "the backend port 8000 is published"; fi
if docker compose port valkey 6379 2>/dev/null | grep -Eq ':[1-9][0-9]*$'; then fail "the Valkey port 6379 is published"; fi
[[ "$(docker compose port frontend 8080)" == 127.0.0.1:* ]] || fail "the frontend is not published on 127.0.0.1 only"
REPLY=$(docker compose exec -T backend python -c '
import socket
s = socket.create_connection(("valkey", 6379), timeout=3)
s.sendall(b"PING\r\n")
print(s.recv(64).decode())')
[[ "$REPLY" == -NOAUTH* ]] || fail "Valkey answered PING without a password with: $REPLY"

echo "8. the background-removal model is readable by the runtime user (rembg saves it 0600 for root)"
docker compose exec -T backend python -c 'from rembg import new_session; new_session("u2net")' >/dev/null 2>&1 \
  || fail "the backend cannot load the u2net model (every photo would keep its background)"

echo "SMOKE OK"
