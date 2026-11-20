"""The visual check before the pick: the decision model grades the collages, the best five go on (judge.py)."""

import hashlib
import io
import logging
from typing import Any

import pytest
from PIL import Image

from kyapehnu.agent.llm.base import LLMProvider, LLMUnavailableError
from kyapehnu.wardrobe.outfits import judge
from kyapehnu.wardrobe.outfits.pick import differs_enough
from tests.fakes import FakeTransport
from tests.outfit_fixtures import item
from tests.test_pick import cand


class _Store:
    """A file store whose every picture is a solid colour taken from its path, so each collage differs."""

    def __init__(self) -> None:
        self.opened: list[str] = []

    def open(self, path: str) -> io.BytesIO:
        self.opened.append(path)
        shade = hashlib.sha1(path.encode()).digest()
        im = Image.new("RGB", (12, 12), (shade[0], shade[1], shade[2]))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        buf.seek(0)
        return buf


class _Judging(FakeTransport):
    """A transport whose decision answers depend on the picture, whatever thread asks first."""

    def __init__(self, verdicts: dict[bytes, tuple[str, str, float]]) -> None:
        super().__init__()
        self.verdicts = verdicts  # picture -> (grade, clash, clash confidence)

    def decide_image(self, *, image: bytes, text: str, questions: list[dict[str, Any]], max_retries: Any = None):
        self.decide_calls.append({"image": image, "text": text, "questions": questions, "max_retries": max_retries})
        grade, clash, sure = self.verdicts[image]
        if isinstance(grade, Exception):
            raise grade
        return [
            {"type": "choice", "name": "grade", "choice": grade, "confidence": 0.8},
            {"type": "choice", "name": "clash", "choice": clash, "confidence": sure},
        ]


def _pictured(category: str, i: int):
    return item(i, category, thumb_path=f"thumb/{i}.webp")


def _looks(n: int) -> list:
    return [
        cand(_pictured("top", 10 + i), _pictured("bottom", 20 + i), _pictured("footwear", 30 + i), score=1 - i / 100)
        for i in range(n)
    ]


def _judged(ranked: list, verdicts: list) -> tuple[judge.Shortlist, _Judging, _Store]:
    store = _Store()
    pictures = judge._pictures(store, ranked[: len(verdicts)])  # a verdict per candidate, in the rules' order
    transport = _Judging(dict(zip(pictures, verdicts, strict=True)))
    store.opened.clear()  # only what the shortlist itself reads
    return judge.shortlist(1, LLMProvider(transport), store, ranked, "party"), transport, store


def test_the_best_grade_comes_first_the_rules_score_breaks_a_tie_and_the_pictures_follow_their_candidates():
    ranked = _looks(4)

    out, transport, _ = _judged(
        ranked, [("fair", "no", 1), ("excellent", "no", 1), ("good", "no", 1), ("good", "no", 1)]
    )

    assert out.judged and [c.ids for c in out.candidates] == [
        ranked[1].ids,
        ranked[2].ids,
        ranked[3].ids,
        ranked[0].ids,
    ]
    assert [transport.verdicts[p][0] for p in out.pictures] == ["excellent", "good", "good", "fair"]
    assert len(transport.decide_calls) == 4 and all(len(c["questions"]) == 2 for c in transport.decide_calls)
    assert all("party" in c["text"] for c in transport.decide_calls)


def test_a_sure_clash_sinks_a_candidate_and_an_unsure_one_does_not():
    ranked = _looks(3)

    out, _, _ = _judged(ranked, [("excellent", "yes", 0.9), ("good", "yes", 0.4), ("fair", "no", 1)])

    assert [c.ids for c in out.candidates] == [ranked[1].ids, ranked[2].ids, ranked[0].ids]


def test_eight_are_graded_and_five_go_on():
    ranked = _looks(10)

    out, transport, _ = _judged(ranked, [("good", "no", 1)] * 8)

    assert len(transport.decide_calls) == 8 and len(out.candidates) == len(out.pictures) == 5
    assert [c.ids for c in out.candidates] == [c.ids for c in ranked[:5]]  # equal grades: the rules' order


def test_one_failed_grade_leaves_the_rules_order_whole(caplog):
    caplog.set_level(logging.INFO)
    ranked = _looks(3)

    out, _, _ = _judged(ranked, [("poor", "no", 1), (LLMUnavailableError("down"), "no", 1), ("excellent", "no", 1)])

    assert not out.judged and [c.ids for c in out.candidates] == [c.ids for c in ranked]
    assert all(p is not None for p in out.pictures)
    assert "event=outfit_judge_skipped reason=LLMUnavailableError" in caplog.text


def test_without_a_decision_model_or_a_provider_nothing_is_asked_and_the_pictures_still_come():
    ranked = _looks(7)
    store = _Store()

    class _Blind(FakeTransport):
        decide_image = None  # type: ignore[assignment]

    for provider in (None, LLMProvider(_Blind())):
        out = judge.shortlist(1, provider, store, ranked, "party")
        assert not out.judged and len(out.candidates) == 5 and all(p is not None for p in out.pictures)


def test_each_item_picture_is_read_once_for_the_whole_batch():
    ranked = [
        cand(_pictured("top", 1), _pictured("bottom", 2), _pictured("footwear", 3 + i), score=0.9) for i in range(4)
    ]  # top and bottom shared

    _, _, store = _judged(ranked, [("good", "no", 1)] * 4)

    assert len(store.opened) == len(set(store.opened)) == 6


def test_the_judge_calls_carry_the_providers_retry_setting():
    ranked = _looks(2)
    store = _Store()
    pictures = judge._pictures(store, ranked)
    transport = _Judging({p: ("good", "no", 1) for p in pictures})

    judge.shortlist(1, LLMProvider(transport).with_max_retries(0), store, ranked, "party")

    assert [c["max_retries"] for c in transport.decide_calls] == [0, 0]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"choice": "good"}, 2.0),
        (
            {
                "choice": "poor",
                "probabilities": [{"value": "poor", "probability": 0.5}, {"value": "excellent", "probability": 0.5}],
            },
            1.5,
        ),
        ({"choice": "fair", "probabilities": {"fair": 0.25, "good": 0.75}}, 1.75),
        ({"choice": "fair", "probabilities": [{"value": "odd", "probability": 1}]}, 1.0),  # unknown values: the choice
        ({"choice": "garment"}, None),
        ({}, None),
    ],
)
def test_the_grade_is_the_expected_value_over_the_probabilities_or_the_choice(answer, expected):
    assert judge.grade_of(answer) == expected


def test_a_clash_counts_only_when_the_model_is_sure():
    assert judge.clashes({"choice": "yes", "confidence": 0.7}) and judge.clashes({"choice": "yes"})
    assert not judge.clashes({"choice": "yes", "confidence": 0.69}) and not judge.clashes({"choice": "no"})


# --- different looks before shoe variants (D12 at the judge) --------------------------------------------------------


def _variants() -> list:
    """Six looks (three tops by two bottoms), each with every one of three pairs of shoes, in score order: a base's
    shoe variants sit next to each other, as the rules rank a small wardrobe."""
    tops = [_pictured("top", 10 + i) for i in range(3)]
    bottoms = [_pictured("bottom", 20 + i) for i in range(2)]
    shoes = [_pictured("footwear", 30 + i) for i in range(3)]
    looks = [(t, b) for t in tops for b in bottoms]
    return [cand(t, b, s, score=1 - n / 100) for n, (t, b) in enumerate(looks) for s in shoes]


def _all_differ(candidates: list) -> bool:
    return all(differs_enough(a.ids, b.ids) for a in candidates for b in candidates if a is not b)


def test_the_judge_grades_different_looks_before_shoe_variants_and_the_best_still_comes_first():
    ranked = _variants()

    out, transport, _ = _judged(ranked, [("good", "no", 1)] * len(ranked))

    assert out.judged and len(transport.decide_calls) == 8 and out.candidates[0].ids == ranked[0].ids
    assert _all_differ(out.candidates)  # five looks: never one look in three pairs of shoes


def test_a_better_graded_shoe_variant_leads_and_different_looks_still_follow_it():
    ranked = _variants()
    verdicts = [("good", "no", 1)] * len(ranked)
    verdicts[1] = ("excellent", "no", 1)  # the top look in its second pair of shoes

    out, transport, _ = _judged(ranked, verdicts)

    assert out.judged and out.candidates[0].ids == ranked[1].ids
    assert _all_differ(out.candidates[:3])  # its own first pair of shoes is not the next look
