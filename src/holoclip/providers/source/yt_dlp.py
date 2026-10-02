from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from holoclip.providers.source.base import (
    AcquiredSource,
    AcquisitionProfile,
    SourceCandidate,
    SourceMetadata,
)


_SOURCE_HINTS = (
    "元動画",
    "元配信",
    "本編",
    "配信元",
    "original",
    "source",
)
_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+")


class YtDlpError(RuntimeError):
    pass


def require_yt_dlp() -> str:
    executable = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if executable is None:
        raise RuntimeError(
            "yt-dlp was not found on PATH. Install the official yt-dlp binary "
            "or package and make sure the 'yt-dlp' command is available."
        )
    return executable


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        if len(detail) > 1200:
            detail = detail[-1200:]
        raise YtDlpError(
            f"yt-dlp failed with exit code {exc.returncode}"
            + (f": {detail}" if detail else "")
        ) from exc


def _metadata_from_payload(url: str, payload: dict) -> SourceMetadata:
    external_id = str(payload.get("id") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not external_id:
        raise YtDlpError("yt-dlp metadata did not contain a video id")
    if not title:
        title = external_id

    duration_raw = payload.get("duration")
    try:
        duration = float(duration_raw) if duration_raw is not None else None
    except (TypeError, ValueError):
        duration = None

    return SourceMetadata(
        provider="yt-dlp",
        url=url,
        webpage_url=str(payload.get("webpage_url") or "").strip() or None,
        external_id=external_id,
        title=title,
        channel=str(payload.get("channel") or payload.get("uploader") or "").strip()
        or None,
        channel_id=str(
            payload.get("channel_id") or payload.get("uploader_id") or ""
        ).strip()
        or None,
        description=str(payload.get("description") or "").strip() or None,
        duration=duration,
        upload_date=str(payload.get("upload_date") or "").strip() or None,
        live_status=str(payload.get("live_status") or "").strip() or None,
        extractor=str(payload.get("extractor_key") or payload.get("extractor") or "").strip()
        or None,
    )


def _youtube_video_id(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    if host in {"youtu.be", "www.youtu.be"}:
        value = parsed.path.strip("/").split("/", 1)[0]
        return value or None

    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parsed.path == "/watch":
            values = parse_qs(parsed.query).get("v", [])
            return values[0] if values and values[0] else None
        for prefix in ("/live/", "/shorts/"):
            if parsed.path.startswith(prefix):
                parts = [part for part in parsed.path.split("/") if part]
                return parts[1] if len(parts) >= 2 else None
    return None


def _trim_url(value: str) -> str:
    return value.rstrip(".,、。!！?？:：;；)]}>")


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned[:100] or "source"


def choose_source_candidate(
    candidates: list[SourceCandidate],
    *,
    minimum_score: float = 0.70,
    minimum_margin: float = 0.15,
) -> SourceCandidate | None:
    if not candidates:
        return None
    ordered = sorted(candidates, key=lambda item: item.score, reverse=True)
    best = ordered[0]
    if len(ordered) == 1:
        return best
    if best.score < minimum_score:
        return None
    if best.score - ordered[1].score < minimum_margin:
        return None
    return best


class YtDlpSourceProvider:
    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or require_yt_dlp()

    def probe(self, url: str) -> SourceMetadata:
        result = _run(
            [
                self.executable,
                "--ignore-config",
                "--no-playlist",
                "--skip-download",
                "--dump-single-json",
                url,
            ]
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise YtDlpError("yt-dlp metadata output was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise YtDlpError("yt-dlp metadata output must be a JSON object")
        return _metadata_from_payload(url, payload)

    def source_candidates(
        self,
        metadata: SourceMetadata,
    ) -> list[SourceCandidate]:
        description = metadata.description or ""
        if not description:
            return []

        clip_video_id = _youtube_video_id(metadata.webpage_url or metadata.url)
        by_url: dict[str, SourceCandidate] = {}

        for line in description.splitlines():
            lowered = line.casefold()
            explicit = any(hint.casefold() in lowered for hint in _SOURCE_HINTS)
            for raw_url in _URL_RE.findall(line):
                url = _trim_url(raw_url)
                source_video_id = _youtube_video_id(url)
                if source_video_id is None:
                    continue
                if clip_video_id and source_video_id == clip_video_id:
                    continue

                score = 0.90 if explicit else 0.45
                reason = (
                    "description_explicit_source_label"
                    if explicit
                    else "description_youtube_link"
                )
                candidate = SourceCandidate(
                    url=url,
                    score=score,
                    context=line.strip()[:500] or None,
                    reason=reason,
                )
                existing = by_url.get(url)
                if existing is None or candidate.score > existing.score:
                    by_url[url] = candidate

        return sorted(
            by_url.values(),
            key=lambda item: (-item.score, item.url),
        )

    @staticmethod
    def _existing_media(directory: Path) -> Path | None:
        if not directory.exists():
            return None
        candidates = []
        for path in directory.glob("media.*"):
            if not path.is_file():
                continue
            if path.name.endswith(".info.json"):
                continue
            if path.suffix in {".part", ".ytdl", ".json"}:
                continue
            candidates.append(path)
        if not candidates:
            return None
        return sorted(candidates)[0]

    def acquire(
        self,
        url: str,
        output_root: Path,
        *,
        profile: AcquisitionProfile = AcquisitionProfile.TRAINING_AUDIO,
    ) -> AcquiredSource:
        metadata = self.probe(url)
        directory = output_root / _safe_component(metadata.external_id)
        directory.mkdir(parents=True, exist_ok=True)
        info_path = directory / "media.info.json"

        existing = self._existing_media(directory)
        if existing is not None:
            return AcquiredSource(
                metadata=metadata,
                profile=profile,
                media_path=existing,
                info_json_path=info_path if info_path.exists() else None,
                reused=True,
            )

        command = [
            self.executable,
            "--ignore-config",
            "--no-playlist",
            "--continue",
            "--no-overwrites",
            "--no-progress",
            "--write-info-json",
            "--paths",
            str(directory),
            "--output",
            "media.%(ext)s",
            "--print",
            "after_move:filepath",
        ]

        if profile is AcquisitionProfile.TRAINING_AUDIO:
            command.extend(["--format", "bestaudio/best"])
        else:
            command.extend(
                [
                    "--format",
                    "bv*+ba/b",
                    "--merge-output-format",
                    "mkv",
                ]
            )

        command.append(url)
        result = _run(command)

        printed_paths = [
            Path(line.strip())
            for line in result.stdout.splitlines()
            if line.strip()
        ]
        media_path = next(
            (path for path in reversed(printed_paths) if path.exists()),
            None,
        )
        if media_path is None:
            media_path = self._existing_media(directory)
        if media_path is None:
            raise YtDlpError(
                f"yt-dlp completed but no media file was found under {directory}"
            )

        return AcquiredSource(
            metadata=metadata,
            profile=profile,
            media_path=media_path,
            info_json_path=info_path if info_path.exists() else None,
            reused=False,
        )
