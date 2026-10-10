"""Windows scheduled worker enrollment stays preview-only without apply."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import render_source_queue_task as task


def _allowed(monkeypatch, tmp_path):
    queue = tmp_path / "queue.sqlite3"
    queue.write_bytes(b"mock-enrolled-queue")
    script = tmp_path / "installer.ps1"
    runner = tmp_path / "runner.ps1"
    script.write_text("mock script")
    runner.write_text("mock runner")
    monkeypatch.setattr(task, "_INSTALL", script)
    monkeypatch.setattr(task, "_RUNNER", runner)
    monkeypatch.setattr(task, "_windows", lambda: True)
    monkeypatch.setattr(
        task, "inspect_render_queue", lambda **k: {"status": "LOCAL_RENDER_QUEUE_HAS_PENDING"},
    )
    monkeypatch.setattr(task, "_task_state", lambda name: "LAN_TASK_ABSENT_OBSERVED")
    monkeypatch.setattr(task.shutil, "which", lambda name: "mock-powershell")
    return queue


def test_schedule_preview_does_not_install_or_start(tmp_path, monkeypatch):
    state = _allowed(monkeypatch, tmp_path)
    monkeypatch.setattr(
        task.subprocess, "run",
        lambda *a, **kw: pytest.fail("preview must not invoke PowerShell installer"),
    )
    result = task.prepare_render_queue_task(queue=state)
    assert result["status"] == "LOCAL_RENDER_QUEUE_TASK_PREVIEW_ELIGIBLE"
    assert result["task_registered"] is False
    assert result["task_started"] is False
    assert result["ready"] is False


def test_explicit_task_register_once_after_absence_recheck(tmp_path, monkeypatch):
    state = _allowed(monkeypatch, tmp_path)
    checks = []
    def mock_runner(argv, **kwargs):
        checks.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=b"LOCAL_RENDER_QUEUE_TASK_REGISTERED_NOT_STARTED\n",
            stderr=b"",
        )
    monkeypatch.setattr(task.subprocess, "run", mock_runner)
    report = task.prepare_render_queue_task(queue=state, apply=True)
    assert report["status"] == "LOCAL_RENDER_QUEUE_TASK_REGISTERED_NOT_STARTED"
    assert report["task_registered"] is True
    assert report["task_started"] is False
    assert "-IntervalMinutes" in checks[0][0]
    assert checks[0][1]["timeout"] == 45


def test_task_collision_blocks_registration(tmp_path, monkeypatch):
    state = _allowed(monkeypatch, tmp_path)
    monkeypatch.setattr(task, "_task_state", lambda name: "LAN_TASK_ALREADY_PRESENT")
    monkeypatch.setattr(
        task.subprocess, "run",
        lambda *a, **k: pytest.fail("must not overwrite existing Windows task"),
    )
    report = task.prepare_render_queue_task(queue=state, apply=True)
    assert report["status"] == "LOCAL_RENDER_QUEUE_TASK_PREVIEW_BLOCKED"


def test_changed_task_between_preflight_and_apply_stops(tmp_path, monkeypatch):
    state = _allowed(monkeypatch, tmp_path)
    statuses = iter(("LAN_TASK_ABSENT_OBSERVED", "LAN_TASK_ALREADY_PRESENT"))
    monkeypatch.setattr(task, "_task_state", lambda name: next(statuses))
    monkeypatch.setattr(
        task.subprocess, "run",
        lambda *a, **k: pytest.fail("must not overwrite task that just appeared"),
    )
    report = task.prepare_render_queue_task(queue=state, apply=True)
    assert report["status"] == "LOCAL_RENDER_QUEUE_TASK_APPLY_BLOCKED"


def test_task_installer_failure_reports_sanitized_unconfirmed(tmp_path, monkeypatch):
    state = _allowed(monkeypatch, tmp_path)
    monkeypatch.setattr(
        task.subprocess, "run",
        lambda *a, **k: SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"C:\\SECRET\\raw scheduler error",
        ),
    )
    report = task.prepare_render_queue_task(queue=state, apply=True)
    assert report["status"] == "LOCAL_RENDER_QUEUE_TASK_APPLY_UNCONFIRMED"
    assert "SECRET" not in str(report)
    assert report["task_registered"] is False


@pytest.mark.parametrize("bad", ["bad/name", "", "x" * 91])
def test_invalid_task_name_rejected_before_scheduler(tmp_path, monkeypatch, bad):
    state = _allowed(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="LOCAL_RENDER_QUEUE_TASK_INVALID_NAME"):
        task.prepare_render_queue_task(queue=state, task_name=bad)


def test_non_windows_not_registered(tmp_path, monkeypatch):
    state = _allowed(monkeypatch, tmp_path)
    monkeypatch.setattr(task, "_windows", lambda: False)
    report = task.prepare_render_queue_task(queue=state, apply=True)
    assert report["status"] == "LOCAL_RENDER_QUEUE_TASK_PREVIEW_BLOCKED"


def test_cli_task_without_queue_returns_nonzero(tmp_path):
    result = CliRunner().invoke(cli.app, [
        "render-source-queue-task-prepare", "--queue",
        str(tmp_path / "missing.sqlite3"),
    ])
    assert result.exit_code == 6
    assert "ready" in result.stdout
