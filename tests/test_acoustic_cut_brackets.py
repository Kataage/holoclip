"""Bounded acoustic cut evidence: true discontinuities, silence and safe resumption."""
import json
from pathlib import Path

import numpy as np
import pytest

from holoclip.acoustic_cut_brackets import refine_acoustic_cut_brackets
from holoclip.examples import upsert_human_clip_examples
from holoclip.holdout_cut_pipeline import refine_reserved_cut_candidates
from test_editorial_profile import trusted_example
from test_visual_edit_evidence import pair_fixture, run_visual


def _features(count: int, seed: int = 23):
    rng = np.random.default_rng(seed)
    values = rng.normal(size=(count, 12))
    return (values / np.linalg.norm(values, axis=1, keepdims=True)).astype("float32")


def _segment(clip_start: float, duration: float, source_start: float) -> dict:
    return {"clip_start": clip_start, "clip_end": clip_start + duration,
            "source_start": source_start, "source_end": source_start + duration}


def test_continuous_verified_audio_has_no_false_cut():
    source = _features(1800)
    clip = source[200:440].copy()
    result = refine_acoustic_cut_brackets(
        clip, source, [_segment(0., 60., 50.)],
    )
    assert result["status"] == "AUDIO_SUPPORTED_INTERVALS"
    assert len(result["points"]) >= 30
    assert result["transitions"] == []
    assert set(point["offset_seconds"] for point in result["points"]) == {50.}
    assert result["resolution_seconds"] == .25
    assert result["unknowns"]


def test_two_edits_bracket_a_real_jump_without_inventing_exact_frame():
    source = _features(1500)
    clip = np.concatenate((source[200:280], source[800:880]), axis=0)
    result = refine_acoustic_cut_brackets(
        clip, source, [_segment(0., 20., 50.), _segment(20., 20., 200.)]
    )
    assert result["status"] == "AUDIO_SUPPORTED_INTERVALS"
    changes = [row for row in result["transitions"]
               if row["status"] == "CUT_INTERVAL_CANDIDATE"]
    assert len(changes) == 1
    assert changes[0]["interval"][0] <= 20 <= changes[0]["interval"][1]
    assert changes[0]["interval"][0] < changes[0]["interval"][1]
    assert changes[0]["offset_change_seconds"] == 130.
    assert changes[0]["basis"] == "nonidentical_unique_acoustic_offsets"


def test_long_unsupported_gap_cannot_be_called_a_proven_cut():
    source = _features(2000)
    unrelated = _features(160, seed=91)
    clip = np.concatenate((source[80:160], unrelated, source[700:780]), axis=0)
    ranges = [_segment(0., 20., 20.), _segment(60., 20., 175.)]
    result = refine_acoustic_cut_brackets(clip, source, ranges)
    assert any(t["status"] == "UNKNOWN_WIDE_GAP" for t in result["transitions"])
    assert not any(t["status"] == "CUT_INTERVAL_CANDIDATE" for t in result["transitions"])


def test_zero_audio_and_unmatched_audio_are_unknown():
    source = _features(700)
    empty = np.zeros((120, 12), dtype=np.float32)
    row = refine_acoustic_cut_brackets(empty, source, [_segment(0, 30, 10)])
    assert row["status"] == "UNKNOWN_NO_UNIQUE_POINTS"
    assert row["transitions"] == []
    mismatch = refine_acoustic_cut_brackets(
        _features(120, seed=567), source, [_segment(0, 30, 10)])
    assert mismatch["status"] == "UNKNOWN_NO_UNIQUE_POINTS"


def test_sampling_budget_and_invented_audio_offsets_fail_closed():
    source = _features(1800)
    clip = source[200:560].copy()
    capped = refine_acoustic_cut_brackets(
        clip, source, [_segment(0., 90., 50.)], max_points=10,
    )
    assert capped["status"] == "UNKNOWN_SAMPLING_BUDGET"
    assert capped["points"] == []
    assert capped["transitions"] == []
    with pytest.raises(ValueError):
        refine_acoustic_cut_brackets(
            clip, source, [_segment(0., 90., 800.)],
        )
    with pytest.raises(ValueError):
        refine_acoustic_cut_brackets(
            clip, source, [_segment(0., 90., 50.)], sample_step_seconds=.01,
        )



def test_bounded_adaptive_sampling_preserves_all_three_anchored_regions():
    source = _features(2100)
    clip = np.concatenate((
        source[80:200], source[640:760], source[1000:1120],
    ), axis=0)
    ranges = [
        _segment(0., 30., 20.),
        _segment(30., 30., 160.),
        _segment(60., 30., 250.),
    ]
    # 1s or 1.5s sampling exceeds 50 starts. A complete 2s plan fits.
    result = refine_acoustic_cut_brackets(
        clip, source, ranges, max_points=50,
    )
    assert result["version"] == "bounded-acoustic-cut-brackets-v2"
    assert result["status"] == "AUDIO_SUPPORTED_INTERVALS"
    assert result["sampling_strategy"] == "FULL_COVERAGE_BUDGET_ADAPTED"
    assert result["sample_spacing_seconds"] == 2.
    assert result["tested_points"] == result["planned_points"]
    assert result["tested_points"] <= 50
    assert {point["group"] for point in result["points"]} == {0, 1, 2}
    assert sorted(set(point["offset_seconds"] for point in result["points"])) == [
        20., 130., 190.,
    ]
    candidate = [t for t in result["transitions"]
                 if t["status"] == "CUT_INTERVAL_CANDIDATE"]
    assert len(candidate) == 2
    for expected in (30., 60.):
        assert any(a <= expected <= b for a, b in
                   (x["interval"] for x in candidate))
    assert not any(x["status"] == "UNKNOWN_WIDE_GAP" for x in result["transitions"])


def test_adaptive_1_5_second_spacing_when_needed():
    source = _features(1600)
    clip = source[120:440].copy()
    result = refine_acoustic_cut_brackets(
        clip, source, [_segment(0., 80., 30.)], max_points=60,
    )
    assert result["status"] == "AUDIO_SUPPORTED_INTERVALS"
    assert result["sample_spacing_seconds"] == 1.5
    assert result["tested_points"] <= 60
    assert result["transitions"] == []


def test_unsatisfiable_full_span_fails_before_partial_sample_claim():
    source = _features(2100)
    clip = np.concatenate((
        source[80:200], source[640:760], source[1000:1120],
    ), axis=0)
    ranges = [
        _segment(0., 30., 20.),
        _segment(30., 30., 160.),
        _segment(60., 30., 250.),
    ]
    blocked = refine_acoustic_cut_brackets(
        clip, source, ranges, max_points=44,
    )
    assert blocked["status"] == "UNKNOWN_SAMPLING_BUDGET"
    assert blocked["points"] == []
    assert blocked["transitions"] == []
    assert blocked["tested_points"] == 0
    assert blocked["sampling_strategy"] == "FULL_COVERAGE_OR_UNKNOWN"


def test_every_section_tail_is_sampled_even_with_stride_remainder():
    source = _features(1700)
    # 25s is not divisible into 2s starts after excluding a 2s window.
    clip = np.concatenate((source[80:180], source[600:700]), axis=0)
    result = refine_acoustic_cut_brackets(
        clip, source, [_segment(0., 25., 20.),
                       _segment(25., 25., 125.)],
        max_points=26,
    )
    assert result["status"] == "AUDIO_SUPPORTED_INTERVALS"
    assert result["sample_spacing_seconds"] == 2.
    assert result["planned_points"] == result["tested_points"] <= 26
    assert any(p["clip_start"] == 23. for p in result["points"])
    assert any(p["clip_start"] == 48. for p in result["points"])



def _setup(tmp_path):
    provider, store, state, prepared, audio_root = pair_fixture(tmp_path)
    result = run_visual(provider, store, state, prepared, audio_root)
    assert result["counts"] == {"OBSERVED_UNREVIEWED_VISUAL_GEOMETRY": 1}
    return provider, store, state, prepared, audio_root


def _run(provider, store, state, prepared, audio_root, **kwargs):
    return refine_reserved_cut_candidates(
        provider, store=store, state_path=state, prepared_root=prepared,
        audio_root=audio_root, pairs_root=prepared / "source_pairs",
        visual_root=prepared / "visual_edits", output=prepared / "cut_brackets",
        policy_rules=kwargs.pop("policy_rules", []), max_clips=2, **kwargs,
    )


def _stub_fingerprint(clip, source):
    def read(path, cache_root):
        if "source_audio" in Path(path).parts:
            return source
        return clip
    return read


def test_unattended_refinement_records_unreviewed_intervals_and_reuses(tmp_path):
    provider, store, state, prepared, audio_root = _setup(tmp_path)
    source = _features(800)
    clip = source[160:360].copy()
    seen = []

    def measured(clip_features, source_features, ranges, *, max_points):
        seen.append((len(clip_features), len(source_features), max_points))
        return refine_acoustic_cut_brackets(
            clip_features, source_features, ranges, max_points=max_points,
        )

    first = _run(
        provider, store, state, prepared, audio_root,
        fingerprint=_stub_fingerprint(clip, source), refine=measured,
    )
    assert first["counts"] == {"MEASURED_UNREVIEWED_CUT_BRACKETS": 1}
    assert seen
    path = prepared / "cut_brackets" / "holdout-cut-brackets.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["qualification_allowed"] is False
    assert report["frame_accurate_cuts_verified"] is False
    assert report["benchmark_held_out"] is False
    assert report["reviewed_gold_available"] is False
    assert report["event_split_verified"] is False
    row = next(iter(report["records"].values()))
    assert row["status"] == "MEASURED_UNREVIEWED_CUT_BRACKETS"
    assert row["source_absolute_seek_verified"] is False
    assert row["visual_cut_boundary_confirmed"] is False
    assert row["refinement"]["status"] == "AUDIO_SUPPORTED_INTERVALS"
    second = _run(
        provider, store, state, prepared, audio_root,
        fingerprint=_stub_fingerprint(clip, source), refine=measured,
    )
    assert second["counts"] == first["counts"]
    assert len(seen) == 1


def test_learning_collision_blocks_all_metadata_probes(tmp_path):
    _, store, state, prepared, audio_root = _setup(tmp_path)
    upsert_human_clip_examples(
        store, [trusted_example("leak", stream_id="stream-evaluation")],
    )

    class NoNetwork:
        def probe(self, url):
            raise AssertionError("Do not probe when reserved stream is learning")

    result = _run(NoNetwork(), store, state, prepared, audio_root)
    assert result["attempted"] == 0
    assert result["counts"] == {"BLOCKED_SPLIT_CONFLICT": 1}


def test_corrupt_measured_frame_blocks_without_new_network(tmp_path):
    _, store, state, prepared, audio_root = _setup(tmp_path)
    payload = json.loads(
        (prepared / "source_pairs" / "holdout-source-pairs.json").read_text(encoding="utf-8")
    )
    original = next(iter(payload["records"].values()))["pairs"][0]
    measured = json.loads(Path(original["source_measurement"]).read_text(encoding="utf-8"))
    Path(measured["window"]["frames"][0]["locator"]).write_bytes(b"broken-frame")

    class NoNetwork:
        def probe(self, url):
            raise AssertionError("Bad artifact must be stopped before probing")

    result = _run(NoNetwork(), store, state, prepared, audio_root)
    assert result["attempted"] == 0
    assert result["counts"] == {"BLOCKED_MEASUREMENT_INTEGRITY": 1}


def test_policy_change_blocks_refinement_without_ai(tmp_path):
    provider, store, state, prepared, audio_root = _setup(tmp_path)
    provider.policy_block = True
    result = _run(provider, store, state, prepared, audio_root)
    assert result["counts"] == {"QUARANTINED_POLICY": 1}


def test_rejected_visual_report_cannot_create_cut_brackets(tmp_path):
    provider, store, state, prepared, audio_root = _setup(tmp_path)
    path = prepared / "visual_edits" / "holdout-visual-observations.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["qualification_allowed"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        _run(provider, store, state, prepared, audio_root)


def test_missing_original_video_span_does_not_inherit_visual_provenance(tmp_path):
    provider, store, state, prepared, audio_root = _setup(tmp_path)
    path = prepared / "source_pairs" / "holdout-source-pairs.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    next(iter(payload["records"].values()))["pairs"][0]["source_start"] = 45.
    path.write_text(json.dumps(payload), encoding="utf-8")

    class NoNetwork:
        def probe(self, url):
            raise AssertionError("Uncovered source range must block before probing")

    result = _run(NoNetwork(), store, state, prepared, audio_root)
    assert result["attempted"] == 0
    assert result["counts"] == {"BLOCKED_AUDIO_VISUAL_RANGE_MISMATCH": 1}


def test_untrusted_visual_method_does_not_generate_cut_confidence(tmp_path):
    provider, store, state, prepared, audio_root = _setup(tmp_path)
    path = prepared / "visual_edits" / "holdout-visual-observations.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    next(iter(payload["records"].values()))["method_version"] = "unverified-model"
    path.write_text(json.dumps(payload), encoding="utf-8")

    class NoNetwork:
        def probe(self, url):
            raise AssertionError("Mismatched visual evidence must block before probing")

    result = _run(NoNetwork(), store, state, prepared, audio_root)
    assert result["attempted"] == 0
    assert result["counts"] == {"BLOCKED_VISUAL_EVIDENCE_UNVERIFIED": 1}
