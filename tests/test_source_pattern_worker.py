"""A run's audit is a bounded fact, not gold or silent duplicate success."""
import asyncio
import json

import pytest

from holoclip.source_pattern_worker import (
    VERSION, _state_lock, run_locked_source_pattern_collection,
)

NO_GOLD = {
    "source_identity_verified": False,
    "reviewed_gold_available": False,
    "qualification_allowed": False,
    "ready": False,
    "production_ready": False,
    "editor_cut_precision": None,
    "editor_cut_recall": None,
    "real_original_match_false_positive_rate": None,
}


def _run(tmp_path, **overrides):
    options = dict(
        store=tmp_path / "examples.jsonl",
        state_path=tmp_path / "state.json",
        output=tmp_path / "videos",
        report_path=tmp_path / "last-run.json",
        policy_rules=[],
        cycles=2, discover=False,
    )
    options.update(overrides)
    return asyncio.run(run_locked_source_pattern_collection(None, **options))


def test_success_writes_audit_without_gold(tmp_path, monkeypatch):
    from holoclip import source_pattern_worker as worker

    async def fake(_provider, **_kwargs):
        return {
            "version": "test-collector",
            "status": "UNKNOWN_NO_REAL_PUBLIC_VIDEO_PATTERN_MEASUREMENTS",
            **NO_GOLD,
        }

    monkeypatch.setattr(worker, "collect_public_source_patterns", fake)
    result = _run(tmp_path)
    report = json.loads((tmp_path / "last-run.json").read_text(encoding="utf-8"))
    assert report["version"] == VERSION
    assert report["status"] == result["status"]
    assert report["result"]["status"] == result["status"]
    assert result["report_path"] == str(tmp_path / "last-run.json")
    assert report["started_at_utc"] <= report["finished_at_utc"]
    assert report["ready"] is False
    assert report["reviewed_gold_available"] is False
    # Existing lock file is normal: the OS lock is released on exit.
    assert (tmp_path / ".source-pattern-collector.lock").exists()
    result2 = _run(tmp_path)
    assert result2["status"] == result["status"]


def test_second_process_lock_refuses_network_without_replacing_audit(tmp_path, monkeypatch):
    from holoclip import source_pattern_worker as worker

    async def forbidden(_provider, **_kwargs):
        raise AssertionError("overlapping process entered real collection")

    monkeypatch.setattr(worker, "collect_public_source_patterns", forbidden)
    state = tmp_path / "state.json"
    with _state_lock(state) as acquired:
        assert acquired is True
        outcome = _run(tmp_path)
    assert outcome["status"] == "BLOCKED_CONCURRENT_COLLECTION"
    assert outcome["ready"] is False
    assert not (tmp_path / "last-run.json").exists()


def test_crash_is_audited_and_lock_released(tmp_path, monkeypatch):
    from holoclip import source_pattern_worker as worker

    async def broken(_provider, **_kwargs):
        raise RuntimeError("network location must not be copied to audit")

    monkeypatch.setattr(worker, "collect_public_source_patterns", broken)
    with pytest.raises(RuntimeError):
        _run(tmp_path)
    report = json.loads((tmp_path / "last-run.json").read_text(encoding="utf-8"))
    assert report["status"] == "UNKNOWN_COLLECTION_EXECUTION_FAILURE"
    assert report["error_class"] == "RuntimeError"
    assert "network location" not in json.dumps(report)
    assert report["result"] is None
    assert report["ready"] is False
    with _state_lock(tmp_path / "state.json") as acquired:
        assert acquired is True


def test_forged_success_claim_cannot_be_written_as_valid_audit(tmp_path, monkeypatch):
    from holoclip import source_pattern_worker as worker

    async def promoted(_provider, **_kwargs):
        return {
            "status": "MEASURED_UNREVIEWED_CONSTRUCTED_PUBLIC_VIDEO_PATTERNS",
            **{**NO_GOLD, "ready": True},
        }

    monkeypatch.setattr(worker, "collect_public_source_patterns", promoted)
    with pytest.raises(ValueError, match="gold"):
        _run(tmp_path)
    report = json.loads((tmp_path / "last-run.json").read_text(encoding="utf-8"))
    assert report["status"] == "UNKNOWN_COLLECTION_EXECUTION_FAILURE"


def test_lock_path_symlink_refused(tmp_path):
    (tmp_path / ".source-pattern-collector.lock").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="symlink"):
        _run(tmp_path)


@pytest.mark.parametrize("which", ["state", "examples"])
def test_report_cannot_clobber_source_files(tmp_path, which):
    path = tmp_path / ("state.json" if which == "state" else "examples.jsonl")
    with pytest.raises(ValueError, match="overwrite"):
        _run(tmp_path, report_path=path)
    assert not path.exists()
