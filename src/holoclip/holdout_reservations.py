"""Conservative unattended holdout *reservations*, not fabricated evaluation gold.

A reservation is only metadata. It must never be converted into HUMAN_GOLD or a
production qualification without independently validated, source-grounded evidence.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from holoclip.examples import load_human_clip_store


def should_reserve_holdout(stream_id: str, *, every: int = 5) -> bool:
    """Stable source-stream partition independent of process hash seed or search order."""
    if not stream_id.strip():
        raise ValueError("Holdout reservation requires a non-empty source stream ID")
    if every < 2:
        raise ValueError("Holdout partition must leave room for learning streams")
    digest = hashlib.sha256(("holoclip-holdout-v1:" + stream_id).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % every == 0


def holdout_inventory(store: Path, state_path: Path) -> dict:
    """Report reserved metadata only. Does not acquire media or claim reviewed labels."""
    # Imported locally: acquisition uses the pure should_reserve_holdout helper above.
    from holoclip.corpus_growth import CorpusGrowthState

    state = (
        CorpusGrowthState.model_validate_json(state_path.read_text(encoding="utf-8"))
        if state_path.exists() else CorpusGrowthState()
    )
    training_ids = {x.stream_id for x in load_human_clip_store(store) if x.stream_id}
    # Include previously queued learning streams, even before they are ingested.
    training_ids.update(
        x.source_stream_id for x in state.items.values()
        if x.source_stream_id and x.status not in {"holdout_reserved", "split_conflict",
                                                    "discovered", "retryable"}
    )
    reservations = sorted({
        *state.holdout_stream_ids,
        *(x.source_stream_id for x in state.items.values()
          if x.status in {"holdout_reserved", "split_conflict"} and x.source_stream_id),
    })
    conflicts = sorted(set(reservations) & training_ids)
    clips = [
        {"clip_url": x.clip_url, "source_url": x.source_url,
         "source_stream_id": x.source_stream_id, "status": x.status,
         "reason": x.reason}
        for x in state.items.values()
        if x.source_stream_id in reservations
    ]
    clips.sort(key=lambda x: (x["source_stream_id"], x["clip_url"]))
    return {
        "version": "autonomous-holdout-reservations-v1",
        "status": "BLOCKED_SPLIT_CONFLICT" if conflicts else "UNREVIEWED_METADATA_ONLY",
        "reserved_streams": reservations,
        "reserved_clips": clips,
        "conflicted_streams": conflicts,
        "qualification_allowed": False,
        "reviewed_gold_available": False,
        "notes": [
            "Metadata reservations are not held-out measured benchmark cases.",
            "Never set held_out=True or HUMAN_GOLD from this inventory.",
            "Resolve multi-POV/same-event leakage before building an evaluation manifest.",
        ],
    }


def export_holdout_inventory(store: Path, state_path: Path, output: Path) -> dict:
    report = holdout_inventory(store, state_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    temporary.replace(output)
    return report
