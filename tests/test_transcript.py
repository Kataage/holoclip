from holoclip.models import TranscriptSegment
from holoclip.transcript import chunk_segments


def test_chunk_segments_uses_overlap() -> None:
    segments = [
        TranscriptSegment(start=0, end=20, text="a"),
        TranscriptSegment(start=50, end=70, text="b"),
        TranscriptSegment(start=100, end=120, text="c"),
        TranscriptSegment(start=150, end=170, text="d"),
    ]

    chunks = chunk_segments(
        segments,
        window_seconds=120,
        overlap_seconds=60,
    )

    assert len(chunks) >= 2
    assert "a" in chunks[0].text
    assert "c" in chunks[0].text
    assert any("c" in chunk.text for chunk in chunks[1:])


def test_empty_transcript_returns_no_chunks() -> None:
    assert chunk_segments([]) == []
