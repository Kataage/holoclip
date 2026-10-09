"""LAN onboarding tests use loopback only; never use YouTube or real PCs."""
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
    _CoordinatorServer, _CoordinatorState,
    configure_lan_client, create_coordinator_secret,
)
from holoclip.source_ytdlp_lan_onboarding import (
    diagnose_lan_client, import_lan_secret,
)


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in (
        "HOLOCLIP_YTDLP_COORDINATOR_URL",
        "HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE",
        "HOLOCLIP_YTDLP_COORDINATOR_CONFIG",
    ):
        monkeypatch.delenv(key, raising=False)


@contextmanager
def running_server(tmp_path):
    secret = tmp_path / "server.secret"
    create_coordinator_secret(secret)
    server = _CoordinatorServer(
        ("127.0.0.1", 0),
        bytes.fromhex(secret.read_text(encoding="ascii").strip()),
        _CoordinatorState(tmp_path / "state.sqlite3"),
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", secret
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def test_import_valid_secret_idempotently_without_credential_logging(tmp_path):
    source = tmp_path / "transport" / "imported.secret"
    create_coordinator_secret(source)
    dest = tmp_path / "client" / "private.secret"
    result = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-import-secret", "--source-file", str(source),
        "--secret-file", str(dest),
    ])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["status"] == "IMPORTED"
    assert data["windows_acl_verified"] is False
    assert source.read_bytes() == dest.read_bytes()
    assert source.read_text().strip() not in result.stdout
    again = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-import-secret", "--source-file", str(source),
        "--secret-file", str(dest),
    ])
    assert again.exit_code == 0, again.stdout
    assert json.loads(again.stdout)["status"] == "ALREADY_IMPORTED"


def test_different_existing_credential_never_overwritten(tmp_path):
    source, dest = tmp_path / "a", tmp_path / "b"
    create_coordinator_secret(source)
    create_coordinator_secret(dest)
    original = dest.read_bytes()
    outcome = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-import-secret", "--source-file", str(source),
        "--secret-file", str(dest),
    ])
    assert outcome.exit_code == 6
    assert dest.read_bytes() == original
    assert source.read_text().strip() not in outcome.stdout
    assert dest.read_text().strip() not in outcome.stdout


@pytest.mark.parametrize("bad_secret", ["", "a" * 64 + "extra", "X" * 64, "abc"])
def test_invalid_secret_rejected_without_creating_destination(tmp_path, bad_secret):
    source, dest = tmp_path / "source", tmp_path / "destination"
    source.write_text(bad_secret, encoding="ascii")
    report = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-import-secret", "--source-file", str(source),
        "--secret-file", str(dest),
    ])
    assert report.exit_code == 6
    assert not dest.exists()


def test_symlinked_source_and_target_rejected(tmp_path):
    source = tmp_path / "private.secret"
    create_coordinator_secret(source)
    link = tmp_path / "symlink.secret"
    try:
        link.symlink_to(source)
    except (NotImplementedError, OSError):
        pytest.skip("symlink not supported in this environment")
    with pytest.raises(ValueError, match="path"):
        import_lan_secret(source_file=link, secret_file=tmp_path / "out.secret")
    with pytest.raises(ValueError, match="path"):
        import_lan_secret(source_file=source, secret_file=link)
    assert source.exists()
    assert source.read_text().strip() not in str(diagnose_lan_client())


def test_offline_doctor_no_profile_returns_machine_readable_deferred(tmp_path, monkeypatch):
    monkeypatch.setattr(
        yt_dlp, "_run_process",
        lambda *_a, **_kw: pytest.fail("yt-dlp must not be started by LAN doctor"),
    )
    result = CliRunner().invoke(cli.app, ["examples-ytdlp-lan-doctor"])
    assert result.exit_code == 6
    data = json.loads(result.stdout)
    assert data["status"] == "NOT_CONFIGURED"
    assert data["authenticated"] is False
    assert data["youtube_contacted"] is False
    assert data["ready"] is False
    assert data["two_host_verified"] is False


def test_doctor_authenticated_status_never_reserves_a_lease(tmp_path, monkeypatch):
    monkeypatch.setattr(
        yt_dlp, "_run_process",
        lambda *_a, **_kw: pytest.fail("yt-dlp must not start"),
    )
    with running_server(tmp_path) as (url, source):
        local_secret = tmp_path / "pc-b.secret"
        assert import_lan_secret(source_file=source, secret_file=local_secret) == "IMPORTED"
        configure_lan_client(url=url, secret_file=local_secret)
        report = CliRunner().invoke(cli.app, ["examples-ytdlp-lan-doctor"])
        assert report.exit_code == 0, report.output
        data = json.loads(report.stdout)
        assert data["status"] == "AUTHENTICATED_LOCAL_LAN_COORDINATOR"
        assert data["authenticated"] is True
        assert data["remote_gate_status"] == "AVAILABLE"
        assert data["shared_ip_coordination_verified"] is False
        assert data["youtube_contacted"] is False
        assert data["ready"] is False
        assert source.read_text().strip() not in report.stdout
        assert url not in report.stdout
        # Backward-compatible status alias shares the same strict doctor.
        status = CliRunner().invoke(cli.app, ["examples-ytdlp-lan-status"])
        assert status.exit_code == 0
        assert json.loads(status.stdout)["remote_gate_status"] == "AVAILABLE"


def test_wrong_secret_or_disconnected_server_returns_safe_failure(tmp_path):
    with running_server(tmp_path) as (url, source):
        wrong = tmp_path / "wrong.secret"
        create_coordinator_secret(wrong)
        configure_lan_client(url=url, secret_file=wrong)
        report = diagnose_lan_client()
        assert report["status"] == "LOCAL_YTDLP_COORDINATOR_UNAVAILABLE"
        assert report["authenticated"] is False
        assert source.read_text().strip() not in json.dumps(report)
        assert wrong.read_text().strip() not in json.dumps(report)
    # No YouTube attempts after the server is gone.
    report = diagnose_lan_client()
    assert report["status"] == "LOCAL_YTDLP_COORDINATOR_UNAVAILABLE"


def test_missing_profile_latch_and_partial_env_report_safe_errors(tmp_path, monkeypatch):
    source = tmp_path / "secret"
    create_coordinator_secret(source)
    configure_lan_client(url="http://127.0.0.1:8878", secret_file=source)
    path = Path("data/runtime/yt-dlp-lan-client.json")
    path.unlink()
    result = CliRunner().invoke(cli.app, ["examples-ytdlp-lan-doctor"])
    assert result.exit_code == 6
    assert json.loads(result.stdout)["status"] == "LOCAL_YTDLP_COORDINATOR_CONFIGURATION_INVALID"
    monkeypatch.setenv("HOLOCLIP_YTDLP_COORDINATOR_URL", "http://127.0.0.1:8878")
    partial = CliRunner().invoke(cli.app, ["examples-ytdlp-lan-doctor"])
    assert partial.exit_code == 6
    assert json.loads(partial.stdout)["status"] == "LOCAL_YTDLP_COORDINATOR_CONFIGURATION_INVALID"


def test_doctor_refuses_invalid_signed_result_without_leaking_payload(monkeypatch):
    from holoclip.source_ytdlp_lan_onboarding import LanCoordinatorClient

    class MockClient:
        def status(self):
            return {"ok": True, "code": "INJECTED_VALUE", "secret": "do not print"}

    monkeypatch.setattr(LanCoordinatorClient, "from_env", lambda: MockClient())
    data = diagnose_lan_client()
    assert data["status"] == "LOCAL_YTDLP_COORDINATOR_RESPONSE_UNTRUSTED"
    assert "INJECTED_VALUE" not in json.dumps(data)
    assert "do not print" not in json.dumps(data)
