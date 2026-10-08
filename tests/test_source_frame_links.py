"""Audio-conditioned source/finished frame links must not fabricate edited cuts."""
import json
from pathlib import Path

import cv2
import pytest

from holoclip.clip_policy import ClipPolicyRule, PolicyDecision
from holoclip.examples import upsert_human_clip_examples
from holoclip.holdout_frame_links_pipeline import correlate_reserved_source_frames
from holoclip.source_frame_links import link_measured_frames
from holoclip.video_measurement import checksum_file
from test_editorial_profile import trusted_example
from test_source_section_clock import calibrated, run as run_clock
from test_visual_edit_evidence import pair_fixture, texture, transformed, window


def clock(origin=40.):
    return {
        "status": "PARTIAL_SECTION_AUDIO_CLOCK", "origin_seconds": origin,
        "frame_pts_verified": True, "original_video_absolute_pts_verified": False,
    }


def test_two_separated_real_picture_correspondences_agree_with_audio(tmp_path):
    a, b = texture(13), texture(43)
    source = window(tmp_path, "source", [(1, a), (3, b)])
    finished = window(tmp_path, "finished", [
        (1, transformed(a, zoom=1.2)), (3, transformed(b, zoom=1.18)),
    ])
    result = link_measured_frames(
        source, finished, clock(), audio_offset_seconds=40.,
    )
    assert result["status"] == "PARTIAL_AUDIO_VISUAL_FRAME_LINKS"
    assert len(result["frame_links"]) == 2
    assert result["examined_frame_comparisons"] == 4
    for item in result["frame_links"]:
        assert abs(item["audio_visual_residual_seconds"]) <= .05
        assert item["orb_inliers"] >= 12
        assert item["kind"] in {"zoom_candidate", "zoom_reframe_candidate"}
    assert result["original_video_absolute_pts_verified"] is False
    assert result["frame_accurate_cuts_verified"] is False
    assert result["dense_output_source_map_verified"] is False


def test_repeated_source_background_is_not_unique_time_map(tmp_path):
    a = texture(55)
    source = window(tmp_path, "source", [(1, a), (3, a), (6, a)])
    finished = window(tmp_path, "finished", [
        (1, transformed(a)), (3, transformed(a)),
    ])
    r = link_measured_frames(
        source, finished, clock(), audio_offset_seconds=40.,
        time_tolerance_seconds=2.,
    )
    assert r["status"] != "PARTIAL_AUDIO_VISUAL_FRAME_LINKS"
    assert not r["frame_links"]
    assert r["rejected"]["repeated_visual_content"] >= 1


def test_reverse_order_and_wrong_audio_origin_cannot_be_proved(tmp_path):
    a, b = texture(20), texture(75)
    source = window(tmp_path, "source", [(1, a), (3, b)])
    reversed_clip = window(tmp_path, "finished", [(1, b), (3, a)])
    wrong = link_measured_frames(
        source, reversed_clip, clock(), audio_offset_seconds=40.,
    )
    assert wrong["status"] != "PARTIAL_AUDIO_VISUAL_FRAME_LINKS"
    assert not wrong["frame_links"]
    shifted = link_measured_frames(
        source, window(tmp_path, "normal", [(1, a), (3, b)]),
        clock(origin=55.), audio_offset_seconds=40.,
    )
    assert shifted["status"] == "UNKNOWN_NO_COMPATIBLE_VISUAL_LINKS"
    assert not shifted["frame_links"]


def test_missing_clock_and_frame_budget_fail_closed_without_image_io(tmp_path):
    image = texture()
    src = window(tmp_path, "source", [(1, image), (2, image)])
    dst = window(tmp_path, "finish", [(1, image), (2, image)])
    unknown = link_measured_frames(
        src, dst, {"status": "UNKNOWN_NONUNIQUE_SOURCE_AUDIO"},
        audio_offset_seconds=40.,
    )
    assert unknown["status"] == "UNKNOWN_CLOCK"
    assert unknown["frame_links"] == []
    budget = link_measured_frames(
        src, dst, clock(), audio_offset_seconds=40., max_comparisons=3,
    )
    assert budget["status"] == "UNKNOWN_FRAME_PAIR_BUDGET"
    with pytest.raises(ValueError):
        link_measured_frames(src, dst, clock(), audio_offset_seconds=float("nan"))


def _add_second_verified_frame(manifest_path, *, label):
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    images = [texture(13), texture(75)]
    frame = raw["window"]["frames"][0]
    for index, image in enumerate(images):
        filename = manifest_path.parent / f"{label}-real-{index}.png"
        content = image if label == "source" else transformed(image, zoom=1.20)
        assert cv2.imwrite(str(filename), content)
        item = {**frame, "id": f"{label}-f{index}", "shot_id": f"{label}-s{index}",
                "pts_seconds": .5 + index * 2.,
                "locator": str(filename), "checksum": checksum_file(filename)}
        if index == 0:
            raw["window"]["frames"][0] = item
        else:
            raw["window"]["frames"].append(item)
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")


def fixture(tmp_path):
    provider, store, state, prepared, audio_root = pair_fixture(tmp_path)
    pair_path = prepared / "source_pairs" / "holdout-source-pairs.json"
    e = next(iter(json.loads(pair_path.read_text(encoding="utf-8"))["records"].values()))
    for label in ("source", "finished"):
        _add_second_verified_frame(Path(e["pairs"][0][f"{label}_measurement"]), label=label)
    result = run_clock(
        provider, store, state, prepared, audio_root,
        calibrate=calibrated,
    )
    assert result["counts"] == {"MEASURED_SOURCE_SECTION_CLOCK_DIAGNOSTICS": 1}
    return provider, store, state, prepared, audio_root


def run(provider, store, state, prepared, audio_root, **kwargs):
    return correlate_reserved_source_frames(
        provider, store=store, state_path=state,
        prepared_root=prepared, audio_root=audio_root,
        pairs_root=prepared / "source_pairs", clocks_root=prepared / "source_clocks",
        output=prepared / "frame_links", policy_rules=kwargs.pop("policy_rules", []),
        max_clips=2, **kwargs,
    )


def test_complete_holdout_frame_correspondence_and_resumability(tmp_path):
    provider, store, state, prepared, audio_root = fixture(tmp_path)
    first = run(provider, store, state, prepared, audio_root)
    assert first["counts"] == {"MEASURED_UNREVIEWED_FRAME_LINKS": 1}
    report_path = prepared / "frame_links" / "holdout-frame-links.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    for key in (
        "qualification_allowed", "benchmark_held_out",
        "event_split_verified", "frame_accurate_cuts_verified",
        "reviewed_gold_available", "original_video_absolute_pts_verified",
        "dense_output_source_map_verified",
    ):
        assert report[key] is False
    row = next(iter(report["records"].values()))
    obs = row["group_observations"][0]
    assert obs["status"] == "PARTIAL_AUDIO_VISUAL_FRAME_LINKS"
    assert len(obs["frame_links"]) == 2
    assert row["status"] == "MEASURED_UNREVIEWED_FRAME_LINKS"
    def no_compare(*args, **kwargs):
        raise AssertionError("Clean cache must reuse checked linked frames")
    assert run(
        provider, store, state, prepared, audio_root,
        correlate=no_compare,
    )["counts"] == first["counts"]


def test_corrupt_measured_picture_blocks_before_metadata_requests(tmp_path):
    _, store, state, prepared, audio_root = fixture(tmp_path)
    pair = next(iter(json.loads(
        (prepared / "source_pairs" / "holdout-source-pairs.json").read_text(encoding="utf-8")
    )["records"].values()))["pairs"][0]
    manifest = json.loads(Path(pair["source_measurement"]).read_text(encoding="utf-8"))
    Path(manifest["window"]["frames"][0]["locator"]).write_bytes(b"corrupted")
    class NoNetwork:
        def probe(self, url):
            raise AssertionError("Media integrity must be checked before probing")
    output = run(NoNetwork(), store, state, prepared, audio_root)
    assert output["attempted"] == 0
    assert output["counts"] == {"BLOCKED_MEASUREMENT_INTEGRITY": 1}


def test_holdout_learning_collision_blocks_network_and_correspondence(tmp_path):
    _, store, state, prepared, audio_root = fixture(tmp_path)
    upsert_human_clip_examples(store, [
        trusted_example("leak", stream_id="stream-evaluation"),
    ])
    class NoNetwork:
        def probe(self, url):
            raise AssertionError("Learning contamination should stop before probing")
    output = run(NoNetwork(), store, state, prepared, audio_root)
    assert output["attempted"] == 0
    assert output["counts"] == {"BLOCKED_SPLIT_CONFLICT": 1}


def test_revoked_range_and_tampered_source_clock_prevent_links(tmp_path):
    provider, store, state, prepared, audio_root = fixture(tmp_path)
    rule = ClipPolicyRule(
        id="revoked-source-range", decision=PolicyDecision.BLOCK,
        source_id="stream-evaluation", start=45, end=47, reason="revoked",
    )
    denied = run(provider, store, state, prepared, audio_root, policy_rules=[rule])
    assert denied["counts"] == {"QUARANTINED_POLICY": 1}

    p = prepared / "source_clocks" / "holdout-source-clock.json"
    clock_report = json.loads(p.read_text(encoding="utf-8"))
    next(iter(clock_report["records"].values()))["fingerprint"] = "bad"
    p.write_text(json.dumps(clock_report), encoding="utf-8")
    class NoNetwork:
        def probe(self, url):
            raise AssertionError("Tampered source clock is stopped before network")
    invalid = run(NoNetwork(), store, state, prepared, audio_root)
    assert invalid["counts"] == {"BLOCKED_MEASUREMENT_INTEGRITY": 1}


def test_unproven_audio_clock_retains_unknown_status_without_guessing(tmp_path):
    provider, store, state, prepared, audio_root = fixture(tmp_path)
    p = prepared / "source_clocks" / "holdout-source-clock.json"
    clock_report = json.loads(p.read_text(encoding="utf-8"))
    item = next(iter(clock_report["records"].values()))["section_clocks"][0]
    item["status"] = "UNKNOWN_NONUNIQUE_SOURCE_AUDIO"
    item["origin_seconds"] = None
    item["frame_pts_verified"] = False
    p.write_text(json.dumps(clock_report), encoding="utf-8")
    result = run(provider, store, state, prepared, audio_root)
    assert result["counts"] == {"MEASURED_UNREVIEWED_FRAME_LINKS": 1}
    report = json.loads(
        (prepared / "frame_links" / "holdout-frame-links.json").read_text(encoding="utf-8")
    )
    obs = next(iter(report["records"].values()))["group_observations"][0]
    assert obs["status"] == "UNKNOWN_CLOCK"
    assert not obs["frame_links"]
