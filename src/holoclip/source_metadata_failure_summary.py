"""Sanitized local classification of YouTube metadata probe failures.

No URLs, raw yt-dlp stderr, media, credentials or clip titles leave this module.
The categories are diagnostic only, never a bypass or source match claim.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from holoclip.corpus_growth import CorpusGrowthState


def _public_probe_failure_category(message: str) -> str:
    """Only finite categorical public-access diagnostics, no raw URL or log."""
    normalized = message.casefold()
    if any(token in normalized for token in (
        "login required", "private video", "sign in to confirm your age",
    )):
        return "VIDEO_REQUIRES_LOGIN_OR_RESTRICTED"
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


