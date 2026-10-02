# holoclip

AI-assisted hololive clip discovery pipeline.

The first milestone is deliberately narrow:

> Given one long stream, surface a small set of moments that a human would genuinely consider worth clipping, and explain the story structure behind each one.

Video editing, thumbnails and publishing are intentionally deferred until this ranking problem is proven.

## Current v0.1 flow

    local media file
        -> FFmpeg audio chunks
        -> Groq Whisper timed transcript
        -> overlapping transcript chunks
        -> broad discovery with the fast NIM model
        -> overlap dedupe
        -> editorial selection with the strong NIM model
        -> saved evaluation run
        -> human accept / maybe / reject

NVIDIA NIM access is wrapped by a conservative process-local concurrency and RPM limiter. Groq transcription similarly uses a conservative request rate and stops instead of falling through to a paid provider.

## Setup

Python 3.12+ and FFmpeg on PATH are required.

~~~powershell
git clone https://github.com/Kataage/holoclip.git
cd holoclip
uv sync --python 3.12 --extra dev
copy .env.example .env
~~~

Set these values in .env:

~~~text
NVIDIA_API_KEY=...
GROQ_API_KEY=...
~~~

No paid-provider fallback exists.

## One-command discovery

For local media:

~~~powershell
uv run holoclip discover path\to\stream.mp4
~~~

This automatically:

1. extracts lightweight audio chunks with FFmpeg
2. transcribes them with Groq Whisper
3. writes a timed transcript under data/transcripts/
4. runs NIM candidate discovery
5. writes an evaluation run under data/evaluation/runs/

Then review the generated candidates:

~~~powershell
uv run holoclip review data\evaluation\runs\stream.json
~~~

During review:

- `p`: play only the proposed source segment (with a small context pad) using FFplay
- `c`: show surrounding transcript
- `a`: accept
- `m`: maybe
- `r`: reject
- `s`: skip
- `q`: stop

New evaluation runs created by `discover` remember the source media path. For an older run, supply it explicitly:

~~~powershell
uv run holoclip review data\evaluation\runs\stream.json --media path\to\stream.mkv
~~~

## Transcription only

~~~powershell
uv run holoclip transcribe path\to\stream.mp4
~~~

By default, long media is converted to 15-minute 16 kHz mono MP3 chunks before upload. This keeps local compute low and keeps individual uploads comfortably small.

## Analyze an existing transcript

~~~powershell
uv run holoclip analyze path\to\transcript.json --top-k 5
~~~

The transcript format is:

~~~json
{
  "segments": [
    {
      "start": 0.0,
      "end": 5.2,
      "text": "今日はちょっと話したいことがあって"
    }
  ]
}
~~~

Timestamps are seconds from the start of the source.

## Evaluation

Calculate current metrics:

~~~powershell
uv run holoclip metrics --runs-dir data\evaluation\runs
~~~

See docs/evaluation.md for the validation protocol.

## What this does not do yet

- acquire arbitrary third-party YouTube media
- build the existing-clip human-sense database
- render videos
- publish videos

Source acquisition remains isolated from the ranking core so the project does not depend on one platform-specific extraction method.

See docs/architecture.md, docs/evaluation.md and docs/roadmap.md.


## Human-made clip example store

Existing human-made clips are used as weak supervision rather than model fine-tuning.

Import a JSON manifest:

~~~powershell
uv run holoclip examples-import data\examples\human_clips.example.json
~~~

The validated examples are stored by default in:

~~~text
data\examples\human_clips.jsonl
~~~

Inspect coverage:

~~~powershell
uv run holoclip examples-stats
~~~

The schema keeps clip/source provenance, resolved source timestamps, abstract story structure,
members/topics/traits, relationship context, normalized performance metadata, and alignment
confidence. Source timestamps may be omitted initially and filled by the planned automatic
clip-to-source transcript alignment stage.


## Align a human-made clip back to its source stream

For the first local learning test, supply a local human-made clip file and an already-created
transcript of the original source stream:

~~~powershell
uv run holoclip examples-align path\to\clip.mp4 data\transcripts\source-stream.json
~~~

The command:

1. transcribes the clip with Groq Whisper (and caches that transcript)
2. matches multiple spoken-text anchors against the source transcript
3. estimates the original source start/end timestamps
4. prints confidence and a few anchor matches
5. does **not** write to the learning store by default

After the alignment looks correct, rerun with provenance and `--save`:

~~~powershell
uv run holoclip examples-align path\to\clip.mp4 data\transcripts\source-stream.json ^
  --clip-url "https://www.youtube.com/watch?v=CLIP_ID" ^
  --clip-title "clip title" ^
  --source-url "https://www.youtube.com/watch?v=SOURCE_ID" ^
  --stream-id "SOURCE_ID" ^
  --save
~~~

If confidence is below the default threshold (0.55), the example is stored without accepted
source timestamps so it cannot silently become trusted training data. The threshold can be
changed with `--min-confidence`.

This alignment stage is intentionally local-first and independent of YouTube acquisition.
A future SourceProvider can supply the same media/transcript inputs without changing the
alignment or retrieval logic.


## Enrich an aligned human clip into a learning example

After a real human-made clip has been aligned and saved, enrich it with the strong editorial
model:

~~~powershell
uv run holoclip examples-enrich <EXAMPLE_ID>
~~~

This adds:

- abstract setup / development / payoff
- why a human editor likely selected the event
- reusable structural traits
- members and topics when supported by the transcript
- relationship context
- context requirement
- adjacent source windows as **weak negatives**

Weak negatives are deliberately labeled weak: a nearby region not selected by this particular
clip editor is useful contrast, but it is not treated as proof that the region is objectively
uninteresting.

The enrichment records the model and extraction method so later retrieval decisions remain
auditable.


## Multi-cut alignment

Human-made clips often remove long stretches from the original source or join several source
moments into one clip. Alignment v2 therefore stores an explicit edit map instead of assuming
one continuous source range:

~~~text
clip 00:00-00:30 -> source 10:00-10:30
clip 00:30-01:00 -> source 20:00-20:30
~~~

`examples-align` prints every detected segment and, when saved, writes them to
`alignment_segments`. The legacy `source_start/source_end` fields remain as a bounding range
for backward compatibility.

During enrichment, source gaps between selected segments are preserved as
`removed_between_selected_segments` weak negatives. This lets later retrieval learn not only
what human editors selected, but also what they deliberately skipped between retained moments.


## Migrate existing local data to the canonical store

The current JSON/JSONL evaluation and human-example files remain valid during the migration
period. Import them into the canonical SQLite store without deleting or rewriting the originals:

~~~powershell
uv run holoclip migrate-legacy
~~~

Defaults:

~~~text
evaluation runs: data/evaluation/runs/
human examples: data/examples/human_clips.jsonl
canonical DB:   data/holoclip.db
~~~

Use `--strict` when migration diagnostics should fail the command on ambiguous or malformed
legacy rows. Migration is idempotent: rerunning the same import updates the same canonical IDs
rather than creating duplicate entities.

Legacy feedback JSONL remains in active use for the current review/metrics workflow because old
feedback records do not contain a trustworthy creation timestamp. Holoclip does not invent one
during migration.


## Structural retrieval from human-made clips

After an aligned human clip has been enriched, Holoclip automatically refreshes the derived
structural retrieval index. Positive examples are embedded from abstract editorial structure
(setup, development, payoff, selection reason, traits, topics and relationship context) rather
than clip title wording. Weak negatives are indexed separately.

The current hosted embedding default is:

~~~text
nvidia/nemotron-3-embed-1b
~~~

Override it with `NVIDIA_NIM_EMBEDDING_MODEL` when needed.

Manual rebuild/debug commands:

~~~powershell
uv run holoclip examples-index
uv run holoclip examples-retrieve "unexpected confession with setup and payoff"
~~~

The index is derived state in `data/holoclip.db`; the HumanClipExample store remains the
source material for rebuilding it. Re-indexing unchanged examples reuses their existing
vectors, changed structures are re-embedded, and documents belonging to removed examples are
removed from the active derived index.

## URL-based human clip ingestion with yt-dlp

Holoclip can now use yt-dlp through the replaceable SourceProvider boundary to turn a public
human-made clip URL into a weak-supervision example.

For the common case:

~~~powershell
uv run holoclip examples-ingest-url "https://www.youtube.com/watch?v=CLIP_ID"
~~~

The command:

1. reads clip metadata with yt-dlp
2. looks for an unambiguous original/source YouTube link in the clip description
3. downloads the clip and original source using the training-audio profile
4. transcribes both with the existing resumable Groq pipeline
5. aligns the human clip back to source timestamps
6. writes/updates HumanClipExample
7. extracts setup/development/payoff/selection reason and weak negatives with the strong LLM
8. refreshes the structural retrieval index

If the description has no unique source, specify it explicitly:

~~~powershell
uv run holoclip examples-ingest-url "https://www.youtube.com/watch?v=CLIP_ID" ^
  --source-url "https://www.youtube.com/watch?v=SOURCE_ID"
~~~

Training acquisition intentionally downloads best audio rather than full-resolution video because
transcription/alignment do not need the full video stream. The source cache is separated by
profile:

~~~text
data/sources/examples/clips/<id>/training_audio/
data/sources/examples/sources/<id>/training_audio/
~~~

A future editing-video acquisition can coexist under editing_video/ without mistaking audio
for a completed video download.

Holoclip invokes yt-dlp with --ignore-config so user-global yt-dlp settings cannot silently
change the deterministic training pipeline. It does not enable browser cookies or authenticated
access by default.


### yt-dlp runtime freshness policy

yt-dlp is intentionally **not pinned as a normal Holoclip Python dependency**.

For normal source acquisition Holoclip uses uv/uvx as a managed tool runtime:

- default channel: `nightly`
- each new Holoclip source-ingest process refreshes yt-dlp package metadata
- the latest resolved yt-dlp version is then pinned for the lifetime of that provider/job
- if extraction fails, Holoclip performs one forced runtime refresh and retries once
- the exact yt-dlp version, channel, and JavaScript runtime are stored in human-example acquisition provenance

This avoids keeping a stale downloader in a long-lived project environment while still making one
ingest job reproducible.

Check the source runtime without downloading media:

~~~powershell
uv run holoclip source-doctor
~~~

The default can be changed for debugging/reproducibility:

~~~powershell
set HOLOCLIP_YTDLP_CHANNEL=stable
uv run holoclip source-doctor
~~~

For reliable current YouTube extraction, Holoclip also checks for a supported JavaScript runtime.
Deno >= 2.3 is preferred by yt-dlp; Node >= 22 is also supported and is enabled explicitly by
Holoclip when selected. The managed yt-dlp package uses the `default` extra so the EJS component
is present.
