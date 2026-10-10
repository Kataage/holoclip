#!/usr/bin/env python3
"""Local, offline-only Holoclip CI preflight if private hosted runners are blocked.

This is not a GitHub Actions status or a Linux+Windows two-runner certificate.
Run from the current checkout with an existing uv environment. No Docker,
downloads, external media, updater calls, or self-hosted runner installation.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WINDOWS_TESTS = (
    "tests/test_source_pattern_worker.py",
    "tests/test_corpus_state_writer_lock.py",
    "tests/test_source_pattern_diagnostics.py",
    "tests/test_source_ytdlp_lan_client_setup.py",
    "tests/test_source_ytdlp_lan_onboarding.py",
    "tests/test_source_ytdlp_lan_ssh_transfer.py",
    "tests/test_source_ytdlp_lan_setup_doctor.py",
    "tests/test_source_ytdlp_lan_pair_evidence.py",
    "tests/test_source_ytdlp_lan_pair_collect.py",
    "tests/test_source_ytdlp_lan_local_bootstrap.py",
    "tests/test_source_ytdlp_lan_task_prepare.py",
    "tests/test_source_ytdlp_lan_firewall_prepare.py",
    "tests/test_source_ytdlp_lan_pc_a_prepare.py",
    "tests/test_source_ytdlp_lan_pc_b_prepare.py",
    "tests/test_source_ytdlp_lan_deployment_diagnosis.py",
    "tests/test_source_ytdlp_lan_pacing_trial.py",
    "tests/test_source_ytdlp_lan_evidence_history.py",
)
WINDOWS_PS_SCRIPTS = (
    "test-source-pattern-windows.ps1",
    "test-source-pattern-install.ps1",
    "test-ytdlp-lan-task.ps1",
    "test-ytdlp-lan-acl.ps1",
    "test-ytdlp-lan-setup-doctor.ps1",
    "test-ytdlp-lan-firewall-scope.ps1",
    "test-ytdlp-lan-firewall-prepare.ps1",
)
MAX_COMMAND_SECONDS = 1200


def _run(command: list[str], *, timeout: int = MAX_COMMAND_SECONDS) -> int:
    # Never use a command shell; do not echo absolute paths or test stdout
    # (which could inadvertently include local credentials or machine data).
    env = dict(os.environ, UV_OFFLINE="1", UV_NO_DOWNLOADS="1", UV_NO_SYNC="1")
    try:
        proc = subprocess.run(
            command, cwd=REPO, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127
    return proc.returncode


def _git_head() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=REPO,
            stdin=subprocess.DEVNULL, capture_output=True,
            timeout=10, check=False, text=True,
        )
        head = proc.stdout.strip()
        return head if proc.returncode == 0 and re.fullmatch("[a-f0-9]{40}", head) else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _tracked_clean() -> bool:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=REPO, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=10, check=False,
        )
        return proc.returncode == 0 and not proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False


def _plan(is_windows: bool, uv: str, *, full_windows: bool) -> list[tuple[str, list[str]]]:
    pre = [uv, "run", "--offline", "--no-sync"]
    steps = [("ruff", [*pre, "ruff", "check", "."])]
    if is_windows:
        steps.append(("windows_pytest", [*pre, "pytest", "-q", *WINDOWS_TESTS]))
        steps.append((
            "windows_process_tree",
            [*pre, "pytest", "-q", "tests/test_source_ytdlp.py",
             "-k", "native_process or timeout_terminates_descendant_process"],
        ))
        shell = Path(os.environ.get("SystemRoot", "C:/Windows")) / (
            "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        for script in WINDOWS_PS_SCRIPTS:
            steps.append((
                script,
                [str(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                 "Bypass", "-File", str(REPO / "scripts" / script)],
            ))
        if full_windows:
            steps.append(("optional_windows_full_pytest", [*pre, "pytest", "-q"]))
    else:
        steps.append(("linux_full_pytest", [*pre, "pytest", "-q"]))
    return steps


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Offline local CI preflight (never a substitute for both GitHub runners).",
    )
    parser.add_argument(
        "--expected-head", default=None,
        help="Optional 40-character Git SHA; refuse to claim clean results for other code.",
    )
    parser.add_argument(
        "--plan", action="store_true",
        help="Print step labels only, without executing checks.",
    )
    parser.add_argument(
        "--full-windows-pytest", action="store_true",
        help="On Windows also run Linux-oriented full pytest as an extra diagnostic.",
    )
    args = parser.parse_args(sys.argv[1:])
    is_windows = platform.system() == "Windows"
    current_head = _git_head()
    expected_valid = (args.expected_head is None or
                      re.fullmatch("[a-f0-9]{40}", args.expected_head) is not None)
    sha_matches = expected_valid and (args.expected_head is None or
                                      args.expected_head == current_head)
    clean = _tracked_clean()
    runtime_ok = sys.version_info[:2] == (3, 12)
    uv = shutil.which("uv")
    steps = _plan(is_windows, uv or "uv", full_windows=args.full_windows_pytest)
    report: dict[str, object] = {
        "status": "LOCAL_PREFLIGHT_NOT_RUN",
        "platform": "windows" if is_windows else platform.system().lower(),
        "python_312": runtime_ok,
        "git_head": current_head,
        "expected_sha_matches": bool(sha_matches),
        "tracked_worktree_clean": clean,
        "uv_installed": uv is not None,
        "results": [],
        "network_access_intended": False,
        "docker_required": False,
        "github_actions_ci_verified": False,
        "both_platforms_verified": False,
        "two_host_verified": False,
        "ready": False,
    }
    if args.plan:
        report["planned_steps"] = [name for name, _ in steps]
        print(json.dumps(report, sort_keys=True))
        return 0
    if (current_head is None or not sha_matches or not clean or
            not runtime_ok or uv is None):
        report["status"] = "LOCAL_PREFLIGHT_PREREQUISITE_FAILED"
        print(json.dumps(report, sort_keys=True))
        return 6
    result_rows: list[dict[str, object]] = []
    for name, command in steps:
        code = _run(command)
        result_rows.append({"check": name, "passed": code == 0, "exit_code": code})
        if code != 0:
            break
    report["results"] = result_rows
    report["status"] = (
        "LOCAL_PREFLIGHT_PASSED_NOT_GITHUB_CI"
        if len(result_rows) == len(steps) and all(x["passed"] for x in result_rows)
        else "LOCAL_PREFLIGHT_FAILED"
    )
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "LOCAL_PREFLIGHT_PASSED_NOT_GITHUB_CI" else 6


if __name__ == "__main__":
    sys.exit(main())
