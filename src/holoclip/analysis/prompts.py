from __future__ import annotations

import json

from holoclip.models import ClipCandidate, TranscriptChunk


DISCOVERY_SYSTEM = """You are an editorial researcher for Japanese livestream clips.

Your job is NOT to reward loudness alone. Find moments a human editor might genuinely turn into a clip.

Look for structures such as:
- setup -> contradiction -> payoff
- misunderstanding that escalates
- unexpected confession or unusually candid story
- character gap
- relationship dynamics
- callback or accidental foreshadowing
- a small remark whose meaning becomes funny later
- a coherent mini-story with a beginning and payoff
- game events with a clear setup/reaction/payoff

Talk streams matter as much as game streams.

Be recall-oriented in this first pass: keep plausible candidates rather than only obvious viral moments.
Do not invent context outside the supplied transcript.
Return JSON only.
"""


def discovery_messages(
    chunk: TranscriptChunk,
    *,
    editorial_profile: dict[str, object] | None = None,
) -> list[dict[str, str]]:
    schema = {
        "candidates": [
            {
                "start": 0.0,
                "end": 0.0,
                "working_title": "Japanese working title",
                "setup": "what establishes the situation",
                "development": "what changes or escalates",
                "payoff": "what makes the segment land",
                "why_interesting": "structural reason, not just topic name",
                "traits": ["contradiction", "escalation"],
                "context_requirement": "low|medium|high",
                "confidence": 0.0,
                "warnings": ["context_risk"],
            }
        ]
    }
    learned = ""
    if editorial_profile is not None:
        learned = (
            "Learned editorial profile from trusted human-made clips:\n"
            f"{json.dumps(editorial_profile, ensure_ascii=False)}\n\n"
            "Use this profile as the primary editorial prior. Match observable "
            "mechanisms, required evidence, rejection cues and boundary rules; "
            "do not force a pattern when the transcript does not support it. "
            "The profile describes reusable editing behavior, not topic templates.\n\n"
        )

    user = (
        f"Chunk id: {chunk.id}\n"
        f"Valid time range: {chunk.start:.3f} - {chunk.end:.3f} seconds\n\n"
        + learned
        + "Transcript:\n"
        f"{chunk.text}\n\n"
        "Return candidates whose timestamps stay inside the valid time range. "
        "Zero candidates is allowed. JSON shape example:\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )
    return [
        {"role": "system", "content": DISCOVERY_SYSTEM},
        {"role": "user", "content": user},
    ]


GUIDED_DISCOVERY_SYSTEM = """You are doing a second-pass editorial search over a Japanese livestream transcript.

A first-pass researcher already found some candidates. Your job is NOT to re-rank or restate those moments.
Use the retrieved human-edited examples only as weak evidence for reusable editorial mechanisms and search the transcript for ADDITIONAL moments the first pass may have missed.

Rules:
- do not repeat or slightly rephrase an already-known candidate
- positive examples are mechanisms, not templates
- weak negatives are context-specific omissions, not universally bad content
- prefer a genuinely different moment over a semantically similar copy
- the new moment must be supported by the supplied transcript
- zero new candidates is completely valid
- do not invent context outside the transcript
- keep timestamps inside the valid chunk range

Return JSON only.
"""


def guided_discovery_messages(
    chunk: TranscriptChunk,
    *,
    existing_candidates: list[ClipCandidate],
    retrieval_evidence: dict[str, object],
    editorial_profile: dict[str, object] | None = None,
) -> list[dict[str, str]]:
    schema = {
        "candidates": [
            {
                "start": 0.0,
                "end": 0.0,
                "working_title": "Japanese working title",
                "setup": "what establishes the situation",
                "development": "what changes or escalates",
                "payoff": "what makes the segment land",
                "why_interesting": "structural reason, not just topic name",
                "traits": ["contradiction", "escalation"],
                "context_requirement": "low|medium|high",
                "confidence": 0.0,
                "warnings": ["context_risk"],
            }
        ]
    }
    known = [
        {
            "start": item.start,
            "end": item.end,
            "working_title": item.working_title,
            "setup": item.setup,
            "development": item.development,
            "payoff": item.payoff,
        }
        for item in existing_candidates
    ]
    user = (
        f"Chunk id: {chunk.id}\n"
        f"Valid time range: {chunk.start:.3f} - {chunk.end:.3f} seconds\n\n"
        "Already-known first-pass candidates (do not repeat):\n"
        f"{json.dumps(known, ensure_ascii=False)}\n\n"
        "Retrieved human-editor evidence:\n"
        f"{json.dumps(retrieval_evidence, ensure_ascii=False)}\n\n"
        "Transcript:\n"
        f"{chunk.text}\n\n"
        "Return only ADDITIONAL candidates whose timestamps stay inside the valid time range. "
        "Zero candidates is allowed. JSON shape example:\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )
    return [
        {"role": "system", "content": GUIDED_DISCOVERY_SYSTEM},
        {"role": "user", "content": _profile_instruction(editorial_profile) + user},
    ]


FINAL_REVIEW_SYSTEM = """You are the senior editor reviewing clip ideas from a long livestream.

Select only ideas whose actual described content supports a watchable mini-story, reaction, relationship moment, or meaningful payoff.
Prefer candidates understandable to a new viewer unless fan context is the point.
Penalize candidates where the title sounds better than the described material.
Penalize context-risk and likely misinterpretation.
Do not select duplicates of the same underlying moment.

When retrieved human examples are provided:
- treat positive examples as weak evidence for reusable editorial mechanisms, not templates
- treat weak negatives only as moments an editor omitted in that specific context, not as objectively bad content
- compare setup/development/payoff, contrast, relationship dynamics, callback, escalation and context needs
- do not copy titles, wording, jokes, thumbnails, or exact edit structures
- similarity alone is not proof that a candidate is good
- a genuinely novel candidate may still outrank all retrieved examples

Return JSON only.
"""


def final_review_messages(
    candidates: list[ClipCandidate],
    *,
    top_k: int,
    retrieval_evidence: list[dict[str, object]] | None = None,
    editorial_profile: dict[str, object] | None = None,
) -> list[dict[str, str]]:
    compact = []
    for index, candidate in enumerate(candidates):
        item = {
                "index": index,
                "start": candidate.start,
                "end": candidate.end,
                "working_title": candidate.working_title,
                "setup": candidate.setup,
                "development": candidate.development,
                "payoff": candidate.payoff,
                "why_interesting": candidate.why_interesting,
                "traits": candidate.traits,
                "context_requirement": candidate.context_requirement,
                "confidence": candidate.confidence,
                "warnings": [warning.value for warning in candidate.warnings],
            }
        if retrieval_evidence is not None and index < len(retrieval_evidence):
            item["retrieval"] = retrieval_evidence[index]
        if candidate.editorial_edit_plan is not None:
            item["measured_edit_plan"] = candidate.editorial_edit_plan.model_dump(mode="json")
        compact.append(item)

    user = (
        f"Choose at most {top_k} candidates from the following list.\n"
        "Rank strongest first. Return JSON as "
        '{"ranked":[{"index":0,"confidence":0.0,"reason":"brief editorial reason",'
        '"retrieval_assessment":"how retrieved positives/weak negatives affected the decision"}]}.\n\n'
        + json.dumps(compact, ensure_ascii=False)
    )
    return [
        {"role": "system", "content": FINAL_REVIEW_SYSTEM},
        {"role": "user", "content": _profile_instruction(editorial_profile) + user},
    ]


def _profile_instruction(profile: dict | None) -> str:
    if profile is None:
        return ""
    return ("Learned editorial profile (primary prior; retrieval is supporting evidence):\n"
            + json.dumps(profile, ensure_ascii=False)
            + "\nRequire the observed mechanism and its must-have elements, avoid contextual near misses, "
              "retain necessary setup and reaction, and use learned start/end/cut principles. "
              "Do not force a pattern on unsupported material.\n\n")
