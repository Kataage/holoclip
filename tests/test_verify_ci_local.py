"""Zero-external-access tests for local non-GitHub CI preflight wrapper."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def verifier():
    path = Path(__file__).resolve().parents[1] / "scripts" / "verify-ci-local.py"
    spec = importlib.util.spec_from_file_location("holoclip_local_ci", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plan_covers_full_linux_ci_and_windows_offline_probes(verifier):
    linux = verifier._plan(False, "uv", full_windows=False)
    windows = verifier._plan(True, "uv", full_windows=False)
    assert [name for name, _ in linux] == ["ruff", "linux_full_pytest"]
    assert windows[0][0] == "ruff"
    assert "windows_pytest" in dict(windows)
    assert "windows_process_tree" in dict(windows)
    assert "test-ytdlp-lan-setup-doctor.ps1" in dict(windows)
    assert "test-ytdlp-lan-acl.ps1" in dict(windows)
    assert "optional_windows_full_pytest" not in dict(windows)
    assert "optional_windows_full_pytest" in dict(verifier._plan(
        True, "uv", full_windows=True,
    ))
    for _name, args in windows + linux:
        assert "-no-sync" not in args
        if args[0] == "uv":
            assert "--no-sync" in args
            assert "--offline" in args


def test_local_runner_never_claims_github_or_two_host_ci(verifier, monkeypatch, capsys):
    head = "a" * 40
    monkeypatch.setattr(verifier, "_git_head", lambda: head)
    monkeypatch.setattr(verifier, "_tracked_clean", lambda: True)
    monkeypatch.setattr(verifier.platform, "system", lambda: "Linux")
    monkeypatch.setattr(verifier.shutil, "which", lambda command: "uv" if command == "uv" else None)
    monkeypatch.setattr(verifier, "sys", SimpleNamespace(
        argv=["verify-ci-local.py", "--expected-head", head],
        version_info=(3, 12, 3),
    ))
    calls = []
    monkeypatch.setattr(verifier, "_run", lambda cmd: calls.append(cmd) or 0)
    assert verifier.main() == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "LOCAL_PREFLIGHT_PASSED_NOT_GITHUB_CI"
    assert data["github_actions_ci_verified"] is False
    assert data["both_platforms_verified"] is False
    assert data["ready"] is False
    assert data["two_host_verified"] is False
    assert len(calls) == 2


def test_wrong_sha_never_executes_tests(verifier, monkeypatch, capsys):
    monkeypatch.setattr(verifier, "_git_head", lambda: "a" * 40)
    monkeypatch.setattr(verifier, "_tracked_clean", lambda: True)
    monkeypatch.setattr(verifier.shutil, "which", lambda *_: "uv")
    monkeypatch.setattr(verifier, "sys", SimpleNamespace(
        argv=["verify-ci-local.py", "--expected-head", "b" * 40],
        version_info=(3, 12),
    ))
    monkeypatch.setattr(verifier, "_run", lambda *_: pytest.fail("no tests should start"))
    assert verifier.main() == 6
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "LOCAL_PREFLIGHT_PREREQUISITE_FAILED"
    assert data["expected_sha_matches"] is False


def test_subprocess_uses_no_network_flags_and_no_shell(verifier, monkeypatch, tmp_path):
    monkeypatch.setattr(verifier, "REPO", tmp_path)
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert verifier._run(["uv", "run", "--offline", "--no-sync", "pytest", "-q"]) == 0
    cmd, kw = calls[0]
    assert cmd[0][0] == "uv"
    assert kw["env"]["UV_OFFLINE"] == "1"
    assert kw["env"]["UV_NO_DOWNLOADS"] == "1"
    assert kw["env"]["UV_NO_SYNC"] == "1"
    assert kw["stdin"] == subprocess.DEVNULL
    assert kw["capture_output"] is True
    assert "shell" not in kw


def test_subprocess_timeout_never_becomes_pass(verifier, monkeypatch):
    def timed_out(*_args, **_kw):
        raise subprocess.TimeoutExpired("pytest", 1)
    monkeypatch.setattr(subprocess, "run", timed_out)
    assert verifier._run(["pytest", "-q"]) == 127
