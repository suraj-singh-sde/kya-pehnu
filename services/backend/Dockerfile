FROM ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 AS uv

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

COPY --from=uv /uv /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv U2NET_HOME=/app/models
WORKDIR /app

# The runtime tree only: no dev group and no HEIC extra (ADR 0025). Dependencies first, so a code change reuses the layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY kyapehnu ./kyapehnu
COPY alembic.ini config.example.yaml ./
RUN uv sync --frozen --no-dev

# Bake the background-removal model into the image: the stack must work without a download at first use.
# rembg saves the file 0600 for root; the runtime user below must be able to read it.
RUN /app/.venv/bin/python -c "from rembg import new_session; new_session('u2net')" && chmod -R a+rX /app/models

RUN useradd -u 10001 --create-home kyapehnu && mkdir /data && chown kyapehnu /data
USER kyapehnu
ENV PATH=/app/.venv/bin:$PATH KYAPEHNU_DATA_DIR=/data
EXPOSE 8000

# One worker: the sign-in rate limit, the turn lock and the housekeeping thread assume one process per stack.
CMD ["uvicorn", "kyapehnu.core.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
