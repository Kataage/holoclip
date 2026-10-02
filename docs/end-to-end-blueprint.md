# Holoclip End-to-End Blueprint

Status: architecture baseline  
Date: 2026-10-02

## 1. Product definition

Holoclip is not a timestamp finder and not merely an automatic video cutter.

The target product is an autonomous editorial system that can:

1. discover source streams and related public context,
2. extract multimodal evidence,
3. remember historical events, relationships, topics, and prior editorial decisions,
4. learn from human-made clips without merely imitating them,
5. proactively propose multiple editorial ideas,
6. assemble one or more source moments into a coherent narrative,
7. produce deterministic edit plans and previews,
8. ask the user for minimal high-value feedback,
9. publish only after explicit approval,
10. learn from review and post-publication outcomes.

The user should not have to watch every source stream, manually identify every interesting moment, or specify every clip format in advance.

## 2. Non-negotiable design rules

### 2.1 Separate evidence, editorial judgment, and execution

Holoclip must keep these layers distinct:

~~~text
Evidence extraction
    -> editorial reasoning
    -> narrative/timeline planning
    -> deterministic media execution
~~~

FFmpeg never decides what is interesting.
The LLM never fabricates measured timestamps.
Retrieval never silently becomes the final judgment.

### 2.2 Measured media time is authoritative

LLM output should reference stable IDs whenever possible:

- transcript_segment_id
- transcript_word_id
- chat_window_id
- audio_event_id
- shot_id
- source_moment_id

The application resolves those IDs back to measured source times.

Raw floating-point timestamps emitted by an LLM are transitional compatibility only.

### 2.3 One published clip may use many sources

Never permanently assume:

~~~text
one clip = one stream
~~~

The canonical model must support:

~~~text
one ClipProject
  -> many SourceAssets
  -> many source ranges
  -> arbitrary editorial ordering
~~~

Same-stream continuous clips and same-stream multi-cut clips are special cases.

### 2.4 Human clips are supervision, not templates

Human-made clips teach:

- what editors selected,
- what they omitted,
- how they structured an idea,
- what kind of context they preserved,
- which patterns recur across successful editorial decisions.

They do not define the only acceptable output.

Holoclip must keep an explicit exploration path for novel ideas.

### 2.5 Missing modalities must degrade gracefully

A stream without usable chat or visual signals must still be analyzable from transcript/audio.
A game stream with weak transcript semantics may lean more heavily on chat/audio/visual signals.

No optional modality may become a hard global dependency.

### 2.6 Every expensive stage is resumable

A timeout, quota reset, machine reboot, or provider failure must not restart completed upstream work.

Every expensive artifact needs:

- stable input identity
- stage version
- provider/model version where relevant
- status
- output reference
- error state
- retry metadata

### 2.7 Human approval remains the publishing gate

The long-term UX can reduce review burden aggressively, but publication is a distinct action.

A high model score must not itself trigger irreversible publication.

## 3. Canonical entities

These are conceptual contracts. Storage implementation may evolve, but these identities should remain stable.

### 3.1 SourceAsset

Represents one source media object or logical stream.

~~~text
SourceAsset
  id
  platform
  external_id
  canonical_url
  title
  channel_id
  channel_name
  published_at
  duration
  availability
  access_class
  source_type
  metadata
  provenance
  content_fingerprint?
~~~

Examples:

- one YouTube archive
- one local media file
- one authorized uploaded source
- one external clip used as a human-supervision example

The source identity must not depend on a local filesystem path.

### 3.2 SourceMaterial

Represents locally or remotely available material derived from a SourceAsset.

~~~text
SourceMaterial
  id
  source_asset_id
  kind
    audio
    video
    representative_frames
    chat
    transcript
  locator
  quality
  time_range?
  checksum
  expires_at?
  temporary
~~~

This separates "the source exists" from "we currently possess bytes for it".

### 3.3 TranscriptSegment

The long-term transcript contract becomes:

~~~text
TranscriptSegment
  id
  source_asset_id
  start
  end
  text
  speaker?
  confidence?
  word_ids[]
  transcription_artifact_id
~~~

IDs must be stable within one transcript artifact.

Word-level timing is optional initially but the contract allows it.

### 3.4 EvidenceItem

A common envelope for observed evidence.

~~~text
EvidenceItem
  id
  source_asset_id
  kind
  start
  end
  payload
  confidence
  producer
  artifact_id
~~~

Kinds can include:

- transcript_semantic
- chat_burst
- chat_reaction
- laughter
- shout
- silence
- prosody_shift
- game_event
- shot_change
- face/reaction
- on_screen_text
- topic_change
- historical_match

This gives EditorialIdea one provenance format rather than separate ad-hoc references.

### 3.5 SourceMoment

A normalized source-local editorial unit.

~~~text
SourceMoment
  id
  source_asset_id
  evidence_ids[]
  transcript_segment_ids[]
  start
  end
  moment_type
  summary
  traits[]
  entities[]
  topics[]
  confidence
~~~

A SourceMoment is not yet a clip idea.
It is a reusable observed event/moment.

### 3.6 HumanClipExample

HumanClipExample becomes supervision around a human editorial artifact.

~~~text
HumanClipExample
  id
  clip_source_asset_id
  source_asset_ids[]
  clip_timeline
  story_structure
  selection_reason
  traits[]
  entities[]
  relationship_context
  performance_features
  weak_negatives[]
  alignment_confidence
  enrichment_provenance
~~~

Legacy single-source fields remain import compatibility only.

### 3.7 TimelineSegment

The universal edit-map primitive.

~~~text
TimelineSegment
  id
  source_asset_id
  source_start
  source_end
  source_segment_ids[]
  output_order
  purpose
  confidence
  transition_hint?
~~~

Purposes may include:

- hook
- setup
- context
- development
- contrast
- callback
- reaction
- payoff
- epilogue

### 3.8 ClipTimeline

~~~text
ClipTimeline
  id
  segments[]
  estimated_duration
  timeline_version
  evidence_ids[]
~~~

This is semantic structure, not yet a fully specified render pipeline.

### 3.9 EditorialIdea

The core product-level proposal.

~~~text
EditorialIdea
  id
  archetype
  thesis
  viewer_promise
  hook
  target_duration_range
  required_source_asset_ids[]
  optional_source_asset_ids[]
  evidence_ids[]
  source_moment_ids[]
  narrative_beats[]
  example_support_ids[]
  counterevidence_ids[]
  novelty_reason
  novelty_score
  duplicate_risk
  policy_risks[]
  confidence
  generation_horizon
  status
~~~

Generation horizons:

- stream_local
- recent_cross_stream
- historical
- periodic
- exploratory

### 3.10 NarrativePlan

EditorialIdea says what story should be made.
NarrativePlan says how the story should unfold.

~~~text
NarrativePlan
  id
  idea_id
  beats[]
    purpose
    source_moment_ids[]
    required_context
    optional_context
  expected_duration
  unresolved_questions[]
~~~

### 3.11 TimelinePlan

Resolves NarrativePlan into precise measured source ranges.

~~~text
TimelinePlan
  id
  narrative_plan_id
  timeline_segments[]
  total_duration
  boundary_confidence
  unresolved_boundaries[]
~~~

This is where boundary refinement occurs.

### 3.12 RenderPlan

The deterministic execution contract.

~~~text
RenderPlan
  id
  timeline_plan_id
  asset_requests[]
  video_format
  audio_format
  subtitle_plan
  reframing_plan
  emphasis_plan
  transition_plan
  render_profile
  provenance_block
~~~

No editorial discovery should happen inside RenderPlan execution.

### 3.13 ReviewRecord

Review must generalize beyond candidate acceptance.

~~~text
ReviewRecord
  id
  target_type
    source_moment
    editorial_idea
    preview
    published_video
  target_id
  decision
    accept
    maybe
    reject
    revise
    publish
  reasons[]
  freeform_note?
  created_at
~~~

Later review UX can remain simple while storing richer structured reasons.

### 3.14 PublicationRecord

~~~text
PublicationRecord
  id
  clip_project_id
  platform
  external_id
  published_at
  title
  description
  thumbnail_ref
  source_attributions[]
  policy_check_version
~~~

### 3.15 OutcomeSnapshot

Post-publication signals are observations, not direct labels of editorial quality.

~~~text
OutcomeSnapshot
  publication_id
  observed_at
  age_hours
  views
  impressions?
  ctr?
  watch_time?
  average_view_duration?
  retention_summary?
  likes?
  comments?
  normalized_cohort_features
~~~

Keep outcome signals separable from:

- idea quality
- edit quality
- packaging quality
- timing/channel effects

## 4. Canonical pipeline

### Stage 0 — discovery trigger

Possible triggers:

- new source archive becomes public
- user imports local media
- recent event produces multiple related streams
- historical/periodic editorial job
- new human clip example
- user asks for a specific theme

Output:
SourceAsset records and jobs.

### Stage 1 — source eligibility and metadata

Resolve:

- source identity
- metadata
- access class
- policy eligibility
- source relationships
- availability state

Do not acquire full media unless downstream needs it.

### Stage 2 — lightweight evidence acquisition

Prefer the cheapest useful signals first:

1. metadata
2. transcript/audio
3. chat
4. selected frames / shot information
5. full-resolution video segments only when needed

The goal is to avoid downloading/rendering expensive media before editorial value is established.

### Stage 3 — transcription and time indexing

Create stable TranscriptSegment IDs.
If word-level timing exists, create word IDs.

Persist chunk-level progress so failed transcription resumes.

### Stage 4 — multimodal evidence extraction

Independent producers create EvidenceItems:

- transcript semantic/events
- chat bursts/reactions
- audio/prosody
- visual/shot/game cues
- entity/topic mentions

Each producer may succeed or fail independently.

### Stage 5 — SourceMoment discovery

High-recall moment detection operates over one or more evidence types.

This replaces the long-term idea that a ClipCandidate itself is the final unit.

Output:
many SourceMoments with evidence provenance.

### Stage 6 — memory/retrieval enrichment

Retrieve:

- similar positive human examples
- weak negatives
- same member
- same relationship
- same topic
- recent related events
- historical callbacks
- previous Holoclip ideas/publications
- duplicate/similarity warnings

Retrieval enriches context; it does not decide the final idea by itself.

### Stage 7 — autonomous EditorialIdea generation

Run several ideation horizons.

#### 7.1 Stream-local

Generate possible:

- micro moment
- single-topic story
- same-stream multi-cut
- contextual explainer

#### 7.2 Recent cross-stream

Look for:

- multiple reactions to same event
- collaboration POV differences
- contrasting statements
- recent callbacks

#### 7.3 Historical

Look for:

- old prediction -> current outcome
- recurring joke
- relationship evolution
- before/after growth
- contradiction/callback

#### 7.4 Periodic

Generate:

- weekly digest
- event compilation
- relationship compilation
- recurring-theme history
- under-clipped archive idea

#### 7.5 Exploration

Explicitly search for ideas weakly supported by prior human examples but strongly supported by current evidence.

Output:
EditorialIdea portfolio, not one magical winner.

### Stage 8 — idea ranking and portfolio selection

Ranking dimensions include:

- viewer promise
- thesis clarity
- evidence quality
- narrative completeness
- context sufficiency
- novelty
- duplicate risk
- policy/source risk
- expected format fit
- confidence
- portfolio diversity

Portfolio selection prevents the top results from all being variants of one idea.

"No publishable idea" is a valid output.

### Stage 9 — NarrativePlan

For accepted/top ideas, decide:

- hook
- setup
- necessary context
- development
- contrast/callback
- payoff
- optional epilogue

Do not yet perform frame-accurate media operations.

### Stage 10 — boundary refinement and TimelinePlan

Resolve narrative beats to measured IDs and exact source ranges.

For each timeline segment:

- identify exact transcript/word/shot IDs
- refine start/end
- preserve source provenance
- record boundary confidence

This is where alignment-v2 techniques evolve into general source timeline planning.

### Stage 11 — asset request

Only now ask SourceProvider for required media ranges.

~~~text
AssetRequest
  source_asset_id
  start
  end
  media_kind
  quality
  reason
~~~

SourceProvider decides how those bytes are supplied.

### Stage 12 — preview RenderPlan

Create low-cost preview:

- lower resolution/bitrate where appropriate
- deterministic cuts
- baseline subtitles
- minimal safe reframing
- no expensive decorative work unless necessary for evaluation

Preview should test editorial quality, not final polish.

### Stage 13 — human review

Primary review surface:

- Play
- Publish
- Revise
- Reject

Optional structured reasons:

- weak idea
- context missing
- wrong boundary
- wrong ordering
- too long
- too short
- repetitive
- misleading
- presentation issue
- policy concern

The system should infer as much as possible from action history so manual labeling stays cheap.

### Stage 14 — revision

Revise should target the smallest relevant layer.

Examples:

- boundary issue -> TimelinePlan only
- missing context -> NarrativePlan/TimelinePlan
- weak concept -> EditorialIdea
- subtitle styling -> RenderPlan
- source acquisition failure -> SourceProvider

Do not restart from transcription when the problem is only rendering.

### Stage 15 — final render

After approval:

- acquire final-quality requested ranges
- deterministic render
- final captions/reframing/presentation
- provenance/attribution generation
- policy checks

### Stage 16 — publication

Publication remains explicit.

Generate:

- title suggestions
- description
- source attribution
- thumbnail suggestions/assets
- schedule suggestions

Store exact publication metadata.

### Stage 17 — post-publication observation

Collect outcomes where available.

Use normalized cohorts and age windows.

Never collapse outcome into one "funny score".

### Stage 18 — learning loop

Update:

- human preference model/retrieval
- example usefulness
- idea archetype priors
- revision patterns
- packaging performance models
- exploration budget

Fine-tuning remains optional and only becomes justified after a sufficiently large, clean, evaluated dataset exists.

## 5. State machine and orchestration

Every long-running stage must use explicit state.

Recommended job states:

~~~text
pending
running
succeeded
failed_retryable
failed_terminal
blocked_quota
blocked_dependency
cancelled
stale
~~~

Every job stores:

- job_id
- stage
- subject_type
- subject_id
- input_fingerprint
- stage_version
- attempt
- provider
- model
- started_at
- completed_at
- output_artifact_ids[]
- error_class
- error_detail
- retry_after?

### Idempotency

A stage should be safely rerunnable.

If:

~~~text
input_fingerprint + stage_version + provider/model version
~~~

already has a successful artifact, reuse it unless forced.

### Staleness

Artifacts can become stale when:

- prompt/schema version changes
- model/provider changes materially
- upstream transcript changes
- source metadata changes
- user corrects provenance/alignment

Stale is different from deleted.
Keep provenance so comparisons remain possible.

## 6. Storage architecture

### 6.1 Canonical structured store

Use a repository/storage interface.

Recommended local reference implementation:
SQLite.

Recommended cloud target:
SQL-backed store compatible with the same logical schema.

The important decision is not a vendor; it is that entities/relationships are queryable and versioned.

JSONL remains acceptable for:
- export
- fixtures
- append-only debug traces
- migration input

It should not remain the long-term canonical relational store.

### 6.2 Artifact store

Large or stage-specific files belong outside relational rows:

- transcripts
- model raw responses
- audio chunks
- frame samples
- embeddings snapshots if needed
- preview renders
- final renders

Artifact rows store:
- checksum
- size
- content type
- logical owner
- local/cloud locator
- temporary/persistent flag
- expiry policy

### 6.3 Vector/search index

Vector search is derived state.

The canonical text/structure lives in the structured store.
Embeddings can be regenerated.

Do not make the vector index the only copy of editorial knowledge.

### 6.4 Graph relationships

Do not require a dedicated graph database initially.

Represent edges relationally:

~~~text
EntityEdge
  from_type
  from_id
  relation
  to_type
  to_id
  confidence
  evidence_ids[]
~~~

A graph database can be introduced later only if query complexity justifies it.

## 7. Local and cloud responsibility split

The architecture should support local-only development and later hybrid deployment.

### Cloud / always-on side

Best suited for:

- source discovery/metadata polling
- orchestration
- job queue
- structured metadata
- human-example database
- retrieval/index
- idea generation jobs
- review UI/API
- notification
- publication state

### Local render/analysis agent

Best suited for:

- local or user-authorized source material
- FFmpeg
- media segment extraction
- heavy local visual/audio processing
- preview/final rendering
- temporary media cache

The local agent should be stateless enough that it can reconnect and claim jobs.

### Important invariant

Cloud orchestration must not assume the local agent is always online.

A job requiring the local agent becomes blocked_dependency, not failed.

## 8. Provider boundaries

### SourceProvider

Capabilities, not implementation names, should drive behavior.

External extractor/downloader runtimes are operational dependencies, not ordinary static library
dependencies. For fast-moving providers such as yt-dlp:

- keep the extractor behind SourceProvider
- refresh the preferred release channel at the start of a new acquisition process
- pin the resolved extractor version for the lifetime of that job
- record extractor version/channel/runtime in provenance
- permit one forced fresh-runtime retry after an extractor failure
- never allow an extractor update to mutate semantic/editorial state
- keep a local/manual SourceProvider path so acquisition can be replaced independently

~~~text
get_metadata(source)
get_transcript(source)?
get_chat(source)?
materialize_audio(source, range?)
materialize_video(source, range?, quality?)
get_frames(source, ranges?)
~~~

Each provider reports supported capabilities.

The pipeline selects the cheapest compatible route.

### TranscriptionProvider

~~~text
transcribe(material, range?, resume_token?)
~~~

Must support persisted chunk progress at the Holoclip layer even if provider API itself has no resume mechanism.

### LLMProvider

Used for:

- semantic discovery
- enrichment
- editorial ideation
- narrative planning
- comparison/reasoning

It should not own storage, timestamps, or media execution.

### EmbeddingProvider

Derived search representation only.

### RenderEngine

Consumes RenderPlan deterministically.

Initial implementation can be FFmpeg-based.

## 9. Human-example learning

### 9.1 Positive evidence

Preserve:

- what was retained
- order
- source mappings
- story structure
- context
- performance metadata
- channel/editor provenance

### 9.2 Weak negatives

Categories:

- adjacent_unselected_before
- adjacent_unselected_after
- removed_between_selected_segments
- similar_unselected_same_source
- rejected_holoclip_candidate
- fair_low_performing_example

Never interpret one weak negative as proof that a moment is objectively bad.

### 9.3 Multi-clipper consensus

If several independent clip channels select the same underlying source event:

- collapse to one SourceMoment/event
- retain all clip examples
- increment consensus evidence

Consensus is a strong selection signal but still not an absolute quality label.

### 9.4 Human-example retrieval

Retrieve both positive and negative evidence.

Store retrieved example IDs on the resulting idea/decision trace.

### 9.5 Fine-tuning gate

Do not fine-tune before all are true:

- enough clean examples
- reliable provenance/alignment
- stable schemas
- a holdout evaluation set
- demonstrated RAG baseline
- evidence that fine-tuning solves a measured failure not already solved by retrieval/prompting

## 10. Editorial archetypes

The idea generator must proactively consider at least:

1. Micro moment
2. Single-topic mini story
3. Same-stream multi-cut story
4. Historical callback/payoff
5. Multi-member reaction montage
6. Relationship/chemistry story
7. Contrast/comparison
8. Chronology/evolution
9. Event digest/compilation
10. Contextual explainer
11. Exploratory/original concept

Duration is an output of archetype/material fit, not a global constant.

## 11. Feedback design

The user should not be forced into exhaustive labeling.

### Minimal actions

- accept/publish
- revise
- reject

### Passive signals

Also learn from:

- preview replay
- seek behavior if available
- revision type
- repeated boundary adjustments
- final publish choice
- abandonment

These are weaker signals and must be treated accordingly.

### Preference separation

Do not mix:

- "this event is objectively notable"
- "the user personally wants to publish this"
- "this format fits the channel strategy"

Keep separate features/labels where possible.

## 12. Evaluation gates

### Gate A — source alignment

Must pass on:
- continuous clips
- same-stream multi-cut
- cross-source examples

Metrics:
- segment precision/recall
- boundary error
- edit-map ordering

### Gate B — moment discovery

Top-K usefulness plus known-clip recall.
Also track accepted novel moments.

### Gate C — human-example retrieval

Must improve editorial decisions versus no-retrieval baseline.

### Gate D — autonomous idea generation

Must produce useful ideas across:
- local
- same-stream multi-cut
- cross-stream/multi-member

Must retain accepted exploratory ideas.

### Gate E — timeline planning

Human review should find source/order/boundary errors at an acceptable rate before rendering automation expands.

### Gate F — preview product loop

Measure:
- preview -> publish
- revise
- reject
- review time
- wasted render work

### Gate G — publication learning

Only after enough comparable outcomes exist should post-publication signals alter ranking priors.

## 13. Migration from the current PoC

Do not rewrite working code all at once.

### Step 1

Introduce stable IDs and canonical SourceAsset/ClipTimeline models alongside current models.

### Step 2

Add adapters:

~~~text
legacy TranscriptSegment -> canonical TranscriptSegment
ClipCandidate -> SourceMoment + compatibility candidate view
HumanClipExample single source fields -> SourceAsset + ClipTimeline
EvaluationRun.stream_id -> SourceAsset reference
~~~

### Step 3

New features use canonical models.
Old CLI commands continue through adapters.

### Step 4

Move canonical persistence to SQLite repository layer.

### Step 5

Migrate JSON/JSONL data through explicit import commands.

### Step 6

Deprecate legacy timestamp-only contracts only after real data migration tests pass.

## 14. Anti-patterns to avoid

Do not:

- build one giant end-to-end LLM prompt
- let an LLM invent exact timestamps
- use views as the direct quality label
- make chat volume the direct quality label
- assume one source per clip
- force all videos into one duration
- treat unselected material as hard negative
- hard-code one downloader into editorial logic
- render full-quality video before idea approval
- couple cloud orchestration to an always-online local PC
- hide retrieval provenance
- silently reuse stale artifacts after schema/model changes
- fine-tune before RAG evaluation
- make every generated video follow one template

## 15. Error and fallback policy

### Provider quota exhausted

State:
blocked_quota.

Behavior:
- persist progress
- do not fall through to paid provider
- resume after quota/reset/manual action

### LLM timeout

Retry boundedly.
Persist completed parallel tasks.
Never discard already completed broad-analysis chunks.

### Transcription partial failure

Persist every successful chunk.
Retry only missing chunks.

### Missing source media

Idea can remain valid but TimelinePlan/RenderPlan becomes blocked_dependency.

### Missing optional modality

Continue with lower evidence coverage.
Record missing modality in provenance.

### Ambiguous human-clip alignment

Do not auto-trust.
Store unresolved alignment and request targeted review later.

### Local agent offline

Queue remains pending/blocked_dependency.

## 16. Observability

Every user-visible long job should expose:

- current stage
- completed/total units
- whether progress is persisted
- retry/error state
- next dependency

Debug traces should connect:

~~~text
published clip
 -> RenderPlan
 -> TimelinePlan
 -> EditorialIdea
 -> SourceMoments
 -> EvidenceItems
 -> SourceAssets
~~~

and:

~~~text
EditorialIdea
 -> retrieved HumanClipExample IDs
 -> ReviewRecords
~~~

This traceability is required for debugging editorial mistakes.

## 17. Versioning

Version independently:

- schema_version
- prompt_version
- stage_version
- alignment_method
- feature_extractor_version
- retrieval_version
- idea_generator_version
- timeline_planner_version
- render_plan_version

Do not use one global app version as the only provenance.

## 18. Security and secrets

- API keys stay outside artifacts and repository data.
- Job artifacts must never serialize environment secrets.
- Source locators may be sensitive and should be handled separately from public provenance.
- Logs should redact authorization headers and provider secrets.

## 19. Deployment principle

Do not cloud-migrate because local execution feels inconvenient.

Cloud migration occurs when:

- local end-to-end quality is proven,
- canonical storage/repository interfaces are stable,
- jobs are resumable,
- cloud deployment materially reduces operator burden.

The local reference implementation remains a first-class supported execution mode.

## 20. Implementation order

### Foundation A — canonical data model

1. SourceAsset
2. stable TranscriptSegment IDs
3. EvidenceItem
4. SourceMoment
5. TimelineSegment / ClipTimeline
6. EditorialIdea
7. ReviewRecord

### Foundation B — persistence/orchestration

8. repository interface
9. SQLite reference store
10. ArtifactRecord
11. StageRun / resumability
12. migration/import of current JSON/JSONL

### Editorial intelligence

13. adapt current candidate detector into SourceMoment discovery
14. human-example structural retrieval
15. EditorialIdeaGenerator stream-local
16. portfolio ranking/diversity
17. recent cross-stream
18. historical graph/retrieval
19. exploration strategy

### Multimodal

20. chat
21. audio
22. visual/shot

### Planning/execution

23. NarrativePlan
24. boundary refinement
25. TimelinePlan
26. AssetRequest
27. RenderPlan
28. FFmpeg preview

### Product loop

29. review UI
30. revision routing
31. final render
32. publication
33. outcome observation
34. learning loop

## 21. Immediate next milestone

Before adding more learning examples, implement the smallest canonical foundation:

- SourceAsset
- stable TranscriptSegment IDs
- TimelineSegment / ClipTimeline
- compatibility conversion from current alignment v2
- EditorialIdea schema
- local SQLite repository abstraction
- StageRun/artifact resumability skeleton

Then validate one existing continuous example and one synthetic multicut example through the new models.

Only after that should we scale human-example ingestion.
