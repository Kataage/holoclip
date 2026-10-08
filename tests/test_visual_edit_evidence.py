"""Synthetic pixel geometry and adversarial holdout-integrity tests."""
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from holoclip.clip_policy import ClipPolicyRule, PolicyDecision
from holoclip.examples import upsert_human_clip_examples
from holoclip.holdout_visual_pipeline import inspect_reserved_visual_edits
from holoclip.video_measurement import checksum_file
from holoclip.video_semantics import MeasuredFrame, MeasuredVideoWindow
from holoclip.visual_edit_evidence import inspect_visual_pair
from test_bounded_source_evidence import (
    SectionProvider, _execute, _source_evidence,
)
from test_editorial_profile import trusted_example


def texture(seed=13):
    rng = np.random.default_rng(seed)
    image = rng.integers(0, 256, size=(360, 640), dtype=np.uint8)
    # Large structured detail in addition to small texture
    for k in range(30):
        x, y = int(rng.integers(15, 625)), int(rng.integers(15, 345))
        cv2.circle(image, (x, y), int(rng.integers(4, 14)), 200, 2)
    return image


def transformed(image, zoom=1.0, x=0., y=0.):
    h, w = image.shape
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), 0, zoom)
    matrix[0, 2] += x
    matrix[1, 2] += y
    return cv2.warpAffine(image, matrix, (w, h), borderMode=cv2.BORDER_REFLECT)


def window(tmp_path, name, images):
    frames = []
    for i, (pts, image) in enumerate(images):
        path = tmp_path / f"{name}-{i}.png"
        cv2.imwrite(str(path), image)
        frames.append(MeasuredFrame(
            id=f"{name}-f{i}", shot_id=f"{name}-s{i}", pts_seconds=pts,
            locator=str(path), checksum=checksum_file(path),
        ))
    return MeasuredVideoWindow(
        id=name, source_asset_id=name, source_start=0, source_end=20,
        video_path=str(tmp_path / f"{name}.mp4"),
        media_sha256="not-produced-here", frames=tuple(frames),
        contact_sheet_paths=(), extraction_version="fixture",
    )


@pytest.mark.parametrize("zoom,x,y,kind", [
    (1.0, 0, 0, "identity_like"),
    (1.22, 0, 0, "zoom_candidate"),
    (1.0, 95, 4, "reframe_candidate"),
    (1.24, 85, 0, "zoom_reframe_candidate"),
])
def test_measured_orb_geometry_responds_to_actual_transform(tmp_path, zoom, x, y, kind):
    image = texture()
    src = window(tmp_path, "source", [(1., image)])
    dst = window(tmp_path, "finished", [(1., transformed(image, zoom, x, y))])
    result = inspect_visual_pair(src, dst)
    assert result["status"] == "SPARSE_VISUAL_CORRESPONDENCE"
    obs = result["observations"][0]
    assert obs["kind"] == kind
    assert obs["source_frame_id"] == "source-f0"
    assert obs["finished_frame_id"] == "finished-f0"
    assert abs(obs["zoom_factor_candidate"] - zoom) < .09
    assert obs["orb_inliers"] >= 12
    assert obs["reprojection_rmse_px"] <= 3.5
    assert obs["temporal_uniqueness_tested"] is False
    assert result["unknowns"]


def test_no_visual_match_and_ambiguous_repeated_frame_are_unknown(tmp_path):
    image = texture()
    unrelated = texture(seed=78)
    source = window(tmp_path, "source", [(1, image)])
    finished = window(tmp_path, "finished", [(1, unrelated)])
    assert inspect_visual_pair(source, finished)["status"] == "UNKNOWN_VISUAL_MATCH"
    repeated = window(tmp_path, "repeat", [(1., image), (3., image)])
    assert inspect_visual_pair(repeated, window(tmp_path, "one", [(1., image)]))[
        "status"
    ] == "UNKNOWN_VISUAL_MATCH"


def test_sparse_visual_budgets_do_not_silently_drop_frames(tmp_path):
    image = texture()
    source = window(tmp_path, "source", [(float(i), image) for i in range(5)])
    finished = window(tmp_path, "finished", [(1, image)])
    assert inspect_visual_pair(source, finished, max_frames=3)["status"] == "UNKNOWN_FRAME_BUDGET"
    with pytest.raises(ValueError):
        inspect_visual_pair(source, finished, max_frames=100)


def pair_fixture(tmp_path):
    underlying, store, state, prepared, audio_root = _source_evidence(tmp_path)
    provider = SectionProvider(underlying)
    assert _execute(provider, store, state, prepared, audio_root)["counts"] == {
        "MEASURED_UNREVIEWED_SOURCE_PAIRS": 1
    }
    record_path = prepared / "source_pairs" / "holdout-source-pairs.json"
    raw = json.loads(record_path.read_text(encoding="utf-8"))
    pair = next(iter(raw["records"].values()))["pairs"][0]
    image = texture()
    for label, filename, data in (
        ("source", pair["source_measurement"], image),
        ("finished", pair["finished_measurement"], transformed(image, zoom=1.24)),
    ):
        manifest_path = Path(filename)
        measurement = json.loads(manifest_path.read_text(encoding="utf-8"))
        frame = measurement["window"]["frames"][0]
        frame["id"] = f"{label}-f0"
        frame["shot_id"] = f"{label}-s0"
        assert cv2.imwrite(frame["locator"], data)
        frame["checksum"] = checksum_file(Path(frame["locator"]))
        manifest_path.write_text(json.dumps(measurement), encoding="utf-8")
    return provider, store, state, prepared, audio_root


def run_visual(provider, store, state, prepared, audio_root, **kwargs):
    return inspect_reserved_visual_edits(
        provider, store=store, state_path=state,
        prepared_root=prepared, audio_root=audio_root,
        pairs_root=prepared / "source_pairs",
        output=prepared / "visual_edits", policy_rules=kwargs.pop("policy_rules", []),
        max_clips=2, **kwargs,
    )


def test_unattended_visual_observations_and_hash_bound_resume(tmp_path):
    provider, store, state, prepared, audio_root = pair_fixture(tmp_path)
    first = run_visual(provider, store, state, prepared, audio_root)
    assert first["counts"] == {"OBSERVED_UNREVIEWED_VISUAL_GEOMETRY": 1}
    report_path = prepared / "visual_edits" / "holdout-visual-observations.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["qualification_allowed"] is False
    assert report["event_split_verified"] is False
    assert report["benchmark_held_out"] is False
    assert report["reviewed_gold_available"] is False
    assert report["frame_accurate_cuts_verified"] is False
    obs = next(iter(report["records"].values()))["pair_observations"][0]
    assert obs["status"] == "SPARSE_VISUAL_CORRESPONDENCE"
    assert obs["observations"][0]["kind"] == "zoom_candidate"
    assert obs["absolute_seek_verified"] is False
    assert run_visual(provider, store, state, prepared, audio_root)["counts"] == {
        "OBSERVED_UNREVIEWED_VISUAL_GEOMETRY": 1
    }
    # Revalidation catches a changed frame without trusting prior status.
    source_path = Path(next(iter(
        json.loads((prepared / "source_pairs" / "holdout-source-pairs.json")
                   .read_text(encoding="utf-8"))["records"].values()
    ))["pairs"][0]["source_measurement"])
    measurement = json.loads(source_path.read_text(encoding="utf-8"))
    Path(measurement["window"]["frames"][0]["locator"]).write_bytes(b"corrupt")
    invalid = run_visual(provider, store, state, prepared, audio_root)
    assert invalid["counts"] == {"BLOCKED_MEASUREMENT_INTEGRITY": 1}


def test_split_conflict_blocks_before_metadata_probe(tmp_path):
    _, store, state, prepared, audio_root = pair_fixture(tmp_path)
    upsert_human_clip_examples(store, [trusted_example("leaked", stream_id="stream-evaluation")])

    class NoNetwork:
        def probe(self, url):
            raise AssertionError("No metadata probes for leaked original stream")

    result = run_visual(NoNetwork(), store, state, prepared, audio_root)
    assert result["attempted"] == 0
    assert result["counts"] == {"BLOCKED_SPLIT_CONFLICT": 1}


def test_range_policy_block_before_image_interpretation(tmp_path):
    provider, store, state, prepared, audio_root = pair_fixture(tmp_path)
    forbidden = ClipPolicyRule(
        id="range", decision=PolicyDecision.BLOCK,
        source_id="stream-evaluation", start=44, end=50, reason="restricted",
    )
    def must_not_inspect(*args, **kwargs):
        raise AssertionError("Policy was supposed to quarantine this source range")

    result = run_visual(
        provider, store, state, prepared, audio_root,
        policy_rules=[forbidden], inspect=must_not_inspect,
    )
    assert result["counts"] == {"QUARANTINED_POLICY": 1}
