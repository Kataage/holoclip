"""Private-LAN shared-IP pacing tests. Loopback only; never contacts YouTube."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip.providers.source import yt_dlp
from holoclip.source_ytdlp_lan_coordinator import (
    LanCoordinatorClient, _CoordinatorServer, _CoordinatorState,
    _signed, create_coordinator_secret,
)
from holoclip.source_ytdlp_pacing import LocalYtDlpDeferred


@contextmanager
def running_lan(tmp_path):
    secret_file = tmp_path / "private.secret"
    create_coordinator_secret(secret_file)
    secret = bytes.fromhex(secret_file.read_text(encoding="ascii").strip())
    state = tmp_path / "shared-gate.sqlite3"
    server = _CoordinatorServer(
        ("127.0.0.1", 0), secret, _CoordinatorState(state),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        yield url, secret_file, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_shared_coordinator_serializes_two_pcs_and_retains_inter_run_gap(tmp_path):
    with running_lan(tmp_path) as (url, secret, state):
        pc_a = LanCoordinatorClient(url, secret_file=secret)
        pc_b = LanCoordinatorClient(url, secret_file=secret)
        assert pc_a.status()["code"] == "AVAILABLE"
        with pc_a.reserve(timeout_seconds=30):
            assert pc_b.status()["code"] == "ACTIVE"
            with pytest.raises(LocalYtDlpDeferred, match="ACCESS_DEFERRED"):
                with pc_b.reserve(timeout_seconds=30):
                    pytest.fail("second PC ran while first held the shared lease")
        assert pc_b.status()["code"] == "WAIT_GAP"
        with pytest.raises(LocalYtDlpDeferred, match="ACCESS_DEFERRED"):
            with pc_b.reserve(timeout_seconds=30):
                pytest.fail("second PC bypassed shared 20-second gap")
        # Synthetic passage of time, never sleep 20 seconds or send media.
        with sqlite3.connect(state) as db:
            db.execute("UPDATE access SET next_at=0 WHERE id=1")
        with pc_b.reserve(timeout_seconds=30):
            assert pc_a.status()["code"] == "ACTIVE"


def test_signed_http_rejects_wrong_secret_and_replayed_nonce(tmp_path):
    with running_lan(tmp_path) as (url, secret_file, _state):
        bad_secret = tmp_path / "wrong.secret"
        create_coordinator_secret(bad_secret)
        with pytest.raises(LocalYtDlpDeferred, match="UNAVAILABLE"):
            LanCoordinatorClient(url, secret_file=bad_secret).status()
        real_secret = bytes.fromhex(secret_file.read_text().strip())
        payload = b'{"op":"status"}'
        nonce = "b" * 32
        stamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-Holoclip-Time": stamp,
            "X-Holoclip-Nonce": nonce,
            "X-Holoclip-Signature": _signed(real_secret, nonce, stamp, payload),
        }
        req = Request(url + "/request", data=payload, method="POST", headers=headers)
        with urlopen(req, timeout=3) as response:
            assert response.status == 200
        with pytest.raises(HTTPError) as exc:
            urlopen(req, timeout=3)
        assert exc.value.code == 403


@pytest.mark.parametrize("url", [
    "http://8.8.8.8:8878", "http://0.0.0.0:8878",
    "http://localhost:8878", "https://192.168.1.23:8878",
    "http://192.168.1.23:8878/other",
    "http://user:pass@192.168.1.23:8878",
    "http://192.168.1.23:80",
    "http://192.168.1.23:8878?token=foo",
])
def test_public_or_malformed_lan_coordinator_hosts_fail_closed(tmp_path, url):
    secret = tmp_path / "secret"
    create_coordinator_secret(secret)
    with pytest.raises(ValueError):
        LanCoordinatorClient(url, secret_file=secret)


def test_env_is_opt_in_and_partial_configuration_can_never_fallback(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("HOLOCLIP_YTDLP_COORDINATOR_URL", raising=False)
    monkeypatch.delenv("HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE", raising=False)
    assert LanCoordinatorClient.from_env() is None
    monkeypatch.setenv("HOLOCLIP_YTDLP_COORDINATOR_URL", "http://127.0.0.1:8878")
    with pytest.raises(LocalYtDlpDeferred, match="CONFIGURATION_INVALID"):
        LanCoordinatorClient.from_env()
    monkeypatch.setenv(
        "HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE", str(tmp_path / "missing"),
    )
    with pytest.raises(LocalYtDlpDeferred, match="CONFIGURATION_INVALID"):
        LanCoordinatorClient.from_env()


def test_unreachable_coordinator_blocks_all_video_metadata_without_fallback(
    tmp_path, monkeypatch,
):
    secret = tmp_path / "secret"
    create_coordinator_secret(secret)
    # Bind the server to a loopback ephemeral port then CLOSE it before probe.
    with running_lan(tmp_path / "other") as (url, _unused_secret, _state):
        pass
    monkeypatch.setenv("HOLOCLIP_YTDLP_COORDINATOR_URL", url)
    monkeypatch.setenv("HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE", str(secret))
    provider = yt_dlp.YtDlpSourceProvider(
        executable="yt-dlp", manage_updates=False,
        pacing_path=tmp_path / "pacing.db",
    )
    monkeypatch.setattr(
        yt_dlp, "_run_process",
        lambda *_a, **_kw: pytest.fail("never start yt-dlp without LAN permission"),
    )
    with pytest.raises(yt_dlp.YtDlpError, match="COORDINATOR_UNAVAILABLE"):
        provider.probe("https://www.youtube.com/watch?v=AAAAAAAAAAA")
    assert not (tmp_path / "pacing.db").exists()


def test_429_is_shared_across_both_pcs_with_no_immediate_retry(
    tmp_path, monkeypatch,
):
    with running_lan(tmp_path) as (url, secret, _state):
        monkeypatch.setenv("HOLOCLIP_YTDLP_COORDINATOR_URL", url)
        monkeypatch.setenv("HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE", str(secret))
        pc_a = yt_dlp.YtDlpSourceProvider(
            executable="yt-dlp", manage_updates=False,
            pacing_path=tmp_path / "a" / "gate.db",
        )
        pc_b = yt_dlp.YtDlpSourceProvider(
            executable="yt-dlp", manage_updates=False,
            pacing_path=tmp_path / "b" / "gate.db",
        )
        monkeypatch.setattr(pc_a.runtime, "command", lambda args: ["yt-dlp", *args])
        monkeypatch.setattr(pc_b.runtime, "command", lambda args: ["yt-dlp", *args])
        commands = []

        def rate_limited(cmd, **_kwargs):
            commands.append(cmd)
            raise yt_dlp.YtDlpError("HTTP Error 429: Too Many Requests")

        monkeypatch.setattr(yt_dlp, "_run_process", rate_limited)
        with pytest.raises(yt_dlp.YtDlpError, match="429"):
            pc_a.probe("https://youtu.be/AAAAAAAAAAA")
        assert len(commands) == 1
        assert pc_a.lan_coordinator.status()["code"] == "COOLDOWN"
        with pytest.raises(yt_dlp.YtDlpError, match="COORDINATOR_ACCESS_DEFERRED"):
            pc_b.probe("https://youtu.be/BBBBBBBBBBB")
        assert len(commands) == 1
        assert pc_a.pacer.path.exists()
        assert not pc_b.pacer.path.exists()


def test_crashed_coordinator_lease_survives_restarted_state_instance(tmp_path):
    state = _CoordinatorState(tmp_path / "central.sqlite3")
    first = state.execute({"op": "reserve", "timeout_seconds": 30})
    assert first["ok"] is True
    restarted = _CoordinatorState(state.path)
    assert restarted.execute({"op": "status"})["code"] == "ACTIVE"
    blocked = restarted.execute({"op": "reserve", "timeout_seconds": 30})
    assert blocked["ok"] is False
    # Token possession alone cannot release a different lease.
    assert restarted.execute({
        "op": "release", "lease_token": "c" * 64,
    })["ok"] is False
    assert restarted.execute({
        "op": "release", "lease_token": first["lease_token"],
    })["ok"] is True


def test_cli_secret_generation_never_prints_secret_or_overwrites(tmp_path):
    path = tmp_path / "data" / "secret.txt"
    runner = CliRunner()
    first = runner.invoke(cli.app, [
        "examples-ytdlp-lan-init", "--secret-file", str(path),
    ])
    assert first.exit_code == 0, first.output
    assert "LOCAL_LAN_COORDINATOR_SECRET_CREATED" in first.stdout
    secret = path.read_text().strip()
    assert len(secret) == 64
    assert secret not in first.stdout
    second = runner.invoke(cli.app, [
        "examples-ytdlp-lan-init", "--secret-file", str(path),
    ])
    assert second.exit_code != 0
    assert path.read_text().strip() == secret


def test_cli_remote_status_requires_authenticated_coordinator(
    tmp_path, monkeypatch,
):
    with running_lan(tmp_path) as (url, secret, _state):
        monkeypatch.setenv("HOLOCLIP_YTDLP_COORDINATOR_URL", url)
        monkeypatch.setenv("HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE", str(secret))
        result = CliRunner().invoke(cli.app, ["examples-ytdlp-lan-status"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "AUTHENTICATED_LOCAL_LAN_COORDINATOR"
        assert payload["remote_gate_status"] == "AVAILABLE"
        assert payload["ready"] is False
        assert str(secret) not in result.stdout
    failed = CliRunner().invoke(cli.app, ["examples-ytdlp-lan-status"])
    assert failed.exit_code == 6
    assert "COORDINATOR_UNAVAILABLE" in failed.stdout


def test_server_host_bind_public_is_rejected_prior_to_listening(tmp_path):
    from holoclip.source_ytdlp_lan_coordinator import serve_lan_ytdlp_coordinator

    with pytest.raises(ValueError, match="LAN"):
        serve_lan_ytdlp_coordinator(bind_ip="0.0.0.0", port=8878,
                                   secret_file=tmp_path / "missing")


def test_shared_coordinator_status_does_not_mean_real_video_qualification(tmp_path):
    with running_lan(tmp_path) as (url, secret, _state):
        client = LanCoordinatorClient(url, secret_file=secret)
        assert client.status()["code"] == "AVAILABLE"
        assert "video" not in json.dumps(client.status()).lower()
