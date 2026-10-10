"""Cut-aligned offline ASR caption sidecars: no invented word timestamps."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from holoclip.domain import ClipTimeline, SourceAsset, TimelineSegment
from holoclip.render_plan import LocalRenderSource
from holoclip.render_subtitles import compose_cut_aligned_srt, export_cut_aligned_srt


VID_A = "AbcDef12345"
VID_B = "ZyxWvu98765"


def _case(tmp_path):
    root = tmp_path / "transcripts"
    root.mkdir()
    sources = [
        LocalRenderSource(
            asset=SourceAsset(id=name, platform="youtube",
                              external_id=external_id,
                              access_class="public", availability="available",
                              source_type="stream"),
            path=tmp_path / f"{name}.mkv", local_render_approved=True,
        )
        for name, external_id in (("a", VID_A), ("b", VID_B))
    ]
    timeline = ClipTimeline(
        id="cut-clock",
        segments=[
            TimelineSegment(
                id="source-b-first", source_asset_id="b",
                source_start=50, source_end=52, output_order=0,
            ),
            TimelineSegment(
                id="source-a-second", source_asset_id="a",
                source_start=10, source_end=13, output_order=1,
            ),
        ], estimated_duration=5,
    )
    def save(vid, segments, *, folder=None):
        destination = folder or root
        destination.mkdir(parents=True, exist_ok=True)
        (destination / f"{vid}.json").write_text(json.dumps(
            segments, ensure_ascii=False,
        ), encoding="utf-8")
    save(VID_A, [
        {"start": 9.5, "end": 10.5, "text": "前の部分"},
        {"start": 10.5, "end": 11.5, "text": "ありがとう"},
        {"start": 12.0, "end": 13.4, "text": "後ろで切れる"},
    ])
    save(VID_B, [
        {"start": 50.25, "end": 51.25, "text": "びっくり！"},
    ])
    return timeline, sources, root, save


def test_multisource_cut_order_and_partial_utterance_dropped(tmp_path):
    timeline, sources, root, _ = _case(tmp_path)
    text, count, dropped = compose_cut_aligned_srt(
        timeline, sources, transcript_root=root,
    )
    assert count == 2 and dropped == 2
    assert "00:00:00,250 --> 00:00:01,250\nびっくり！" in text
    assert "00:00:02,500 --> 00:00:03,500\nありがとう" in text
    assert text.index("びっくり") < text.index("ありがとう")
    assert "前の部分" not in text and "後ろで切れる" not in text


def test_output_boundaries_follow_compacted_cut_not_original_source_time(tmp_path):
    timeline, sources, root, _ = _case(tmp_path)
    timeline.segments[0].source_end = 51
    timeline.estimated_duration = 4
    text, _, _ = compose_cut_aligned_srt(timeline, sources, transcript_root=root)
    assert "びっくり！" not in text  # interval crosses new first cut
    assert "00:00:01,500 --> 00:00:02,500" in text


def test_reused_same_stream_maps_cues_to_each_cut_occurrence(tmp_path):
    timeline, sources, root, _ = _case(tmp_path)
    timeline.segments[0].source_asset_id = "a"
    timeline.segments[0].source_start = 10
    timeline.segments[0].source_end = 12
    text, count, dropped = compose_cut_aligned_srt(
        timeline, sources[:1], transcript_root=root,
    )
    assert count == 2
    assert text.count("ありがとう") == 2
    assert "00:00:00,500 --> 00:00:01,500" in text
    assert "00:00:02,500 --> 00:00:03,500" in text


def test_missing_transcript_refuses_partial_multi_source_subtitles(tmp_path):
    timeline, sources, root, _ = _case(tmp_path)
    (root / f"{VID_A}.json").unlink()
    with pytest.raises(ValueError, match="LOCAL_SUBTITLE_TRANSCRIPT_NOT_AVAILABLE"):
        compose_cut_aligned_srt(timeline, sources, transcript_root=root)


def test_ambiguous_transcript_layouts_fail_closed(tmp_path):
    timeline, sources, root, save = _case(tmp_path)
    save(VID_A, [{"start": 10.5, "end": 11.5, "text": "same"}],
         folder=root / "examples" / "sources")
    with pytest.raises(ValueError, match="LOCAL_SUBTITLE_TRANSCRIPT_AMBIGUOUS"):
        compose_cut_aligned_srt(timeline, sources, transcript_root=root)


def test_uses_existing_ingest_transcript_layout(tmp_path):
    timeline, sources, root, save = _case(tmp_path)
    data = (root / f"{VID_A}.json").read_bytes()
    (root / f"{VID_A}.json").unlink()
    folder = root / "examples" / "sources"
    folder.mkdir(parents=True)
    (folder / f"{VID_A}.json").write_bytes(data)
    text, count, _ = compose_cut_aligned_srt(
        timeline, sources, transcript_root=root,
    )
    assert count == 2 and "ありがとう" in text


def test_metadata_mismatched_source_video_id_blocks(tmp_path):
    timeline, sources, root, _ = _case(tmp_path)
    target = root / f"{VID_A}.json"
    document = json.loads(target.read_text())
    target.write_text(json.dumps({
        "source_id": VID_B, "segments": document,
    }))
    with pytest.raises(ValueError, match="LOCAL_SUBTITLE_TRANSCRIPT_ID_MISMATCH"):
        compose_cut_aligned_srt(timeline, sources, transcript_root=root)


def test_untrusted_asr_controls_or_markup_not_emitted_raw(tmp_path):
    timeline, sources, root, save = _case(tmp_path)
    save(VID_A, [
        {"start": 10.5, "end": 11.5,
         "text": "<script> & 不適切な HTML"},
        {"start": 11.5, "end": 12.1, "text": "bad\u202esequence"},
    ])
    text, count, dropped = compose_cut_aligned_srt(
        timeline, sources, transcript_root=root,
    )
    assert "script&gt;" in text and "<script>" not in text
    assert "bad" not in text
    assert dropped >= 1 and count == 2


@pytest.mark.parametrize("bad", [
    [{"start": 11, "end": 12, "text": "later"},
     {"start": 10, "end": 11, "text": "earlier"}],
    [{"start": 10, "end": 13, "text": ""}],
    {"wrong": "schema"},
])
def test_malformed_or_unsorted_transcript_rejected(tmp_path, bad):
    timeline, sources, root, _ = _case(tmp_path)
    (root / f"{VID_A}.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        compose_cut_aligned_srt(timeline, sources, transcript_root=root)


def test_transcript_symlink_is_not_read(tmp_path):
    timeline, sources, root, _ = _case(tmp_path)
    original = root / f"{VID_A}.json"
    original.unlink()
    external = tmp_path / "private.json"
    external.write_text("PRIVATE")
    try:
        original.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported")
    with pytest.raises(ValueError, match="LOCAL_RENDER_LINKED_PATH"):
        compose_cut_aligned_srt(timeline, sources, transcript_root=root)
    assert external.read_text() == "PRIVATE"


def test_create_only_srt_and_no_overwrite(tmp_path):
    timeline, sources, root, _ = _case(tmp_path)
    video = tmp_path / "local-preview.mp4"
    video.write_bytes(b"synthetic already-verified mp4")
    outcome = export_cut_aligned_srt(
        timeline, sources, transcript_root=root, mp4_output=video,
    )
    assert outcome["status"] == "LOCAL_SUBTITLE_SRT_CREATED_UNVERIFIED_ASR"
    assert outcome["cue_count"] == 2
    assert len(outcome["output_sha256"]) == 64
    assert outcome["ready"] is False
    sidecar = video.with_suffix(".srt")
    before = sidecar.read_bytes()
    repeated = export_cut_aligned_srt(
        timeline, sources, transcript_root=root, mp4_output=video,
    )
    assert repeated["status"] == "LOCAL_SUBTITLE_UNAVAILABLE_OR_UNTRUSTED"
    assert sidecar.read_bytes() == before


def test_existing_operator_sidecar_never_overwritten(tmp_path):
    timeline, sources, root, _ = _case(tmp_path)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"media")
    sidecar = video.with_suffix(".srt")
    sidecar.write_bytes(b"operator subtitle remains")
    result = export_cut_aligned_srt(
        timeline, sources, transcript_root=root, mp4_output=video,
    )
    assert result["status"] == "LOCAL_SUBTITLE_UNAVAILABLE_OR_UNTRUSTED"
    assert sidecar.read_bytes() == b"operator subtitle remains"


def test_missing_caption_data_does_not_create_empty_sidecar(tmp_path):
    timeline, sources, root, _ = _case(tmp_path)
    (root / f"{VID_B}.json").unlink()
    video = tmp_path / "video.mp4"
    video.write_bytes(b"already verified")
    result = export_cut_aligned_srt(
        timeline, sources, transcript_root=root, mp4_output=video,
    )
    assert result["status"] == "LOCAL_SUBTITLE_UNAVAILABLE_OR_UNTRUSTED"
    assert result["cue_count"] == 0
    assert not video.with_suffix(".srt").exists()


def test_failed_mp4_is_not_captioned(tmp_path):
    timeline, sources, root, _ = _case(tmp_path)
    video = tmp_path / "not-created.mp4"
    result = export_cut_aligned_srt(
        timeline, sources, transcript_root=root, mp4_output=video,
    )
    assert result["status"] == "LOCAL_SUBTITLE_UNAVAILABLE_OR_UNTRUSTED"
    assert not video.with_suffix(".srt").exists()
