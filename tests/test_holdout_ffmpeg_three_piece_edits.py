"""Three synthetic FFmpeg edit passages, with two known scripted boundaries.

Actual FFmpeg, FFT fingerprints, native *section* PTS, image matching and
all nine real reserved stages run. No Hololive or independently reviewed gold.
"""
from __future__ import annotations

import json
import shutil

import pytest

from holoclip.corpus_growth import CorpusGrowthItem, CorpusGrowthState
from holoclip.examples import human_clip_identity
from holoclip.holdout_e2e_trial import run_reserved_holdout_e2e
from holoclip.scripted_cut_metrics import score_scripted_cut_report, REFERENCE_VERSION
from test_holdout_real_ffmpeg_chain import (
    CLIP_URL, SOURCE_ID, SOURCE_URL, GeneratedVideoProvider,
    _ffmpeg, _generated_media,
)

PIECES = ((7., 23.), (31., 47.), (55., 71.))
KNOWN_BOUNDARIES = (16., 32.)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="Real FFmpeg and ffprobe required for multi-cut test",
)
def test_three_actual_ffmpeg_edits_keep_three_source_islands_and_both_cut_brackets(tmp_path):
    wav, original, _ = _generated_media(tmp_path / "generated")
    finished = tmp_path / "generated" / "three_cut.mp4"
    filters = []
    parts = []
    for i, (start, end) in enumerate(PIECES):
        filters.append(
            f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{i}];"
            f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]"
        )
        parts += [f"[v{i}]", f"[a{i}]"]
    filters.append("".join(parts) + "concat=n=3:v=1:a=1[finishedv][finisheda]")
    _ffmpeg(
        "-i", str(original), "-filter_complex", ";".join(filters),
        "-map", "[finishedv]", "-map", "[finisheda]",
        "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
        "-c:a", "aac", "-b:a", "128k", "-t", "48", str(finished),
    )
    provider = GeneratedVideoProvider(wav, original, finished)
    state = tmp_path / "state.json"
    state.write_text(CorpusGrowthState(
        holdout_stream_ids=[SOURCE_ID],
        items={"three-parts": CorpusGrowthItem(
            clip_url=CLIP_URL, source_url=SOURCE_URL,
            source_stream_id=SOURCE_ID,
            status="holdout_reserved",
            query="synthetic three-source-passage test",
        )},
    ).model_dump_json(), encoding="utf-8")
    root = tmp_path / "evidence"
    messages = []
    result = run_reserved_holdout_e2e(
        provider, store=tmp_path / "empty-learning.jsonl",
        state_path=state, root=root, policy_rules=[], max_clips=1,
        progress=messages.append,
    )
    assert result["status"] == "UNREVIEWED_MEASUREMENTS_COMPLETE", (
        result["status"], result.get("blocked_stage"),
        result.get("error_class"), messages,
    )
    assert len(result["stages"]) == 9
    assert len(provider.video_range_requests) == 3
    assert result["observations"]["all_nine_stages_measured_clips"] == 1
    audio_report = json.loads(
        (root / "holdout-audio-alignment.json").read_text(encoding="utf-8")
    )
    audio = next(iter(audio_report["records"].values()))
    assert audio["status"] == "PARTIAL_AUDIO_MATCH"
    assert len(audio["ranges"]) == 3
    assert audio["source_monotonic"] is True
    acoustic_report = json.loads(
        (root / "cut_brackets" / "holdout-cut-brackets.json").read_text(encoding="utf-8")
    )
    acoustic = next(iter(acoustic_report["records"].values()))["refinement"]
    candidates = [
        t["interval"] for t in acoustic["transitions"]
        if t["status"] == "CUT_INTERVAL_CANDIDATE"
    ]
    # Known boundaries are true ONLY for our own scripted artificial media.
    assert all(any(lo <= scripted <= hi for lo, hi in candidates)
               for scripted in KNOWN_BOUNDARIES), candidates
    synthetic_score = score_scripted_cut_report({
        "version": REFERENCE_VERSION,
        "reference_kind": "SCRIPTED_FFMPEG_ONLY",
        "clip_id": human_clip_identity(CLIP_URL),
        "duration_seconds": 48.,
        "cut_seconds": list(KNOWN_BOUNDARIES),
    }, acoustic_report)
    assert synthetic_score["true_positive_candidates"] == 2
    assert synthetic_score["missed_scripted_cuts"] == 0
    assert synthetic_score["scripted_interval_recall"] == 1.
    assert synthetic_score["false_positive_candidates"] >= 0
    assert synthetic_score["matched_midpoint_median_absolute_error_seconds"] is not None
    assert synthetic_score["reviewed_gold_available"] is False
    assert synthetic_score["editor_cut_recall"] is None
    assert synthetic_score["ready"] is False

    assert result["ready"] is False
    assert result["qualification_allowed"] is False
    assert result["frame_accurate_cuts_verified"] is False
    assert result["reviewed_gold_available"] is False
    assert result["dense_output_source_map_verified"] is False
    assert result["observations"]["editor_cut_precision"] is None
    assert result["observations"]["editor_cut_recall"] is None
