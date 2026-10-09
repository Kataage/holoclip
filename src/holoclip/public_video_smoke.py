"""One-shot, bounded public-source media smoke on a disposable Linux runner.

Real network/download attempts happen ONLY from the explicit workflow entrypoint.
Local-byte evidence remains *unreviewed*, never public source identity or gold.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

from holoclip.source_pattern_diagnostics import diagnose_source_pattern_collector

VERSION = "public-media-smoke-evidence-v1"
TIMEOUT_SECONDS = 12 * 60
_NO_GOLD = {
    "actual_public_video_verified": False,
    "source_identity_verified": False,
    "reviewed_gold_available": False,
    "qualification_allowed": False,
    "ready": False,
    "production_ready": False,
}


def _nonnegative(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


def summarize_smoke(
    *, return_code: int, timed_out: bool, collector_audit: dict | None,
    diagnosis: dict | None, diagnostic_error: str | None = None,
) -> dict:
    """Require both a measured new source sample and currently verified bytes.

    These are operational diagnostics only, not real source-match/editor gold.
    Do not promote a syntactically valid but unqualified run to green CI.
    """
    result = collector_audit.get("result") if isinstance(collector_audit, dict) else None
    cache = (diagnosis.get("current_source_cache_byte_attestation")
             if isinstance(diagnosis, dict) else None)
    new_samples = (
        _nonnegative(result.get("new_unique_video_samples_this_run"))
        if isinstance(result, dict) else None
    )
    new_streams = (
        _nonnegative(result.get("new_distinct_candidate_streams_this_run"))
        if isinstance(result, dict) else None
    )
    verified = (
        _nonnegative(cache.get("cache_section_status_counts", {}).get(
            "VERIFIED_LOCAL_SOURCE_SECTION_BYTES_AND_RECEIPT_NOT_GOLD", 0,
        ))
        if isinstance(cache, dict) else None
    )
    controls = (
        diagnosis.get("last_run_stage_funnel")
        if isinstance(diagnosis, dict) else None
    )
    trustworthy = (
        return_code == 0 and not timed_out
        and isinstance(collector_audit, dict)
        and collector_audit.get("result") is result
        and isinstance(result, dict)
        and all(collector_audit.get(k) is v for k, v in
                _NO_GOLD.items() if k != "actual_public_video_verified")
        and all(result.get(k) is v for k, v in
                _NO_GOLD.items() if k != "actual_public_video_verified")
        and isinstance(diagnosis, dict)
        and diagnosis.get("ready") is False
        and diagnosis.get("source_identity_verified") is False
        and diagnosis.get("reviewed_gold_available") is False
        and isinstance(cache, dict)
        and cache.get("ready") is False
        and cache.get("actual_public_video_verified") is False
        and cache.get("source_identity_verified") is False
        and cache.get("complete_pending_queue_audited") is True
        and diagnostic_error is None
    )
    if timed_out:
        status = "UNKNOWN_PUBLIC_VIDEO_SMOKE_DEADLINE"
    elif return_code != 0:
        status = "UNKNOWN_PUBLIC_VIDEO_SMOKE_COMMAND_FAILURE"
    elif not trustworthy or new_samples is None or new_streams is None or verified is None:
        status = "BLOCKED_INVALID_OR_MISSING_PUBLIC_VIDEO_SMOKE_EVIDENCE"
    elif new_samples > 0 and new_streams > 0 and verified > 0:
        status = "MEASURED_UNREVIEWED_CONSTRUCTED_SOURCE_PATTERNS_WITH_LOCAL_BYTES"
    else:
        status = "UNKNOWN_NO_NEW_MEASURED_AND_ATTESTED_PUBLIC_VIDEO_SAMPLES"
    report = {
        "version": VERSION,
        "status": status,
        "command_exit_code": return_code,
        "command_timed_out": timed_out,
        "collector_status": (collector_audit.get("status")
                             if isinstance(collector_audit, dict) else None),
        "collector_error_class": (collector_audit.get("error_class")
                                  if isinstance(collector_audit, dict) else None),
        "doctor_status": (diagnosis.get("status")
                          if isinstance(diagnosis, dict) else None),
        "diagnostic_error_class": diagnostic_error,
        "new_unique_source_video_pattern_samples": new_samples,
        "new_distinct_candidate_streams": new_streams,
        "verified_local_source_section_records": verified,
        "source_visual_outcomes": (
            controls.get("candidate_visual_status_counts", {})
            if isinstance(controls, dict) else {}
        ),
        "constructed_control_outcomes": (
            controls.get("constructed_pattern_status_counts", {})
            if isinstance(controls, dict) else {}
        ),
        "cache_audit_status": (
            cache.get("status") if isinstance(cache, dict) else None
        ),
        "limited_runner_network_environment": True,
        "video_bytes_uploaded_to_actions": False,
        **_NO_GOLD,
    }
    report["is_unreviewed_operational_smoke_success"] = (
        status == "MEASURED_UNREVIEWED_CONSTRUCTED_SOURCE_PATTERNS_WITH_LOCAL_BYTES"
    )
    return report


def _invoke_bounded_collector(workdir: Path, log_path: Path) -> tuple[int, bool]:
    """Terminate only the dedicated public-video child process group on timeout."""
    command = [
        "uv", "run", "--extra", "media", "holoclip",
        "examples-collect-source-patterns",
        "--cycles", "1", "--max-clips", "1", "--max-candidates", "1",
        "--scan-limit", "5", "--target-distinct-streams", "1",
        "--store", "data/public-video-smoke/human_clips.jsonl",
        "--state", "data/public-video-smoke/corpus.json",
        "--output", "data/public-video-smoke/video",
        "--report-path", "data/public-video-smoke/collector.json",
    ]
    with log_path.open("w", encoding="utf-8") as log:
        child = subprocess.Popen(
            command, cwd=workdir, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            return child.wait(timeout=TIMEOUT_SECONDS), False
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            return 124, True


def main() -> int:
    if os.name != "posix":
        raise RuntimeError("The disposable public-video smoke runs only on Linux")
    root = Path.cwd()
    data = root / "data" / "public-video-smoke"
    data.mkdir(parents=True, exist_ok=True)
    code, timed_out = _invoke_bounded_collector(root, data / "collector.log")
    audit = None
    audit_path = data / "collector.json"
    diagnostic_error = None
    if audit_path.is_file():
        try:
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            diagnostic_error = type(exc).__name__
    diagnosis = None
    try:
        diagnosis = diagnose_source_pattern_collector(
            state_path=data / "corpus.json",
            report_path=audit_path,
            launcher_path=data / "no-windows-launcher.json",
            check_tools=False,
            media_output=data / "video",
        )
        (data / "doctor.json").write_text(
            json.dumps(diagnosis, indent=2, ensure_ascii=False), encoding="utf-8",
        )
    except (OSError, ValueError, RuntimeError) as exc:
        diagnostic_error = type(exc).__name__
    result = summarize_smoke(
        return_code=code, timed_out=timed_out, collector_audit=audit,
        diagnosis=diagnosis, diagnostic_error=diagnostic_error,
    )
    (data / "summary.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    short = (f"Holoclip public-video smoke: {result['status']}\n"
             f"new patterns: {result['new_unique_source_video_pattern_samples']}\n"
             f"locally attested sections: {result['verified_local_source_section_records']}\n"
             "Source identity, human gold and production readiness: FALSE\n")
    print(short)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write("### Bounded public-video smoke (unreviewed)\n\n")
            handle.write(chr(96) + result["status"] + chr(96) + "\\n\\n")
            handle.write("Original-stream identity and editor gold remain unverified.\n")
    # A nominally exited collector with zero new real measurements is NOT green.
    return 0 if result["is_unreviewed_operational_smoke_success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
