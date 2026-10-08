"""Strictly offline diagnostics for reference-free cut observations in saved trials.

The persisted JSON is unreviewed, and this audit checks *internal consistency*
only. It does not inspect original media, prove independent cut labels, or
authorize provider routing / learned editing parameters.
"""
from __future__ import annotations

import math
from collections import Counter

from holoclip.real_cut_observations import (
    VERSION, aggregate_real_cut_observations,
)

_COMPLETE = {"UNREVIEWED_MEASUREMENTS_COMPLETE", "SIMULATED_STAGE_TEST_COMPLETE"}
_NO_GOLD_FALSE = (
    "ready", "production_ready", "qualification_allowed",
    "reviewed_gold_available", "benchmark_held_out", "event_split_verified",
    "frame_accurate_cuts_verified", "dense_output_source_map_verified",
)
_VALID_STATUSES = {
    "UNREVIEWED_REFERENCE_FREE_OBSERVATIONS",
    "UNKNOWN_ACOUSTIC_SUPPORT", "UNVERIFIED_CUT_MEASUREMENT",
}
_VALID_SIDE = {
    "NOT_MEASURED_OR_UNVERIFIED", "UNVERIFIED_CROSS_STAGE_PROVENANCE",
    "INVALID_SIDE_SCHEMA", "UNKNOWN_TRANSITION_BUDGET",
    "UNVERIFIED_SIDE_INTERVAL_LINKS", "MEASURED_UNREVIEWED",
}
_UNKNOWN_AUDIO = {"UNKNOWN_WIDE_GAP", "UNKNOWN_CONTRADICTORY_AUDIO"}
_NONNEGATIVE = ("cut_candidate_count", "unknown_audio_transition_count",
                "overbroad_candidate_count")


def _count(value: object, *, ceiling: int = 256) -> bool:
    return type(value) is int and 0 <= value <= ceiling


def _width(value: object) -> bool:
    return (type(value) in (int, float) and math.isfinite(value)
            and 0 <= value <= 3600)


def _checked_row(clip_id: str, value: object) -> dict:
    if not isinstance(value, dict) or value.get("version") != VERSION:
        raise ValueError("Malformed persisted cut observations")
    if (value.get("reference_kind") != "NO_INDEPENDENT_EDITOR_REFERENCE"
            or any(value.get(flag) is not False for flag in _NO_GOLD_FALSE)
            or value.get("editor_cut_precision") is not None
            or value.get("editor_cut_recall") is not None
            or value.get("status") not in _VALID_STATUSES
            or value.get("two_sided_status") not in _VALID_SIDE):
        raise ValueError("Promoted or unsupported real cut observation")
    if value.get("clip_id", clip_id) != clip_id:
        raise ValueError("Misattributed cut observation identity")
    survey_status = value.get("output_only_visual_status", "NOT_MEASURED_OR_UNVERIFIED")
    survey_count = value.get("output_only_visual_change_count")
    survey_unmatched = value.get("output_only_visual_without_acoustic_count")
    survey_coverage = value.get("output_only_visual_coverage_seconds")
    if survey_status not in {
        "NOT_MEASURED_OR_UNVERIFIED",
        "MEASURED_UNREVIEWED_OUTPUT_ONLY_VISUAL_SIGNALS",
        "UNKNOWN_BOUNDED_VISUAL_DECODE",
        "UNKNOWN_INSUFFICIENT_VISUAL_SAMPLING",
        "UNKNOWN_VISUAL_PEAK_BUDGET",
    }:
        raise ValueError("Unsupported output-only visual observation status")
    if survey_status == "MEASURED_UNREVIEWED_OUTPUT_ONLY_VISUAL_SIGNALS":
        if (not _count(survey_count, ceiling=18)
                or not _count(survey_unmatched, ceiling=18)
                or survey_unmatched > survey_count
                or type(survey_coverage) not in (int, float)
                or not math.isfinite(survey_coverage)
                or not 0 <= survey_coverage <= 86400):
            raise ValueError("Impossible output-only visual survey totals")
    elif (survey_count is not None or survey_unmatched is not None
          or (survey_status == "NOT_MEASURED_OR_UNVERIFIED"
              and survey_coverage is not None)):
        raise ValueError("Unknown visual survey was promoted to measured")
    elif survey_coverage is not None and (
        type(survey_coverage) not in (int, float)
        or not math.isfinite(survey_coverage)
        or not 0 <= survey_coverage <= 86400
    ):
        raise ValueError("Malformed visual survey coverage")
    source_status = value.get("source_visual_check_status",
                              "NOT_MEASURED_OR_UNVERIFIED")
    source_cols = (
        value.get("source_visual_change_also_observed"),
        value.get("source_visual_no_isolated_change_observed"),
        value.get("source_visual_unknown_correspondence"),
    )
    if source_status == "MEASURED_UNREVIEWED_SOURCE_VISUAL_CHECKS":
        if (any(not _count(n, ceiling=18) for n in source_cols)
                or sum(source_cols) != (
                    value.get("output_only_visual_without_acoustic_count")
                )):
            raise ValueError("Contradictory output/source visual crosscheck totals")
    elif (source_status not in {
        "NOT_MEASURED_OR_UNVERIFIED",
        "UNKNOWN_NO_VERIFIED_VISUAL_SURVEY",
        "UNKNOWN_SOURCE_VISUAL_PROBE_BUDGET",
    } or any(n is not None for n in source_cols)):
        raise ValueError("Unverified source-side visual evidence was counted")
    search_status = value.get("visual_only_search_status",
                              "NOT_MEASURED_OR_UNVERIFIED")
    search_keys = (
        "visual_only_source_skip_hypotheses",
        "visual_only_source_continuity_hypotheses",
        "visual_only_source_group_switch_hypotheses",
        "visual_only_unknown_correspondences",
    )
    search_counts = [value.get(k) for k in search_keys]
    if search_status == "MEASURED_UNREVIEWED_VISUAL_SEQUENCE_SEARCH":
        if (any(not _count(n, ceiling=3) for n in search_counts)
                or sum(search_counts) != (
                    value.get("output_only_visual_without_acoustic_count")
                )):
            raise ValueError("Visual-only search counts contradict output survey")
    elif (search_status not in {
        "NOT_MEASURED_OR_UNVERIFIED",
        "UNKNOWN_NO_MEASURED_OUTPUT_SURVEY",
        "UNKNOWN_VISUAL_SEARCH_PEAK_BUDGET",
        "NO_UNMATCHED_OUTPUT_VISUAL_PEAKS",
        "UNKNOWN_NO_PINNED_SOURCE_SECTIONS",
        "UNKNOWN_VISUAL_FRAME_COVERAGE",
        "UNKNOWN_VISUAL_SEARCH_COMPARISON_BUDGET",
        "UNKNOWN_VISUAL_IMAGE_DECODE",
    } or any(v is not None for v in search_counts)):
        raise ValueError("Unverified audio-independent search was counted")
    if value["status"] != "UNREVIEWED_REFERENCE_FREE_OBSERVATIONS":
        if (any(value.get(field) is not None for field in (
                *_NONNEGATIVE, "candidate_median_width_seconds",
                "candidate_p90_width_seconds",
                "output_visual_corroborating_candidates",
                "two_sided_multimodal_candidates",
                "two_sided_unresolved_candidates",
                "unknown_audio_transition_statuses",
            )) or value["two_sided_status"] != "NOT_MEASURED_OR_UNVERIFIED"):
            raise ValueError("Unsupported audio was converted to false measurements")
        return value
    if (not all(_count(value.get(key)) for key in _NONNEGATIVE)
            or value["overbroad_candidate_count"] > value["cut_candidate_count"]):
        raise ValueError("Unbounded or contradictory cut candidate counts")
    by_reason = value.get("unknown_audio_transition_statuses")
    if (not isinstance(by_reason, dict)
            or not set(by_reason) <= _UNKNOWN_AUDIO
            or any(not _count(v) for v in by_reason.values())
            or sum(by_reason.values()) != value["unknown_audio_transition_count"]):
        raise ValueError("Unverifiable unknown acoustic transition counts")
    n = value["cut_candidate_count"]
    widths = (value.get("candidate_median_width_seconds"),
              value.get("candidate_p90_width_seconds"))
    if ((n == 0 and widths != (None, None))
            or (n > 0 and not all(_width(w) for w in widths))):
        raise ValueError("Invalid measured candidate width distribution")
    visual = value.get("output_visual_corroborating_candidates")
    if visual is not None and (not _count(visual) or visual > n):
        raise ValueError("Impossible output visual candidate count")
    if value["two_sided_status"] == "MEASURED_UNREVIEWED":
        positive = value.get("two_sided_multimodal_candidates")
        unknown = value.get("two_sided_unresolved_candidates")
        if (not _count(positive) or not _count(unknown)
                or positive + unknown != n):
            raise ValueError("Two-sided candidate counts do not match audio")
    elif (value.get("two_sided_multimodal_candidates") is not None
          or value.get("two_sided_unresolved_candidates") is not None):
        raise ValueError("Unverified source side was counted")
    return value


def _checked_rows(value: object, expected: set[str]) -> dict[str, dict]:
    if (not isinstance(value, dict) or len(value) > 240
            or any(not isinstance(k, str) or k not in expected
                   for k in value)):
        raise ValueError("Unreserved or excessive cut observation clip IDs")
    return {key: _checked_row(key, row) for key, row in sorted(value.items())}


def _triage_codes(row: dict) -> list[str]:
    """Independent diagnostic signals, NOT mutually exclusive quality grades."""
    visual_only = (
        ["OUTPUT_ONLY_VISUAL_SIGNAL_WITHOUT_ACOUSTIC_CANDIDATE"]
        if (row.get("output_only_visual_without_acoustic_count") or 0) > 0
        else []
    )
    if (row.get("visual_only_source_skip_hypotheses") or 0) > 0:
        visual_only.append("VISUAL_ONLY_SOURCE_SKIP_HYPOTHESIS_NOT_GOLD")
    if (row.get("visual_only_source_group_switch_hypotheses") or 0) > 0:
        visual_only.append("VISUAL_ONLY_SOURCE_GROUP_SWITCH_HYPOTHESIS_NOT_GOLD")
    if row["status"] == "UNKNOWN_ACOUSTIC_SUPPORT":
        return ["ACOUSTIC_SUPPORT_UNKNOWN", *visual_only]
    if row["status"] != "UNREVIEWED_REFERENCE_FREE_OBSERVATIONS":
        return ["CUT_MEASUREMENT_UNVERIFIED", *visual_only]
    codes = visual_only
    if (row.get("source_visual_change_also_observed") or 0) > 0:
        codes.append("SOURCE_VISUAL_CHANGE_ALSO_OBSERVED_NOT_GOLD")
    if (row.get("source_visual_no_isolated_change_observed") or 0) > 0:
        codes.append("SOURCE_CHANGE_NOT_OBSERVED_NOT_PROOF_OF_EDIT")
    if row["cut_candidate_count"] == 0:
        codes.append("NO_ACOUSTIC_CANDIDATES_OBSERVED")
    if row["overbroad_candidate_count"]:
        codes.append("OVERBROAD_ACOUSTIC_INTERVALS")
    if row["unknown_audio_transition_count"]:
        codes.append("UNRESOLVED_ACOUSTIC_TRANSITIONS")
    if row["output_visual_corroborating_candidates"] is None:
        codes.append("OUTPUT_VISUAL_EVIDENCE_UNKNOWN")
    elif row["output_visual_corroborating_candidates"] < row["cut_candidate_count"]:
        codes.append("PARTIAL_OUTPUT_VISUAL_CORROBORATION")
    if row["two_sided_status"] != "MEASURED_UNREVIEWED":
        codes.append("SOURCE_SIDE_EVIDENCE_UNKNOWN")
    elif (row["cut_candidate_count"] > 0
          and row["two_sided_multimodal_candidates"] == 0):
        codes.append("NO_SOURCE_SIDE_MULTIMODAL_CANDIDATES")
    return codes or ["REFERENCE_FREE_ONLY_NO_GOLD"]


def diagnose_saved_real_cut_observations(audit: dict, rows: list[dict],
                                         *, multi_batch: bool) -> dict:
    """Validate persistence and surface anomalous *signals*, not editor truth.

    Validates the saved per-batch summaries and recomputes cumulative totals.
    Does not verify media hashes or replay any FFmpeg stage; that requires the
    actual Windows media pipeline. Legacy audits are explicitly unavailable.
    """
    if multi_batch:
        has_meta = ("real_cut_observations" in audit
                    or "real_cut_observation_totals" in audit
                    or any("real_cut_observations" in batch
                           for batch in audit.get("batches", [])))
        if not has_meta:
            return {"status": "LEGACY_NO_CUT_OBSERVATIONS"}
        if ("real_cut_observations" not in audit
                or "real_cut_observation_totals" not in audit):
            raise ValueError("Incomplete cumulative cut diagnostic evidence")
        expected = set()
        for row in rows:
            if row["status"] in _COMPLETE:
                expected.update(row["selected_clip_ids"])
        observed = _checked_rows(audit["real_cut_observations"], expected)
        combined: dict[str, dict] = {}
        for saved_batch, batch in zip(audit["batches"], rows):
            if "real_cut_observations" not in saved_batch:
                raise ValueError("Missing per-batch cut observation evidence")
            selected = set(batch["selected_clip_ids"])
            this = _checked_rows(saved_batch["real_cut_observations"], selected)
            if batch["status"] in _COMPLETE:
                if set(this) != selected or set(this) & set(combined):
                    raise ValueError("Completed clip lacks unique cut evidence")
                combined.update(this)
        if observed != combined:
            raise ValueError("Cumulative cut evidence disagrees with batches")
    else:
        raw = audit.get("observations", {})
        if not isinstance(raw, dict):
            raise ValueError("Invalid single-run cut observation container")
        if ("real_cut_observations" not in raw
                and "real_cut_observation_totals" not in raw):
            return {"status": "LEGACY_NO_CUT_OBSERVATIONS"}
        if ("real_cut_observations" not in raw
                or "real_cut_observation_totals" not in raw):
            raise ValueError("Incomplete single-batch cut diagnostic evidence")
        selected = set(rows[0]["selected_clip_ids"]) if rows else set()
        observed = _checked_rows(raw["real_cut_observations"], selected)
        if rows and rows[0]["status"] in _COMPLETE and set(observed) != selected:
            raise ValueError("Completed single-run cut observations are missing")
    totals = (audit.get("real_cut_observation_totals") if multi_batch
              else audit["observations"]["real_cut_observation_totals"])
    if totals != aggregate_real_cut_observations(observed):
        raise ValueError("Forged or stale cut observation cumulative totals")
    findings = {clip_id: _triage_codes(row)
                for clip_id, row in observed.items()}
    codes = Counter(code for all_codes in findings.values() for code in all_codes)
    return {
        "status": "REFERENCE_FREE_SIGNAL_TRIAGE",
        "source": "SAVED_UNREVIEWED_OBSERVATIONS_ONLY",
        "independent_editor_gold": False,
        "real_media_rechecked": False,
        "clips_with_diagnostics": len(findings),
        "clips_with_reference_free_measurements": totals["described_clips"],
        "signal_counts": dict(sorted(codes.items())),
        "per_clip_signal_codes": findings,
        "editor_cut_precision": None,
        "editor_cut_recall": None,
        "ready": False,
        "qualification_allowed": False,
    }
