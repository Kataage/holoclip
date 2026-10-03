from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from holoclip.analysis.candidate_detector import CandidateDetector
from holoclip.config import GroqTranscriptionSettings
from holoclip.execution import (
    StageArtifactCache,
    StageStatus,
    make_input_fingerprint,
)
from holoclip.media import AudioChunk
from holoclip.models import TranscriptChunk, TranscriptSegment
from holoclip.providers.transcription.groq import GroqRateLimitDeferred
from holoclip.repository import SQLiteCanonicalRepository
from holoclip import transcription


class FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_json(
        self,
        messages,
        *,
        model=None,
        temperature=0.1,
        max_tokens=4096,
        reasoning_effort=None,
    ) -> dict[str, Any]:
        self.calls += 1
        return {
            "candidates": [
                {
                    "start": 1,
                    "end": 5,
                    "working_title": "候補",
                    "setup": "前振り",
                    "development": "展開",
                    "payoff": "オチ",
                    "why_interesting": "面白い",
                    "traits": ["reveal"],
                    "confidence": 0.8,
                }
            ]
        }


def test_stage_cache_reuses_exact_version_and_not_changed_version(
    tmp_path: Path,
) -> None:
    with SQLiteCanonicalRepository(tmp_path / "state.db") as repository:
        cache = StageArtifactCache(repository, tmp_path / "artifacts")
        fingerprint = make_input_fingerprint({"value": 1})

        run = cache.start(
            stage="example",
            subject_type="unit",
            subject_id="a",
            input_fingerprint=fingerprint,
            stage_version="1",
            provider="test",
            model="model-a",
        )
        cache.succeed_json(run, {"ok": True}, kind="example")

        assert cache.reusable_json(
            stage="example",
            subject_type="unit",
            subject_id="a",
            input_fingerprint=fingerprint,
            stage_version="1",
            provider="test",
            model="model-a",
        ) == {"ok": True}

        assert cache.reusable_json(
            stage="example",
            subject_type="unit",
            subject_id="a",
            input_fingerprint=fingerprint,
            stage_version="2",
            provider="test",
            model="model-a",
        ) is None


@pytest.mark.asyncio
async def test_broad_candidate_discovery_reuses_completed_chunk(
    tmp_path: Path,
) -> None:
    llm = FakeLLM()
    chunk = TranscriptChunk(
        id="chunk-0000",
        start=0,
        end=10,
        text="[00:00:01] テストです",
        segment_indices=[0],
    )

    with SQLiteCanonicalRepository(tmp_path / "state.db") as repository:
        cache = StageArtifactCache(repository, tmp_path / "artifacts")
        detector = CandidateDetector(
            llm,
            fast_model="fast",
            strong_model="strong",
            stage_cache=cache,
            cache_provider="test",
        )

        first = await detector.discover_broad([chunk])
        second = await detector.discover_broad([chunk])

    assert llm.calls == 1
    assert len(first) == 1
    assert len(second) == 1
    assert second[0].working_title == "候補"


@pytest.mark.asyncio
async def test_transcription_retry_reuses_successful_chunk_after_quota_block(
    tmp_path: Path,
    monkeypatch,
) -> None:
    media_path = tmp_path / "stream.mkv"
    media_path.write_bytes(b"video")
    offsets_called: list[float] = []
    quota_failures = {"remaining": 1}

    def fake_split(
        _media_path: Path,
        output_dir: Path,
        *,
        chunk_seconds: int,
    ) -> list[AudioChunk]:
        assert chunk_seconds == 900
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(2):
            path = output_dir / f"chunk-{index:04d}.mp3"
            path.write_bytes(b"audio")
            paths.append(
                AudioChunk(
                    path=path,
                    offset_seconds=float(index * chunk_seconds),
                )
            )
        return paths

    class FakeTranscriber:
        def __init__(self, _settings) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def transcribe_file(
            self,
            _path: Path,
            *,
            offset_seconds: float = 0.0,
        ) -> list[TranscriptSegment]:
            offsets_called.append(offset_seconds)
            if offset_seconds == 900 and quota_failures["remaining"]:
                quota_failures["remaining"] -= 1
                raise GroqRateLimitDeferred(3600)
            return [
                TranscriptSegment(
                    start=offset_seconds,
                    end=offset_seconds + 10,
                    text=f"chunk {int(offset_seconds)}",
                )
            ]

    monkeypatch.setattr(transcription, "split_media_audio", fake_split)
    monkeypatch.setattr(
        transcription,
        "MeshTranscriber",
        FakeTranscriber,
    )

    settings = GroqTranscriptionSettings(
        api_key="test-key",
        rpm=60000,
        chunk_seconds=900,
    )

    with SQLiteCanonicalRepository(tmp_path / "state.db") as repository:
        cache = StageArtifactCache(repository, tmp_path / "artifacts")

        with pytest.raises(GroqRateLimitDeferred):
            await transcription.transcribe_media(
                media_path,
                settings,
                stage_cache=cache,
            )

        media_identity = transcription.file_input_identity(media_path)
        second_fingerprint = make_input_fingerprint(
            {
                "media": media_identity,
                "chunk_index": 1,
                "offset_seconds": 900.0,
                "chunk_seconds": 900,
                "language": settings.language,
            }
        )
        blocked = repository.latest_stage_run(
            stage="transcription_chunk",
            subject_type="local_media_chunk",
            subject_id=f"{media_identity['path']}#chunk-0001",
            input_fingerprint=second_fingerprint,
            stage_version="mesh-1",
            provider="capability_mesh",
            model="asr_segment_timestamps",
            prompt_version=None,
        )
        assert blocked is not None
        assert blocked.status is StageStatus.BLOCKED_QUOTA
        assert blocked.retry_after == 3600

        result = await transcription.transcribe_media(
            media_path,
            settings,
            stage_cache=cache,
        )

    assert offsets_called == [0.0, 900.0, 900.0]
    assert [item.text for item in result] == ["chunk 0", "chunk 900"]
