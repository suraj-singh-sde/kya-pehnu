.PHONY: up down logs backup smoke dev dev-api dev-web test test-fast lint typecheck audit licenses

# An AND-expression passes when every part is allowed (MPL-2.0 is pre-approved in governance.md); OR passes on one allowed branch.
LICENSE_ALLOW := MIT;MIT License;MIT-CMU;Apache-2.0;Apache Software License;Apache License 2.0;BSD-2-Clause;BSD-3-Clause;BSD License;ISC;ISC License (ISCL);PSF-2.0;Python Software Foundation License;Python-2.0;0BSD;Zlib;CC0-1.0;Unlicense;The Unlicense (Unlicense);MIT-0;BlueOak-1.0.0;MPL-2.0;Mozilla Public License 2.0 (MPL 2.0);2-clause BSD License;3-Clause BSD License;Apache-2.0 OR BSD-2-Clause;Apache-2.0 OR BSD-3-Clause;MIT AND BSD-3-Clause;MIT AND PSF-2.0;MPL-2.0 AND MIT;BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0;BSD-2-Clause AND Apache-2.0 WITH LLVM-exception
NPM_ALLOW := MIT;Apache-2.0;BSD-2-Clause;BSD-3-Clause;ISC;0BSD;MIT-0;CC0-1.0;BlueOak-1.0.0;Unlicense;Zlib;MPL-2.0

# The whole stack in containers (needs a .env, see .env.example).
up:
	docker compose up -d --build --wait

down:
	docker compose down

logs:
	docker compose logs -f

backup:
	docker compose exec backend kyapehnu backup

# Starts the stack and runs scripts/compose-smoke.sh against it. Afterwards: make down, or docker compose down -v to
# drop the data volume too.
smoke: up
	scripts/compose-smoke.sh

# Both servers run on this computer; Valkey runs in a container (compose.dev.yaml opens it on 127.0.0.1:6379).
# Vite proxies /api and /images, so the public URL is Vite's address.
dev:
	docker compose -f compose.yaml -f compose.dev.yaml up -d --wait valkey
	$(MAKE) -j2 dev-api dev-web

dev-api:
	cd services/backend && KYAPEHNU_DATA_DIR=../../data KYAPEHNU_PUBLIC_URL=http://localhost:5173 \
		KYAPEHNU_VALKEY_URL=redis://127.0.0.1:6379/0 KYAPEHNU_DOCS_ENABLED=true \
		uv run $(if $(wildcard .env),--env-file ../../.env) uvicorn kyapehnu.core.app:create_app --factory --reload \
		--host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-access-log

dev-web:
	cd services/frontend && npm run dev

lint:
	cd services/backend && uv run ruff check . && uv run ruff format --check .

typecheck:
	cd services/backend && uv run mypy --strict kyapehnu
	cd services/frontend && npm run build

test:
	cd services/backend && uv run pytest -n auto --allow-hosts=127.0.0.1,::1 --cov=kyapehnu --cov-fail-under=80
	cd services/backend && uv run pytest -n0 --no-cov --allow-hosts=127.0.0.1,::1 tests/test_load_slow.py
	cd services/frontend && npm run build

test-fast:
	cd services/backend && uv run pytest -x -q -n auto -m "not slow and not live" --timeout=60 --allow-hosts=127.0.0.1,::1

# pip-audit reports no severity, so any finding fails. Ignoring one needs --ignore-vuln <ID> here,
# a comment, and.
audit:
	cd services/backend && uv run pip-audit --skip-editable
	cd services/frontend && npm audit --audit-level=high --omit=dev
	cd services/frontend && npm audit --audit-level=high

# The runtime tree only (no dev group, no heic extra), in its own venv so the dev venv is untouched.
# Match is exact (no partial matching, which would let "X AND GPL" through).
# caniuse-lite is CC-BY-4.0, human-approved for that package only (governance.md exceptions).
licenses:
	cd services/backend && UV_PROJECT_ENVIRONMENT=.venv-image uv sync --no-dev --frozen
	cd services/backend && if uv pip show --python .venv-image/bin/python pillow-heif >/dev/null 2>&1; then echo "pillow-heif in the runtime tree"; exit 1; fi
	cd services/backend && .venv-image/bin/python -c "import kyapehnu.core.app, kyapehnu.wardrobe.ingest.images as i, sys; sys.exit(i.HEIC_AVAILABLE)"
	cd services/backend && uvx --from 'pip-licenses>=5' pip-licenses --python .venv-image/bin/python --from=mixed --allow-only="$(LICENSE_ALLOW)"
	cd services/frontend && npx --yes license-checker-rseidelsohn --production --excludePrivatePackages --excludePackages "caniuse-lite" --onlyAllow "$(NPM_ALLOW)"
