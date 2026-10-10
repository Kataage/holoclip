# Local multi-source RenderPlan v1 — offline FFmpeg preview

Status: **technical A/V assembly baseline only**; production readiness remains false.

## Inputs and preview workflow

The new Holoclip command takes a canonical ClipTimeline JSON file and a JSON
array of explicit source mappings. Each source mapping has:
- "asset": SourceAsset JSON with matching "id", "platform", "external_id",
  "availability": "available", "access_class": "local"/"public"/"authorized".
- "path": full path to an existing local video file.
- "local_render_approved": true, explicitly asserted by the operator.

Example mapping (one entry per distinct source_asset_id):

    [
      {
        "asset": {"id":"asset-a","platform":"local",
                  "external_id":"source-a","availability":"available",
                  "access_class":"local"},
        "path":"D:\\AI\\holoclip\\data\\sources\\a.mp4",
        "local_render_approved":true
      },
      {
        "asset": {"id":"asset-b","platform":"local",
                  "external_id":"source-b","availability":"available",
                  "access_class":"local"},
        "path":"D:\\AI\\holoclip\\data\\sources\\b.mp4",
        "local_render_approved":true
      }
    ]

For a timeline whose ordered segments reference asset-a and asset-b:

    uv run holoclip render-local-preview --timeline "D:\AI\holoclip\data\timeline.json" --sources "D:\AI\holoclip\data\local-sources.json" --output "D:\AI\holoclip\data\preview.mp4"

This performs read-only SHA-256 hashing, ffprobe source validation, segment
bounds inspection and deterministic plan compilation. It does not create
an output or access the network. Explicit execution:

    uv run holoclip render-local-preview --timeline "D:\AI\holoclip\data\timeline.json" --sources "D:\AI\holoclip\data\local-sources.json" --output "D:\AI\holoclip\data\preview.mp4" --execute

Requires local FFmpeg and ffprobe on PATH, with no Docker, yt-dlp, NIM
API, ComfyUI or cloud resource.

## Resolve existing full-video caches without a manual source manifest

Existing canonical SourceAsset IDs can be matched to already-acquired
local complete yt-dlp editing_video caches:

    uv run holoclip render-local-preview --timeline data/clip-timeline.json --auto-sources --database data/holoclip.db --cache-root data/sources --approve-local-preview --output data/preview.mp4

The default still validates without output. To produce an MP4 add
--execute. The manual --sources and --auto-sources modes are exclusive.

Source identity comes from read-only canonical SQLite: the exact
source_asset_id must exist, be AVAILABLE, have a public/authorized
YouTube SourceAsset, and have a canonical 11-character video ID.
Supported fixed local cache layouts relative to --cache-root:

    <root>/<video-id>/editing_video/media.*
    <root>/original/<video-id>/editing_video/media.*
    <root>/examples/sources/<video-id>/editing_video/media.*

Each directory requires one full video with a media.info.json whose
id and Youtube extractor identity match the canonical asset.
training_audio and editing_video_sections must never substitute
for a full original: bounded range PTS does not guarantee absolute
source coordinates. Missing or ambiguous caches, wrong identity,
unknown assets and linked paths fail closed. No downloads, cleanup,
SSH or metadata web probes take place.

--approve-local-preview is an explicit local technical-use assertion,
NOT clipping rights, copyright clearance, or publication approval.
Source bytes and A/V durations are separately hashed and checked
by the preexisting RenderPlan compiler/executor. A successful preview
still retains ready=false and has no upload path.

## Missing source acquisition through the existing shared yt-dlp gate

If the automatic source mapper reports a missing full stream, inspect the
existing caches and the canonical timeline *offline* with:

    uv run holoclip render-local-source-acquire --timeline data/timeline.json --database data/holoclip.db --cache-root data/sources

This command never probes YouTube and never creates/overwrites media. It
reports missing source asset IDs in timeline order and cache counts. Existing
corrupt, ambiguous, linked or untrusted caches are blockers, NOT invitations
to overwrite them or guess another candidate. Audio-only caches and bounded
video sections never substitute for complete original video timestamps.

To explicitly try acquiring **one** missing complete public source video,
after installing and configuring the authenticated PC-A LAN coordinator
and existing conservative local yt-dlp pacing gate:

    uv run holoclip render-local-source-acquire --timeline data/timeline.json --database data/holoclip.db --cache-root data/sources --execute --approve-local-preview

Only the first missing stream by timeline order is attempted per command.
The default max recorded archive duration is 14400 seconds (4 hours), and
yt-dlp's max file size request is 512 MiB; change using
--max-duration-seconds (60..14400) and --max-filesize-mb (64..1024).
These are *requested acquisition limits*, not guarantees that all protocols
can stop downloads at exactly the limit. After download, observed file
size is rechecked and the media/receipt are validated before reuse.
Access may take substantial time and network bandwidth; do not automate
rapid loops. A configured shared-LAN coordinator is mandatory to execute
this command even when only one PC is actually downloading.

Before downloading, one yt-dlp metadata probe is made **through the same
shared lease/local pacing**. Live/upcoming sources, unknown or oversized
durations, mismatched IDs and blocked/review clip rules are rejected.
The metadata policy registry is read from data/policy/clip_rules.jsonl by
default; optional --policy-file points to a different local registry.
Then a single bounded full-video acquisition uses the existing provider
with its rate-limit cooldown and conservative request pacing. Neither
stage bypasses the LAN gate, resets cooldowns or silently tries another
source after a failed request. No publishing is performed.

The approval flag is **not proof of ownership or a platform-policy grant**.
Only locally approved technical previews should use this path; apply
applicable Hololive source restrictions and review finished edits separately.
The output status remains ready=false even when an archive is acquired.

## Durable unattended missing-source queue (Windows, preview-first)

The approved missing-video acquisition can now be enrolled in a local
SQLite queue and advanced by a bounded Windows Task Scheduler worker.
One saved job snapshot records the measured ClipTimeline, canonical
database/cache/policy paths and acquisition budgets, with no passwords,
tokens, OAuth cookies or yt-dlp stderr. Store this local runtime DB
privately; it contains source asset IDs and machine-local paths.

Step 1, preview offline (no queue file created or network request):

    uv run holoclip render-source-queue-enqueue --timeline data/timeline.json

To enroll once, explicitly authorize local preview acquisition:

    uv run holoclip render-source-queue-enqueue --timeline data/timeline.json --apply --approve-local-preview

Step 2, preview the current-worker state without network:

    uv run holoclip render-source-queue-status
    uv run holoclip render-source-queue-run

Step 3, optionally execute a *single* bounded queue worker iteration:

    uv run holoclip render-source-queue-run --execute

One attempt per invocation means no rapid bulk downloads. The worker
uses the existing authenticated PC-A LAN coordinator, local SQLite
yt-dlp pacing, conservative native yt-dlp sleeps, source policy and
full-video identity checks. First source in a timeline is acquired
and the durable job stays QUEUED if another source remains missing.
An existing valid complete cache is recognized without new network.

Step 4, to schedule recurring single-item checks on Windows, inspect
the preview then explicitly install the current-user task:

    uv run holoclip render-source-queue-task-prepare
    uv run holoclip render-source-queue-task-prepare --apply

These steps register, but do not immediately start, a non-elevated
interactive Task Scheduler job that repeats approximately hourly
(configurable --interval-minutes 30..1440). It requires the existing
uv environment and an already enrolled queue. It never overwrites
an existing task or resets yt-dlp cooldown. Windows user sign-in
is required for interactive tasks; this alone does not guarantee
24-hour service uptime or automatic wake after a PC shutdown.

**Fail-closed recovery:** an interrupted RUNNING attempt stays RUNNING
and blocks later jobs. Do not assume unfinished media is safe.
After 2 hours, inspect the job ID from render-source-queue-status,
and run a read-only reconciliation:

    uv run holoclip render-source-queue-reconcile --job-id JOB_ID

Add --apply only after checking the result. If all caches are
fully source-duration-matched and FFprobe verifies both A/V tracks,
reconciliation can mark COMPLETE. Otherwise it moves the job to
PAUSED. It never downloads while reconciling. Unknown errors,
rate-limit/bot checks, timeouts and policy restrictions also PAUSE
without retry. Only explicit --apply --resume on a *PAUSED* job
rearms it after fixing the cause; the queue does not silently
re-enroll or reset a block. Shared pacing contention may become
DEFERRED with a minimum 30-minute delay, at most three total tries;
after that the job requires manual intervention. A paused or running
oldest job blocks later jobs to avoid shared-IP bursts.

Queue preview and hosted CI never contact YouTube, owner Windows PCs
or Task Scheduler. The scheduler tests replace Windows cmdlets with
mocks. No source policy or copyrights are automatically certified,
and neither acquired media nor a generated preview is publishable
without separate verification. All results remain ready=false.

## Deterministic technical behavior and safety

- Supports continuous, same-source multi-cut and multiple-source ordered
  ClipTimelines, up to 32 segments and 1800 seconds of output.
- All source assets must be available and each locally approved; all required
  source IDs must map exactly once. Approval assertion does not establish
  source copyright, no-clipping policy compliance or permission to publish.
- Local source inputs only, 50 GiB maximum per input, .mp4/.mkv/.mov/.webm.
  No symlink/junction path, missing input or duplicate media path accepted.
  Measured ffprobe A/V presence and duration and input bytes are checked.
- Explicit video and audio are mandatory: missing track blocks processing.
  No fabricated silent soundtrack. FFmpeg uses measured trim/atrim,
  timestamps reset, constant 1280x720 at 30fps, H.264 yuv420p and
  48kHz stereo AAC. No shell execution or network protocol whitelist.
- Input hashes are verified before and after FFmpeg. On output, ffprobe
  confirms positive audio/video and duration within 0.5 seconds of the
  plan. QA or FFmpeg failures leave no committed output.
- Renders to a temporary sibling directory; hardlink create-only commit
  prevents overwriting an existing output, including a concurrent writer.
  The output parent directory must exist and the filename must be new .mp4.
- Outputs include stable plan ID and technical measurements, but suppress
  FFmpeg error logs that could expose private local file details.

Successful result is LOCAL_RENDER_BASIC_AV_QA_PASSED_NOT_PRODUCTION_READY.
This means basic A/V integrity, NOT verified visual cut continuity,
subtitles, audio balance, localization, source permissions, clip quality
or publishing eligibility. The full Editing Profile, more sophisticated
RenderPlan, subtitle/zoom/audio-mix strategies, quality/repair loop and
approval-gated publishing remain outstanding. Real Hololive footage was
not run in hosted CI; only synthetic local media is used for integration.
No production or editor quality readiness is promoted: ready=false.
