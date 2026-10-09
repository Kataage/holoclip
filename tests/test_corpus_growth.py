from types import SimpleNamespace

import pytest

from holoclip.corpus_growth import CorpusGrowthItem, CorpusGrowthState, grow_corpus
from holoclip.examples import load_human_clip_store, upsert_human_clip_examples
from holoclip.providers.source.base import SourceCandidate, SourceMetadata
from test_editorial_profile import trusted_example
from typer.testing import CliRunner
from holoclip import cli


class PublicProvider:
    def __init__(self):
        self.searches = []
        self.bad_source = False
        self.paid = False

    def search_public_clips(self, query, *, limit, offset):
        self.searches.append((query, offset))
        return [self.probe("https://youtu.be/clip")]

    def probe(self, url):
        source = "source" in url
        return SourceMetadata(
            provider="fake",
            url=url,
            external_id="source" if source else "clip",
            title="public stream" if source else "human clip",
            availability="subscriber_only" if self.paid else "public",
            live_status="was_live" if source else "not_live",
            channel="member" if source else "editor",
        )

    def source_candidates(self, metadata):
        return (
            []
            if self.bad_source
            else [
                SourceCandidate(
                    url="https://youtu.be/source",
                    score=0.9,
                    reason="description_explicit_source_label",
                )
            ]
        )


@pytest.mark.asyncio
async def test_autonomous_discovery_acquisition_quality_quarantine_and_resume(tmp_path):
    store = tmp_path / "examples.jsonl"
    state_path = tmp_path / "growth.json"
    provider = PublicProvider()
    calls = []

    async def ingest(**kwargs):
        calls.append(kwargs)
        example = trusted_example("clip-example", stream_id="source").model_copy(
            update={"alignment_confidence": 0.65}
        )
        upsert_human_clip_examples(store, [example])
        return SimpleNamespace(example_id=example.id)

    args = dict(
        store=store,
        state_path=state_path,
        ingest=ingest,
        policy_rules=[],
        scan_limit=5,
        max_new_examples=1,
    )
    report = await grow_corpus(provider, **args)
    assert len(calls) == 1
    assert report["trusted_examples"] == 0
    state = CorpusGrowthState.model_validate_json(state_path.read_text(encoding="utf-8"))
    assert next(iter(state.items.values())).status == "review"
    await grow_corpus(provider, **args)
    assert len(calls) == 1  # no automatic review promotion/re-download
    assert provider.searches[-1] != provider.searches[0]
    assert len(load_human_clip_store(store)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["paid", "bad_source"])
async def test_uncertain_or_paid_metadata_cannot_reach_audio_acquisition(tmp_path, problem):
    provider = PublicProvider()
    setattr(provider, problem, True)

    async def ingest(**kwargs):
        pytest.fail("quarantined material must not be acquired")

    report = await grow_corpus(
        provider,
        store=tmp_path / "store",
        state_path=tmp_path / "state",
        ingest=ingest,
        policy_rules=[],
        scan_limit=5,
        max_new_examples=1,
    )
    assert report["attempted"] == 0
    assert report["queue_counts"] == {"review": 1}


@pytest.mark.asyncio
async def test_metadata_only_mode_never_downloads_and_keeps_queue(tmp_path):
    async def ingest(**kwargs):
        pytest.fail("metadata-only must not acquire")

    report = await grow_corpus(
        PublicProvider(),
        store=tmp_path / "store",
        state_path=tmp_path / "state",
        ingest=ingest,
        policy_rules=[],
        discover_only=True,
        scan_limit=5,
    )
    assert report["attempted"] == 0
    assert report["resolved"] == 0
    assert report["source_visual_newly_pending"] == 1
    assert report["explicit_source_visual_newly_pending"] == 1
    assert report["queue_counts"] == {"source_visual_pending": 1}
    state = CorpusGrowthState.model_validate_json(
        (tmp_path / "state").read_text(encoding="utf-8")
    )
    item = next(iter(state.items.values()))
    assert item.source_url is None and item.source_stream_id is None
    assert item.source_scout["source_identity_verified"] is False
    assert item.source_scout["used_in_learning_or_holdout"] is False
    assert len(item.source_scout["candidate_sources"]) == 1
    assert item.source_scout["candidate_sources"][0]["source_url"] == "https://youtu.be/source"


@pytest.mark.asyncio
async def test_local_metadata_bot_challenge_stops_network_probes_but_keeps_queue(tmp_path):
    from holoclip.corpus_growth import CorpusGrowthItem

    state_path = tmp_path / "corpus.json"
    state = CorpusGrowthState()
    for index in range(7):
        state.items[f"candidate-{index}"] = CorpusGrowthItem(
            clip_url=f"https://youtu.be/{index:011d}",
            query="hololive clips Japanese",
        )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")

    class Challenged(PublicProvider):
        def __init__(self):
            super().__init__()
            self.probed = []

        def search_public_clips(self, query, *, limit, offset):
            self.searches.append((query, offset))
            return []

        def probe(self, url):
            self.probed.append(url)
            raise RuntimeError(
                "Sign in to confirm you're not a bot "
                "https://www.youtube.com/watch?v=SECRET_DATA"
            )

    provider = Challenged()

    async def forbidden(**_kwargs):
        pytest.fail("access challenges must never start ingestion")

    report = await grow_corpus(
        provider, store=tmp_path/"clips.jsonl", state_path=state_path,
        ingest=forbidden, policy_rules=[], discover_only=True, scan_limit=7,
    )
    assert len(provider.probed) == 3
    assert report["metadata_resolution_attempted"] == 3
    assert report["metadata_access_paused"] is True
    assert report["metadata_access_pause_reason"] == (
        "REPEATED_YOUTUBE_CHALLENGE_OR_RATE_LIMIT"
    )
    assert report["metadata_failure_category_counts"] == {
        "YOUTUBE_BOT_OR_INTERACTIVE_CHALLENGE": 3,
    }
    assert report["metadata_resolution_pending"] == 7
    assert report["source_visual_newly_pending"] == 0
    assert report["attempted"] == 0
    saved = CorpusGrowthState.model_validate_json(
        state_path.read_text(encoding="utf-8"),
    )
    assert saved.cursors["corpus_metadata_pending_round_robin"] == 3
    assert sum(item.status == "discovered" for item in saved.items.values()) == 4
    assert sum(item.status == "retryable" for item in saved.items.values()) == 3
    assert all(item.source_stream_id is None for item in saved.items.values())

    # The next scheduled invocation can retry without losing discovery state.
    resumed = Challenged()
    next_report = await grow_corpus(
        resumed, store=tmp_path/"clips.jsonl", state_path=state_path,
        ingest=forbidden, policy_rules=[], discover_only=True, scan_limit=7,
    )
    assert len(resumed.probed) == 3
    assert next_report["metadata_access_paused"] is True
    after = CorpusGrowthState.model_validate_json(
        state_path.read_text(encoding="utf-8"),
    )
    assert after.cursors["corpus_metadata_pending_round_robin"] == 6


@pytest.mark.asyncio
async def test_challenged_source_lookup_counts_after_successful_clip_probe(tmp_path):
    from holoclip.corpus_growth import CorpusGrowthItem

    state_path = tmp_path / "corpus.json"
    state = CorpusGrowthState()
    for index in range(5):
        state.items[f"entry-{index}"] = CorpusGrowthItem(
            clip_url=f"https://youtu.be/{index:011d}", query="hololive",
        )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")

    class SourceLookupChallenged(PublicProvider):
        def __init__(self):
            super().__init__()
            self.probe_calls = 0
            self.source_calls = 0

        def search_public_clips(self, *_args, **_kwargs):
            return []

        def probe(self, url):
            self.probe_calls += 1
            return super().probe(url)

        def source_candidates(self, _metadata):
            self.source_calls += 1
            raise RuntimeError("Sign in to confirm you're not a bot")

    provider = SourceLookupChallenged()

    async def forbidden(**_kwargs):
        pytest.fail("no media ingestion")

    report = await grow_corpus(
        provider, store=tmp_path/"clips.jsonl", state_path=state_path,
        ingest=forbidden, policy_rules=[], scan_limit=5, discover_only=True,
    )
    assert provider.probe_calls == 3
    assert provider.source_calls == 3
    assert report["metadata_access_paused"] is True
    assert report["metadata_failure_category_counts"] == {
        "YOUTUBE_BOT_OR_INTERACTIVE_CHALLENGE": 3,
    }


@pytest.mark.asyncio
async def test_metadata_breaker_also_stops_queued_learning_media_acquisition(tmp_path):
    from holoclip.corpus_growth import CorpusGrowthItem

    path = tmp_path / "corpus.json"
    state = CorpusGrowthState()
    state.items["queued"] = CorpusGrowthItem(
        clip_url="https://youtu.be/queued",
        source_url="https://youtu.be/original",
        source_stream_id="original",
        query="hololive", status="queued",
    )
    for index in range(4):
        state.items[f"fresh-{index}"] = CorpusGrowthItem(
            clip_url=f"https://youtu.be/{index:011d}", query="hololive",
        )
    path.write_text(state.model_dump_json(), encoding="utf-8")

    class Challenged(PublicProvider):
        def search_public_clips(self, *_args, **_kwargs):
            return []

        def probe(self, _url):
            raise RuntimeError("HTTP Error 429: Too Many Requests")

    async def ingest(**_kwargs):
        pytest.fail("queued media download must stop on host-wide challenge")

    result = await grow_corpus(
        Challenged(), store=tmp_path / "clips.jsonl", state_path=path,
        ingest=ingest, policy_rules=[], scan_limit=5,
        max_new_examples=2, discover_only=False,
    )
    assert result["metadata_access_paused"] is True
    assert result["attempted"] == 0
    assert result["queue_counts"]["queued"] == 1
    assert result["metadata_failure_category_counts"] == {"HTTP_RATE_LIMIT": 3}


@pytest.mark.asyncio
async def test_metadata_challenges_are_not_confused_with_unrelated_probe_failures(tmp_path):
    from holoclip.corpus_growth import CorpusGrowthItem

    state_path = tmp_path / "corpus.json"
    state = CorpusGrowthState()
    for index in range(6):
        state.items[f"candidate-{index}"] = CorpusGrowthItem(
            clip_url=f"https://youtu.be/{index:011d}", query="hololive",
        )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")

    class Mixed(PublicProvider):
        def __init__(self):
            super().__init__()
            self.probed = []

        def search_public_clips(self, *_args, **_kwargs):
            return []

        def probe(self, url):
            self.probed.append(url)
            if len(self.probed) % 2:
                raise RuntimeError("HTTP Error 429: Too Many Requests")
            raise RuntimeError("Video unavailable")

    provider = Mixed()

    async def forbidden(**_kwargs):
        pytest.fail("no media ingestion")

    report = await grow_corpus(
        provider, store=tmp_path/"clips.jsonl", state_path=state_path,
        ingest=forbidden, policy_rules=[], discover_only=True, scan_limit=6,
    )
    assert len(provider.probed) == 6
    assert report["metadata_access_paused"] is False
    assert report["metadata_failure_category_counts"] == {
        "HTTP_RATE_LIMIT": 3, "VIDEO_UNAVAILABLE": 3,
    }
    assert report["metadata_resolution_attempted"] == 6


def test_growth_keeps_running_after_count_floors_if_model_is_unready(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = tmp_path / "human_clips.jsonl"
    upsert_human_clip_examples(
        store, [trusted_example(str(i), stream_id=f"s{i // 2}") for i in range(30)]
    )

    class Context:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(cli, "_load_env_file", lambda: None)
    monkeypatch.setattr(cli, "YtDlpSourceProvider", lambda **kwargs: object())
    monkeypatch.setattr(cli, "MeshLLMClient", Context)
    monkeypatch.setattr(cli, "MeshEmbeddingClient", Context)
    targets = []

    async def grow(*args, **kwargs):
        targets.append(kwargs["target_trusted"])
        return {"trusted_examples": 30, "trusted_streams": 15}

    async def refresh(*args, **kwargs):
        return None

    monkeypatch.setattr(cli, "grow_corpus", grow)
    monkeypatch.setattr(cli, "_refresh_editorial_profile", refresh)
    result = CliRunner().invoke(
        cli.app,
        [
            "examples-grow-corpus",
            "--store",
            str(store),
            "--profile",
            str(tmp_path / "profile.json"),
            "--database",
            str(tmp_path / "db"),
            "--cycles",
            "3",
        ],
    )
    assert result.exit_code == 0, result.output
    assert targets == [33, 33, 33]



def test_growth_continues_when_profile_refresh_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = tmp_path / "human_clips.jsonl"
    upsert_human_clip_examples(
        store,
        [trusted_example(str(i), stream_id=f"s{i // 2}") for i in range(30)],
    )

    class Context:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(cli, "_load_env_file", lambda: None)
    monkeypatch.setattr(cli, "YtDlpSourceProvider", lambda **kwargs: object())
    monkeypatch.setattr(cli, "MeshLLMClient", Context)
    monkeypatch.setattr(cli, "MeshEmbeddingClient", Context)
    targets = []

    async def grow(*args, **kwargs):
        targets.append(kwargs["target_trusted"])
        return {"trusted_examples": 30, "trusted_streams": 15}

    async def refresh(*args, **kwargs):
        raise ValueError("strong editorial induction unavailable")

    monkeypatch.setattr(cli, "grow_corpus", grow)
    monkeypatch.setattr(cli, "_refresh_editorial_profile", refresh)
    result = CliRunner().invoke(
        cli.app,
        [
            "examples-grow-corpus",
            "--store",
            str(store),
            "--profile",
            str(tmp_path / "profile.json"),
            "--database",
            str(tmp_path / "db"),
            "--cycles",
            "3",
        ],
    )

    assert result.exit_code == 0, result.output
    assert targets == [33, 33, 33]
    assert "continuing acquisition" in result.output


@pytest.mark.asyncio
async def test_relevance_search_cycles_to_first_page_after_finite_budget(tmp_path):
    class RelevanceProvider(PublicProvider):
        def search_public_clips(self, query, *, limit, offset):
            self.searches.append((query, offset))
            return []  # New uploads may appear after a later refresh.

    provider = RelevanceProvider()

    async def prohibited(**_kwargs):
        pytest.fail("metadata-only mode must not ingest")

    state_path = tmp_path / "state.json"
    state = CorpusGrowthState()
    query = "ホロライブ 切り抜き コラボ"
    state.cursors[query] = 99999
    state.cursors["ホロライブ 切り抜き 雑談"] = 49
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    report = await grow_corpus(
        provider, store=tmp_path / "clips", state_path=state_path,
        ingest=prohibited, policy_rules=[], scan_limit=15,
        discover_only=True,
    )
    assert report["attempted"] == 0
    assert report["empty_search_pages"] >= 1
    assert report["search_page_wraps"] >= 1
    # A huge stale cursor must not generate ytsearch100000 on restart.
    assert all(offset < 50 for _, offset in provider.searches)
    persisted = CorpusGrowthState.model_validate_json(
        state_path.read_text(encoding="utf-8"),
    )
    assert persisted.cursors[query] == 0
    assert persisted.cursors["ホロライブ 切り抜き 雑談"] == 0


@pytest.mark.asyncio
async def test_source_discovery_wraps_after_last_bounded_page(tmp_path):
    provider = PublicProvider()

    async def prohibited(**_kwargs):
        pytest.fail("metadata only")

    path = tmp_path / "state.json"
    state = CorpusGrowthState()
    for query in [
        "ホロライブ 切り抜き 雑談", "ホロライブ 切り抜き コラボ",
        "hololive clips Japanese",
    ]:
        state.cursors[query] = 45
    path.write_text(state.model_dump_json(), encoding="utf-8")
    report = await grow_corpus(
        provider, store=tmp_path / "clips", state_path=path,
        ingest=prohibited, policy_rules=[],
        scan_limit=15, discover_only=True,
    )
    assert report["search_page_wraps"] == 3
    assert report["scanned"] == 15
    assert sorted(offset for _, offset in provider.searches) == [45, 45, 45]
    persisted = CorpusGrowthState.model_validate_json(
        path.read_text(encoding="utf-8"),
    )
    assert all(persisted.cursors[q] == 0 for q, _ in provider.searches)


@pytest.mark.asyncio
async def test_explicit_metadata_source_never_claims_gold_or_requeues_on_restart(tmp_path):
    """Self-sufficient unattended discovery can sample human-cited originals."""
    provider = PublicProvider()

    async def forbidden(**_kwargs):
        pytest.fail("metadata-only must never download media or ingest")

    args = dict(
        store=tmp_path / "examples.jsonl", state_path=tmp_path / "corpus.json",
        ingest=forbidden, policy_rules=[], discover_only=True, scan_limit=5,
    )
    first = await grow_corpus(provider, **args)
    second = await grow_corpus(provider, **args)
    assert first["source_visual_newly_pending"] == 1
    assert first["explicit_source_visual_newly_pending"] == 1
    assert second["source_visual_newly_pending"] == 0
    assert second["explicit_source_visual_newly_pending"] == 0
    assert second["queue_counts"] == {"source_visual_pending": 1}
    state = CorpusGrowthState.model_validate_json(
        (tmp_path / "corpus.json").read_text(encoding="utf-8")
    )
    item = next(iter(state.items.values()))
    assert item.status == "source_visual_pending"
    assert item.source_url is None
    assert item.source_stream_id is None
    assert item.source_scout["status"] == "UNVERIFIED_PUBLIC_SOURCE_SHORTLIST"
    assert item.source_scout["candidate_sources"][0]["source_identity_verified"] is False
    assert not state.holdout_stream_ids


@pytest.mark.asyncio
async def test_metadata_only_explicit_candidate_failing_public_source_policy_is_review(
    tmp_path,
):
    class PrivateSource(PublicProvider):
        def probe(self, url):
            meta = super().probe(url)
            if "source" in url:
                return meta.model_copy(update={"availability": "subscriber_only"})
            return meta

    async def forbidden(**_kwargs):
        pytest.fail("metadata-only must not acquire a private source")

    report = await grow_corpus(
        PrivateSource(), store=tmp_path / "store", state_path=tmp_path / "state",
        ingest=forbidden, policy_rules=[], discover_only=True, scan_limit=5,
    )
    assert report["source_visual_newly_pending"] == 0
    assert report["queue_counts"] == {"review": 1}
    state = CorpusGrowthState.model_validate_json(
        (tmp_path / "state").read_text(encoding="utf-8")
    )
    item = next(iter(state.items.values()))
    assert item.source_url is None and item.source_stream_id is None
    assert item.source_scout["candidate_sources"] == []


@pytest.mark.asyncio
async def test_metadata_failed_probes_rotate_instead_of_starving_fresh_clips(
    tmp_path,
):
    """Seven persistently failing clips must all be attempted within four runs."""
    class FailedProbeProvider(PublicProvider):
        def __init__(self):
            super().__init__()
            self.probes = []

        def search_public_clips(self, query, *, limit, offset):
            self.searches.append((query, offset))
            return []

        def probe(self, url):
            self.probes.append(url)
            raise OSError("offline fixture probe unavailable")

    provider = FailedProbeProvider()

    async def forbidden(**_kwargs):
        pytest.fail("metadata-only may not ingest")

    state_path = tmp_path / "state.json"
    state = CorpusGrowthState()
    urls = [f"https://youtu.be/{i:011d}" for i in range(7)]
    for i, url in enumerate(urls):
        state.items[f"clip-{i}"] = CorpusGrowthItem(
            clip_url=url, query="test", clip_channel="editor",
        )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")

    for _ in range(4):
        report = await grow_corpus(
            provider, store=tmp_path / "clips", state_path=state_path,
            ingest=forbidden, policy_rules=[], discover_only=True,
            scan_limit=2,
        )
        assert report["metadata_resolution_attempted"] == 2
        assert report["metadata_resolution_pending"] == 7
        assert sum(report["queue_counts"].values()) == 7
    assert provider.probes[:7] == urls
    persisted = CorpusGrowthState.model_validate_json(
        state_path.read_text(encoding="utf-8")
    )
    assert persisted.cursors["corpus_metadata_pending_round_robin"] == 8


@pytest.mark.asyncio
async def test_queued_learning_items_do_not_consume_metadata_probe_slots(tmp_path):
    class NoNewSearch(PublicProvider):
        def search_public_clips(self, query, *, limit, offset):
            self.searches.append((query, offset))
            return []

    provider = NoNewSearch()

    async def forbidden(**_kwargs):
        pytest.fail("metadata-only must not ingest")

    path = tmp_path / "state.json"
    state = CorpusGrowthState()
    for i in range(5):
        state.items[f"queued-{i}"] = CorpusGrowthItem(
            clip_url=f"https://youtu.be/{i:011d}", status="queued",
            source_url="https://youtu.be/source",
            source_stream_id="source", query="test", clip_channel="editor",
        )
    state.items["new"] = CorpusGrowthItem(
        clip_url="https://youtu.be/new-clip", query="test", clip_channel="editor",
    )
    path.write_text(state.model_dump_json(), encoding="utf-8")
    report = await grow_corpus(
        provider, store=tmp_path / "store", state_path=path,
        ingest=forbidden, policy_rules=[], discover_only=True, scan_limit=1,
    )
    assert report["metadata_resolution_attempted"] == 1
    assert report["source_visual_newly_pending"] == 1
    assert report["queue_counts"] == {"queued": 5, "source_visual_pending": 1}
    assert provider.searches
    state = CorpusGrowthState.model_validate_json(path.read_text(encoding="utf-8"))
    assert state.items["new"].source_url is None
    assert state.items["new"].source_stream_id is None


@pytest.mark.asyncio
async def test_metadata_only_does_not_steal_ingestion_retry_with_assigned_stream(
    tmp_path,
):
    class NoNewSearch(PublicProvider):
        def search_public_clips(self, query, *, limit, offset):
            self.searches.append((query, offset))
            return []

    provider = NoNewSearch()

    async def forbidden(**_kwargs):
        pytest.fail("metadata-only cannot run ingester")

    path = tmp_path / "state.json"
    state = CorpusGrowthState()
    state.items["ingest-retry"] = CorpusGrowthItem(
        clip_url="https://youtu.be/clip", source_url="https://youtu.be/source",
        source_stream_id="source", status="retryable", query="test",
    )
    path.write_text(state.model_dump_json(), encoding="utf-8")
    report = await grow_corpus(
        provider, store=tmp_path / "clips", state_path=path,
        ingest=forbidden, policy_rules=[], discover_only=True, scan_limit=1,
    )
    assert report["metadata_resolution_attempted"] == 0
    assert report["metadata_resolution_pending"] == 0
    assert report["queue_counts"] == {"retryable": 1}
    state = CorpusGrowthState.model_validate_json(path.read_text(encoding="utf-8"))
    assert state.items["ingest-retry"].source_url == "https://youtu.be/source"
    assert state.items["ingest-retry"].source_stream_id == "source"
    assert state.items["ingest-retry"].source_scout is None


@pytest.mark.asyncio
async def test_corrupt_metadata_pending_cursor_blocks_before_network(tmp_path):
    provider = PublicProvider()

    async def forbidden(**_kwargs):
        pytest.fail("must reject corrupt metadata cursor without network")

    path = tmp_path / "state.json"
    state = CorpusGrowthState()
    state.cursors["corpus_metadata_pending_round_robin"] = -1
    path.write_text(state.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="metadata resolution cursor"):
        await grow_corpus(
            provider, store=tmp_path / "clips", state_path=path,
            ingest=forbidden, policy_rules=[], discover_only=True,
        )
    assert provider.searches == []
