"""Offline review-sampling pack from already saved technical edit ledgers.

This is an UNLABELLED sampling frame, not a human-gold benchmark. The existing
trusted human-clip corpus provides alignment/diversity *context only*, never
labels for black frames, audio clipping or subtitle legibility. No media is
downloaded, decoded, edited or published; bytes are hash-verified locally.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path

from holoclip.example_quality import (
    LearningQuality, evaluate_human_example_learning_quality,
)
from holoclip.examples import human_clip_identity, load_human_clip_store
from holoclip.render_plan import _hash, _no_links
from holoclip.render_quality_review import REVIEW_VERSION

PACK_VERSION = "holoclip-unlabelled-edit-review-pack-v1"
DEFAULT_EXAMPLE_STORE = Path("data/examples/human_clips.jsonl")
DEFAULT_REVIEW_ROOT = Path("data/render-previews")
DEFAULT_PACK = Path("data/render-previews/edit-review-calibration.json")
_JOB = re.compile(r"renderqueue_[a-f0-9]{32}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_KINDS = frozenset({
    "blackout", "freeze", "fast_subtitle", "wide_subtitle",
    "audio_peak", "audio_low_mean",
})
_MEASURED = frozenset({
    "LOCAL_EDIT_DIAGNOSTIC_MEASURED_FLAGS",
    "LOCAL_EDIT_DIAGNOSTIC_MEASURED_NO_FLAGS",
    "LOCAL_EDIT_DIAGNOSTIC_MEASURED_PARTIAL",
})
_MAX_EXAMPLES = 1000
_MAX_JOBS = 40
_MAX_CANDIDATES = 30
_MAX_PER_KIND = 6
_MAX_MANIFEST = 65536
_MAX_PACK_BYTES = 65536


def _read_json(path: Path) -> dict:
    path = _no_links(path)
    if not path.is_file() or not 0 < path.stat().st_size <= _MAX_MANIFEST:
        raise ValueError("LOCAL_EDIT_PACK_LEDGER_UNTRUSTED")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("LOCAL_EDIT_PACK_LEDGER_UNTRUSTED") from exc
    if not isinstance(doc, dict):
        raise ValueError("LOCAL_EDIT_PACK_LEDGER_UNTRUSTED")
    return doc


def _validate_ledger(
    root: Path, ledger: Path,
) -> tuple[dict, list[dict]] | None:
    if ledger.parent != root or not ledger.name.endswith(".review.json"):
        raise ValueError("LOCAL_EDIT_PACK_LEDGER_PATH_INVALID")
    job = ledger.name[:-12]
    if not _JOB.fullmatch(job):
        raise ValueError("LOCAL_EDIT_PACK_JOB_ID_INVALID")
    doc = _read_json(ledger)
    if (
        doc.get("version") != REVIEW_VERSION
        or doc.get("diagnostic_status") not in _MEASURED
        or doc.get("ready") is not False
        or doc.get("production_ready") is not False
        or doc.get("human_editorial_qa_completed") is not False
        or doc.get("publication_performed") is not False
    ):
        raise ValueError("LOCAL_EDIT_PACK_LEDGER_STATE_UNTRUSTED")
    base_sha = doc.get("base_sha256")
    final_sha = doc.get("final_sha256")
    candidates = doc.get("candidates")
    if (
        not isinstance(base_sha, str) or not _SHA.fullmatch(base_sha)
        or not isinstance(final_sha, str) or not _SHA.fullmatch(final_sha)
        or not isinstance(candidates, list)
        or len(candidates) > 300
        or type(doc.get("candidate_count")) is not int
        or doc["candidate_count"] != len(candidates)
    ):
        raise ValueError("LOCAL_EDIT_PACK_LEDGER_UNTRUSTED")
    base = _no_links(root / f"{job}.mp4")
    if not base.is_file() or _hash(base)[0] != base_sha:
        return None  # Existing receipt is stale; do not sample it.
    # The chosen audio may be unmodified, leveled or captioned. Require
    # exact final sha content, not just a filename or a status claim.
    siblings = (
        base,
        _no_links(root / f"{job}.leveled.mp4"),
        _no_links(root / f"{job}.captioned.mp4"),
    )
    final_verified = False
    for file in siblings:
        if file.is_file() and _hash(file)[0] == final_sha:
            final_verified = True
            break
    if not final_verified:
        return None
    for item in candidates:
        if not isinstance(item, dict):
            raise ValueError("LOCAL_EDIT_PACK_CANDIDATE_INVALID")
        kind = item.get("kind")
        clock = item.get("start_seconds")
        if (
            kind not in _KINDS or type(clock) not in (int, float)
            or not 0 <= clock <= 181
            or type(item.get("auto_repair_allowed")) is not bool
            or item["auto_repair_allowed"] is not False
            or item.get("human_editorial_qa_completed") is not False
            or item.get("position") not in {"inside_cut", "cut_boundary"}
            or not isinstance(item.get("segment_ids"), list)
            or not 1 <= len(item["segment_ids"]) <= 32
            or not all(
                isinstance(x, str) and 1 <= len(x) <= 128
                for x in item["segment_ids"]
            )
        ):
            raise ValueError("LOCAL_EDIT_PACK_CANDIDATE_INVALID")
    if (
        doc["diagnostic_status"] == "LOCAL_EDIT_DIAGNOSTIC_MEASURED_FLAGS"
        and not candidates
        or doc["diagnostic_status"] != "LOCAL_EDIT_DIAGNOSTIC_MEASURED_FLAGS"
        and candidates
    ):
        raise ValueError("LOCAL_EDIT_PACK_LEDGER_MISMATCH")
    return doc, candidates


def _corpus_context(store: Path) -> dict:
    """Alignment trust is not an editor's yes/no judgement of visual signals."""
    store = _no_links(store)
    if not store.exists():
        return {
            "trusted_alignment_examples": 0,
            "unique_trusted_source_streams": 0,
            "existing_human_edit_quality_labels": 0,
            "store_available": False,
        }
    if not store.is_file() or store.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("LOCAL_EDIT_PACK_CORPUS_UNTRUSTED")
    examples = load_human_clip_store(store)
    if len(examples) > _MAX_EXAMPLES:
        raise ValueError("LOCAL_EDIT_PACK_CORPUS_TOO_LARGE")
    clips, streams = set(), set()
    for e in examples:
        if evaluate_human_example_learning_quality(e).decision is not LearningQuality.TRUSTED:
            continue
        identity = human_clip_identity(e.clip_url)
        if identity in clips:
            raise ValueError("LOCAL_EDIT_PACK_DUPLICATE_TRUSTED_CLIP")
        clips.add(identity)
        if e.stream_id:
            streams.add(e.stream_id)
    return {
        "trusted_alignment_examples": len(clips),
        "unique_trusted_source_streams": len(streams),
        # Never interpret human editing as explicit 'not anomalous' gold.
        "existing_human_edit_quality_labels": 0,
        "store_available": True,
    }


def compile_review_calibration_pack(
    *, preview_root: Path = DEFAULT_REVIEW_ROOT,
    example_store: Path = DEFAULT_EXAMPLE_STORE,
    max_jobs: int = _MAX_JOBS,
    max_candidates: int = _MAX_CANDIDATES,
) -> dict:
    """Return bounded unlabeled candidate/control pack. No writes or network."""
    if not 1 <= max_jobs <= _MAX_JOBS or not 1 <= max_candidates <= _MAX_CANDIDATES:
        raise ValueError("LOCAL_EDIT_PACK_LIMIT_UNTRUSTED")
    root = _no_links(preview_root)
    corpus = _corpus_context(example_store)
    report = {
        "version": PACK_VERSION,
        "status": "LOCAL_EDIT_PACK_NO_VERIFIED_REVIEWS",
        "reviewed_job_count": 0,
        "stale_media_job_count": 0,
        "candidate_count": 0,
        "unlabelled_controls": [],
        "candidate_samples": [],
        "anomaly_counts": {},
        "human_reference": corpus,
        "calibration_metrics_available": False,
        "false_positive_rate": None,
        "precision": None,
        "recall": None,
        "auto_repair_allowed": False,
        "publication_performed": False,
        "ready": False,
        "production_ready": False,
    }
    if not root.exists():
        return report
    if not root.is_dir():
        raise ValueError("LOCAL_EDIT_PACK_ROOT_UNTRUSTED")
    ledgers = sorted(root.glob("renderqueue_*.review.json"))
    if len(ledgers) > max_jobs:
        # A bounded snapshot is preferable to silently reading an arbitrarily
        # large backlog and misrepresenting it as comprehensive.
        report["status"] = "LOCAL_EDIT_PACK_TOO_MANY_REVIEWS"
        return report
    per_kind: dict[str, list[dict]] = {kind: [] for kind in sorted(_KINDS)}
    controls: list[dict] = []
    all_counts: Counter[str] = Counter()
    stale = 0
    verified = 0
    for ledger in ledgers:
        record = _validate_ledger(root, _no_links(ledger))
        if record is None:
            stale += 1
            continue
        doc, candidates = record
        verified += 1
        job = ledger.name[:-12]
        if not candidates:
            controls.append({
                "job_id": job, "base_sha256": doc["base_sha256"],
                "classification": "NO_DETECTOR_FLAGS_NOT_HUMAN_GOOD",
            })
        for item in candidates:
            kind = item["kind"]
            all_counts[kind] += 1
            per_kind[kind].append({
                "job_id": job,
                "base_sha256": doc["base_sha256"],
                "final_sha256": doc["final_sha256"],
                "kind": kind,
                "at_seconds": round(float(item["start_seconds"]), 3),
                "position": item["position"],
                "segment_count": len(item["segment_ids"]),
                "review_state": "UNLABELLED_REQUIRES_INDEPENDENT_ASSESSMENT",
            })
    # Deterministic round-robin across kinds, not the most common signal
    # dominating the entire evaluation queue. Stable reruns, no random split
    # or invented holdout provenance.
    selected = []
    for i in range(_MAX_PER_KIND):
        for kind in sorted(per_kind):
            if len(selected) >= max_candidates:
                break
            if i < len(per_kind[kind]):
                selected.append(per_kind[kind][i])
    report.update({
        "status": (
            "LOCAL_EDIT_PACK_UNLABELLED_REVIEW_READY" if selected or controls
            else "LOCAL_EDIT_PACK_NO_VERIFIED_REVIEWS"
        ),
        "reviewed_job_count": verified,
        "stale_media_job_count": stale,
        "candidate_count": sum(all_counts.values()),
        "candidate_samples": selected,
        "unlabelled_controls": controls[:max_candidates],
        "anomaly_counts": dict(sorted(all_counts.items())),
    })
    return report


def export_review_calibration_pack(
    *, preview_root: Path = DEFAULT_REVIEW_ROOT,
    example_store: Path = DEFAULT_EXAMPLE_STORE,
    output: Path = DEFAULT_PACK,
    execute: bool = False,
) -> dict:
    report = compile_review_calibration_pack(
        preview_root=preview_root, example_store=example_store,
    )
    if not execute:
        return report
    target = _no_links(output)
    if target.suffix.lower() != ".json" or target.exists():
        raise ValueError("LOCAL_EDIT_PACK_OUTPUT_UNSAFE")
    encoded = (json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ) + "\n").encode("utf-8")
    if len(encoded) > _MAX_PACK_BYTES:
        raise ValueError("LOCAL_EDIT_PACK_OUTPUT_TOO_LARGE")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Do not silently overwrite even a stale calibration file.
    with tempfile.TemporaryDirectory(
        prefix=".holoclip-calibration-", dir=target.parent,
    ) as work:
        staged = Path(work) / "calibration.json"
        staged.write_bytes(encoded)
        try:
            os.link(staged, target)
        except OSError as exc:
            raise ValueError("LOCAL_EDIT_PACK_OUTPUT_ALREADY_EXISTS") from exc
    return report
