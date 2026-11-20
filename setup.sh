#!/usr/bin/env sh
# Creates .env with a random Valkey password. Safe to re-run: an existing .env is left alone.
set -eu
[ -f .env ] && { echo ".env exists, nothing changed."; exit 0; }
cp .env.example .env
pw=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40)
sed -i.bak "s/^KYAPEHNU_VALKEY_PASSWORD=.*/KYAPEHNU_VALKEY_PASSWORD=$pw/" .env && rm .env.bak
echo ".env written. Next: docker compose up -d --build --wait   then open http://localhost:8080"
