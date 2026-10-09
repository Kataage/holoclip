"""One-shot, bounded public-source media smoke on a disposable Linux runner.

Real network/download attempts happen ONLY from the explicit workflow entrypoint.
Local-byte evidence remains *unreviewed*, never public source identity or gold.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from collections import Counter
from pathlib import Path

from holoclip.corpus_growth import CorpusGrowthState

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


def _public_probe_failure_category(message: str) -> str:
    """Only finite categorical public-access diagnostics, no raw URL or log."""
    normalized = message.casefold()
    if any(token in normalized for token in (
        "not a bot", "sign in to confirm", "captcha", "bot detection",
        "unusual traffic",
    )):
        return "YOUTUBE_BOT_OR_INTERACTIVE_CHALLENGE"
    if any(token in normalized for token in (
        "http error 429", "status code 429", "too many requests", "rate limit",
    )):
        return "HTTP_RATE_LIMIT"
    if any(token in normalized for token in (
        "http error 403", "403 forbidden", "status code 403",
    )):
        return "HTTP_FORBIDDEN"
    if any(token in normalized for token in (
        "login required", "private video", "sign in to confirm your age",
    )):
        return "VIDEO_REQUIRES_LOGIN_OR_RESTRICTED"
    if any(token in normalized for token in (
        "video unavailable", "removed by the uploader", "has been removed",
    )):
        return "VIDEO_UNAVAILABLE"
    if any(token in normalized for token in (
        "timeout", "timed out", "deadline exceeded",
    )):
        return "NETWORK_OR_PROCESS_TIMEOUT"
    if any(token in normalized for token in (
        "certificate", "tls handshake", "ssl:",
    )):
        return "TLS_OR_CERTIFICATE_FAILURE"
    if any(token in normalized for token in (
        "unsupported url", "unable to extract", "unsupported extractor",
    )):
        return "EXTRACTOR_UNSUPPORTED_OR_CHANGED"
    if any(token in normalized for token in (
        "runtime refresh", "no js runtime", "javascript runtime",
        "neither uv/uvx",
    )):
        return "LOCAL_EXTRACTOR_RUNTIME_FAILURE"
    return "UNCLASSIFIED_METADATA_PROBE_FAILURE"


def _saved_metadata_probe_failures(state_path: Path) -> dict:
    """Count failed public clip probes without exposing source URLs or stderr."""
    result = {
        "retryable_clip_records": 0,
        "exception_class_counts": {},
        "failure_category_counts": {},
        "raw_error_text_included": False,
    }
    if not state_path.is_file():
        return result
    state = CorpusGrowthState.model_validate_json(
        state_path.read_text(encoding="utf-8"),
    )
    kinds: Counter[str] = Counter()
    groups: Counter[str] = Counter()
    for item in state.items.values():
        if item.status != "retryable":
            continue
        result["retryable_clip_records"] += 1
        reason = item.reason if isinstance(item.reason, str) else ""
        name, separator, message = reason.partition(":")
        name = name.strip()
        # Recorded exception class must never contain an untrusted URL/title.
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", name):
            name = "UNKNOWN_EXCEPTION_CLASS"
        kinds[name] += 1
        groups[_public_probe_failure_category(
            message if separator else reason,
        )] += 1
    result["exception_class_counts"] = dict(sorted(kinds.items()))
    result["failure_category_counts"] = dict(sorted(groups.items()))
    return result


def summarize_smoke(
    *, return_code: int, timed_out: bool, collector_audit: dict | None,
    diagnosis: dict | None, diagnostic_error: str | None = None,
    metadata_probe_failures: dict | None = None,
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
        "public_clip_metadata_probe_failures": metadata_probe_failures,
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


def _linux_descendant_pids(root_pid: int) -> list[int]:
    """Snapshot same-user /proc children, deepest first, with no shell commands.

    yt-dlp creates its *own* process group for FFmpeg. Killing only the uv
    process group does not kill those nested sessions. Snapshot the exact
    descendants BEFORE stopping the parent so no task runner, neighboring
    job or unrelated user's process can be selected by broad name matching.
    """
    children: dict[int, list[int]] = {}
    try:
        paths = list(Path("/proc").iterdir())
    except OSError:
        return []
    for path in paths:
        if not path.name.isdecimal():
            continue
        try:
            values = {}
            with (path / "status").open(encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith(("PPid:", "Uid:")):
                        key, value = line.split(":", 1)
                        values[key] = value.strip().split()[0]
            if int(values["Uid"]) != os.getuid():
                continue
            ppid = int(values["PPid"])
            pid = int(path.name)
        except (OSError, ValueError, KeyError, IndexError):
            continue
        children.setdefault(ppid, []).append(pid)
    order = []
    to_visit = [root_pid]
    seen = {root_pid}
    while to_visit:
        current = to_visit.pop()
        for child in children.get(current, ()):
            if child not in seen:
                seen.add(child)
                order.append(child)
                to_visit.append(child)
    # Child-first so the launchers do not orphan their independently-grouped
    # yt-dlp / FFmpeg children before those PIDs can be signaled.
    return list(reversed(order))


def _terminate_smoke_tree(root_pid: int) -> None:
    for pid in _linux_descendant_pids(root_pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        os.killpg(root_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _invoke_bounded_collector(workdir: Path, log_path: Path) -> tuple[int, bool]:
    """Stop nested yt-dlp/FFmpeg descendants and the dedicated uv group."""
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
            _terminate_smoke_tree(child.pid)
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
    diagnostic_error = None
    try:
        code, timed_out = _invoke_bounded_collector(
            root, data / "collector.log",
        )
    except (OSError, RuntimeError) as exc:
        # uv/tool launch failures must still yield a machine-readable,
        # non-green artifact rather than disappearing before upload.
        code, timed_out = 127, False
        diagnostic_error = type(exc).__name__
    audit = None
    audit_path = data / "collector.json"
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
    metadata_failures = None
    try:
        metadata_failures = _saved_metadata_probe_failures(
            data / "corpus.json",
        )
    except (OSError, ValueError, UnicodeError) as exc:
        diagnostic_error = type(exc).__name__
    result = summarize_smoke(
        return_code=code, timed_out=timed_out, collector_audit=audit,
        diagnosis=diagnosis, diagnostic_error=diagnostic_error,
        metadata_probe_failures=metadata_failures,
    )
    (data / "summary.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    short = (f"Holoclip public-video smoke: {result['status']}\n"
             f"new patterns: {result['new_unique_source_video_pattern_samples']}\n"
             f"locally attested sections: {result['verified_local_source_section_records']}\n"
             "Source identity, human gold and production readiness: FALSE\n")
    if isinstance(metadata_failures, dict) and metadata_failures[
        "retryable_clip_records"
    ]:
        print("metadata probe failure categories:",
              metadata_failures["failure_category_counts"])
    print(short)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write("### Bounded public-video smoke (unreviewed)\n\n")
            handle.write(chr(96) + result["status"] + chr(96) + "\n\n")
            handle.write("Original-stream identity and editor gold remain unverified.\n")
    # A nominally exited collector with zero new real measurements is NOT green.
    return 0 if result["is_unreviewed_operational_smoke_success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
