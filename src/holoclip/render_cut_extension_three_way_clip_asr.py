"""Bounded A/B/B2 finished-clip ASR re-observation compared with source ASR.

A distinct transcription of actual edited audio can flag discrepancies, but
neither transcript is ground truth and no textual comparison proves intelligibility.
No source downloads; temporary cropped MP3s are deleted immediately afterward.
"""
from __future__ import annotations

import difflib
import math
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

from holoclip.models import TranscriptSegment
from holoclip.render_cut_extension_clip_asr_alignment import temporal_asr_review
from holoclip.render_cut_extension_three_way_acoustic import (
    _context as _verified_context,
    prepare_three_way_acoustic,
)
from holoclip.render_cut_extension_three_way_triage import _focus_windows
from holoclip.render_plan import _hash, _no_links
from holoclip.render_same_source_semantic_review import (
    save_semantic_review, validate_semantic_output,
)

VERSION = "holoclip-three-way-independent-clip-asr-review-v2"
_ROLES = ("a", "b", "b2")
_MAX_SECONDS = 8.0
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_TRANSCRIPT_SEGMENTS = 25
_MAX_ASR_CHARS = 700
_MAX_TEXT = 160
_MATCH_THRESHOLD = 0.78
_SHA = re.compile(r"[a-f0-9]{64}\Z")


def prepare_three_way_clip_asr(**kwargs) -> dict:
    """Use canonical A/B/B2 sources and finished media rather than uploaded JSON."""
    acoustic = prepare_three_way_acoustic(**kwargs)
    output = _no_links(acoustic["receipt"].with_name(
        f"{acoustic['dialogue']['review']['diagnostics']['finished']['b2']['output'].stem}"
        ".three-way-clip-asr.json"
    ))
    return {"acoustic": acoustic, "receipt": output}


def _basis(prepared: dict) -> tuple[dict, dict, dict]:
    observed, _diagnostic, pack, _digest = _verified_context(prepared["acoustic"])
    review = prepared["acoustic"]["dialogue"]["review"]
    focus = _focus_windows(review, observed)
    return observed, pack, focus


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", value)
    # Japanese has no mandatory word separators. Compare Unicode characters,
    # never use naïve English tokenization or pretend to have phoneme alignment.
    return "".join(c.casefold() for c in text
                   if not unicodedata.category(c).startswith(("P", "Z", "C")))


def _safe_asr(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("LOCAL_CLIP_ASR_TEXT_INVALID")
    if any(unicodedata.category(c) in {"Cc", "Cs", "Cf"} for c in value):
        raise ValueError("LOCAL_CLIP_ASR_TEXT_INVALID")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > _MAX_TEXT:
        raise ValueError("LOCAL_CLIP_ASR_TEXT_INVALID")
    return normalized


def _segments(rows: list[TranscriptSegment], duration: float) -> tuple[str, int]:
    if not isinstance(rows, list) or len(rows) > _MAX_TRANSCRIPT_SEGMENTS:
        raise ValueError("LOCAL_CLIP_ASR_SEGMENTS_INVALID")
    if not math.isfinite(duration) or not 0 < duration <= _MAX_SECONDS:
        raise ValueError("LOCAL_CLIP_ASR_DURATION_INVALID")
    texts = []
    previous = -1.0
    for item in rows:
        if not isinstance(item, TranscriptSegment):
            raise ValueError("LOCAL_CLIP_ASR_SEGMENTS_INVALID")
        start, end = item.start, item.end
        if (not all(math.isfinite(x) for x in (start, end))
                or not 0 <= start < end <= duration + .75
                or start < previous):
            raise ValueError("LOCAL_CLIP_ASR_CLOCK_INVALID")
        previous = start
        texts.append(_safe_asr(item.text))
    combined = _normalize(" ".join(texts))
    if len(combined) > _MAX_ASR_CHARS:
        raise ValueError("LOCAL_CLIP_ASR_TEXT_TOO_LONG")
    return combined, len(rows)


def _evidence_for_focus(excerpts: list[dict], window: dict) -> list[dict]:
    lo, hi = window["output_start_seconds"], window["output_end_seconds"]
    matched = []
    for item in excerpts:
        if (item.get("fully_contained_in_clip") is not True
                or item["output_start_seconds"] < lo - .002
                or item["output_end_seconds"] > hi + .002):
            continue
        text = _safe_asr(item["unverified_asr_text"])
        normalized = _normalize(text)
        if normalized:
            matched.append({
                "id": item["evidence_id"], "text": normalized,
                "output_start_seconds": item["output_start_seconds"],
                "output_end_seconds": item["output_end_seconds"],
            })
    return matched


def _compare_observation(
    *, source: list[dict], output_asr: str, output_segment_count: int,
) -> dict:
    comparisons = []
    for example in source:
        text = example["text"]
        # Repeated short Japanese phrases and ASR hallucinations are common:
        # store a similarity proxy only, never a verified presence or absence.
        ratio = difflib.SequenceMatcher(
            None, text, output_asr, autojunk=False,
        ).ratio() if output_asr else 0.0
        contained = text in output_asr if output_asr else False
        signal = ("TEXTUAL_OVERLAP_OBSERVED_UNVERIFIED"
                  if contained or ratio >= _MATCH_THRESHOLD else
                  "LOW_TEXTUAL_OVERLAP_REVIEW_REQUIRED")
        comparisons.append({
            "source_evidence_id": example["id"],
            "text_similarity_proxy": round(ratio, 3),
            "exact_normalized_containment": contained,
            "signal": signal,
            "spoken_words_verified": False,
            "source_asr_verified": False,
            "missing_speech_proven": False,
        })
    return {
        "recognized_segment_count": output_segment_count,
        "recognized_normalized_character_count": len(output_asr),
        "source_fully_contained_asr_excerpt_count": len(source),
        "comparison_count": len(comparisons),
        "low_overlap_review_count": sum(
            x["signal"] == "LOW_TEXTUAL_OVERLAP_REVIEW_REQUIRED"
            for x in comparisons
        ),
        "comparison": comparisons,
        "no_recognized_text_is_not_proof_of_silence": not bool(output_asr),
        "repeated_words_or_missing_phrases_not_independently_verified": True,
    }


def _read_clip(ffmpeg: str, source: Path, target: Path, *, start: float,
               end: float) -> None:
    if (not all(math.isfinite(x) for x in (start, end))
            or not 0 <= start < end <= 180
            or not .3 <= end - start <= _MAX_SECONDS):
        raise ValueError("LOCAL_CLIP_ASR_WINDOW_INVALID")
    args = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-n", "-ss", f"{start:.6f}", "-t", f"{end-start:.6f}",
        "-protocol_whitelist", "file,pipe", "-i", str(source),
        "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
        "-codec:a", "libmp3lame", "-b:a", "32k",
        "-map_metadata", "-1", "-f", "mp3", str(target),
    ]
    try:
        proc = subprocess.run(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
            timeout=35,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ValueError("LOCAL_CLIP_ASR_EXTRACT_UNAVAILABLE") from exc
    if (proc.returncode != 0 or not target.is_file()
            or not 512 <= target.stat().st_size <= _MAX_FILE_BYTES):
        raise ValueError("LOCAL_CLIP_ASR_EXTRACT_UNAVAILABLE")


def preview_clip_asr(prepared: dict) -> dict:
    """Local-only readiness, no decoder, ASR provider, text or file creation."""
    observed, pack, focus = _basis(prepared)
    return {
        "version": VERSION,
        "status": "LOCAL_CLIP_ASR_CROSSCHECK_PREVIEW_ONLY",
        "job_id": pack["job_id"],
        "trial_id": pack["trial_id"],
        "b2_id": pack["b2_id"],
        "source_asr_sha256": pack["source_asr_sha256"],
        "three_way_diagnostic_sha256": pack["three_way_diagnostic_sha256"],
        "review_plan_sha256": pack["review_plan_sha256"],
        "finished_mp4_sha256": {
            role: observed["roles"][role]["final_sha256"] for role in _ROLES
        },
        "expected_source_asr_excerpts_in_focus": {
            role: len(_evidence_for_focus(pack["variants"][role]["excerpts"],
                                          focus[role])) for role in _ROLES
        },
        "clip_audio_transcribed": False,
        "source_asr_is_not_ground_truth": True,
        "transcript_text_exported": False,
        "source_audio_independently_verified": False,
        "cross_transcription_is_not_word_accuracy": True,
        "missing_or_repeated_speech_confirmed": False,
        "editorial_quality_improvement": None,
        "best_variant": None,
        "human_editorial_qa_completed": False,
        "auto_adoption_allowed": False,
        "publication_performed": False,
        "ready": False, "production_ready": False,
    }


async def observe_three_way_clip_asr(prepared: dict, transcriber) -> dict:
    """Explicit public-data-only bounded extracts, rehash after each ASR call."""
    if os.getenv("HOLOCLIP_DATA_CLASS") != "public":
        raise ValueError("LOCAL_CLIP_ASR_EXPLICIT_PUBLIC_DATA_REQUIRED")
    output = validate_semantic_output(_no_links(prepared["receipt"]))
    observed, pack, focus = _basis(prepared)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("LOCAL_CLIP_ASR_FFMPEG_UNAVAILABLE")
    result = preview_clip_asr(prepared)
    role_results = {}
    # OS temporary directory, never placed next to original MP4 or source.
    with tempfile.TemporaryDirectory(prefix="holoclip-clip-asr-") as temporary:
        for role in _ROLES:
            path = _no_links(observed["roles"][role]["final"])
            digest = observed["roles"][role]["final_sha256"]
            if not _SHA.fullmatch(digest) or _hash(path)[0] != digest:
                raise ValueError("LOCAL_CLIP_ASR_FINISHED_MP4_CHANGED")
            window = focus[role]
            start, end = window["output_start_seconds"], window["output_end_seconds"]
            audio = Path(temporary) / f"{role}.mp3"
            _read_clip(ffmpeg, path, audio, start=start, end=end)
            if _hash(path)[0] != digest:
                raise ValueError("LOCAL_CLIP_ASR_VIDEO_CHANGED_DURING_EXTRACT")
            segments = await transcriber.transcribe_file(audio)
            heard, count = _segments(segments, end - start)
            expected = _evidence_for_focus(
                pack["variants"][role]["excerpts"], window,
            )
            temporal = temporal_asr_review(
                expected=expected, heard=segments,
                output_focus_start_seconds=start,
                output_focus_end_seconds=end, normalize=_normalize,
            )
            role_results[role] = {
                "output_focus_start_seconds": start,
                "output_focus_end_seconds": end,
                **_compare_observation(
                    source=expected, output_asr=heard, output_segment_count=count,
                ),
                "temporal_asr_review": temporal,
                "model_provenance": _safe_provenance(
                    getattr(transcriber, "last_provenance", None)
                ),
                "edited_audio_transcribed_from_local_crop": True,
                "human_auditory_check_completed": False,
            }
            # Keep only one audio crop on disk at a time; no third-party source
            # gets copied from local source archive in this command.
            audio.unlink()

    fresh_observed, fresh_pack, fresh_focus = _basis(prepared)
    if (fresh_pack != pack or fresh_focus != focus
            or fresh_observed["receipt_hashes"] != observed["receipt_hashes"]
            or fresh_observed["sibling_hashes"] != observed["sibling_hashes"]
            or fresh_observed["asr_sha256"] != observed["asr_sha256"]):
        raise ValueError("LOCAL_CLIP_ASR_LINEAGE_CHANGED_DURING_INFERENCE")
    for role in _ROLES:
        first, current = observed["roles"][role], fresh_observed["roles"][role]
        if (first["base_sha256"] != current["base_sha256"]
                or first["final_sha256"] != current["final_sha256"]
                or first["caption_sha256"] != current["caption_sha256"]
                or _hash(first["base"])[0] != first["base_sha256"]
                or _hash(first["final"])[0] != first["final_sha256"]):
            raise ValueError("LOCAL_CLIP_ASR_MEDIA_CHANGED_DURING_INFERENCE")
    result.update({
        "status": "LOCAL_CLIP_ASR_TEXTUAL_MISMATCH_HINTS_NOT_VERIFIED_SPEECH",
        "variants": role_results,
        "clip_audio_transcribed": True,
        "actual_finished_clip_audio_sent_to_asr_provider": True,
        "cross_transcription_is_not_word_accuracy": True,
        "missing_or_repeated_speech_confirmed": False,
    })
    save_semantic_review(result, output)
    return result


def _safe_provenance(provenance: object) -> dict:
    if not isinstance(provenance, dict):
        return {"provider": None, "model": None}
    def safe(value):
        if not isinstance(value, str) or not 0 < len(value) <= 120:
            return None
        if any(unicodedata.category(c) in {"Cc", "Cs", "Cf"} for c in value):
            return None
        return value
    return {"provider": safe(provenance.get("provider")),
            "model": safe(provenance.get("model"))}
