# Roadmap

## Phase 0 - bootstrap

- [x] Repository initialized
- [x] Provider boundaries defined
- [ ] Conservative NIM rate-limit client
- [ ] Transcript and candidate contracts
- [ ] CI/lint/test workflow

## Phase 1 - candidate discovery PoC

Target: one timed transcript in, 3-10 useful clip candidates out.

- [ ] Import JSON timed transcripts
- [ ] Overlapping chunker
- [ ] Broad candidate extraction with fast model
- [ ] Candidate dedupe/merge
- [ ] Final editorial review with strong model
- [ ] CLI output
- [ ] Save accept/maybe/reject feedback

Exit criterion: across a small validation set, Top-5 regularly contains at least two candidates a human considers usable.

## Phase 2 - human-sense example database

- [ ] Define existing-clip example schema
- [ ] Ingest 50-200 representative successful clips
- [ ] Extract abstract story/humor structure
- [ ] Add weak negatives
- [ ] Embedding retrieval
- [ ] Compare new candidates against positive and negative examples
- [ ] Normalize public performance signals rather than using raw views alone

## Phase 3 - transcript/source automation

- [ ] TranscriptionProvider interface
- [ ] Groq Whisper adapter
- [ ] LocalFileProvider
- [ ] Source acquisition strategy review
- [ ] Stream metadata ingest
- [ ] Optional live-chat reaction features

## Phase 4 - edit plan

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
