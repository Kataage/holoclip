"""Local safe runner for scheduled, bounded, unreviewed source video collection.

A platform-held advisory lock serializes the *collector* on a shared corpus
state path. This is not a lock for independent external writes to that state.
"""
from __future__ import annotations

import contextlib
import os
from datetime import datetime, timezone
from pathlib import Path

from holoclip.editorial_profile import _atomic_json
from holoclip.source_pattern_collection import collect_public_source_patterns

VERSION = "source-pattern-unattended-run-v1"
_NO_GOLD = {
    "source_identity_verified": False,
    "reviewed_gold_available": False,
    "qualification_allowed": False,
    "ready": False,
    "production_ready": False,
    "editor_cut_precision": None,
    "editor_cut_recall": None,
    "real_original_match_false_positive_rate": None,
}


@contextlib.contextmanager
def _state_lock(state_path: Path):
    """OS-released lock, including after process crashes.

    Keep the file on disk: unlinking while another process waits can produce
    an unsafe second inode on POSIX. Windows and POSIX both lock byte 0.
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock = state_path.parent / ".source-pattern-collector.lock"
    if lock.is_symlink():
        raise ValueError("Unsafe source-pattern collection lock symlink")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    handle = os.open(lock, flags, 0o600)
    acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(handle, 0, os.SEEK_SET)
                msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (OSError, BlockingIOError):
            pass
        yield acquired
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt
                os.lseek(handle, 0, os.SEEK_SET)
                msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)


def _safe_report_paths(*, state_path: Path, report_path: Path, store: Path,
                       output: Path) -> None:
    protected = (state_path, store)
    if any(report_path.resolve() == path.resolve() for path in protected):
        raise ValueError("Collection report must not overwrite corpus state or examples")
    if report_path.is_symlink() or report_path.with_suffix(
            report_path.suffix + ".tmp").is_symlink():
        raise ValueError("Unsafe collection report path symlink")
    if state_path.is_symlink():
        raise ValueError("Unsafe corpus state symlink")
    if output.resolve() == state_path.resolve():
        raise ValueError("Media output must not overwrite the corpus state")


async def run_locked_source_pattern_collection(
    provider, *, store: Path, state_path: Path, output: Path, report_path: Path,
    policy_rules: list, cycles: int = 3, max_clips: int = 2,
    max_candidates: int = 2, scan_limit: int = 20,
    target_distinct_streams: int = 3, discover: bool = True, progress=None,
) -> dict:
    """Persist audit of each scheduled run; never reuse failed run as success."""
    _safe_report_paths(
        state_path=state_path, report_path=report_path, store=store, output=output,
    )
    with _state_lock(state_path) as acquired:
        if not acquired:
            return {
                "version": VERSION,
                "status": "BLOCKED_CONCURRENT_COLLECTION",
                "report_path": str(report_path),
                **_NO_GOLD,
            }
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            result = await collect_public_source_patterns(
                provider, store=store, state_path=state_path, output=output,
                policy_rules=policy_rules, cycles=cycles, max_clips=max_clips,
                max_candidates=max_candidates, scan_limit=scan_limit,
                target_distinct_streams=target_distinct_streams,
                discover=discover, progress=progress,
            )
            if any(result.get(k) is not v for k, v in _NO_GOLD.items()):
                raise ValueError("Unsafe collected result claimed verified gold")
            audit = {
                "version": VERSION,
                "status": result["status"],
                "started_at_utc": started_at,
                "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                "result": result,
                **_NO_GOLD,
            }
            _atomic_json(report_path, audit)
            return {**result, "report_path": str(report_path)}
        except Exception as exc:
            # Crash/exception never fabricates a completed public measurement.
            _atomic_json(report_path, {
                "version": VERSION,
                "status": "UNKNOWN_COLLECTION_EXECUTION_FAILURE",
                "started_at_utc": started_at,
                "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                "error_class": type(exc).__name__,
                "result": None,
                **_NO_GOLD,
            })
            raise
