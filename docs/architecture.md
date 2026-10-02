# Architecture

## Goal of v0.1

Prove one thing before building editing or publishing automation:

> Given a long stream transcript, can the system surface a small set of moments that a human would genuinely consider worth clipping, and explain why?

If this fails, downstream automation has little value.

## Design principles

1. Human-interest discovery first. Editing, subtitles, thumbnails and publishing come later.
2. Free-only execution. No automatic fallback to paid APIs. When a free quota is exhausted, work queues pause.
3. Provider boundaries. LLM, transcription, source acquisition, embedding and storage are replaceable.
4. Rate limits are normal. NVIDIA NIM calls go through a queue/rate limiter with bounded concurrency, retry and backoff.
5. Do not couple the core to YouTube media acquisition. Source acquisition remains an adapter.
6. Feedback is data. Accept/reject decisions become evidence for later ranking.

## v0.1 flow

    Timed transcript
        |
        v
    overlapping semantic chunks
        |
        v
    fast/broad candidate discovery
        |
        v
    candidate normalize + dedupe
        |
        v
    strong-model editorial review
        |
        v
    Top-K clip candidates
        |
        v
    human accept / maybe / reject

## Provider boundaries

### LLMProvider

The caller must not know NVIDIA-specific HTTP details.

Requirements:
- bounded concurrency
- request-per-minute throttling
- 429/5xx retry with exponential backoff
- Retry-After support
- configurable model IDs

### TranscriptionProvider

Added after candidate discovery is proven. Groq Whisper is the initial free-endpoint candidate, but the core will not depend on it.

### SourceProvider

A later adapter for metadata, audio input, video segments and representative frames. A local-file provider comes first for repeatable testing.

### ExampleRetriever

Retrieves structurally similar successful and failed clipping examples. SQLite is sufficient for the first implementation; cloud storage is a deployment concern.

## Candidate contract

Every candidate contains:
- start/end timestamps
- working title
- setup/development/payoff
- why it may be interesting
- humor/story traits
- context requirement
- confidence
- source chunk IDs
- warnings such as context risk or possible misinterpretation

The model is not asked for one magical funny score. It must provide evidence and structure.

## Human gate

The future publishing path retains human approval. Weak candidates can eventually be hidden automatically, but context-sensitive clips must not be auto-published merely because a model produced a high score.


## Editorial intelligence target

The long-term product is not a timestamp ranker. It is an autonomous editorial system.

~~~text
new archive / recent event / historical trigger
    -> evidence extraction
    -> event/moment graph
    -> autonomous EditorialIdea generation
    -> retrieve human positive + weak-negative examples
    -> narrative planning
    -> multi-source timeline planning
    -> deterministic render plan
    -> preview
    -> human approval
~~~

The system must proactively consider multiple output archetypes without requiring the user to
specify them: micro moments, single-topic stories, same-stream multi-cut stories, historical
callbacks, multi-member reaction montages, relationship stories, comparisons, chronology,
event digests, long compilations, and contextual explainers.

See `docs/editorial-intelligence-research-2026-10.md` for the research basis and full target
architecture.

## Source-independent timeline

Do not permanently model one human clip or generated clip as belonging to exactly one source.

The general contract is:

~~~text
ClipProject
  sources[]
  timeline[]
    source_asset_id
    source_start/source_end
    output_start/output_end
    purpose
    confidence
~~~

Alignment v2 is the single-source special case of this general structure.

## Multimodal evidence

Candidate and idea reasoning may use independent evidence layers:

- transcript semantics
- audience/live-chat reaction
- acoustic/prosodic reaction
- visual/game/scene evidence
- historical/entity/relationship context
- human-made clip weak supervision
- normalized performance metadata

A missing modality must degrade gracefully rather than block the entire pipeline.

## Measured-time principle

LLMs should progressively stop emitting raw timestamps. Prefer stable transcript segment IDs,
word IDs, shot IDs, or reaction-window IDs and resolve those identifiers back to measured
timestamps deterministically. This reduces timestamp arithmetic errors and makes decisions
auditable.

## Resumability

Every expensive stage should persist an artifact keyed by source/content identity so retries
resume rather than restart. Transcription chunks, feature extraction, broad discovery,
alignment, enrichment, retrieval, idea generation, edit planning, and rendering should all be
independently resumable.
