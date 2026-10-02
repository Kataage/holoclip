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
python -m venv .venv
.venv\Scripts\activate
python -m pip install -e ".[dev]"
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
holoclip discover path\to\stream.mp4
~~~

This automatically:

1. extracts lightweight audio chunks with FFmpeg
2. transcribes them with Groq Whisper
3. writes a timed transcript under data/transcripts/
4. runs NIM candidate discovery
5. writes an evaluation run under data/evaluation/runs/

Then review the generated candidates:

~~~powershell
holoclip review data\evaluation\runs\stream.json
~~~

Press a/m/r for accept/maybe/reject, s to skip, or q to stop.

## Transcription only

~~~powershell
holoclip transcribe path\to\stream.mp4
~~~

By default, long media is converted to 15-minute 16 kHz mono MP3 chunks before upload. This keeps local compute low and keeps individual uploads comfortably small.

## Analyze an existing transcript

~~~powershell
holoclip analyze path\to\transcript.json --top-k 5
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
holoclip metrics --runs-dir data\evaluation\runs
~~~

See docs/evaluation.md for the validation protocol.

## What this does not do yet

- acquire arbitrary third-party YouTube media
- build the existing-clip human-sense database
- render videos
- publish videos

Source acquisition remains isolated from the ranking core so the project does not depend on one platform-specific extraction method.

See docs/architecture.md, docs/evaluation.md and docs/roadmap.md.
