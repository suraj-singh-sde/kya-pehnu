"""AC-1, AC-2 (layout part), AC-41: the repository has the services/ layout and nothing of the old installer."""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from kyapehnu.core.cli import main

REPO_ROOT = Path(__file__).parents[3]
PACKAGE_DIR = Path(__file__).parents[1] / "kyapehnu"


@pytest.mark.parametrize("name", ["backend", "frontend", "install.sh", "install.ps1"])
def test_the_old_top_level_paths_are_gone(name):
    assert not (REPO_ROOT / name).exists()


def test_the_two_services_live_under_services():
    assert (REPO_ROOT / "services" / "backend" / "pyproject.toml").is_file()
    assert (REPO_ROOT / "services" / "frontend" / "package.json").is_file()
    assert (REPO_ROOT / "services" / "backend" / "config.example.yaml").is_file()


@pytest.mark.parametrize("target", ["wheel", "serve"])
def test_the_makefile_has_no_wheel_or_serve_target(target):
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert not re.search(rf"^{target}:", makefile, flags=re.MULTILINE)


def test_the_makefile_commands_name_the_services_folders():
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert not re.search(r"cd (backend|frontend)\b", makefile)


def test_the_cli_lists_no_start_serve_or_dev_command():
    out = io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    commands = re.search(r"\{([^}]*)\}", out.getvalue())
    assert exit_info.value.code == 0
    assert commands is not None
    listed = set(commands.group(1).split(","))
    assert {"start", "serve", "dev"}.isdisjoint(listed)
    assert {"migrate", "seed", "backup", "openapi"} <= listed


def test_no_source_file_mentions_a_shortcut():
    offenders = [p for p in PACKAGE_DIR.rglob("*.py") if "shortcut" in p.read_text(encoding="utf-8").lower()]
    assert offenders == []


def test_the_ci_has_no_wheel_or_install_smoke_job():
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert not re.search(r"^  (wheel|install-smoke):", ci, flags=re.MULTILINE)
    assert "install.sh" not in ci
    assert ".whl" not in release


# ---- WP-5: the container stack, CI and the docs (LLD 8.1, 9.2, 9.3; AC-2, 16, 26, 28, 38, 39, 41, 46, 47) ----------

SERVICES = REPO_ROOT / "services"
BACKEND_DOCKERFILE = SERVICES / "backend" / "Dockerfile"
FRONTEND_DOCKERFILE = SERVICES / "frontend" / "Dockerfile"
NGINX_CONF = SERVICES / "frontend" / "nginx.conf"
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "compose-smoke.sh"
DIGEST = r"@sha256:[0-9a-f]{64}"
TURN_MAX_S = (
    1200  # LLD 6: TURN_LOCK_TTL_S (1260 s, the nginx timeout) = TURN_MAX_S + 60. The agent package owns the code.
)
SHA_PINNED_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# Compose's merge tags (`ports: !override []`) are plain sequences / nulls for these checks.
yaml.SafeLoader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node))
yaml.SafeLoader.add_constructor("!reset", lambda loader, node: None)


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(read(path))


@pytest.fixture(scope="module")
def compose() -> dict:
    return load_yaml(REPO_ROOT / "compose.yaml")


@pytest.fixture(scope="module")
def ci() -> dict:
    return load_yaml(REPO_ROOT / ".github" / "workflows" / "ci.yml")


def run_text(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


def nginx_text() -> str:
    return re.sub(r"#[^\n]*", "", read(NGINX_CONF))


def nginx_locations() -> dict[str, str]:
    """location specifier (`/api/chat/`, `= /readyz`) -> the text inside its braces."""
    text, found = nginx_text(), {}
    for match in re.finditer(r"location\s+((?:=\s+)?\S+)\s*\{", text):
        depth, i = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        found[re.sub(r"\s+", " ", match.group(1))] = text[match.end() : i - 1]
    return found


def seconds(value: str) -> int:
    parsed = re.fullmatch(r"(\d+)([sm]?)", value)
    assert parsed, value
    return int(parsed.group(1)) * (60 if parsed.group(2) == "m" else 1)


def directive(block: str, name: str) -> str:
    match = re.search(rf"(?<![\w-]){name}\s+([^;]+);", block)
    assert match, f"{name} is missing"
    return match.group(1).strip()


CONTAINER_FILES = [
    "compose.yaml",
    "compose.dev.yaml",
    ".env.example",
    "services/backend/Dockerfile",
    "services/backend/.dockerignore",
    "services/frontend/Dockerfile",
    "services/frontend/.dockerignore",
    "services/frontend/nginx.conf",
    "scripts/compose-smoke.sh",
]


@pytest.mark.parametrize("name", CONTAINER_FILES)
def test_the_container_files_exist(name):
    assert (REPO_ROOT / name).is_file()


# -- compose.yaml


def test_compose_has_the_three_services_and_a_project_name(compose):
    assert set(compose["services"]) == {"frontend", "backend", "valkey"}
    assert compose["name"] == "kyapehnu"


def test_compose_build_contexts_are_exactly_the_two_service_folders(compose):
    contexts = {}
    for name in ("backend", "frontend"):
        build = compose["services"][name]["build"]
        contexts[name] = (build if isinstance(build, str) else build["context"]).rstrip("/").removeprefix("./")

    assert contexts == {"backend": "services/backend", "frontend": "services/frontend"}
    assert "build" not in compose["services"]["valkey"]


@pytest.mark.parametrize("dockerfile", [BACKEND_DOCKERFILE, FRONTEND_DOCKERFILE], ids=["backend", "frontend"])
def test_a_dockerfile_copies_nothing_from_outside_its_own_folder(dockerfile):
    sources = []
    for line in read(dockerfile).splitlines():
        parts = line.split()
        if parts[:1] == ["COPY"] and not any(p.startswith("--from") for p in parts):
            sources += [p for p in parts[1:-1] if not p.startswith("--")]

    assert sources, "no COPY found"
    assert not [s for s in sources if s.startswith(("..", "/")) or ".." in Path(s).parts]
    assert "ADD " not in read(dockerfile)


def test_valkey_and_the_backend_publish_no_host_port(compose):
    for name in ("valkey", "backend"):
        service = compose["services"][name]
        assert "ports" not in service, name
        assert service.get("network_mode") != "host", name


def test_the_frontend_is_published_on_the_loopback_address_only(compose):
    ports = compose["services"]["frontend"]["ports"]

    assert len(ports) == 1
    assert re.fullmatch(r"127\.0\.0\.1:\$\{KYAPEHNU_PORT:-8080\}:8080", ports[0])


def test_the_valkey_command_is_a_one_element_list_that_reads_the_password_from_the_secret_file(compose):
    valkey = compose["services"]["valkey"]

    assert valkey["entrypoint"] == ["/bin/sh", "-c"]
    assert isinstance(valkey["command"], list) and len(valkey["command"]) == 1  # a string would be split into words
    script = valkey["command"][0]
    assert "/run/secrets/valkey_password" in script
    assert "requirepass" in script and "exec valkey-server" in script
    assert "--requirepass" not in script  # the credential is never in argv
    assert "maxmemory 128mb" in script and "volatile-lru" in script
    assert 'save ""' in script and "appendonly no" in script
    assert "environment" not in valkey and "ports" not in valkey and "volumes" not in valkey


def test_the_valkey_password_is_a_compose_secret_taken_from_the_environment(compose):
    assert compose["services"]["valkey"]["secrets"] == ["valkey_password"]
    assert compose["secrets"]["valkey_password"] == {"environment": "KYAPEHNU_VALKEY_PASSWORD"}
    health = " ".join(compose["services"]["valkey"]["healthcheck"]["test"])
    assert "VALKEYCLI_AUTH" in health and "/run/secrets/valkey_password" in health and "PONG" in health


def test_the_backend_environment_fails_fast_without_a_valkey_password_and_holds_no_literal_secret(compose):
    environment = compose["services"]["backend"]["environment"]
    if isinstance(environment, list):
        environment = dict(item.split("=", 1) for item in environment)

    assert environment["KYAPEHNU_DATA_DIR"] == "/data"
    assert environment["KYAPEHNU_VALKEY_URL"] == "redis://valkey:6379/0"
    assert environment["KYAPEHNU_VALKEY_PASSWORD"].startswith("${KYAPEHNU_VALKEY_PASSWORD:?")
    for key, value in environment.items():
        if re.search(r"SECRET|PASSWORD|API_KEY|TOKEN", key):
            assert str(value).startswith("${"), key
    assert "KYAPEHNU_GOOGLE_CLIENT_ID" in environment and "KYAPEHNU_GOOGLE_CLIENT_SECRET" in environment
    assert "KYAPEHNU_PUBLIC_URL" in environment


def test_valkey_sits_on_the_internal_network_only(compose):
    services, networks = compose["services"], compose["networks"]

    assert networks["store"]["internal"] is True
    assert not (networks["edge"] or {}).get("internal")
    assert services["valkey"]["networks"] == ["store"]
    assert set(services["backend"]["networks"]) == {"edge", "store"}
    assert services["frontend"]["networks"] == ["edge"]


def test_the_data_lives_in_a_named_volume_and_services_start_in_order_of_health(compose):
    services = compose["services"]

    assert "kyapehnu-data:/data" in services["backend"]["volumes"]
    assert "kyapehnu-data" in compose["volumes"]
    assert services["backend"]["depends_on"]["valkey"]["condition"] == "service_healthy"
    assert services["frontend"]["depends_on"]["backend"]["condition"] == "service_healthy"
    assert all("healthcheck" in services[name] for name in services)
    assert services["backend"]["restart"] == "unless-stopped"
    assert "/readyz" in json.dumps(services["backend"]["healthcheck"])
    assert "/readyz" in json.dumps(services["frontend"]["healthcheck"])  # AC-28: backend down means frontend unhealthy


def test_the_compose_images_are_pinned_by_digest(compose):
    images = {name: s["image"] for name, s in compose["services"].items() if "image" in s}

    assert set(images) == {"valkey"}
    assert re.fullmatch(rf"valkey/valkey:8\.1[\w.-]*{DIGEST}", images["valkey"])


def test_the_dev_compose_file_opens_valkey_and_the_backend_on_loopback_only():
    dev = load_yaml(REPO_ROOT / "compose.dev.yaml")

    assert set(dev["services"]) == {"valkey", "backend"}
    assert dev["services"]["valkey"]["ports"] == ["127.0.0.1:6379:6379"]
    assert set(dev["services"]["valkey"]["networks"]) == {"store", "edge"}  # an internal network cannot open a port
    [backend_port] = dev["services"]["backend"]["ports"]
    assert backend_port.startswith("127.0.0.1:") and backend_port.endswith(":8000")
    assert dev["services"]["backend"]["environment"]["KYAPEHNU_DEV_LOGIN"] == "1"


def test_the_compose_file_takes_the_dev_login_from_env_and_opens_no_backend_port():
    prod = load_yaml(REPO_ROOT / "compose.yaml")

    # env-driven and loopback-gated by the app itself (it refuses to start with it on any other public URL)
    assert prod["services"]["backend"]["environment"]["KYAPEHNU_DEV_LOGIN"] == "${KYAPEHNU_DEV_LOGIN:-1}"
    assert "ports" not in prod["services"]["backend"]


# -- Dockerfiles


def from_lines(dockerfile: Path) -> list[tuple[str, str | None]]:
    """(image reference, stage alias) of every FROM."""
    found = []
    for line in read(dockerfile).splitlines():
        match = re.match(r"FROM\s+(?:--\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", line, flags=re.IGNORECASE)
        if match:
            found.append((match.group(1), match.group(2)))
    return found


@pytest.mark.parametrize("dockerfile", [BACKEND_DOCKERFILE, FRONTEND_DOCKERFILE], ids=["backend", "frontend"])
def test_every_base_image_of_a_dockerfile_is_pinned_by_digest(dockerfile):
    stages, refs = set(), []
    for image, alias in from_lines(dockerfile):
        if image not in stages:
            refs.append(image)
        if alias:
            stages.add(alias)
    refs += re.findall(r"--from=(\S+)", read(dockerfile))

    external = [r for r in refs if r not in stages]
    assert external
    assert [r for r in external if not re.search(DIGEST + "$", r)] == []


def test_the_backend_image_runs_as_an_unprivileged_user_with_the_frozen_runtime_tree():
    text = read(BACKEND_DOCKERFILE)

    assert re.search(r"useradd[^\n]*-u\s+10001[^\n]*kyapehnu|useradd[^\n]*kyapehnu[^\n]*-u\s+10001", text)
    assert re.search(r"^USER kyapehnu\s*$", text, flags=re.MULTILINE)
    assert "uv sync --frozen --no-dev" in text
    assert "ghcr.io/astral-sh/uv" in text and "python:3.12-slim-bookworm" in text
    cmd = re.search(r"^CMD\s+(\[.*\])\s*$", text, flags=re.MULTILINE)
    assert cmd, "CMD must be the exec form"
    args = json.loads(cmd.group(1))
    assert args[:2] == ["uvicorn", "kyapehnu.core.app:create_app"] and "--factory" in args
    assert "--no-access-log" in args and "--proxy-headers" in args
    assert args[args.index("--workers") + 1] == "1"


def test_the_frontend_image_serves_the_build_with_unprivileged_nginx_on_8080():
    text = read(FRONTEND_DOCKERFILE)

    assert "npm ci" in text and "npm run build" in text
    assert "nginxinc/nginx-unprivileged" in text
    assert re.search(r"^EXPOSE 8080\s*$", text, flags=re.MULTILINE)
    assert re.search(r"COPY\s+nginx\.conf\s+/etc/nginx/nginx\.conf", text)


@pytest.mark.parametrize(
    ("service", "entries"),
    [("backend", {".venv*", "tests", "data"}), ("frontend", {"node_modules", "dist"})],
)
def test_the_dockerignore_files_keep_local_state_out_of_the_build_context(service, entries):
    lines = {line.strip().rstrip("/") for line in read(SERVICES / service / ".dockerignore").splitlines()}

    assert entries <= lines


# -- nginx


def test_nginx_logs_no_access_line_and_only_critical_errors():
    text = nginx_text()

    assert re.search(r"^\s*access_log\s+off\s*;", text, flags=re.MULTILINE)
    assert re.search(r"^\s*error_log\s+\S+\s+crit\s*;", text, flags=re.MULTILINE)
    assert re.search(r"^\s*server_tokens\s+off\s*;", text, flags=re.MULTILINE)
    assert re.search(r"^\s*pid\s+/tmp/nginx\.pid\s*;", text, flags=re.MULTILINE)
    assert re.search(r"listen\s+8080\s*;", text)


def test_nginx_overwrites_x_forwarded_for_with_the_peer_address_in_every_proxied_location():
    proxied = {spec: body for spec, body in nginx_locations().items() if "proxy_pass" in body}

    assert {"/api/chat/", "/api/", "/images/", "= /readyz"} <= set(proxied)
    assert "$proxy_add_x_forwarded_for" not in nginx_text()  # appending would keep a forged address in front
    for spec in ("/api/chat/", "/api/", "/images/"):
        assert re.search(r"proxy_set_header\s+X-Forwarded-For\s+\$remote_addr\s*;", proxied[spec]), spec
        assert re.search(r"proxy_set_header\s+X-Forwarded-Proto\s+\$scheme\s*;", proxied[spec]), spec
        assert re.search(r"proxy_set_header\s+Host\s+\$http_host\s*;", proxied[spec]), spec
        assert re.search(r"proxy_pass\s+\$backend\s*;", proxied[spec]), spec


def test_the_chat_location_waits_at_least_as_long_as_a_chat_turn_can_run():
    chat = nginx_locations()["/api/chat/"]

    assert seconds(directive(chat, "proxy_read_timeout")) >= TURN_MAX_S
    assert seconds(directive(chat, "proxy_send_timeout")) >= TURN_MAX_S
    assert seconds(directive(chat, "proxy_read_timeout")) == seconds(directive(chat, "proxy_send_timeout"))


@pytest.mark.parametrize("spec", ["/api/", "/images/"])
def test_the_other_proxied_locations_time_out_after_300_seconds(spec):
    body = nginx_locations()[spec]

    assert seconds(directive(body, "proxy_read_timeout")) == 300
    assert seconds(directive(body, "proxy_send_timeout")) == 300


def test_nginx_limits_the_upload_body_like_the_application_and_streams_it():
    text = nginx_text()

    assert re.search(r"client_max_body_size\s+5001m\s*;", text)
    assert re.search(r"client_body_timeout\s+60s\s*;", text)
    assert re.search(r"proxy_request_buffering\s+off\s*;", text)


def test_nginx_health_and_spa_locations():
    locations = nginx_locations()

    assert re.search(r"return\s+200\b", locations["= /livez"])
    assert "proxy_pass" in locations["= /readyz"]  # the frontend is unhealthy while the backend is down
    assert re.search(r"try_files\s+\$uri\s+/index\.html\s*;", locations["/"])
    assert "expires" in locations["/assets/"]
    # the shell must be revalidated on every load, or a cached one names asset hashes an upgrade removed
    assert re.search(r'add_header\s+Cache-Control\s+"no-cache"\s*;', locations["/"])
    # nginx: a location with its own add_header inherits none from the server block, so the shell repeats them
    for header in ("X-Content-Type-Options nosniff", "Referrer-Policy same-origin", "X-Frame-Options DENY"):
        assert re.search(r"add_header\s+" + header + r"\s*;", locations["/"]), header
    assert not [spec for spec in locations if "docs" in spec or "openapi" in spec]  # they fall through to the SPA


def test_nginx_resolves_the_backend_at_request_time_and_sets_the_static_response_headers():
    text = nginx_text()

    assert re.search(r"resolver\s+127\.0\.0\.11\b", text)
    assert re.search(r"set\s+\$backend\s+http://backend:8000\s*;", text)
    for header, value in (
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "same-origin"),
        ("X-Frame-Options", "DENY"),
    ):
        assert re.search(rf"add_header\s+{header}\s+\"?{value}\"?", text), header


# -- CI


def test_ci_has_the_five_jobs(ci):
    assert {"backend-linux", "backend-unit", "frontend", "licenses", "compose-smoke"} <= set(ci["jobs"])


def test_the_linux_job_runs_the_suite_against_a_pinned_valkey_service(ci):
    job = ci["jobs"]["backend-linux"]
    valkey = job["services"]["valkey"]

    assert re.fullmatch(rf"valkey/valkey:8\.1\.10-alpine{DIGEST}", valkey["image"])
    assert valkey["ports"] == ["127.0.0.1:6379:6379"]
    assert "valkey-cli ping" in valkey["options"] and "--health-cmd" in valkey["options"]
    assert "KYAPEHNU_TEST_VALKEY_URL" in json.dumps(job) and "redis://127.0.0.1:6379" in json.dumps(job)
    tests = run_text(job)
    assert "--cov-fail-under=80" in tests and '-m "not live"' in tests and "not slow" not in tests


def test_the_macos_and_windows_job_has_no_valkey_and_skips_slow_tests(ci):
    job = ci["jobs"]["backend-unit"]

    assert job["strategy"]["matrix"]["os"] == ["macos-latest", "windows-latest"]
    assert "services" not in job and "KYAPEHNU_TEST_VALKEY_URL" not in json.dumps(job)
    assert "not slow and not live" in run_text(job)


def test_the_frontend_job_greps_the_build_and_the_sources(ci):
    text = run_text(ci["jobs"]["frontend"])

    assert "grep" in text and "dist/assets" in text  # no absolute API or image URL in the bundle
    assert "ingest/folder" in text  # no folder-import screen in src


def test_the_compose_smoke_job_builds_the_stack_smokes_it_and_always_tears_it_down(ci):
    job = ci["jobs"]["compose-smoke"]
    text = run_text(job)

    assert job["runs-on"].startswith("ubuntu")
    assert {"backend-linux", "backend-unit", "frontend", "licenses"} <= set(job["needs"])
    for needle in (
        "docker compose version",
        "docker compose up -d --build --wait",
        "scripts/compose-smoke.sh",
        "KYAPEHNU_VALKEY_PASSWORD",
        "openssl rand -hex 32",
    ):
        assert needle in text, needle
    always = [s for s in job["steps"] if str(s.get("if", "")).strip("${} ") == "always()"]
    assert any("docker compose logs" in s["run"] and "smoke-logs.txt" in s["run"] for s in always)
    assert any("docker compose down -v" in s["run"] for s in always)


@pytest.mark.parametrize("workflow", ["ci.yml", "release.yml"])
def test_every_action_in_a_workflow_is_pinned_to_a_commit(workflow):
    jobs = load_yaml(REPO_ROOT / ".github" / "workflows" / workflow)["jobs"]
    uses = [step["uses"] for job in jobs.values() for step in job["steps"] if "uses" in step]

    assert uses
    assert [u for u in uses if not SHA_PINNED_ACTION.fullmatch(u)] == []


def test_the_release_workflow_creates_a_release_with_notes_and_no_files():
    release = read(REPO_ROOT / ".github" / "workflows" / "release.yml")

    assert "gh release create" in release
    assert not re.search(r"gh release upload|--attach|files:|dist/|VERSION", release)


# -- Makefile, .env.example


@pytest.mark.parametrize("target", ["up", "down", "logs", "backup", "smoke"])
def test_the_makefile_has_the_stack_targets(target):
    assert re.search(rf"^{target}:", read(REPO_ROOT / "Makefile"), flags=re.MULTILINE)


def test_the_stack_targets_run_compose_and_the_smoke_script():
    makefile = read(REPO_ROOT / "Makefile")

    assert "docker compose up -d --build --wait" in makefile
    assert "docker compose exec backend kyapehnu backup" in makefile
    assert "scripts/compose-smoke.sh" in makefile
    assert "compose.dev.yaml" in makefile  # `make dev` starts only Valkey from it
    assert not re.search(r"^seed:", makefile, flags=re.MULTILINE)  # seeding needs an account: docker compose exec


COMPOSE_VARIABLES = [
    "KYAPEHNU_PORT",
    "KYAPEHNU_PUBLIC_URL",
    "KYAPEHNU_GOOGLE_CLIENT_ID",
    "KYAPEHNU_GOOGLE_CLIENT_SECRET",
    "KYAPEHNU_VALKEY_PASSWORD",
]


@pytest.mark.parametrize("name", COMPOSE_VARIABLES)
def test_the_env_example_lists_every_compose_variable(name):
    assert re.search(rf"^#?\s*{name}=", read(REPO_ROOT / ".env.example"), flags=re.MULTILINE)


@pytest.mark.parametrize(
    "name",
    ["KYAPEHNU_GOOGLE_CLIENT_SECRET", "KYAPEHNU_VALKEY_PASSWORD", "ANTHROPIC_API_KEY", "KYAPEHNU_OPENAI_API_KEY"],
)
def test_the_env_example_commits_no_secret_value(name):
    values = re.findall(rf"^{name}=(.*)$", read(REPO_ROOT / ".env.example"), flags=re.MULTILINE)

    assert values and all(v.strip() in ("", "ollama") for v in values)  # "ollama" is the documented no-key placeholder


def test_the_env_example_tells_how_to_generate_the_valkey_password_and_env_is_git_ignored():
    assert "openssl rand -hex 32" in read(REPO_ROOT / ".env.example")
    assert ".env" in read(REPO_ROOT / ".gitignore").splitlines()


# -- scripts/compose-smoke.sh


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no executable bit")
def test_the_smoke_script_is_executable():
    assert os.access(SMOKE_SCRIPT, os.X_OK)


# On Windows, "bash" on PATH can be the WSL launcher stub, which prints an installation hint and exits 1.
@pytest.mark.skipif(shutil.which("bash") is None or sys.platform == "win32", reason="needs a real bash")
def test_the_smoke_script_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(SMOKE_SCRIPT)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "needle",
    [
        "set -euo pipefail",
        "/readyz",
        "/wardrobe",  # step 1: the SPA fallback
        "/api/auth/me",  # step 2 and 3: 401, then 200 with a session
        "accounts.google.com",
        "session:",  # step 3: the hashed key in Valkey
        "sha256",
        "/api/auth/google/callback",  # step 4: marker values
        "/api/weather",
        "docker compose stop backend",  # step 5: upstream failure
        "docker compose start backend",
        "X-Forwarded-For",  # step 6: the rate limit cannot be bypassed
        "Retry-After",
        "docker compose port backend 8000",  # step 7: exposure
        "docker compose port valkey 6379",
        "NOAUTH",
    ],
)
def test_the_smoke_script_covers_its_steps(needle):
    assert needle in read(SMOKE_SCRIPT)


def test_the_smoke_script_starts_with_a_bash_shebang():
    assert read(SMOKE_SCRIPT).startswith(("#!/usr/bin/env bash", "#!/bin/bash"))


# -- README (AC-41, AC-47, AC-40)


def section(text: str, heading: str) -> str:
    """The text under `heading` (any level) up to the next heading of the same or a higher level."""
    match = re.search(rf"^(#+)\s+{heading}\s*$", text, flags=re.MULTILINE)
    assert match, f"README has no '{heading}' heading"
    level = len(match.group(1))
    rest = text[match.end() :]
    following = re.search(rf"^#{{1,{level}}}\s", rest, flags=re.MULTILINE)
    return rest[: following.start()] if following else rest


# -- ADRs (LLD 11)

ADR_FILES = [
    "0015-server-side-sessions-in-valkey",
    "0016-oauth-state-in-signed-cookie",
    "0017-auth-rate-limit-with-limits",
    "0018-proxy-trust-and-client-address",
    "0019-valkey-trust-boundary",
    "0020-sqlite-volume-and-startup-migration-rule",
    "0021-services-layout-and-explicit-paths",
    "0022-owner-column-and-owner-keyed-image-paths",
    "0023-phase3-import-not-in-place-upgrade",
    "0024-folder-import-cli-and-admin-role",
    "0025-backend-image-without-heic",
    "0026-no-per-user-storage-quota",
]


@pytest.mark.parametrize("name", ADR_FILES)
def test_the_new_adr_exists_with_a_title_and_a_status(name):
    text = read(REPO_ROOT / "docs" / "adr" / f"{name}.md")

    assert text.startswith(f"# ADR-{name[:4]}:")
    assert re.search(r"^Status:\s*Accepted", text, flags=re.MULTILINE)


@pytest.mark.parametrize(
    ("adr", "status"),
    [
        ("0002-host-allowlist-same-origin", r"amended by 0018"),
        ("0003-loopback-check-both-run-modes", r"superseded by 0018"),
        ("0005-in-memory-weather-cache", r"superseded by 0021"),
        ("0013-no-tools-detection-remembered-in-process", r"superseded by 0021"),
    ],
)
def test_the_old_adrs_carry_their_new_status(adr, status):
    status_line = re.search(r"^Status:.*$", read(REPO_ROOT / "docs" / "adr" / f"{adr}.md"), flags=re.MULTILINE)

    assert status_line and re.search(status, status_line.group(0), flags=re.IGNORECASE)
