# Editing Intelligence Architecture v1

Status: normative design baseline  
Parent: #80  
Scope: learn post-production craft from aligned human-made clips, compile learned decisions into a deterministic RenderPlan, render, validate, and repair.

## 1. Product definition

Holoclip is not complete when it can identify timestamps, concatenate ranges, or burn literal subtitles.

The production target is:

> Learn what human clip editors select, how they transform source footage into a coherent and entertaining finished clip, and apply those generalized editorial and post-production mechanisms to unseen streams without copying any specific editor's identity.

The system therefore has two distinct learned models:

1. **Editorial Profile** — what is worth making, why, and what source/story boundaries are needed.
2. **Editing Profile** — how a selected story should be paced, captioned, reframed, mixed, emphasized, transitioned, and presented.

They are compiled into a deterministic execution contract:

```text
Editorial Profile
        +
Editing Profile
        +
Measured source evidence
        ↓
NarrativePlan / TimelinePlan
        ↓
RenderPlan
        ↓
Deterministic renderer
        ↓
Render QA
        ↓
Repair / re-render
        ↓
Finished clip
```

No discovery is allowed inside the renderer.

## 2. Non-negotiable rules

### 2.1 Measure before interpreting

If a property can be measured from media, it must not be invented by an LLM.

Measured examples:
- output/source time correspondence
- cut boundaries
- source gaps
- frame geometry
- crop/scale/translation
- subtitle bounding boxes
- displayed text intervals
- loudness
- fade duration
- shot boundaries
- source switching
- freeze/speed mapping

LLMs may interpret purpose after the measurement exists.

### 2.2 The source-to-finished transformation is supervision

The training target is not the finished clip alone.

For each human example, learning compares:

```text
original source footage
        ↓
human editing transformation
        ↓
finished clip
```

The transformation itself is the primary editing supervision.

### 2.3 Unknown is not absent

Failure to detect an edit is stored as unknown, not as evidence that the edit did not happen.

Every observation carries:
- confidence
- extractor/version
- source evidence
- measured time/region
- unknown/error state where appropriate

### 2.4 Separate observation, interpretation, planning, execution

```text
Editing Observation
  measured media facts

Editing Learning Card
  measured facts + contextual interpretation

Editing Profile
  generalized corpus-level mechanisms

RenderPlan
  exact actions for one new clip

Renderer
  deterministic execution only
```

### 2.5 No exact style cloning

Do not learn one editor's:
- watermark/logo identity
- exact font/color branding
- proprietary intro/outro
- repeated proprietary graphics
- exact signature layout

Learn the function:
- emphasis
- hierarchy
- readability
- reaction focus
- context explanation
- pacing
- continuity

Holoclip branding is a separate configurable layer.

### 2.6 No premature production use

An Editing Profile has its own readiness gate.

An unready Editing Profile must not act as a strong production prior.

### 2.7 Every expensive stage is resumable

Observation and learning stages are content-addressed and cached.

A retry must not redo successful:
- media probe
- source/output alignment
- scene analysis
- OCR
- audio analysis
- learning-card construction
- pattern induction
- support validation
- render
- QA

## 3. Technology decisions

### 3.1 Authoritative media I/O and render: FFmpeg + ffprobe

Use FFmpeg/ffprobe as the authoritative layer for:
- stream/container metadata
- frame/time extraction
- audio extraction
- exact trim requests
- final encode
- filter graph execution
- subtitle burn-in
- audio mix
- preview/final render

Python/OpenCV decoder timestamps are not authoritative for measured media time.

FFmpeg version must be recorded in artifact provenance.

### 3.2 Shot/cut/fade observation: PySceneDetect 0.7.x

Use PySceneDetect as the default scene-boundary provider.

Initial detectors:
- AdaptiveDetector for fast cuts under motion
- ContentDetector as secondary evidence
- ThresholdDetector for fades/dips

Shot boundaries are also hard constraints for reframing interpolation: automated camera motion must never interpolate through an unrelated edit.

### 3.3 Frame geometry and source-output registration: OpenCV

Use OpenCV for image-space measurement, not semantic editorial judgment.

Primary operations:
- ORB/AKAZE feature detection
- robust feature matching
- RANSAC homography
- affine/crop/scale/translation fitting
- template/region comparison
- optical-flow/local tracking where necessary

The source/output mapping engine uses multiple evidence channels; OpenCV registration is one validator, not the sole temporal matcher.

### 3.4 Audio fine alignment and features: librosa + NumPy

Use FFmpeg to decode canonical PCM.

Use librosa/NumPy for:
- mel/MFCC features
- short-window similarity
- Dynamic Time Warping
- RMS/spectral features
- local timing refinement

The existing transcript alignment provides a coarse search window. Audio DTW refines within that bounded window rather than searching an entire archive.

### 3.5 Loudness measurement: pyloudnorm + FFmpeg measurements

Use BS.1770-style loudness measurement for analysis and validation.

Record:
- integrated LUFS
- short-term/loudness windows where useful
- peaks
- gain delta from source
- continuity across cuts

Final normalization remains a RenderPlan decision; the observer measures what the human edit did.

### 3.6 Japanese subtitle/text observation: zero-cost cloud OCR path

Use a replaceable TextObservationProvider, but do not require a local AI/OCR model for the initial production path.

Initial production strategy:
1. detect and track candidate text regions deterministically with OpenCV
2. crop representative text regions
3. send only those bounded crops to the zero-cost cloud vision provider
4. receive Japanese text plus confidence
5. keep region coordinates/timing authoritative from OpenCV tracking, not from the VLM

Primary text-reading provider:
- Gemini Developer API Free Tier, selected by verified capability

Free cloud fallback:
- NVIDIA NIM free image/VLM endpoint

This separates:
- **where/when text exists** -> deterministic local measurement
- **what the text says** -> free cloud vision inference

Do not OCR every video frame. Track stable regions and inspect only representative changes.

A dedicated OCR API/model may replace the recognition step later only if it is also verifiably zero-cost and wins the Holoclip Japanese-caption benchmark.

### 3.7 Speech/word timing: Groq Free Plan

Retain the existing transcript pipeline for coarse semantics.

Add a replaceable WordTimingProvider for selected training/runtime ranges.

Initial production provider:
- Groq Free Plan
- `whisper-large-v3`
- request both segment and word timestamps where needed

Use word timing to link:
- spoken word
- displayed caption
- emphasis interval

The system must use the free-plan quota only. A 429/quota exhaustion never upgrades to paid processing; the stage becomes BLOCKED_QUOTA and resumes after reset.

Word timing is not required to ingest a clip if unavailable; missing timing degrades the relevant observation dimension.

### 3.8 Subtitle render representation: ASS/SSA + libass through FFmpeg

Use ASS as the first production subtitle-plan target because it supports:
- styling
- outline/shadow
- position
- per-line overrides
- time-based emphasis

The renderer generates ASS from SubtitlePlan; libass/FFmpeg executes it.

Do not bind learned editing patterns to a specific font identity.

### 3.9 Advanced 2D motion graphics: Motion Canvas as an optional secondary renderer

FFmpeg remains the authoritative media/timeline/audio/final-encode engine, but it is not required to author every visual effect directly in a filter graph.

Use a second deterministic graphics backend for edits that are awkward or brittle in FFmpeg:
- animated emphasis text
- kinetic typography
- multi-element reaction layouts
- animated context cards
- arrows/callouts/highlights
- complex picture-in-picture compositions
- reusable branded motion components
- short vector/shape animations

Initial preferred backend: **Motion Canvas**.

Reasons:
- local execution
- TypeScript/code-defined deterministic animation
- real-time preview/editor for development
- image-sequence output
- official FFmpeg exporter
- MIT-licensed project
- suitable as a graphics layer rather than a traditional editor replacement

Motion Canvas is NOT the authoritative timeline/media engine and is NOT allowed to make editorial decisions.

Preferred composition flow:

```text
RenderPlan
   ├─ base media/audio/timeline actions -> FFmpeg
   ├─ ordinary dialogue subtitles      -> ASS/libass
   └─ advanced graphics events         -> Motion Canvas
                                              ↓
                                  transparent graphics artifact
                                              ↓
                         FFmpeg final composite / mux / encode
```

The advanced graphics backend must emit deterministic artifacts from RenderPlan data.
It must not receive free-form prompts at render time.

Internal alpha transport format (PNG sequence vs alpha-capable intermediate codec) is selected by benchmark for:
- determinism
- Windows reliability
- render speed
- storage cost
- alpha fidelity

Do not make Motion Canvas a core Python dependency. Run it as an isolated Node/TypeScript worker with a versioned JSON contract.

### 3.10 Renderer alternatives and promotion policy

Do not hard-code the architecture to one external graphics implementation.

Define:
- BaseMediaRenderer
- SubtitleRenderer
- MotionGraphicsRenderer
- FinalCompositor

Production default v1:
- BaseMediaRenderer = FFmpeg
- SubtitleRenderer = ASS/libass
- MotionGraphicsRenderer = Motion Canvas for advanced events only
- FinalCompositor = FFmpeg

Remotion is functionally capable of programmatic video/motion composition, but it is source-available under a proprietary license rather than OSI open source. It must not become a mandatory dependency for Holoclip. It may be benchmarked later as an optional provider only if its current license remains acceptable for the deployment.

VapourSynth may be benchmarked later for specialized frame-processing/filter pipelines, but it is not required for the first production renderer.

### 3.11 Zero-cost cloud AI policy

**Hard product constraint: paid AI/API usage is forbidden in the production path.**

Local AI inference is intentionally excluded from the initial provider set. Free deterministic local media processing such as FFmpeg, OpenCV, PySceneDetect, librosa and Motion Canvas remains allowed.

Do not define an "AI stack" as a fixed list of vendors. Maintain a **zero-cost candidate topology**, then let the capability router use only models that pass #99 for the relevant capability.

#### Native-video candidates

Benchmark at minimum:
- Google Gemini Developer API: strongest currently verified Free Tier video-capable model
- NVIDIA `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` Free Endpoint
- NVIDIA `nvidia/cosmos3-nano-reasoner` Free Endpoint

Important caveats:
- Gemini free entitlements are volatile and must be reverified.
- Nemotron Omni's model card states English-only language support, so Japanese dialogue semantics require separate evidence/provider support.
- Cosmos is physical-world oriented and must prove transfer quality on VTuber/game footage.

No one of these is permanently designated primary before the Holoclip benchmark.

#### Shot-aware multi-image / contact-sheet candidates

Benchmark at minimum:
- Groq Free Plan `qwen/qwen3.8-27b`
- NVIDIA Free Endpoint `z-ai/glm-5.3-flash`
- NVIDIA Free Endpoint `meta/llama-3.2-90b-vision-instruct`
- Cloudflare Workers AI Free `@cf/meta/llama-3.2-11b-vision-instruct`
- Mistral Studio Free-mode vision model, only after a live no-payment probe confirms zero-cost account behavior

This path is REQUIRED even when native video works. It prevents native-video API access from becoming a single point of failure.

#### Japanese text reading candidates

OpenCV owns region geometry/timing. Cloud models only read/interpret bounded crops.

Benchmark at minimum:
- currently free Gemini vision model
- Groq `qwen/qwen3.8-27b`
- Mistral Free-mode vision model
- NVIDIA `z-ai/glm-5.3-flash`
- NVIDIA Llama 3.2 Vision
- Cloudflare Llama 3.2 11B Vision

Do not promote one by vendor reputation; use Japanese caption benchmark results.

#### Strong editorial reasoning candidates

Benchmark at minimum:
- Groq Free Plan `openai/gpt-oss-120b`
- Groq Free Plan `qwen/qwen3.8-27b`
- NVIDIA Free Endpoint `z-ai/glm-5.3`
- NVIDIA Free Endpoint `z-ai/glm-5.3-flash`
- strongest currently verified free Gemini text model
- Cerebras current Free-tier text model
- Mistral Free-mode text model
- qualifying Cloudflare Workers-Free text model

Known Holoclip evidence already shows `z-ai/glm-5.3` can time out on bounded strong-model calls, so it must never be the sole production strong-reasoning route.

#### ASR / word timing candidates

Current preferred candidate:
- Groq Free Plan `whisper-large-v3`

Conditional:
- `whisper-large-v3-turbo` only for a capability/stage where Japanese quality benchmark clears the same required quality floor

ASR remains capability-routed and resumable. Free quota exhaustion never enables a paid tier.

#### Embedding candidates

Current preferred candidate:
- NVIDIA Free Endpoint `nvidia/nemotron-3-embed-1b`

Embedding artifacts must record model/version/dimension. Indexes must be rebuildable if a provider disappears. Vectors from different embedding spaces must never be mixed in one index.

#### Specialized zero-cost candidates

Benchmark narrowly where they add measurable value:
- NVIDIA Active Speaker Detection Free Endpoint for collab/multi-participant visible-speaker evidence
- NVIDIA Nemotron Omni audio/video observations as an independent multimodal signal
- NVIDIA grounding-capable vision models for focus-target hints

Specialized providers complement generic VLMs; they do not silently replace them.

#### Tertiary / diagnostic providers

- OpenRouter Free: small-volume cross-check and emergency reasoning only; free pool is too volatile/limited for bulk corpus work.
- Hugging Face Inference Providers: benchmark/dev only by default because free-user credits are too small for reliable bulk production.
- SambaNova: reject from automatic ZERO_COST_ONLY routing while its current plan requires payment method/credit purchase to make requests.

See `docs/zero-cost-provider-matrix-2026-10.md` for the dated research snapshot.

**Promotion authority:** issue #99 benchmark artifacts, consumed by #97 capability routing.

**Quality rule:** a fallback is valid only if it clears the SAME production quality floor for that capability. If no zero-cost route clears the floor, the stage BLOCKS/DEFERS. It never lowers quality merely to keep throughput moving.

### 3.12 Capability-first zero-cost provider mesh

Do not architect Holoclip around a vendor or a model family.

Architect around **capabilities**.

Required AI capability classes:

- `video_semantics_native`
- `video_semantics_multiframe`
- `image_semantics`
- `visual_grounding_hint`
- `japanese_text_reading`
- `asr_segment_timestamps`
- `asr_word_timestamps`
- `text_reasoning_strong`
- `text_reasoning_fast`
- `structured_json`
- `embedding`
- `semantic_render_qa`

Each production stage declares:
- required capabilities
- optional capabilities
- minimum benchmark score
- acceptable latency class
- maximum quota cost
- privacy/data-use class
- whether multi-provider confirmation is required

The router selects from all currently verified zero-cost providers.

A single stage may be fulfilled through different execution modes:

#### Mode A — native video
A provider accepts bounded video directly.

Use when:
- free
- benchmarked
- quota available
- native video improves quality enough to justify quota usage

#### Mode B — multi-image / shot sequence
Use deterministic PySceneDetect shot segmentation and frame sampling, then submit:
- representative frames
- denser reaction/payoff frames
- transcript context
- measured event IDs

This is the primary architecture-level fallback and must work even if no free native-video API exists.

#### Mode C — contact-sheet semantic inspection
For providers with limited image count:
- compose measured frames into deterministic contact sheets
- annotate each tile with frame/shot ID outside the semantic image area
- provide transcript/event index separately

This is lower fidelity but highly portable across free image-VLM APIs.

#### Mode D — evidence fusion without VLM video support
For nonvisual or quota-constrained stages combine:
- transcript
- audio events
- OCR/text observations
- OpenCV geometry
- scene boundaries
- individual image-VLM observations

This mode cannot claim visually unsupported facts, but the pipeline can continue for nonvisual decisions.

Native video is therefore an optimization and quality opportunity, not a single point of failure.

### 3.13 Candidate provider pool

The provider registry tracks a broad zero-cost pool. Current research snapshot and official references live in `docs/zero-cost-provider-matrix-2026-10.md`.

Current categories:

- **Gemini Developer API Free Tier** — native video, image/multimodal semantics, Japanese visual understanding; volatile entitlement, verify before use.
- **NVIDIA API Catalog Free Endpoints** — independent native-video (Nemotron Omni, Cosmos), image-VLM/grounding, strong reasoning, embeddings, and specialized Active Speaker Detection.
- **Groq Free Plan** — primary ASR candidate plus Qwen 3.8 multimodal image reasoning/OCR and GPT-OSS 120B text reasoning.
- **Cloudflare Workers AI Free** — independent image-VLM/text route under the Workers Free neuron allocation; reject any model requiring Workers Paid.
- **Mistral Studio Free mode** — independent vision/text candidate, pending live verification that the actual account/API path remains no-payment.
- **Cerebras Free** — text-only fast reasoning / independent second opinion candidate.
- **OpenRouter Free** — tertiary/cross-check only because request allowance and free-model pool are volatile.
- **Hugging Face Inference Providers** — benchmark-only by default because free credits are too small for bulk production.
- **SambaNova** — rejected from automatic ZERO_COST_ONLY routing under the current payment/credit requirement.
- **GitHub/Copilot interactive model surfaces** — not production providers unless a stable automation-suitable zero-cost API contract is separately verified.

Provider status is dynamic. A model may be qualified for one capability and rejected for another.

### 3.14 Provider selection policy

For each stage:

1. Filter providers by required capability.
2. Reject anything not currently verified zero-cost.
3. Reject provider/data-policy combinations incompatible with the media.
4. Reject providers below the benchmark quality floor.
5. Reject providers whose quota ledger cannot safely complete the requested unit.
6. Rank remaining providers by:
   - benchmark quality
   - reliability/success rate
   - hallucination/error rate
   - quota efficiency
   - latency
7. Execute the best eligible provider/mode.
8. Persist exact provider/model/mode/version in artifact provenance.

Never encode "Gemini first" or "NVIDIA second" directly into business logic. Those are registry scores/policies, not architecture.

### 3.15 Confidence, disagreement, and ensemble policy

For ordinary low-risk semantic observations:
- one benchmarked provider may be sufficient.

For high-impact ambiguous decisions:
- use a second independent free provider when quota permits.

Examples:
- visual target determines a strong crop/zoom
- interpretation changes whether a payoff is retained
- semantic QA claims subtitle meaning is wrong
- a context card asserts a factual relationship/event

Persist:
- provider A result/confidence
- provider B result/confidence
- agreement/disagreement
- reconciliation decision

If providers disagree materially:
- prefer measured deterministic evidence
- reduce confidence
- suppress aggressive editing action
- optionally defer until free quota allows another check

Do not ensemble every event; reserve it for high-impact ambiguity.

### 3.16 Provider qualification benchmark

Provider qualification is separate from model marketing benchmarks.

Maintain a Holoclip-specific held-out benchmark covering:
- Japanese VTuber conversation
- game UI
- Live2D/avatar reactions
- multiple speakers/avatars
- fast scene changes
- visually small UI events
- Japanese stylized subtitles
- source-vs-finished edit comparison
- comedy timing
- serious/emotional clips

Score by capability:
- semantic event accuracy
- Japanese comprehension
- reaction classification
- UI/object recognition
- text reading accuracy
- hallucination rate
- structured-output validity
- consistency
- quota consumption
- latency
- provider failure rate

A provider can be qualified for one capability and rejected for another.

### 3.12 Zero-cost enforcement

"Free tier" is not merely documentation; it is a runtime policy.

Implement a provider registry with:
- provider
- model ID
- capability
- endpoint class
- cost class: FREE_TIER / FREE_ENDPOINT
- verified_at date
- verification URL/reference
- known quota units
- whether billing must remain disabled
- data-use/privacy note

Runtime rules:
- no provider/model without current zero-cost verification may run
- no paid partner/serverless endpoint may be configured as automatic fallback
- no API request is retried by enabling billing
- quota exhaustion => BLOCKED_QUOTA, persist progress, resume later
- authentication/availability failure => try only another verified zero-cost provider
- if no zero-cost provider is available => stop that stage safely
- all expensive stages are cached to avoid wasting free quota
- maintain a daily quota ledger per provider/model
- use conservative internal budgets below published free limits
- re-check provider/free-tier status periodically and before production promotion

Gemini free-tier requests are currently eligible for product-improvement use by Google. Therefore:
- only public/source media intended for this workflow should be sent through the free path
- secrets/private user material must not be uploaded by default
- use `store=false` where supported
- delete temporary uploaded files after successful processing when the API permits it

### 3.17 Zero-cost quota strategy

Gemini free-tier video:
- prefer short selected clips instead of full multi-hour archives
- exploit existing Editorial/Timeline narrowing before VLM upload
- never assume Gemini App subscription entitlements equal Gemini Developer API entitlements
- re-verify free API model access before each production learning cycle
- if only a Flash-Lite-class video model remains free, use it and benchmark quality; do not pay for Flash/Pro
- Files API artifacts are temporary and should be deleted after use
- one successful semantic observation is cached and never re-requested for the same fingerprint/model/prompt

Groq free-plan ASR:
- chunk audio to remain within free upload constraints
- observe free-plan RPM/RPD/audio limits
- preserve completed chunks and resume after quota reset

NVIDIA free endpoints:
- heavy calls remain sequential unless the endpoint explicitly permits more
- timeout/rate-limit state is persisted
- never redirect to a paid partner endpoint

The scheduler optimizes for **eventual completion at zero monetary cost**, not fastest wall-clock completion.

### 3.18 Semantic video understanding: VLM-backed VideoUnderstandingProvider

A perfect editing system must understand not only how pixels changed, but what is happening in the video.

OpenCV, shot detection and OCR measure:
- where a crop/zoom happened
- where a cut occurred
- what text was visible

They do not reliably answer:
- which visible subject is currently important
- whether the payoff is a facial/avatar reaction or an in-game event
- which game/UI region carries the meaning
- whether a visual gag occurred without a strong transcript signal
- whether several visible participants react differently
- whether a context overlay points at an important object/state
- whether a zoom emphasized the speaker, the game, an item, or a reaction

Introduce a replaceable **VideoUnderstandingProvider** backed by a VLM.

The provider is a required semantic observation layer for production Editing Readiness, but no single VLM implementation is permanently mandatory.

Supported provider styles:
- native video VLM over bounded short clips
- multi-image VLM over shot-aware sampled frames/contact sheets

The provider must consume measured IDs and bounded media windows. It returns semantic observations linked to:
- source asset ID
- shot IDs
- transcript segment IDs
- measured start/end windows
- sampled frame IDs

It must NOT invent authoritative timestamps.

Suggested semantic observation schema:
- visible subjects / participant roles
- dominant visual focus
- meaningful screen regions
- facial/avatar reaction description
- gameplay/UI event description
- object/item/context references
- scene purpose
- visual surprise/change
- multi-participant interaction
- recommended focus target candidates
- confidence
- evidence frame/shot IDs
- uncertainty/unknown

### 3.19 VLM sampling policy

Do not send entire long-form streams frame-by-frame to a VLM.

Use coarse-to-fine, event-driven visual understanding:

1. Editorial discovery narrows source ranges.
2. PySceneDetect establishes shot boundaries.
3. Deterministic sampling selects representative frames per shot.
4. OCR/OpenCV/audio/transcript events mark regions requiring denser inspection.
5. VideoUnderstandingProvider examines:
   - representative shot frames
   - short clips around reactions/payoffs/cuts
   - denser frames only when motion or semantics are ambiguous.
6. Results are cached by media fingerprint, measured window, frame IDs, provider/model/version and prompt version.

This keeps visual semantics affordable and resumable.

### 3.20 Runtime VLM role

VLM understanding is used both during training and production.

Training:
- explain what a measured human edit visually emphasized
- distinguish face/avatar focus from gameplay/UI/object focus
- link learned visual editing mechanisms to semantic triggers

Runtime on an unseen source:
- inspect only the selected TimelinePlan ranges
- identify the visual subject/region relevant to each narrative beat
- provide candidates to ReframePlan/OverlayPlan generation
- validate whether a learned visual-emphasis pattern applies

If VLM evidence is unavailable or low confidence:
- do not fabricate semantic crop/focus targets
- fall back to safe source framing or deterministic measured layout rules
- mark advanced visual actions unavailable

### 3.21 VisualTargetProvider

Keep **VisualTargetProvider** distinct from general VLM understanding.

VideoUnderstandingProvider answers:
> What is happening and what appears semantically important?

VisualTargetProvider answers:
> Given that semantic target, where exactly is it in image coordinates over time?

VisualTargetProvider may use:
- tracking
- detection
- segmentation
- template matching
- model-specific region grounding

The VLM must not be treated as a pixel-accurate tracker unless benchmark evidence supports it.

### 3.22 Heavy ML provider policy

Local AI inference is not part of the initial candidate set. Benchmark only external zero-cost providers unless this product constraint is explicitly changed later.

Do not hard-code the architecture to:
- a community anime-face detector
- a specific YOLO checkpoint
- a specific VLM
- an audio-event classifier

Introduce interfaces for:
- VideoUnderstandingProvider
- VisualTargetProvider
- AudioEventProvider
- OCRProvider

Provider promotion is benchmark-based.

For VLM providers benchmark at minimum:
- semantic event accuracy on held-out human clips
- focus-target correctness
- hallucination rate
- shot/beat grounding accuracy
- Japanese context comprehension
- runtime
- API/GPU/VRAM cost
- reproducibility/caching behavior

Only providers that pass the Holoclip visual-understanding benchmark may become production defaults.

## 4. Training asset contract

For a trusted human clip used in Editing Learning, preserve:

- finished human video or a locally analyzable derived copy
- clip audio
- original source identity
- aligned source ranges
- source video for aligned ranges at sufficient quality
- source audio
- source transcript
- clip transcript
- alignment artifact
- media fingerprints
- extraction policy/provenance

Do not discard finished video after audio/transcript extraction.

The source video does not need to be downloaded in full if only aligned ranges are needed.

## 5. Observation pipeline

### 5.1 Media probe

Create MediaProbeArtifact for source and finished clip:
- codec
- width/height
- fps/time base
- duration
- audio streams
- sample rate/channels
- color metadata
- rotation/display metadata

### 5.2 Coarse temporal map

Seed from existing human clip/source alignment.

Represent output intervals as candidate mappings to:
- source asset
- source time range
- alignment confidence

### 5.3 Fine audio temporal map

Within each coarse alignment window:
- extract canonical PCM
- derive bounded audio features
- refine source/output mapping with similarity + DTW

This produces a piecewise OutputSourceTimeMap.

The local derivative of the time map is informative:
- approximately 1.0 => normal playback
- approximately 0 => freeze/held frame candidate
- stable != 1 => speed change candidate
- discontinuity => cut/source jump

### 5.4 Visual registration

At sampled output points:
- predict source time from temporal map
- extract corresponding source frame
- register output/source image
- estimate crop/scale/translation/homography
- record residual/unexplained regions

Use temporal and visual evidence together.

If they disagree beyond a threshold, mark the region uncertain.

### 5.5 Scene and transition observation

Run shot/fade detectors on finished output.

Classify candidate transition families from measured evidence:
- hard cut
- jump cut
- fade/dip
- crossfade candidate
- source switch
- unknown custom transition

Do not infer decorative transition type without sufficient evidence.

### 5.6 Subtitle/text observation

Track text regions across frames.

For each text event:
- first/last visible time
- bounding polygon
- OCR text
- confidence
- approximate fill/outline/shadow/color statistics
- approximate glyph/box size
- alignment/position role
- motion/static state

Link OCR text to transcript words/segments where confidence permits.

Classify text role separately:
- speech caption
- emphasis
- reaction text
- context/explanation
- identity/source label
- title/hook card
- unknown

Role classification can use an LLM only after measured text/time/region evidence exists.

### 5.7 Visual transform observation

Derive:
- crop rectangle
- zoom/punch-in scale
- pan/translation
- stable layout changes
- inserted frame/image regions
- picture-in-picture candidates
- freeze candidates
- source switches

A transform crossing a shot boundary must be split.

### 5.8 Semantic visual observation with VLM

For each relevant measured shot/window, produce VLM semantic observations.

Training compares:
- what the source visually contained
- what the finished edit emphasized
- which narrative beat the emphasis served

Examples:
- measured punch-in + VLM identifies surprised avatar reaction
- measured crop + VLM identifies important game UI/result region
- source switch + VLM identifies another participant's reaction
- context card + VLM identifies the object/event being explained

The VLM result never replaces the measured transform. It explains the semantic trigger/purpose.

### 5.9 Audio transformation observation

Measure source vs output:
- loudness change
- peak handling
- fades
- cut continuity
- silence retained/removed
- residual audio after level/time compensation

Residual audio may indicate:
- added SFX
- added BGM
- added voice/other sound
- source mismatch

Do not classify residual type as SFX/BGM without a reliable provider; unknown_added_audio is valid.

### 5.10 Narrative beat linkage

Link measured editing events to:
- hook
- setup
- context
- development
- contrast
- reaction
- payoff
- epilogue

This is where interpretation enters.

Example:
- measured zoom event at 43.2-43.8 s
- linked payoff/reaction beat
- interpretation: reaction emphasis

The event remains measured even if interpretation later changes.

## 6. Editing Learning Card

Each trusted analyzable example produces one persisted card with:

- example ID
- finished clip asset ID
- source asset IDs
- corpus/editor provenance
- media probes
- OutputSourceTimeMap
- cut/source-switch events
- pace/compression measurements
- pause removal/preservation observations
- subtitle events
- visual transform events
- semantic visual/VLM observations
- visual target observations where available
- overlay events
- audio transform events
- transition events
- narrative beat linkage
- observation coverage by dimension
- unknown/error dimensions
- extractor versions
- fingerprints

The card is deterministic from observation artifacts plus explicitly versioned interpretation prompts.

## 7. Corpus-level Editing Profile

Do not retrieve one similar edited clip and call that learning.

Induce reusable EditingPatterns from the cards.

EditingPattern fields:

- stable ID
- name
- purpose
- trigger conditions
- required evidence
- action family
- timing strategy
- intensity/range
- anti-patterns
- incompatible conditions
- supported narrative beats
- supporting example IDs
- supporting source stream IDs
- supporting distinct clip-editor IDs
- confidence
- maturity
- observation dimensions required

Pattern examples are discovered from data, not a fixed taxonomy.

## 8. Pattern maturity

A pattern is CORE only when code verifies at least:

- 3 trusted editing cards
- 2 distinct source streams
- 2 distinct human clip editors
- valid support assessment
- all required observation dimensions present

Otherwise it is EMERGING.

LLM output cannot self-promote maturity.

## 9. Editing Readiness v1

Editing Readiness is separate from Editorial Readiness.

Initial conservative gate:

### Corpus diversity
- >= 40 trusted clips with analyzable finished video
- >= 20 distinct source streams
- >= 8 distinct human clip editors

### Observation quality
- median source/output temporal-map coverage >= 90%
- >= 80% of cards have temporal-map coverage >= 80%
- >= 80% of caption-heavy cards have usable text observation
- >= 80% of cards have usable visual-transform observation
- >= 80% of visually relevant cards have grounded VLM semantic observation on their key beats
- all CORE patterns whose trigger/action depends on visual semantics have validated VLM support
- audio measurement available on >= 90% of cards

### Learned mechanism maturity
- >= 8 CORE editing patterns
- CORE patterns cover >= 80% of analyzable cards
- CORE patterns span >= 4 action families
- pacing and subtitle families must both be represented
- visual emphasis/reframe must be represented when the corpus contains sufficient visual-edit evidence

These are engineering floors, not proof of perfection.

The likely production corpus should grow beyond 40 clips; 50-100+ representative clips is expected.

## 10. Style families

Do not average all editors into one style.

Learn:
1. universal mechanisms shared across editors
2. context-dependent editing families
3. Holoclip-owned render profiles

Possible context axes:
- comedy/reaction
- serious/storytelling
- gaming
- multi-POV/event
- short-form
- long-form

A new clip selects a treatment based on story/evidence.

It does not randomly combine all learned techniques.

## 11. RenderPlan contract

RenderPlan is a complete execution specification.

No unresolved editorial decision is allowed inside the renderer.

### TimelinePlan
- exact source ranges
- output order
- speed/freeze operations
- retained pause annotations
- source asset IDs
- measured segment IDs

### SubtitlePlan
For each caption:
- transcript/word linkage
- final caption text
- start/end
- line breaks
- position role
- style role
- emphasis spans
- speaker role
- provenance

### ReframePlan
For each event:
- source segment
- start/end
- crop rectangle or focus target
- scale
- translation
- interpolation/keyframes
- purpose
- provenance

### OverlayPlan
- content
- role
- start/end
- layout role
- provenance

### AudioPlan
- source gain
- target loudness
- fades
- ducking
- retained silence decisions
- optional SFX/BGM cue only when justified
- provenance

### TransitionPlan
- transition family
- start/end
- duration
- purpose
- provenance

### RenderProfile
Holoclip-owned branding/config:
- base typography
- allowed emphasis palette
- safe margins
- output resolution
- encode profile
- channel watermark if desired

Learned mechanisms may select semantic style roles; branding resolves roles to actual visual identity.

## 12. Renderer

Rendering is deterministic and split into composable backends.

### 12.1 Base media renderer — FFmpeg

FFmpeg remains authoritative for:
- trim/extract
- concat
- multi-source assembly
- speed/freeze
- ordinary crop/scale/pan
- audio mix
- transitions that are naturally expressed as media filters
- final encode

### 12.2 Subtitle renderer — ASS/libass

ASS/libass handles the normal high-volume dialogue-caption path:
- text
- outline/shadow
- placement
- semantic style roles
- emphasis spans
- timing

Do not send every caption through a browser graphics renderer.

### 12.3 Motion graphics renderer — Motion Canvas

Motion Canvas handles RenderPlan events that need a true 2D animation/composition layer:
- kinetic typography
- animated punchline/reaction treatments
- callouts/arrows/boxes
- context cards
- multi-element layouts
- animated PIP frames
- branded motion components

The worker receives only validated JSON/asset references and emits a deterministic transparent graphics artifact.

### 12.4 Final compositor — FFmpeg

FFmpeg composites:
- base rendered media
- ASS captions
- motion-graphics layers
- final audio

and performs the final encode.

The renderer stack never decides what is interesting or which effect should be used. Those decisions are complete in RenderPlan before any renderer starts.

A simple clip should be able to bypass Motion Canvas entirely. Complexity is paid only when the plan needs it.

## 13. Render QA

Every preview/final render receives automated validation.

### Structural QA
- output duration matches plan
- expected segments present
- no duplicate/missing plan segments
- source attribution/provenance complete

### Video QA
- no black/blank corruption
- no accidental frozen region
- no invalid dimensions
- no crop outside source bounds
- no transform crossing forbidden shot boundary
- no unexpected duplicate frames above threshold

### Subtitle QA
- no missing glyphs
- no overflow outside safe region
- reading speed within configured bounds
- no pathological caption density
- no unintended overlapping caption layers

### Audio QA
- integrated loudness in target range
- no clipping beyond threshold
- no unexpected silent gaps
- cut-boundary discontinuity check

### Semantic QA
Later strong-model pass using measured plan/evidence:
- setup remains understandable
- payoff remains present
- context overlay is factually supported
- caption meaning does not invert transcript
- multi-source chronology is coherent

Repairable QA failures trigger plan revision and re-render.

## 14. Benchmark and promotion policy

Issue #99 owns the real-media provider qualification benchmark. No provider becomes production-default because it "looks good" on one clip.

Maintain a benchmark corpus with diverse:
- editors
- source streams
- genres
- resolutions
- subtitle styles
- editing density

For each observation provider measure:
- success coverage
- confidence calibration
- boundary error
- false positive rate where reference can be generated
- runtime
- CPU/GPU/VRAM cost
- failure modes

A provider must pass benchmark thresholds before promotion.

## 15. Dependency isolation

Keep the main package lean.

Recommended architecture:

```text
holoclip core
  pydantic/httpx/typer
        |
        +-- external FFmpeg/ffprobe
        |
        +-- deterministic editing-analysis environment
              numpy
              opencv-python-headless
              scenedetect
              librosa
              soundfile
              pyloudnorm
        |
        +-- zero-cost cloud AI providers
              Gemini Developer API Free Tier
              NVIDIA API Catalog Free Endpoints
              Groq Free Plan
        |
        +-- isolated motion-graphics worker
              Node.js
              TypeScript
              Motion Canvas
```

Heavy OCR/CV dependencies must not make ordinary transcript candidate discovery fail to install.

Provider outputs are JSON/Pydantic contracts and can be produced by subprocess workers if dependency conflicts require isolation on Windows.

## 16. Resource policy for the current Windows environment

- Local AI inference is excluded from the initial production design.
- FFmpeg/OpenCV/PySceneDetect/audio measurement run locally as deterministic non-AI processing.
- VLM/LLM/embedding/ASR inference uses verified zero-cost cloud endpoints.
- The local RTX3060 is therefore not required for the AI inference path and remains available for the user's other workloads.
- Heavy training-corpus analysis is resumable and quota-aware.
- Never require all frames to be processed at full resolution.
- Coarse-to-fine analysis:
  1. alignment window
  2. low-rate frame sampling
  3. refine only around detected edits/text/events
- Cache every expensive observation artifact.

## 17. Strict phase order

Parent issue #80 is split into ordered gates.

### Gate A — Training asset preservation
Finished human-video media/evidence is retained and reproducibly addressable.

### Gate B — Source/output temporal map
Measured source↔finished time mapping works with confidence and unknown states.

### Gate C — Visual/text/audio/semantic-video observation
Independent observation providers produce provenance-backed events, including grounded VLM semantic observations for visually relevant beats.

### Gate D — Editing Learning Cards
One trusted example can be fully represented without LLM-invented media facts.

### Gate E — Editing Profile
Corpus induction, support validation, maturity, style families, readiness.

### Gate F — RenderPlan
Learned editing mechanisms compile into a validated deterministic plan.

### Gate G — Renderer
Plan executes reproducibly through the hybrid renderer stack:
- FFmpeg base media/final compositor
- ASS/libass normal subtitles
- Motion Canvas advanced 2D graphics when requested by the plan

### Gate H — QA/repair
Common edit/render failures are detected and repaired.

### Gate I — Unseen preview validation
A ready Editorial + Editing Profile produces a deliberately edited unseen preview.

Do not start production publishing until Gate I passes.

## 18. Completion definition

"Can cut and concatenate video" is explicitly NOT a completion criterion.

Editing Intelligence v1 is complete only when:

- finished human clips are used as transformation supervision
- temporal/visual/text/audio editing facts are measured
- generalized editing mechanisms are induced with real support provenance
- readiness blocks immature learning
- an unseen clip can receive content-appropriate learned editing
- RenderPlan is deterministic and inspectable
- renderer output passes automatic QA
- failures resume from the relevant stage
- the system does not require the operator to annotate every edit
- Ruff/Pytest and real-media validation pass

Only then is the publishing layer allowed to become the primary next milestone.

## 19. External technology references

Current zero-cost provider research snapshot: `docs/zero-cost-provider-matrix-2026-10.md`.

- FFmpeg / ffprobe: authoritative media execution/probing layer.
- PySceneDetect 0.7.x: scene/cut/fade observation.
- OpenCV: feature matching, homography, tracking and geometric measurement.
- Gemini Developer API Free Tier: capability-routed video/text vision provider; current baseline is `gemini-3.5-flash-lite`, subject to runtime free-status verification.
- NVIDIA NIM Free Endpoints: free image-VLM/text reasoning fallback and strong/fast reasoning providers.
- NVIDIA nemotron-3-embed-1b Free Endpoint: structural embeddings.
- Groq Free Plan whisper-large-v3: ASR and word timing.
- VLM-backed VideoUnderstandingProvider: semantic understanding of bounded video/shot windows, using zero-cost cloud providers only.
- librosa: bounded audio feature extraction and DTW refinement.
- pyloudnorm: BS.1770-style loudness measurement.
- ASS/SSA + libass: rich deterministic subtitle render representation.
- Motion Canvas: optional deterministic advanced 2D motion-graphics backend; FFmpeg remains authoritative for final media composition/encode.
