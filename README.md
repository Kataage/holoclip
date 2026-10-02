# holoclip

AI-assisted hololive clip discovery pipeline.

The first milestone is deliberately narrow:

> Given one long timed transcript, surface a small set of moments that a human would genuinely consider worth clipping, and explain the story structure behind each one.

Video editing, thumbnails and publishing are intentionally deferred until this ranking problem is proven.

## Current v0.1 flow

    timed transcript JSON
        -> overlapping chunks
        -> broad discovery with the fast NIM model
        -> overlap dedupe
        -> editorial selection with the strong NIM model
        -> Top-K candidates

NVIDIA NIM access is wrapped by a conservative process-local concurrency and RPM limiter. HTTP 429 and retryable server errors use bounded exponential backoff. Limits are configuration, not assumptions.

## Setup

Python 3.12+ is required.

~~~powershell
git clone https://github.com/Kataage/holoclip.git
cd holoclip
python -m venv .venv
.venv\Scripts\activate
python -m pip install -e ".[dev]"
copy .env.example .env
~~~

Set NVIDIA_API_KEY in .env.

The default model IDs and conservative request settings are also configurable there. No paid-provider fallback exists.

## Transcript format

The PoC accepts either a JSON array or an object containing a segments array.

~~~json
{
  "segments": [
    {
      "start": 0.0,
      "end": 5.2,
      "text": "今日はちょっと話したいことがあって"
    },
    {
      "start": 5.2,
      "end": 11.4,
      "text": "speaker is optional",
      "speaker": "talent"
    }
  ]
}
~~~

Timestamps are seconds from the start of the source.

## Run

~~~powershell
holoclip analyze path\to\transcript.json --top-k 5
~~~

Useful tuning options:

~~~powershell
holoclip analyze transcript.json --window-minutes 10 --overlap-minutes 2
~~~

Output is JSON containing candidate start/end times, a working title, setup/development/payoff, structural interest, traits, context requirements and warnings.

## What this does not do yet

- acquire third-party YouTube media
- transcribe media automatically
- ingest existing successful clips into the human-sense database
- render videos
- publish videos

Those stages come after the discovery quality passes the evaluation gate.

See docs/architecture.md, docs/evaluation.md and docs/roadmap.md.
