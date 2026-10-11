"""Synthetic source-clock omission fixtures with known role time spans.

This evaluates only deterministic source-selection coverage and evidence
citation contract. Synthetic fixtures are NOT human-rated editorial gold,
real-video semantic model accuracy, or evidence that a cut is funny.
"""
from __future__ import annotations

from holoclip.render_same_source_selection_presence import source_span_coverage
from holoclip.render_same_source_semantic_review import _validate_candidate

VERSION = "holoclip-synthetic-semantic-coverage-v1"
_ROLES = {
    "context": (7, 10),
    "setup": (10, 14),
    "development": (20, 24),
    "payoff": (30, 34),
    "reaction": (34, 38),
}
_FULL = "SOURCE_CLOCK_FULLY_SELECTED"
_NONE = "SOURCE_CLOCK_NOT_SELECTED"
_PARTIAL = "SOURCE_CLOCK_PARTIALLY_SELECTED"
_CASES = (
    ("full_sequence", [(7, 14), (20, 24), (30, 38)], {}),
    ("setup_omitted", [(7, 10), (20, 24), (30, 38)], {"setup": _NONE}),
    ("development_omitted", [(7, 14), (30, 38)], {"development": _NONE}),
    ("payoff_omitted", [(7, 14), (20, 24), (34, 38)], {"payoff": _NONE}),
    ("reaction_omitted", [(7, 14), (20, 24), (30, 34)], {"reaction": _NONE}),
    ("context_omitted", [(10, 14), (20, 24), (30, 38)], {"context": _NONE}),
    ("payoff_clipped", [(7, 14), (20, 24), (30, 32), (34, 38)],
     {"payoff": _PARTIAL}),
    ("replay_does_not_inflate", [(7, 14), (20, 24), (30, 38),
                                (30, 34)], {}),
    ("reordered_is_still_selected", [(30, 38), (20, 24), (7, 14)], {}),
)


def _guardrail_examples() -> dict:
    """Expose an important *remaining* semantic blind spot as a known result."""
    excerpt = {
        "id": "e01", "source_start_seconds": 10,
        "source_end_seconds": 14,
        "source_window_selection_side": "generated_only",
        "source_clock_selection_status": _FULL,
        "unverified_asr_text": "最初の説明",
    }
    case = {"source_timed_unverified_asr": [excerpt]}
    model = {"roles": [
        {"role": role, "status": "INSUFFICIENT_EVIDENCE",
         "evidence_id": None, "quote": None}
        for role in ("setup", "development", "payoff")
    ]}
    model["roles"][0] = {
        "role": "setup", "status": "POSSIBLE_TEXTUAL_REFERENCE",
        "evidence_id": "e01", "quote": "最初の説明",
    }
    correctly_cited = _validate_candidate(model, case)
    hallucinated = {**model, "roles": [dict(x) for x in model["roles"]]}
    hallucinated["roles"][0]["quote"] = "音声にない引用"
    fabricated_quote_rejected = False
    try:
        _validate_candidate(hallucinated, case)
    except ValueError:
        fabricated_quote_rejected = True
    # Correct quotation intentionally attached to the WRONG semantic role;
    # the syntactic citation guard must NOT pretend it knows Japanese semantics.
    wrong_role = {**model, "roles": [dict(x) for x in model["roles"]]}
    wrong_role["roles"][0] = dict(wrong_role["roles"][0])
    wrong_role["roles"][0]["role"] = "payoff"
    wrong_role["roles"][2]["role"] = "setup"
    semantically_wrong_quote_passes = False
    try:
        _validate_candidate(wrong_role, case)
        semantically_wrong_quote_passes = True
    except ValueError:
        pass
    # A semantically suitable ASR quote outside the selected generated
    # timeline is still source-quoted and therefore must not be promoted to
    # "present in output". This independent source-clock gate catches it.
    outside = source_span_coverage(30, 34, [(10, 14), (20, 24)])
    return {
        "verbatim_quote_validator_accepts_exact_quote":
            correctly_cited[0]["quote"] == "最初の説明",
        "fabricated_source_quote_rejected": fabricated_quote_rejected,
        "wrong_role_but_valid_quote_remains_semantically_unverified":
            semantically_wrong_quote_passes,
        "omitted_payoff_source_clock_not_selected":
            outside["source_clock_selection_status"] == _NONE,
        "independent_semantic_role_accuracy_measured": False,
    }


def evaluate_synthetic_semantic_fixtures() -> dict:
    fixtures = []
    for name, generated, deviations in _CASES:
        checks = {}
        for role, (start, end) in _ROLES.items():
            actual = source_span_coverage(start, end, generated)
            expected = deviations.get(role, _FULL)
            checks[role] = {
                "expected_source_clock_selection": expected,
                "observed_source_clock_selection": actual["source_clock_selection_status"],
                "contract_matches_synthetic_oracle":
                    actual["source_clock_selection_status"] == expected,
            }
        fixtures.append({
            "fixture": name,
            "source_clock_contract_passed":
                all(x["contract_matches_synthetic_oracle"] for x in checks.values()),
            "role_span_checks": checks,
        })
    guardrails = _guardrail_examples()
    passed = (
        all(row["source_clock_contract_passed"] for row in fixtures)
        and all(value is True for key, value in guardrails.items()
                if key != "independent_semantic_role_accuracy_measured")
    )
    return {
        "version": VERSION,
        "status": (
            "LOCAL_SYNTHETIC_SOURCE_CLOCK_CONTRACT_PASSED"
            if passed else "LOCAL_SYNTHETIC_SOURCE_CLOCK_CONTRACT_FAILED"
        ),
        "fixtures": fixtures,
        "fixture_count": len(fixtures),
        "synthetic_oracle_span_checks": len(fixtures) * len(_ROLES),
        "synthetic_oracle_checks_passed": sum(
            x["contract_matches_synthetic_oracle"]
            for row in fixtures for x in row["role_span_checks"].values()
        ),
        "guardrails": guardrails,
        "synthetic_oracle_not_real_video_ground_truth": True,
        "does_not_run_live_model_or_asr": True,
        "semantic_human_quality_precision": None,
        "semantic_human_quality_recall": None,
        "real_video_validation_complete": False,
        "production_ready": False,
        "ready": False,
        "publication_performed": False,
    }
