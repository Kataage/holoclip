"""Bounded read-only SHA/receipt checks of previously cached public-video sections.

A valid local cache receipt establishes *local byte provenance* only. Neither
an yt-dlp receipt nor a matching file hash verifies the original Hololive
stream's identity, editor intent, rights, PTS, or independently reviewed gold.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from holoclip.corpus_growth import CorpusGrowthState
from holoclip.providers.source.yt_dlp import _safe_component

VERSION = "unreviewed-video-section-cache-attestation-v1"
MAX_CLIPS = 4
MAX_SECTIONS = 48
MAX_SECTION_BYTES = 64 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MEDIA = re.compile(r"media\.[a-zA-Z0-9]{2,10}\Z")
_NO_GOLD = {
    "actual_public_video_verified": False,
    "source_identity_verified": False,
    "reviewed_gold_available": False,
    "qualification_allowed": False,
    "ready": False,
    "production_ready": False,
}


def _sha(value: object) -> bool:
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _digest(media: Path) -> str:
    digest = hashlib.sha256()
    with media.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_section(
    root: Path, *, source_id: str, source_url: str,
    start: float, end: float, expected_sha: str,
) -> str:
    """Never download, follow a symlink, or trust the receipt without hashing."""
    identity = hashlib.sha256(
        f"yt-section-v1:{source_id}:{start:.3f}:{end:.3f}".encode("utf-8")
    ).hexdigest()[:24]
    source_root = root / "original" / _safe_component(source_id)
    section_root = source_root / "editing_video_sections"
    folder = section_root / identity
    receipt_path = folder / "section-integrity.json"
    # Reject symlinks at every boundary; the cache path is derived only from
    # fixed components and sanitized metadata IDs, never a persisted path.
    if any(p.is_symlink() for p in (
        root, root / "original", source_root, section_root, folder,
        receipt_path,
    )):
        return "BLOCKED_UNSAFE_CACHE_LINK"
    if not receipt_path.is_file():
        return "UNKNOWN_SOURCE_CACHE_MISSING"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return "BLOCKED_MALFORMED_CACHE_RECEIPT"
    if not isinstance(receipt, dict):
        return "BLOCKED_MALFORMED_CACHE_RECEIPT"
    identity_expected = {
        "version": "yt-range-cache-v1",
        "source_id": source_id,
        "source_url": source_url,
        "requested_start": round(start, 3),
        "requested_end": round(end, 3),
    }
    if any(receipt.get(key) != value for key, value in identity_expected.items()):
        return "BLOCKED_CACHE_IDENTITY_OR_RANGE_MISMATCH"
    filename = receipt.get("filename")
    if (not isinstance(filename, str) or _MEDIA.fullmatch(filename) is None
            or not _sha(receipt.get("sha256"))
            or receipt["sha256"] != expected_sha):
        return "BLOCKED_CACHE_SHA_OR_FILENAME_MISMATCH"
    media = folder / filename
    if media.is_symlink():
        return "BLOCKED_UNSAFE_CACHE_LINK"
    try:
        size = media.stat().st_size
        if not media.is_file() or not 0 < size <= MAX_SECTION_BYTES:
            return "UNKNOWN_CACHE_MEDIA_MISSING_OR_TOO_LARGE"
        if _digest(media) != expected_sha:
            return "BLOCKED_CACHE_MEDIA_BYTES_MISMATCH"
    except OSError:
        return "UNKNOWN_CACHE_MEDIA_READ_FAILURE"
    return "VERIFIED_LOCAL_SOURCE_SECTION_BYTES_AND_RECEIPT_NOT_GOLD"


def audit_cached_source_video_sections(
    *, state_path: Path, output_root: Path, max_clips: int = MAX_CLIPS,
    clip_keys: list[str] | None = None,
) -> dict:
    """Audit current persisted latest observations, with a *visible* scope cap."""
    if type(max_clips) is not int or not 1 <= max_clips <= MAX_CLIPS:
        raise ValueError("Invalid bounded source cache audit clip limit")
    result = {
        "version": VERSION,
        "status": "UNKNOWN_NO_SOURCE_CACHE_EVIDENCE",
        "clip_budget": max_clips,
        "section_budget": MAX_SECTIONS,
        "clip_selection": (
            "CURRENT_RUN_ATTEMPTED_CLIPS"
            if clip_keys is not None else "PENDING_SORTED_PREFIX"
        ),
        "total_pending_clips": 0,
        "inspected_clips": 0,
        "eligible_latest_source_section_records": 0,
        "deduplicated_section_records_checked": 0,
        "not_checked_section_records": 0,
        "cache_section_status_counts": {},
        "complete_pending_queue_audited": False,
        **_NO_GOLD,
    }
    if not state_path.is_file():
        return result
    if state_path.is_symlink():
        raise ValueError("Unsafe corpus state symlink")
    state = CorpusGrowthState.model_validate_json(
        state_path.read_text(encoding="utf-8")
    )
    pending = sorted(
        (key, item) for key, item in state.items.items()
        if item.status == "source_visual_pending"
    )
    result["total_pending_clips"] = len(pending)
    if clip_keys is None:
        chosen = pending[:max_clips]
    else:
        if (not isinstance(clip_keys, list) or len(clip_keys) > max_clips
                or any(not isinstance(key, str) for key in clip_keys)
                or len(set(clip_keys)) != len(clip_keys)):
            raise ValueError("Invalid bounded current-run cache audit keys")
        by_key = dict(pending)
        if any(key not in by_key for key in clip_keys):
            raise ValueError("Attempted clip is no longer pending")
        chosen = [(key, by_key[key]) for key in clip_keys]
    seen: set[tuple] = set()
    statuses = Counter()
    omitted = 0
    for _, item in chosen:
        scout = item.source_scout
        if not isinstance(scout, dict):
            raise ValueError("Missing persisted source shortlist")
        candidates = scout.get("candidate_sources")
        if not isinstance(candidates, list):
            raise ValueError("Malformed persisted source shortlist")
        by_id = {}
        for candidate in candidates:
            if (not isinstance(candidate, dict)
                    or not isinstance(candidate.get("source_stream_id"), str)
                    or not isinstance(candidate.get("source_url"), str)):
                raise ValueError("Malformed candidate source metadata")
            ident = candidate["source_stream_id"]
            if ident in by_id:
                raise ValueError("Ambiguous cached source identity")
            by_id[ident] = candidate["source_url"]
        probe = scout.get("visual_probe")
        if probe is None:
            result["inspected_clips"] += 1
            continue
        observations = probe.get("candidate_observations") if isinstance(probe, dict) else None
        if not isinstance(observations, list):
            raise ValueError("Invalid saved source probe records")
        for observation in observations:
            if not isinstance(observation, dict):
                raise ValueError("Invalid source video observation")
            windows = observation.get("sampled_source_windows")
            if not isinstance(windows, list):
                raise ValueError("Invalid sampled original video windows")
            if not windows:
                continue
            source_id = observation.get("source_stream_id")
            if source_id not in by_id:
                raise ValueError("Unknown cached source identity")
            for section in windows:
                if not isinstance(section, dict):
                    raise ValueError("Malformed persisted source window")
                requested = section.get("requested_range")
                digest = section.get("media_sha256")
                if (not isinstance(requested, list) or len(requested) != 2
                        or any(type(t) not in (int, float) for t in requested)
                        or not 0 <= requested[0] < requested[1]
                        or not 5 <= requested[1] - requested[0] <= 120
                        or not _sha(digest)):
                    raise ValueError("Malformed original section range or SHA")
                result["eligible_latest_source_section_records"] += 1
                start, end = requested
                key = (source_id, by_id[source_id], start, end, digest)
                if key in seen:
                    continue
                seen.add(key)
                if result["deduplicated_section_records_checked"] >= MAX_SECTIONS:
                    omitted += 1
                    continue
                status = _verify_section(
                    output_root, source_id=source_id,
                    source_url=by_id[source_id],
                    start=start, end=end, expected_sha=digest,
                )
                statuses[status] += 1
                result["deduplicated_section_records_checked"] += 1
        result["inspected_clips"] += 1
    result["not_checked_section_records"] = omitted
    result["cache_section_status_counts"] = dict(sorted(statuses.items()))
    full = len(pending) == len(chosen) and omitted == 0
    result["complete_pending_queue_audited"] = full
    verified = statuses["VERIFIED_LOCAL_SOURCE_SECTION_BYTES_AND_RECEIPT_NOT_GOLD"]
    if not seen:
        result["status"] = "UNKNOWN_NO_SAVED_SOURCE_SECTION_RECORDS"
    elif not full:
        result["status"] = "UNKNOWN_PARTIAL_BOUNDED_CACHE_AUDIT"
    elif verified == len(seen):
        result["status"] = "VERIFIED_ONLY_LOCAL_SOURCE_SECTION_BYTES_NOT_GOLD"
    else:
        result["status"] = "UNKNOWN_SOURCE_CACHE_GAPS_OR_INTEGRITY_FAILURES"
    return result


def checked_cached_video_attestation(value: dict) -> dict:
    """Fail closed on a forged 'verified' record in a saved collector report."""
    if (not isinstance(value, dict) or value.get("version") != VERSION
            or value.get("clip_selection") not in {
                "CURRENT_RUN_ATTEMPTED_CLIPS", "PENDING_SORTED_PREFIX",
            }
            or any(value.get(k) is not v for k, v in _NO_GOLD.items())
            or value.get("status") not in {
                "UNKNOWN_NO_SOURCE_CACHE_EVIDENCE",
                "UNKNOWN_NO_SAVED_SOURCE_SECTION_RECORDS",
                "UNKNOWN_PARTIAL_BOUNDED_CACHE_AUDIT",
                "UNKNOWN_SOURCE_CACHE_GAPS_OR_INTEGRITY_FAILURES",
                "VERIFIED_ONLY_LOCAL_SOURCE_SECTION_BYTES_NOT_GOLD",
            }):
        raise ValueError("Malformed or promoted cached video attestation")
    fields = (
        "clip_budget", "section_budget", "total_pending_clips",
        "inspected_clips", "eligible_latest_source_section_records",
        "deduplicated_section_records_checked", "not_checked_section_records",
    )
    if any(type(value.get(k)) is not int or value[k] < 0 for k in fields):
        raise ValueError("Invalid cache attestation counts")
    if (not 1 <= value["clip_budget"] <= MAX_CLIPS
            or value["section_budget"] != MAX_SECTIONS
            or value["inspected_clips"] > value["clip_budget"]
            or value["inspected_clips"] > value["total_pending_clips"]
            or value["deduplicated_section_records_checked"] > MAX_SECTIONS
            or type(value.get("complete_pending_queue_audited")) is not bool
            or (value["complete_pending_queue_audited"] and
                (value["inspected_clips"] != value["total_pending_clips"]
                 or value["not_checked_section_records"] != 0))):
        raise ValueError("Contradictory cache audit scope")
    counts = value.get("cache_section_status_counts")
    if (not isinstance(counts, dict)
            or any(not isinstance(k, str) or type(v) is not int or v <= 0
                   for k, v in counts.items())
            or sum(counts.values()) != value["deduplicated_section_records_checked"]):
        raise ValueError("Contradictory cache attestation status totals")
    if value["status"] == "VERIFIED_ONLY_LOCAL_SOURCE_SECTION_BYTES_NOT_GOLD":
        if (not value["complete_pending_queue_audited"]
                or not counts
                or set(counts) != {
                    "VERIFIED_LOCAL_SOURCE_SECTION_BYTES_AND_RECEIPT_NOT_GOLD",
                }):
            raise ValueError("Forged cache byte verification claim")
    return value
