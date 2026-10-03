import pytest


from holoclip.editorial_profile import (
    EditorialLearningCard,
    EditorialPattern,
    EditorialProfile,
    LearningReadiness,
    card_fingerprint,
    validate_editorial_profile,
)


@pytest.fixture
def free_cost_guard(tmp_path, monkeypatch):
    """Explicit mock entitlement; no test bypass exists in production code."""
    from test_cost_policy import NOW, guard_at
    from holoclip.providers.cost import CostGuard
    guard = guard_at(tmp_path / "quota.db")
    guard.accounts += guard_at(tmp_path / "quota.db", provider="groq").accounts
    guard.clock = lambda: NOW
    monkeypatch.setattr(CostGuard, "from_env", classmethod(lambda cls: guard))
    monkeypatch.setenv("HOLOCLIP_DATA_CLASS", "public")
    return guard


@pytest.fixture
def learned_ready_profile():
    cards = [
        EditorialLearningCard(
            example_id=f"e{i}",
            stream_id=f"s{i // 2}",
            setup="setup",
            development="turn",
            payoff="payoff",
            why_selected="mechanism",
            provenance={"quality": {"decision": "trusted"}},
            clip_duration_seconds=60,
        )
        for i in range(30)
    ]
    patterns = [
        EditorialPattern(
            id=f"p{n}",
            name=f"Contrast payoff {n}",
            mechanism=f"distinct mechanism {n}",
            observable_cues=["turn"],
            must_have=["payoff"],
            anti_patterns=["no turn"],
            boundary_strategy=["retain setup and reaction"],
            support_assessments={
                f"e{i}": "verified selected evidence" for i in range(n * 6, (n + 1) * 6)
            },
            support_example_ids=[f"e{i}" for i in range(n * 6, (n + 1) * 6)],
        )
        for n in range(5)
    ]
    principles = {
        "global_principles": ["Prefer a clear turn and payoff."],
        "rejection_principles": ["Reject unresolved rambling."],
        "boundary_principles": ["Include enough setup to understand the turn."],
    }
    profile = EditorialProfile(
        model="strong",
        corpus_fingerprint=card_fingerprint(cards),
        trusted_example_ids=[c.example_id for c in cards],
        trusted_examples=30,
        unique_streams=15,
        patterns=patterns,
        learning_cards=cards,
        principle_evidence={
            k: [
                {
                    "text": text,
                    "support_example_ids": [c.example_id for c in cards],
                    "weak_negative_refs": [],
                }
                for text in v
            ]
            for k, v in principles.items()
        },
        **principles,
        readiness=LearningReadiness(
            ready=False,
            trusted_examples=0,
            unique_streams=0,
            core_patterns=0,
            core_coverage_ratio=0,
        ),
    )
    return validate_editorial_profile(profile)
