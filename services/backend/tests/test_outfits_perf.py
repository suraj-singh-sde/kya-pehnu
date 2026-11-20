"""AC-79: with 1,000 items, 2,000 wear logs, cached weather and no provider, suggest takes at most 2 s, swap 1 s.
AC-40: the same limits hold with 500 feedback rows on top (the learned preference joins every score).

Slow: it runs in the full gate, not in the fast loop. Besides the clock, an operation count guards the algorithm
(D18): the number of full scores is bounded by `max_candidates`, whatever the wardrobe size.
"""

import os
import statistics
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as clock

import pytest
from sqlmodel import Session, select

from kyapehnu.wardrobe.models import Feedback, Item, Outfit, WearLog
from kyapehnu.wardrobe.outfits import candidates, preference, scoring
from tests.fakes import OWNER_ID

pytestmark = pytest.mark.slow

# The bounds are for a laptop (NFR); hosted CI runners are shared and about half as fast, so they get twice the time.
SLOW_RUNNER = 2.0 if os.environ.get("CI") else 1.0
SUGGEST_SECONDS = 2.0 * SLOW_RUNNER
SWAP_SECONDS = 1.0 * SLOW_RUNNER
RUNS = 5
MAX_FULL_SCORES = 300
CATEGORIES = (("top", 300), ("bottom", 250), ("dress", 50), ("footwear", 150), ("outerwear", 150), ("accessory", 100))
WEARS = 2000
OUTFITS = 500
FEEDBACK_ROWS = 500
RATED_OUTFITS = 300


def build_wardrobe(s: Session) -> list[Item]:
    items = [
        Item(
            user_id=OWNER_ID,
            name=f"{category} {n}",
            category=category,
            colors=[{"hex": "#336699", "name": "blue"}],
            pattern="solid" if n % 3 else "stripe",
            formality=2 + n % 3,
            warmth=2 + n % 3,  # every item fits a party on a cool evening
        )
        for category, count in CATEGORIES
        for n in range(count)
    ]
    s.add_all(items)
    s.commit()
    return items


def build_history(s: Session, items: list[Item]) -> None:
    ids = [i.id for i in items]
    outfits = [
        Outfit(
            user_id=OWNER_ID,
            item_ids=ids[k : k + 3],
            item_key=",".join(map(str, sorted(ids[k : k + 3]))),
            name=f"Outfit {k}",
        )
        for k in range(OUTFITS)
    ]
    s.add_all(outfits)
    s.commit()
    today = date.today()
    s.add_all(
        WearLog(
            user_id=OWNER_ID,
            outfit_id=outfits[k % OUTFITS].id if k % 4 else None,
            item_ids=outfits[k % OUTFITS].item_ids,
            worn_on=today - timedelta(days=k % 120),
            audience_tags=["Priya"] if k % 5 == 0 else [],
        )
        for k in range(WEARS)
    )
    s.commit()


def median_seconds(run: Callable[[], None]) -> float:
    run()  # warm-up
    times = []
    for _ in range(RUNS):
        start = time.perf_counter()
        run()
        times.append(time.perf_counter() - start)
    return statistics.median(times)


def build_feedback(s: Session) -> None:
    """500 thumbs over 300 of the outfits (some outfits are rated more than once)."""
    outfit_ids = [o.id for o in s.exec(select(Outfit).order_by(Outfit.id))][:RATED_OUTFITS]
    s.add_all(
        Feedback(user_id=OWNER_ID, outfit_id=outfit_ids[k % RATED_OUTFITS], rating=-1 if k % 3 == 0 else 1)
        for k in range(FEEDBACK_ROWS)
    )
    s.commit()


def measure_suggest_and_swap(client, fake_weather, monkeypatch, *, feedback: bool) -> list[dict]:
    client.app.state.ctx.settings = None  # no provider
    fake_weather.values["apparent_temperature"] = 20.0  # a cool evening: layers are optional, so every slot is used
    with Session(client.app.state.ctx.engine) as s:
        items = build_wardrobe(s)
        build_history(s, items)
        if feedback:
            build_feedback(s)
    event = {
        "type": "party",
        "starts_at": datetime.combine(date.today() + timedelta(days=1), clock(19, 0)).isoformat(),
        "place": "Gurugram",
        "audience_tags": ["Priya"],
    }
    first = client.post("/api/outfits/suggest", json={"event": event})  # fills the weather cache
    assert first.status_code == 200 and len(first.json()["suggestions"]) == 3
    event_id = first.json()["event"]["id"]
    outfit = first.json()["suggestions"][0]
    swap_body = {"event_id": event_id, "item_ids": outfit["item_ids"], "item_id": outfit["item_ids"][0]}

    scored, loads = [], []
    real_score, real_load = scoring.score, preference.load
    monkeypatch.setattr(candidates, "score", lambda *a, **k: scored.append(1) or real_score(*a, **k))
    monkeypatch.setattr(preference, "load", lambda *a, **k: loads.append(1) or real_load(*a, **k))

    def suggest() -> None:
        assert client.post("/api/outfits/suggest", json={"event_id": event_id}).status_code == 200

    def swap() -> None:
        assert client.post("/api/outfits/swap", json=swap_body).status_code == 200

    suggest_time = median_seconds(suggest)
    full_scores = len(scored) // (RUNS + 1)
    suggest_loads = len(loads)
    swap_time = median_seconds(swap)

    assert full_scores == MAX_FULL_SCORES  # the wardrobe has far more outfits than the cap
    assert suggest_loads == RUNS + 1  # the learned preference is read once per request, never once per outfit
    assert len(loads) - suggest_loads == RUNS + 1
    assert suggest_time <= SUGGEST_SECONDS, f"suggest median {suggest_time:.2f}s"
    assert swap_time <= SWAP_SECONDS, f"swap median {swap_time:.2f}s"
    assert len(fake_weather.calls("forecast")) == 1  # every later request came from the weather cache
    return first.json()["suggestions"]


def test_suggest_and_swap_stay_inside_their_time_and_work_bounds(client, fake_weather, monkeypatch):
    measure_suggest_and_swap(client, fake_weather, monkeypatch, feedback=False)


def test_suggest_and_swap_stay_inside_their_time_with_500_feedback_rows(client, fake_weather, monkeypatch):
    suggestions = measure_suggest_and_swap(client, fake_weather, monkeypatch, feedback=True)

    assert any(s["parts"]["preference"] != 0.5 for s in suggestions)  # the ratings really are in the score
