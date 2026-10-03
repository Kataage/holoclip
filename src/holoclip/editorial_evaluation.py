"""Automatic, blinded ON/OFF diagnostics, enabled only after corpus readiness."""

from __future__ import annotations

from pathlib import Path

from holoclip.analysis.candidate_detector import CandidateDetector
from holoclip.editorial_profile import (
    _cached_call,
    _messages,
    editorial_profile_prompt_payload,
    validate_editorial_profile,
)
from holoclip.evaluation_leak import build_evaluation_leak_report
from holoclip.execution import make_input_fingerprint
from holoclip.transcript import chunk_segments, load_transcript


async def run_editorial_ab(
    llm,
    *,
    transcript: Path,
    stream_id: str,
    examples,
    profile,
    fast_model: str,
    strong_model: str,
    stage_cache=None,
    top_k=5,
    fast_reasoning_effort="low",
    strong_reasoning_effort="high",
) -> dict:
    profile = validate_editorial_profile(profile)
    if editorial_profile_prompt_payload(profile) is None:
        raise ValueError("editorial learning readiness=false; no ON/OFF or operator review yet")
    if stream_id in {c.stream_id for c in profile.learning_cards}:
        raise ValueError("ON/OFF requires an unseen source stream")
    leak = build_evaluation_leak_report(transcript, examples)
    if (
        leak.suspected_overlap
        or leak.missing_training_transcripts
        or not leak.significant_candidate_segments
    ):
        raise ValueError("unseen-stream leakage check is incomplete or found training overlap")
    segments = load_transcript(transcript)
    chunks = chunk_segments(segments)
    arms = {}
    # Both arms use identical models, temperatures, transcripts and selector settings.
    # Individual example retrieval is OFF to isolate the corpus editorial prior.
    for name, prior in (("off", None), ("on", profile)):
        detector = CandidateDetector(
            llm,
            fast_model=fast_model,
            strong_model=strong_model,
            fast_reasoning_effort=fast_reasoning_effort,
            strong_reasoning_effort=strong_reasoning_effort,
            final_temperature=0.0,
            stage_cache=stage_cache,
            cache_provider="nvidia_nim",
            editorial_profile=prior,
        )
        broad = await detector.discover_broad(chunks)
        candidates = await detector.final_select(broad, top_k=top_k)
        arms[name] = {
            "broad_count": len(broad),
            "selected_count": len(candidates),
            "candidates": [c.model_dump(mode="json") for c in candidates],
        }
    # Hide which arm used the profile from the automatic diagnostic judge.
    order = ["on", "off"] if int(make_input_fingerprint(stream_id)[0], 16) % 2 else ["off", "on"]
    blinded = {label: arms[name]["candidates"] for label, name in zip(("A", "B"), order)}
    judge = await _cached_call(
        llm,
        _messages(
            "Blindly compare these candidate sets using their actual described material: evidence-grounded interest, "
            "setup/development/payoff completeness, viewer context sufficiency, duplicate burden, "
            "and plausible boundaries. Do not favor titles or verbosity. "
            "Return {winner:A|B|tie, reason:string, A:{completeness:number, context_sufficiency:number}, "
            "B:{completeness:number, context_sufficiency:number}}. This is an automated diagnostic, not human acceptance.",
            blinded,
        ),
        stage="editorial_blind_ab_diagnostic",
        model=strong_model,
        reasoning_effort=strong_reasoning_effort,
        stage_cache=stage_cache,
        cache_provider="nvidia_nim",
        prompt_version="editorial-blind-ab-v1",
    )
    winner = judge.get("winner")
    return {
        "experiment": "corpus-editorial-ab-v1",
        "stream_id": stream_id,
        "profile_fingerprint": profile.fingerprint,
        "leak_check": leak.model_dump(mode="json"),
        "arms": arms,
        "blind_arm_mapping": dict(zip(("A", "B"), order)),
        "automatic_diagnostic": judge,
        "automatic_winner": order[0] if winner == "A" else order[1] if winner == "B" else "tie",
        "human_validation": "not_requested",
        "limitations": [
            "automatic LLM diagnostic is not independent human acceptance",
            "one unseen stream does not establish generalization",
            "discovery/selection comparison isolates the profile; boundary planning is evaluated separately",
        ],
    }
