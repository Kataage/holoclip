"""Offline human-edit *context*, not manufactured human preference or quality gold.

Human clip alignment can describe segment length and selected-source gaps;
it does NOT label whether an unrelated A/B video/audio/subtitle is better.
Human source streams are grouped, reserved holdouts are excluded, and source
leakage conflicts block publication of a trusted reference.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import tempfile
from collections import defaultdict
from pathlib import Path

from holoclip.corpus_growth import CorpusGrowthState
from holoclip.example_quality import LearningQuality, evaluate_human_example_learning_quality
from holoclip.examples import human_clip_identity, load_human_clip_store
from holoclip.render_ab_metrics import VERSION as AB_METRICS_VERSION
from holoclip.render_plan import _no_links

VERSION = "holoclip-human-edit-structural-context-v1"
DEFAULT_HUMAN_STORE = Path("data/examples/human_clips.jsonl")
DEFAULT_GROWTH_STATE = Path("data/examples/corpus_growth.json")
DEFAULT_AB_ROOT = Path("data/render-previews")
DEFAULT_OUTPUT = Path("data/render-previews/human-edit-structural-context.json")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_JOB = re.compile(r"renderqueue_[0-9a-f]{32}\Z")
_KINDS = {"audio", "subtitle", "cut"}
_MAX_EXAMPLES = 1000
_MAX_CORPUS_BYTES = 8 * 1024 * 1024
_MAX_STATE_BYTES = 4 * 1024 * 1024
_MAX_METRIC_JSON = 65536
_MAX_DISCOVERY = 10000
_MAX_PAGE = 40
_MAX_RESULT = 65536


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bounded(path: Path, limit: int) -> bytes:
    path = _no_links(path)
    if not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise ValueError("LOCAL_HUMAN_CONTEXT_INPUT_UNTRUSTED")
    raw = path.read_bytes()
    if not 0 < len(raw) <= limit:
        raise ValueError("LOCAL_HUMAN_CONTEXT_INPUT_UNTRUSTED")
    return raw


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def _human_reference(store: Path, state_file: Path) -> dict:
    store, state_file = _no_links(store), _no_links(state_file)
    result = {
        "store_available": False, "growth_state_available": False,
        "corpus_sha256": None, "growth_state_sha256": None,
        "all_examples": 0, "trusted_alignments": 0,
        "excluded_untrusted": 0, "excluded_no_stream": 0,
        "excluded_holdout_stream": 0, "split_conflict_count": 0,
        "trusted_clip_count": 0, "trusted_source_streams": 0,
        "aligned_spans_observed": 0, "positive_source_gaps_observed": 0,
        "median_aligned_span_seconds": None,
        "median_positive_source_gap_seconds": None,
        "median_stream_level_clip_length_seconds": None,
        "provenance_quality": "NO_TRUSTED_STREAM_REPRESENTATION",
        "human_labeled_a_b_preferences": 0,
        "human_labeled_media_defects": 0,
    }
    reserved = set()
    if state_file.exists():
        raw_state = _read_bounded(state_file, _MAX_STATE_BYTES)
        state = CorpusGrowthState.model_validate_json(raw_state)
        reserved = set(state.holdout_stream_ids)
        reserved.update(
            e.source_stream_id for e in state.items.values()
            if e.status in {"holdout_reserved", "split_conflict"}
            and e.source_stream_id
        )
        result["growth_state_available"] = True
        result["growth_state_sha256"] = _hash_bytes(raw_state)
    if not store.exists():
        return result
    raw = _read_bounded(store, _MAX_CORPUS_BYTES)
    result["corpus_sha256"] = _hash_bytes(raw)
    result["store_available"] = True
    examples = load_human_clip_store(store)
    if len(examples) > _MAX_EXAMPLES:
        raise ValueError("LOCAL_HUMAN_CONTEXT_CORPUS_TOO_LARGE")
    result["all_examples"] = len(examples)
    # Each source stream has only one vote in the top-level duration median.
    groups: dict[str, list[float]] = defaultdict(list)
    spans, gaps = [], []
    identities = set()
    learning_streams = {e.stream_id for e in examples if e.stream_id}
    # Metadata reservations cannot be silently reused as training context,
    # even if a reserved clip is currently low-quality or unaligned.
    conflicts = reserved & learning_streams
    result["split_conflict_count"] = len(conflicts)
    for example in examples:
        quality = evaluate_human_example_learning_quality(example)
        if quality.decision is not LearningQuality.TRUSTED:
            result["excluded_untrusted"] += 1
            continue
        result["trusted_alignments"] += 1
        identity = human_clip_identity(example.clip_url)
        if identity in identities:
            raise ValueError("LOCAL_HUMAN_CONTEXT_DUPLICATE_TRUSTED_CLIP")
        identities.add(identity)
        if not example.stream_id:
            result["excluded_no_stream"] += 1
            continue
        if example.stream_id in reserved:
            result["excluded_holdout_stream"] += 1
            continue
        metrics = quality.metrics
        if not 0 < metrics.clip_timeline_seconds <= 1800:
            # Alignment trust is not a trustworthy clip-length sensor here.
            result["excluded_untrusted"] += 1
            continue
        aligned = sorted(example.alignment_segments, key=lambda s: s.clip_start)
        if len(aligned) > 200:
            raise ValueError("LOCAL_HUMAN_CONTEXT_SEGMENTS_TOO_LARGE")
        for seg in aligned:
            span = seg.clip_end - seg.clip_start
            if not math.isfinite(span) or not 0 < span <= 1800:
                raise ValueError("LOCAL_HUMAN_CONTEXT_SPAN_UNTRUSTED")
            spans.append(span)
        for left, right in zip(aligned, aligned[1:]):
            # A positive original-source gap is observed omitted material.
            # Do not treat same-stream aligned anchor boundaries as actual
            # frame-accurate editorial cuts or negative quality examples.
            gap = right.source_start - left.source_end
            if not math.isfinite(gap):
                raise ValueError("LOCAL_HUMAN_CONTEXT_GAP_UNTRUSTED")
            if gap > 0:
                gaps.append(gap)
        groups[example.stream_id].append(metrics.clip_timeline_seconds)
        result["trusted_clip_count"] += 1
    result["trusted_source_streams"] = len(groups)
    result["aligned_spans_observed"] = len(spans)
    result["positive_source_gaps_observed"] = len(gaps)
    # Never publish a held-out/learning overlap as a reference.
    if conflicts:
        result["provenance_quality"] = "BLOCKED_HOLDOUT_TRAINING_CONFLICT"
        return {**result,
                "trusted_clip_count": 0, "trusted_source_streams": 0,
                "aligned_spans_observed": 0, "positive_source_gaps_observed": 0}
    result["median_aligned_span_seconds"] = _median(spans)
    result["median_positive_source_gap_seconds"] = _median(gaps)
    result["median_stream_level_clip_length_seconds"] = _median([
        statistics.median(groups[k]) for k in sorted(groups)
    ])
    if len(groups) < 2 or sum(map(len, groups.values())) < 3:
        result["provenance_quality"] = "INSUFFICIENT_STREAM_DIVERSITY"
    elif not state_file.exists():
        result["provenance_quality"] = "HOLDOUT_RESERVATIONS_NOT_VERIFIABLE"
    else:
        result["provenance_quality"] = "TRUSTED_ALIGNMENT_METADATA_ONLY"
    return result


def _ab_page(root: Path, *, after_job_id: str | None, max_jobs: int) -> dict:
    if after_job_id is not None and not _JOB.fullmatch(after_job_id):
        raise ValueError("LOCAL_HUMAN_CONTEXT_CURSOR_UNTRUSTED")
    root = _no_links(root)
    inventory = []
    if root.exists():
        if not root.is_dir():
            raise ValueError("LOCAL_HUMAN_CONTEXT_PREVIEW_ROOT_UNTRUSTED")
        inventory = sorted(root.glob("renderqueue_*.ab-metrics.json"))
    if len(inventory) > _MAX_DISCOVERY:
        raise ValueError("LOCAL_HUMAN_CONTEXT_INVENTORY_TOO_LARGE")
    remaining = [path for path in inventory
                 if after_job_id is None or path.name[:-16] > after_job_id]
    selected = remaining[:max_jobs]
    reported = {
        "inventory_count": len(inventory),
        "window_count": len(selected),
        "has_more": len(remaining) > max_jobs,
        "next_cursor": selected[-1].name[:-16] if selected else None,
        "verified_metric_packs": 0,
        "stale_or_invalid_metric_packs": 0,
        "technical_pair_count": 0,
        "pair_kind_counts": {"audio": 0, "cut": 0, "subtitle": 0},
        "video_ssim_medians": {"audio": None, "cut": None, "subtitle": None},
        "audio_mean_delta_medians_db": {
            "audio": None, "cut": None, "subtitle": None,
        },
        "all_signal_metrics_remeasured_this_pass": False,
    }
    similarity: dict[str, list[float]] = defaultdict(list)
    audio: dict[str, list[float]] = defaultdict(list)
    for path in selected:
        if not _JOB.fullmatch(path.name[:-16]):
            raise ValueError("LOCAL_HUMAN_CONTEXT_JOB_PATH_UNTRUSTED")
        path = _no_links(path)
        try:
            raw = _read_bounded(path, _MAX_METRIC_JSON)
            report = json.loads(raw)
            if (not isinstance(report, dict)
                    or report.get("version") != AB_METRICS_VERSION
                    or report.get("ready") is not False
                    or report.get("production_ready") is not False
                    or report.get("human_quality_qualified") is not False
                    or report.get("auto_adoption_allowed") is not False
                    or report.get("objective_technical_metrics_only") is not True
                    or report.get("creative_quality_ground_truth_available") is not False
                    or type(report.get("measured_pair_count")) is not int
                    or not 1 <= report["measured_pair_count"] <= 3
                    or not isinstance(report.get("comparisons"), list)
                    or len(report["comparisons"]) != report["measured_pair_count"]
                    or not isinstance(report.get("triage_sha256"), str)
                    or not _SHA.fullmatch(report["triage_sha256"])):
                raise ValueError("LOCAL_HUMAN_CONTEXT_METRICS_UNTRUSTED")
            triage = _no_links(path.with_name(f"{path.name[:-16]}.triage.json"))
            if _hash_bytes(_read_bounded(triage, _MAX_METRIC_JSON)) != report["triage_sha256"]:
                raise ValueError("LOCAL_HUMAN_CONTEXT_TRIAGE_CHANGED")
            vals = []
            kinds = set()
            for comp in report["comparisons"]:
                if not isinstance(comp, dict) or comp.get("kind") not in _KINDS:
                    raise ValueError("LOCAL_HUMAN_CONTEXT_PAIR_UNTRUSTED")
                kind = comp["kind"]
                if kind in kinds:
                    raise ValueError("LOCAL_HUMAN_CONTEXT_DUPLICATE_KIND")
                kinds.add(kind)
                ss = comp.get("decoded_video_ssim")
                delta = comp.get("after_minus_before_mean_db")
                if (type(ss) not in (int, float) or not math.isfinite(ss)
                        or not 0 <= ss <= 1
                        or type(delta) not in (int, float)
                        or not math.isfinite(delta) or not -100 <= delta <= 100):
                    raise ValueError("LOCAL_HUMAN_CONTEXT_SIGNAL_INVALID")
                receipt_sha = comp.get("ab_receipt_sha256")
                if not isinstance(receipt_sha, str) or not _SHA.fullmatch(receipt_sha):
                    raise ValueError("LOCAL_HUMAN_CONTEXT_PAIR_SHA_INVALID")
                suffix = {"audio": ".ab-preview", "subtitle": ".subtitle-ab",
                          "cut": ".cut-ab"}[kind]
                receipt = path.with_name(f"{path.name[:-16]}{suffix}") / "receipt.json"
                if _hash_bytes(_read_bounded(receipt, _MAX_METRIC_JSON)) != receipt_sha:
                    raise ValueError("LOCAL_HUMAN_CONTEXT_PAIR_RECEIPT_CHANGED")
                vals.append((kind, float(ss), float(delta)))
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            reported["stale_or_invalid_metric_packs"] += 1
            continue
        reported["verified_metric_packs"] += 1
        for kind, ss, delta in vals:
            reported["pair_kind_counts"][kind] += 1
            reported["technical_pair_count"] += 1
            similarity[kind].append(ss)
            audio[kind].append(delta)
    for kind in sorted(_KINDS):
        reported["video_ssim_medians"][kind] = _median(similarity[kind])
        reported["audio_mean_delta_medians_db"][kind] = _median(audio[kind])
    return reported


def compile_human_edit_context(
    *, example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_AB_ROOT,
    after_job_id: str | None = None,
    max_jobs: int = _MAX_PAGE,
) -> dict:
    if type(max_jobs) is not int or not 1 <= max_jobs <= _MAX_PAGE:
        raise ValueError("LOCAL_HUMAN_CONTEXT_LIMIT_UNTRUSTED")
    human = _human_reference(example_store, growth_state)
    ab = _ab_page(preview_root, after_job_id=after_job_id, max_jobs=max_jobs)
    if human["split_conflict_count"]:
        status = "LOCAL_HUMAN_EDIT_CONTEXT_SPLIT_CONFLICT"
    elif not human["trusted_clip_count"]:
        status = "LOCAL_HUMAN_EDIT_CONTEXT_NO_TRUSTED_REFERENCES"
    else:
        status = "LOCAL_HUMAN_EDIT_CONTEXT_STRUCTURAL_ONLY"
    return {
        "version": VERSION,
        "status": status,
        "human_alignment_reference": human,
        "generated_ab_technical_signals": ab,
        "after_job_id": after_job_id,
        "metric_domains_directly_comparable": False,
        "actual_cut_positions_from_human_media_measured": False,
        "learned_human_cut_preferences_available": False,
        "source_overlap_with_generated_jobs_verified": False,
        "human_editorial_quality_labels_available": False,
        "ab_preference_accuracy": None,
        "precision": None, "recall": None,
        "production_qualification_allowed": False,
        "auto_adoption_allowed": False,
        "publication_performed": False,
        "ready": False, "production_ready": False,
    }


def export_human_edit_context(
    *, output: Path = DEFAULT_OUTPUT, execute: bool = False,
    example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_AB_ROOT,
    after_job_id: str | None = None,
    max_jobs: int = _MAX_PAGE,
) -> dict:
    data = compile_human_edit_context(
        example_store=example_store, growth_state=growth_state,
        preview_root=preview_root, after_job_id=after_job_id,
        max_jobs=max_jobs,
    )
    if not execute:
        return data
    target = _no_links(output)
    if target.suffix.lower() != ".json" or target.exists():
        raise ValueError("LOCAL_HUMAN_CONTEXT_OUTPUT_UNSAFE")
    raw = (json.dumps(data, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > _MAX_RESULT:
        raise ValueError("LOCAL_HUMAN_CONTEXT_REPORT_TOO_LARGE")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".holoclip-human-context-", dir=target.parent,
    ) as td:
        stage = Path(td) / "reference.json"
        stage.write_bytes(raw)
        try:
            os.link(stage, target)
        except OSError as exc:
            raise ValueError("LOCAL_HUMAN_CONTEXT_OUTPUT_EXISTS") from exc
    return data
