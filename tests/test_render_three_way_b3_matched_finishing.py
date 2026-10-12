"""B3 audio/caption finishing parity with A/B/B2: mock-only, create-only."""
from __future__ import annotations

import hashlib
import json

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_three_way_b3_local_render as render
from holoclip import render_three_way_b3_matched_finishing as finish
from holoclip.render_cut_extension_finish_ab import _SUCCESS, _stage_paths
from test_render_cut_extension_mp4_ab import _args
from test_render_three_way_b3_local_render import _eligible, _fake_render


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case(tmp_path, monkeypatch):
    options, _, source, transcript, edited = _eligible(tmp_path, monkeypatch)
    dry = render.run_existing_b3_preview(**options)
    _fake_render(monkeypatch, dry["b3_expected_seconds"])
    rendered = render.run_existing_b3_preview(
        **options, execute=True, approve_local_preview=True,
    )
    assert rendered["status"] == "LOCAL_B3_RENDERED_BASIC_AV_QA_NOT_EDITED_QUALITY"
    monkeypatch.setattr(finish, "_probe", lambda path: (
        dry["b3_expected_seconds"] if "-speechb3-" in path.name else 7.0,
        True, True,
    ))
    return options, source, transcript, edited


def _mock_stages(monkeypatch, fail=None):
    count = {"audio": 0, "caption": 0, "burnin": 0}
    monkeypatch.setattr(finish, "resolve_cached_render_sources",
                        lambda *a, **k: [])
    monkeypatch.setattr(finish, "_offline_render_policy", lambda *a: None)

    def subtitles(timeline, sources, *, transcript_root, mp4_output):
        count["caption"] += 1
        if fail == "caption":
            return {"status": "LOCAL_SUBTITLE_UNAVAILABLE_OR_UNTRUSTED"}
        output = mp4_output.with_suffix(".srt")
        output.write_bytes(b"1\n00:00:00,000 --> 00:00:01,000\nsubtitles\n")
        return {
            "status": _SUCCESS["caption"], "output_sha256": _sha(output),
            "cue_count": 1, "dropped_cue_count": 0,
        }

    def audio(*, mp4_output, video_sha256, timeline):
        count["audio"] += 1
        if fail == "audio":
            return {"status": "LOCAL_AUDIO_LEVELING_UNAVAILABLE_OR_FAILED"}
        output = mp4_output.with_name(f"{mp4_output.stem}.leveled.mp4")
        output.write_bytes(b"independent leveled B3 * 100" * 10)
        return {"status": _SUCCESS["audio"],
                "output_sha256": _sha(output), "max_abs_gain_db": 1.5}

    def burn(*, mp4_output, video_sha256, subtitle_sha256):
        count["burnin"] += 1
        if fail == "burnin":
            return {"status": "LOCAL_SUBTITLE_BURNIN_UNAVAILABLE_OR_FAILED"}
        output = mp4_output.with_name(
            f"{mp4_output.stem.removesuffix('.leveled')}.captioned.mp4"
        )
        output.write_bytes(b"independent captioned B3 * 100" * 10)
        return {"status": _SUCCESS["burnin"], "output_sha256": _sha(output)}

    monkeypatch.setattr(finish, "export_cut_aligned_srt", subtitles)
    monkeypatch.setattr(finish, "level_preview_audio", audio)
    monkeypatch.setattr(finish, "burn_captioned_preview", burn)
    return count


def test_default_previews_without_ffmpeg_writes(tmp_path, monkeypatch):
    options, source, asr, edited = _case(tmp_path, monkeypatch)
    before = source.read_bytes(), asr.read_bytes(), edited.read_bytes()
    monkeypatch.setattr(
        finish, "level_preview_audio",
        lambda *a, **k: pytest.fail("preview must not encode"),
    )
    report = finish.run_existing_b3_matched_finishing(**options)
    assert report["status"] == "LOCAL_B3_FINISH_READ_ONLY_PREVIEW"
    assert report["b3_original_mp4_sha256"]
    assert report["b3_optional_stages_match_a_b_b2"] is False
    assert report["ready"] is False
    assert not list(options["preview_root"].rglob("*-speechb3-*.matched.json"))
    assert (source.read_bytes(), asr.read_bytes(), edited.read_bytes()) == before
    assert str(tmp_path) not in json.dumps(report)
    assert "まだ発話は続いている" not in json.dumps(report, ensure_ascii=False)


def test_requires_explicit_permission_and_no_partial_writes(tmp_path, monkeypatch):
    options, _, _, _ = _case(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="APPROVAL_REQUIRED"):
        finish.run_existing_b3_matched_finishing(**options, execute=True)
    assert not list(options["preview_root"].rglob("*-speechb3-*.matched.json"))


def test_matched_finishing_matches_success_stages_and_keeps_predecessors(
    tmp_path, monkeypatch,
):
    options, source, asr, edited = _case(tmp_path, monkeypatch)
    b3_file = next(options["preview_root"].rglob("*-speechb3-*.mp4"))
    before = source.read_bytes(), asr.read_bytes(), edited.read_bytes(), b3_file.read_bytes()
    calls = _mock_stages(monkeypatch)
    result = finish.run_existing_b3_matched_finishing(
        **options, execute=True, approve_local_preview=True,
    )
    assert result["status"] == "LOCAL_B3_FINISH_STAGES_TECHNICALLY_COMPARABLE"
    assert result["b3_optional_stages_match_a_b_b2"] is True
    for key in ("audio", "caption", "burnin"):
        assert calls[key] == int(result[f"baseline_{'audio_leveling' if key == 'audio' else key}_expected"])
    assert result["b3_final_filename"] and result["b3_final_sha256"]
    assert result["b3_measured_final_seconds"] is not None
    assert result["human_auditory_qa_completed"] is False
    assert result["actual_audio_quality_improvement"] is None
    assert result["editorial_quality_improvement"] is None
    assert result["publication_performed"] is False and result["ready"] is False
    receipt = next(options["preview_root"].rglob("*-speechb3-*.matched.json"))
    assert json.loads(receipt.read_text("utf-8")) == result
    assert (source.read_bytes(), asr.read_bytes(), edited.read_bytes(),
            b3_file.read_bytes()) == before
    old_bytes = receipt.read_bytes()
    with pytest.raises(ValueError, match="EXISTS|UNSAFE"):
        finish.run_existing_b3_matched_finishing(
            **options, execute=True, approve_local_preview=True,
        )
    assert receipt.read_bytes() == old_bytes


@pytest.mark.parametrize("kind", ["receipt", "audio", "caption", "burnin"])
def test_existing_b3_outputs_not_overwritten(tmp_path, monkeypatch, kind):
    options, _, _, _ = _case(tmp_path, monkeypatch)
    file = next(options["preview_root"].rglob("*-speechb3-*.mp4"))
    destination = (
        file.with_name(f"{file.stem}.matched.json")
        if kind == "receipt" else _stage_paths(file)[kind]
    )
    destination.write_bytes(b"owner-created artifact")
    calls = _mock_stages(monkeypatch)
    with pytest.raises(ValueError, match="EXISTS|UNSAFE"):
        finish.run_existing_b3_matched_finishing(
            **options, execute=True, approve_local_preview=True,
        )
    assert destination.read_bytes() == b"owner-created artifact"
    assert all(x == 0 for x in calls.values())


def test_failed_optional_stage_never_claims_comparability(tmp_path, monkeypatch):
    options, _, _, _ = _case(tmp_path, monkeypatch)
    first = finish.run_existing_b3_matched_finishing(**options)
    enabled = (
        "caption" if first["baseline_caption_expected"]
        else "audio" if first["baseline_audio_leveling_expected"]
        else "burnin" if first["baseline_burnin_expected"]
        else None
    )
    if enabled is None:
        pytest.skip("baseline has no optional finishing stage")
    _mock_stages(monkeypatch, fail=enabled)
    result = finish.run_existing_b3_matched_finishing(
        **options, execute=True, approve_local_preview=True,
    )
    assert result["status"] == "LOCAL_B3_FINISH_INCOMPLETE"
    assert result["b3_optional_stages_match_a_b_b2"] is False
    assert result["b3_final_sha256"] is None
    assert result["editorial_quality_improvement"] is None


def test_changed_audio_asr_or_b3_receipt_stops_before_finishing(
    tmp_path, monkeypatch,
):
    options, _, _, edited = _case(tmp_path, monkeypatch)
    raw = edited.read_bytes()
    edited.write_bytes(b'{"status":"INVALID"}')
    calls = _mock_stages(monkeypatch)
    with pytest.raises(ValueError):
        finish.run_existing_b3_matched_finishing(
            **options, execute=True, approve_local_preview=True,
        )
    assert all(x == 0 for x in calls.values())
    edited.write_bytes(raw)
    receipt = next(options["preview_root"].rglob("*-speechb3-*.json"))
    receipt.write_bytes(b'{"status":"TAMPERED"}')
    with pytest.raises(ValueError):
        finish.run_existing_b3_matched_finishing(
            **options, execute=True, approve_local_preview=True,
        )
    assert all(x == 0 for x in calls.values())


def test_predecessor_mutation_during_encode_blocks_matched_receipt(
    tmp_path, monkeypatch,
):
    options, source, _, _ = _case(tmp_path, monkeypatch)
    calls = _mock_stages(monkeypatch)
    normal = finish.level_preview_audio

    def change_baseline(**kwargs):
        result = normal(**kwargs)
        source.write_bytes(b"new predecessor bytes")
        return result

    if not finish.run_existing_b3_matched_finishing(
            **options)["baseline_audio_leveling_expected"]:
        pytest.skip("baseline audio stage absent")
    monkeypatch.setattr(finish, "level_preview_audio", change_baseline)
    with pytest.raises(ValueError):
        finish.run_existing_b3_matched_finishing(
            **options, execute=True, approve_local_preview=True,
        )
    assert calls["audio"] == 1
    assert not list(options["preview_root"].rglob("*-speechb3-*.matched.json"))


def test_cli_default_and_denied_execution(tmp_path, monkeypatch):
    options, _, _, _ = _case(tmp_path, monkeypatch)
    argv = ["render-three-way-b3-matched-finishing", *_args(options)[1:],
            "--job-id", options["job_id"],
            "--proposal-index", str(options["proposal_index"])]
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["status"] == "LOCAL_B3_FINISH_READ_ONLY_PREVIEW"
    denied = CliRunner().invoke(cli.app, [*argv, "--execute"])
    assert denied.exit_code == 6, denied.stdout
    assert json.loads(denied.stdout)["ready"] is False



def test_false_success_without_stage_file_cannot_claim_technical_parity(
    tmp_path, monkeypatch,
):
    options, _, _, _ = _case(tmp_path, monkeypatch)
    _mock_stages(monkeypatch)
    expected = finish.run_existing_b3_matched_finishing(**options)
    if expected["baseline_caption_expected"]:
        def bogus_caption(timeline, sources, *, transcript_root, mp4_output):
            return {
                "status": _SUCCESS["caption"],
                "output_sha256": "0" * 64, "cue_count": 1,
                "dropped_cue_count": 0,
            }
        monkeypatch.setattr(finish, "export_cut_aligned_srt", bogus_caption)
    elif expected["baseline_audio_leveling_expected"]:
        def bogus_audio(*, mp4_output, video_sha256, timeline):
            return {
                "status": _SUCCESS["audio"],
                "output_sha256": "0" * 64, "max_abs_gain_db": 1.0,
            }
        monkeypatch.setattr(finish, "level_preview_audio", bogus_audio)
    else:
        pytest.skip("baseline has no successful caption/audio stage")
    with pytest.raises(ValueError, match="STAGE_NOT_CREATED|STAGE_HASH_INVALID"):
        finish.run_existing_b3_matched_finishing(
            **options, execute=True, approve_local_preview=True,
        )
    assert not list(options["preview_root"].rglob("*-speechb3-*.matched.json"))
