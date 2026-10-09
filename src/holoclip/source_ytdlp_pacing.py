"""Conservative OS-process-shared yt-dlp invocation gate for native collectors.

SQLite atomic reservations avoid bursts across independently started CLIs.
The gate NEVER evades site controls. Access restrictions pause later work.
All errors are fixed safe codes; no URL or raw yt-dlp stderr is stored.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULT_GAP_SECONDS = 20.0
DEFAULT_COOLDOWN_SECONDS = 6 * 3600
MAX_PACING_WAIT_SECONDS = 60.0


class LocalYtDlpDeferred(RuntimeError):
    """Safe non-network result when another invocation/cooldown is active."""


class LocalYtDlpPacer:
    def __init__(
        self, path: Path, *, gap_seconds: float = DEFAULT_GAP_SECONDS,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        if (type(gap_seconds) not in (float, int)
                or not 10 <= gap_seconds <= 3600
                or type(cooldown_seconds) not in (float, int)
                or not 3600 <= cooldown_seconds <= 604800):
            raise ValueError("Unsafe yt-dlp pacing limits")
        self.path = path
        self.gap_seconds = float(gap_seconds)
        self.cooldown_seconds = float(cooldown_seconds)

    def _connect(self) -> sqlite3.Connection:
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise LocalYtDlpDeferred("LOCAL_YTDLP_PACING_UNSAFE_STATE")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=4)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS access ("
                "id INTEGER PRIMARY KEY CHECK(id=1),"
                "next_at REAL NOT NULL,"
                "active_until REAL NOT NULL,"
                "cooldown_until REAL NOT NULL)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO access VALUES (1, 0, 0, 0)"
            )
            connection.commit()
            return connection
        except Exception:
            connection.close()
            raise

    @contextmanager
    def reserve(self, *, timeout_seconds: int):
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
            raise ValueError("Invalid yt-dlp pacing process timeout")
        token = None
        try:
            with self._connect() as db:
                # BEGIN IMMEDIATE guarantees a one-at-a-time cross-process
                # reservation; sleeping occurs only *outside* SQL transactions.
                db.execute("BEGIN IMMEDIATE")
                now = time.time()
                next_at, active, cooldown = db.execute(
                    "SELECT next_at, active_until, cooldown_until "
                    "FROM access WHERE id=1"
                ).fetchone()
                if cooldown > now:
                    raise LocalYtDlpDeferred("LOCAL_YTDLP_COOLDOWN_ACTIVE")
                if active > now:
                    raise LocalYtDlpDeferred("LOCAL_YTDLP_OTHER_PROCESS_ACTIVE")
                if next_at > now:
                    wait = next_at - now
                    if wait > MAX_PACING_WAIT_SECONDS:
                        raise LocalYtDlpDeferred("LOCAL_YTDLP_PACING_WAIT_TOO_LONG")
                    # Commit by leaving context then retry from the outside.
                else:
                    token = now + timeout_seconds + 30
                    db.execute(
                        "UPDATE access SET next_at=?, active_until=? WHERE id=1",
                        (now + self.gap_seconds, token),
                    )
                    wait = 0.0
            if token is None:
                time.sleep(max(0.0, wait))
                # Recheck reservation after sleeping: another process may
                # have reserved the slot in the meantime.
                with self.reserve(timeout_seconds=timeout_seconds):
                    yield
                return
            try:
                yield
            finally:
                with self._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    now = time.time()
                    current = db.execute(
                        "SELECT active_until, next_at FROM access WHERE id=1"
                    ).fetchone()
                    # Never clear a different/newer invocation's reservation.
                    if current[0] == token:
                        db.execute(
                            "UPDATE access SET active_until=0, next_at=? WHERE id=1",
                            (max(current[1], now + self.gap_seconds),),
                        )
        except LocalYtDlpDeferred:
            raise
        except sqlite3.Error as exc:
            raise LocalYtDlpDeferred("LOCAL_YTDLP_PACING_STORAGE_FAILURE") from exc

    def impose_cooldown(self) -> None:
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                now = time.time()
                db.execute(
                    "UPDATE access SET cooldown_until="
                    "MAX(cooldown_until, ?) WHERE id=1",
                    (now + self.cooldown_seconds,),
                )
        except sqlite3.Error as exc:
            raise LocalYtDlpDeferred("LOCAL_YTDLP_PACING_STORAGE_FAILURE") from exc
