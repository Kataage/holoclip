"""Metadata-only, fail-closed candidate discovery for unreferenced human clips.

This is *not* original-stream identity confirmation. Search ranking, uploader
names, titles and plausible durations cannot replace source-video correspondence.
The next stage can use this persisted shortlist as a bounded visual probe queue.
"""
from __future__ import annotations

import re

from holoclip.clip_policy import (
    PolicyDecision, PolicyOperation, evaluate_source_metadata,
)
from holoclip.providers.source.base import SourceCandidate, SourceMetadata

VERSION = "unresolved-public-source-shortlist-v1"
MAX_QUERIES = 2
MAX_SEARCH_RESULTS = 5
MAX_PROBES = 6
MAX_SHORTLIST = 4


def _queries(clip: SourceMetadata) -> list[str]:
    title = (clip.title or "").strip()
    if not 8 <= len(title) <= 300:
        return []
    # Keep this deterministic, not AI-generated. The title is *only* a
    # discovery hint; it must not be treated as an editor reference.
    cleaned = re.sub(
        r"[\[【（(][^\]】）)]*(?:切り抜き|clip|ホロライブ)[^\]】）)]*[\]】）)]",
        " ", title, flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"(?i)切り抜き|hololive\s*clips?|#\S+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" 　|｜:：-—")
    if len(cleaned) < 5:
        return []
    prefix = cleaned[:70]
    return [f"{prefix} ホロライブ 元配信", f"{prefix} 配信アーカイブ"]


def scout_unresolved_public_source(
    provider, clip: SourceMetadata, *,
    explicit_candidates: list[SourceCandidate],
    policy_rules: list, max_probes: int = MAX_PROBES,
) -> dict:
    """Find reviewable candidate IDs, *never* assign a source URL or split.

    Only public/ended, metadata-allowed candidates are retained. Original
    video snippets are not acquired by this stage and a shortlist is never
    evidence of an actual source, exact timestamps or editorial accuracy.
    """
    if not 1 <= max_probes <= MAX_PROBES:
        raise ValueError("Invalid public metadata source-probe budget")
    result = {
        "version": VERSION,
        "status": "UNKNOWN_NOT_SEARCHED",
        "candidate_kind": "UNVERIFIED_PUBLIC_METADATA_ONLY",
        "searched_queries": [],
        "candidate_sources": [],
        "metadata_probes": 0,
        "visual_correspondence_tested": False,
        "source_identity_verified": False,
        "qualification_allowed": False,
        "reviewed_gold_available": False,
        "event_split_verified": False,
        "used_in_learning_or_holdout": False,
    }
    if (clip.availability != "public"
            or clip.live_status not in ("not_live", "was_live")):
        result["status"] = "BLOCKED_CLIP_NOT_PUBLIC_ENDED"
        return result
    if (evaluate_source_metadata(
            clip, rules=policy_rules, operation=PolicyOperation.LEARN
        ).decision is not PolicyDecision.ALLOW):
        result["status"] = "BLOCKED_CLIP_METADATA_POLICY"
        return result

    proposals: list[tuple[str, str]] = []
    for candidate in explicit_candidates[:MAX_PROBES]:
        if not isinstance(candidate, SourceCandidate):
            raise ValueError("Untrusted explicitly extracted source link")
        proposals.append((candidate.url, "description_link_not_disambiguated"))

    search_failed = False
    # Explicitly ambiguous links are better candidates than broad search
    # results. Do not spend requests widening already bounded descriptions.
    if not proposals:
        for query in _queries(clip)[:MAX_QUERIES]:
            result["searched_queries"].append(query)
            try:
                discovered = provider.search_public_clips(
                    query, limit=MAX_SEARCH_RESULTS, offset=0,
                )
            except Exception:
                search_failed = True
                continue
            if not isinstance(discovered, list):
                search_failed = True
                continue
            for candidate in discovered[:MAX_SEARCH_RESULTS]:
                if isinstance(candidate, SourceMetadata):
                    proposals.append(
                        (candidate.url, "public_metadata_search_unverified")
                    )

    seen_urls = set()
    seen_ids = {clip.external_id}
    for url, route in proposals:
        if result["metadata_probes"] >= max_probes:
            break
        if not isinstance(url, str) or not url or url in seen_urls:
            continue
        seen_urls.add(url)
        result["metadata_probes"] += 1
        try:
            source = provider.probe(url)
            if (not isinstance(source, SourceMetadata)
                    or source.external_id in seen_ids
                    or source.url != url
                    or source.availability != "public"
                    or source.live_status not in ("not_live", "was_live")):
                continue
            seen_ids.add(source.external_id)
            # Search results are not explicit original links, and matching a
            # short clip of the same editor is a common false discovery.
            if route == "public_metadata_search_unverified":
                if (source.duration is None
                        or source.duration < max(
                            300., (clip.duration or 0.) * 1.5
                        )
                        or (
                            clip.channel_id and source.channel_id
                            and clip.channel_id == source.channel_id
                        )):
                    continue
            policy = evaluate_source_metadata(
                source, rules=policy_rules, operation=PolicyOperation.LEARN,
            )
            if policy.decision is not PolicyDecision.ALLOW:
                continue
            result["candidate_sources"].append({
                "source_url": source.url,
                "source_stream_id": source.external_id,
                "title": source.title[:160],
                "channel_id": source.channel_id,
                "duration_seconds": source.duration,
                "discovery_basis": route,
                "visual_correspondence": "NOT_MEASURED",
                "source_identity_verified": False,
            })
            if len(result["candidate_sources"]) >= MAX_SHORTLIST:
                break
        except Exception:
            search_failed = True
            continue
    result["status"] = (
        "UNVERIFIED_PUBLIC_SOURCE_SHORTLIST"
        if result["candidate_sources"] else
        "UNKNOWN_DISCOVERY_ERRORS"
        if search_failed else
        "UNKNOWN_NO_PLAUSIBLE_PUBLIC_SOURCES"
    )
    return result
