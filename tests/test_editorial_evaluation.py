import pytest

from holoclip.editorial_evaluation import run_editorial_ab
from holoclip.execution import StageArtifactCache
from holoclip.models import TranscriptSegment
from holoclip.repository import SQLiteCanonicalRepository
from holoclip.transcript import save_transcript
from test_editorial_profile import trusted_example


@pytest.mark.asyncio
async def test_ab_blocks_unready_and_seen_streams_before_api(learned_ready_profile, tmp_path):
    args = dict(
        transcript=tmp_path / "unused", examples=[], fast_model="fast", strong_model="strong"
    )
    with pytest.raises(ValueError, match="readiness=false"):
        await run_editorial_ab(
            None,
            profile=learned_ready_profile.model_copy(update={"learning_cards": []}),
            stream_id="new",
            **args,
        )
    with pytest.raises(ValueError, match="unseen"):
        await run_editorial_ab(None, profile=learned_ready_profile, stream_id="s0", **args)


@pytest.mark.asyncio
async def test_automatic_blind_ab_is_resumable_and_not_human_acceptance(
    tmp_path, learned_ready_profile
):
    path = tmp_path / "unseen.json"
    training = tmp_path / "training.json"
    save_transcript(
        [
            TranscriptSegment(
                start=0, end=60, text="An entirely new conversation about an unexpected ending."
            )
        ],
        path,
    )
    save_transcript(
        [
            TranscriptSegment(
                start=0, end=60, text="Unrelated previous source material with a different event."
            )
        ],
        training,
    )
    example = trusted_example("e0", stream_id="s0").model_copy(
        update={"source_transcript_path": str(training)}
    )

    class LLM:
        def __init__(self):
            self.messages = []

        async def chat_json(self, messages, **kwargs):
            self.messages.append(messages)
            if "Blindly compare" in messages[1]["content"]:
                assert "profile_fingerprint" not in messages[1]["content"]
                return {"winner": "tie", "reason": "empty diagnostic sets"}
            return {"candidates": []}

    llm = LLM()
    with SQLiteCanonicalRepository(tmp_path / "db") as repo:
        cache = StageArtifactCache(repo, tmp_path / "artifacts")
        args = dict(
            transcript=path,
            stream_id="new",
            examples=[example],
            profile=learned_ready_profile,
            fast_model="fast",
            strong_model="strong",
            stage_cache=cache,
        )
        report = await run_editorial_ab(llm, **args)
        count = len(llm.messages)
        assert await run_editorial_ab(llm, **args) == report
        assert len(llm.messages) == count
    assert report["automatic_winner"] == "tie"
    assert report["human_validation"] == "not_requested"
    assert not report["leak_check"]["suspected_overlap"]
