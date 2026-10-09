"""Optional authenticated LAN yt-dlp coordination, entirely stdlib and offline-testable.

A single private-LAN coordinator serializes *yt-dlp subprocesses*, not clips.
Use on every PC sharing an outward IP; unconfigured PCs are OUTSIDE its gate.
Failure to authenticate/reach a configured coordinator fails closed.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from holoclip.source_ytdlp_pacing import LocalYtDlpDeferred

LAN_COORDINATOR_VERSION = "holoclip-lan-ytdlp-v1"
DEFAULT_COORDINATOR_STATE = Path("data/runtime/yt-dlp-lan-coordinator.sqlite3")
DEFAULT_SECRET_PATH = Path("data/runtime/yt-dlp-lan-secret.txt")
DEFAULT_CLIENT_CONFIG = Path("data/runtime/yt-dlp-lan-client.json")
CLIENT_CONFIG_VERSION = "holoclip-lan-client-v1"
_CLIENT_REQUIRED_MARKER = "holoclip-lan-client-required-v1\n"
_ENV_CONFIG = "HOLOCLIP_YTDLP_COORDINATOR_CONFIG"
_MAX_CLIENT_CONFIG_BYTES = 4096
_ALLOWED_REASONS = {"HTTP_RATE_LIMIT", "YOUTUBE_BOT_OR_INTERACTIVE_CHALLENGE"}
_ENV_URL = "HOLOCLIP_YTDLP_COORDINATOR_URL"
_ENV_SECRET = "HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE"
_MAX_REQUEST_BYTES = 1024
_GAP_SECONDS = 20
_COOLDOWN_SECONDS = 21600


def _private_ipv4(raw: str) -> str:
    try:
        addr = ipaddress.IPv4Address(raw)
    except ipaddress.AddressValueError as exc:
        raise ValueError("Coordinator requires a literal private/loopback IPv4") from exc
    if not (addr.is_loopback or addr in ipaddress.IPv4Network("10.0.0.0/8")
            or addr in ipaddress.IPv4Network("172.16.0.0/12")
            or addr in ipaddress.IPv4Network("192.168.0.0/16")):
        raise ValueError("Coordinator address must be a trusted LAN IPv4")
    return str(addr)


def _url(url: str) -> str:
    parts = urlsplit(url)
    if (parts.scheme != "http" or parts.username is not None or
            parts.password is not None or parts.path not in ("", "/")
            or parts.query or parts.fragment or parts.hostname is None):
        raise ValueError("Coordinator URL must be HTTP to a literal trusted LAN address")
    host = _private_ipv4(parts.hostname)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid coordinator port") from exc
    if port is None or not 1024 <= port <= 65535:
        raise ValueError("Coordinator port out of range")
    return f"http://{host}:{port}/request"


def _secret(path: Path) -> bytes:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Unsafe LAN coordinator secret symlink")
    if not path.is_file() or not 64 <= path.stat().st_size <= 128:
        raise ValueError("Missing or invalid LAN coordinator secret")
    raw = path.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[a-f0-9]{64}", raw):
        raise ValueError("Invalid LAN coordinator secret encoding")
    return bytes.fromhex(raw)


def create_coordinator_secret(path: Path) -> None:
    """One-time opt-in setup. Never overwrite an existing credential."""
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Unsafe LAN coordinator secret path")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(secrets.token_hex(32) + "\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _reject_linked_path(path: Path) -> None:
    # Check all existing parents, not just the immediate parent. Do not
    # accidentally follow a redirected credential/config directory.
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("Unsafe LAN client configuration path")


def _client_required_marker(path: Path) -> Path:
    return path.with_name(path.name + ".required")


def _validate_required_marker(path: Path) -> None:
    marker = _client_required_marker(path)
    _reject_linked_path(marker)
    if marker.exists() and marker.read_text(encoding="ascii") != _CLIENT_REQUIRED_MARKER:
        raise ValueError("Corrupt LAN client required marker")


def _client_config_path() -> tuple[Path, bool]:
    configured = os.environ.get(_ENV_CONFIG, "").strip()
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise ValueError("LAN client configuration override must be absolute")
        return path, True
    return DEFAULT_CLIENT_CONFIG, False


def _read_client_config(path: Path) -> tuple[str, Path]:
    _reject_linked_path(path)
    _validate_required_marker(path)
    if not path.is_file() or not 1 <= path.stat().st_size <= _MAX_CLIENT_CONFIG_BYTES:
        raise ValueError("Missing or oversized LAN client configuration")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or
            set(data) != {"version", "coordinator_url", "secret_file"} or
            data.get("version") != CLIENT_CONFIG_VERSION):
        raise ValueError("Unsupported LAN client configuration format")
    url = data["coordinator_url"]
    secret = data["secret_file"]
    if (not isinstance(url, str) or not isinstance(secret, str) or
            not secret or not Path(secret).is_absolute()):
        raise ValueError("Invalid LAN client configuration values")
    _url(url)
    _secret(Path(secret))
    return url, Path(secret)


def configure_lan_client(
    *, url: str, secret_file: Path, config_path: Path = DEFAULT_CLIENT_CONFIG,
    replace: bool = False,
) -> str:
    """Store a local non-secret client profile; never copy credential bytes."""
    import tempfile

    endpoint = _url(url)
    # Store a canonical URL; the request path is added by the client.
    canonical_url = endpoint.removesuffix("/request")
    secret_path = secret_file.absolute()
    target = config_path.absolute()
    _reject_linked_path(secret_path)
    _reject_linked_path(target)
    if target == secret_path:
        raise ValueError("Client configuration cannot replace its credential")
    _secret(secret_path)
    payload = {
        "version": CLIENT_CONFIG_VERSION,
        "coordinator_url": canonical_url,
        "secret_file": str(secret_path),
    }
    raw = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(raw) > _MAX_CLIENT_CONFIG_BYTES:
        raise ValueError("LAN client configuration is too large")
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_linked_path(target)
    _validate_required_marker(target)
    marker = _client_required_marker(target)
    if not marker.exists():
        # Persist the opt-in latch *before* the profile. A later deleted
        # profile can never silently resume local-only yt-dlp pacing.
        with os.fdopen(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w",
                       encoding="ascii") as handle:
            handle.write(_CLIENT_REQUIRED_MARKER)
    if target.exists():
        existing = target.read_bytes()
        if existing == raw:
            return "ALREADY_CONFIGURED"
        if not replace:
            raise ValueError("LAN client configuration exists; explicit replace required")
    if not replace:
        # O_EXCL guarantees another writer cannot silently replace a profile.
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
            handle.write(raw)
    else:
        # Atomic replacement avoids ever serving a partially rewritten profile.
        descriptor, temporary = tempfile.mkstemp(prefix=".lan-client-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
            _reject_linked_path(target)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return "CONFIGURED"


def _signed(secret: bytes, nonce: str, timestamp: str, body: bytes) -> str:
    message = nonce.encode("ascii") + b":" + timestamp.encode("ascii") + b":" + body
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LocalYtDlpDeferred("LOCAL_YTDLP_COORDINATOR_UNAVAILABLE")


class LanCoordinatorClient:
    def __init__(self, url: str, *, secret_file: Path):
        self.url = _url(url)
        self.secret = _secret(secret_file)
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    @classmethod
    def from_env(cls) -> LanCoordinatorClient | None:
        url = os.environ.get(_ENV_URL, "").strip()
        secret_path = os.environ.get(_ENV_SECRET, "").strip()
        try:
            if url or secret_path:
                # Any partial legacy environment override fails closed.
                # It can never silently bypass an installed LAN profile.
                if not url or not secret_path:
                    raise ValueError("Partial LAN coordinator environment")
                return cls(url, secret_file=Path(secret_path))
            config_path, explicitly_required = _client_config_path()
            marker = _client_required_marker(config_path)
            if (explicitly_required or config_path.exists() or config_path.is_symlink()
                    or marker.exists() or marker.is_symlink()):
                saved_url, saved_secret = _read_client_config(config_path)
                return cls(saved_url, secret_file=saved_secret)
            return None
        except (ValueError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LocalYtDlpDeferred(
                "LOCAL_YTDLP_COORDINATOR_CONFIGURATION_INVALID"
            ) from exc

    def _call(self, operation: str, **parameters) -> dict:
        payload = json.dumps(
            {"op": operation, **parameters}, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        nonce = secrets.token_hex(16)
        stamp = str(int(time.time()))
        request = Request(
            self.url, data=payload, method="POST", headers={
                "Content-Type": "application/json",
                "X-Holoclip-Time": stamp,
                "X-Holoclip-Nonce": nonce,
                "X-Holoclip-Signature": _signed(
                    self.secret, nonce, stamp, payload,
                ),
            },
        )
        try:
            with self._opener.open(request, timeout=4) as response:
                if response.status != 200:
                    raise LocalYtDlpDeferred("LOCAL_YTDLP_COORDINATOR_UNAVAILABLE")
                data = response.read(_MAX_REQUEST_BYTES + 1)
                remote_signature = response.headers.get(
                    "X-Holoclip-Response-Signature", "",
                )
            if len(data) > _MAX_REQUEST_BYTES or not hmac.compare_digest(
                remote_signature,
                _signed(self.secret, nonce, "response", data),
            ):
                raise LocalYtDlpDeferred(
                    "LOCAL_YTDLP_COORDINATOR_RESPONSE_UNTRUSTED"
                )
            reply = json.loads(data)
            if (not isinstance(reply, dict) or
                    reply.get("version") != LAN_COORDINATOR_VERSION or
                    type(reply.get("ok")) is not bool):
                raise ValueError("Bad response schema")
            return reply
        except LocalYtDlpDeferred:
            raise
        except (URLError, OSError, TimeoutError, ValueError,
                json.JSONDecodeError) as exc:
            raise LocalYtDlpDeferred(
                "LOCAL_YTDLP_COORDINATOR_UNAVAILABLE"
            ) from exc

    def status(self) -> dict:
        return self._call("status")

    @contextmanager
    def reserve(self, *, timeout_seconds: int):
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
            raise ValueError("Invalid remote media process timeout")
        response = self._call("reserve", timeout_seconds=timeout_seconds)
        if response.get("ok") is not True:
            raise LocalYtDlpDeferred("LOCAL_YTDLP_COORDINATOR_ACCESS_DEFERRED")
        lease = response.get("lease_token")
        if not isinstance(lease, str) or not re.fullmatch(r"[a-f0-9]{64}", lease):
            raise LocalYtDlpDeferred("LOCAL_YTDLP_COORDINATOR_RESPONSE_UNTRUSTED")
        try:
            yield
        finally:
            release = self._call("release", lease_token=lease)
            if release.get("ok") is not True:
                raise LocalYtDlpDeferred(
                    "LOCAL_YTDLP_COORDINATOR_RELEASE_UNCONFIRMED"
                )

    def impose_cooldown(self, reason: str) -> None:
        if reason not in _ALLOWED_REASONS:
            raise ValueError("Invalid LAN coordinator cooldown category")
        if self._call("cooldown", reason=reason).get("ok") is not True:
            raise LocalYtDlpDeferred(
                "LOCAL_YTDLP_COORDINATOR_COOLDOWN_UNCONFIRMED"
            )


class _CoordinatorState:
    """Central on-disk lease survives coordinator crashes and restarts."""

    def __init__(self, path: Path):
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("Unsafe LAN coordinator state symlink")
        self.path = path

    def execute(self, request: dict) -> dict:
        op = request.get("op")
        if op not in {"reserve", "release", "cooldown", "status"}:
            return {"ok": False, "code": "INVALID_OPERATION"}
        try:
            if self.path.is_symlink() or self.path.parent.is_symlink():
                return {"ok": False, "code": "UNSAFE_COORDINATOR_STORAGE"}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS access ("
                    "id INTEGER PRIMARY KEY CHECK(id=1),"
                    "next_at REAL NOT NULL, active_until REAL NOT NULL,"
                    "cooldown_until REAL NOT NULL, cooldown_reason TEXT NOT NULL,"
                    "lease_hash TEXT NOT NULL)"
                )
                db.execute(
                    "INSERT OR IGNORE INTO access VALUES "
                    "(1,0,0,0,'UNKNOWN_HISTORICAL_COOLDOWN_REASON','')"
                )
                row = db.execute(
                    "SELECT next_at, active_until, cooldown_until,"
                    "cooldown_reason, lease_hash FROM access WHERE id=1"
                ).fetchone()
                if row is None:
                    return {"ok": False, "code": "CORRUPT_COORDINATOR_STORAGE"}
                next_at, active, cooldown, reason, lease_hash = row
                # Coordinator identity is persisted in SQLite, independent of
                # process restarts, secret copies and individual clients.
                # It is NOT a machine attestation or an editor quality gate.
                db.execute(
                    "CREATE TABLE IF NOT EXISTS coordinator_identity ("
                    "id INTEGER PRIMARY KEY CHECK(id=1), instance_id TEXT NOT NULL)"
                )
                db.execute(
                    "INSERT OR IGNORE INTO coordinator_identity VALUES (1, ?)",
                    (secrets.token_hex(32),),
                )
                identity_row = db.execute(
                    "SELECT instance_id FROM coordinator_identity WHERE id=1"
                ).fetchone()
                instance_id = identity_row[0] if identity_row else None
                if (not isinstance(instance_id, str) or
                        not re.fullmatch("[a-f0-9]{64}", instance_id)):
                    return {"ok": False, "code": "CORRUPT_COORDINATOR_STORAGE"}
                now = time.time()
                if (any(type(v) not in (float, int) or
                        not 0 <= v <= 253402300799
                        for v in (next_at, active, cooldown))
                        or reason not in (
                            _ALLOWED_REASONS |
                            {"UNKNOWN_HISTORICAL_COOLDOWN_REASON"}
                        ) or not isinstance(lease_hash, str)):
                    return {"ok": False, "code": "CORRUPT_COORDINATOR_STORAGE"}
                if op == "status":
                    return {
                        "ok": True,
                        "code": ("COOLDOWN" if cooldown > now else
                                 "ACTIVE" if active > now else
                                 "WAIT_GAP" if next_at > now else "AVAILABLE"),
                        "coordinator_instance_id": instance_id,
                    }
                if op == "cooldown":
                    reason = request.get("reason")
                    if reason not in _ALLOWED_REASONS:
                        return {"ok": False, "code": "INVALID_COOLDOWN_REASON"}
                    db.execute(
                        "UPDATE access SET cooldown_until=MAX(cooldown_until,?),"
                        "cooldown_reason=? WHERE id=1",
                        (now + _COOLDOWN_SECONDS, reason),
                    )
                    return {"ok": True, "code": "COOLDOWN_SAVED"}
                if op == "release":
                    token = request.get("lease_token")
                    if (not isinstance(token, str) or
                            not re.fullmatch("[a-f0-9]{64}", token) or
                            not lease_hash or
                            not hmac.compare_digest(
                                hashlib.sha256(token.encode("ascii")).hexdigest(),
                                lease_hash,
                            )):
                        return {"ok": False, "code": "LEASE_NOT_OWNED"}
                    db.execute(
                        "UPDATE access SET active_until=0, lease_hash='',"
                        "next_at=MAX(next_at,?) WHERE id=1",
                        (now + _GAP_SECONDS,),
                    )
                    return {"ok": True, "code": "LEASE_RELEASED"}
                # New reservation, with both a *global* gap and an exclusive
                # active lease. A lost client leaves a lease until deadline.
                timeout = request.get("timeout_seconds")
                if type(timeout) is not int or not 1 <= timeout <= 3600:
                    return {"ok": False, "code": "INVALID_TIMEOUT"}
                if cooldown > now:
                    return {"ok": False, "code": "COOLDOWN"}
                if active > now:
                    return {"ok": False, "code": "ACTIVE"}
                if next_at > now:
                    return {"ok": False, "code": "WAIT_GAP"}
                lease = secrets.token_hex(32)
                db.execute(
                    "UPDATE access SET active_until=?, next_at=?,"
                    "lease_hash=? WHERE id=1",
                    (now + timeout + 30, now + _GAP_SECONDS,
                     hashlib.sha256(lease.encode("ascii")).hexdigest()),
                )
                return {"ok": True, "code": "LEASE_GRANTED",
                        "lease_token": lease}
        except (sqlite3.Error, OSError):
            return {"ok": False, "code": "COORDINATOR_STORAGE_FAILURE"}


class _CoordinatorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, secret: bytes, state: _CoordinatorState):
        super().__init__(address, _CoordinatorHandler)
        self.secret = secret
        self.state = state
        self.seen = {}
        self.nonce_lock = threading.Lock()


class _CoordinatorHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        # No operator-controlled payloads/headers/IPs enter logs.
        pass

    def do_POST(self):
        try:
            if self.path != "/request":
                self.send_error(404)
                return
            size_text = self.headers.get("Content-Length", "")
            if not size_text.isdecimal() or not 1 <= int(size_text) <= _MAX_REQUEST_BYTES:
                self.send_error(400)
                return
            nonce = self.headers.get("X-Holoclip-Nonce", "")
            stamp = self.headers.get("X-Holoclip-Time", "")
            signature = self.headers.get("X-Holoclip-Signature", "")
            if (not re.fullmatch("[a-f0-9]{32}", nonce)
                    or not stamp.isdecimal()
                    or abs(int(stamp) - int(time.time())) > 90):
                self.send_error(403)
                return
            data = self.rfile.read(int(size_text))
            if not hmac.compare_digest(
                signature, _signed(self.server.secret, nonce, stamp, data),
            ):
                self.send_error(403)
                return
            with self.server.nonce_lock:
                now = time.time()
                self.server.seen = {
                    k: t for k, t in self.server.seen.items() if now - t < 181
                }
                if nonce in self.server.seen or len(self.server.seen) >= 4096:
                    self.send_error(403)
                    return
                self.server.seen[nonce] = now
            request = json.loads(data)
            if not isinstance(request, dict):
                self.send_error(400)
                return
            result = self.server.state.execute(request)
            # Keyed, non-reversible tag of the *observed TCP peer*.
            # This never discloses the address or claims hardware identity.
            if request.get("op") == "status" and result.get("ok") is True:
                remote_address = str(self.client_address[0])
                result["observed_peer_tag"] = hmac.new(
                    self.server.secret, b"holoclip-peer-v1:" +
                    remote_address.encode("ascii"), hashlib.sha256,
                ).hexdigest()
            body = json.dumps(
                {"version": LAN_COORDINATOR_VERSION, **result},
                sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "X-Holoclip-Response-Signature",
                _signed(self.server.secret, nonce, "response", body),
            )
            self.end_headers()
            self.wfile.write(body)
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            self.send_error(400)
        except (OSError, ConnectionError):
            return


def serve_lan_ytdlp_coordinator(
    *, bind_ip: str = "127.0.0.1", port: int = 8878,
    secret_file: Path = DEFAULT_SECRET_PATH,
    state_path: Path = DEFAULT_COORDINATOR_STATE,
) -> None:
    host = _private_ipv4(bind_ip)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("LAN coordinator port must be 1024..65535")
    secret = _secret(secret_file)
    with _CoordinatorServer((host, port), secret, _CoordinatorState(state_path)) as server:
        server.serve_forever(poll_interval=0.3)
