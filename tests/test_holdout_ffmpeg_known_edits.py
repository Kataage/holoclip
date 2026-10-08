"""Controlled real-FFmpeg discontiguous edit with a known cut boundary.

The ground truth here is *only the test's own edit script*. It is not
reviewer-verified Hololive gold and must never enter the learning corpus.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from holoclip.corpus_growth import CorpusGrowthItem, CorpusGrowthState
from holoclip.holdout_e2e_trial import run_reserved_holdout_e2e
from test_holdout_real_ffmpeg_chain import (
    CLIP_URL, SOURCE_ID, SOURCE_URL, GeneratedVideoProvider,
    _ffmpeg, _generated_media,
)

# Two separate observed original time spans, neither borrowed from the
# original continuous clip fixture. The cut should occur at output t=22s.
ORIGINAL_PIECES = ((9., 31.), (44., 66.))
SCRIPTED_CUT_SECOND = ORIGINAL_PIECES[0][1] - ORIGINAL_PIECES[0][0]
EDITED_DURATION = sum(end - start for start, end in ORIGINAL_PIECES)


def _assemble_discontiguous_edit(source: Path, output: Path, *, background=False):
    """Decode once, concatenate both AV streams in re-encoded output time."""
    a0, b0 = ORIGINAL_PIECES[0]
    a1, b1 = ORIGINAL_PIECES[1]
    args = ["-i", str(source)]
    if background:
        args += ["-f", "lavfi", "-i",
                 f"sine=frequency=967:sample_rate=16000:duration={EDITED_DURATION}"]
    graph = (
        f"[0:v]trim=start={a0}:end={b0},setpts=PTS-STARTPTS[v0];"
        f"[0:a]atrim=start={a0}:end={b0},asetpts=PTS-STARTPTS[a0];"
        f"[0:v]trim=start={a1}:end={b1},setpts=PTS-STARTPTS[v1];"
        f"[0:a]atrim=start={a1}:end={b1},asetpts=PTS-STARTPTS[a1];"
        "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
    )
    if background:
        graph += ";[a][1:a]amix=inputs=2:weights='1 0.04':normalize=0[final]"
        audio_map = "[final]"
    else:
        audio_map = "[a]"
    _ffmpeg(
        *args, "-filter_complex", graph, "-map", "[v]", "-map", audio_map,
        "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-t", str(EDITED_DURATION), str(output),
    )


def _run(tmp_path, *, background=False):
    # Actual FFmpeg encoded native source and independently re-encoded edit.
    wav, original, _ = _generated_media(tmp_path / "files")
    clip = tmp_path / "files" / "discontinuous.mp4"
    _assemble_discontiguous_edit(original, clip, background=background)
    provider = GeneratedVideoProvider(wav, original, clip)
    state_path = tmp_path / "growth.json"
    state_path.write_text(CorpusGrowthState(
        holdout_stream_ids=[SOURCE_ID],
        items={"cut-example": CorpusGrowthItem(
            clip_url=CLIP_URL, source_url=SOURCE_URL,
            source_stream_id=SOURCE_ID, status="holdout_reserved",
            query="controlled scripted FFmpeg edit (not external media)",
        )},
    ).model_dump_json(), encoding="utf-8")
    root = tmp_path / "evidence"
    messages = []
    result = run_reserved_holdout_e2e(
        provider, store=tmp_path / "unmodified-training.jsonl",
        state_path=state_path, root=root, max_clips=1, policy_rules=[],
        progress=messages.append,
    )
    return result, root, provider, messages


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="Actual FFmpeg binaries required for encoded edit test",
)
@pytest.mark.parametrize("background", [False, True], ids=["hard-edit", "edit-with-bgm"])
def test_full_measured_pipeline_on_real_encoded_noncontiguous_edit(tmp_path, background):
    result, root, provider, messages = _run(tmp_path, background=background)
    assert result["status"] == "UNREVIEWED_MEASUREMENTS_COMPLETE", (
        result["status"], result.get("blocked_stage"), result.get("error_class"),
        result.get("observations", {}).get("stage_status_counts"), messages,
    )
    assert result["real_media_run_completed"] is True
    assert len(result["stages"]) == 9
    assert provider.video_range_requests
    assert len(provider.video_range_requests) >= 2
    for start, end in provider.video_range_requests:
        assert 0 <= start < end <= 78
        assert end - start <= 120
    observed = result["observations"]
    assert observed["clips_with_unique_original_audio_matches"] == 1

    cut_doc = json.loads(
        (root / "cut_brackets" / "holdout-cut-brackets.json").read_text(encoding="utf-8")
    )
    cut_row = next(iter(cut_doc["records"].values()))
    acoustic = cut_row["refinement"]
    assert acoustic["status"] == "AUDIO_SUPPORTED_INTERVALS"
    candidates = [
        entry for entry in acoustic["transitions"]
        if entry["status"] == "CUT_INTERVAL_CANDIDATE"
    ]
    # The independently SCRIPTED cut time is test-only truth. An approximate
    # acoustic bracket must straddle it; it does NOT prove a native editor frame.
    assert any(
        start <= SCRIPTED_CUT_SECOND <= end
        for item in candidates
        for start, end in (item["interval"],)
    ), {
        "scripted_boundary": SCRIPTED_CUT_SECOND,
        "actual_transitions": acoustic["transitions"],
        "ranges": json.loads(
            (root / "holdout-audio-alignment.json").read_text(encoding="utf-8")
        )["records"],
    }

    # Even a successful cut *candidate* remains intentionally unreviewed.
    for flag in (
        "qualification_allowed", "benchmark_held_out", "event_split_verified",
        "reviewed_gold_available", "frame_accurate_cuts_verified",
        "dense_output_source_map_verified",
    ):
        assert result[flag] is False
    assert result["ready"] is False
    for field in (
        "editor_cut_precision", "editor_cut_recall",
        "native_timestamp_error_seconds", "event_holdout_accuracy",
    ):
        assert observed[field] is None
