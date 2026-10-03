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

### 3.6 Japanese subtitle/text observation: PaddleOCR provider

Use a replaceable TextObservationProvider.

Initial default benchmark target:
- PaddleOCR 3.x
- PP-OCRv5 Japanese recognition

OCR is isolated from the core package because it is a heavy dependency.

The provider returns:
- text
- confidence
- bounding polygon
- frame/time range
- orientation

Do not OCR every video frame. Detect/track stable text regions and OCR representative frames.

PP-OCRv6 may replace the initial recognizer only after a corpus benchmark demonstrates equal or better Japanese clip-caption performance.

### 3.7 Speech/word timing

Retain the existing transcript pipeline for coarse semantics.

Add a replaceable WordTimingProvider for selected training/runtime ranges.

Preferred local option:
- Whisper large-v3 word timestamps / compatible faster implementation

Use word timing to link:
- spoken word
- displayed caption
- emphasis interval

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

### 3.11 Heavy ML providers remain optional

Do not make the first Editing Intelligence implementation depend on:
- a community anime-face detector
- a specific YOLO checkpoint
- a video-language model
- an audio-event classifier

Introduce interfaces for:
- VisualTargetProvider
- AudioEventProvider
- OCRProvider

Only promote a provider to default after a measured benchmark on the Holoclip corpus.

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

### 5.8 Audio transformation observation

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

### 5.9 Narrative beat linkage

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

No provider becomes production-default because it "looks good" on one clip.

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
        +-- optional editing-analysis environment
              numpy
              opencv-python-headless
              scenedetect
              librosa
              soundfile
              pyloudnorm
        |
        +-- optional OCR provider environment
              PaddleOCR/PaddlePaddle
        |
        +-- isolated motion-graphics worker
              Node.js
              TypeScript
              Motion Canvas
```

Heavy OCR/CV dependencies must not make ordinary transcript candidate discovery fail to install.

Provider outputs are JSON/Pydantic contracts and can be produced by subprocess workers if dependency conflicts require isolation on Windows.

## 16. Resource policy for the current Windows/RTX3060 environment

- FFmpeg/OpenCV/PySceneDetect/audio measurement should run CPU-first.
- GPU is optional for OCR/word timing and should not be a hard dependency.
- Heavy training-corpus analysis is resumable and may run when GPU is available.
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

### Gate C — Visual/text/audio observation
Independent observation providers produce provenance-backed events.

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

- FFmpeg / ffprobe: authoritative media execution/probing layer.
- PySceneDetect 0.7.x: scene/cut/fade observation.
- OpenCV: feature matching, homography, tracking and geometric measurement.
- PaddleOCR 3.x / Japanese-capable PP-OCR model: subtitle/text observation.
- Whisper large-v3 compatible word timing: speech/caption linkage.
- librosa: bounded audio feature extraction and DTW refinement.
- pyloudnorm: BS.1770-style loudness measurement.
- ASS/SSA + libass: rich deterministic subtitle render representation.
- Motion Canvas: optional deterministic advanced 2D motion-graphics backend; FFmpeg remains authoritative for final media composition/encode.
