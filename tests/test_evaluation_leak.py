from pathlib import Path

from holoclip.evaluation_leak import (
    build_evaluation_leak_report,
    compare_transcripts_for_leakage,
)
from holoclip.examples import HumanClipExample
from holoclip.models import TranscriptSegment
from holoclip.transcript import save_transcript


def seg(start: float, text: str, duration: float = 5.0) -> TranscriptSegment:
    return TranscriptSegment(
        start=start,
        end=start + duration,
        text=text,
    )


def test_same_transcript_is_flagged_as_leakage() -> None:
    transcript = [
        seg(0, "今日はとても大事なお知らせがあります"),
        seg(10, "まず最初に昨日の出来事から話していきます"),
        seg(20, "そのあと予想外の展開になってびっくりしました"),
    ]

    exact_count, exact_ratio, jaccard, suspected = (
        compare_transcripts_for_leakage(transcript, transcript)
    )

    assert exact_count == 3
    assert exact_ratio == 1.0
    assert jaccard == 1.0
    assert suspected is True


def test_unrelated_transcripts_are_not_flagged() -> None:
    candidate = [
        seg(0, "宇宙船のエンジンを修理するゲームを遊びます"),
        seg(10, "惑星の軌道を確認しながら燃料を補給します"),
        seg(20, "最後に基地へ戻ってミッションを終了します"),
    ]
    training = [
        seg(0, "今日はりんごとみかんを買いに行きました"),
        seg(10, "夕食はカレーを作ってみんなで食べました"),
        seg(20, "そのあと猫の写真を見ながら雑談しました"),
    ]

    exact_count, exact_ratio, jaccard, suspected = (
        compare_transcripts_for_leakage(candidate, training)
    )

    assert exact_count == 0
    assert exact_ratio == 0.0
    assert jaccard < 0.15
    assert suspected is False


def test_report_checks_unique_training_sources_and_reports_missing(
    tmp_path: Path,
) -> None:
    candidate_path = tmp_path / "candidate.json"
    training_path = tmp_path / "training.json"
    missing_path = tmp_path / "missing.json"

    candidate = [
        seg(0, "今日はとても大事なお知らせがあります"),
        seg(10, "まず最初に昨日の出来事から話していきます"),
    ]
    save_transcript(candidate, candidate_path)
    save_transcript(candidate, training_path)

    examples = [
        HumanClipExample(
            id="a",
            clip_url="https://example.invalid/a",
            clip_title="a",
            stream_id="stream-a",
            source_transcript_path=str(training_path),
        ),
        HumanClipExample(
            id="b",
            clip_url="https://example.invalid/b",
            clip_title="b",
            stream_id="stream-a",
            source_transcript_path=str(training_path),
        ),
        HumanClipExample(
            id="c",
            clip_url="https://example.invalid/c",
            clip_title="c",
            stream_id="stream-c",
            source_transcript_path=str(missing_path),
        ),
    ]

    report = build_evaluation_leak_report(candidate_path, examples)

    assert report.suspected_overlap is True
    assert report.missing_training_transcripts == 1
    assert len(report.comparisons) == 2

    existing = next(item for item in report.comparisons if item.exists)
    assert existing.stream_id == "stream-a"
    assert existing.example_ids == ["a", "b"]
    assert existing.suspected_overlap is True

    missing = next(item for item in report.comparisons if not item.exists)
    assert missing.stream_id == "stream-c"
    assert missing.segment_count == 0
