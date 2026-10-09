from types import SimpleNamespace

import pytest

from holoclip.corpus_growth import CorpusGrowthState, grow_corpus
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
