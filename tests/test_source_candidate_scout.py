"""Public source discovery is unattended metadata triage, never identity proof."""
import pytest

from holoclip.corpus_growth import CorpusGrowthState, grow_corpus
from holoclip.source_candidate_scout import (
    VERSION, scout_unresolved_public_source,
)
from holoclip.providers.source.base import SourceCandidate, SourceMetadata


CLIP = "https://www.youtube.com/watch?v=AAAAAAAAAAA"
SOURCE_A = "https://www.youtube.com/watch?v=BBBBBBBBBBB"
SOURCE_B = "https://www.youtube.com/watch?v=CCCCCCCCCCC"


def meta(url, *, duration, availability="public", channel="hololive"):
    video_id = url.split("=")[-1]
    return SourceMetadata(
        provider="fake", url=url, external_id=video_id,
        title=("【切り抜き】 星街すいせいが語る思い出の配信" if url == CLIP
               else "ホロライブ 雑談配信 アーカイブ"),
        channel=channel, channel_id=channel,
        duration=duration, live_status="was_live",
        availability=availability,
    )


class Provider:
    def __init__(self, explicit=False, unavailable=False):
        self.explicit = explicit
        self.unavailable = unavailable
        self.searches = []
        self.probes = []
        self.clip = meta(CLIP, duration=70., channel="clipping-editor")

    def probe(self, url):
        self.probes.append(url)
        if url == CLIP:
            return self.clip
        if url == SOURCE_A:
            return meta(url, duration=7300.)
        if url == SOURCE_B:
            return meta(url, duration=6500.,
                        availability="subscriber_only" if self.unavailable else "public")
        raise AssertionError(f"Unrelated or invented source must not be probed: {url}")

    def source_candidates(self, clip):
        if self.explicit:
            return [
                SourceCandidate(
                    url=url, reason="description_explicit_source_label", score=.9
                )
                for url in (SOURCE_A, SOURCE_B)
            ]
        return []

    def search_public_clips(self, query, *, limit, offset):
        self.searches.append((query, limit, offset))
        return [self.clip, meta(SOURCE_A, duration=7300.)]


def test_missing_description_automatically_stores_public_shortlist_not_source_gold():
    provider = Provider()
    report = scout_unresolved_public_source(
        provider, provider.clip, explicit_candidates=[], policy_rules=[],
    )
    assert report["version"] == VERSION
    assert report["status"] == "UNVERIFIED_PUBLIC_SOURCE_SHORTLIST"
    assert len(report["searched_queries"]) == 2
    assert len(provider.searches) == 2
    assert report["metadata_probes"] <= 6
    assert [r["source_url"] for r in report["candidate_sources"]] == [SOURCE_A]
    assert report["candidate_sources"][0]["visual_correspondence"] == "NOT_MEASURED"
    for flag in ("visual_correspondence_tested", "source_identity_verified",
                 "qualification_allowed", "reviewed_gold_available",
                 "event_split_verified", "used_in_learning_or_holdout"):
        assert report[flag] is False


def test_ambiguous_explicit_links_do_not_trigger_broad_search_or_pick_winner():
    provider = Provider(explicit=True, unavailable=True)
    report = scout_unresolved_public_source(
        provider, provider.clip,
        explicit_candidates=provider.source_candidates(provider.clip),
        policy_rules=[],
    )
    assert report["status"] == "UNVERIFIED_PUBLIC_SOURCE_SHORTLIST"
    assert not provider.searches
    assert len(report["candidate_sources"]) == 1
    assert report["candidate_sources"][0]["source_url"] == SOURCE_A
    assert report["source_identity_verified"] is False


def test_nonpublic_candidate_and_same_video_rejected_and_budgets_honored():
    provider = Provider(unavailable=True)
    provider.probe(SOURCE_A)
    with pytest.raises(ValueError):
        scout_unresolved_public_source(
            provider, provider.clip, explicit_candidates=[],
            policy_rules=[], max_probes=100,
        )
    class BadResults(Provider):
        def search_public_clips(self, query, *, limit, offset):
            return [self.clip, meta(SOURCE_B, duration=6500.)]
    b = BadResults(unavailable=True)
    outcome = scout_unresolved_public_source(
        b, b.clip, explicit_candidates=[], policy_rules=[],
    )
    assert outcome["status"] == "UNKNOWN_NO_PLAUSIBLE_PUBLIC_SOURCES"
    assert outcome["candidate_sources"] == []


@pytest.mark.asyncio
async def test_growth_saves_shortlist_without_assigning_unknown_source(tmp_path):
    provider = Provider()
    def search(query, *, limit, offset):
        if "元配信" in query or "配信アーカイブ" in query:
            return [provider.clip, meta(SOURCE_A, duration=7300.)]
        return [provider.clip]
    # Normal corpus search discovers the human clip; scout searches a
    # different phrasing and sees the plausible ORIGINAL. Neither assigns it.
    provider.search_public_clips = search

    async def forbidden_ingest(**kwargs):
        raise AssertionError("Unverified shortlist must not start ingest")

    result = await grow_corpus(
        provider, store=tmp_path / "learning.jsonl",
        state_path=tmp_path / "growth.json",
        ingest=forbidden_ingest, policy_rules=[],
        scan_limit=5, max_new_examples=1,
    )
    state = CorpusGrowthState.model_validate_json(
        (tmp_path / "growth.json").read_text(encoding="utf-8")
    )
    assert result["attempted"] == 0
    assert len(state.items) == 1
    item = next(iter(state.items.values()))
    assert item.status == "source_visual_pending"
    assert item.source_url is None
    assert item.source_stream_id is None
    assert item.source_scout["source_identity_verified"] is False
    assert item.source_scout["status"] == "UNVERIFIED_PUBLIC_SOURCE_SHORTLIST"
    assert result["holdout_reserved_streams"] == 0


@pytest.mark.asyncio
async def test_growth_rejects_ambiguous_description_links_without_visual_proof(tmp_path):
    provider = Provider(explicit=True)

    def search(query, *, limit, offset):
        return [provider.clip]
    provider.search_public_clips = search

    async def no_ingest(**kwargs):
        raise AssertionError("Even two public explicit links cannot auto-resolve")

    await grow_corpus(
        provider, store=tmp_path / "learning.jsonl",
        state_path=tmp_path / "growth.json",
        ingest=no_ingest, policy_rules=[], scan_limit=5,
    )
    state = CorpusGrowthState.model_validate_json(
        (tmp_path / "growth.json").read_text(encoding="utf-8")
    )
    item = next(iter(state.items.values()))
    assert item.status == "source_visual_pending"
    assert len(item.source_scout["candidate_sources"]) == 2
    assert item.source_url is None and item.source_stream_id is None
