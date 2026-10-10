"""Preview-first current-user Windows scheduled acquisition worker enrollment."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from holoclip.render_source_queue import DEFAULT_QUEUE, _queue_path, inspect_render_queue
from holoclip.source_ytdlp_lan_task_prepare import _TASK_NAME, _task_state, _windows

_ROOT = Path(__file__).resolve().parents[2]
_INSTALL = _ROOT / "scripts" / "install-render-source-queue-task.ps1"
_RUNNER = _ROOT / "scripts" / "run-render-source-queue.ps1"
_DEFAULT_TASK_NAME = "Holoclip render source queue"


def prepare_render_queue_task(
    *, queue: Path = DEFAULT_QUEUE,
    task_name: str = _DEFAULT_TASK_NAME,
    interval_minutes: int = 60, apply: bool = False,
) -> dict[str, object]:
    if not isinstance(task_name, str) or not _TASK_NAME.fullmatch(task_name):
        raise ValueError("LOCAL_RENDER_QUEUE_TASK_INVALID_NAME")
    if type(interval_minutes) is not int or not 30 <= interval_minutes <= 1440:
        raise ValueError("LOCAL_RENDER_QUEUE_TASK_INVALID_INTERVAL")
    path = _queue_path(queue)
    snapshot = inspect_render_queue(queue=path)
    state = _task_state(task_name) if _windows() else "LAN_TASK_ENUMERATION_UNAVAILABLE"
    report: dict[str, object] = {
        "status": "LOCAL_RENDER_QUEUE_TASK_PREVIEW_BLOCKED",
        "queue_status": snapshot["status"],
        "task_state": state,
        "task_registered": False,
        "task_started": False,
        "existing_task_overwritten": False,
        "youtube_contacted": False,
        "ready": False, "production_ready": False,
    }
    if (not _windows() or
            snapshot["status"] == "LOCAL_RENDER_QUEUE_NOT_CREATED" or
            state != "LAN_TASK_ABSENT_OBSERVED" or
            not _INSTALL.is_file() or not _RUNNER.is_file()):
        return report
    report["status"] = "LOCAL_RENDER_QUEUE_TASK_PREVIEW_ELIGIBLE"
    if not apply:
        return report
    if _task_state(task_name) != "LAN_TASK_ABSENT_OBSERVED":
        report["status"] = "LOCAL_RENDER_QUEUE_TASK_APPLY_BLOCKED"
        return report
    exe = shutil.which("powershell.exe")
    if not exe:
        report["status"] = "LOCAL_RENDER_QUEUE_TASK_APPLY_BLOCKED"
        return report
    try:
        process = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(_INSTALL), "-TaskName", task_name,
             "-QueuePath", str(path), "-IntervalMinutes", str(interval_minutes)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=45,
            check=False,
        )
        if (process.returncode != 0 or
                process.stdout.strip() != b"LOCAL_RENDER_QUEUE_TASK_REGISTERED_NOT_STARTED"):
            report["status"] = "LOCAL_RENDER_QUEUE_TASK_APPLY_UNCONFIRMED"
            return report
    except (OSError, subprocess.TimeoutExpired):
        report["status"] = "LOCAL_RENDER_QUEUE_TASK_APPLY_UNCONFIRMED"
        return report
    report["status"] = "LOCAL_RENDER_QUEUE_TASK_REGISTERED_NOT_STARTED"
    report["task_registered"] = True
    return report
