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
