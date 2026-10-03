import json

import pytest
from typer.testing import CliRunner

from holoclip.cli import app, _load_ready_editorial_profile, _refresh_editorial_profile
from holoclip.config import NimSettings
from holoclip.analysis.candidate_detector import CandidateDetector
from holoclip.domain_adapters import candidate_to_clip_timeline
from holoclip.editorial_cards import boundary_measurements
from holoclip.editorial_profile import (
    PatternMaturity,
    ReadinessThresholds,
    _batch_plan,
    _pack_plan,
    _pattern,
    _valid_partition,
    build_editorial_learning_cards,
    card_fingerprint,
    editorial_profile_prompt_payload,
    induce_editorial_profile,
    load_editorial_profile,
    save_editorial_profile,
    validate_editorial_profile,
)
from holoclip.editorial_planning import validate_edit_plan, plan_candidate_boundaries
from holoclip.examples import upsert_human_clip_examples
from holoclip.execution import StageArtifactCache
from holoclip.models import ClipCandidate, TranscriptChunk, TranscriptSegment
from holoclip.repository import SQLiteCanonicalRepository
from holoclip.transcript import save_transcript
from test_editorial_profile import trusted_example


def test_cards_keep_full_selected_omitted_evidence_and_measured_timing(tmp_path):
    example = trusted_example("a", stream_id="s")
    example.clip_transcript_path = "clip.json"
    example.source_transcript_path = "source.json"
    save_transcript(
        [
            TranscriptSegment(start=0, end=10, text="setup"),
            TranscriptSegment(start=10, end=20, text="turn"),
            TranscriptSegment(start=20, end=50, text="payoff"),
            TranscriptSegment(start=50, end=60, text="reaction"),
        ],
        tmp_path / "clip.json",
    )
    save_transcript(
        [
            TranscriptSegment(start=100, end=125, text="selected setup"),
            TranscriptSegment(start=125, end=145, text="omitted redundancy"),
            TranscriptSegment(start=145, end=180, text="selected payoff"),
        ],
        tmp_path / "source.json",
    )
    card = build_editorial_learning_cards([example], transcript_root=tmp_path)[0]
    assert card.selected_ranges[0]["source_excerpt"].endswith("selected setup")
    assert card.removed_ranges[0]["excerpt"].endswith("omitted redundancy")
    assert card.contrast_evidence[0]["positive_example_id"] == "a"
    assert (
        boundary_measurements(
            card, {"development_start_segment_id": "clip:1", "payoff_end_segment_id": "clip:2"}
        )["post_payoff_seconds"]
        == 10
    )
    assert (
        boundary_measurements(
            card, {"development_start_segment_id": "invented", "payoff_end_segment_id": "clip:2"}
        )["setup_lead_seconds"]
        is None
    )
    assert card == build_editorial_learning_cards([example], transcript_root=tmp_path)[0]
    assert card_fingerprint([card]) == card_fingerprint(
        build_editorial_learning_cards([example], transcript_root=tmp_path)
    )


def test_missing_clip_coverage_is_not_mislabeled_an_editorial_cut():
    example = trusted_example("a", stream_id="s")
    example.alignment_segments[1].clip_start = 28
    card = build_editorial_learning_cards([example])[0]
    assert not card.removed_ranges
    assert len(card.uncertain_gaps) == 1
    assert card.observed_cut_style == "unknown"


def test_review_rejected_and_legacy_negatives_are_excluded():
    good = trusted_example("a", stream_id="s")
    review = trusted_example("b", stream_id="s2").model_copy(update={"alignment_confidence": 0.65})
    rejected = trusted_example("c", stream_id="s3").model_copy(update={"alignment_confidence": 0.4})
    good.weak_negative_method = "legacy"
    cards = build_editorial_learning_cards([good, review, rejected])
    assert [c.example_id for c in cards] == ["a"]
    assert not cards[0].weak_negative_refs
    with pytest.raises(ValueError, match="duplicate"):
        build_editorial_learning_cards([good, good])


def test_support_and_stream_counts_ignore_model_claims_and_cross_example_negatives():
    cards = build_editorial_learning_cards(
        [trusted_example(str(i), stream_id="same") for i in range(3)]
    )
    by_id = {c.example_id: c for c in cards}
    pattern = _pattern(
        {
            "name": "turn",
            "mechanism": "payoff",
            "support_example_ids": ["0", "1", "2", "fiction"],
            "support_stream_ids": ["s1", "s2", "s3"],
            "maturity": "core",
            "confidence": "nan",
            "weak_negative_refs": ["0:weak:0", "fiction:weak:0"],
        },
        by_id,
        ReadinessThresholds(),
    )
    assert pattern.maturity is PatternMaturity.EMERGING
    assert pattern.support_stream_ids == ["same"]
    assert pattern.weak_negative_refs == ["0:weak:0"]
    assert pattern.confidence == 0.5
    assert (
        _pattern(
            {"name": "none", "mechanism": "fiction", "support_example_ids": ["fiction"]},
            by_id,
            ReadinessThresholds(),
        )
        is None
    )


class CorpusLLM:
    def __init__(self):
        self.calls = []
        self.fail_merge = False

    async def chat_json(self, messages, **kwargs):
        instruction, raw = messages[1]["content"].split("\n", 1)
        data = json.loads(raw)
        self.calls.append((instruction, data, kwargs))
        fields = {
            "name": "turn",
            "mechanism": "setup changes into payoff",
            "observable_cues": ["change"],
            "must_have": ["payoff"],
            "anti_patterns": ["no change"],
            "boundary_strategy": ["retain setup and reaction"],
        }
        if instruction.startswith("Compare selected"):
            return {"selection_mechanism": "setup changes into payoff"}
        if instruction.startswith("Assess each"):
            return {
                "assessments": [
                    {"pattern_id": p["pattern_id"], "supports": True, "reason": "actual turn"}
                    for p in data["patterns"]
                ]
            }
        if instruction.startswith("Induce patterns"):
            ids = [x["example_id"] for x in data]
            return {
                **{
                    k: [{"text": k, "support_example_ids": ids}]
                    for k in ("global_principles", "rejection_principles", "boundary_principles")
                },
                "patterns": [{**fields, "support_example_ids": ids + ["invented"]}],
            }
        if self.fail_merge:
            self.fail_merge = False
            return {"patterns": [{**fields, "input_pattern_ids": ["invented"]}]}
        return {
            "patterns": [{**fields, "input_pattern_ids": [x["input_pattern_id"] for x in data]}]
        }


@pytest.mark.asyncio
async def test_resume_cache_incremental_batches_and_reproducible_provenance(tmp_path):
    examples = [trusted_example(str(i), stream_id=f"s{i}") for i in range(3)]
    llm = CorpusLLM()
    with SQLiteCanonicalRepository(tmp_path / "db") as repo:
        cache = StageArtifactCache(repo, tmp_path / "artifacts")
        args = dict(
            model="strong",
            observation_model="fast",
            batch_size=2,
            stage_cache=cache,
            plan_path=tmp_path / "plan.json",
            cards_directory=tmp_path / "cards",
        )
        llm.fail_merge = True
        with pytest.raises(ValueError, match="invalid response"):
            await induce_editorial_profile(llm, examples, **args)
        partial_calls = sum(c[0].startswith("Induce patterns") for c in llm.calls)
        profile = await induce_editorial_profile(llm, examples, **args)
        assert sum(c[0].startswith("Induce patterns") for c in llm.calls) == partial_calls
        calls = len(llm.calls)
        repeated = await induce_editorial_profile(llm, list(reversed(examples)), **args)
        assert len(llm.calls) == calls
        assert profile == repeated
        assert profile.patterns[0].support_example_ids == ["0", "1", "2"]
        assert profile.patterns[0].maturity is PatternMaturity.CORE
        assert not profile.readiness.ready
        added = await induce_editorial_profile(
            llm, examples + [trusted_example("a", stream_id="sa")], **args
        )
        assert sum(c[0].startswith("Induce patterns") for c in llm.calls) == partial_calls + 1
        assert added.corpus_fingerprint != profile.corpus_fingerprint
        assert added.batch_example_ids[:2] == profile.batch_example_ids
        save_editorial_profile(added, tmp_path / "profile.json")
        assert load_editorial_profile(tmp_path / "profile.json") == added


def test_hundreds_of_cards_keep_stable_bounded_batches():
    cards = build_editorial_learning_cards(
        [trusted_example(str(i), stream_id=f"s{i}") for i in range(340)]
    )
    plan = _batch_plan(cards, [], 8)
    assert len(plan) == 43
    assert max(map(len, plan)) <= 8
    assert len({x for b in plan for x in b}) == 340
    assert _valid_partition(
        {"patterns": [{"name": "a", "mechanism": "b", "input_pattern_ids": ["0", "1"]}]}, 2
    )
    assert not _valid_partition(
        {"patterns": [{"name": "a", "mechanism": "b", "input_pattern_ids": ["0", "0"]}]}, 2
    )
    summaries = {str(i): {"long_observation": "x" * 40000} for i in range(8)}
    assert _pack_plan([list(summaries)], summaries) == [[str(i)] for i in range(8)]


def test_forged_readiness_does_not_override_actual_provenance(learned_ready_profile):
    profile = learned_ready_profile.model_copy(deep=True)
    profile.learning_cards = profile.learning_cards[:9]
    profile.trusted_examples = 900
    assert editorial_profile_prompt_payload(profile) is None
    checked = validate_editorial_profile(profile)
    assert checked.trusted_examples == 9
    assert not checked.readiness.ready


def test_diagnostic_fast_profile_cannot_become_a_production_prior(learned_ready_profile):
    profile = learned_ready_profile.model_copy(deep=True)
    profile.induction_config["production_eligible"] = False
    assert editorial_profile_prompt_payload(profile) is None
    assert any("not eligible" in r for r in validate_editorial_profile(profile).readiness.reasons)


def test_url_aliases_cannot_inflate_trusted_human_clip_counts():
    first = trusted_example("a", stream_id="s")
    alias = trusted_example("b", stream_id="other")
    alias.clip_url = "https://www.youtube.com/watch?v=a&t=5"
    with pytest.raises(ValueError, match="duplicate trusted human video identity"):
        build_editorial_learning_cards([first, alias])


@pytest.mark.asyncio
async def test_profile_content_change_invalidates_discovery_cache(tmp_path, learned_ready_profile):
    class DiscoveryLLM:
        calls = 0

        async def chat_json(self, *args, **kwargs):
            self.calls += 1
            return {"candidates": []}

    llm = DiscoveryLLM()
    chunk = TranscriptChunk(id="c", start=0, end=60, text="actual transcript", segment_indices=[0])
    with SQLiteCanonicalRepository(tmp_path / "db") as repo:
        cache = StageArtifactCache(repo, tmp_path / "artifacts")

        async def run(profile):
            detector = CandidateDetector(
                llm,
                fast_model="fast",
                strong_model="strong",
                stage_cache=cache,
                editorial_profile=profile,
            )
            await detector.discover_broad([chunk])

        await run(learned_ready_profile)
        await run(learned_ready_profile)
        assert llm.calls == 1
        changed = learned_ready_profile.model_copy(deep=True)
        changed.patterns[0].boundary_strategy = ["new supported ending principle"]
        await run(changed)
        assert llm.calls == 2


def test_review_is_blocked_before_readiness_and_existing_labels_remain(tmp_path):
    feedback = tmp_path / "feedback.jsonl"
    feedback.write_text("existing holdout labels\n", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "label",
            "stream",
            "candidate",
            "accept",
            "--feedback-file",
            str(feedback),
            "--editorial-profile",
            str(tmp_path / "absent.json"),
        ],
    )
    assert result.exit_code == 6
    assert "frozen validation data" in result.output
    assert feedback.read_text(encoding="utf-8") == "existing holdout labels\n"


def test_stale_profile_is_ignored_by_cli(tmp_path, learned_ready_profile):
    path = tmp_path / "editorial_profile.json"
    save_editorial_profile(learned_ready_profile, path)
    upsert_human_clip_examples(
        tmp_path / "human_clips.jsonl", [trusted_example("new", stream_id="new-source")]
    )
    assert _load_ready_editorial_profile(path) is None


@pytest.mark.asyncio
async def test_normal_flow_relearns_changed_trusted_corpus_without_operator_labels(tmp_path):
    path = tmp_path / "editorial_profile.json"
    store = tmp_path / "human_clips.jsonl"
    upsert_human_clip_examples(
        store, [trusted_example("0", stream_id="s0"), trusted_example("1", stream_id="s1")]
    )
    settings = NimSettings(api_key="test", model="strong", fast_model="fast")
    llm = CorpusLLM()
    with SQLiteCanonicalRepository(tmp_path / "db") as repo:
        cache = StageArtifactCache(repo, tmp_path / "artifacts")
        args = dict(settings=settings, stage_cache=cache, path=path)
        assert await _refresh_editorial_profile(llm, **args) is None
        previous = load_editorial_profile(path)
        calls = len(llm.calls)
        assert await _refresh_editorial_profile(llm, **args) is None
        assert len(llm.calls) == calls
        upsert_human_clip_examples(store, [trusted_example("2", stream_id="s2")])
        assert await _refresh_editorial_profile(llm, **args) is None
        updated = load_editorial_profile(path)
        assert updated.trusted_examples == 3
        assert updated.corpus_fingerprint != previous.corpus_fingerprint


@pytest.mark.asyncio
async def test_measured_multi_cut_planning_and_timeline_adapter(learned_ready_profile):
    profile = learned_ready_profile
    pattern = editorial_profile_prompt_payload(profile)["patterns"][0]["id"]
    rows = [{"id": "s0", "start": 10, "end": 20}, {"id": "s1", "start": 30, "end": 50}]
    payload = {
        "pattern_ids": [pattern, "invented"],
        "spans": [
            {"start_segment_id": "s0", "end_segment_id": "s0"},
            {"start_segment_id": "s1", "end_segment_id": "s1"},
        ],
        "reason": "necessary setup and payoff",
    }
    plan = validate_edit_plan(payload, rows=rows, profile=profile)
    assert plan.selected_seconds == 30
    assert plan.omitted_seconds == 10
    candidate = ClipCandidate(
        start=10,
        end=50,
        working_title="story",
        setup="setup",
        development="turn",
        payoff="payoff",
        why_interesting="reason",
        editorial_edit_plan=plan,
    )
    timeline = candidate_to_clip_timeline("asset", candidate)
    assert len(timeline.segments) == 2
    assert timeline.estimated_duration == 30
    bad = {**payload, "spans": [{"start_segment_id": "invented", "end_segment_id": "s0"}]}
    assert validate_edit_plan(bad, rows=rows, profile=profile) is None
    unready = profile.model_copy(update={"learning_cards": []})
    assert await plan_candidate_boundaries(
        None, [candidate], [], profile=unready, model="strong"
    ) == [candidate]
