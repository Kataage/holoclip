"""Conservative OS-process-shared yt-dlp invocation gate for native collectors.

SQLite atomic reservations avoid bursts across independently started CLIs.
The gate NEVER evades site controls. Access restrictions pause later work.
All errors are fixed safe codes; no URL or raw yt-dlp stderr is stored.
"""
from __future__ import annotations

import math
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_GAP_SECONDS = 20.0
DEFAULT_COOLDOWN_SECONDS = 6 * 3600
MAX_PACING_WAIT_SECONDS = 60.0
PACING_SCHEMA_VERSION = "local-ytdlp-pacing-inspection-v1"
DEFAULT_PACING_PATH = Path("data/runtime/yt-dlp-access.sqlite3")
_COOLDOWN_REASONS = {
    "HTTP_RATE_LIMIT", "YOUTUBE_BOT_OR_INTERACTIVE_CHALLENGE",
    "UNKNOWN_HISTORICAL_COOLDOWN_REASON",
}
_MAX_UNIX_TIMESTAMP = 253402300799.0


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
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=4)
            try:
                # Serialize the legacy-v1 schema upgrade with reservations
                # from other local processes. Existing cooldowns survive.
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS access ("
                    "id INTEGER PRIMARY KEY CHECK(id=1),"
                    "next_at REAL NOT NULL,"
                    "active_until REAL NOT NULL,"
                    "cooldown_until REAL NOT NULL,"
                    "cooldown_reason TEXT NOT NULL DEFAULT "
                    "'UNKNOWN_HISTORICAL_COOLDOWN_REASON')"
                )
                fields = {
                    row[1] for row in connection.execute("PRAGMA table_info(access)")
                }
                if "cooldown_reason" not in fields:
                    connection.execute(
                        "ALTER TABLE access ADD COLUMN cooldown_reason TEXT "
                        "NOT NULL DEFAULT 'UNKNOWN_HISTORICAL_COOLDOWN_REASON'"
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO access "
                    "(id, next_at, active_until, cooldown_until) "
                    "VALUES (1, 0, 0, 0)"
                )
                connection.commit()
                return connection
            except Exception:
                connection.rollback()
                connection.close()
                raise
        except (sqlite3.Error, OSError) as exc:
            raise LocalYtDlpDeferred(
                "LOCAL_YTDLP_PACING_STORAGE_FAILURE"
            ) from exc

    @contextmanager
    def reserve(self, *, timeout_seconds: int):
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
            raise ValueError("Invalid yt-dlp pacing process timeout")
        token = None
        waited_seconds = 0.0
        try:
            while token is None:
                with closing(self._connect()) as db, db:
                    # BEGIN IMMEDIATE serializes reservation across local
                    # processes. Never sleep while holding a SQL write lock.
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
                        if waited_seconds + wait > MAX_PACING_WAIT_SECONDS:
                            raise LocalYtDlpDeferred(
                                "LOCAL_YTDLP_PACING_WAIT_TOO_LONG"
                            )
                    else:
                        token = now + timeout_seconds + 30
                        db.execute(
                            "UPDATE access SET next_at=?, active_until=? WHERE id=1",
                            (now + self.gap_seconds, token),
                        )
                        wait = 0.0
                if token is None:
                    # An unrelated process may take the slot while we sleep;
                    # bounded iterative rechecks avoid unbounded recursion.
                    time.sleep(wait)
                    waited_seconds += wait
            try:
                yield
            finally:
                with closing(self._connect()) as db, db:
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

    def impose_cooldown(
        self, reason: str = "UNKNOWN_HISTORICAL_COOLDOWN_REASON",
    ) -> None:
        if reason not in _COOLDOWN_REASONS:
            raise ValueError("Unsupported safe yt-dlp cooldown reason code")
        try:
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                now = time.time()
                db.execute(
                    "UPDATE access SET cooldown_until="
                    "MAX(cooldown_until, ?), cooldown_reason=? WHERE id=1",
                    (now + self.cooldown_seconds, reason),
                )
        except sqlite3.Error as exc:
            raise LocalYtDlpDeferred("LOCAL_YTDLP_PACING_STORAGE_FAILURE") from exc


def inspect_local_ytdlp_pacing(
    path: Path = DEFAULT_PACING_PATH, *, now_seconds: float | None = None,
) -> dict:
    """Read-only status; never creates a gate, acquires media or changes a lease.

    Timestamps are the earliest estimate from *saved local state*, not a
    promise that YouTube will allow traffic after the cooldown expires.
    """
    state = {
        "version": PACING_SCHEMA_VERSION,
        "pacing_status": "UNKNOWN_NO_LOCAL_PACING_RECORD",
        "cooldown_reason": None,
        "cooldown_until_utc": None,
        "minimum_gap_until_utc": None,
        "active_lease_until_utc": None,
        "next_attempt_not_before_utc": None,
        "seconds_until_not_before": None,
        "fresh_network_probes_attempted": False,
        "filesystem_mutated": False,
        "site_access_guaranteed": False,
        "ready": False,
        "production_ready": False,
    }

    def blocked() -> dict:
        return {**state, "pacing_status": "BLOCKED_UNREADABLE_LOCAL_PACING_STATE"}

    try:
        if (path.is_symlink() or path.parent.is_symlink()):
            return blocked()
        if not path.exists():
            return state
        if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            return blocked()
        clock = time.time() if now_seconds is None else now_seconds
        if (type(clock) not in (int, float)
                or not math.isfinite(clock)
                or not 0 <= clock <= _MAX_UNIX_TIMESTAMP):
            return blocked()

        # URI mode=ro prevents even initializing the DB schema. Legacy DBs
        # lacking the optional reason field must remain readable.
        uri = path.absolute().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, timeout=3, uri=True)) as db:
            fields = {
                row[1] for row in db.execute("PRAGMA table_info(access)")
            }
            required = {"next_at", "active_until", "cooldown_until"}
            if not required.issubset(fields):
                return blocked()
            has_reason = "cooldown_reason" in fields
            columns = (
                "next_at, active_until, cooldown_until, cooldown_reason"
                if has_reason else
                "next_at, active_until, cooldown_until"
            )
            rows = db.execute(
                "SELECT " + columns + " FROM access WHERE id=1"
            ).fetchall()
        if len(rows) != 1:
            return blocked()
        next_at, active, cooldown = rows[0][:3]
        if any(type(value) not in (float, int)
               or not math.isfinite(value)
               or not 0 <= value <= _MAX_UNIX_TIMESTAMP
               for value in (next_at, active, cooldown)):
            return blocked()
        cause = rows[0][3] if has_reason else (
            "UNKNOWN_HISTORICAL_COOLDOWN_REASON"
        )
        if cause not in _COOLDOWN_REASONS:
            return blocked()
        if cooldown > clock:
            status = "COOLDOWN_AFTER_YOUTUBE_ACCESS_RESTRICTION"
        elif active > clock:
            status = "ACTIVE_OTHER_LOCAL_YTDLP_PROCESS"
        elif next_at > clock:
            status = "WAITING_MINIMUM_LOCAL_ACCESS_GAP"
        else:
            status = "LOCAL_PACING_GATE_AVAILABLE"
        not_before = max(clock, next_at, active, cooldown)

        def stamp(value: float, *, only_future: bool = True) -> str | None:
            if only_future and value <= clock:
                return None
            return datetime.fromtimestamp(
                value, timezone.utc,
            ).isoformat().replace("+00:00", "Z")

        return {
            **state,
            "pacing_status": status,
            "cooldown_reason": cause if cooldown > clock else None,
            "cooldown_until_utc": stamp(cooldown),
            "minimum_gap_until_utc": stamp(next_at),
            "active_lease_until_utc": stamp(active),
            "next_attempt_not_before_utc": (
                stamp(not_before) if not_before > clock else None
            ),
            "seconds_until_not_before": (
                math.ceil(not_before - clock) if not_before > clock else 0
            ),
        }
    except (OSError, sqlite3.Error, OverflowError, ValueError):
        return blocked()
