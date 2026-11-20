"""Color math shared by dedupe (Phase 1) and color harmony (Phase 2)."""

import colorsys
import math
import re
from collections.abc import Mapping, Sequence

SRGB_LINEAR_CUTOFF = 0.04045
LAB_EPSILON = 0.008856
D65_WHITE_X, D65_WHITE_Z = 0.95047, 1.08883


def hex_to_lab(hex_: str) -> tuple[float, float, float]:
    h = hex_.lstrip("#")
    rgb = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    r, g, b = (c / 12.92 if c <= SRGB_LINEAR_CUTOFF else ((c + 0.055) / 1.055) ** 2.4 for c in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / D65_WHITE_X
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / D65_WHITE_Z

    def f(t: float) -> float:
        return t ** (1 / 3) if t > LAB_EPSILON else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(a: str, b: str) -> float:
    """CIE76 color difference between two hex colors."""
    return math.dist(hex_to_lab(a), hex_to_lab(b))


NEUTRAL_NAME = re.compile(r"\b(?:black|white|grey|gray|navy|beige|brown|denim|cream|ivory|khaki)\b", re.I)
NEUTRAL_SATURATION = 0.15
# (rgb hue, ryb hue) in degrees: the artist's wheel stretches yellow and green, so red-green are opposites.
RYB_HUE_STOPS = ((0, 0), (30, 60), (60, 120), (120, 180), (240, 240), (300, 300), (360, 360))
HUE_BUCKET_DEGREES = 30
MAX_DISTINCT_HUES = 3
# (distance in degrees below which the score applies, score); 150 and wider is opposite or neighbouring: high again
PAIR_SCORES = ((75, 1.0), (105, 0.3), (135, 0.6), (150, 0.3))
CLASHING_HUES = 0.0


def _hls(hex_: str) -> tuple[float, float, float]:
    h = hex_.lstrip("#")
    return colorsys.rgb_to_hls(*(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)))


def is_neutral(color: Mapping[str, str]) -> bool:
    """A colour {hex, name} that goes with everything: a neutral name, or nearly grey."""
    return bool(NEUTRAL_NAME.search(color.get("name", ""))) or _hls(color["hex"])[2] < NEUTRAL_SATURATION


def ryb_hue(hex_: str) -> float:
    """Hue in degrees on the red-yellow-blue wheel."""
    rgb_hue = _hls(hex_)[0] * 360
    for (x0, y0), (x1, y1) in zip(RYB_HUE_STOPS, RYB_HUE_STOPS[1:], strict=False):
        if rgb_hue <= x1:
            return y0 + (y1 - y0) * (rgb_hue - x0) / (x1 - x0)
    return RYB_HUE_STOPS[-1][1]


def hue_pair_score(distance: float) -> float:
    """Score for two hues `distance` degrees apart (0..180)."""
    for below, value in PAIR_SCORES:
        if distance < below:
            return value
    return 1.0


def harmony(colors: Sequence[Mapping[str, str]]) -> float:
    """How well the primary colours of an outfit go together, 0..1. Neutrals go with anything."""
    hues = [ryb_hue(c["hex"]) for c in colors if not is_neutral(c)]
    if len(hues) < 2:
        return 1.0
    if len({int(h // HUE_BUCKET_DEGREES) % 12 for h in hues}) > MAX_DISTINCT_HUES:
        return CLASHING_HUES
    return min(hue_pair_score(min(abs(a - b), 360 - abs(a - b))) for i, a in enumerate(hues) for b in hues[i + 1 :])
