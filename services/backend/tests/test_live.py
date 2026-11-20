"""Opt-in smoke test against the provider configured in .env: uv run pytest -m live"""

import pytest

from kyapehnu.agent.llm import build_transport
from kyapehnu.agent.llm.base import LLMProvider
from kyapehnu.core.config import Settings
from kyapehnu.wardrobe.ingest import images
from kyapehnu.wardrobe.ingest.tagging import analyze_photo
from kyapehnu.wardrobe.seed import draw_silhouette


@pytest.mark.live
def test_real_provider_tags_a_shirt():
    provider = LLMProvider(build_transport(Settings()))
    result = analyze_photo(provider, images.to_jpeg_bytes(draw_silhouette("top", "#1F2A44", "solid")))
    assert result.garments, "expected at least one garment"
    assert all(len(g.bbox) == 4 for g in result.garments)
