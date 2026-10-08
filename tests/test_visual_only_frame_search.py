"""No-audio visual frame search: sparse sequences, ambiguity, budgets and no gold."""
from types import SimpleNamespace

import pytest

from holoclip.visual_only_frame_search import (
    MAX_PEAKS, VERSION, search_source_visual_sequences,
)


def frame(name, pts):
    return SimpleNamespace(id=name, locator=name, pts_seconds=float(pts),
                           checksum="sha-"+name)


def survey(intervals=None):
    intervals = [[4.8, 5.2]] if intervals is None else intervals
    return {
        "version": "bounded-output-only-visual-survey-v1",
        "status": "MEASURED_UNREVIEWED_OUTPUT_ONLY_VISUAL_SIGNALS",
        "observed_peaks": [{
            "time_bracket_seconds": span,
            "relation": "NO_ACOUSTIC_CANDIDATE_AT_SAMPLED_TIME",
            "verified_edit_cut": False,
            "source_scene_change_ruled_out": False,
        } for span in intervals],
        "verified_edit_cut": False, "frame_accurate_cuts_verified": False,
        "reviewed_gold_available": False, "qualification_allowed": False,
    }


def fixtures(offset=0., *, repeat=False, two_groups=False):
    source = [frame("s10", 10), frame("s11", 11),
              frame("s15", 15), frame("s16", 16)]
    finished = [frame("f3", 3), frame("f4", 4),
                frame("f6", 6), frame("f7", 7)]
    windows = {
        "src0": SimpleNamespace(frames=source, source_start=0.),
        "dst0": SimpleNamespace(frames=finished, source_start=0.),
    }
    sections = [{
        "group": 0, "source_measurement": "src0", "finished_measurement": "dst0",
    }]
    if two_groups:
        windows["src1"] = SimpleNamespace(
            frames=[frame("r10", 10), frame("r11", 11)],
            source_start=0.,
        )
        windows["dst1"] = SimpleNamespace(frames=[], source_start=0.)
        sections.append({
            "group": 1, "source_measurement": "src1", "finished_measurement": "dst1",
        })
    matches = {
        ("s10", "f3"): .8, ("s11", "f4"): .8,
        ("s15", "f6"): .8, ("s16", "f7"): .8,
    }
    if offset:
        # Move right image sequence to a nearby source point: continuity.
        source[2].pts_seconds = 13.
        source[3].pts_seconds = 14.
    if two_groups:
        for k in [("s15", "f6"), ("s16", "f7")]:
            del matches[k]
        matches[("r10", "f6")] = .8
        matches[("r11", "f7")] = .8
    if repeat:
        # A similarly matching frame 4s away must defeat uniqueness.
        matches[("s15", "f3")] = .78
    def measure(left, right):
        score = matches.get((left, right), 0.)
        return {"score": score} if score else None
    return sections, windows.get, measure


def run(sections, loader, compare, *, input_survey=None):
    return search_source_visual_sequences(
        input_survey or survey(), sections,
        load_window=loader, extract=lambda locator: locator, compare=compare,
    )


def test_two_sided_image_trajectory_finds_visual_skip_without_audio_clock():
    sections, loader, compare = fixtures()
    found = run(sections, loader, compare)
    assert found["version"] == VERSION
    assert found["status"] == "MEASURED_UNREVIEWED_VISUAL_SEQUENCE_SEARCH"
    assert found["visual_source_skip_hypotheses"] == 1
    assert found["visual_source_group_switch_hypotheses"] == 0
    assert found["unknown_visual_correspondences"] == 0
    assert found["tested_image_comparisons"] == 16
    assert found["peak_observations"][0]["status"] == (
        "VISUAL_ONLY_SOURCE_SKIP_HYPOTHESIS"
    )
    assert found["peak_observations"][0]["sides"][0][
        "source_local_samples_seconds"
    ] == [10., 11.]
    for flag in (
        "verified_edit_cut", "source_scene_change_ruled_out",
        "frame_accurate_cuts_verified", "original_video_absolute_pts_verified",
        "dense_output_source_map_verified", "reviewed_gold_available",
        "qualification_allowed",
    ):
        assert found[flag] is False


def test_visual_continuity_and_source_group_switch_are_separate_hypotheses():
    sections, loader, compare = fixtures(offset=1.)
    same = run(sections, loader, compare)
    assert same["visual_source_continuity_hypotheses"] == 1
    assert same["visual_source_skip_hypotheses"] == 0

    sections, loader, compare = fixtures(two_groups=True)
    different = run(sections, loader, compare)
    assert different["visual_source_group_switch_hypotheses"] == 1
    assert different["verified_edit_cut"] is False


def test_repeated_frames_reject_ambiguous_source_match():
    sections, loader, compare = fixtures(repeat=True)
    observed = run(sections, loader, compare)
    assert observed["unknown_visual_correspondences"] == 1
    assert observed["visual_source_skip_hypotheses"] == 0
    assert observed["peak_observations"][0]["status"] == (
        "UNKNOWN_VISUAL_MATCH_NOT_UNIQUE"
    )



def test_static_original_or_finished_frames_do_not_establish_visual_trajectory():
    sections, windows, compare = fixtures()
    # The two times contain exactly the same bytes, even though their IDs
    # and display timestamps differ.
    windows("src0").frames[0].checksum = "same-content"
    windows("src0").frames[1].checksum = "same-content"
    static_source = run(sections, windows, compare)
    assert static_source["unknown_visual_correspondences"] == 1
    assert static_source["visual_source_skip_hypotheses"] == 0

    sections, windows, compare = fixtures()
    windows("dst0").frames[0].checksum = "same-output"
    windows("dst0").frames[1].checksum = "same-output"
    static_output = run(sections, windows, compare)
    assert static_output["unknown_visual_correspondences"] == 1
    assert static_output["visual_source_skip_hypotheses"] == 0


def test_no_both_side_frames_returns_unknown_not_zero_edits():
    sections, loader, compare = fixtures()
    report = run(sections, loader, compare, input_survey=survey([[.5, 1.]]))
    assert report["unknown_visual_correspondences"] == 1
    assert report["peak_observations"][0]["status"] == (
        "UNKNOWN_NO_TWO_SIDED_FINISHED_FRAMES"
    )


def test_budget_exhaustion_preempts_image_load_and_partial_hypotheses():
    sections, loader, compare = fixtures()
    def forbidden(path):
        raise AssertionError("Should fail on peak budget before reading frames")
    spans = [[2.*i+1., 2.*i+1.2] for i in range(MAX_PEAKS + 1)]
    blocked = search_source_visual_sequences(
        survey(spans), sections, load_window=forbidden,
    )
    assert blocked["status"] == "UNKNOWN_VISUAL_SEARCH_PEAK_BUDGET"
    assert blocked["visual_source_skip_hypotheses"] == 0
    assert blocked["tested_image_comparisons"] == 0
    assert blocked["unknown_visual_correspondences"] == MAX_PEAKS + 1


def test_invalid_survey_or_promoted_peak_not_accepted():
    sections, loader, compare = fixtures()
    changed = survey()
    changed["observed_peaks"][0]["verified_edit_cut"] = True
    with pytest.raises(ValueError):
        run(sections, loader, compare, input_survey=changed)
    absent = survey()
    absent["status"] = "UNKNOWN_BOUNDED_VISUAL_DECODE"
    unknown = run(sections, loader, compare, input_survey=absent)
    assert unknown["status"] == "UNKNOWN_NO_MEASURED_OUTPUT_SURVEY"
    assert unknown["visual_source_skip_hypotheses"] == 0


def test_actual_orb_image_correspondence_across_muted_visual_edit(tmp_path):
    """No acoustic offset supplied; actual image descriptors find source islands."""
    import hashlib
    import shutil
    import cv2
    import numpy as np

    source_times = [10., 11., 15., 16.]
    output_times = [3., 4., 6., 7.]
    originals, finished = [], []
    for i, (s_time, f_time) in enumerate(zip(source_times, output_times)):
        image = np.random.default_rng(230 + i).integers(
            0, 256, (360, 640), dtype=np.uint8,
        )
        spath = tmp_path / f"src{i}.png"
        opath = tmp_path / f"clip{i}.png"
        assert cv2.imwrite(str(spath), image)
        shutil.copyfile(spath, opath)
        originals.append(SimpleNamespace(
            id=f"source-{i}", locator=str(spath), pts_seconds=s_time,
            checksum=hashlib.sha256(spath.read_bytes()).hexdigest(),
        ))
        finished.append(SimpleNamespace(
            id=f"output-{i}", locator=str(opath), pts_seconds=f_time,
            checksum=hashlib.sha256(opath.read_bytes()).hexdigest(),
        ))
    windows = {
        "source": SimpleNamespace(frames=originals, source_start=0.),
        "finished": SimpleNamespace(frames=finished, source_start=0.),
    }
    result = search_source_visual_sequences(
        survey(), [{
            "group": 0, "source_measurement": "source",
            "finished_measurement": "finished",
        }], load_window=windows.__getitem__,
    )
    assert result["visual_source_skip_hypotheses"] == 1
    assert result["peak_observations"][0]["status"] == (
        "VISUAL_ONLY_SOURCE_SKIP_HYPOTHESIS"
    )
    assert result["verified_edit_cut"] is False
