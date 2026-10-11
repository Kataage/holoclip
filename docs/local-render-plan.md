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

## Localized non-destructive edit review ledger (PR #214)

The same bounded, offline PR #213 diagnostic scan now retains **where** an
abnormality was seen, not only how often. Black intervals include the measured
start/end seconds. Frozen frames include the onset (their end can be unknown).
Fast and overly wide subtitles include their own SRT cue number and start/end
timestamps, but **never subtitle text**. Audio sampled-peak/low-mean warnings
refer only to the first at-most-10 seconds sampled: do not assume an entire
stream or later reaction was measured.

Each detected event is mapped to all ordered ClipTimeline segments it overlaps.
A blackout crossing a cut joins the two adjacent segment IDs so the next
reviewer does not inspect only one source video. A freeze without an FFmpeg
end marker is treated as an onset only; it does not accuse every later cut.
Every event receives a fixed, bounded inspection action and
\`auto_repair_allowed=false\`. The ledger makes **no inference** that dark or
static illustration segments are an error, nor any unvalidated estimate of
quality or probability.

For successful measured scans, the local worker creates an immutable sibling
\`renderqueue_<id>.review.json\` atomically on the same filesystem. It has
strict event count and size limits, no shell/network/LLM/OCR access, no source
paths, no transcript text, and includes the original/final MP4 content hashes
plus the scan status and explicit \`ready=false\`. A second run cannot
overwrite an existing manifest. The worker commits an independent
\`render_review_evidence\` status/hash/candidate count to the existing SQLite
transaction and exposes read-only aggregate \`review_counts\`. If the
optional diagnostic was skipped or a manifest could not be safely written,
its review status is UNAVAILABLE; the original video technical QA is intact.

This is **instrumentation for future calibration**, not learned calibration
itself. Withheld, real human-rated examples are still required to measure
which alarms indicate genuine faults and which represent stylistic intent.
Automatic edits, deletion, retiming and publication remain blocked pending
such qualification. Never claim a perfect clip just because it has zero
detected candidates.

## Read-only edit-quality diagnostics (PR #213)

After the original MP4 technical QA, optional audio leveling and caption
burn-in, the existing local scheduled worker runs a **bounded read-only**
FFmpeg diagnostic pass. It never modifies source media, subtitles or finished
MP4s, does not use YouTube, LLMs or OCR, and never uploads or publishes.

The uncaptioned local MP4 is scanned at 2fps and 160x90 with FFmpeg
\`blackdetect\` (>=1 second, about 98% very dark) and \`freezedetect\`
(>=2 seconds), capped at 100 events each. The last selected audio track
(base, leveled or captioned MP4) is inspected using \`volumedetect\` on up to
the first 90 seconds, recording its mean/peak sample dBFS. The SHA-256
verified *generated* sibling SRT is checked for unusually fast lines
(over 20 Unicode characters/second), unsafe wrapping over the two-line
baseline, and duration clock consistency. No captions means a **partial**
diagnostic, not a fake subtitle success.

All tool invocations are offline, use fixed validated FFmpeg arguments,
have strict time and stderr limits (including a 60-second ceiling for the
video scan and at most 30 seconds for the audio sample) and verify local
input content SHA-256
both before and after scanning. No operator SRT or unrelated MP4 is scanned
on untrusted provenance. Only bounded counters and numeric diagnostics
are persisted; no sensitive transcript lines or paths are logged.

\`render_quality_evidence\` preserves the diagnostic version, source/final
MP4 identities, frame anomaly counts, caption observation state and flags,
audio mean/peak and combined warning count. Its statuses mean exactly
\`MEASURED_FLAGS\`, \`MEASURED_NO_FLAGS\`, \`MEASURED_PARTIAL\` or
\`UNAVAILABLE\` under the \`LOCAL_EDIT_DIAGNOSTIC_*\` prefix.
Read-only worker status adds aggregate quality counts. The scheduled worker skips the optional scan entirely once earlier work
has consumed 50 minutes, reserving time under the 55-minute Windows task
deadline. An unavailable diagnostic never revokes existing technical MP4 QA.

**Important:** Dark scenes, still illustrations, intentional holds and
strong reactions can be perfectly good editing choices. Thresholds are
screening heuristics, not human-labeled calibration. Absence of flags is
not evidence of engaging content, correct Japanese ASR, legible text on
complex backgrounds, reliable perceptual LUFS or source rights.
The tool does not silently "repair" a suspected artistic element.
Automatic repair is deferred until data supports a safe, reversible
rule and real held-out footage can distinguish bugs from style.

\`ready=false\` / \`production_ready=false\` and manual publishing
approval remain unchanged. CI runs offline synthetic/mocked checks, never
tests live YouTube media.

## Conservative visual-style baseline (PR #212)

The initial local FFmpeg render now compiles a **finite, deterministic** visual
style directly into each ordered ClipTimeline segment. It adds no second
video encode, user operation, Docker, network or new paid service, and does not
change source trim positions, output order, duration, A/V cut clocks or SRT
timing. Any existing completed MP4 is never overwritten or re-rendered.

The safe default is **the original 1280x720 aspect-preserving letterbox and
hard cut**. A subtle progressive center zoom (capped at 1.035x) applies only
to clips marked REACTION or PAYOFF with segment confidence >= 0.92 and at least
two seconds of footage. Because the pipeline does not yet verify face position
or game UI safety, a center zoom is *not* proof of good composition; it may
still trim a small part of the visible frame and requires real-video review.

A brief 80 ms dip-to-black at a source boundary (synchronized on both audio
and video) is allowed **only** when the incoming segment explicitly requests
the fixed hint \`dip_to_black\`, both segments have confidence >= 0.95,
are different SourceAssets, and each segment is at least 1.5 seconds.
Unknown hints and free-form LLM filter strings are ignored, not executed.
No default dissolve or fade is invented; the duration and cut clock are
preserved. Speech near those deliberate boundaries may still be affected.

The deterministic \`VISUAL_STYLE_VERSION\` forms part of the RenderPlan ID
and is verified before FFmpeg starts, preventing an old fingerprint from
silently producing different style results. A successful local technical MP4
receives a \`visual_style\` summary (zoomed cut count, hinted transition
count and style version). The auto queue saves this as separate
\`render_visual_evidence\` in the final technical QA transaction. Status
\`LOCAL_RENDER_VISUAL_ENCODED_TECHNICAL_ONLY\` means the renderer returned
the expected style summary *and* its original A/V QA succeeded; it **does not**
certify natural composition, continuity or real-frame quality. A missing or
mismatched summary is recorded as UNVERIFIED, without inventing success.
The read-only preview status displays aggregate visual evidence.

CI exercises bounded filter decisions and synthetic real FFmpeg A/V
roundtrips only; real unseen streams, actual reaction crops and scene-aware
transitions remain unqualified. Publication remains blocked and
\`ready=false\` is unchanged.

## Offline cut-aware audio leveling (PR #211)

After the existing deterministic original MP4 technical QA, the local worker
attempts an **optional** new-only audio finishing stage. This is automatic;
it does not require additional manual files, paid APIs, Docker, yt-dlp or cloud
providers. The original MP4/SRT and original source cache are never modified.

For each ordered \`ClipTimeline\` cut (up to 32, total no more than 180 seconds)
the offline FFmpeg \`volumedetect\` filter measures mean and sample-peak dBFS.
The correction targets approximately -22 dBFS **mean per cut** (not perceived
LUFS), capped at -6 to +6 dB, with positive gain further bounded so measured
peaks would not exceed -1.5 dBFS. Quiet/silent cuts are **not amplified**.
The worker splits the already-rendered local output audio by the recorded
cut-clock, applies these deterministic numeric gains, concatenates audio,
then applies a final sample limiter without automatic gain-up.
Video is stream-copied, audio is re-encoded at 48 kHz / 192 kbps AAC.

Output is \`renderqueue_<id>.leveled.mp4\` only after a fresh ffprobe A/V and
duration check, per-cut second-pass volume/peak measurements, a bounded
gain-agreement check (accounting for AAC and frame boundaries), and source
hash re-validation. The final no-overwrite commit uses a create-only hardlink;
an existing output, malformed timeline, FFmpeg error, unsupported signal or
unexpected gain/peak produces optional failure without invalidating the base
MP4 technical QA. The stage is bounded to a 420-second encode timeout plus
short bounded per-cut measurements. It never automatically retries failed
work or replaces an existing file.

If the audio output passes, the existing subtitle burn-in uses the measured
\`*.leveled.mp4\` audio and original corresponding \`*.srt\`, creating the
same \`renderqueue_<id>.captioned.mp4\` as before. If not, subtitle burn-in
falls back to the unmodified technical MP4 as in PR #210. When SRT is missing,
audio leveling may still produce a standalone audio-leveled MP4.

\`render_audio_evidence\` records status, hash, cut count, maximum applied
absolute gain and the spread of cut mean levels before and after correction,
alongside technical and caption evidence in SQLite. The read-only status
reports aggregate audio successes/failures. These diagnostics are **not**
listening-quality certification: dBFS isn't LUFS, music/game/audio mixing
cannot be separated automatically in this baseline, true-peak intersample
overshoot isn't fully qualified, and output quality on unseen Hololive
recordings remains unknown. Original \`QA_PASSED\` is unchanged; all routes
remain \`ready=false\`, with no publication.

## Optional Japanese subtitle burn-in (after PR #209)

For a newly completed source-acquisition job, the existing offline scheduled
renderer now *also* attempts a second local-only FFmpeg encode when PR #209
generated a trustworthy SRT. This produces a new sibling
`renderqueue_<id>.captioned.mp4`, leaving the original QA-verified MP4 and
`renderqueue_<id>.srt` unchanged. No video is overwritten or uploaded.

A strict parser checks the SRT sequence, ordering, time ranges, text controls
and maximum cue length. Caption text is safely rendered into a fixed 1280x720
ASS style (white bold Japanese text, dark outline, bottom center, at most two
lines). This is a deterministic **baseline**, not learned, speaker-aware or
editorially reviewed styling. It does not guess missing ASR or word timing.
The input MP4 and SRT must match their measured SHA-256 receipts; FFmpeg uses
local-only input with libass and checks the new MP4's video, audio and measured
duration before a create-only hardlink commit.

Japanese font discovery is automatic and conservative: Windows Meiryo / Yu
Gothic, or Linux Noto Sans CJK JP / TakaoGothic. FFmpeg with the `ass`
filter and a recognized local font are required. If missing, if SRT is
untrusted, or if the second encode fails, the original technical MP4/SRT are
still valid and the optional burn-in is recorded as unavailable/failed.
No silent font fallback to unreadable Japanese is considered a quality pass.
The second encode has a 10-minute ceiling so that a full-length automatic
180-second preview plus burn-in stays inside the existing bounded Windows
scheduled-task budget. Slow hosts may need a later separate manual retry;
there is no unbounded or silent retry.

`render_burnin_evidence` saves the burn-in status, output and SRT hashes
transactionally with the original queue's technical QA row. The read-only
`render-ready-preview` command exposes burn-in counts. The successful status
is `LOCAL_SUBTITLE_BURNED_PREVIEW_UNVERIFIED_EDITORIAL`: it certifies
neither Japanese transcription correctness, visual legibility on every scene,
creative timing, policy rights, nor publication readiness. The original
`QA_PASSED` means only independent A/V technical QA. Both remain
`ready=false`. The existing Windows worker needs no extra per-clip setup.

## Deterministic cut-aligned subtitle sidecar (PR #209)

The completed-source offline renderer now also attempts to create an
**optional UTF-8 SRT subtitle sidecar** next to the verified technical
preview MP4. This is a transcript timing aid, not burned-in graphics.
For example, data/render-previews/renderqueue_....mp4 gains a sibling
renderqueue_....srt when usable source ASR already exists.

No input preparation is needed when Holoclip's existing discover or
human-example ingestion has saved timed source ASR under one of:

    data/transcripts/<youtube-video-id>.json
    data/transcripts/examples/sources/<youtube-video-id>.json

The default root can be changed using `--transcript-root` on
`uv run holoclip render-ready-preview --execute`. The normal Windows
scheduled pipeline uses the same default source transcript directory.
Missing source transcripts cause no network ASR call and no invented
captions. Ambiguous duplicates, mismatched transcript metadata,
linked paths, oversized or invalid records fail closed without
modifying the MP4 or an existing operator-provided SRT.

The compiler maps each canonical source transcript interval to each
selected original-stream cut, then shifts it into **output clock**
according to `output_order`, including disjoint cuts and multiple
sources. A complete utterance must fit inside a selected source cut;
utterances crossing a cut boundary are dropped instead of assigning
guessed word-by-word timestamps. Ineligible overly long/malformed
utterances and ambiguous overlaps are also skipped. No ASR text is
rewritten or hallucinated. Existing caption text is HTML-escaped
for safe SRT output.

Rendered sidecars are created using a no-clobber filesystem commit;
a matching SRT already present is preserved unmodified. The
optional output SHA-256, cue count, dropped cue count and reason
code are persisted in render_caption_evidence alongside technical
MP4 QA, and aggregate counts are visible in the read-only
`render-ready-preview` status. Neither ASR text nor transcript paths
are copied into the public worker result.

**Current limitation:** The SRT is a sidecar, not visual captions
burned into the video. It does not identify speakers, style text,
accurately align words, or confirm recognition/caption semantics.
ASR may be inaccurate, and a no-subtitles result does not block
the independent A/V QA_PASSED state. A production-grade subtitle
style and editorial QA/burn-in stage remains separate. All reports
retain ready=false and no publication occurs.

## Automatic technical MP4 after complete-source acquisition (PR #208)

The existing Windows periodic source queue worker now has a local-only
second stage. It attempts at most one missing video download per scheduled
tick. **Only when the source queue is fully complete and no download
was attempted in this tick** does the same worker invoke an offline
one-clip MP4 renderer on a later tick. This protects the scheduled
task's resource/time budget; it neither contacts YouTube nor makes
an upload.

Inspect completed-source preview eligibility without starting FFmpeg:

    uv run holoclip render-ready-preview

To explicitly process one locally ready timeline outside Task Scheduler:

    uv run holoclip render-ready-preview --execute

Existing --queue and --preview-root options allow configurable SQLite
state and preview output directories. The default output root is
data/render-previews; outputs are named after the stable acquisition
job ID, creating a local MP4 only if the filename does not exist.

Every run rechecks the source queue COMPLETE status, the enrolled
measured ClipTimeline, original archive identities and full-duration
ffprobe AV measurements, and the cached yt-dlp receipt plus local
RENDER source/range policy rules. A source without trustworthy
title/description metadata, a no-clipping marker, or a policy
BLOCK/REVIEW must not render. Source hashes and decode time ranges
are again validated by the existing local RenderPlan.

Render progress is stored durably in a separate render_jobs table
within the original SQLite source queue. State flow:

    pending complete source -> RENDERING -> QA_PASSED
                                      -> PAUSED on validation/QA error

Claims are serialised with SQLite BEGIN IMMEDIATE. A crashed
RENDERING job or a PAUSED job blocks additional auto renders.
Neither will be retried or overwritten automatically. An interrupted
render can leave a new untracked output: investigate manually;
do not treat file existence as proof of success.

A successful QA_PASSED row records plan ID, MP4 path, output SHA-256
and measured duration. It means only ffprobe A/V tracks, output
duration and basic source/plan checks passed, **not** verified
subtitles, transitions, sync accuracy, creative quality, policy
clearance or publishing. The current scheduled integration permits
only clips up to **180 seconds** automatically, to keep rendering
separate from a long download within a bounded 55-minute scheduler
task. Larger clips must go through explicitly controlled offline
`render-local-preview --execute` with adequate resource budgeting.

No changes promote production readiness. `ready=false` and
`human_editorial_qa_completed=false` remain mandatory, and
publishing always requires separate policy/quality approval.

## Automatic source-queue enrollment after candidate analysis (PR #207)

After creating the PR #206 durable queue, the next manual step—the
per-candidate ClipTimeline file and enqueue command—can be removed with
**one-time** operator opt-in. Preview the settings with no file writes:

    uv run holoclip render-candidate-autoqueue-setup

Then explicitly enable *local technical preview acquisition only*:

    uv run holoclip render-candidate-autoqueue-setup --apply --approve-local-preview

Settings are saved to data/runtime/render-candidate-autoqueue.json; the
file is never overwritten by setup. --config, --queue, --database,
--cache-root and --policy-file are configurable. Configure before
analysis; there is no need to repeat flags for each stream.

Subsequent normal `holoclip analyze` and `holoclip discover` calls
automatically offer their in-process evaluated candidate runs to the
existing SQLite source queue. No separate timeline JSON or sources.json
is written. If autoqueue is disabled, existing analysis output stays
unchanged; if it is configured but blocked, candidate results remain
available and a fixed nonsecret warning code is printed.

Enrolled candidates must have an exact canonical YouTube source asset ID
(their --stream-id, or derived ID, must match its 11-character YouTube ID),
an existing available public/authorized source of type stream/video,
confidence >= 0.85 by default, no candidate warnings, consistent
candidate IDs and measured cut boundaries, and <= 600 seconds of
selected video. Default max enrollment is one candidate per analysis run,
configurable up to three. This is only a conservative prefilter;
confidence or a low warning count is **not** ground-truth editorial
quality proof. Unknown/mismatched stream IDs, private/unavailable
source assets and malformed model plans are skipped, not guessed.

Only local SQLite queue writes happen during analyze/discover: yt-dlp,
media downloads, FFmpeg, external APIs beyond the existing analysis,
or actual Windows Task Scheduler operations are not performed by
this bridge. The previously approved queue worker still independently
checks metadata, restrictions, LAN-wide access pacing and cooldown
before requesting at most one archive per scheduled iteration.

A normal `discover` run already saves its evaluation run to disk;
`analyze` enrolls from the current candidate run without requiring
`--save-run`. No automatic publication occurs. This stage does not yet
perform post-download FFmpeg rendering or completed-clip editorial QA;
`ready=false` remains mandatory.

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
