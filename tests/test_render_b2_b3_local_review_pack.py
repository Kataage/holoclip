"""Offline source/B2/B3 playback pack tests; all FFmpeg and media are synthetic."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_b2_b3_local_review_pack as pack
from holoclip import render_b2_b3_finished_audio_asr as asr
from test_render_b2_b3_audio_review import _measured
from test_render_cut_extension_mp4_ab import _args


def _case(tmp_path, monkeypatch):
    options, basis, source, transcript, edited = _measured(tmp_path, monkeypatch)
    # The review target is upstream's *unrated* prioritized source clock hint,
    # not a fabricated gold transcript or an auto-selected winner.
    monkeypatch.setattr(pack, "_review", lambda b, r, sha: {
        "status": "LOCAL_B2_B3_REVIEW_TARGETS_UNRATED_ASR_AND_QA",
        "review_target_count": 1, "targets": [{
            "kind": "B3_NEW_ASR_WARNING", "priority": "HIGH_REVIEW",
            "source_start_seconds": None, "source_end_seconds": None,
            "source_asr_interval_id": None,
        }],
        "b2_b3_audio_asr_receipt_sha256": sha,
    })
    return options, basis, source, transcript, edited


def _fake_media(tmp_path, monkeypatch, state, *, before_commit=None):
    cached = tmp_path / "cached-source.mp4"
    cached.write_bytes(b"entire cached video locally owned" * 20)
    ids = {c.source_asset_id for c in state["basis"]["context"]["roles"]["b2"]["timeline"].segments}
    assert len(ids) == 1
    monkeypatch.setattr(pack, "resolve_cached_render_sources", lambda *a, **kw: [
        SimpleNamespace(asset=SimpleNamespace(id=next(iter(ids))), path=cached),
    ])
    monkeypatch.setattr(pack.shutil, "which", lambda program: "fake-ffmpeg")
    durations = {}
    writes = []

    def fake_encode(ffmpeg, source, output, window):
        assert ffmpeg == "fake-ffmpeg"
        assert output.parent != source.parent or output != source
        output.write_bytes(("new, unqualified "+output.stem).encode() * 20)
        durations[str(output)] = window["expected_seconds"]
        writes.append(source)
        if before_commit is not None and len(writes) == 3:
            before_commit()
    monkeypatch.setattr(pack, "_encode", fake_encode)
    monkeypatch.setattr(pack, "_probe", lambda media: (
        durations.get(str(media), 1200.), True, True,
    ))
    return cached, writes


def test_default_is_read_only_and_never_resolves_or_decodes(tmp_path, monkeypatch):
    options, basis, source, transcript, edited = _case(tmp_path, monkeypatch)
    before = [p.read_bytes() for p in (source, transcript, edited, basis["receipt"])]
    monkeypatch.setattr(pack, "resolve_cached_render_sources",
                        lambda *a, **kw: pytest.fail("preview must not touch full source cache"))
    monkeypatch.setattr(pack, "_encode",
                        lambda *a, **kw: pytest.fail("preview must not decode"))
    report = pack.run_local_review_pack(**options)
    assert report["status"] == "LOCAL_REVIEW_PACK_READ_ONLY_PLAN"
    assert report["review_target_priority"] == "HIGH_REVIEW"
    assert report["source_asr_not_verified_spoken_words"] is True
    assert report["created_review_clips"] == {}
    assert report["number_of_review_clips"] == 0
    assert report["automatic_winner"] is None and report["ready"] is False
    assert not list(options["preview_root"].rglob("*.playback.json"))
    assert [p.read_bytes() for p in (source, transcript, edited, basis["receipt"])] == before
    assert str(tmp_path) not in json.dumps(report)


def test_explicit_approval_required_no_source_access(tmp_path, monkeypatch):
    options, _, _, _, _ = _case(tmp_path, monkeypatch)
    monkeypatch.setattr(pack, "resolve_cached_render_sources",
                        lambda *a, **kw: pytest.fail("no consent"))
    with pytest.raises(ValueError, match="APPROVAL_REQUIRED"):
        pack.run_local_review_pack(**options, execute=True)


def test_creates_three_bounded_clips_and_immutable_unrated_receipt(
    tmp_path, monkeypatch,
):
    options, basis, source, transcript, edited = _case(tmp_path, monkeypatch)
    preview = pack.run_local_review_pack(**options)
    state = pack._basis(options, 0)
    cached, writes = _fake_media(tmp_path, monkeypatch, state)
    previous = [p.read_bytes() for p in (
        source, transcript, edited, basis["receipt"], cached,
    )]
    result = pack.run_local_review_pack(
        **options, execute=True, approve_local_preview=True,
    )
    assert result["status"] == "LOCAL_REVIEW_PACK_LOCAL_CLIPS_UNRATED"
    assert result["number_of_review_clips"] == len(writes) == 3
    assert set(result["created_review_clips"]) == {"source", "b2", "b3"}
    assert result["rendered_source_window"]["expected_seconds"] <= 6.1
    assert all(meta["window"]["expected_seconds"] <= 6.1
               for meta in result["created_review_clips"].values())
    assert result["originals_modified"] is False
    assert result["source_video_downloaded"] is False
    assert result["new_provider_request"] is False
    assert result["automatic_adoption_allowed"] is False
    assert result["publication_performed"] is False and result["ready"] is False
    for role, meta in result["created_review_clips"].items():
        target = state["artifacts"][role]
        assert target.is_file()
        assert target.name == meta["filename"]
        assert pack._hash(target)[0] == meta["sha256"]
    receipt = state["receipt"]
    assert json.loads(receipt.read_text(encoding="utf-8")) == result
    assert [p.read_bytes() for p in (
        source, transcript, edited, basis["receipt"], cached,
    )] == previous
    assert preview["status"] == "LOCAL_REVIEW_PACK_READ_ONLY_PLAN"
    with pytest.raises(ValueError, match="EXISTS"):
        pack.run_local_review_pack(
            **options, execute=True, approve_local_preview=True,
        )
    assert len(writes) == 3
    assert str(tmp_path) not in json.dumps(result)


@pytest.mark.parametrize("occupied", ["source", "b2", "b3", "receipt"])
def test_any_preexisting_destination_blocks_before_all_encoding(
    tmp_path, monkeypatch, occupied,
):
    options, _, _, _, _ = _case(tmp_path, monkeypatch)
    state = pack._basis(options, 0)
    dest = state["receipt"] if occupied == "receipt" else state["artifacts"][occupied]
    dest.write_bytes(b"user-owned destination")
    _, writes = _fake_media(tmp_path, monkeypatch, state)
    with pytest.raises(ValueError, match="EXISTS"):
        pack.run_local_review_pack(
            **options, execute=True, approve_local_preview=True,
        )
    assert writes == [] and dest.read_bytes() == b"user-owned destination"


def test_changed_finished_mp4_mid_decode_leaves_no_pack(tmp_path, monkeypatch):
    options, _, _, _, _ = _case(tmp_path, monkeypatch)
    state = pack._basis(options, 0)
    finished_b2 = state["basis"]["context"]["roles"]["b2"]["final"]
    original = finished_b2.read_bytes()
    def mutate():
        finished_b2.write_bytes(b"changed while staged")
    _fake_media(tmp_path, monkeypatch, state, before_commit=mutate)
    try:
        with pytest.raises(ValueError):
            pack.run_local_review_pack(
                **options, execute=True, approve_local_preview=True,
            )
    finally:
        finished_b2.write_bytes(original)
    assert not state["receipt"].exists()
    assert all(not item.exists() for item in state["artifacts"].values())


def test_changed_asr_receipt_blocks_decode(tmp_path, monkeypatch):
    options, basis, _, _, _ = _case(tmp_path, monkeypatch)
    state = pack._basis(options, 0)
    basis["receipt"].write_text('{"status":"UNTRUSTED"}', encoding="utf-8")
    monkeypatch.setattr(pack, "_encode",
                        lambda *a, **k: pytest.fail("untrusted ASR cannot decode"))
    with pytest.raises(ValueError):
        pack.run_local_review_pack(
            **options, execute=True, approve_local_preview=True,
        )
    assert not state["receipt"].exists()


def test_source_clock_projection_respects_omission_and_repetition():
    cut = lambda lo, hi, order: SimpleNamespace(
        source_asset_id="source", source_start=lo, source_end=hi,
        output_order=order,
    )
    timeline = SimpleNamespace(segments=[
        cut(10, 15, 0), cut(30, 33, 1),
    ])
    assert pack._project(timeline, "source", 12) == 2.
    assert pack._project(timeline, "source", 31) == 6.
    assert pack._project(timeline, "source", 20) is None
    duplicate = SimpleNamespace(segments=[cut(10, 15, 0), cut(10, 15, 1)])
    assert pack._project(duplicate, "source", 12) is None


def test_cli_preview_and_denied_execution(tmp_path, monkeypatch):
    options, _, _, _, _ = _case(tmp_path, monkeypatch)
    args = [
        "render-b2-b3-local-review-pack", *_args(options)[1:],
        "--job-id", options["job_id"],
        "--proposal-index", str(options["proposal_index"]),
    ]
    preview = CliRunner().invoke(cli.app, args)
    assert preview.exit_code == 0, preview.stdout
    assert json.loads(preview.stdout)["status"] == "LOCAL_REVIEW_PACK_READ_ONLY_PLAN"
    denied = CliRunner().invoke(cli.app, [*args, "--execute"])
    assert denied.exit_code == 6
    assert json.loads(denied.stdout)["ready"] is False
