# Roadmap

## Phase 0 - bootstrap

- [x] Repository initialized
- [x] Provider boundaries defined
- [x] Conservative NIM rate-limit client
- [x] Transcript and candidate contracts
- [x] CI/lint/test workflow

## Phase 1 - candidate discovery PoC

Target: one timed transcript in, 3-10 useful clip candidates out.

- [x] Import JSON timed transcripts
- [x] Overlapping chunker
- [x] Broad candidate extraction with fast model
- [x] Candidate dedupe/merge
- [x] Final editorial review with strong model
- [x] CLI output
- [x] Save accept/maybe/reject feedback
- [x] Add repeatable validation run/metric format
- [ ] Build a real-stream validation corpus
- [ ] Run real NIM smoke test with a Japanese transcript

Exit criterion: across a small validation set, Top-5 regularly contains at least two candidates a human considers usable.

## Phase 2 - human-sense example database

- [x] Define existing-clip example schema
- [x] Local clip -> source transcript alignment
- [x] Multi-cut same-stream alignment map
- [x] Extract abstract story/humor structure
- [x] Add adjacent and removed-between-segments weak negatives
- [ ] Generalize examples to multiple SourceAssets / one ClipTimeline
- [ ] Validate three real clip families before scaling
  - single-topic / near-continuous
  - same-stream multi-cut
  - cross-stream / multi-member
- [ ] Ingest 10-30 validation examples
- [ ] Expand to 50-200 representative examples after validation
- [ ] Embedding / structural retrieval
- [ ] Compare new candidates against positive and negative examples
- [ ] Duplicate-event collapse and multi-clipper consensus
- [ ] Normalize public performance signals rather than using raw views alone

## Phase 2.5 - autonomous editorial intelligence

- [ ] SourceAsset + ClipTimeline data model
- [ ] Stable transcript-segment / word-ID temporal references
- [ ] EditorialIdea contract
- [ ] Autonomous idea generation for multiple archetypes
- [ ] Stream-local idea generation
- [ ] Recent cross-stream / multi-POV idea generation
- [ ] Historical callback / relationship / chronology retrieval
- [ ] Novelty and duplicate/template detection
- [ ] Evidence provenance in every idea
- [ ] Portfolio diversity across format, member, duration and tone

## Phase 3 - transcript/source automation

- [x] TranscriptionProvider interface
- [x] Groq Whisper adapter
- [x] Local media input with FFmpeg audio chunking
- [x] One-command local media -> transcript -> candidate run
- [ ] Source acquisition strategy review
- [ ] Stream metadata ingest
- [ ] Live-chat ingestion and lag-aware reaction features
- [ ] Audio/prosodic reaction features
- [ ] Visual/shot features
- [ ] Historical entity/topic/relationship graph
- [ ] Stage-level persistent resume/cache

## Phase 4 - edit plan

- [ ] NarrativePlan from EditorialIdea
- [ ] Multi-source deterministic TimelinePlan
- [ ] Deterministic EditPlan JSON
- [ ] FFmpeg segment assembly
- [ ] Subtitle generation
- [ ] Lightweight emphasis/zoom rules
- [ ] Preview generation

## Phase 5 - approval UI

- [ ] Candidate cards
- [ ] Short preview
- [ ] accept / maybe / reject
- [ ] feedback history
- [ ] notify only when quality threshold is met

## Phase 6 - publishing

- [ ] title/description/thumbnail suggestions
- [ ] source attribution fields
- [ ] policy checklist
- [ ] YouTube upload integration
- [ ] explicit final approval


## Quality rule

Do not advance a phase merely because the pipeline executes. Each phase must demonstrate that
it improves editorial quality or reduces operator burden on real examples.

The system must retain an exploration path so it can discover original ideas that were not
already present in the human-clip example database.
