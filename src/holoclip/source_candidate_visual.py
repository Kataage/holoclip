"""Bounded visual probes of unresolved source shortlists, with no source promotion.

Search metadata is not proof. Video probes examine only short, policy-checked
public sections; sparse frame matches are unreviewed and cannot supply
original video PTS, verified source identity, editor gold, or a learning split.
"""
from __future__ import annotations

import math
from pathlib import Path

from holoclip.clip_policy import (
    PolicyDecision, PolicyOperation, evaluate_source_metadata,
    evaluate_source_ranges,
)
from holoclip.corpus_growth import CorpusGrowthState, _public
from holoclip.editorial_profile import _atomic_json
from holoclip.examples import human_clip_identity, load_human_clip_store
from holoclip.providers.source.base import SourceMetadata
from holoclip.source_candidate_scout import VERSION as SCOUT_VERSION
from holoclip.video_measurement import checksum_file, measure_window, probe_video
from holoclip.visual_edit_evidence import _compare_features, _orb_features

VERSION = "bounded-shortlist-video-probes-v1"
WINDOW_SECONDS = 12.
MAX_CLIPS = 2
MAX_CANDIDATES = 2
SOURCE_WINDOWS = 3
MAX_FRAMES_PER_WINDOW = 18
MAX_FINISHED_FRAMES = 12
MAX_PAIRS = 2000
MAX_BYTES = 64 * 1024 * 1024

_NO_GOLD = {
    "source_identity_verified": False,
    "original_video_absolute_pts_verified": False,
    "frame_accurate_cuts_verified": False,
    "reviewed_gold_available": False,
    "qualification_allowed": False,
    "event_split_verified": False,
    "used_in_learning_or_holdout": False,
}


def _starts(duration: float) -> list[float]:
    if (type(duration) not in (float, int)
            or not math.isfinite(duration) or duration < 300):
        return []
    if duration > 24 * 3600:
        return []
    return [round(v, 3) for v in (
        0., (duration - WINDOW_SECONDS) / 2, duration - WINDOW_SECONDS,
    )]


def _measured_frames(window, max_frames: int) -> list:
    frames = sorted(window.frames, key=lambda f: (f.pts_seconds, f.id))
    if len(frames) <= max_frames:
        return frames
    if max_frames < 2:
        raise ValueError("Frame budget cannot establish a sequence")
    return [frames[(i * (len(frames)-1)) // (max_frames-1)]
            for i in range(max_frames)]


def compare_sparse_visual_sequences(
    finished, sources: list, *, extract=_orb_features,
    compare=_compare_features,
) -> dict:
    """Corroborate 3 distinct, temporally coherent sampled frames at most.

    Each source window has *local re-encoded* frame timestamps. Never compare
    across separate original windows as though their native PTS were continuous.
    """
    result = {
        "status": "UNKNOWN_NO_VISUAL_SEQUENCE", "matched_frames": 0,
        "examined_frame_comparisons": 0, "matched_source_window": None,
        "source_frame_times_local": [], "finished_frame_times_local": [],
        **_NO_GOLD,
    }
    dst = _measured_frames(finished, MAX_FINISHED_FRAMES)
    groups = [_measured_frames(w, MAX_FRAMES_PER_WINDOW) for w in sources]
    comparisons = len(dst) * sum(len(g) for g in groups)
    if comparisons > MAX_PAIRS:
        result["status"] = "UNKNOWN_VISUAL_COMPARISON_BUDGET"
        return result
    if len(dst) < 3 or not any(len(g) >= 3 for g in groups):
        return result
    result["examined_frame_comparisons"] = comparisons
    try:
        source_features = {f.id: extract(f.locator) for group in groups for f in group}
        finished_features = {f.id: extract(f.locator) for f in dst}
    except (OSError, ValueError):
        result["status"] = "UNKNOWN_IMAGE_DECODE"
        return result
    # Each finished frame must have one unambiguous location among *all*
    # sampled source sections. Repeated gameplay/HUD frames are not anchors.
    matches = []
    for frame in dst:
        ranked = []
        for group_idx, group in enumerate(groups):
            for other in group:
                observed = compare(source_features[other.id], finished_features[frame.id])
                if (isinstance(observed, dict)
                        and type(observed.get("score")) in (float, int)
                        and math.isfinite(observed["score"])
                        and .25 <= observed["score"] <= 1):
                    ranked.append((observed["score"], group_idx, other))
        ranked.sort(key=lambda x: (-x[0], x[1], x[2].pts_seconds, x[2].id))
        if not ranked:
            continue
        strength, group_idx, best = ranked[0]
        different = [
            x[0] for x in ranked[1:]
            if x[1] != group_idx or abs(x[2].pts_seconds - best.pts_seconds) > 1
        ]
        if different and strength - max(different) < .09:
            continue
        matches.append((frame, group_idx, best))
    # A coherent increasing visual *trajectory*, not single-frame similarity.
    good = []
    for group in range(len(groups)):
        candidates = sorted(
            ((a, c) for a, b, c in matches if b == group),
            key=lambda pair: (pair[0].pts_seconds, pair[0].id),
        )
        for i in range(len(candidates)):
            seq = [candidates[i]]
            for pair in candidates[i+1:]:
                prev_clip, prev_source = seq[-1]
                clip_delta = pair[0].pts_seconds - prev_clip.pts_seconds
                source_delta = pair[1].pts_seconds - prev_source.pts_seconds
                if (clip_delta >= .4 and source_delta >= .4
                        and abs(source_delta - clip_delta) <= .75
                        and pair[0].checksum != prev_clip.checksum
                        and pair[1].checksum != prev_source.checksum):
                    seq.append(pair)
                if len(seq) >= 3:
                    break
            if len(seq) >= 3:
                offsets = [source.pts_seconds - clip.pts_seconds
                           for clip, source in seq]
                if max(offsets) - min(offsets) <= .75:
                    good.append((group, seq))
                break
    # Separate source windows, repeated scenes and multiple trajectories
    # are ambiguity, *not* evidence that one stream is the original.
    if len(good) > 1:
        result["status"] = "UNKNOWN_MULTIPLE_VISUAL_TRAJECTORIES"
    elif good:
        group, seq = good[0]
        result.update({
            "status": "VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD",
            "matched_frames": len(seq),
            "matched_source_window": group,
            "source_frame_times_local": [
                round(src.pts_seconds, 3) for _, src in seq
            ],
            "finished_frame_times_local": [
                round(dst_frame.pts_seconds, 3) for dst_frame, _ in seq
            ],
        })
    return result


def _video_measure(acquired, root: Path, *, asset_id: str, start: float,
                   end: float, measure, probe):
    media = Path(acquired.media_path)
    if (not media.is_file() or media.is_symlink()
            or not 0 < media.stat().st_size <= MAX_BYTES):
        raise ValueError("Original video probe exceeds bounded local media limits")
    details = probe(media)
    if not any(row.get("codec_type") == "video" for row in details["streams"]):
        raise ValueError("Bounded video probe has no video")
    actual = float(details["format"]["duration"])
    expected = end - start
    if (not math.isfinite(actual) or not max(5., expected-3.) <= actual <= expected+3.):
        raise ValueError("Received video section does not match bounded request")
    window = measure(
        media, root, asset_id=asset_id, start=0.,
        end=min(actual, WINDOW_SECONDS + 2.),
        transcript={}, dense_times=[2., 4., 6., 8., 10.],
    )
    return window, checksum_file(media)


def verify_pending_source_visual(
    provider, *, store: Path, state_path: Path, output: Path,
    policy_rules: list, max_clips: int = 1, max_candidates: int = 2,
    progress=None, measure=measure_window, probe=probe_video,
    compare_sequences=compare_sparse_visual_sequences,
) -> dict:
    """Run bounded automatic video checks; retain original identity as unknown."""
    if not 1 <= max_clips <= MAX_CLIPS or not 1 <= max_candidates <= MAX_CANDIDATES:
        raise ValueError("Invalid visual source candidate budget")
    if not state_path.exists():
        return {"version": VERSION, "attempted": 0, "counts": {},
                "reason": "UNKNOWN_NO_GROWTH_STATE", **_NO_GOLD}
    state = CorpusGrowthState.model_validate_json(
        state_path.read_text(encoding="utf-8")
    )
    learning = load_human_clip_store(store)
    learned_clips = {human_clip_identity(x.clip_url) for x in learning}
    training_ids = {x.stream_id for x in learning if x.stream_id}
    counts = {}
    attempted = 0

    def persist():
        _atomic_json(state_path, state.model_dump(mode="json"))

    for key, item in sorted(state.items.items()):
        if attempted >= max_clips:
            break
        if item.status != "source_visual_pending":
            continue
        attempted += 1
        report = {
            "version": VERSION,
            "status": "UNKNOWN_NO_VERIFIED_VISUAL_CORROBORATION",
            "candidate_observations": [],
            "finished_video_sha256": None,
            **_NO_GOLD,
        }

        def finish(status):
            report["status"] = status
            # This field stores diagnostics only; it is *never* the canonical
            # source identity, even if one sampled candidate looks plausible.
            item.source_scout["visual_probe"] = report
            persist()
            counts[status] = counts.get(status, 0) + 1
            if progress:
                progress(f"visual source scout {key}: {status}")

        scout = item.source_scout
        if (not isinstance(scout, dict)
                or scout.get("version") != SCOUT_VERSION
                or scout.get("status") != "UNVERIFIED_PUBLIC_SOURCE_SHORTLIST"
                or not 1 <= len(scout.get("candidate_sources", [])) <= 4
                or item.source_url is not None or item.source_stream_id is not None
                or human_clip_identity(item.clip_url) in learned_clips):
            finish("BLOCKED_UNVERIFIED_SOURCE_QUEUE")
            continue
        try:
            clip = provider.probe(item.clip_url)
            if (not isinstance(clip, SourceMetadata)
                    or not _public(clip)
                    or human_clip_identity(clip.url) != human_clip_identity(item.clip_url)
                    or clip.duration is None or not 8 <= clip.duration <= 180
                    or evaluate_source_metadata(
                        clip, rules=policy_rules, operation=PolicyOperation.LEARN,
                    ).decision is not PolicyDecision.ALLOW):
                finish("BLOCKED_CLIP_IDENTITY_POLICY_OR_DURATION")
                continue
            clip_start = max(0., (clip.duration-WINDOW_SECONDS)/2)
            clip_end = min(clip.duration, clip_start+WINDOW_SECONDS)
            allowed = evaluate_source_ranges(
                clip, ranges=[(clip_start, clip_end)],
                rules=policy_rules, operation=PolicyOperation.LEARN,
            )
            if allowed.decision is not PolicyDecision.ALLOW:
                finish("BLOCKED_CLIP_RANGE_POLICY")
                continue
            acquired = provider.acquire_video_range(
                item.clip_url, output / "finished", start=clip_start,
                end=clip_end,
            )
            if (acquired.metadata.external_id != clip.external_id
                    or abs(acquired.requested_start-clip_start) > .001
                    or abs(acquired.requested_end-clip_end) > .001):
                raise ValueError("Finished probe identity changed")
            finished, finished_sha = _video_measure(
                acquired, output / "measured",
                asset_id=f"{key}-finished", start=clip_start, end=clip_end,
                measure=measure, probe=probe,
            )
            report["finished_video_sha256"] = finished_sha
            for index, candidate in enumerate(scout["candidate_sources"][:max_candidates]):
                observed = {
                    "source_url": candidate.get("source_url"),
                    "source_stream_id": candidate.get("source_stream_id"),
                    "status": "UNKNOWN_NO_VISUAL_CORROBORATION",
                    "sampled_source_windows": [],
                    "visual_correspondence": "UNKNOWN_NOT_MEASURED",
                    "source_identity_verified": False,
                }
                report["candidate_observations"].append(observed)
                try:
                    url = candidate["source_url"]
                    source = provider.probe(url)
                    if (not isinstance(source, SourceMetadata) or not _public(source)
                            or source.url != url
                            or source.external_id != candidate["source_stream_id"]
                            or source.external_id in training_ids
                            and source.external_id in state.holdout_stream_ids
                            or source.external_id == clip.external_id
                            or source.duration is None
                            or candidate.get("duration_seconds") != source.duration
                            or evaluate_source_metadata(
                                source, rules=policy_rules, operation=PolicyOperation.LEARN,
                            ).decision is not PolicyDecision.ALLOW):
                        observed["status"] = "BLOCKED_SOURCE_IDENTITY_OR_POLICY"
                        continue
                    spans = [(start, start+WINDOW_SECONDS)
                             for start in _starts(source.duration)]
                    if len(spans) != SOURCE_WINDOWS or evaluate_source_ranges(
                            source, ranges=spans,
                            rules=policy_rules, operation=PolicyOperation.LEARN,
                        ).decision is not PolicyDecision.ALLOW:
                        observed["status"] = "BLOCKED_SOURCE_RANGE_POLICY"
                        continue
                    windows = []
                    for section_index, (start, end) in enumerate(spans):
                        section = provider.acquire_video_range(
                            source.url, output / "original", start=start, end=end,
                        )
                        if (section.metadata.external_id != source.external_id
                                or abs(section.requested_start-start) > .001
                                or abs(section.requested_end-end) > .001):
                            raise ValueError("Source section response changed identity")
                        window, digest = _video_measure(
                            section, output / "measured",
                            asset_id=f"{key}-source-{index}-{section_index}",
                            start=start, end=end, measure=measure, probe=probe,
                        )
                        windows.append(window)
                        observed["sampled_source_windows"].append({
                            "requested_range": [start, end], "media_sha256": digest,
                            "source_native_pts_verified": False,
                        })
                    visual = compare_sequences(finished, windows)
                    if (not isinstance(visual, dict)
                            or any(visual.get(flag) is not False for flag in _NO_GOLD)
                            or visual.get("status") not in {
                                "VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD",
                                "UNKNOWN_NO_VISUAL_SEQUENCE",
                                "UNKNOWN_VISUAL_COMPARISON_BUDGET",
                                "UNKNOWN_IMAGE_DECODE",
                                "UNKNOWN_MULTIPLE_VISUAL_TRAJECTORIES",
                            }):
                        raise ValueError("Source visual result was promoted")
                    observed["status"] = visual["status"]
                    observed["visual_correspondence"] = visual["status"]
                    observed["sampled_sequence"] = visual
                except Exception as exc:
                    observed["status"] = "UNKNOWN_BOUNDED_SOURCE_PROBE_FAILURE"
                    observed["error_class"] = type(exc).__name__
                finally:
                    item.source_scout["visual_probe"] = report
                    persist()
            matched = sum(
                x["status"] == "VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD"
                for x in report["candidate_observations"]
            )
            finish(
                "UNREVIEWED_VISUAL_CANDIDATE_CORROBORATION"
                if matched == 1 else
                "UNKNOWN_AMBIGUOUS_MULTIPLE_SOURCE_CANDIDATES"
                if matched > 1 else
                "UNKNOWN_NO_VISUAL_MATCH_IN_BOUNDED_SAMPLES"
            )
        except Exception as exc:
            report["error_class"] = type(exc).__name__
            finish("UNKNOWN_BOUNDED_FINISHED_PROBE_FAILURE")
    return {"version": VERSION, "attempted": attempted, "counts": counts,
            "state_path": str(state_path), **_NO_GOLD}
