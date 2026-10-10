"""Bootstrap dry-run, opt-in local changes, and public-repo credential protections."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip import source_ytdlp_lan_local_bootstrap as bootstrap
from holoclip.source_ytdlp_lan_coordinator import (
    _client_required_marker, _secret, configure_lan_client,
    create_coordinator_secret,
)


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in (
        "HOLOCLIP_YTDLP_COORDINATOR_URL",
        "HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE",
        "HOLOCLIP_YTDLP_COORDINATOR_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)


def _paths(tmp_path):
    folder = tmp_path / "data" / "runtime"
    return folder / "lan-secret.txt", folder / "lan-client.json"


def _go(secret, config, **kw):
    return bootstrap.bootstrap_local_lan(
        role="pc-a", coordinator_ip="192.168.1.10",
        secret_file=secret, config_file=config, **kw,
    )


def test_default_preview_does_not_touch_files(tmp_path, monkeypatch):
    secret, config = _paths(tmp_path)
    monkeypatch.setattr(bootstrap, "_secure_windows_path",
                        lambda *_a, **_kw: pytest.fail("ACL mutation in preview"))
    report = _go(secret, config)
    assert report["status"] == "LAN_BOOTSTRAP_PREVIEW"
    assert report["planned_actions"] == [
        "CREATE_NEW_PC_A_CREDENTIAL",
        "VERIFY_OR_RESTRICT_LOCAL_NTFS_ACL",
        "CREATE_LATCHED_LOCAL_CLIENT_PROFILE",
    ]
    assert report["changes_applied"] is False
    assert report["credential_generated"] is False
    assert report["ready"] is False and report["two_host_verified"] is False
    assert report["youtube_contacted"] is False
    assert not secret.exists() and not config.exists()
    assert not secret.parent.exists()


def test_pc_b_without_trusted_secret_refuses_apply_and_never_generates(tmp_path, monkeypatch):
    secret, config = _paths(tmp_path)
    monkeypatch.setattr(bootstrap, "_is_windows", lambda: True)
    result = bootstrap.bootstrap_local_lan(
        role="pc-b", coordinator_ip="192.168.1.10",
        secret_file=secret, config_file=config, apply=True,
    )
    assert result["status"] == "LAN_BOOTSTRAP_TRUSTED_CREDENTIAL_REQUIRED"
    assert not secret.exists()
    assert not config.exists()


def test_pc_a_apply_generates_secures_and_latches_local_client(tmp_path, monkeypatch):
    secret, config = _paths(tmp_path)
    monkeypatch.setattr(bootstrap, "_is_windows", lambda: True)
    calls = []
    def secure(path, *, apply):
        calls.append((path, apply))
        assert path.is_file()
        return True
    monkeypatch.setattr(bootstrap, "_secure_windows_path", secure)
    data = _go(secret, config, apply=True)
    assert data["status"] == "LAN_BOOTSTRAP_LOCAL_PREPARED"
    assert data["credential_generated"] is True
    assert data["ready"] is False
    assert len(_secret(secret)) == 32
    assert calls == [(secret, True)]
    assert config.exists()
    assert _client_required_marker(config).exists()
    payload = json.loads(config.read_text())
    assert payload["coordinator_url"] == "http://192.168.1.10:8878"
    assert payload["secret_file"] == str(secret)
    assert secret.read_text().strip() not in json.dumps(data)
    assert str(secret) not in json.dumps(data)
    assert data["task_installed"] is False
    assert data["firewall_modified"] is False


def test_idempotent_second_apply_never_rotates_or_replaces(tmp_path, monkeypatch):
    secret, config = _paths(tmp_path)
    monkeypatch.setattr(bootstrap, "_is_windows", lambda: True)
    monkeypatch.setattr(bootstrap, "_secure_windows_path", lambda *_a, **_kw: True)
    first = _go(secret, config, apply=True)
    raw = secret.read_bytes()
    profile = config.read_bytes()
    second = _go(secret, config, apply=True)
    assert first["credential_generated"] is True
    assert second["credential_generated"] is False
    assert second["client_profile_result"] == "ALREADY_CONFIGURED"
    assert secret.read_bytes() == raw
    assert config.read_bytes() == profile


def test_pc_b_with_imported_secret_can_enroll_without_overwriting(tmp_path, monkeypatch):
    secret, config = _paths(tmp_path)
    create_coordinator_secret(secret)
    original = secret.read_bytes()
    monkeypatch.setattr(bootstrap, "_is_windows", lambda: True)
    monkeypatch.setattr(bootstrap, "_secure_windows_path", lambda *_a, **_kw: True)
    data = bootstrap.bootstrap_local_lan(
        role="pc-b", coordinator_ip="192.168.1.10",
        secret_file=secret, config_file=config, apply=True,
    )
    assert data["status"] == "LAN_BOOTSTRAP_LOCAL_PREPARED"
    assert data["credential_generated"] is False
    assert data["credential_bytes_transferred"] is False
    assert secret.read_bytes() == original
    assert _client_required_marker(config).exists()


def test_windows_only_apply_and_no_writes_on_linux(tmp_path, monkeypatch):
    secret, config = _paths(tmp_path)
    monkeypatch.setattr(bootstrap, "_is_windows", lambda: False)
    with pytest.raises(ValueError, match="WINDOWS_APPLY_REQUIRED"):
        _go(secret, config, apply=True)
    assert not secret.exists() and not config.exists()


def test_failed_acl_cleans_up_only_new_secret(tmp_path, monkeypatch):
    secret, config = _paths(tmp_path)
    monkeypatch.setattr(bootstrap, "_is_windows", lambda: True)
    monkeypatch.setattr(bootstrap, "_secure_windows_path",
                        lambda *_a, **_kw: False)
    with pytest.raises(ValueError, match="CREDENTIAL_ACL_UNVERIFIED"):
        _go(secret, config, apply=True)
    assert not secret.exists()
    assert not config.exists()
    assert not _client_required_marker(config).exists()


def test_existing_acl_failure_never_removes_existing_secret(tmp_path, monkeypatch):
    secret, config = _paths(tmp_path)
    create_coordinator_secret(secret)
    original = secret.read_bytes()
    monkeypatch.setattr(bootstrap, "_is_windows", lambda: True)
    monkeypatch.setattr(bootstrap, "_secure_windows_path",
                        lambda *_a, **_kw: False)
    with pytest.raises(ValueError, match="CREDENTIAL_ACL_UNVERIFIED"):
        _go(secret, config, apply=True)
    assert secret.read_bytes() == original


def test_existing_different_profile_fails_before_any_acl_changes(tmp_path, monkeypatch):
    secret, config = _paths(tmp_path)
    create_coordinator_secret(secret)
    configure_lan_client(url="http://192.168.1.12:8878", secret_file=secret,
                         config_path=config)
    monkeypatch.setattr(bootstrap, "_is_windows", lambda: True)
    monkeypatch.setattr(bootstrap, "_secure_windows_path",
                        lambda *_a, **_kw: pytest.fail("should not mutate ACL"))
    with pytest.raises(ValueError, match="EXISTING_CONFIG_DIFFERS"):
        _go(secret, config, apply=True)
    assert json.loads(config.read_text())["coordinator_url"] == "http://192.168.1.12:8878"


def test_missing_config_with_required_latch_is_repaired_not_disabled(tmp_path, monkeypatch):
    secret, config = _paths(tmp_path)
    monkeypatch.setattr(bootstrap, "_is_windows", lambda: True)
    monkeypatch.setattr(bootstrap, "_secure_windows_path", lambda *_a, **_kw: True)
    _go(secret, config, apply=True)
    config.unlink()
    state = _go(secret, config, apply=True)
    assert state["status"] == "LAN_BOOTSTRAP_LOCAL_PREPARED"
    assert _client_required_marker(config).exists()


@pytest.mark.parametrize("invalid_ip", [
    "8.8.8.8", "127.0.0.1", "localhost", "0.0.0.0", "192.168.001.010",
])
def test_public_invalid_or_loopback_ip_rejected(tmp_path, invalid_ip):
    secret, config = _paths(tmp_path)
    with pytest.raises(ValueError):
        bootstrap.bootstrap_local_lan(role="pc-a", coordinator_ip=invalid_ip,
                                      secret_file=secret, config_file=config)
    assert not secret.exists()


def test_source_tree_paths_not_written(tmp_path):
    src = bootstrap._REPO / "src" / "holoclip" / "unsafe-secret.txt"
    config = tmp_path / "data" / "client.json"
    with pytest.raises(ValueError, match="SOURCE_TREE_PATH_FORBIDDEN"):
        bootstrap.bootstrap_local_lan(
            role="pc-a", coordinator_ip="192.168.1.10",
            secret_file=src, config_file=config,
        )


def test_symlink_secret_and_corrupt_existing_credential_fail_closed(tmp_path):
    secret, config = _paths(tmp_path)
    secret.parent.mkdir(parents=True)
    other = tmp_path / "other"
    other.write_text("bad")
    try:
        secret.symlink_to(other)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(ValueError):
        _go(secret, config)
    secret.unlink()
    secret.write_text("invalid")
    with pytest.raises(ValueError, match="EXISTING_CREDENTIAL_INVALID"):
        _go(secret, config)


def test_cli_default_preview_and_apply_requires_windows(tmp_path):
    secret, config = _paths(tmp_path)
    command = [
        "examples-ytdlp-lan-bootstrap-local", "--role", "pc-a",
        "--coordinator-ip", "192.168.1.10",
        "--secret-file", str(secret), "--config-file", str(config),
    ]
    preview = CliRunner().invoke(cli.app, command)
    assert preview.exit_code == 0, preview.output
    data = json.loads(preview.stdout)
    assert data["status"] == "LAN_BOOTSTRAP_PREVIEW"
    assert not secret.exists()
    if not bootstrap._is_windows():
        applied = CliRunner().invoke(cli.app, command + ["--apply"])
        assert applied.exit_code == 6
        info = json.loads(applied.stdout)
        assert info["reason"] == "LAN_BOOTSTRAP_WINDOWS_APPLY_REQUIRED"
        assert not secret.exists()
