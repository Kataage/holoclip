from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel

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
    "taken from this stream",
    "taken from this video",
    "clip is taken from",
    "clip taken from",
)
_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+")


class YtDlpError(RuntimeError):
    pass


class YtDlpRuntimeInfo(BaseModel):
    version: str
    channel: str
    js_runtime: str
    managed_by: str


def _run_process(
    command: list[str],
    *,
    label: str = "command",
) -> subprocess.CompletedProcess[str]:
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
            f"{label} failed with exit code {exc.returncode}"
            + (f": {detail}" if detail else "")
        ) from exc


def _version_tuple(raw: str) -> tuple[int, ...]:
    match = re.search(r"(\d+(?:\.\d+)+)", raw)
    if match is None:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _runtime_version(executable: str) -> tuple[int, ...]:
    result = _run_process([executable, "--version"], label=executable)
    return _version_tuple(result.stdout.strip() or result.stderr.strip())


def detect_js_runtime() -> tuple[str, str]:
    deno = shutil.which("deno") or shutil.which("deno.exe")
    if deno is not None:
        version = _runtime_version(deno)
        if version >= (2, 3, 0):
            return "deno", deno

    node = shutil.which("node") or shutil.which("node.exe")
    if node is not None:
        version = _runtime_version(node)
        if version >= (22, 0, 0):
            return "node", node

    raise RuntimeError(
        "A supported JavaScript runtime is required for reliable YouTube extraction. "
        "Install Deno >= 2.3.0 (recommended) or Node >= 22.0.0 and make sure it is on PATH."
    )


def _uv_tool_launcher() -> list[str] | None:
    uvx = shutil.which("uvx") or shutil.which("uvx.exe")
    if uvx is not None:
        return [uvx]

    uv = shutil.which("uv") or shutil.which("uv.exe")
    if uv is not None:
        return [uv, "tool", "run"]
    return None


class ManagedYtDlpRuntime:
    """Resolve one fresh yt-dlp runtime per Holoclip process.

    Normal operation uses uv/uvx to refresh yt-dlp package metadata and resolve the latest
    selected-channel release once. Subsequent yt-dlp calls use uv's offline cache for the same
    prerelease/stable requirement so the displayed nightly build identifier is never incorrectly
    reused as a package version specifier.
    """

    def __init__(
        self,
        *,
        channel: str = "nightly",
        executable: str | None = None,
        js_runtime: str | None = None,
        manage_updates: bool = True,
    ) -> None:
        if channel not in {"stable", "nightly"}:
            raise ValueError("yt-dlp channel must be stable or nightly")
        self.channel = channel
        self.explicit_executable = executable
        self.explicit_js_runtime = js_runtime
        self.manage_updates = manage_updates
        self._command_prefix: list[str] | None = None
        self._runtime_info: YtDlpRuntimeInfo | None = None

    @property
    def info(self) -> YtDlpRuntimeInfo:
        self.ensure()
        assert self._runtime_info is not None
        return self._runtime_info

    @property
    def command_prefix(self) -> list[str]:
        self.ensure()
        assert self._command_prefix is not None
        return list(self._command_prefix)

    def _js_runtime(self) -> tuple[str, str | None]:
        if self.explicit_js_runtime:
            raw = self.explicit_js_runtime.strip()
            if ":" in raw:
                name, path = raw.split(":", 1)
                return name.strip(), path.strip() or None
            return raw, None
        name, path = detect_js_runtime()
        return name, path

    def _managed_uv_runtime(
        self,
        launcher: list[str],
        *,
        js_name: str,
    ) -> None:
        resolve = [*launcher, "--refresh-package", "yt-dlp"]
        if self.channel == "nightly":
            resolve.extend(["--prerelease", "allow"])
        resolve.extend(["--from", "yt-dlp[default]", "yt-dlp", "--version"])

        result = _run_process(resolve, label="yt-dlp runtime refresh")
        version = (result.stdout.strip() or result.stderr.strip()).splitlines()[-1].strip()
        if not version:
            raise YtDlpError("yt-dlp runtime refresh returned an empty version")

        # yt-dlp nightly's displayed build identifier is not guaranteed to be a
        # valid package specifier that can be fed back into PyPI/uv as
        # "yt-dlp==<displayed version>". Warm uv's cache once with network
        # access, then keep all commands in this provider on the cached
        # prerelease resolution for the rest of the job.
        prefix = [*launcher, "--offline"]
        if self.channel == "nightly":
            prefix.extend(["--prerelease", "allow"])
        prefix.extend(
            [
                "--from",
                "yt-dlp[default]",
                "yt-dlp",
            ]
        )
        self._command_prefix = prefix
        self._runtime_info = YtDlpRuntimeInfo(
            version=version,
            channel=self.channel,
            js_runtime=js_name,
            managed_by="uvx",
        )

    def _direct_runtime(
        self,
        executable: str,
        *,
        js_name: str,
    ) -> None:
        if self.manage_updates:
            try:
                _run_process(
                    [executable, "--update-to", self.channel],
                    label=f"yt-dlp {self.channel} update",
                )
            except YtDlpError as exc:
                raise YtDlpError(
                    f"{exc}. This yt-dlp executable could not self-update. "
                    "Install uv/uvx so Holoclip can manage a fresh yt-dlp runtime, "
                    "or update the executable manually."
                ) from exc

        version_result = _run_process([executable, "--version"], label="yt-dlp version")
        version = (
            version_result.stdout.strip() or version_result.stderr.strip()
        ).splitlines()[-1].strip()
        if not version:
            raise YtDlpError("yt-dlp version output was empty")

        self._command_prefix = [executable]
        self._runtime_info = YtDlpRuntimeInfo(
            version=version,
            channel=self.channel,
            js_runtime=js_name,
            managed_by="direct",
        )

    def ensure(self) -> None:
        if self._command_prefix is not None:
            return

        js_name, _js_path = self._js_runtime()

        if self.explicit_executable is not None:
            self._direct_runtime(
                self.explicit_executable,
                js_name=js_name,
            )
            return

        launcher = _uv_tool_launcher()
        if launcher is not None and self.manage_updates:
            self._managed_uv_runtime(
                launcher,
                js_name=js_name,
            )
            return

        executable = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
        if executable is None:
            raise RuntimeError(
                "Neither uv/uvx nor a yt-dlp executable was found on PATH. "
                "Holoclip uses uv/uvx to resolve the latest yt-dlp runtime by default."
            )
        self._direct_runtime(
            executable,
            js_name=js_name,
        )

    def refresh(self) -> None:
        self._command_prefix = None
        self._runtime_info = None
        self.ensure()

    def command(self, args: list[str]) -> list[str]:
        self.ensure()
        js_name, js_path = self._js_runtime()
        runtime_arg = js_name if js_path is None else f"{js_name}:{js_path}"
        return [
            *self.command_prefix,
            "--ignore-config",
            "--js-runtimes",
            runtime_arg,
            *args,
        ]


def _metadata_from_payload(
    url: str,
    payload: dict,
    *,
    runtime: YtDlpRuntimeInfo,
) -> SourceMetadata:
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
        provider_version=runtime.version,
        provider_channel=runtime.channel,
        js_runtime=runtime.js_runtime,
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
    if best.score < minimum_score:
        return None
    if len(ordered) == 1:
        return best
    if best.score - ordered[1].score < minimum_margin:
        return None
    return best


class YtDlpSourceProvider:
    def __init__(
        self,
        executable: str | None = None,
        *,
        channel: str = "nightly",
        js_runtime: str | None = None,
        manage_updates: bool = True,
    ) -> None:
        self.runtime = ManagedYtDlpRuntime(
            channel=channel,
            executable=executable,
            js_runtime=js_runtime,
            manage_updates=manage_updates,
        )

    @property
    def runtime_info(self) -> YtDlpRuntimeInfo:
        return self.runtime.info

    def _run_yt_dlp(
        self,
        args: list[str],
        *,
        label: str,
        retry_with_fresh_runtime: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return _run_process(
                self.runtime.command(args),
                label=label,
            )
        except YtDlpError as first_error:
            if not retry_with_fresh_runtime or not self.runtime.manage_updates:
                raise
            self.runtime.refresh()
            try:
                return _run_process(
                    self.runtime.command(args),
                    label=f"{label} after runtime refresh",
                )
            except YtDlpError as second_error:
                raise YtDlpError(
                    f"{second_error}; initial attempt also failed: {first_error}"
                ) from second_error

    def probe(self, url: str) -> SourceMetadata:
        result = self._run_yt_dlp(
            [
                "--no-playlist",
                "--skip-download",
                "--dump-single-json",
                url,
            ],
            label="yt-dlp metadata probe",
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise YtDlpError("yt-dlp metadata output was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise YtDlpError("yt-dlp metadata output must be a JSON object")
        return _metadata_from_payload(
            url,
            payload,
            runtime=self.runtime_info,
        )

    def source_candidates(
        self,
        metadata: SourceMetadata,
    ) -> list[SourceCandidate]:
        description = metadata.description or ""
        if not description:
            return []

        clip_video_id = _youtube_video_id(metadata.webpage_url or metadata.url)
        by_url: dict[str, SourceCandidate] = {}

        lines = description.splitlines()
        for index, line in enumerate(lines):
            # Human clippers commonly write a source label, then the stream title,
            # then the URL on separate lines. Judge the URL with nearby context
            # instead of only the URL's own line.
            context_start = max(0, index - 3)
            # Do not let a source label attached to an earlier URL leak into a
            # later unrelated link such as "related video". A URL closes the
            # preceding context block.
            for prior_index in range(context_start, index):
                if _URL_RE.search(lines[prior_index]):
                    context_start = prior_index + 1
            context_lines = lines[context_start : index + 1]
            context = "\n".join(part.strip() for part in context_lines if part.strip())
            lowered = context.casefold()
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
                    context=context[:500] or None,
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
        directory = (
            output_root
            / _safe_component(metadata.external_id)
            / profile.value
        )
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

        args = [
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
            args.extend(["--format", "bestaudio/best"])
        else:
            args.extend(
                [
                    "--format",
                    "bv*+ba/b",
                    "--merge-output-format",
                    "mkv",
                ]
            )

        args.append(url)
        result = self._run_yt_dlp(
            args,
            label="yt-dlp acquisition",
        )

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
