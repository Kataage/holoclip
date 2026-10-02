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


def discovery_messages(chunk: TranscriptChunk) -> list[dict[str, str]]:
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
    user = (
        f"Chunk id: {chunk.id}\n"
        f"Valid time range: {chunk.start:.3f} - {chunk.end:.3f} seconds\n\n"
        "Transcript:\n"
        f"{chunk.text}\n\n"
        "Return candidates whose timestamps stay inside the valid time range. "
        "Zero candidates is allowed. JSON shape example:\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )
    return [
        {"role": "system", "content": DISCOVERY_SYSTEM},
        {"role": "user", "content": user},
    ]


FINAL_REVIEW_SYSTEM = """You are the senior editor reviewing clip ideas from a long livestream.

Select only ideas whose actual described content supports a watchable mini-story, reaction, relationship moment, or meaningful payoff.
Prefer candidates understandable to a new viewer unless fan context is the point.
Penalize candidates where the title sounds better than the described material.
Penalize context-risk and likely misinterpretation.
Do not select duplicates of the same underlying moment.
Return JSON only.
"""


def final_review_messages(
    candidates: list[ClipCandidate],
    *,
    top_k: int,
) -> list[dict[str, str]]:
    compact = []
    for index, candidate in enumerate(candidates):
        compact.append(
            {
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
        )

    user = (
        f"Choose at most {top_k} candidates from the following list.\n"
        "Rank strongest first. Return JSON as "
        '{"ranked":[{"index":0,"confidence":0.0,"reason":"brief editorial reason"}]}.\n\n'
        + json.dumps(compact, ensure_ascii=False)
    )
    return [
        {"role": "system", "content": FINAL_REVIEW_SYSTEM},
        {"role": "user", "content": user},
    ]
