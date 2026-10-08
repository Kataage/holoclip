"""Offline partial source-to-finished audio evidence for reserved holdouts.

Never promotes a held-out benchmark, human gold, or a ready production route.
"""
from __future__ import annotations

import json
from pathlib import Path

from holoclip.audio_anchor_alignment import MATCH_SAMPLING_VERSION, measured_audio_alignment
from holoclip.clip_policy import PolicyDecision, PolicyOperation, evaluate_source_metadata
from holoclip.corpus_growth import CorpusGrowthState, _public
from holoclip.examples import human_clip_identity, load_human_clip_store
from holoclip.holdout_preparation import _measured, _candidates, prioritize_unprocessed
from holoclip.holdout_reservations import holdout_inventory
from holoclip.provider_benchmark import atomic_json
from holoclip.providers.source.base import AcquisitionProfile
from holoclip.providers.source.yt_dlp import choose_source_candidate
from holoclip.video_measurement import checksum_file

VERSION = "reserved-holdout-audio-alignment-v1"


def align_reserved_holdouts(provider, *, store: Path, state_path: Path,
                            prepared_root: Path, output: Path, policy_rules: list,
                            max_clips: int = 3, progress=None,
                            align=measured_audio_alignment) -> dict:
    """Align finished clips to full original *audio*, never downloading full source video.

    The finished video must already have passed prepare_reserved_holdouts. New
    policy/split conflicts are checked before any source audio network request.
    """
    if not 1 <= max_clips <= 20:
        raise ValueError("Audio alignment limit must be 1..20")
    prepared_path = prepared_root / "holdout-preparation.json"
    prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
    if (prepared.get("version") != "reserved-holdout-measurement-v1"
            or prepared.get("qualification_allowed") is not False
            or prepared.get("event_split_verified") is not False):
        raise ValueError("Expected unreviewed reserved finished-video preparation")
    state = CorpusGrowthState.model_validate_json(state_path.read_text(encoding="utf-8"))
    inventory = holdout_inventory(store, state_path)
    training = load_human_clip_store(store)
    learning_streams = {x.stream_id for x in training if x.stream_id}
    learning_clips = {human_clip_identity(x.clip_url) for x in training}
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "holdout-audio-alignment.json"
    old = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    records = old.get("records", {}) if old.get("version") == VERSION else {}
    result = {"version": VERSION, "status": "UNREVIEWED_AUDIO_EVIDENCE",
              "qualification_allowed": False, "benchmark_held_out": False,
              "event_split_verified": False, "reviewed_gold_available": False,
              "records": records}

    def save():
        atomic_json(report_path, result)

    attempts = 0
    for item in prioritize_unprocessed(
        _candidates(state), records,
        {"PARTIAL_AUDIO_MATCH", "INSUFFICIENT_UNIQUE_ANCHORS", "INSUFFICIENT_AUDIO"},
    ):
        if attempts >= max_clips:
            break
        clip_id = human_clip_identity(item.clip_url)
        base = {"clip_url": item.clip_url, "source_url": item.source_url,
                "source_stream_id": item.source_stream_id, "clip_id": clip_id,
                "qualification_allowed": False, "event_split_verified": False,
                "reviewed_gold_available": False}

        def block(status, reason):
            records[clip_id] = {**base, "status": status, "reason": reason}
            save()
            if progress:
                progress(f"holdout audio {clip_id}: {status}")

        # Refuse known collisions and missing/corrupt prepared-video fingerprints
        # BEFORE any network operation, including metadata probing.
        if (item.source_stream_id in learning_streams
                or item.source_stream_id in inventory["conflicted_streams"]
                or clip_id in learning_clips):
            block("BLOCKED_SPLIT_CONFLICT", "source/clip overlaps known learning history")
            continue
        if item.source_stream_id not in state.holdout_stream_ids or not item.source_url:
            block("BLOCKED_NOT_RESERVED", "source does not have a durable reservation")
            continue
        prepared_record = prepared.get("records", {}).get(clip_id)
        if (not prepared_record or prepared_record.get("status") != "PREPARED_UNREVIEWED"
                or prepared_record.get("source_stream_id") != item.source_stream_id
                or prepared_record.get("source_url") != item.source_url
                or prepared_record.get("clip_url") != item.clip_url
                or not _measured(prepared_record)):
            block("BLOCKED_UNVERIFIED_FINISHED_VIDEO", "reprepare and verify finished media")
            continue
        attempts += 1
        try:
            clip = provider.probe(item.clip_url)
            source = provider.probe(item.source_url)
            if not _public(clip) or not _public(source):
                block("QUARANTINED_NOT_PUBLIC_ENDED", "public/ended status changed")
                continue
            if (source.external_id != item.source_stream_id
                    or human_clip_identity(source.url) != human_clip_identity(item.source_url)
                    or human_clip_identity(clip.url) != clip_id):
                block("QUARANTINED_SOURCE_CHANGED", "source or finished clip identity changed")
                continue
            chosen = choose_source_candidate(provider.source_candidates(clip))
            if chosen is None or human_clip_identity(chosen.url) != human_clip_identity(item.source_url):
                block("QUARANTINED_SOURCE_AMBIGUOUS", "source link no longer unambiguous")
                continue
            cp = evaluate_source_metadata(
                clip, rules=policy_rules, operation=PolicyOperation.LEARN
            )
            sp = evaluate_source_metadata(
                source, rules=policy_rules, operation=PolicyOperation.LEARN
            )
            if cp.decision is not PolicyDecision.ALLOW or sp.decision is not PolicyDecision.ALLOW:
                block("QUARANTINED_POLICY", "clip or source requires policy review")
                continue
            source_audio = provider.acquire(
                item.source_url, output / "source_audio", profile=AcquisitionProfile.TRAINING_AUDIO
            )
            if (source_audio.metadata.external_id != item.source_stream_id
                    or human_clip_identity(source_audio.metadata.url)
                    != human_clip_identity(item.source_url)):
                block("QUARANTINED_ACQUIRED_SOURCE_IDENTITY", "source download identity changed")
                continue
            clip_media = Path(prepared_record["media_path"])
            source_media = Path(source_audio.media_path)
            clip_sha = checksum_file(clip_media)
            source_sha = checksum_file(source_media)
            old_record = records.get(clip_id, {})
            # Previous alignment is safe to reuse only with verified live policy,
            # the same media bytes, same method and source-stream split.
            if (old_record.get("status") in {"PARTIAL_AUDIO_MATCH", "INSUFFICIENT_UNIQUE_ANCHORS",
                                             "INSUFFICIENT_AUDIO"}
                    and old_record.get("clip_media_sha256") == clip_sha
                    and old_record.get("source_media_sha256") == source_sha
                    and old_record.get("source_stream_id") == item.source_stream_id
                    and old_record.get("fingerprint_version") == "audio-band-anchor-v1"
                    and old_record.get("match_sampling_version") == MATCH_SAMPLING_VERSION):
                if progress:
                    progress(f"holdout audio {clip_id}: reused checked alignment")
                continue
            evidence = align(clip_media, source_media, output / "fingerprints")
            records[clip_id] = {
                **base, **evidence, "status": evidence["status"],
                "clip_media_sha256": clip_sha, "source_media_sha256": source_sha,
                "fingerprint_version": "audio-band-anchor-v1",
                "match_sampling_version": MATCH_SAMPLING_VERSION,
                "source_audio_path": str(source_media),
                "policy_clip": cp.model_dump(mode="json"),
                "policy_source": sp.model_dump(mode="json"),
            }
            save()
            if progress:
                progress(f"holdout audio {clip_id}: {evidence['status']}")
        except Exception as exc:
            # Never persist raw exception messages or credentials to the report.
            records[clip_id] = {**base, "status": "RETRYABLE_FAILURE",
                                "error_class": type(exc).__name__}
            save()
            if progress:
                progress(f"holdout audio {clip_id}: RETRYABLE_FAILURE")
    save()
    counts = {}
    for row in records.values():
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"report_path": str(report_path), "attempted": attempts,
            "counts": counts, "qualification_allowed": False,
            "event_split_verified": False, "reviewed_gold_available": False}
