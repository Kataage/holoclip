"""Offline form aggregation stays unqualified, rejects stale media and fake input."""
from __future__ import annotations

import copy
import json
from contextlib import nullcontext

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_b2_b3_blind_full_study as study
from holoclip import render_b2_b3_blind_rating_ingest as ingest
from test_render_b2_b3_blind_full_study import _fixture


def _environment(tmp_path, monkeypatch):
    a, b, state, evidence = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(ingest, "_exclusive_run_lock", lambda *a, **k: nullcontext())
    report = study.run_blind_full_study(
        preview_root=tmp_path, execute=True, approve_local_preview=True,
    )
    root = tmp_path / "original.review-00.playback.blind-study"
    mapping = tmp_path / "original.review-00.playback.blind-map.json"
    form = {
        "version": ingest.RATING_VERSION,
        "study_id": report["study_id"],
        "claimed_human_rated": True,
        "ratings": {
            "candidate-1": {
                "speech_naturalness": 5,
                "narrative_continuity": 4,
                "payoff_completion": 3,
            },
            "candidate-2": {
                "speech_naturalness": 2,
                "narrative_continuity": 3,
                "payoff_completion": 4,
            },
        },
        "preference": "candidate-1",
        "independent_human_gold_verified": False,
        "automatic_adoption_allowed": False,
    }
    rating = tmp_path / "rating.json"
    rating.write_text(json.dumps(form, ensure_ascii=False), encoding="utf-8")
    return a, b, root, mapping, form, rating


def test_real_study_no_labels_abstains_without_writing(tmp_path, monkeypatch):
    _environment(tmp_path, monkeypatch)
    result = ingest.ingest_blind_ratings(rating_paths=[], preview_root=tmp_path)
    assert result["status"] == "BLIND_RATING_NO_SUBMISSIONS"
    assert result["independent_human_gold_verified"] is False
    assert result["ready"] is False
    assert "dimension_summaries_by_anonymous_slot" not in result
    assert len(list(tmp_path.glob("*.json"))) == 2  # private map and rating only


def test_one_rating_reports_anonymous_unverified_scores(tmp_path, monkeypatch):
    a, b, root, mapping, form, rating = _environment(tmp_path, monkeypatch)
    result = ingest.ingest_blind_ratings(
        rating_paths=[rating], preview_root=tmp_path,
    )
    assert result["status"] == "BLIND_RATING_SUBMISSIONS_UNVERIFIED_AGGREGATED"
    assert result["submitted_rating_file_count"] == 1
    stats = result["dimension_summaries_by_anonymous_slot"]
    assert stats["speech_naturalness"]["candidate-1"]["unverified_submitted_mean"] == 5
    assert stats["speech_naturalness"]["candidate-2"]["unverified_submitted_mean"] == 2
    assert result["unverified_preference_counts"]["candidate-1"] == 1
    assert result["distinct_reviewers_confirmed"] is False
    assert result["viewed_complete_videos_verified"] is False
    assert result["automatic_winner"] is None
    assert result["automatic_adoption_allowed"] is False
    assert result["ready"] is False
    assert result["source_diverse_holdout_validated"] is False
    assert "b2-variant" not in json.dumps(result)
    assert "b3-variant" not in json.dumps(result)
    assert json.loads(rating.read_text(encoding="utf-8")) == form
    assert set(json.loads(mapping.read_text(encoding="utf-8"))["role_mapping"]) == {
        "candidate-1", "candidate-2",
    }


def test_two_submissions_aggregate_but_not_independent_gold(tmp_path, monkeypatch):
    _, _, _, _, form, rating = _environment(tmp_path, monkeypatch)
    second = copy.deepcopy(form)
    second["ratings"]["candidate-1"]["speech_naturalness"] = 1
    second["preference"] = "candidate-2"
    other = tmp_path / "rating-2.json"
    other.write_text(json.dumps(second), encoding="utf-8")
    result = ingest.ingest_blind_ratings(
        rating_paths=[rating, other], preview_root=tmp_path,
    )
    stats = result["dimension_summaries_by_anonymous_slot"]
    assert stats["speech_naturalness"]["candidate-1"]["unverified_submitted_mean"] == 3
    assert result["unverified_preference_counts"]["candidate-1"] == 1
    assert result["unverified_preference_counts"]["candidate-2"] == 1
    assert result["human_agreement_measured"] is False
    assert result["distinct_reviewers_confirmed"] is False
    assert result["automatic_winner"] is None


@pytest.mark.parametrize("change", [
    lambda f: f["ratings"]["candidate-1"].update(speech_naturalness=0),
    lambda f: f["ratings"]["candidate-1"].update(speech_naturalness=6),
    lambda f: f["ratings"]["candidate-1"].update(speech_naturalness=4.5),
    lambda f: f["ratings"]["candidate-1"].update(speech_naturalness=True),
    lambda f: f["ratings"]["candidate-1"].update(payoff_completion=None),
    lambda f: f["ratings"]["candidate-2"].update(extra=5),
    lambda f: f["ratings"].update({"candidate-3": {}}),
    lambda f: f.update(study_id="wrong"),
    lambda f: f.update(independent_human_gold_verified=True),
    lambda f: f.update(automatic_adoption_allowed=True),
    lambda f: f.update(claimed_human_rated=False),
    lambda f: f.update(preference="b3"),
    lambda f: f.update(score=9),
])
def test_invalid_rating_form_fails_closed(tmp_path, monkeypatch, change):
    _, _, _, _, form, rating = _environment(tmp_path, monkeypatch)
    invalid = copy.deepcopy(form)
    change(invalid)
    rating.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(ValueError, match="BLIND_RATING"):
        ingest.ingest_blind_ratings(rating_paths=[rating], preview_root=tmp_path)


def test_duplicate_file_bytes_and_oversize_rejected(tmp_path, monkeypatch):
    _, _, _, _, _, rating = _environment(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="DUPLICATED_FORM_BYTES"):
        ingest.ingest_blind_ratings(
            rating_paths=[rating, rating], preview_root=tmp_path,
        )
    rating.write_text("x" * (ingest._MAX_FORM_BYTES + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="FILE_MISSING_UNSAFE"):
        ingest.ingest_blind_ratings(rating_paths=[rating], preview_root=tmp_path)


def test_changed_full_video_copy_and_mapping_blocks(tmp_path, monkeypatch):
    _, _, root, mapping, _, rating = _environment(tmp_path, monkeypatch)
    (root / "candidate-1.mp4").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="CANDIDATE_MEDIA_CHANGED"):
        ingest.ingest_blind_ratings(rating_paths=[rating], preview_root=tmp_path)

    # Restore by separate fresh fixture in another temporary directory.
    target = tmp_path / "different"
    target.mkdir()
    _, _, _, mapping2, _, rating2 = _environment(target, monkeypatch)
    changed = json.loads(mapping2.read_text(encoding="utf-8"))
    current_role = changed["role_mapping"]["candidate-1"]["role"]
    changed["role_mapping"]["candidate-1"]["role"] = (
        "b2" if current_role == "b3" else "b3"
    )
    mapping2.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="PRIVATE_MAPPING_CHANGED"):
        ingest.ingest_blind_ratings(rating_paths=[rating2], preview_root=target)


def test_source_media_and_review_html_tampering_blocks(tmp_path, monkeypatch):
    a, _, root, _, _, rating = _environment(tmp_path, monkeypatch)
    a.write_bytes(b"modified original")
    with pytest.raises(ValueError, match="FINISHED_MEDIA_CHANGED"):
        ingest.ingest_blind_ratings(rating_paths=[rating], preview_root=tmp_path)

    # On a separate unchanged study, a form substitution also blocks.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    _, _, root2, _, _, rating2 = _environment(fresh, monkeypatch)
    (root2 / "review.html").write_text("<script>changed</script>", encoding="utf-8")
    with pytest.raises(ValueError, match="REVIEW_FORM_CHANGED"):
        ingest.ingest_blind_ratings(rating_paths=[rating2], preview_root=fresh)


def test_create_only_aggregate_and_cli(tmp_path, monkeypatch):
    _, _, _, _, _, rating = _environment(tmp_path, monkeypatch)
    args = ["render-b2-b3-blind-rating-ingest",
            "--preview-root", str(tmp_path), "--rating", str(rating)]
    preview = CliRunner().invoke(cli.app, args)
    assert preview.exit_code == 0, preview.stdout
    assert json.loads(preview.stdout)["status"] == (
        "BLIND_RATING_SUBMISSIONS_UNVERIFIED_AGGREGATED")
    outfile = tmp_path / "summaries" / "review-summary.json"
    saved = CliRunner().invoke(cli.app, [*args, "--output", str(outfile)])
    assert saved.exit_code == 0, saved.stdout
    assert json.loads(outfile.read_text(encoding="utf-8")) == json.loads(saved.stdout)
    original = outfile.read_bytes()
    again = CliRunner().invoke(cli.app, [*args, "--output", str(outfile)])
    assert again.exit_code == 6
    assert outfile.read_bytes() == original
    assert json.loads(again.stdout)["ready"] is False


def test_symlink_submission_is_rejected(tmp_path, monkeypatch):
    _, _, _, _, _, rating = _environment(tmp_path, monkeypatch)
    linked = tmp_path / "linked.json"
    try:
        linked.symlink_to(rating)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises((ValueError, OSError)):
        ingest.ingest_blind_ratings(
            rating_paths=[linked], preview_root=tmp_path,
        )


def test_native_windows_crlf_form_is_accepted_without_weakening_content_check(
    tmp_path, monkeypatch,
):
    _, _, root, _, _, rating = _environment(tmp_path, monkeypatch)
    form = root / "review.html"
    html = form.read_bytes()
    normalized = html.replace(b"\r\n", b"\n")
    form.write_bytes(normalized.replace(b"\n", b"\r\n"))
    report = ingest.ingest_blind_ratings(
        rating_paths=[rating], preview_root=tmp_path,
    )
    assert report["status"] == "BLIND_RATING_SUBMISSIONS_UNVERIFIED_AGGREGATED"
    form.write_bytes(form.read_bytes() + b"<!-- unexpected edit -->")
    with pytest.raises(ValueError, match="REVIEW_FORM_CHANGED"):
        ingest.ingest_blind_ratings(
            rating_paths=[rating], preview_root=tmp_path,
        )
