"""Resumable autonomous corpus acquisition, with conservative quarantine and diversity."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field

from holoclip.clip_policy import PolicyDecision, PolicyOperation, evaluate_source_metadata
from holoclip.editorial_profile import _atomic_json
from holoclip.example_ingest import ClipPolicyGateError, SourceResolutionError
from holoclip.example_quality import LearningQuality, evaluate_human_example_learning_quality
from holoclip.holdout_reservations import should_reserve_holdout
from holoclip.examples import (
    HumanClipExample,
    load_human_clip_store,
    make_human_clip_example_id,
    human_clip_identity,
)
from holoclip.providers.source.base import SourceMetadata
from holoclip.providers.source.yt_dlp import choose_source_candidate
from holoclip.source_candidate_scout import scout_unresolved_public_source


# ytsearch relevance pages are neither infinite nor a reliable new-uploads feed.
# Periodically revisit the first page to discover newly published public clips.
MAX_DISCOVERY_CURSOR = 50


class CorpusGrowthItem(BaseModel):
    clip_url: str
    source_url: str | None = None
    source_stream_id: str | None = None
    source_channel: str | None = None
    source_scout: dict | None = None
    clip_channel: str | None = None
    query: str
    status: str = "discovered"
    reason: str | None = None
    attempts: int = 0
    example_id: str | None = None


class CorpusGrowthState(BaseModel):
    version: str = "autonomous-corpus-growth-v1"
    cursors: dict[str, int] = Field(default_factory=dict)
    items: dict[str, CorpusGrowthItem] = Field(default_factory=dict)
    # Source-stream reservations are durable; no split may be retroactively relabeled.
    holdout_stream_ids: list[str] = Field(default_factory=list)
    search_errors: list[str] = Field(default_factory=list)


def discovery_queries(examples: list[HumanClipExample]) -> list[str]:
    # Metadata widens acquisition; it is excluded from mechanism learning.
    counts = Counter(m for x in examples for m in x.members)
    underrepresented = sorted(counts, key=lambda m: (counts[m], m))[:4]
    return ["ホロライブ 切り抜き 雑談", "ホロライブ 切り抜き コラボ", "hololive clips Japanese"] + [
        f"{m} ホロライブ 切り抜き" for m in underrepresented
    ]


def _public(metadata: SourceMetadata) -> bool:
    return metadata.availability == "public" and metadata.live_status in {"not_live", "was_live"}


async def grow_corpus(
    provider,
    *,
    store: Path,
    state_path: Path,
    ingest: Callable[..., Awaitable],
    policy_rules: list,
    scan_limit: int = 20,
    max_new_examples: int = 3,
    target_trusted: int = 30,
    target_streams: int = 15,
    discover_only: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict:
    if not 1 <= scan_limit <= 100 or not 1 <= max_new_examples <= 100:
        raise ValueError("corpus scan/acquisition limits must be between 1 and 100")
    if target_trusted < 30 or target_streams < 15:
        raise ValueError("corpus targets must preserve the 30 trusted / 15 streams floor")
    state = (
        CorpusGrowthState.model_validate_json(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else CorpusGrowthState()
    )

    def persist():
        _atomic_json(state_path, state.model_dump(mode="json"))

    # Fail closed *before* any remote metadata request when a checkpoint was
    # corrupted. The cursor is shared across invocations, not just cycles.
    pending_cursor = state.cursors.get("corpus_metadata_pending_round_robin", 0)
    if type(pending_cursor) is not int or pending_cursor < 0:
        raise ValueError("Invalid corpus metadata resolution cursor")

    examples = load_human_clip_store(store)
    # Preserve every legacy corpus stream as training, including review-only examples.
    # Queued learning items also claim their streams to prevent cross-clip leakage.
    learning_streams = {x.stream_id for x in examples if x.stream_id}
    learning_streams.update(
        x.source_stream_id for x in state.items.values()
        if x.source_stream_id and x.status in {
            "queued", "trusted", "review", "policy_review", "rejected", "duplicate",
        }
    )
    reserved_streams = set(state.holdout_stream_ids)
    reserved_streams.update(
        x.source_stream_id for x in state.items.values()
        if x.status in {"holdout_reserved", "split_conflict"} and x.source_stream_id
    )
    state.holdout_stream_ids = sorted(reserved_streams)
    for existing in state.items.values():
        if (existing.status == "holdout_reserved"
                and existing.source_stream_id in learning_streams):
            existing.status = "split_conflict"
            existing.reason = "source stream already exists in learning corpus"
    known = {x.id for x in examples}
    known_videos = {human_clip_identity(x.clip_url) for x in examples}
    queries = discovery_queries(examples)
    # Round-robin pages ensure continued runs move past the same popular results.
    scanned = 0
    search_page_wraps = 0
    empty_search_pages = 0
    for query in sorted(queries, key=lambda q: (state.cursors.get(q, 0), q)):
        if scanned >= scan_limit:
            break
        limit = min(5, scan_limit - scanned)
        offset = state.cursors.get(query, 0)
        if type(offset) is not int or offset < 0:
            raise ValueError("Invalid persistent source discovery cursor")
        # Old state can have arbitrarily high cursors. Reset *before* hitting
        # yt-dlp with an ever-growing ytsearchN request.
        if offset >= MAX_DISCOVERY_CURSOR:
            offset = 0
            state.cursors[query] = 0
            search_page_wraps += 1
            persist()
        try:
            rows = provider.search_public_clips(query, limit=limit, offset=offset)
        except Exception as exc:
            state.search_errors = (state.search_errors + [f"{query}: {type(exc).__name__}: {exc}"])[
                -20:
            ]
            persist()
            continue
        if not rows:
            # An empty result page is not proof of zero public clips forever.
            # The next scheduled invocation scans fresh first-page results.
            state.cursors[query] = 0
            empty_search_pages += 1
            search_page_wraps += 1
        else:
            state.cursors[query] = (
                0 if offset + limit >= MAX_DISCOVERY_CURSOR else offset + limit
            )
            if state.cursors[query] == 0:
                search_page_wraps += 1
        scanned += limit
        for row in rows:
            example_id = make_human_clip_example_id(row.url)
            if (
                example_id not in known
                and example_id not in state.items
                and human_clip_identity(row.url) not in known_videos
            ):
                state.items[example_id] = CorpusGrowthItem(
                    clip_url=row.url, query=query, clip_channel=row.channel
                )
        persist()

    source_counts = Counter(x.stream_id for x in examples if x.stream_id)
    editor_counts = Counter(x.clip_channel for x in examples if x.clip_channel)
    channel_counts = Counter(
        x.source_channel for x in state.items.values() if x.status == "trusted" and x.source_channel
    )
    attempted = 0
    resolved = 0
    source_visual_newly_pending = 0
    explicit_visual_newly_pending = 0
    # A fixed sorted-first scan re-probed the same failed URLs forever and
    # starved fresh clips once the backlog exceeded scan_limit. Already-queued
    # learning items also used to waste the limited metadata resolution slots.
    # In metadata-only mode, do not reclassify acquisition retries that already
    # own a learning stream into unverified visual-only candidates.
    pending = sorted(
        (
            (key, item) for key, item in state.items.items()
            if item.status == "discovered"
            or (
                item.status == "retryable"
                and (not discover_only or (
                    item.source_url is None and item.source_stream_id is None
                ))
            )
        ),
        key=lambda entry: (
            editor_counts[entry[1].clip_channel], entry[1].clip_url, entry[0],
        ),
    )
    rotated = (
        pending[pending_cursor % len(pending):]
        + pending[:pending_cursor % len(pending)]
        if pending else []
    )
    metadata_resolution_attempted = 0
    metadata_retryable_attempted = 0
    for _key, item in rotated[:scan_limit]:
        metadata_resolution_attempted += 1
        if item.status == "retryable":
            metadata_retryable_attempted += 1
        # Persist BEFORE probing network so a crash/timeout cannot strand
        # later candidate clips behind the same permanently failing URL.
        state.cursors["corpus_metadata_pending_round_robin"] = (
            pending_cursor + metadata_resolution_attempted
        )
        persist()
        try:
            clip = provider.probe(item.clip_url)
            if not _public(clip):
                item.status, item.reason = "review", "clip public/ended status unverified"
                continue
            clip_policy = evaluate_source_metadata(
                clip, rules=policy_rules, operation=PolicyOperation.LEARN
            )
            if clip_policy.decision is not PolicyDecision.ALLOW:
                item.status, item.reason = "policy_review", "clip metadata policy is not allow"
                continue
            candidates = provider.source_candidates(clip)
            chosen = choose_source_candidate(candidates)
            # The unattended pattern collector calls grow_corpus with
            # discover_only=True. Previously *unambiguous description links*
            # bypassed the visual-sampling queue and became training/holdout
            # assignments, so the most informative public originals never
            # received any actual bounded video measurements. A description
            # link is a discovery hint, not independently verified gold.
            if chosen is None or discover_only:
                proposed = candidates if chosen is None else [chosen]
                item.source_scout = scout_unresolved_public_source(
                    provider, clip, explicit_candidates=proposed,
                    policy_rules=policy_rules,
                )
                if item.source_scout["candidate_sources"]:
                    item.status, item.reason = (
                        "source_visual_pending",
                        "explicit source link awaits unreviewed visual measurement"
                        if chosen is not None else
                        "ambiguous original candidates await visual measurement",
                    )
                    source_visual_newly_pending += 1
                    if chosen is not None:
                        explicit_visual_newly_pending += 1
                else:
                    item.status, item.reason = (
                        "review", "no independently verified source discovered",
                    )
                continue
            # Normal corpus ingestion still resolves chosen public links and
            # applies the existing training/holdout split. Only metadata-only
            # collection routes them into *unreviewed* video sampling.
            item.source_scout = None
            source = provider.probe(chosen.url)
            item.source_url, item.source_stream_id = chosen.url, source.external_id
            item.source_channel, item.clip_channel = (
                source.channel_id or source.channel,
                clip.channel,
            )
            if not _public(source):
                item.status, item.reason = "review", "source public/ended status unverified"
                continue
            policy = evaluate_source_metadata(
                source, rules=policy_rules, operation=PolicyOperation.LEARN
            )
            # Deferred range findings are handled by existing ingest after alignment.
            if policy.decision is PolicyDecision.BLOCK or any(
                f.decision is PolicyDecision.REVIEW and not f.defer_until_ranges
                for f in policy.findings
            ):
                item.status, item.reason = (
                    "policy_review",
                    "source metadata policy requires quarantine",
                )
                continue
            stream_id = item.source_stream_id
            if not stream_id:
                item.status, item.reason = "review", "source stream identity missing"
                continue
            if stream_id in reserved_streams and stream_id in learning_streams:
                item.status, item.reason = (
                    "split_conflict", "source stream is in both learning and holdout"
                )
            elif stream_id in reserved_streams:
                item.status, item.reason = "holdout_reserved", None
                resolved += 1
            elif stream_id in learning_streams:
                item.status, item.reason = "queued", None
                resolved += 1
            elif learning_streams and should_reserve_holdout(stream_id):
                # Reserve before acquisition/enrichment, not after seeing model answers.
                reserved_streams.add(stream_id)
                state.holdout_stream_ids = sorted(reserved_streams)
                item.status, item.reason = "holdout_reserved", None
                resolved += 1
            else:
                learning_streams.add(stream_id)
                item.status, item.reason = "queued", None
                resolved += 1
        except Exception as exc:
            item.status, item.reason = "retryable", f"{type(exc).__name__}: {exc}"
        finally:
            persist()
    if not discover_only:
        while attempted < max_new_examples:
            examples = load_human_clip_store(store)
            trusted = [
                x
                for x in examples
                if evaluate_human_example_learning_quality(x).decision is LearningQuality.TRUSTED
            ]
            if (
                len(trusted) >= target_trusted
                and len({x.stream_id for x in trusted if x.stream_id}) >= target_streams
            ):
                break
            queue = [x for x in state.items.values() if x.status == "queued"]
            existing_videos = {human_clip_identity(x.clip_url) for x in examples}
            for item in queue:
                if human_clip_identity(item.clip_url) in existing_videos:
                    item.status, item.reason = (
                        "duplicate",
                        "canonical clip already exists in corpus",
                    )
            queue = [x for x in queue if x.status == "queued"]
            persist()
            if not queue:
                break
            item = min(
                queue,
                key=lambda x: (
                    source_counts[x.source_stream_id],
                    channel_counts[x.source_channel],
                    editor_counts[x.clip_channel],
                    x.query,
                    x.clip_url,
                ),
            )
            attempted += 1
            item.attempts += 1
            if progress:
                progress(f"corpus acquisition {attempted}/{max_new_examples}: {item.clip_url}")
            try:
                result = await ingest(clip_url=item.clip_url, source_url=item.source_url)
                item.example_id = result.example_id
                example = next(x for x in load_human_clip_store(store) if x.id == result.example_id)
                quality = evaluate_human_example_learning_quality(example)
                item.status = quality.decision.value
                item.reason = ",".join(r.value for r in quality.reasons) or None
                if item.status == "trusted":
                    source_counts[item.source_stream_id] += 1
                    channel_counts[item.source_channel] += 1
                    editor_counts[item.clip_channel] += 1
            except SourceResolutionError as exc:
                item.status, item.reason = "review", str(exc)
            except ClipPolicyGateError as exc:
                item.status, item.reason = "policy_review", str(exc)
            except Exception as exc:
                item.status, item.reason = "retryable", f"{type(exc).__name__}: {exc}"
            persist()
    trusted = [
        x
        for x in load_human_clip_store(store)
        if evaluate_human_example_learning_quality(x).decision is LearningQuality.TRUSTED
    ]
    return {
        "scanned": scanned,
        "search_page_wraps": search_page_wraps,
        "empty_search_pages": empty_search_pages,
        "resolved": resolved,
        "metadata_resolution_attempted": metadata_resolution_attempted,
        "metadata_retryable_attempted": metadata_retryable_attempted,
        "metadata_resolution_pending": sum(
            item.status in {"discovered", "retryable"}
            and (not discover_only or (
                item.source_url is None and item.source_stream_id is None
            ))
            for item in state.items.values()
        ),
        "source_visual_newly_pending": source_visual_newly_pending,
        "explicit_source_visual_newly_pending": explicit_visual_newly_pending,
        "attempted": attempted,
        "trusted_examples": len(trusted),
        "trusted_streams": len({x.stream_id for x in trusted if x.stream_id}),
        "target_trusted": target_trusted,
        "target_streams": target_streams,
        "queue_counts": dict(Counter(x.status for x in state.items.values())),
        "holdout_reserved_streams": len(reserved_streams),
        "holdout_reserved_clips": sum(
            x.status == "holdout_reserved" for x in state.items.values()
        ),
        "split_conflicts": sorted(reserved_streams & learning_streams),
        "search_errors": state.search_errors,
        "state_path": str(state_path),
    }
