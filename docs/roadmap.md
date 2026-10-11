# Roadmap

## Current audited delivery gap (2026-10-11)

The [end-to-end source-to-finished-clip audit](end-to-end-audit-2026-10-11.md)
separates existing candidate/transcript/alignment **diagnostics** from missing
RenderPlan, deterministic assembly, subtitle/motion/audio production, QA and
publication approval. Source/LAN mocks and a good `ClipTimeline` are **not** a
production-finished clip. Editorial boundary validations now fail closed on
invalid LLM and stale persisted cut plans. Do not count design-only checks as
complete implementation, and do not unlock production router without real
held-out qualification; `ready=false` remains the default.


## Provider foundation status, October 4 2026

PRs #101–#106 have merged after individual CI success: ZERO_COST_ONLY dispatch guards,
account-scoped quota/resume state, capability/quality routing, measured visual modes,
real-media benchmark/qualification artifacts and production protocol adapters. The
[live smoke](provider-smoke-2026-10-04.md) is unqualified transport evidence.
Blinded small-subset review and independent weak grades support #107; actual gold
calibration and Japanese/Live2D/game UI/reaction/OCR qualification remain pending.
Historical implementation checkboxes below do not certify current qualified production
readiness. Editing Profile, RenderPlan, deterministic rendering and QA/repair remain
separate delivery gates in #80/#81–#89.

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

## Phase 1.5 - canonical foundation

This is the migration bridge from the working PoC to the long-term architecture in
`docs/end-to-end-blueprint.md`.

- [ ] SourceAsset canonical identity
- [ ] Stable TranscriptSegment IDs
- [ ] EvidenceItem / SourceMoment contracts
- [ ] TimelineSegment / ClipTimeline contracts
- [ ] EditorialIdea contract
- [ ] ReviewRecord contract
- [ ] Repository interface + local SQLite implementation
- [ ] ArtifactRecord / StageRun resumability skeleton
- [ ] Compatibility adapters for current ClipCandidate / EvaluationRun / HumanClipExample
- [ ] JSON/JSONL migration/import path

Exit criterion: existing continuous and multi-cut tests pass through the canonical model without
losing provenance, and successful upstream stages can be reused after a simulated retry.

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

## Phase 3.9 - local deterministic A/V baseline (2026-10-11)

- [x] Compile measured multi-source ClipTimeline into bounded local FFmpeg A/V render plan
- [x] Preview-only missing full-video inventory; opt-in one-at-a-time paced source acquisition with metadata/policy checks and file/duration budgets
- [x] Durable Windows-local queued missing-source acquisition, serialized one-at-a-time worker, Task Scheduler opt-in, pacing delay and fail-closed manual crash reconciliation
- [x] Once-authorized automatic candidate selection -> durable source queue enrollment from analyze/discover, bounded confidence/warning/stream eligibility filters
- [x] Detect COMPLETED acquisition queue and persist a separately claimed local FFmpeg MP4 render with policy/AV checks and technical QA evidence; opt-in scheduled worker processes one render in a later idle tick
- [x] Auto-create optional cut-aligned SRT sidecars from traceable local source transcripts, with no guessed partial word timings and persistent caption evidence
- [x] First deterministic Japanese ASS/libass burn-in into a separate create-only captioned MP4, with font detection and optional QA evidence (not editorially qualified)
- [x] First conservative cut-aware audio leveling baseline: per-cut measured dBFS correction capped at ±6 dB, silence guard, peak limiter, post-encode QA, separate MP4 and durable optional evidence
- [ ] Complete perceptual LUFS/audio-dialogue qualification, music versus speech balancing and verified near-boundary listening tests
- [x] Localized review ledgers for measured anomalies: event timestamps mapped to ordered cuts, suggested manual inspection action and immutable SHA-256-bound JSON, no automatic content edits
- [x] Exact canonical YouTube source-ID matched human-vs-generated interval geometry on QA-passed offline rendered timelines, holdout collisions blocked, human alignment treated as in-sample reference only
- [x] Read-only per-pair source-selection disagreement windows and playback source-time backtracks, union-corrected gap detection; no automatic edit proposals or human-quality scores
- [x] Page-local, one-per-job inspection priority queue for strongest observed source-selection differences with exact-interval playback order check, never a creative quality score or auto-adoption
- [x] Optional bounded before/within/after local ASR source-time context for ranked discrepancies with explicit source provenance limitations and missing evidence status
- [x] Source-clock handoff + ASR speech-boundary observations for actual generated output join order (bounded, provenance-qualified, not creative narrative roles or a quality score)
- [x] Source-grounded human-aligned first/middle/last positional coverage and bounded clip-level setup/development/payoff reference annotations (NOT role-time anchors or creative quality labels)
- [x] Opt-in, qualified-provider semantic textual-reference hypotheses with strict exact-ASR-citation validator, bounded dry-run evidence and immutable fail-closed receipt (never editorial gold or auto-edit)
- [x] Independent offline re-audit of semantic receipts against newly regenerated same-source/holdout/ASR evidence, verbatim quote ID/clock/source-side and explicit missing evidence, with no creative precision/recall claims
- [x] Built-in offline synthetic context/setup/development/payoff/reaction omissions and replay fixture benchmark with selection-bound ASR citation flags; explicitly exposes real semantic role accuracy blind spot, no production pass
- [x] Read-only real existing human-alignment omission probes (opening/middle/closing, optional source-ASR observations, bounded and privacy-hashed), no semantics-as-ground-truth or new manual datasets
- [x] Reconcile ingest's native segments-only source transcript format with provenance-based real/semantic evaluation eligibility; exact trusted manifest path and source URL checks without weakening filename-only restrictions
- [x] Bounded one-command real local corpus readiness audit with explicit missing/ambiguous ASR reasons and source-diverse evidence suggestions, no semantic quality labels or user-prepared dataset
- [x] Dry-run source-grounded real human-vs-omission contrasts and explicit qualified AI textual comparison of lost/retained ASR (strict paired citations, no semantic gold or auto-edit)
- [x] Explicit qualified two-order model repeatability probe (per-run exact lost/retained quote verification, route identity, redacted quote hashes, no narrative quality inference)
- [x] Offline re-audit of redacted two-order receipts against regenerated local source clock/ASR substring hashes, route comparisons, per-variant and aggregate counts; not signed authenticity or creative-quality gold
- [x] Bounded ASR-clock-backed real cut extension trials from verified adjacent human-only context, ordered before/after timeline previews; no blind editing or media substitution
- [x] Explicit FFmpeg MP4 cut-extension trial B against rehashed QA-passed original A, current cache/policy checks, immutable SHA-bound technical comparison sidecar, no automatic adoption or quality promotion
- [x] Replay original A's QA-documented cut-audio leveling and caption/SRT/burnin stages onto extension B with immutable matched-stage SHA evidence, no quality inference or overwrite
- [x] Recheck original A MP4 bytes after finishing B and retain FFprobe-measured final B duration separately from expected timeline duration (offline regression only; real A/B qualification still pending)
- [x] Source-ASR-clock speech boundary audit for one finished cut-extension A/B with local decoded A-adjacent/B-added audio dBFS sampling, checksum-protected report and no creative-quality claims (requires owner-local real video execution for actual measurement)
- [x] Conservative preview-only alternative cut edges for ASR utterances clipped by the current finished trial, with strict <=2s additional budget, no false improvement claims, and no automatic execution (real creative validation remains pending)
- [x] Preview-first B2 MP4 rendering from separately SHA-verified A/B and source-ASR boundary proposal, with local source cache/policy/FFmpeg QA, immutable three-way duration receipt and no matched finishing or editorial promotion
- [x] Explicit B2 matched optional audio leveling/SRT/burn-in on existing SHA-pinned local B2 video with separate immutable stage-parity receipt, no editorial quality or publication promotion
- [x] Read-only-first, SHA-verified three-way A/B/B2 local decoded A/V and subtitle technical review hints, immutable sidecar and no creative ranking (real footage evaluation still required)
- [x] Three-way source-time-matched, review-first operator triage worklist grounded in local measured diagnostics and existing review-ledger cut mapping, without automatic winner/quality claims
- [x] Provenance-guarded offline structural reference from trusted human-made clips and bounded A/B technical receipts, with holdout collision blocking and no cross-domain quality claims
- [x] SHA-bound independent A/B signal audit for audio/subtitle/cut snippets: remeasure audio mean/peak and decoded SSIM, no invented human preference or auto-adoption
- [x] Create-only, bounded original-vs-trial cut seam video A/B preview with timestamp-preserving visual darkening, independent SHA-pinned receipt, and no automatic promotion
- [x] Create-only, bounded Japanese fast-subtitle A/B typography comparison preserving SRT text/clock and immutable baseline; no claimed timing repair or autonomous adoption
- [x] Initial reversible offline short audio-peak A/B experiment: unchanged verified original MP4 rollback reference, separately SHA-verified before/after listening samples, no automatic adoption
- [ ] Calibrate actual before/after outcomes and add genuinely safe repair candidates beyond sample attenuation using independently rated real videos
- [x] Provenance-bound, deterministic review inspection priority and non-executable repair-checklist drafts, preserving every warning and unverified editorial status
- [x] Source-clock-aligned bounded black/freeze anomaly context from existing cached original media, independent immutable evidence and fail-closed source windows, never automatic repair
- [x] Durable append-only per-render review backlog and multi-page review sampling beyond the 40-ledger cap, with separate SQLite evidence, no new media downloads and honest unlabelled status
- [x] Create SHA-verified, balanced offline unlabeled quality-diagnostic sampling packs from local review ledgers; report existing trusted human alignment corpus separately and never invent visual/audio human gold
- [ ] Obtain independent human-rated labels for sampled signals and calibrate precision/recall across sources before any automatic correction
- [x] First bounded, read-only edit diagnostics: black/frozen-frame hints, source audio dBFS peak/mean, caption reading rate and line overflow; persisted QA evidence, never a production pass
- [ ] Calibrated human-rated thresholds, real footage frame review, safe automatic suggested repairs with preview/rollback, and measured publishing qualification
- [x] First conservative single-pass guarded visual-style baseline (high-confidence reaction/payoff gentle <=3.5% zoom, explicitly hinted 80ms A/V dip, unchanged cut clock and technical evidence)
- [ ] Learned framing/camera focus, scene-aware natural transitions, actual subtitle legibility, word-level alignment and human-quality QA/repair before approved publishing
- [ ] Complete creative editing, captions, sound processing, human-quality evaluation, output repair and publication approval without promoting unqualified previews
- [ ] Integrate completion of source acquisition with local rendering, technical QA and further editorial review without repeated user preparation; never auto-publish unqualified clips
- [ ] Automatically enroll queue jobs from candidate selection and connect post-acquisition rendering/QA without per-video setup or unsupported failure auto-retries
- [x] Read-only automatic canonical source ID to full editing_video cache mapping, with identity and ambiguity fail-closed checks
- [x] Preview-first local render command, explicit execution, input hashes, no-overwrite commit and basic A/V ffprobe QA
- [ ] Complete formal NarrativePlan/RenderPlan authoring and media-editing profile
- [ ] Subtitle, framing, transitions, loudness and editorial QA/repair
- [ ] Real unseen-stream validation and production provider qualification

This baseline renders **only local A/V previews**; it is not production-ready or policy-certified. See [local renderer guide](local-render-plan.md).

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
