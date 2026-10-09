"""Cross-command source-state concurrency protects saved evidence, not just jobs."""
import errno
import os

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip.source_pattern_worker import _state_lock


def test_visual_cli_refuses_overlapping_collector_before_public_requests(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(cli, "_load_env_file", lambda: None)
    invoked = []

    def forbidden(*args, **kwargs):
        invoked.append("video")
        raise AssertionError("raced public video acquisition")

    monkeypatch.setattr(cli, "verify_pending_source_visual", forbidden)
    monkeypatch.setattr(cli, "YtDlpSourceProvider", lambda **kwargs: object())
    state = tmp_path / "shared.json"
    with _state_lock(state) as acquired:
        assert acquired is True
        res = CliRunner().invoke(cli.app, [
            "examples-verify-source-visual",
            "--state", str(state),
        ])
    assert res.exit_code == 6, res.output
    assert "BLOCKED_CONCURRENT_CORPUS_STATE_WRITER" in res.output
    assert invoked == []
    assert not state.exists()


def test_discover_only_corpus_growth_cli_refuses_overlap_without_discovery(
    tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_load_env_file", lambda: None)
    monkeypatch.setattr(cli, "YtDlpSourceProvider", lambda **kwargs: object())
    invoked = []

    async def forbidden(*args, **kwargs):
        invoked.append("search")
        raise AssertionError("raced corpus discovery")

    monkeypatch.setattr(cli, "grow_corpus", forbidden)
    state = tmp_path / "corpus.json"
    with _state_lock(state) as acquired:
        assert acquired is True
        res = CliRunner().invoke(cli.app, [
            "examples-grow-corpus", "--discover-only",
            "--state", str(state),
            "--database", str(tmp_path / "db.sqlite"),
        ])
    assert res.exit_code == 6, res.output
    assert "BLOCKED_CONCURRENT_CORPUS_STATE_WRITER" in res.output
    assert invoked == []
    assert not state.exists()


def test_visual_cli_releases_lock_on_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_load_env_file", lambda: None)
    monkeypatch.setattr(cli, "YtDlpSourceProvider", lambda **kwargs: object())

    def fail(*args, **kwargs):
        raise RuntimeError("unavailable video")

    monkeypatch.setattr(cli, "verify_pending_source_visual", fail)
    state = tmp_path / "state.json"
    first = CliRunner().invoke(cli.app, [
        "examples-verify-source-visual", "--state", str(state),
    ])
    assert first.exit_code != 0
    with _state_lock(state) as acquired:
        assert acquired is True


def test_visual_cli_runs_after_previous_writer_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_load_env_file", lambda: None)
    monkeypatch.setattr(cli, "YtDlpSourceProvider", lambda **kwargs: object())
    calls = []

    def synthetic(_provider, **kwargs):
        calls.append(kwargs["state_path"])
        return {
            "status": "UNKNOWN_NO_MEASURED_VIDEO",
            "source_identity_verified": False,
            "qualification_allowed": False,
        }

    monkeypatch.setattr(cli, "verify_pending_source_visual", synthetic)
    state = tmp_path / "state.json"
    with _state_lock(state) as acquired:
        assert acquired
    res = CliRunner().invoke(cli.app, [
        "examples-verify-source-visual", "--state", str(state),
    ])
    assert res.exit_code == 0, res.output
    assert calls == [state]
    assert "UNKNOWN_NO_MEASURED_VIDEO" in res.output


def test_locking_runtime_error_not_falsely_described_as_busy(tmp_path, monkeypatch):
    from holoclip import source_pattern_worker as worker

    if os.name == "nt":
        pytest.skip("fcntl error injection applies to POSIX CI")
    import fcntl

    def fail(_fd, _operation):
        raise OSError(errno.EIO, "filesystem I/O failure")

    monkeypatch.setattr(fcntl, "flock", fail)
    with pytest.raises(OSError) as exc:
        with worker._state_lock(tmp_path / "state.json"):
            pass
    assert exc.value.errno == errno.EIO
