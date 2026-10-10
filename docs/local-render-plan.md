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
