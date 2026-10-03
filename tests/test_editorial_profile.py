import pytest

from holoclip.alignment import ALIGNMENT_METHOD
from holoclip.editorial_profile import (
    EditorialPattern,
    PatternMaturity,
    build_editorial_learning_cards,
    editorial_profile_prompt_payload,
    induce_editorial_profile,
)
from holoclip.examples import (
    AlignmentSegmentExample,
    HumanClipExample,
    WeakNegativeExample,
)


def trusted_example(
    example_id: str,
    *,
    stream_id: str,
    start: float = 100.0,
) -> HumanClipExample:
    return HumanClipExample(
        id=example_id,
        clip_url=f"https://youtu.be/{example_id}",
        clip_title=f"clip {example_id}",
        source_url=f"https://youtu.be/{stream_id}",
        stream_id=stream_id,
        source_start=start,
        source_end=start + 60,
        alignment_confidence=0.9,
        alignment_method=ALIGNMENT_METHOD,
        alignment_matched_anchors=6,
        alignment_total_anchors=6,
        alignment_segments=[
            AlignmentSegmentExample(
                clip_start=0,
                clip_end=25,
                source_start=start,
                source_end=start + 25,
                confidence=0.9,
                matched_anchors=3,
            ),
            AlignmentSegmentExample(
                clip_start=25,
                clip_end=60,
                source_start=start + 45,
                source_end=start + 80,
                confidence=0.9,
                matched_anchors=3,
            ),
        ],
        clip_duration_seconds=60,
        setup="ordinary setup",
        development="unexpected turn",
        payoff="clear payoff",
        why_selected="contrast creates a satisfying mini-story",
        traits=["character_gap", "setup_payoff"],
        context_requirement="low",
        weak_negatives=[
            WeakNegativeExample(
                start=start + 25,
                end=start + 40,
                transcript_excerpt="rambling continuation without payoff",
                reason="removed_between_selected_segments",
            )
        ],
        weak_negative_method="coverage-aware-v2",
    )


def test_learning_cards_capture_selection_and_edit_geometry() -> None:
    card = build_editorial_learning_cards([trusted_example("a", stream_id="stream-a")])[0]

    assert card.example_id == "a"
    assert card.selected_source_seconds == 60
    assert card.source_span_seconds == 80
    assert card.removed_source_gap_seconds == [20]
    assert card.weak_negative_refs == ["a:weak:0"]
    assert card.weak_negative_reasons == ["removed_between_selected_segments"]


class FakeLLM:
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.calls = []

    async def chat_json(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if "Assess each proposed pattern" in messages[1]["content"]:
            import json

            data = json.loads(messages[1]["content"].split("\n", 1)[1])
            return {
                "assessments": [
                    {
                        "pattern_id": p["pattern_id"],
                        "supports": True,
                        "reason": "selected evidence supports the mechanism",
                    }
                    for p in data["patterns"]
                ]
            }
        return self.payloads.pop(0)


@pytest.mark.asyncio
async def test_profile_induction_validates_support_and_keeps_small_corpus_unready() -> None:
    examples = [
        trusted_example("a", stream_id="stream-a", start=100),
        trusted_example("b", stream_id="stream-b", start=200),
        trusted_example("c", stream_id="stream-c", start=300),
    ]
    partial = {
        "global_principles": ["payoff matters"],
        "rejection_principles": ["rambling without payoff is weak"],
        "boundary_principles": ["end after payoff"],
        "patterns": [
            {
                "name": "Contrast to payoff",
                "mechanism": "an ordinary setup flips into an unexpected payoff",
                "support_example_ids": ["a", "b", "c"],
                "weak_negative_refs": ["a:weak:0"],
                "confidence": 0.9,
            }
        ],
    }
    consolidated = {
        **partial,
        "patterns": [
            {
                "name": "Contrast to payoff",
                "mechanism": "an ordinary setup flips into an unexpected payoff",
                "observable_cues": ["clear turn"],
                "must_have": ["payoff"],
                "anti_patterns": ["no turn"],
                "boundary_strategy": ["include setup and payoff"],
                "context_requirement": "low",
                "typical_duration_min_seconds": 20,
                "typical_duration_max_seconds": 90,
                "cut_style": "mixed",
                "support_example_ids": ["a", "b", "c", "invented"],
                "weak_negative_refs": ["a:weak:0", "invented:weak:0"],
                "confidence": 0.9,
            }
        ],
    }
    consolidated = {
        **consolidated,
        **{
            k: [
                {"text": text, "support_example_ids": ["a", "b", "c"], "weak_negative_refs": []}
                for text in partial[k]
            ]
            for k in ("global_principles", "rejection_principles", "boundary_principles")
        },
    }
    llm = FakeLLM([{}, {}, {}, consolidated])

    profile = await induce_editorial_profile(
        llm,
        examples,
        model="strong",
        batch_size=12,
    )

    assert profile.trusted_examples == 3
    assert profile.unique_streams == 3
    assert len(profile.patterns) == 1
    pattern = profile.patterns[0]
    assert pattern.maturity is PatternMaturity.CORE
    assert pattern.support_example_ids == ["a", "b", "c"]
    assert pattern.support_stream_ids == ["stream-a", "stream-b", "stream-c"]
    assert pattern.weak_negative_refs == ["a:weak:0"]
    assert profile.readiness.ready is False
    assert any(reason.startswith("trusted_examples") for reason in profile.readiness.reasons)


def test_prompt_payload_uses_only_core_patterns_by_default(learned_ready_profile) -> None:
    profile = learned_ready_profile.model_copy(deep=True)
    profile.patterns.append(
        EditorialPattern(
            id="emerging",
            name="singleton",
            mechanism="one example mechanism",
            support_example_ids=["e0"],
            maturity=PatternMaturity.CORE,
        )
    )
    payload = editorial_profile_prompt_payload(profile)
    assert payload is not None
    assert len(payload["patterns"]) == 5
    assert all(item["name"] != "singleton" for item in payload["patterns"])
