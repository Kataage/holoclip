"""Prepare real finished B2/B3 visual benchmark cases without calling providers.

Opt-in local FFmpeg/PySceneDetect sampling reuses the existing two-variant
blind playback study, verifies all source hashes and creates a v2 benchmark
manifest. Diagnostic-only, in-sample; never human gold or edit promotion.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

from holoclip.provider_benchmark import (
    VERSION as BENCHMARK_VERSION, BenchmarkCase, BenchmarkManifest,
    verified_window,
)
from holoclip.render_b2_b3_blind_edge_association import _cut_pair
from holoclip.render_b2_b3_blind_full_study import _prepare
from holoclip.render_b2_b3_blind_rating_ingest import _verify_study
from holoclip.render_human_edit_context import DEFAULT_GROWTH_STATE, DEFAULT_HUMAN_STORE
from holoclip.render_plan import _hash, _no_links
from holoclip.render_ready_queue import DEFAULT_PREVIEW_ROOT
from holoclip.render_same_source_geometry import DEFAULT_TRANSCRIPT_ROOT
from holoclip.render_source_queue import DEFAULT_QUEUE
from holoclip.render_three_way_existing_media_autopilot import _exclusive_run_lock
from holoclip.video_measurement import measure_window, probe_video

VERSION = "holoclip-b2-b3-real-visual-benchmark-preparation-v1"
_ROLES = ("b2", "b3")
_MAX_DURATION = 180.0
_WINDOW = 6.0
_MAX_FRAMES_PER_CASE = 48
_DIMENSIONS = frozenset({"japanese", "avatar", "game_ui", "reaction", "ocr"})


def _sample_plan(state: dict) -> dict:
    """Same review stages for each real finished video; no fabricated events."""
    edge = _cut_pair(state)
    roles = state["state"]["basis"]["context"]["roles"]
    result = {}
    for role in _ROLES:
        src = _no_links(Path(state["paths"][role]))
        meta = probe_video(src)
        if not isinstance(meta, dict) or not isinstance(meta.get("streams"), list):
            raise ValueError("VISUAL_BENCH_PROBE_INVALID")
        streams = meta["streams"]
        if not any(row.get("codec_type") == "video" for row in streams):
            raise ValueError("VISUAL_BENCH_VIDEO_STREAM_MISSING")
        if not any(row.get("codec_type") == "audio" for row in streams):
            raise ValueError("VISUAL_BENCH_AUDIO_STREAM_MISSING")
        try:
            length = float(meta["format"]["duration"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("VISUAL_BENCH_DURATION_INVALID") from exc
        expected = roles[role]["timeline"].estimated_duration
        if (not math.isfinite(length) or not _WINDOW + 2 <= length <= _MAX_DURATION
                or not math.isfinite(expected) or abs(length - expected) > .75):
            raise ValueError("VISUAL_BENCH_DURATION_MISMATCH")
        segments = sorted(
            roles[role]["timeline"].segments, key=lambda seg: seg.output_order,
        )
        active = edge["source_cut_index"]
        clock = sum(seg.source_end - seg.source_start for seg in segments[:active])
        if edge["edge"] == "EXTEND_END":
            clock += segments[active].source_end - segments[active].source_start
        centers = (
            ("opening", _WINDOW / 2),
            ("edit_edge", clock),
            ("closing", length - _WINDOW / 2),
        )
        windows = []
        for label, center in centers:
            lo = min(max(0.0, center - _WINDOW / 2), length - _WINDOW)
            hi = lo + _WINDOW
            if not 0 <= lo < hi <= length + 0.002:
                raise ValueError("VISUAL_BENCH_WINDOW_INVALID")
            windows.append({
                "region": label,
                "start": round(lo, 3),
                "end": round(hi, 3),
                "output_clock_not_common_source_clock": True,
            })
        result[role] = {
            "finished_sha256": _hash(src)[0],
            "duration_seconds": round(length, 3),
            "windows": windows,
        }
    return {"changed_edit": edge, "roles": result}


def prepare_visual_benchmark(
    *, queue: Path = DEFAULT_QUEUE, example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_PREVIEW_ROOT,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    job_id: str | None = None, proposal_index: int = 0,
    refinement_index: int = 0, candidate_index: int = 0,
    target_index: int = 0, execute: bool = False,
    approve_public_media: bool = False,
    output_root: Path | None = None,
) -> dict:
    """Preview only unless explicit public-data approval permits local extraction."""
    if (type(execute) is not bool or type(approve_public_media) is not bool
            or not 0 <= target_index < 20 or type(target_index) is not int):
        raise ValueError("VISUAL_BENCH_ARGUMENT_INVALID")
    if execute and not approve_public_media:
        raise ValueError("VISUAL_BENCH_PUBLIC_MEDIA_APPROVAL_REQUIRED")
    if execute and os.environ.get("HOLOCLIP_DATA_CLASS") != "public":
        raise ValueError("VISUAL_BENCH_PUBLIC_MEDIA_ENV_REQUIRED")
    params = dict(queue=queue, example_store=example_store,
                  growth_state=growth_state, preview_root=preview_root,
                  transcript_root=transcript_root, job_id=job_id,
                  proposal_index=proposal_index, refinement_index=refinement_index,
                  candidate_index=candidate_index, target_index=target_index)
    state = _prepare(params)
    if state["status"] != "SUPPORTED":
        return {
            "version": VERSION, "status": state["status"],
            "ready": False, "production_ready": False,
            "publication_performed": False,
        }
    lineage = _verify_study(state)
    plan = _sample_plan(state)
    dest = (_no_links(Path(output_root)) if output_root is not None else
            _no_links(state["private"].with_name(
                state["private"].stem + ".visual-benchmark")))
    summary = {
        "version": VERSION,
        "status": "VISUAL_BENCH_READ_ONLY_PLAN",
        "study_id": state["report"]["study_id"],
        "study_receipts_sha256": lineage,
        "source_edits": plan["changed_edit"],
        "variant_windows": plan["roles"],
        "prepared_video_case_count": 6,
        "all_cases_real_media": True,
        "all_cases_held_out": False,
        "independent_human_gold_verified": False,
        "actual_model_calls": 0, "additional_downloads": False,
        "production_promotion_allowed": False,
        "automatic_winner": None,
        "automatic_adoption_allowed": False,
        "publication_performed": False, "ready": False, "production_ready": False,
    }
    if not execute:
        return summary

    with _exclusive_run_lock(
        _no_links(Path(preview_root)), state["evidence"]["job_id"],
    ):
        again = _prepare(params)
        if (again["status"] != "SUPPORTED"
                or _verify_study(again) != lineage
                or _sample_plan(again) != plan
                or dest.exists() or dest.is_symlink()):
            raise ValueError("VISUAL_BENCH_STUDY_CHANGED_OR_OUTPUT_EXISTS")
        # The root is create-only. On failure keep a clearly incomplete folder:
        # never auto-delete user media, overwrite data, or fake a completed run.
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(dest)
        cases = []
        for idx, role in enumerate(state["order"], start=1):
            src = state["paths"][role]
            slot = f"candidate-{idx}"
            for region in plan["roles"][role]["windows"]:
                tag = region["region"]
                window = measure_window(
                    src, dest / f"{slot}-{tag}",
                    asset_id=state["report"]["study_id"] + "-" + slot,
                    start=region["start"], end=region["end"],
                )
                if (len(window.frames) > _MAX_FRAMES_PER_CASE
                        or not window.contact_sheet_paths):
                    raise ValueError("VISUAL_BENCH_TOO_MANY_FRAMES_OR_NO_SHEETS")
                case = BenchmarkCase(
                    id=f"{state['report']['study_id']}-{slot}-{tag}",
                    clip_id=state["report"]["study_id"],
                    task="video", source_url="LOCAL_VERIFIED_FINISHED_VIDEO",
                    teacher_reference="UNVERIFIED_NO_GOLD_REFERENCE",
                    public_data=True, real_media=True, held_out=False,
                    dimensions=_DIMENSIONS,
                    measurement_path=str(
                        Path(window.video_path).parent / "measurement.json"),
                )
                verified_window(case)
                cases.append(case)
            if _hash(src)[0] != plan["roles"][role]["finished_sha256"]:
                raise ValueError("VISUAL_BENCH_SOURCE_CHANGED_DURING_EXTRACTION")
        refreshed = _prepare(params)
        if (refreshed["status"] != "SUPPORTED"
                or _verify_study(refreshed) != lineage
                or _sample_plan(refreshed) != plan):
            raise ValueError("VISUAL_BENCH_STUDY_CHANGED_AFTER_EXTRACTION")
        manifest = BenchmarkManifest(
            version=BENCHMARK_VERSION, cases=tuple(cases),
            training_clip_ids=frozenset({state["report"]["study_id"]}),
            repeats=1, timeout_seconds=60,
        )
        manifest_path = _no_links(dest / "benchmark.json")
        with manifest_path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(manifest.model_dump_json(indent=2))
        result = dict(summary, status="VISUAL_BENCH_LOCAL_MEASUREMENTS_UNQUALIFIED",
                      benchmark_manifest_path=str(manifest_path),
                      benchmark_manifest_sha256=hashlib.sha256(
                          manifest_path.read_bytes()).hexdigest())
        with _no_links(dest / "status.json").open(
            "x", encoding="utf-8", newline="\n",
        ) as handle:
            json.dump(result, handle, ensure_ascii=False, sort_keys=True, indent=2)
        return result
