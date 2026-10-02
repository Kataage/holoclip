from pathlib import Path

from holoclip import media


def test_split_media_audio_returns_offset_chunks(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "stream.mp4"
    source.write_bytes(b"video")
    output = tmp_path / "chunks"

    monkeypatch.setattr(media, "require_ffmpeg", lambda: "ffmpeg")

    def fake_run(command: list[str], check: bool) -> None:
        assert check is True
        assert "-segment_time" in command
        output.mkdir(parents=True, exist_ok=True)
        for index in range(3):
            (output / f"chunk-{index:04d}.mp3").write_bytes(b"audio")

    monkeypatch.setattr(media.subprocess, "run", fake_run)

    chunks = media.split_media_audio(source, output, chunk_seconds=900)

    assert [item.offset_seconds for item in chunks] == [0.0, 900.0, 1800.0]
    assert chunks[0].path.name == "chunk-0000.mp3"


def test_play_media_segment_uses_padding(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "stream.mkv"
    source.write_bytes(b"video")
    captured: list[str] = []

    monkeypatch.setattr(media, "require_ffplay", lambda: "ffplay")

    def fake_run(command: list[str], check: bool) -> None:
        captured.extend(command)
        assert check is False

    monkeypatch.setattr(media.subprocess, "run", fake_run)

    media.play_media_segment(
        source,
        start=100.0,
        end=130.0,
        padding_seconds=10.0,
    )

    assert captured[captured.index("-ss") + 1] == "90.000"
    assert captured[captured.index("-t") + 1] == "50.000"
    assert captured[-1] == str(source)
