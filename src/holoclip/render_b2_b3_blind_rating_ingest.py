"""Offline ingestion of self-reported blind full-length B2/B3 reviewer JSON.

This validates real existing SHA-pinned full MP4 studies and strict submitted
forms, then summarizes anonymous candidate-slot responses. It cannot verify
that a rater actually watched video, is independent, or supplied true labels.
No model learning, quality approval, automatic adoption or publishing.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from holoclip.render_b2_b3_blind_full_study import (
    VERSION as STUDY_VERSION, _prepare, _study_html, _DIMENSIONS,
)
from holoclip.render_human_edit_context import DEFAULT_GROWTH_STATE, DEFAULT_HUMAN_STORE
from holoclip.render_plan import _hash, _no_links
from holoclip.render_ready_queue import DEFAULT_PREVIEW_ROOT
from holoclip.render_same_source_geometry import DEFAULT_TRANSCRIPT_ROOT
from holoclip.render_same_source_semantic_review import (
    save_semantic_review, validate_semantic_output,
)
from holoclip.render_source_queue import DEFAULT_QUEUE
from holoclip.render_three_way_existing_media_autopilot import _exclusive_run_lock

VERSION = "holoclip-b2-b3-blind-rating-ingest-v1"
RATING_VERSION = "holoclip-blind-human-rating-v1"
_SLOTS = ("candidate-1", "candidate-2")
_PREFS = frozenset((*_SLOTS, "tie", "undecided"))
_MAX_JSON_BYTES = 256 * 1024
_MAX_FORM_BYTES = 16 * 1024
_MAX_RATINGS = 20


def _read_json(path: Path, maximum: int) -> tuple[dict, str]:
    path = _no_links(Path(path))
    if (path.suffix.lower() != ".json" or not path.is_file()
            or path.is_symlink() or not 0 < path.stat().st_size <= maximum):
        raise ValueError("BLIND_RATING_FILE_MISSING_UNSAFE_OR_TOO_LARGE")
    original = path.read_bytes()
    if not 0 < len(original) <= maximum:
        raise ValueError("BLIND_RATING_FILE_CHANGED")
    try:
        parsed = json.loads(original)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("BLIND_RATING_JSON_INVALID") from exc
    if not isinstance(parsed, dict) or path.read_bytes() != original:
        raise ValueError("BLIND_RATING_JSON_CHANGED_OR_NONOBJECT")
    return parsed, hashlib.sha256(original).hexdigest()


def _verify_study(state: dict) -> str:
    """Verify complete public package, private mapping and current original MP4s."""
    if state["status"] != "SUPPORTED":
        raise ValueError("BLIND_RATING_STUDY_NOT_ELIGIBLE")
    root, private = state["root"], state["private"]
    public, public_sha = _read_json(root / "study.json", _MAX_JSON_BYTES)
    mapping, private_sha = _read_json(private, _MAX_JSON_BYTES)
    expected = dict(state["report"], status="BLIND_STUDY_FULL_MEDIA_READY_UNRATED")
    if public != expected:
        raise ValueError("BLIND_RATING_STUDY_PUBLIC_RECEIPT_CHANGED")
    expected_mapping = {}
    for index, role in enumerate(state["order"], start=1):
        source_sha, source_size = _hash(state["paths"][role])
        slot = f"candidate-{index}"
        expected_mapping[slot] = {
            "role": role, "filename": f"{slot}.mp4",
            "sha256": source_sha, "size_bytes": source_size,
        }
        media = _no_links(root / f"{slot}.mp4")
        if (not media.is_file() or media.is_symlink()
                or _hash(media) != (source_sha, source_size)):
            raise ValueError("BLIND_RATING_CANDIDATE_MEDIA_CHANGED")
    original = {
        "version": STUDY_VERSION,
        "status": "BLIND_STUDY_PRIVATE_MAPPING_UNRATED",
        "study_id": state["report"]["study_id"],
        "job_id": state["evidence"]["job_id"],
        "b2_id": state["evidence"]["b2_id"],
        "b3_id": state["evidence"]["b3_id"],
        "playback_pack_receipt_sha256":
            state["evidence"]["playback_pack_receipt_sha256"],
        "role_mapping": expected_mapping,
        "human_labels_obtained": False,
        "automatic_adoption_allowed": False,
        "publication_performed": False,
        "ready": False,
        "production_ready": False,
    }
    if mapping != original:
        raise ValueError("BLIND_RATING_PRIVATE_MAPPING_CHANGED")
    html_path = _no_links(root / "review.html")
    expected_html = _study_html(state["report"]["study_id"]).encode("utf-8")
    if (not html_path.is_file() or html_path.is_symlink()
            or html_path.stat().st_size != len(expected_html)
            or html_path.read_bytes() != expected_html):
        raise ValueError("BLIND_RATING_REVIEW_FORM_CHANGED")
    return hashlib.sha256(
        (public_sha + private_sha).encode("ascii"),
    ).hexdigest()


def _validate_rating(value: dict, study_id: str) -> tuple[dict, str]:
    """Browser exports are self-report; strict structure never proves a rater."""
    keys = {
        "version", "study_id", "claimed_human_rated", "ratings", "preference",
        "independent_human_gold_verified", "automatic_adoption_allowed",
    }
    if (set(value) != keys or value["version"] != RATING_VERSION
            or value["study_id"] != study_id
            or value["claimed_human_rated"] is not True
            or value["independent_human_gold_verified"] is not False
            or value["automatic_adoption_allowed"] is not False
            or type(value["preference"]) is not str
            or value["preference"] not in _PREFS):
        raise ValueError("BLIND_RATING_FORM_SCHEMA_OR_CLAIMS_INVALID")
    scores = value["ratings"]
    if not isinstance(scores, dict) or set(scores) != set(_SLOTS):
        raise ValueError("BLIND_RATING_SLOTS_INVALID")
    verified = {}
    for slot in _SLOTS:
        row = scores[slot]
        if not isinstance(row, dict) or set(row) != set(_DIMENSIONS):
            raise ValueError("BLIND_RATING_DIMENSIONS_INVALID")
        if any(type(row[dim]) is not int or not 1 <= row[dim] <= 5
               for dim in _DIMENSIONS):
            raise ValueError("BLIND_RATING_SCORE_INVALID")
        verified[slot] = {dim: row[dim] for dim in _DIMENSIONS}
    return verified, value["preference"]


def _aggregate(validated: list[tuple[dict, str]]) -> dict:
    n = len(validated)
    dimensions = {
        dim: {
            slot: {
                "unverified_submitted_score_sum": sum(
                    ratings[slot][dim] for ratings, _ in validated),
                "unverified_submitted_mean": round(sum(
                    ratings[slot][dim] for ratings, _ in validated) / n, 3),
            }
            for slot in _SLOTS
        } for dim in _DIMENSIONS
    }
    preferences = Counter(pref for _, pref in validated)
    return {
        "unverified_submission_count": n,
        "dimension_summaries_by_anonymous_slot": dimensions,
        "unverified_preference_counts": {
            name: preferences[name] for name in (*_SLOTS, "tie", "undecided")
        },
    }


def ingest_blind_ratings(
    *, rating_paths: list[Path], queue: Path = DEFAULT_QUEUE,
    example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_PREVIEW_ROOT,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    job_id: str | None = None, proposal_index: int = 0,
    refinement_index: int = 0, candidate_index: int = 0,
    target_index: int = 0, output: Path | None = None,
) -> dict:
    """Read-only default; optional immutable aggregation with rechecked inputs."""
    if (not isinstance(rating_paths, list) or len(rating_paths) > _MAX_RATINGS
            or any(not isinstance(path, Path) for path in rating_paths)):
        raise ValueError("BLIND_RATING_INPUT_LIMIT_INVALID")
    params = {
        "queue": queue, "example_store": example_store,
        "growth_state": growth_state, "preview_root": preview_root,
        "transcript_root": transcript_root, "job_id": job_id,
        "proposal_index": proposal_index, "refinement_index": refinement_index,
        "candidate_index": candidate_index, "target_index": target_index,
    }

    def collect() -> dict:
        state = _prepare(params)
        if state["status"] != "SUPPORTED":
            return {
                "version": VERSION, "status": state["status"],
                "ready": False, "production_ready": False,
                "automatic_adoption_allowed": False,
            }
        study_sha = _verify_study(state)
        seen = set()
        validated = []
        for path in rating_paths:
            item, digest = _read_json(path, _MAX_FORM_BYTES)
            if digest in seen:
                raise ValueError("BLIND_RATING_DUPLICATED_FORM_BYTES")
            seen.add(digest)
            validated.append(_validate_rating(item, state["report"]["study_id"]))
        report = {
            "version": VERSION,
            "status": ("BLIND_RATING_SUBMISSIONS_UNVERIFIED_AGGREGATED"
                       if validated else "BLIND_RATING_NO_SUBMISSIONS"),
            "study_id": state["report"]["study_id"],
            "study_receipts_sha256": study_sha,
            "submitted_rating_sha256": sorted(seen),
            "submitted_rating_file_count": len(validated),
            "reviewer_identities_confirmed": False,
            "distinct_reviewers_confirmed": False,
            "viewed_complete_videos_verified": False,
            "independent_human_gold_verified": False,
            "input_self_report_untrusted": True,
            "source_diverse_holdout_validated": False,
            "human_agreement_measured": False,
            "unverified_ratings_never_promoted": True,
            "original_files_modified": False,
            "model_calls_performed": 0,
            "publication_performed": False,
            "automatic_winner": None,
            "automatic_adoption_allowed": False,
            "ready": False, "production_ready": False,
        }
        if validated:
            report.update(_aggregate(validated))
        return report

    result = collect()
    if output is not None:
        target = validate_semantic_output(Path(output))
        if result["status"] != "BLIND_RATING_SUBMISSIONS_UNVERIFIED_AGGREGATED":
            raise ValueError("BLIND_RATING_NO_SUBMISSIONS_TO_SAVE")
        # The same queue/job cannot be concurrently modified by this command.
        study = _prepare(params)
        if study["status"] != "SUPPORTED":
            raise ValueError("BLIND_RATING_STUDY_CHANGED_BEFORE_LOCK")
        with _exclusive_run_lock(
            _no_links(Path(preview_root)), study["evidence"]["job_id"],
        ):
            if collect() != result:
                raise ValueError("BLIND_RATING_EVIDENCE_CHANGED_BEFORE_SAVE")
            save_semantic_review(result, target)
    return result
