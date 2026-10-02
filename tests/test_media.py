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
