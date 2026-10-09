"""The hosted public-media smoke is a bounded real attempt, never fixture gold."""
import subprocess
from pathlib import Path

from holoclip import public_video_smoke as smoke

FLAGS = {
    "source_identity_verified": False,
    "reviewed_gold_available": False,
    "qualification_allowed": False,
    "ready": False,
    "production_ready": False,
}


def _inputs(*, samples=1, distinct=1, verified=1, complete=True):
    audit = {
        "version": "source-pattern-unattended-run-v1",
        "status": "MEASURED_UNREVIEWED_CONSTRUCTED_PUBLIC_VIDEO_PATTERNS",
        "result": {
            "new_unique_video_samples_this_run": samples,
            "new_distinct_candidate_streams_this_run": distinct,
            **FLAGS,
        },
        **FLAGS,
    }
    doctor = {
        "status": "RECORDED_UNREVIEWED_NEW_SOURCE_VIDEO_SAMPLES",
        "last_run_stage_funnel": {
            "candidate_visual_status_counts": {"UNKNOWN_NO_VISUAL_SEQUENCE": 1},
            "constructed_pattern_status_counts": {
                "MEASURED_UNREVIEWED_CONSTRUCTED_VIDEO_PATTERN_SUITE": 1,
            },
        },
        "current_source_cache_byte_attestation": {
            "status": "VERIFIED_ONLY_LOCAL_SOURCE_SECTION_BYTES_NOT_GOLD",
            "complete_pending_queue_audited": complete,
            "cache_section_status_counts": {
                "VERIFIED_LOCAL_SOURCE_SECTION_BYTES_AND_RECEIPT_NOT_GOLD": verified,
            },
            "actual_public_video_verified": False,
            **FLAGS,
        },
        **FLAGS,
    }
    return audit, doctor


def test_measured_public_media_with_revalidated_bytes_stays_unreviewed():
    audit, doctor = _inputs()
    result = smoke.summarize_smoke(
        return_code=0, timed_out=False, collector_audit=audit,
        diagnosis=doctor,
    )
    assert result["is_unreviewed_operational_smoke_success"] is True
    assert result["status"] == (
        "MEASURED_UNREVIEWED_CONSTRUCTED_SOURCE_PATTERNS_WITH_LOCAL_BYTES"
    )
    assert result["source_visual_outcomes"] == {"UNKNOWN_NO_VISUAL_SEQUENCE": 1}
    assert result["actual_public_video_verified"] is False
    assert result["source_identity_verified"] is False
    assert result["reviewed_gold_available"] is False
    assert result["qualification_allowed"] is False
    assert result["ready"] is False
    assert result["video_bytes_uploaded_to_actions"] is False


def test_no_media_or_no_verified_bytes_is_not_a_green_smoke():
    for samples, distinct, verified, complete in [
        (0, 0, 0, True), (2, 1, 0, True), (1, 0, 1, True),
        (1, 1, 1, False),
    ]:
        audit, doctor = _inputs(
            samples=samples, distinct=distinct, verified=verified, complete=complete,
        )
        report = smoke.summarize_smoke(
            return_code=0, timed_out=False, collector_audit=audit, diagnosis=doctor,
        )
        assert report["is_unreviewed_operational_smoke_success"] is False
        assert report["ready"] is False


def test_timeout_and_failed_native_command_are_not_recorded_as_success():
    audit, doctor = _inputs()
    for exit_code, timed_out, expected in [
        (124, True, "UNKNOWN_PUBLIC_VIDEO_SMOKE_DEADLINE"),
        (1, False, "UNKNOWN_PUBLIC_VIDEO_SMOKE_COMMAND_FAILURE"),
    ]:
        report = smoke.summarize_smoke(
            return_code=exit_code, timed_out=timed_out,
            collector_audit=audit, diagnosis=doctor,
        )
        assert report["status"] == expected
        assert report["is_unreviewed_operational_smoke_success"] is False


def test_forged_gold_and_missing_doctor_prevent_success():
    audit, doctor = _inputs()
    audit["result"]["ready"] = True
    result = smoke.summarize_smoke(
        return_code=0, timed_out=False, collector_audit=audit, diagnosis=doctor,
    )
    assert result["status"] == "BLOCKED_INVALID_OR_MISSING_PUBLIC_VIDEO_SMOKE_EVIDENCE"
    assert result["ready"] is False
    audit, doctor = _inputs()
    assert smoke.summarize_smoke(
        return_code=0, timed_out=False, collector_audit=audit, diagnosis=None,
    )["is_unreviewed_operational_smoke_success"] is False


def test_collector_invocation_has_fixed_small_budget_and_no_user_url(tmp_path, monkeypatch):
    calls = []

    class Finished:
        def __init__(self, command, **kwargs):
            calls.append((command, kwargs))

        def wait(self, timeout):
            calls.append(("wait", timeout))
            return 0

    monkeypatch.setattr(smoke.subprocess, "Popen", Finished)
    code, timed_out = smoke._invoke_bounded_collector(
        tmp_path, tmp_path / "output.log",
    )
    assert code == 0 and timed_out is False
    argv, opts = calls[0]
    for flag, value in [
        ("--cycles", "1"), ("--max-clips", "1"),
        ("--max-candidates", "1"), ("--scan-limit", "5"),
        ("--target-distinct-streams", "1"),
    ]:
        assert argv[argv.index(flag) + 1] == value
    assert not any("youtube.com" in arg for arg in argv)
    assert opts["start_new_session"] is True
    assert calls[1] == ("wait", smoke.TIMEOUT_SECONDS)


def test_timeout_kills_only_the_smoke_process_group(tmp_path, monkeypatch):
    terminated = []

    class Hanging:
        pid = 515151
        waits = 0

        def __init__(self, *_args, **_kwargs):
            pass

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("uv", timeout)
            return -9

    monkeypatch.setattr(smoke.subprocess, "Popen", Hanging)
    monkeypatch.setattr(
        smoke, "_linux_descendant_pids", lambda pid: [515153, 515152],
    )
    monkeypatch.setattr(
        smoke.os, "kill", lambda pid, sig: terminated.append((pid, sig)),
    )
    monkeypatch.setattr(
        smoke.os, "killpg",
        lambda pid, signum: terminated.append((pid, signum)),
    )
    code, timed_out = smoke._invoke_bounded_collector(
        tmp_path, tmp_path / "output.log",
    )
    assert (code, timed_out) == (124, True)
    assert terminated == [
        (515153, smoke.signal.SIGKILL),
        (515152, smoke.signal.SIGKILL),
        (515151, smoke.signal.SIGKILL),
    ]


def test_failed_uv_launch_still_writes_non_green_json(tmp_path, monkeypatch):
    import json

    if smoke.os.name != "posix":
        return  # The live workflow is explicitly Linux-only
    monkeypatch.chdir(tmp_path)

    def failed(*_args):
        raise FileNotFoundError("simulated uv not installed")

    monkeypatch.setattr(smoke, "_invoke_bounded_collector", failed)
    assert smoke.main() == 1
    report = json.loads(
        (tmp_path / "data/public-video-smoke/summary.json").read_text(
            encoding="utf-8",
        )
    )
    assert report["status"] == "UNKNOWN_PUBLIC_VIDEO_SMOKE_COMMAND_FAILURE"
    assert report["diagnostic_error_class"] == "FileNotFoundError"
    assert report["is_unreviewed_operational_smoke_success"] is False
    assert report["ready"] is False


def test_workflow_is_single_push_attempt_with_no_video_artifacts():
    yaml = (Path(__file__).resolve().parents[1] /
            ".github/workflows/public-video-smoke.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in yaml
    assert "  push:" in yaml
    assert "  schedule:" not in yaml
    assert "  pull_request:" not in yaml
    assert "timeout-minutes: 18" in yaml
    assert "permissions:\n  contents: read" in yaml
    assert "run: python -m holoclip.public_video_smoke" in yaml
    assert "data/public-video-smoke/summary.json" in yaml
    assert "data/public-video-smoke/collector.json" in yaml
    assert "data/public-video-smoke/doctor.json" in yaml
    assert "data/public-video-smoke/video/" not in yaml
    assert "secrets." not in yaml
    assert "retention-days: 7" in yaml
