"""Source-ID-grounded edit geometry comparison, not subjective quality gold.

Read only the existing completed local render queue, canonical source metadata,
trusted human alignment records and holdout reservation metadata. Compare
interval geometry only for the EXACT same independently identified YouTube
source stream. Training examples remain in-sample references, never holdout gold.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from holoclip.corpus_growth import CorpusGrowthState
from holoclip.domain import ClipTimeline, SourcePlatform
from holoclip.example_quality import LearningQuality, evaluate_human_example_learning_quality
from holoclip.examples import human_clip_identity, load_human_clip_store
from holoclip.render_human_edit_context import (
    DEFAULT_GROWTH_STATE, DEFAULT_HUMAN_STORE, DEFAULT_AB_ROOT,
    _read_bounded,
)
from holoclip.render_plan import _no_links
from holoclip.render_source_queue import DEFAULT_QUEUE, _connect
from holoclip.render_source_resolver import _canonical_assets

VERSION = "holoclip-same-source-edit-geometry-v3"
DEFAULT_OUTPUT = Path("data/render-previews/same-source-edit-geometry.json")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_JOB = re.compile(r"renderqueue_[0-9a-f]{32}\Z")
_VIDEO = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_MAX_PAGE = 40
_MAX_EXAMPLES = 1000
_MAX_STORE_BYTES = 8 * 1024 * 1024
_MAX_STATE_BYTES = 4 * 1024 * 1024
_MAX_TIMELINE = 1_048_576
_MAX_RESULTS = 2 * 1024 * 1024
_MAX_MATCHES = 20
_MAX_INVENTORY = 1000


def _source_video_id(url: str | None) -> str | None:
    if not isinstance(url, str) or len(url) > 2048:
        return None
    value = urlparse(url)
    if value.scheme != "https" or value.username or value.password or value.port:
        return None
    host = (value.hostname or "").lower()
    identifier = None
    if host in {"www.youtube.com", "youtube.com", "m.youtube.com"}:
        if value.path == "/watch":
            qs = parse_qs(value.query)
            if len(qs.get("v", [])) == 1:
                identifier = qs["v"][0]
        elif value.path.startswith(("/live/", "/shorts/")):
            parts = value.path.strip("/").split("/")
            if len(parts) == 2:
                identifier = parts[1]
    elif host in {"youtu.be", "www.youtu.be"}:
        parts = value.path.strip("/").split("/")
        if len(parts) == 1:
            identifier = parts[0]
    return identifier if identifier and _VIDEO.fullmatch(identifier) else None


def _ranges_union(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for start, end in sorted(spans):
        if (not math.isfinite(start) or not math.isfinite(end)
                or not 0 <= start < end <= 24 * 3600):
            raise ValueError("LOCAL_SAME_SOURCE_INTERVAL_INVALID")
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(end, out[-1][1]))
        else:
            out.append((start, end))
    return out


def _seconds(spans: list[tuple[float, float]]) -> float:
    return sum(e - s for s, e in spans)


def _intersection(left: list[tuple[float, float]],
                  right: list[tuple[float, float]]) -> float:
    i = j = 0
    total = 0.0
    while i < len(left) and j < len(right):
        a, b = left[i]
        c, d = right[j]
        total += max(0.0, min(b, d) - max(a, c))
        if b <= d:
            i += 1
        else:
            j += 1
    return total


def _source_seams(spans: list[tuple[float, float]]) -> list[float]:
    # A gap in aligned *source* ranges can indicate an omitted source section.
    # It is NOT ground truth for the exact human cut frame or its intent.
    ordered = _ranges_union(spans)
    return [
        nxt[0] for prev, nxt in zip(ordered, ordered[1:])
        if nxt[0] - prev[1] > 0.5
    ]



def _subtract_source_ranges(
    selected: list[tuple[float, float]],
    other: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Subtract canonical, disjoint source-time intervals; no editorial judgment."""
    result: list[tuple[float, float]] = []
    j = 0
    for start, end in selected:
        cursor = start
        while j < len(other) and other[j][1] <= cursor:
            j += 1
        k = j
        while k < len(other) and other[k][0] < end:
            before, after = other[k]
            if before > cursor:
                result.append((cursor, min(before, end)))
            cursor = max(cursor, after)
            if cursor >= end:
                break
            k += 1
        if cursor < end:
            result.append((cursor, end))
    return result


def _difference_window_summary(spans: list[tuple[float, float]]) -> dict:
    """Three largest source-time disagreements, not proposals to add/remove cuts."""
    selected = sorted(spans, key=lambda pair: (-(pair[1] - pair[0]), pair[0]))
    return {
        "window_count": len(spans),
        "top_windows_by_duration": [
            {
                "source_start_seconds": round(start, 3),
                "source_end_seconds": round(end, 3),
                "duration_seconds": round(end - start, 3),
            }
            for start, end in selected[:3]
        ],
        "windows_not_present_are_not_negative_labels": True,
    }


def _source_time_backtracks(ordered: list[tuple[float, float]]) -> int:
    """Count source-clock reversals in output order; legitimate replays may do this."""
    return sum(
        next_start + 0.5 < start
        for (start, _), (next_start, _) in zip(ordered, ordered[1:])
    )


def _geometry(generated: list[tuple[float, float]],
              human: list[tuple[float, float]]) -> dict:
    gen, ref = _ranges_union(generated), _ranges_union(human)
    gsec, rsec = _seconds(gen), _seconds(ref)
    if gsec <= 0 or rsec <= 0:
        raise ValueError("LOCAL_SAME_SOURCE_EMPTY_INTERVALS")
    both = _intersection(gen, ref)
    gen_seams, ref_seams = _source_seams(gen), _source_seams(ref)
    diffs = [min(abs(g - h) for h in ref_seams) for g in gen_seams] if ref_seams else []
    generated_only = _subtract_source_ranges(gen, ref)
    human_only = _subtract_source_ranges(ref, gen)
    # Exact span multiset equality is essential: union equality alone hides
    # different spans and repeated/omitted material. No fuzzy edit gold.
    exact_order_difference = (
        generated != human if sorted(generated) == sorted(human) else None
    )
    return {
        "generated_unique_source_seconds": round(gsec, 3),
        "human_aligned_unique_source_seconds": round(rsec, 3),
        "shared_source_seconds": round(both, 3),
        "generated_selection_overlap_fraction": round(both / gsec, 6),
        "human_aligned_coverage_fraction": round(both / rsec, 6),
        "selected_source_jaccard": round(both / (gsec + rsec - both), 6),
        "generated_only_source_seconds": round(max(0.0, gsec - both), 3),
        "human_only_source_seconds": round(max(0.0, rsec - both), 3),
        "generated_observed_source_gaps": len(gen_seams),
        "human_observed_source_gaps": len(ref_seams),
        "nearest_human_gap_start_distance_seconds": (
            round(statistics.median(diffs), 3) if diffs else None
        ),
        "selection_difference_diagnostics": {
            "generated_only": _difference_window_summary(generated_only),
            "human_only": _difference_window_summary(human_only),
            "generated_source_time_backtracks": _source_time_backtracks(generated),
            "human_alignment_source_time_backtracks": _source_time_backtracks(human),
            "source_backtrack_is_not_an_editing_error": True,
            "difference_windows_are_not_edit_instructions": True,
            "exact_source_interval_order_differs": exact_order_difference,
        },
        "alignment_is_approximate_not_frame_accurate": True,
        "creative_quality_label": None,
    }




def _inspection_queue(results: list[dict], *, verified_reservations: bool) -> dict:
    """Page-local, one exemplar per render job, never a quality ranking.

    Source-time disagreement (rounded to milliseconds) determines inspection
    ordering. Distinct training clips are not independent validation votes.
    """
    candidates: list[tuple[tuple, dict]] = []
    for job in results:
        per_job: list[tuple[tuple, dict]] = []
        for match in job["matches"]:
            geom = match["geometry"]
            diag = geom["selection_difference_diagnostics"]
            difference = round(
                geom["generated_only_source_seconds"]
                + geom["human_only_source_seconds"], 3
            )
            order_diff = diag["exact_source_interval_order_differs"] is True
            backtrack_diff = abs(
                diag["generated_source_time_backtracks"]
                - diag["human_alignment_source_time_backtracks"]
            )
            reasons = []
            if difference > 0:
                reasons.append("OBSERVED_SOURCE_SELECTION_DIFFERENCE")
            if order_diff:
                reasons.append("EXACT_SPAN_PLAYBACK_ORDER_DIFFERENCE")
            if backtrack_diff:
                reasons.append("SOURCE_CLOCK_BACKTRACK_COUNT_DIFFERENCE")
            if not reasons:
                continue
            item = {
                "job_id": job["job_id"],
                "canonical_source_stream_sha256": job[
                    "canonical_source_stream_sha256"
                ],
                "human_clip_identity_sha256": match[
                    "human_clip_identity_sha256"
                ],
                "human_alignment_confidence": match["human_alignment_confidence"],
                "observed_nonshared_source_seconds": difference,
                "exact_interval_order_differs": (
                    diag["exact_source_interval_order_differs"]
                ),
                "source_clock_backtrack_count_difference": backtrack_diff,
                "inspection_reasons": reasons,
                "generated_only_top_source_windows": diag["generated_only"][
                    "top_windows_by_duration"
                ],
                "human_only_top_source_windows": diag["human_only"][
                    "top_windows_by_duration"
                ],
                "evaluation_split": "TRAINING_REFERENCE_IN_SAMPLE",
                "action": "INSPECT_ONLY",
                "automatic_edit_recommendation": None,
            }
            key = (
                -difference, -int(order_diff), -backtrack_diff,
                match["human_clip_identity_sha256"],
            )
            per_job.append((key, item))
        if per_job:
            candidates.append(min(per_job, key=lambda x: x[0]))
    candidates.sort(key=lambda x: (x[0], x[1]["job_id"]))
    limit = 10
    return {
        "scope": "CURRENT_JOB_PAGE_ONLY",
        "ranking_method": "LARGEST_OBSERVED_NONSHARED_SOURCE_SECONDS",
        "source_reservation_metadata_verified": verified_reservations,
        "matched_jobs_with_observable_differences": len(candidates),
        "listed_jobs": min(len(candidates), limit),
        "omitted_jobs_due_to_limit": max(0, len(candidates) - limit),
        "max_listed_jobs": limit,
        "one_reference_example_per_job": True,
        "human_training_references_are_not_independent_votes": True,
        "discrepancy_magnitude_is_not_editorial_quality": True,
        "results": [item for _, item in candidates[:limit]],
        "auto_adoption_allowed": False,
        "ready": False,
    }


def _human_by_source(store: Path, reserved: set[str]) -> tuple[dict, dict]:
    available = store.exists()
    if available:
        raw = _read_bounded(store, _MAX_STORE_BYTES)
        examples = load_human_clip_store(store)
    else:
        raw = b""
        examples = []
    if len(examples) > _MAX_EXAMPLES:
        raise ValueError("LOCAL_SAME_SOURCE_CORPUS_TOO_LARGE")
    grouped: dict[str, list] = {}
    seen: set[str] = set()
    counts = {
        "available": available,
        "corpus_sha256": hashlib.sha256(raw).hexdigest() if available else None,
        "total_examples": len(examples),
        "trusted_same_source_eligible": 0,
        "excluded_alignment_untrusted": 0,
        "excluded_unverified_source_identity": 0,
        "excluded_reserved": 0,
        "holdout_training_conflicts": 0,
    }
    all_streams = {x.stream_id for x in examples if x.stream_id}
    counts["holdout_training_conflicts"] = len(all_streams & reserved)
    for e in examples:
        if evaluate_human_example_learning_quality(e).decision is not LearningQuality.TRUSTED:
            counts["excluded_alignment_untrusted"] += 1
            continue
        identity = human_clip_identity(e.clip_url)
        if identity in seen:
            raise ValueError("LOCAL_SAME_SOURCE_DUPLICATE_TRUSTED_CLIP")
        seen.add(identity)
        stream = e.stream_id
        if (not stream or not _VIDEO.fullmatch(stream)
                or _source_video_id(e.source_url) != stream
                or not 1 <= len(e.alignment_segments) <= 200):
            counts["excluded_unverified_source_identity"] += 1
            continue
        if stream in reserved:
            counts["excluded_reserved"] += 1
            continue
        grouped.setdefault(stream, []).append(e)
        counts["trusted_same_source_eligible"] += 1
    return grouped, counts


def _reserve_state(path: Path) -> tuple[set[str], str | None, bool]:
    if not path.exists():
        return set(), None, False
    data = _read_bounded(path, _MAX_STATE_BYTES)
    state = CorpusGrowthState.model_validate_json(data)
    reserved = set(state.holdout_stream_ids)
    reserved.update(
        x.source_stream_id for x in state.items.values()
        if x.source_stream_id and x.status in {"holdout_reserved", "split_conflict"}
    )
    return reserved, hashlib.sha256(data).hexdigest(), True


def compare_same_source_edit_geometry(
    *, queue: Path = DEFAULT_QUEUE,
    example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_AB_ROOT,
    after_job_id: str | None = None,
    max_jobs: int = _MAX_PAGE,
) -> dict:
    """Read-only, source-ID-matched, bounded and explicitly in-sample diagnostic."""
    if (type(max_jobs) is not int or not 1 <= max_jobs <= _MAX_PAGE
            or after_job_id is not None and not _JOB.fullmatch(after_job_id)):
        raise ValueError("LOCAL_SAME_SOURCE_CURSOR_OR_LIMIT_INVALID")
    preview = _no_links(preview_root)
    example_store = _no_links(example_store)
    growth_state = _no_links(growth_state)
    reserved, growth_sha, holdout_known = _reserve_state(growth_state)
    grouped, corpus = _human_by_source(example_store, reserved)
    report = {
        "version": VERSION,
        "status": "LOCAL_SAME_SOURCE_NO_COMPLETED_RENDER_JOBS",
        "after_job_id": after_job_id,
        "next_cursor": None, "has_more": False,
        "window_job_count": 0, "inventory_job_count": 0,
        "same_source_compared_jobs": 0, "same_source_pairs": 0,
        "unmatched_source_jobs": 0, "multi_source_jobs_excluded": 0,
        "ineligible_or_stale_jobs": 0,
        "holdout_source_blocked_jobs": 0,
        "corpus": corpus,
        "growth_state_sha256": growth_sha,
        "holdout_reservation_metadata_available": holdout_known,
        "results": [],
        "same_source_identity_method": "canonical_repository_youtube_id",
        "baseline_mp4_independently_rehashed": False,
        "human_alignment_is_ground_truth_for_exact_cuts": False,
        "human_examples_from_training_not_independent_holdout": True,
        "measured_edit_geometry_not_editorial_quality": True,
        "selection_difference_is_not_a_human_quality_label": True,
        "inspection_queue": _inspection_queue(
            [], verified_reservations=holdout_known
        ),
        "heldout_qualification_allowed": False,
        "precision": None, "recall": None,
        "ready": False, "production_ready": False,
        "auto_adoption_allowed": False, "publication_performed": False,
    }
    conn = _connect(queue, create=False)
    if conn is None:
        return report
    try:
        conn.execute("PRAGMA query_only=ON")
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='render_jobs'"
        ).fetchone()
        if not exists:
            return report
        inventory = conn.execute(
            "SELECT q.job_id,q.timeline_json,q.database_path,"
            "r.output_path,r.output_sha256,r.render_plan_id "
            "FROM queue_jobs q INNER JOIN render_jobs r ON q.job_id=r.job_id "
            "WHERE q.state='COMPLETE' AND r.state='QA_PASSED' "
            "ORDER BY q.job_id LIMIT 1001"
        ).fetchall()
        if len(inventory) > _MAX_INVENTORY:
            raise ValueError("LOCAL_SAME_SOURCE_QUEUE_TOO_LARGE")
        report["inventory_job_count"] = len(inventory)
        remaining = [
            row for row in inventory
            if after_job_id is None or row["job_id"] > after_job_id
        ]
        selected = remaining[:max_jobs]
        report["window_job_count"] = len(selected)
        report["has_more"] = len(remaining) > max_jobs
        report["next_cursor"] = selected[-1]["job_id"] if selected else None
        if selected:
            report["status"] = "LOCAL_SAME_SOURCE_UNQUALIFIED_GEOMETRY"
        for row in selected:
            job = row["job_id"]
            if not _JOB.fullmatch(job):
                raise ValueError("LOCAL_SAME_SOURCE_JOB_ID_INVALID")
            try:
                timeline_json = row["timeline_json"]
                if (not isinstance(timeline_json, str)
                        or len(timeline_json) > _MAX_TIMELINE):
                    raise ValueError("LOCAL_SAME_SOURCE_TIMELINE_INVALID")
                timeline = ClipTimeline.model_validate_json(timeline_json)
                media = _no_links(Path(row["output_path"]))
                if (media.parent != preview
                        or media.name != f"{job}.mp4"
                        or not media.is_file()
                        or not isinstance(row["output_sha256"], str)
                        or not _SHA.fullmatch(row["output_sha256"])
                        or not isinstance(row["render_plan_id"], str)
                        or not row["render_plan_id"]):
                    raise ValueError("LOCAL_SAME_SOURCE_QA_MEDIA_UNTRUSTED")
                source_assets = {x.source_asset_id for x in timeline.segments}
                if len(source_assets) != 1:
                    report["multi_source_jobs_excluded"] += 1
                    continue
                asset_id = next(iter(source_assets))
                asset = _canonical_assets(Path(row["database_path"]), {asset_id})[asset_id]
                if (asset.platform is not SourcePlatform.YOUTUBE
                        or not _VIDEO.fullmatch(asset.external_id)):
                    raise ValueError("LOCAL_SAME_SOURCE_SOURCE_ID_UNTRUSTED")
                stream = asset.external_id
                if stream in reserved:
                    report["holdout_source_blocked_jobs"] += 1
                    continue
                examples = grouped.get(stream, [])[:_MAX_MATCHES + 1]
                if not examples:
                    report["unmatched_source_jobs"] += 1
                    continue
                if len(examples) > _MAX_MATCHES:
                    raise ValueError("LOCAL_SAME_SOURCE_TOO_MANY_HUMAN_MATCHES")
                generated = [(s.source_start, s.source_end)
                             for s in sorted(timeline.segments,
                                             key=lambda x: x.output_order)]
                pairs = []
                for human in examples:
                    aligned = sorted(human.alignment_segments,
                                     key=lambda s: (s.clip_start, s.clip_end))
                    original_intervals = [(s.source_start, s.source_end) for s in aligned]
                    score = _geometry(generated, original_intervals)
                    pairs.append({
                        "human_example_id": human.id,
                        "human_clip_identity_sha256": hashlib.sha256(
                            human_clip_identity(human.clip_url).encode("utf-8")
                        ).hexdigest(),
                        "human_alignment_confidence": human.alignment_confidence,
                        "geometry": score,
                        "evaluation_split": "TRAINING_REFERENCE_IN_SAMPLE",
                    })
                report["results"].append({
                    "job_id": job,
                    "canonical_source_stream_sha256": hashlib.sha256(
                        stream.encode("utf-8")
                    ).hexdigest(),
                    "timeline_sha256": hashlib.sha256(
                        timeline_json.encode("utf-8")
                    ).hexdigest(),
                    "base_sha256_from_earlier_technical_qa": row["output_sha256"],
                    "human_match_count": len(pairs),
                    "matches": pairs,
                    "rendered_media_redecoded": False,
                    "qualified_editorial_benchmark": False,
                })
                report["same_source_compared_jobs"] += 1
                report["same_source_pairs"] += len(pairs)
            except (ValueError, OSError, UnicodeError, TypeError, KeyError):
                report["ineligible_or_stale_jobs"] += 1
                continue
    except sqlite3.Error as exc:
        raise ValueError("LOCAL_SAME_SOURCE_QUEUE_READ_FAILED") from exc
    finally:
        conn.close()
    # Any training/holdout collision remains an explicit global blocker.
    if corpus["holdout_training_conflicts"]:
        report["status"] = "LOCAL_SAME_SOURCE_TRAIN_HOLDOUT_CONFLICT"
        report["results"] = []
        report["same_source_compared_jobs"] = 0
        report["same_source_pairs"] = 0
    report["inspection_queue"] = _inspection_queue(
        report["results"], verified_reservations=holdout_known
    )
    return report


def export_same_source_edit_geometry(
    *, output: Path = DEFAULT_OUTPUT, execute: bool = False,
    queue: Path = DEFAULT_QUEUE,
    example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_AB_ROOT,
    after_job_id: str | None = None,
    max_jobs: int = _MAX_PAGE,
) -> dict:
    report = compare_same_source_edit_geometry(
        queue=queue, example_store=example_store, growth_state=growth_state,
        preview_root=preview_root, after_job_id=after_job_id, max_jobs=max_jobs,
    )
    if not execute:
        return report
    target = _no_links(output)
    if target.suffix.lower() != ".json" or target.exists():
        raise ValueError("LOCAL_SAME_SOURCE_OUTPUT_UNSAFE")
    encoded = (json.dumps(report, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > _MAX_RESULTS:
        raise ValueError("LOCAL_SAME_SOURCE_REPORT_TOO_LARGE")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".holoclip-same-source-", dir=target.parent) as td:
        staged = Path(td) / "report.json"
        staged.write_bytes(encoded)
        try:
            os.link(staged, target)
        except OSError as exc:
            raise ValueError("LOCAL_SAME_SOURCE_OUTPUT_EXISTS") from exc
    return report
