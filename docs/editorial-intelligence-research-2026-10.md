# Editorial Intelligence Research — 2026-10

## Purpose

Holoclip must not be a narrow "find one funny timestamp" tool.

The product target is an autonomous editorial system that can discover, propose, assemble, and explain multiple kinds of Hololive clip ideas without requiring the user to already know which moment, member, topic, source stream, or format should be used.

The core product question is therefore:

> Given a growing archive of streams, chat reactions, historical context, and human-made clipping examples, what original clip ideas are worth making now, which sources support each idea, and what edit structure best communicates the idea?

This document records the design implications of reviewing current Hololive clip formats, current open-source clipping systems, current highlight/moment-retrieval research, and current platform constraints.

## Research observations

### Real clip output is heterogeneous

Current Hololive clip ecosystems contain:

- sub-minute micro moments and Shorts
- 1–3 minute single jokes/reactions
- 3–10 minute single-topic stories
- same-stream multi-cut stories
- multi-member reaction montages
- cross-stream callbacks and "one year later" comparisons
- topical compilations
- chronology / history / anniversary compilations
- long 30–60+ minute collections

Holodex clip listings in 2026 show this range directly, including sub-minute clips, multi-minute stories, 10-minute compilations, and hour-long multi-member compilations.

**Decision:** duration must be an output of editorial planning, not a fixed global target. 3–6 minutes remains a useful standard-story prior, not a rule.

### One clip can have many sources

Real compilations can use many talents and many source streams. A "clip example" cannot be modeled as one clip URL plus one source stream forever.

**Decision:** introduce a source-independent editorial timeline:

~~~text
ClipIdea / ClipProject
  sources[]
  timeline[]
    source_asset_id
    source_start/source_end
    output_start/output_end
    purpose
    confidence
~~~

Same-stream multi-cut alignment becomes a special case of the general timeline.

### Selection and editing are separate learning problems

Human-made clips provide at least three different kinds of supervision:

1. **Selection:** which source moments were considered worth retaining.
2. **Structure:** how retained moments were ordered into setup / development / payoff / callback / contrast.
3. **Presentation:** subtitles, zooms, reframing, image inserts, sound effects, pacing, and other visual/audio edits.

These should not be collapsed into one model or one score.

**Decision:** first learn selection + narrative structure. Learn presentation style as a separate edit layer later.

### Chat is a real signal, not just decoration

Prior VTuber-specific research compared source streams and human-made clips and found that many clipped moments coincide with increased audience chat activity. Separate livestream-highlight research also supports combining chat, audio, and video features.

For Japanese VTuber chat, raw message count is insufficient. High-density chat commonly contains repeated short reactions, and topical/discourse changes influence activity.

**Decision:** add a first-class AudienceReactionSignal with:
- messages/minute and burst z-score
- unique chatter count
- duplicate/repeated-reaction ratio
- reaction lexemes/emotes (e.g. 草, w, 888, てぇてぇ) as configurable lexicons
- sentiment/emotion distribution
- lag-aware alignment because chat often reacts after the triggering moment
- novelty versus that stream's local baseline

Chat can boost or contextualize a candidate but must never be the only definition of "interesting".

### Audio and visual evidence matter

Recent multimodal highlight work shows value in combining visual information and affective/audio features, while current moment-retrieval research emphasizes precise temporal localization and global context.

**Decision:** candidate evidence will be layered:

1. transcript semantics
2. audience reaction
3. acoustic/prosodic reaction
4. visual/game/scene signals
5. historical/relationship context
6. human-clip weak supervision
7. normalized public-performance metadata

No single layer is mandatory for every genre.

### Global context and local boundaries are different tasks

Current video moment retrieval/highlight research explicitly distinguishes local moment localization from global saliency/context. Short moments are also unusually difficult to localize precisely.

**Decision:** do not ask one pass to solve everything.

Use:
- broad/high-recall event discovery
- concept-level global reasoning
- separate boundary refinement
- final timeline assembly

### LLMs should select measured IDs, not invent timestamps

Current open-source clipping systems have identified a practical reliability issue: language models are better at copying explicit segment/word identifiers than doing timestamp arithmetic.

**Decision:** migrate candidate/edit contracts from "LLM emits float timestamps" toward:
- transcript segment IDs
- optionally word IDs
- shot IDs / chat-window IDs

Measured timestamps are looked up afterward.

### Resumeability is part of product quality

Long-video analysis is expensive and failure-prone even with free endpoints. Current local-first clippers persist stage artifacts and resume from the failed stage.

**Decision:** every expensive stage must become content-addressed / resumable:
- source metadata
- source/audio fingerprint
- transcription chunks
- chat features
- visual/audio features
- broad discoveries
- clip-source alignments
- example enrichment
- embeddings
- idea generation
- edit plans
- render artifacts

A retry must not repeat successful upstream work.

## Supported editorial archetypes

The system should proactively consider all of these without user prompting.

### A. Micro moment
Typical output: 15–90 seconds.

Examples:
- one perfect reaction
- one punchline
- one surprising phrase
- one fail immediately after confidence
- one "clip this" chat spike

Good for Shorts or a short landscape clip.

### B. Single-topic mini story
Typical output: 1–6 minutes.

Structure:
- setup
- development
- payoff

May be continuous or remove dead space.

### C. Same-stream multi-cut story
Typical output: 2–10 minutes.

Several moments from the same stream are joined while irrelevant middle material is removed.

### D. Callback / payoff across time
Typical output: 2–12 minutes.

Examples:
- past prediction -> current outcome
- "I would never..." -> later doing exactly that
- recurring joke reaching a payoff
- old weakness -> later mastery

Requires historical retrieval.

### E. Multi-member reaction montage
Typical output: 2–15 minutes.

One event/topic viewed through reactions from several members or perspectives.

### F. Relationship / chemistry story
Typical output: 3–15 minutes.

The clip is built around a relationship dynamic rather than one isolated event.

### G. Contrast / comparison
Typical output: 1–10 minutes.

Examples:
- confidence vs failure
- first attempt vs latest attempt
- member A vs member B
- expectation vs reality

### H. Chronology / evolution
Typical output: 5–60+ minutes.

Examples:
- a running joke over months
- a member's growth
- anniversary/debut retrospective
- all reactions to a recurring event

### I. Event digest / compilation
Typical output: 5–60+ minutes.

Examples:
- funniest moments from an event
- many members' reactions
- themed collection

### J. Contextual explainer
Typical output: 3–12 minutes.

For a moment that is not understandable from the local clip alone. The system gathers the minimum historical context necessary for a new viewer.

### K. Discovery idea
An idea that no existing human clip directly teaches.

The system must retain an exploration path so it can surface new concepts instead of only imitating the example database.

## Autonomous ideation

The system must generate proposals without requiring the user to specify a theme.

Introduce an **EditorialIdeaGenerator** above ordinary candidate detection.

It operates at several horizons.

### Stream-local horizon

Triggered after a new archive is available.

Ask:
- What is the strongest standalone moment?
- Is there a better multi-cut story than any individual moment?
- Is a short-form micro moment justified?
- Is there a topic that needs extra context?

### Cross-stream recent horizon

Runs across a recent window.

Ask:
- Did several members react to the same event?
- Did today's event resolve a statement from yesterday/last week?
- Are there contrasting reactions worth combining?
- Is a collaboration story spread across multiple POVs?

### Historical horizon

Search the archive.

Ask:
- Is this a callback to an old statement?
- Has this pattern happened repeatedly?
- Is there a meaningful before/after comparison?
- Does this complete a long-running narrative?

### Periodic editorial horizon

Daily/weekly generation can propose:
- recurring-joke compilation
- member relationship compilation
- weekly event digest
- under-clipped high-quality archive moment
- anniversary/history idea

## Idea contract

A proposed idea should not be only a title.

~~~text
EditorialIdea
  id
  archetype
  thesis
  viewer_promise
  hook
  target_duration_range
  evidence[]
  required_sources[]
  optional_sources[]
  narrative_beats[]
  novelty_reason
  example_support[]
  counterevidence[]
  policy_risks[]
  confidence
~~~

The final selector should rank ideas, not only timestamp candidates.

## Evidence graph

Flat JSONL is adequate for the first PoC, but the conceptual data model should be a graph:

- SourceAsset
- SourceMoment
- TranscriptSegment / Word
- ChatWindow
- AudioEvent
- VisualEvent / Shot
- Talent
- Topic
- Relationship
- Event
- HumanClipExample
- ClipTimeline
- EditorialIdea
- ReviewFeedback

Edges can represent:
- occurs_in
- mentions
- reacts_to
- same_event_as
- callback_to
- contradicts
- follows_up
- clipped_as
- selected_from
- removed_between
- related_to

This is how Holoclip can discover ideas that cross stream boundaries.

## Retrieval strategy

Do not retrieve only "similar positive clips".

For each new idea/candidate retrieve:

- structurally similar positive examples
- structurally similar weak negatives
- same-member examples
- same-relationship examples
- same-topic examples
- historical callbacks
- examples with similar audience reaction but different editorial outcome
- duplicate/similar existing clips to estimate novelty risk

The final reasoning should preserve the IDs of retrieved evidence.

## Diversity and novelty

A perfect ranker can still make a bad channel if every output has the same pattern.

The planner should explicitly optimize a portfolio of ideas across:
- archetype
- talent/member mix
- duration
- topic
- emotional tone
- recent vs archival context
- short vs standard vs compilation

Add a novelty check against:
- existing human clip examples
- previously generated Holoclip ideas
- already published Holoclip videos

This also helps avoid mechanically repetitive output.

## Platform and rights constraints

COVER's current Clip Guidelines require original source URL/title at the beginning of the description, no upload before the source stream ends and VOD is public, and exclude paid/member-only/concert content unless explicitly permitted. Third-party rights and platform rules remain applicable.

YouTube monetization policy continues to evaluate reused clips/compilations separately and expects borrowed content to be changed significantly; YouTube also explicitly treats repetitive or mass-produced template content as inauthentic for monetization.

**Decision:** policy and originality checks are not a final publishing-only step. They influence idea generation:
- exclude disallowed sources before planning
- penalize near-duplicate/template output
- require an editorial thesis / transformation rationale
- keep exact provenance for every source segment

## Source discovery / acquisition

Keep source acquisition replaceable.

Metadata discovery may combine:
- official YouTube Data API
- optional VTuber-specific metadata providers such as Holodex
- local user-provided files
- future authorized integrations

Holodex exposes VTuber-oriented video relationships including clips and source/referral relationships, which may be useful for bootstrapping human-clip/example discovery. It should remain optional and never become a hard dependency.

The official YouTube Data API is suitable for metadata, but arbitrary third-party video-file download is not an official Data API capability; caption download also requires permission to edit the video.

## Rendering architecture

Separate semantic planning from deterministic execution.

~~~text
EditorialIdea
  -> NarrativePlan
  -> TimelinePlan
  -> AssetRequest[]
  -> SourceProvider
  -> measured source segments
  -> RenderPlan
  -> FFmpeg / caption / reframing pipeline
  -> preview
  -> human approval
  -> final render
~~~

Rendering must never be the component deciding what is interesting.

## Presentation-learning layer

Later, compare human clips with aligned source footage to learn:

- cut density
- dead-air removal
- subtitle density and emphasis
- crop/reframe
- zoom timing
- reaction-face emphasis
- inserts
- sound effects
- music use
- pacing

Keep this separate from selection/narrative learning.

Use shot-boundary detection before applying motion/reframing so automated camera movement never interpolates across an edit.

## Evaluation matrix

Top-K acceptance remains useful, but is insufficient.

### Discovery
- Accept@3 / Accept@5
- known-human-clip recall
- novel accepted ideas not present in examples
- candidate burden

### Temporal
- boundary IoU
- source segment precision/recall
- multicut edit-map accuracy
- short-moment localization error

### Editorial
- setup/development/payoff completeness
- context sufficiency
- idea novelty
- archetype diversity
- evidence correctness
- cross-source factual consistency

### Product
- preview -> publish rate
- revise rate
- reject rate
- user review time per published clip
- repeated/manual work avoided
- rendering waste before approval

### Safety/policy
- provenance completeness
- disallowed-source detection
- duplicate/template similarity
- source-title/URL attribution completeness

## Recommended implementation sequence

1. Keep alignment v2 and make it the source-timeline primitive.
2. Generalize from one source stream to SourceAsset + ClipTimeline.
3. Move LLM temporal decisions toward measured segment/word IDs.
4. Add EditorialIdea + EditorialIdeaGenerator.
5. Add transcript-structural retrieval over human clip examples.
6. Add live-chat feature ingestion and lag-aware reaction windows.
7. Add historical/entity/topic/relationship graph.
8. Add audio reaction signals.
9. Add visual/shot signals where transcript/chat are insufficient.
10. Evaluate 3 clip families before scaling examples:
   - local single-topic
   - same-stream multicut
   - cross-stream / multi-member
11. Add deterministic TimelinePlan and preview render.
12. Add presentation-learning and edit-style features.
13. Move persistent orchestration/retrieval to cloud only after local end-to-end quality is demonstrated.

## References reviewed

- COVER, Derivative Works / Clip Guidelines: https://hololivepro.com/en/terms/
- YouTube channel monetization policy: https://support.google.com/youtube/answer/1311392
- YouTube Data API videos: https://developers.google.com/youtube/v3/docs/videos/list
- YouTube Data API captions: https://developers.google.com/youtube/v3/docs/captions/download
- YouTube Clips help: https://support.google.com/youtube/answer/10332730
- Holodex API v2 docs: https://docs.holodex.net/
- Holodex current clip listings: https://holodex.net/
- AutoClip open-source architecture: https://github.com/artbyjazi/autoclip
- Cutroom: https://github.com/Batuktech/Cutroom
- VideoHighlighter: https://github.com/Aseiel/VideoHighlighter
- TVHighlights, CVPR 2026
- Saliency-Guided DETR, WACV 2026
- MomentMix, WACV 2026
- VTuber highlight/chat study (FIT 2023 / CiNii)
- Japanese VTuber just-chatting chat analysis (NINJAL 2026)
