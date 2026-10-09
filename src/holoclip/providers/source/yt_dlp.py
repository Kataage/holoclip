from __future__ import annotations

import json
import hashlib
import math
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel

from holoclip.source_ytdlp_pacing import LocalYtDlpDeferred, LocalYtDlpPacer
from holoclip.source_ytdlp_lan_coordinator import LanCoordinatorClient
from holoclip.source_metadata_failure_summary import _public_probe_failure_category
from holoclip.providers.source.base import (
    AcquiredSource,
    AcquiredVideoRange,
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


class YtDlpTimeout(YtDlpError):
    """A subprocess deadline expired; never retry a potentially hung child."""


# Hard deadlines keep Task Scheduler from permanently skipping future runs
# when a native extractor/update/FFmpeg subprocess hangs.
VERSION_TIMEOUT_SECONDS = 30
RUNTIME_UPDATE_TIMEOUT_SECONDS = 180
METADATA_TIMEOUT_SECONDS = 240
BOUNDED_VIDEO_TIMEOUT_SECONDS = 1200
MEDIA_ACQUISITION_TIMEOUT_SECONDS = 1800


class YtDlpRuntimeInfo(BaseModel):
    version: str
    channel: str
    js_runtime: str
    managed_by: str


def _terminate_native_tree(process: subprocess.Popen[str]) -> bool:
    """Terminate descendants of one private native process tree, never our own.

    POSIX children share a brand-new session. Windows taskkill /T /F targets
    only descendants of this exact Popen PID. It can miss processes that
    deliberately detached or were reparented; failures must stay explicit.
    """
    tree_requested = False
    if os.name == "nt":
        # Always invoke the real OS binary rather than a PATH-shadowed shell
        # command. No shell interpolation, user URL or untrusted flags.
        windows = os.environ.get("SystemRoot") or os.environ.get("WINDIR", r"C:\Windows")
        taskkill = Path(windows) / "System32" / "taskkill.exe"
        try:
            result = subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                check=False, capture_output=True, timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            tree_requested = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            tree_requested = False
    else:
        try:
            # start_new_session=True is mandatory in _run_process below.
            os.killpg(process.pid, signal.SIGKILL)
            tree_requested = True
        except ProcessLookupError:
            # The entire private process group may already be gone.
            tree_requested = process.poll() is not None
        except OSError:
            tree_requested = False
    # Fail-safe even if tree enumeration/termination itself was unavailable.
    try:
        if process.poll() is None:
            process.kill()
    except (OSError, ProcessLookupError):
        tree_requested = False
    return tree_requested


def _reap_native_child(process: subprocess.Popen[str]) -> bool:
    """Bound pipe draining after timeout; a detached child must not re-hang us."""
    try:
        process.communicate(timeout=5)
        return process.poll() is not None
    except subprocess.TimeoutExpired:
        # On systems where descendants kept stdout/stderr open, terminate
        # our direct child and close inherited pipes without blocking forever.
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            return False
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        return False


def _run_process(
    command: list[str],
    *,
    label: str = "command",
    timeout_seconds: int = METADATA_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
        raise ValueError("Invalid yt-dlp subprocess deadline")
    # subprocess.run(timeout=...) kills only the direct process. Start a
    # dedicated OS tree to keep yt-dlp's FFmpeg/downloader descendants in-scope.
    options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt" else {"start_new_session": True}
    )
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", **options,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        tree_requested = _terminate_native_tree(process)
        reaped = _reap_native_child(process)
        cleanup = (
            "TREE_TERMINATION_REQUESTED"
            if tree_requested and reaped else "TREE_CLEANUP_UNCONFIRMED"
        )
        # No command/URL/content in exception text. A failed cleanup stays
        # explicitly unknown; downstream must not treat it as a measurement.
        raise YtDlpTimeout(
            f"{label} exceeded {timeout_seconds}s process deadline; "
            f"cleanup={cleanup}"
        ) from exc
    if process.returncode:
        detail = (stderr or stdout or "").strip()
        if len(detail) > 1200:
            detail = detail[-1200:]
        raise YtDlpError(
            f"{label} failed with exit code {process.returncode}"
            + (f": {detail}" if detail else "")
        )
    return subprocess.CompletedProcess(
        command, process.returncode, stdout=stdout, stderr=stderr,
    )


def _version_tuple(raw: str) -> tuple[int, ...]:
    match = re.search(r"(\d+(?:\.\d+)+)", raw)
    if match is None:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _runtime_version(executable: str) -> tuple[int, ...]:
    result = _run_process(
        [executable, "--version"], label="JavaScript runtime version",
        timeout_seconds=VERSION_TIMEOUT_SECONDS,
    )
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

        result = _run_process(
            resolve, label="yt-dlp runtime refresh",
            timeout_seconds=RUNTIME_UPDATE_TIMEOUT_SECONDS,
        )
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
                    timeout_seconds=RUNTIME_UPDATE_TIMEOUT_SECONDS,
                )
            except YtDlpTimeout:
                raise
            except YtDlpError as exc:
                raise YtDlpError(
                    f"{exc}. This yt-dlp executable could not self-update. "
                    "Install uv/uvx so Holoclip can manage a fresh yt-dlp runtime, "
                    "or update the executable manually."
                ) from exc

        version_result = _run_process(
            [executable, "--version"], label="yt-dlp version",
            timeout_seconds=VERSION_TIMEOUT_SECONDS,
        )
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
        availability=str(payload.get("availability") or "").strip() or None,
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
        pacing_path: Path | None = None,
    ) -> None:
        # Managed public yt-dlp calls share an on-disk OS-process gate across
        # separate Windows scheduled and manual CLI invocations. Explicit
        # direct-executable library callers opt in via pacing_path.
        gate_path = (
            pacing_path if pacing_path is not None else (
                Path("data/runtime/yt-dlp-access.sqlite3")
                if executable is None else None
            )
        )
        self.pacer = LocalYtDlpPacer(gate_path) if gate_path is not None else None
        # Opt-in multi-PC mode is STRICTLY fail-closed. Partial setup fails
        # rather than treating a separate-PC local SQLite gate as sufficient.
        self.lan_coordinator = LanCoordinatorClient.from_env()
        self.runtime = ManagedYtDlpRuntime(
            channel=channel,
            executable=executable,
            js_runtime=js_runtime,
            manage_updates=manage_updates,
        )
        self._metadata_cache: dict[str, SourceMetadata] = {}

    def search_public_clips(self, query: str, *, limit: int = 10, offset: int = 0) -> list[SourceMetadata]:
        """Public metadata discovery only. Resolution/policy gates precede acquisition."""
        if not 1 <= limit <= 50 or offset < 0:
            raise ValueError("invalid public discovery page")
        result = self._run_yt_dlp(
            ["--skip-download", "--flat-playlist", "--dump-single-json",
             "--playlist-start", str(offset + 1), "--playlist-end", str(offset + limit),
             f"ytsearch{offset + limit}:{query}"], label="public clip metadata discovery")
        payload = json.loads(result.stdout)
        items = []
        for row in payload.get("entries", []):
            video_id = row.get("id") if isinstance(row, dict) else None
            if not video_id or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                continue
            url = f"https://www.youtube.com/watch?v={video_id}"
            items.append(SourceMetadata(provider="yt-dlp", url=url, webpage_url=url,
                external_id=video_id, title=row.get("title") or video_id,
                channel=row.get("channel"), channel_id=row.get("channel_id"), duration=row.get("duration")))
        return items

    @property
    def runtime_info(self) -> YtDlpRuntimeInfo:
        return self.runtime.info

    def _run_yt_dlp(
        self,
        args: list[str],
        *,
        label: str,
        timeout_seconds: int = METADATA_TIMEOUT_SECONDS,
        retry_with_fresh_runtime: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        # Conservative yt-dlp-native pacing applies *inside* each subprocess,
        # including all metadata extraction and section download requests.
        # The shared OS-process pacer additionally protects separate searches,
        # probes and downloads from back-to-back launches.
        safe_args = [
            "--sleep-requests", "2",
            "--sleep-interval", "10", "--max-sleep-interval", "20",
            "--concurrent-fragments", "1",
            "--retries", "2", "--fragment-retries", "2",
            "--extractor-retries", "1",
            "--retry-sleep", "extractor:exp=2:15",
            *args,
        ]

        def once(*, current_label: str):
            try:
                # Coordinator first: a locally successful reservation must
                # NEVER override a missing/unreachable shared-IP permission.
                # The second local gate remains defense in depth.
                from contextlib import ExitStack

                with ExitStack() as stack:
                    if self.lan_coordinator is not None:
                        stack.enter_context(
                            self.lan_coordinator.reserve(
                                timeout_seconds=timeout_seconds,
                            )
                        )
                    if self.pacer is not None:
                        stack.enter_context(
                            self.pacer.reserve(timeout_seconds=timeout_seconds)
                        )
                    cmd = self.runtime.command(safe_args)
                    try:
                        return _run_process(
                            cmd, label=current_label, timeout_seconds=timeout_seconds,
                        )
                    except YtDlpError as exc:
                        kind = _public_probe_failure_category(str(exc))
                        if kind in {
                            "YOUTUBE_BOT_OR_INTERACTIVE_CHALLENGE",
                            "HTTP_RATE_LIMIT",
                        }:
                            # Preserve local cooldown even if the coordinator
                            # disappears; all configured remote clients then
                            # fail closed until it returns.
                            try:
                                if self.lan_coordinator is not None:
                                    self.lan_coordinator.impose_cooldown(kind)
                            finally:
                                if self.pacer is not None:
                                    self.pacer.impose_cooldown(reason=kind)
                        raise
            except LocalYtDlpDeferred as exc:
                # Never refresh the extractor or retry if our LAN/host
                # coordinator refused, crashed, or returned untrusted data.
                raise YtDlpError(str(exc)) from exc

        try:
            return once(current_label=label)
        except YtDlpTimeout:
            # A hung process is not an extractor-version problem; stop.
            raise
        except YtDlpError as first_error:
            category = _public_probe_failure_category(str(first_error))
            if (category in {
                    "YOUTUBE_BOT_OR_INTERACTIVE_CHALLENGE",
                    "HTTP_RATE_LIMIT",
                    "VIDEO_REQUIRES_LOGIN_OR_RESTRICTED",
                    "HTTP_FORBIDDEN",
                    "VIDEO_UNAVAILABLE",
                    "LOCAL_YTDLP_PACING_DEFERRED",
                } or not retry_with_fresh_runtime
                    or not self.runtime.manage_updates):
                raise
            self.runtime.refresh()
            try:
                return once(current_label=f"{label} after runtime refresh")
            except YtDlpError as second_error:
                # Never retry a challenged site in the same command.
                raise YtDlpError(
                    f"{second_error}; initial attempt also failed: {first_error}"
                ) from second_error

    def probe(self, url: str) -> SourceMetadata:
        if url in self._metadata_cache:
            return self._metadata_cache[url].model_copy(deep=True)
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
        metadata = _metadata_from_payload(
            url,
            payload,
            runtime=self.runtime_info,
        )
        self._metadata_cache[url] = metadata
        return metadata.model_copy(deep=True)

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

    def acquire_video_range(
        self,
        url: str,
        output_root: Path,
        *,
        start: float,
        end: float,
    ) -> AcquiredVideoRange:
        """Download *only* an original-stream bounded time section with yt-dlp/FFmpeg.

        Never falls back to full-stream acquisition. Downstream must verify measured
        duration/bytes and not mistake the requested seek time for observed PTS.
        """
        if (not math.isfinite(start) or not math.isfinite(end)
                or start < 0 or not 5 <= end - start <= 120):
            raise ValueError("Source video section must be finite and 5..120 seconds")
        metadata = self.probe(url)
        if metadata.duration is not None and end > metadata.duration + 0.25:
            raise ValueError("Requested video section exceeds source duration")
        identity = hashlib.sha256(
            f"yt-section-v1:{metadata.external_id}:{start:.3f}:{end:.3f}".encode("utf-8")
        ).hexdigest()[:24]
        directory = (
            output_root / _safe_component(metadata.external_id)
            / "editing_video_sections" / identity
        )
        directory.mkdir(parents=True, exist_ok=True)
        # Cache entries are proof-carrying: do not reuse just because a path
        # exists. A partial/corrupt section must not be mistaken for a valid
        # bounded original-video acquisition.
        evidence_path = directory / "section-integrity.json"
        existing = self._existing_media(directory)

        def sha256_file(path: Path) -> str:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()

        identity_record = {
            "version": "yt-range-cache-v1",
            "source_id": metadata.external_id,
            "source_url": url,
            "requested_start": round(start, 3),
            "requested_end": round(end, 3),
        }
        if existing is not None:
            verified = False
            if not existing.is_symlink() and evidence_path.is_file():
                try:
                    saved = json.loads(evidence_path.read_text(encoding="utf-8"))
                    verified = (all(saved.get(k) == v for k, v in identity_record.items())
                                and saved.get("filename") == existing.name
                                and existing.stat().st_size <= 256 * 1024 * 1024
                                and saved.get("sha256") == sha256_file(existing))
                except (OSError, ValueError, TypeError):
                    verified = False
            if verified:
                return AcquiredVideoRange(
                    metadata=metadata, media_path=existing, requested_start=start,
                    requested_end=end, reused=True,
                )
            # Only remove the narrowly namespaced cached section on disk;
            # never remove user-provided video media or global download caches.
            existing.unlink()
            evidence_path.unlink(missing_ok=True)
        # --download-sections requires yt-dlp's FFmpeg downloader. Fail if
        # sections are unsupported; never retry via ordinary acquire().
        args = [
            "--no-playlist", "--continue", "--no-overwrites", "--no-progress",
            "--format", "bv*+ba/b",
            "--download-sections", f"*{start:.3f}-{end:.3f}",
            "--force-keyframes-at-cuts", "--merge-output-format", "mkv",
            "--paths", str(directory), "--output", "media.%(ext)s",
            "--print", "after_move:filepath", url,
        ]
        completed = self._run_yt_dlp(
            args, label="yt-dlp bounded source video",
            timeout_seconds=BOUNDED_VIDEO_TIMEOUT_SECONDS,
        )
        paths = [Path(line.strip()) for line in completed.stdout.splitlines() if line.strip()]
        media = next((p for p in reversed(paths) if p.is_file()
                      and p.parent.resolve() == directory.resolve()), None)
        if media is None:
            media = self._existing_media(directory)
        if media is None:
            raise YtDlpError("Bounded source video acquisition returned no media")
        # Reject a skipped section or an unexpected huge result. Never regard
        # this alone as proof that the requested absolute start is frame-accurate.
        if media.stat().st_size > 256 * 1024 * 1024:
            raise YtDlpError("Bounded video exceeds the 256 MiB safety ceiling")
        evidence = {**identity_record, "filename": media.name, "sha256": sha256_file(media)}
        temporary = evidence_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
        temporary.replace(evidence_path)
        return AcquiredVideoRange(
            metadata=metadata, media_path=media, requested_start=start,
            requested_end=end, reused=False,
        )

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
            timeout_seconds=MEDIA_ACQUISITION_TIMEOUT_SECONDS,
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
