# ADR-0025: Backend image without HEIC

Status: Accepted (2026-10-13)

## Context
`pillow-heif` bundles LGPL and GPL codecs, which the governance allow-list does not accept in the core.

## Decision
The backend image is built from the runtime tree only: `uv sync --frozen --no-dev`, no `heic` extra. The extra stays declared in `pyproject.toml` for people who run from source. `make licenses` asserts that `pillow-heif` is absent from the runtime tree. The rembg model `u2net` is baked into the image so no download happens at first use.

## Consequences
iPhone HEIC uploads get the existing friendly "unsupported format" error; the README says to export JPEG. The earlier installer exception for HEIC is obsolete.
