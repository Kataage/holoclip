"""Strict trusted-LAN SSH import tests. No SSH server or YouTube calls."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from holoclip import cli
from holoclip.source_ytdlp_lan_coordinator import create_coordinator_secret
from holoclip.source_ytdlp_lan_ssh_transfer import pull_lan_secret_sftp


@pytest.fixture
def fixture_paths(tmp_path, monkeypatch):
    source = tmp_path / "remote-credential.txt"
    create_coordinator_secret(source)
    hosts = tmp_path / "known_hosts"
    hosts.write_text("192.168.1.20 ssh-ed25519 AAAAFakePinnedKeyInOfflineTest\n")
    target = tmp_path / "target" / "client.secret"
    observed = []

    def fake_run(cmd, *, input=None, **kwargs):
        observed.append((cmd, input, kwargs))
        # Never invoke an actual SSH client: emulate SFTP get output only.
        assert cmd[0] == "sftp-fake"
        assert "-b" in cmd
        assert cmd[cmd.index("-b") + 1] == "-"
        assert input is not None
        match = re.fullmatch(r'get "([^"]+)" "([^"]+)"\n', input)
        assert match is not None
        assert match.group(1) == "D:/AI/holoclip/data/runtime/yt-dlp-lan-secret.txt"
        if kwargs.get("check"):
            pytest.fail("SFTP call must not raise unchecked stderr")
        Path(match.group(2)).write_bytes(source.read_bytes())
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    from holoclip import source_ytdlp_lan_ssh_transfer as transfer

    monkeypatch.setattr(transfer.shutil, "which", lambda name: "sftp-fake")
    monkeypatch.setattr(transfer.subprocess, "run", fake_run)
    acl_seen = []

    def fake_acl(path, *, apply):
        acl_seen.append((path, apply))
        return True

    monkeypatch.setattr(transfer, "_secure_windows_path", fake_acl)
    return source, hosts, target, observed, acl_seen


def _pull(hosts, target, **extra):
    return pull_lan_secret_sftp(
        host="192.168.1.20", user="holoclip",
        remote_path="D:/AI/holoclip/data/runtime/yt-dlp-lan-secret.txt",
        known_hosts=hosts, secret_file=target, **extra,
    )


def test_pinned_sftp_uses_batch_key_only_and_hardened_staging(fixture_paths):
    source, hosts, target, observed, acl_seen = fixture_paths
    result = _pull(hosts, target)
    assert result["status"] == "LAN_SECRET_SFTP_IMPORTED"
    assert result["windows_staging_acl_verified"] is True
    assert result["windows_secret_acl_verified"] is True
    assert result["ready"] is False
    assert result["remote_coordinator_authenticated"] is False
    assert target.read_text().strip() == source.read_text().strip()
    assert len(observed) == 1
    cmd, batch, kwargs = observed[0]
    for option in (
        "BatchMode=yes", "StrictHostKeyChecking=yes",
        "UpdateHostKeys=no", "ForwardAgent=no", "ProxyCommand=none",
        "ProxyJump=none", "PreferredAuthentications=publickey",
        "PasswordAuthentication=no", "KbdInteractiveAuthentication=no",
    ):
        assert option in cmd
    assert "UserKnownHostsFile=" + str(hosts) in cmd
    assert "-P" in cmd and cmd[cmd.index("-P") + 1] == "22"
    assert cmd[-1] == "holoclip@192.168.1.20"
    assert kwargs["timeout"] == 75
    assert len(acl_seen) == 2
    assert acl_seen[0][1] is True
    assert acl_seen[1] == (target, True)
    assert source.read_text().strip() not in json.dumps(result)
    assert source.read_text().strip() not in " ".join(cmd) + batch
    assert list(target.parent.glob(".lan-sftp-*")) == []


def test_repull_is_idempotent_with_identical_secret(fixture_paths):
    source, hosts, target, observed, _acl_seen = fixture_paths
    assert _pull(hosts, target)["status"] == "LAN_SECRET_SFTP_IMPORTED"
    assert _pull(hosts, target)["status"] == "LAN_SECRET_SFTP_ALREADY_IMPORTED"
    assert len(observed) == 2
    assert target.read_text().strip() == source.read_text().strip()


def test_mismatched_secret_does_not_overwrite_target(fixture_paths):
    source, hosts, target, _observed, _acl_seen = fixture_paths
    target.parent.mkdir(parents=True)
    create_coordinator_secret(target)
    original = target.read_bytes()
    with pytest.raises(ValueError, match="Destination credential"):
        _pull(hosts, target)
    assert target.read_bytes() == original
    assert source.read_text().strip() != target.read_text().strip()
    assert list(target.parent.glob(".lan-sftp-*")) == []


@pytest.mark.parametrize("value, field", [
    ("8.8.8.8", "host"),
    ("0.0.0.0", "host"),
    ("somehost", "host"),
    ("name;bad", "user"),
    ("bad@user", "user"),
    ("relative/path.txt", "remote_path"),
    ("D:/safe/../secret", "remote_path"),
    ("D:/safe/file name", "remote_path"),
    ("D:/safe/*", "remote_path"),
    ("D:/safe/evil\n", "remote_path"),
])
def test_invalid_endpoints_never_launch_sftp(fixture_paths, value, field):
    _source, hosts, target, observed, _acl_seen = fixture_paths
    args = {
        "host": "192.168.1.20", "user": "holoclip",
        "remote_path": "D:/AI/holoclip/data/runtime/yt-dlp-lan-secret.txt",
        "known_hosts": hosts, "secret_file": target,
    }
    args[field] = value
    with pytest.raises(ValueError):
        pull_lan_secret_sftp(**args)
    assert not observed
    assert not target.exists()


def test_missing_known_hosts_never_falls_back_to_accept_new(fixture_paths):
    _source, hosts, target, observed, _acl_seen = fixture_paths
    hosts.unlink()
    with pytest.raises(ValueError, match="KNOWN_HOSTS"):
        _pull(hosts, target)
    assert not observed


def test_untrusted_symlinked_known_hosts_never_launches_sftp(fixture_paths, tmp_path):
    _source, hosts, target, observed, _acl_seen = fixture_paths
    link = tmp_path / "hosts-link"
    try:
        link.symlink_to(hosts)
    except (NotImplementedError, OSError):
        pytest.skip("symlink unavailable")
    with pytest.raises(ValueError, match="path"):
        _pull(link, target)
    assert not observed


def test_sftp_failure_is_sanitized_and_does_not_create_secret(fixture_paths, monkeypatch):
    source, hosts, target, _observed, _acl_seen = fixture_paths
    from holoclip import source_ytdlp_lan_ssh_transfer as transfer

    def failed(_cmd, **_kw):
        return SimpleNamespace(
            returncode=255,
            stdout="",
            stderr="secret=" + source.read_text().strip(),
        )

    monkeypatch.setattr(transfer.subprocess, "run", failed)
    response = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-pull-secret", "--host", "192.168.1.20",
        "--user", "holoclip",
        "--remote-path", "D:/AI/holoclip/data/runtime/yt-dlp-lan-secret.txt",
        "--known-hosts", str(hosts), "--secret-file", str(target),
    ])
    assert response.exit_code == 6
    assert json.loads(response.stdout)["status"] == "LAN_SECRET_SFTP_REJECTED"
    assert source.read_text().strip() not in response.stdout
    assert not target.exists()
    assert list(target.parent.glob(".lan-sftp-*")) == []


def test_sftp_timeout_never_creates_credential(fixture_paths, monkeypatch):
    _source, hosts, target, _observed, _acl_seen = fixture_paths
    from holoclip import source_ytdlp_lan_ssh_transfer as transfer

    def timed_out(_cmd, **_kw):
        raise subprocess.TimeoutExpired(cmd="fake", timeout=75)

    monkeypatch.setattr(transfer.subprocess, "run", timed_out)
    with pytest.raises(ValueError, match="LAN_SFTP_TRANSFER_FAILED"):
        _pull(hosts, target)
    assert not target.exists()


def test_acl_failure_cleans_up_new_secret(fixture_paths, monkeypatch):
    _source, hosts, target, _observed, _acl_seen = fixture_paths
    from holoclip import source_ytdlp_lan_ssh_transfer as transfer

    calls = []

    def fail_final(path, *, apply):
        calls.append(path)
        if path == target:
            raise ValueError("LAN_WINDOWS_ACL_NOT_SECURE")
        return True

    monkeypatch.setattr(transfer, "_secure_windows_path", fail_final)
    with pytest.raises(ValueError, match="LAN_WINDOWS_ACL_NOT_SECURE"):
        _pull(hosts, target)
    assert len(calls) == 2
    assert not target.exists()
    assert list(target.parent.glob(".lan-sftp-*")) == []


def test_local_acl_cli_non_windows_fails_closed(monkeypatch, tmp_path):
    from holoclip import cli as cli_module
    secret = tmp_path / "secret"
    create_coordinator_secret(secret)
    monkeypatch.setattr(cli_module, "_secure_windows_path", lambda *_a, **_kw: False)
    result = CliRunner().invoke(cli.app, [
        "examples-ytdlp-lan-secret-acl", "--secret-file", str(secret),
    ])
    assert result.exit_code == 6
    assert json.loads(result.stdout)["status"] == "WINDOWS_REQUIRED"
