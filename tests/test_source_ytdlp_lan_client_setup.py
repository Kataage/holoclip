"""LAN client onboarding tests: offline, loopback only; no public media."""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip.providers.source import yt_dlp
from holoclip.source_ytdlp_lan_coordinator import (
    CLIENT_CONFIG_VERSION,
    LanCoordinatorClient,
    _CoordinatorServer,
    _CoordinatorState,
    configure_lan_client,
    create_coordinator_secret,
)
from holoclip.source_ytdlp_pacing import LocalYtDlpDeferred


@pytest.fixture(autouse=True)
def clear_lan_overrides(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in (
        "HOLOCLIP_YTDLP_COORDINATOR_URL",
        "HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE",
        "HOLOCLIP_YTDLP_COORDINATOR_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)


@contextmanager
def local_server(tmp_path):
    secret = tmp_path / "a.secret"
    create_coordinator_secret(secret)
    server = _CoordinatorServer(
        ("127.0.0.1", 0),
        bytes.fromhex(secret.read_text(encoding="ascii").strip()),
        _CoordinatorState(tmp_path / "central.sqlite3"),
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", secret
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def test_client_onboarding_round_trip_and_authenticated_status(tmp_path):
    with local_server(tmp_path) as (url, secret):
        response = CliRunner().invoke(cli.app, [
            "examples-ytdlp-lan-configure", "--url", url,
            "--secret-file", str(secret),
        ])
        assert response.exit_code == 0, response.output
        payload = json.loads(response.stdout)
        assert payload["status"] == "CONFIGURED"
        assert payload["ready"] is False
        assert payload["remote_authentication_verified"] is False
        contents = Path("data/runtime/yt-dlp-lan-client.json").read_text()
        secret_bytes = secret.read_text().strip()
        assert secret_bytes not in contents
        assert secret_bytes not in response.stdout
        assert url not in response.stdout
        assert json.loads(contents) == {
            "version": CLIENT_CONFIG_VERSION,
            "coordinator_url": url,
            "secret_file": str(secret),
        }
        client = LanCoordinatorClient.from_env()
        assert client is not None
        assert client.status()["code"] == "AVAILABLE"
        remote = CliRunner().invoke(cli.app, ["examples-ytdlp-lan-status"])
        assert remote.exit_code == 0, remote.output
        assert json.loads(remote.stdout)["status"] == "AUTHENTICATED_LOCAL_LAN_COORDINATOR"
        assert secret_bytes not in remote.stdout
        again = CliRunner().invoke(cli.app, [
            "examples-ytdlp-lan-configure", "--url", url,
            "--secret-file", str(secret),
        ])
        assert again.exit_code == 0
        assert json.loads(again.stdout)["status"] == "ALREADY_CONFIGURED"


def test_profile_does_not_overwrite_without_explicit_replace(tmp_path):
    a = tmp_path / "a.secret"
    b = tmp_path / "b.secret"
    create_coordinator_secret(a)
    create_coordinator_secret(b)
    saved_path = tmp_path / "lan-client.json"
    assert configure_lan_client(
        url="http://192.168.1.20:8878", secret_file=a, config_path=saved_path,
    ) == "CONFIGURED"
    initial = saved_path.read_bytes()
    with pytest.raises(ValueError, match="replace"):
        configure_lan_client(
            url="http://192.168.1.21:8878", secret_file=b, config_path=saved_path,
        )
    assert saved_path.read_bytes() == initial
    assert configure_lan_client(
        url="http://192.168.1.21:8878", secret_file=b,
        config_path=saved_path, replace=True,
    ) == "CONFIGURED"
    assert json.loads(saved_path.read_text())["coordinator_url"] == "http://192.168.1.21:8878"
    assert a.read_text().strip() not in saved_path.read_text()
    assert b.read_text().strip() not in saved_path.read_text()


@pytest.mark.parametrize("url", [
    "https://192.168.1.5:8878", "http://8.8.8.8:8878",
    "http://0.0.0.0:8878", "http://localhost:8878",
    "http://192.168.1.5:8878/another",
    "http://192.168.1.5:8878?secret=x",
])
def test_unsafe_setup_url_rejected_before_writing(tmp_path, url):
    secret = tmp_path / "secret"
    create_coordinator_secret(secret)
    path = tmp_path / "client.json"
    response = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-configure", "--url", url,
        "--secret-file", str(secret), "--config-file", str(path),
    ])
    assert response.exit_code == 6
    assert json.loads(response.stdout)["status"] == "LOCAL_LAN_CLIENT_CONFIGURATION_REJECTED"
    assert not path.exists()


@pytest.mark.parametrize("bad_data", [
    "{", "[]", '{"version":"unknown","coordinator_url":"http://127.0.0.1:8878","secret_file":"x"}',
    '{"version":"holoclip-lan-client-v1","coordinator_url":"http://127.0.0.1:8878","secret_file":"x"}',
    '{"version":"holoclip-lan-client-v1","coordinator_url":"http://8.8.8.8:8878","secret_file":"/tmp/fake"}',
])
def test_bad_saved_profile_blocks_provider_before_yt_dlp(tmp_path, monkeypatch, bad_data):
    path = Path("data/runtime/yt-dlp-lan-client.json")
    path.parent.mkdir(parents=True)
    path.write_text(bad_data)
    monkeypatch.setattr(yt_dlp, "_run_process",
                        lambda *_args, **_kw: pytest.fail("unexpected yt-dlp execution"))
    with pytest.raises(LocalYtDlpDeferred, match="CONFIGURATION_INVALID"):
        LanCoordinatorClient.from_env()
    with pytest.raises(LocalYtDlpDeferred, match="CONFIGURATION_INVALID"):
        yt_dlp.YtDlpSourceProvider(
            executable="yt-dlp", manage_updates=False,
            pacing_path=tmp_path / "pacer.sqlite3",
        )
    assert not (tmp_path / "pacer.sqlite3").exists()


def test_partial_legacy_env_never_falls_back_to_saved_profile(tmp_path, monkeypatch):
    secret = tmp_path / "secret"
    create_coordinator_secret(secret)
    configure_lan_client(url="http://127.0.0.1:8878", secret_file=secret)
    monkeypatch.setenv("HOLOCLIP_YTDLP_COORDINATOR_URL", "http://127.0.0.1:8878")
    with pytest.raises(LocalYtDlpDeferred, match="CONFIGURATION_INVALID"):
        LanCoordinatorClient.from_env()
    monkeypatch.setenv("HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE", str(secret))
    assert LanCoordinatorClient.from_env() is not None


def test_explicit_custom_profile_is_required_even_when_missing(tmp_path, monkeypatch):
    path = tmp_path / "missing-client.json"
    monkeypatch.setenv("HOLOCLIP_YTDLP_COORDINATOR_CONFIG", str(path))
    with pytest.raises(LocalYtDlpDeferred, match="CONFIGURATION_INVALID"):
        LanCoordinatorClient.from_env()
    secret = tmp_path / "secret"
    create_coordinator_secret(secret)
    configure_lan_client(url="http://127.0.0.1:8878", secret_file=secret,
                         config_path=path)
    assert LanCoordinatorClient.from_env() is not None
    path.unlink()
    with pytest.raises(LocalYtDlpDeferred, match="CONFIGURATION_INVALID"):
        LanCoordinatorClient.from_env()


def test_symlinked_config_and_missing_secret_refused(tmp_path):
    secret = tmp_path / "secret"
    create_coordinator_secret(secret)
    path = tmp_path / "profile.json"
    with pytest.raises(ValueError):
        configure_lan_client(
            url="http://127.0.0.1:8878",
            secret_file=tmp_path / "missing", config_path=path,
        )
    assert not path.exists()
    if hasattr(os, "symlink"):
        pointed = tmp_path / "actual.json"
        pointed.write_text("{}")
        try:
            os.symlink(pointed, path)
        except OSError:
            pytest.skip("Symlink unavailable")
        with pytest.raises(ValueError, match="path"):
            configure_lan_client(
                url="http://127.0.0.1:8878",
                secret_file=secret, config_path=path,
            )


def test_no_profile_preserves_explicit_legacy_opt_in():
    assert LanCoordinatorClient.from_env() is None
