# Autonomous evidence and unattended learning policy

**Status:** Normative workflow policy; rollout is incremental, not a statement of
current production readiness. Parent issues: #3, #78, #80, #99, #107, #109.

## Product contract: operator effort is an exception, not a prerequisite

Holoclip should discover eligible public Hololive human edits and their public original
sources itself. The operator must not be required to supply clip URLs, timestamps,
transcripts, full-corpus ratings, gold labels or repeated quota-reset commands. A
one-time environment setup (credentials, local folders, permitted sources) and rare
policy/identity decisions may still be needed. Where reliable evidence is not
obtainable automatically, stop at **UNVERIFIED** instead of asking the operator to
invent evidence or silently lowering the quality floor.

The target is evidence-backed **corpus-level editorial pattern learning**, not
necessarily LLM weight fine-tuning. Model fine-tuning can be evaluated later but
does not replace original-stream/video verification, dataset splits or calibrated QA.

## Source-pattern collection (2026-10-09 increment)

`examples-collect-source-patterns` is a bounded, restartable batch runner for
the already implemented five constructed video patterns. It runs metadata-only
public clip discovery (optional), then the existing policy-checked yt-dlp
source/video cross-check across persisted round-robin pending clip keys. Each
cycle reports attempted clip keys, latest acquisition/decoder/policy outcomes
and the **change in globally deduplicated** measured source samples. Results
are saved by the established SHA-bound control histories in corpus state.

Round-robin scheduling prevents one pending clip from occupying every cycle,
but it **does not** guarantee source diversity: several clips can still point
to one original. The report must distinguish attempts, independently counted
source-video samples and distinct candidate source IDs, and treat all absent
measurement denominators as null. Source IDs discovered by metadata are
unverified candidates; no source identity, human cut precision/recall,
original-stream false-positive rate, training gold or production qualification
may be inferred. `ready=false` remains mandatory. Real collection must actually
run against public media before any observed source result is claimed.

## Acquisition and safe data allocation

1. Discover public ended clips by metadata; check source/channel/range policy and
   unambiguous source resolution **before** media acquisition.
2. Deduplicate canonical clip URLs. Group *all* clips sharing an original source stream;
   different clips of that source must never populate opposing training/holdout sets.
3. Respect existing legacy corpus membership: if a stream occurs in any existing
   human-example record (even review-only), treat that stream as learning history,
   not as new held-out benchmark evidence.
4. For newly discovered eligible streams, make a deterministic source-level holdout
   reservation using the versioned SHA-256 rule (one bucket in five); when there is
   no learning stream yet, bootstrap learning before reserving. Persist reservations
   in `data/examples/corpus_growth.json`. A prior reservation always wins over a
   future discovery of another clip from that source; a conflict with external
   training writes becomes `split_conflict`, not an automatic reassignment.
5. Reserved items are excluded from `ingest_human_example_from_urls`, the retrieval
   index and the Editorial Profile. Their *finished clips* may be separately
   acquired for unreviewed measurements only, subject to current source/policy
   checks. Inaccessible or ambiguous candidates remain quarantined; source edit
   mapping and event-level leakage verification remain separate future gates.
6. Source-stream separation is necessary but **not sufficient** for multi-POV/cross-stream
   compilations of the same event. The future split finalizer must build
   event/participant/overlap groups and reject unresolved cross-source contamination
   before declaring a measured case `held_out=true`. Neither a reserved stream nor an
   unchanged public URL proves independence.

## Evidence confidence classes

| Evidence | Eligible use | Not eligible for |
| --- | --- | --- |
| Deterministic FFmpeg/scene/audio measurements, verified hashes, ASR/source alignment with explicit uncertainty | Reference for the directly observed physical/temporal facts | A subjective verdict such as why an event is funny |
| Existing human editor's source-to-finished choices | Weak positive / contextual negative learning signal | Conclusive global ground truth for taste or funny-score |
| Multi-model independent vendor verdict / weak grader | Triage, disagreement discovery, diagnostic ranking | `HUMAN_GOLD`, `HUMAN_REVIEWED_TEACHER`, production promotion |
| Independently verified review/gold bound to immutable source/media/report fingerprints | Promotion candidate within exactly the reviewed capability and dimension | Other untested models, capabilities, modes or sources |

**Never** synthesize a `HUMAN_GOLD`/`HUMAN_REVIEWED_TEACHER` flag merely
because several AI judges agree. Distinct failure modes, independent sources and
calibration are needed. An explicit external verified gold corpus can be used if
provenance, license, applicability and independence are checked; do not claim an
AI-generated label was human reviewed.

## Unattended validation and advancement gates

- Keep a **learning** corpus, **reserved holdout** pool, **measured benchmark**
  and **reviewed promotion gold** as different immutable/traceable layers.
- Measure evidence automatically first: real input hashes, crop/shot/audio facts,
  transcript provenance, stable IDs, original event groups, source/finished edits.
- Generate blind packs and independent *AUTO_WEAK_TEACHER* grades under
  `ZERO_COST_ONLY`; route unresolved disagreement to a bounded quarantine queue.
  Unresolved does not mean silently promoted or silently scored zero.
- Compare models/modes using identical held-out measured windows and actual
  independent repeat requests. Preserve all timeouts, invalid JSON, HTTP errors and
  quota reservations in reliability denominators as specified by #112.
- Only validated gold and full model/capability/mode coverage under the existing
  production floors can promote a route. Current automatic evidence cannot satisfy
  that gold requirement on its own.
- If free account proof, current model evidence or data-policy approval is missing,
  block before inference. No paid fallback, no privileged source scraping, no
  optimistic automatic renewal of billing-disabled claims.
- Persist resumes and checkpoints so the operator is not required to rerun failed
  pages or wait for daily free resets; periodic scheduling is a separate deployment
  responsibility. Stale artifacts remain diagnostics, not authorization.

## Implemented in this increment

`examples-grow-corpus` now reserves eligible previously unseen source streams
**before** ingestion, persisting the source IDs across subsequent searches. Different
human clips citing the same source share the allocation. Existing example streams
remain on the learning side. Reservation conflicts are quarantined.

To inspect the reservations without network access, credentials or media download:

```powershell
uv run holoclip examples-grow-corpus --discover-only
uv run holoclip examples-holdout-status
```

`examples-holdout-status` saves a machine-readable
`data/provider-benchmark/holdout-reservations.json` with
`qualification_allowed=false`, `reviewed_gold_available=false`,
reserved stream IDs and conflicts. It is an inventory, **not** a benchmark manifest
or independently reviewed labels.

## Increment 2: guarded acquisition and deterministic measurements (#118)

`uv run holoclip examples-prepare-holdout` now consumes previously reserved
public/ended clip metadata from `data/examples/corpus_growth.json`. It requires
**no model API keys, manually supplied video URLs or labels**. For up to three
reserved clips per invocation, the command:

- Reloads all known learning streams and canonical clip identities so new
  contamination blocks work **before network probing**.
- Rechecks clip and original-source public/ended status, exact source identity,
  unambiguous source-link resolution and both parties' current LEARN policy.
  Scoped no-clip restrictions quarantine because precise original-source ranges
  are not independently verified.
- Acquires the **finished human edit** using the existing managed yt-dlp
  `EDITING_VIDEO` profile, preserving the original discovery/learning store.
- Calls authoritative ffprobe/FFmpeg/PySceneDetect deterministic measurement on
  at most three 5–120-second video windows per acquired clip (default 20 seconds).
- Saves complete media fingerprints, measurement manifest paths, source and
  policy provenance in a separate atomic checkpoint
  `data/provider-benchmark/holdout/holdout-preparation.json`. Verified cached
  media/frames/sheets are reused on rerun; corrupt or missing bytes cause
  deterministic re-measurement rather than silent acceptance.
- Keeps failed metadata, policy and measurements explicitly quarantined or
  retryable. It neither calls paid/free cloud inference nor adds any examples
  to the training index.

Run after metadata discovery:

```powershell
uv run holoclip examples-grow-corpus --discover-only
uv run holoclip examples-holdout-status
uv run holoclip examples-prepare-holdout --max-clips 3 --window-seconds 20
```

The checkpoint is diagnostic with `qualification_allowed=false`,
`reviewed_gold_available=false`, `event_split_verified=false` and
`benchmark_held_out=false`. **It is not a BenchmarkManifest and must never be
passed directly to `providers qualify`.** Preparation currently measures finished
edits only, not source-to-finished alignment; a later stage must obtain **bounded
original-source ranges**, verify multi-POV/event overlap and exact corresponding
edits before constructing a true held-out benchmark. Availability/policy errors
do not grant exceptions or trigger paid endpoints.

## Increment 3: automatic approximate clip-to-original audio anchors (#119)

Run the new CPU-only, zero-cloud-key source-audio correspondence step after
the existing discovery and finished-video preparation. It downloads the **original
stream's audio only** through the managed source provider, not the entire original
video archive, and creates compact, streamed multiband FFmpeg fingerprints.

```powershell
uv run --extra media holoclip examples-grow-corpus --discover-only
uv run --extra media holoclip examples-prepare-holdout --max-clips 3
uv run --extra media holoclip examples-align-holdout --max-clips 3
```

The aligner searches each well-supported finished-audio fragment across the entire
original stream using FFT correlation. A candidate must exceed **both** a minimum
absolute correlation and a uniqueness margin over distant competing matches.
Silent/ambiguous/synthetic/unmatched fragments are rejected; the output retains
the exact measured fragment positions, scores, correlation margins and approximate
source-offset groups. These measured *audio matches* can expose multi-cut
discontinuities without relying on a clip URL timestamp hint or cloud ASR.

Safety and scope:
- Re-check current public/ended clip/source status, original-link identity,
  source policy, training-stream collisions and verified finished-video bytes
  before downloading original audio.
- Cache source and clip fingerprints by their media SHA-256. Reusing a saved
  alignment requires identical media bytes, method version and current policy.
- Prioritize unseen holdout candidates ahead of retryable errors and already
  cached successes in each bounded cycle, so repeated unattended invocations
  continue through new streams rather than rescanning only the first records.
- Save `data/provider-benchmark/holdout/holdout-audio-alignment.json`,
  including explicit unknowns, comparison scores and provenance. A weak/noisy
  audio match is **not silently repaired by model guesses**.
- Audio matches only support **approximate** original-source time ranges; cut
  transitions may happen between matched anchors. They do NOT measure visual
  reframing, mute sections, captions, overlays, identity of speaking characters
  or exact frame-level editorial cut positions. BGM, soundtrack overlap,
  repeated segments and heavy audio processing can make alignment inconclusive.
- This output **remains unreviewed**: `benchmark_held_out=false`,
  `event_split_verified=false`, `qualification_allowed=false` and
  `reviewed_gold_available=false`. No train dataset is modified and no
  production provider is promoted.

**Next:** source-range-only original video extraction once acoustic windows are
measured; independent cross-stream/same-event leakage checks; finer cut
localization from bounded video/audio; and calibrated independent semantic gold.

## Increment 4: bounded original-video and finished-edit paired measurements (#120)

After `examples-align-holdout` has produced sufficiently unique audio anchors,
`examples-measure-source-pairs` requests **only 5–120-second sections** of the
original video with yt-dlp `--download-sections` and FFmpeg; it NEVER falls
back to downloading an entire original livestream. The new source-adapter
contract `acquire_video_range` is explicitly different from ordinary
`acquire(EDITING_VIDEO)`. Keyframe forcing is used when available but does not
prove the original absolute frame PTS; yt-dlp section timestamps remain
**requested time offsets**, not a definitive cut map. See
[yt-dlp's documented section flag](https://github.com/yt-dlp/yt-dlp#download-options).

```powershell
uv run --extra media holoclip examples-grow-corpus --discover-only
uv run --extra media holoclip examples-prepare-holdout --max-clips 3
uv run --extra media holoclip examples-align-holdout --max-clips 3
uv run --extra media holoclip examples-measure-source-pairs --max-clips 3 --max-ranges 3
```

The bounded stage independently verifies, **before network download**, the
historical learning/holdout split, canonical clip/source identities, finished
video and frame/sheet checksums, original audio checksum and anchor uniqueness.
It then re-checks public/ended status and both LEARN policies, **including
range-scoped restrictions on padded original source sections**, before video
acquisition. Any ambiguous source, revoked policy, corrupted media, weak
anchor, mismatched section identity, missing audio, oversized/implausibly long
section or absent range-aware SourceProvider fails closed. Normal source-video
downloads are NEVER substituted.

For up to 3 original source groups per clip, FFprobe/FFmpeg/PySceneDetect
measure separate source and corresponding finished-video windows. The
range-aware yt-dlp provider stores a SHA-256 and source/time identity sidecar
in each isolated section directory; a missing or damaged sidecar/media is
reacquired instead of silently reused. Both
measurement manifests, section byte hashes, audio hashes, requested absolute
start/end, sampled-frame manifests and explicit uncertainty are persisted
atomically under
`data/provider-benchmark/holdout/source_pairs/holdout-source-pairs.json`.
A repeat run verifies every referenced byte before reusing an artifact.
The default per-clip bound is three sources windows, each 5–120 seconds;
the original-section file ceiling is 256 MiB, checked after downloading.
A source provider that downloads a full archive despite a section request
is rejected by measured duration; this is a validity/safety check, not
a guarantee of network bandwidth consumed by every remote extractor.

**Not established:** video-local zero versus requested original-stream absolute
timestamp, frame-accurate cut transition, crop/zoom delta, subtitles/BGM,
multi-POV event independence, or independently reviewed semantic gold.
Those require separate measured refinement and holdout validation.
No source-pair manifest is a production `BenchmarkManifest` and
`qualification_allowed=false`, `benchmark_held_out=false`,
`frame_accurate_cuts_verified=false`, `event_split_verified=false`
and `reviewed_gold_available=false` remain mandatory.

## Increment 5: visually measured zoom/reframe candidates (#121)

`examples-inspect-visual-edits` consumes only **previously measured source and
finished video pairs** (#120) with verified source-section, decoded-frame,
contact-sheet, finished-video, source-audio and reservation evidence.
No video or AI model is downloaded at this stage; OpenCV ORB and RANSAC run
locally as deterministic *computer vision*, not local model inference.

```powershell
uv run --extra media holoclip examples-grow-corpus --discover-only
uv run --extra media holoclip examples-prepare-holdout
uv run --extra media holoclip examples-align-holdout
uv run --extra media holoclip examples-measure-source-pairs
uv run --extra media holoclip examples-inspect-visual-edits
```

The inspector pairs sparse measured frames using local bounded PTS proximity
only as a **candidate filter**. It estimates a similarity-affine model from
cross-checked ORB matches with RANSAC, rejecting low-inlier, poorly distributed,
non-unique or implausible transforms. It may report measured, bounded
`identity_like`, `zoom_candidate`, `reframe_candidate`,
`zoom_reframe_candidate` or `other_affine_candidate` observations with actual
source+finished frame IDs, PTS, candidate correspondence margin, inlier counts,
coverage, geometric residual, scale and normalized visual center.

Critically:
- A sparse match establishes **candidate geometry for those images only**,
  not the reason a human editor chose a shot.
- If there is no convincing match, repeated imagery or too many frames to
  examine without silent truncation, output is **UNKNOWN**, not absence.
- Two independently decoded source/finished windows have distinct local PTS
  origins; their apparent proximity is not an absolute time truth.
- The measured sampling cannot certify cut frame, clip speed, frozen segment,
  overlay/subtitle presence or multi-POV equivalence. OCR, subtitle effects
  and composition remain separate dimensions under #83–#89.
- Training set contamination, source/clip current policy, range restrictions,
  source identity and every referenced media/frame/sheet byte are checked
  before output reuse. A changed measured input invalidates its cache key.
- Result stays outside production and Editorial Profile, with
  `qualification_allowed=false`, `benchmark_held_out=false`,
  `frame_accurate_cuts_verified=false`, `event_split_verified=false`,
  `reviewed_gold_available=false`.

Output: `data/provider-benchmark/holdout/visual_edits/holdout-visual-observations.json`.
No true held-out semantic gold is created. The next stage should assess
actual performance on diverse public source-to-finished videos, add dense
piecewise temporal mapping around detected discontinuities, and begin stable
subtitle/overlay detection. Fixture-only green CI does not establish that
those downstream semantics are accurate.

## Increment 6: dense audio transition brackets with visual provenance (#122)

`examples-refine-cut-candidates` uses the original full-stream **audio
fingerprint** and finished clip audio already downloaded for #119–#121.
It runs an additional fine-grained, bounded matching pass inside previously
verified coarse source/finished timing groups (default every 1 second, using
2-second templates and a ±3-second local search). Strong spectral agreement
and non-ambiguous competing offsets are required before recording measured
correspondence points.

When adjacent uniquely supported audio points imply different original-source
offsets, it records **a candidate transition interval**, bounded by the
last reliable old-offset sample and first reliable new-offset sample, with
explicit 250ms sampling uncertainty. Intervals wider than six seconds are
classified `UNKNOWN_WIDE_GAP`, not a confidently localized cut. Conflicting
overlapping anchors are `UNKNOWN_CONTRADICTORY_AUDIO`. Missing, silent,
processed, or ambiguous audio stays unknown. Exhausting the fixed 480-point
budget returns **no partial cut conclusions**.

The new stage also reads previously measured source/finished frame pairs and
summarizes visual-correspondence coverage, but **never claims visual evidence
confirms a cut** without matching absolute PTS and frame-neighborhood
measurements. The input guard rechecks source+clip public/ended access, current
LEARN policies including range-specific restrictions, training/holdout split,
reserved original source identity, and verified audio/video/frame bytes.
Resuming cached results requires an exact fingerprint of all upstream media,
frame manifests, acoustic anchors and visual diagnostics. There are no cloud
API calls, new user-supplied timestamps, human labels, or new videos to fetch.

```powershell
uv run --extra media holoclip examples-refine-cut-candidates
```

The output at
`data/provider-benchmark/holdout/cut_brackets/holdout-cut-brackets.json`
remains strictly diagnostic. `qualification_allowed=false`,
`benchmark_held_out=false`, `frame_accurate_cuts_verified=false`,
`event_split_verified=false`, and `reviewed_gold_available=false`.

**Limitations:** a detected discontinuity in an audio offset is not itself
proof of an edit frame; crossfades, silences, replacement music, repeated
samples and transcoding may move or hide acoustic evidence. Coarse audio
alignment is only a hypothesis about original audio, not proof of source
video absolute seek time. This is not a complete continuous
`OutputSourceTimeMap`. Visual temporal corroboration, actual Japanese
video calibration, speed/freeze handling and independently reviewed gold
are required before editing readiness or provider qualification.

## Increment 6: local acoustic cut-transition brackets (#122)

Once `examples-inspect-visual-edits` has recorded conservative source/finished
frame correspondences, the new `examples-refine-cut-candidates` command measures
**denser audio offsets** around previously unique source-aligned clip intervals.
Its work remains bounded (480 tested short fragments per clip by default)
and uses cached 12-band 4 kHz fingerprints from existing local finished video
and original-stream audio; no new model, full-video download, user-selected
timestamps, or cloud inference is needed.

```powershell
uv run --extra media holoclip examples-refine-cut-candidates --max-clips 3
```

For each acoustically supported source-to-finished region, the refiner matches
2-second fragments on a 1-second grid inside a local ±3-second source search
window around the prior approximate offset. It requires a high correlation and
uniqueness over distant local alternatives. Consecutive fragments with stable
offsets are *not* treated as cuts. When two locally unique matches disagree
and their supported clips are separated by at most six seconds, the output
contains a **CUT_INTERVAL_CANDIDATE** with an output-video time interval, not a
single frame or an exact timestamp. Unsupported longer gaps remain
`UNKNOWN_WIDE_GAP`; contradictory overlaps become
`UNKNOWN_CONTRADICTORY_AUDIO`; exhaustion of the configured work budget
produces `UNKNOWN_SAMPLING_BUDGET` with **no partial cut claims**.
Silent/unmatched passages cannot become proof that a cut exists.

The stage accepts only up-to-date, checksum-verified earlier evidence, checks
original stream/candidate identity, policy (including padded source ranges),
reservations and learning-leakage conflicts before reuse or local processing.
Source/finished visual matches are included as diagnostic counts only:
sampled ORB geometry cannot independently confirm the acoustic cut boundary.
Content-hashed evidence is checkpointed under
`data/provider-benchmark/holdout/cut_brackets/holdout-cut-brackets.json`.
All production/gold/held-out/event-split and frame-accurate flags remain
**false**; the old training dataset and editorial profile are untouched.

**Next:** measured source-PTS correction and synchronized dense visual frames
around candidate brackets, verified piecewise time mapping, speed/freeze
discrimination, subtitle/overlay observation and cross-stream event isolation.
Real Hololive clip quality and free-provider production qualification remain
unverified by fixture CI.

## Increment 7: bounded visual cross-check of acoustic cut intervals (#123)

The `examples-refine-cut-candidates` stage now also inspects the finished
human-edited video **only around acoustically supported cut brackets**. Using
local FFmpeg and OpenCV, it samples no more than a 10-second padded window,
at 8 grayscale frames/s at 256×144, and records isolated, high-change visual
peaks in the acoustic interval. The evidence is a sampled clock **interval**,
not an original frame PTS. A strong isolated output visual change may be
labeled `CORROBORATING_OUTPUT_CHANGE`; silent, low-change, high-motion or
otherwise unsupported video remains `UNRESOLVED_VISUAL_EVIDENCE`.
Natural game/camera scene changes can mimic an edited cut, so visual jump
observations never certify a human edit.

The stage preserves the existing source-video pair SHA-256, finished-video/
source audio hash, visual-frame and sheet validation, current source identity,
public/ended access, policy and range restriction, holdout/training isolation,
as well as fail-closed provenance and resumable checkpointing from #122.
Audio/video contradictions, source-time ambiguity, frame-accurate cuts,
cross-POV event overlap, human review, and genuine independent gold are
explicitly unresolved. The old training corpus is never modified.

For complete local staged diagnostics after the earlier phases:

```powershell
uv run --extra media holoclip examples-refine-cut-candidates --max-clips 3
```

The final diagnostic checkpoint stays
`data/provider-benchmark/holdout/cut_brackets/holdout-cut-brackets.json`.
All qualification, true held-out, source-frame, event-split and reviewed-gold
statuses remain **false**. This is not the dense piecewise
`OutputSourceTimeMap` nor a production editing profile. More accurate
source↔output visual correlation with independently verified original PTS
and real Japanese Hololive clips remains acceptance work.

## Increment 8: calibrate source-video section audio clocks (#124)

`examples-calibrate-source-clocks` adds an independent verification stage
**after** bounded source-video pair acquisition (#120), without assuming that
yt-dlp's requested original-source start time exactly equals the downloaded
section's actual beginning.

```powershell
uv run --extra media holoclip examples-calibrate-source-clocks --max-clips 3
```

For each previously checksum-verified and rights-approved **bounded original
video section**, decode its audio fingerprint using the existing local FFmpeg
12-band/4kHz approach and match **at least three independent, unique
4-second fragments** against the already cached full original-stream audio.
Every accepted fragment must exceed correlation and uniqueness thresholds,
and all independently measured offsets must agree within 0.25 seconds.
Missing sound, nonunique recurring source audio, discontinuous download,
unexpected requested-vs-measured section origins, and A/V clock disagreement
stay explicitly `UNKNOWN`. Shortened/misplaced original downloads cannot
become trusted visual source mappings.

When source acoustic origin is supported, ffprobe also inventories the
original **bounded section's native video-frame PTS** and the first audio
packet's PTS. It creates an **approximate audio-relative original stream video
time range**, NOT a verified original-stream video frame clock or a finished
edit cut. AAC priming, source A/V offset, and transcode delay can change the
relative audio/video clock. The 0.25-second audio feature interval is a
resolution bound, **not a calibrated maximum timestamp error**.

Every run re-checks learning/holdout overlap, current public/ended status,
unique source metadata link, clip and original LEARN policy and range-scoped
restrictions; SHA-256 pins the cached source/audio, original/finished video
sections, measurement manifests and decoded frames. If measured source
coverage differs from the originally requested section, rights policy is
checked again on a padded observed interval; a restricted overlap is
**quarantined**. Cached successes are never reused without revalidation.

Output is
`data/provider-benchmark/holdout/source_clocks/holdout-source-clock.json`.
No provider, benchmark, or semantic gold promotion is allowed. Verified
original frame absolute PTS, exact source-to-finished frame mapping,
edit-origin vs original scene transitions, speed/freeze, multi-POV event
independence and live quality calibration remain open. All existing false
qualification, event-split, frame-cut, held-out and reviewed-gold flags
are retained.

## Increment 9: audio-conditioned source↔finished measured frame links (#125)

After source-section clocks are independently calibrated by
`examples-calibrate-source-clocks` (#124), the
`examples-correlate-source-frames` stage compares **actual decoded and
checksum-verified original and finished frame images** (#120). Local OpenCV
ORB keypoints + RANSAC similarity geometry are evaluated against both
**independent acoustic time predictions** and visual duplicate alternatives.
The candidate with the highest visual score is not arbitrarily overridden
by a much weaker in-window match. If a visually identical source frame
appears at different times, uniqueness must be measured; ambiguous repeats
cannot qualify even for a partial link.

```powershell
uv run --extra media holoclip examples-calibrate-source-clocks --max-clips 3
uv run --extra media holoclip examples-correlate-source-frames --max-clips 3
```

For up to 64 measured frames per side (at most 4,096 comparisons), the
inspector measures candidate frame IDs, shot IDs, decoded **window-local**
PTS, estimated clip/source positions, acoustic time residual,
geometric RANSAC coverage, zoom/reframe candidate, competing visual
match margins and rejected/conflicting observations. At least two
temporally separated, monotonically increasing visual links with broadly
consistent offsets are required before a group is reported as
`PARTIAL_AUDIO_VISUAL_FRAME_LINKS`. Unknown, contradictory, single-frame,
budget-exceeded and repeated visual matches are retained as `UNKNOWN`.

The source clock from #124 remains **audio-relative and approximate**;
the measured source frames were themselves extracted from a
**re-encoded bounded section**. Original livestream native frame PTS,
source A/V priming, actual editor-cut frame, scene transition cause,
audio drift and complete OutputSourceTimeMap remain **unverified**. A
partial link must never be extrapolated through gaps or converted into
training gold. The output
`data/provider-benchmark/holdout/frame_links/holdout-frame-links.json`
is a diagnostic report and cannot qualify production AI.

The stage recomputes the previous clock input fingerprint and re-checks
the original section, source/full-audio media, finished clip, sampled
frame and contact-sheet checksums, train/holdout reservation conflicts,
source-link uniqueness, latest public/ended visibility and clip/source
LEARN policies **including both requested and audio-observed source
ranges** before processing or cache reuse. Every qualification,
reviewed-gold, independent-held-out, true original native-frame,
frame-cut and dense-time-map flag remains **false**.

Next: direct source/output decoding at bounded acoustic transition
brackets with independently observed native timestamps and visual
disagreement metrics, then real media accuracy calibration, multi-POV
event isolation, OCR/subtitles and rendering/edit-profile evaluation.

## Increment 10: independent bounded video comparison on both sides of acoustic cuts (#126)

After `examples-refine-cut-candidates` (#122–123),
`examples-calibrate-source-clocks` (#124) and
`examples-correlate-source-frames` (#125), use:

```powershell
uv run --extra media holoclip examples-compare-cut-sides --max-clips 3
```

This compares **actual source and finished image pixels on both sides** of
each acoustically observed source-offset discontinuity. Each side is selected
from independently unique short acoustic anchors and a positively calibrated
bounded original-video section; the comparator decodes only a 1.5-second
source section and a 1.5-second finished-video sample per side at 8fps,
640×360 grayscale, up to 16 frames per bounded window. Frame geometry
uses local OpenCV ORB and RANSAC, requires at least two increasing
source-to-finished matches on **each** side, and actively tests the
opposite source side as a repeated-content confounder.

A `MULTIMODAL_EDIT_JUMP_CANDIDATE` requires a nonzero source-audio time
jump, a previous isolated output visual change and independent visual
correspondence on both sides. It reports the candidate gap and measured
frame-pair geometry, but **never** certifies that the output jump was
introduced by the human editor. Same-scene repeats, source
time continuity, reordered source, unsupported section clock, and
source/output visual mismatch remain explicit `UNKNOWN` states.

The stage enforces current rights/policy on source and finished clips,
both requested and audio-observed source ranges, current public/ended
source identity, reservation/split isolation, source/finished media and
all stored frame/sheet SHA-256, upstream source clock hash and 8-transition
sampling budget before cache reuse. It neither downloads more source media
nor calls an LLM. It persists only unreviewed diagnostic evidence at
`data/provider-benchmark/holdout/cut_sides/holdout-cut-side-evidence.json`.

**Still unverified:** original native absolute frame time, cross-video AV
priming delay, an exact edited cut frame, full piecewise time map, a semantic
editing decision, independent multi-POV event split, reviewed gold, and
actual Hololive media quality. Every production/held-out, reviewed-gold,
event-split, frame-accurate and dense-map flag remains **false**.
Synthetic pixel tests and bounded FFmpeg decoder tests do not
establish real-world acceptance accuracy.

## Increment 11: evidence-only partial OutputSourceTimeMap candidates (#127)

`examples-build-partial-time-map` consumes all *previously measured* reserved
holdout evidence—independently unique audio anchors, source/finished image
frame links, source-section clock, acoustic cut brackets and actual source
cut-side diagnostics—without calling AI inference or acquiring new media.

```powershell
uv run --extra media holoclip examples-build-partial-time-map --max-clips 3
```

This emits an **explicitly non-authoritative partial time-map diagnostic** at
`data/provider-benchmark/holdout/partial_time_maps/holdout-partial-time-map.json`.
A proposed `LOCAL_INTERPOLATION_CANDIDATE` is bounded strictly by two distinct,
already measured, increasing source-to-finished image frame matches, at most
three seconds apart, with stable near-1x time offset/rate and **one uniquely
matched acoustic anchor corroborating both endpoints**. Neither before nor
after those actual frame observations is extrapolated. The service never
crosses an acoustically observed cut/unknown transition, never joins
independent source-window groups and never connects unobserved long gaps.
Those portions are emitted explicitly as `unknown_intervals`.

The candidate is intentionally not a claimed *verified interpolation*:
matching two images and one unique audio span **does not prove every
intermediate video frame** or exact source↔output native PTS. It records
supporting IDs, candidate speed, residual audio clock mismatch and
approximate audio-relative original times; none may directly drive rendering
or serve as learned editorial gold. Even 100% visual/acoustic candidates
would not establish exact human edit causes or frame times.

Before using evidence or cached output, the stage recomputes source-section
clock and cut-side report content fingerprints from SHA-256-checked original
media, finished media, sampled source/finished frames and manifests;
revalidates stream holdout reservation and learning split collision,
canonical public/ended original link, latest clip/source LEARN policy,
and requested plus observed original-source ranges. Corrupted artifacts or
policy restrictions fail closed. Candidate work is bounded (160 frame pairs
by default); exhaustion produces a fully unknown map, not partial output.

**Nonqualification:** `qualification_allowed=false`,
`benchmark_held_out=false`, `event_split_verified=false`,
`reviewed_gold_available=false`,
`frame_accurate_cuts_verified=false`,
`dense_output_source_map_verified=false`,
`interpolation_verified=false`. Native original livestream frame PTS,
cross-video A/V delay, reliable speed/freeze, full piecewise timeline,
subtitles and precise edit cuts, independently reviewed event-isolated
gold and real Hololive clips remain unfinished.

## Increment 12: one-command real-media holdout execution and measurement audit (#128)

Nine existing stage commands (#118–127) can be executed in the correct
dependency order, without the operator manually supplying original video
URLs, source timestamps or repeating every stage invocation:

```powershell
uv run --extra media holoclip examples-validate-holdout-e2e --preflight-only
uv run --extra media holoclip examples-validate-holdout-e2e --max-clips 3
```

The preflight validates that local `corpus_growth.json` contains reserved
unreviewed stream/clip records, checks historical training/reservation
conflicts and refuses to silently select a different subset if there
are more reservations than the bounded `max-clips` budget.
The preflight makes no network calls or video downloads. A full run uses
only public/ended source pairs and the local free FFmpeg/OpenCV environment,
and reuses each previous stage's SHA- and policy-validated checkpoint.
Every stage checks independent source identity, current LEARN restrictions,
strict sample budgets and no train/holdout source conflicts. A blocked,
quarantined, insufficient or failed prerequisite stops subsequent work;
errors reported in the audit include only safe error-class names and
never remote stderr/auth details.

The `holdout-e2e-audit.json` report records actual stage statuses for
every reserved clip, which clips reached all nine stages, measured
source↔finished frame links, source audio matches, multimodal cut candidates,
local candidate time-map coverage and unknown map outcomes.
**Metrics that require independently reviewed gold are explicitly null**:
editor-cut precision/recall, absolute native frame timestamp error and
event-held-out accuracy. Successful completion says only
`UNREVIEWED_MEASUREMENTS_COMPLETE`, never `ready=true`,
`qualification_allowed=true` or `benchmark_held_out=true`.
Tests using injected/fake stage runners are stamped
`SIMULATED_STAGE_TEST_COMPLETE` and cannot claim a real media run.

**Important external-data dependency:** the GitHub repository contains
only example metadata and unit/FFmpeg test fixtures, not the user's
local acquired Hololive videos, original audio, corpus reservations,
or real held-out checkpoint evidence. CI passing does not constitute
real-video calibration. To measure meaningful real-video results, the
command must run against the user's existing reserved sources and local
download cache in a permitted, network-connected Windows environment.
Do not report acceptance rates or unseen clip precision without that run.

## Increment 13: safe multi-batch execution and isolated run reports (#129)

Real reserved holdout lists commonly exceed `--max-clips 3`. The previous
single-command implementation refused *any* work when there were more
reservations than the per-run budget. Instead, successive invocations now
select bounded batches prioritizing source clips with no completed final
stage, while preserving every original holdout reservation and all
nonreserved queued/training history. A temporary projected state under
the holdout output root feeds existing stage processors, so their own
`max_clips` prioritization cannot silently switch clip identities
between stages. The original state is never rewritten or relabeled.

Batch size, selected clip IDs, total reservations and remaining queue
length are reported. Duplicate original clip reservations block the run
rather than merging potentially different source identities. Exclusive
local lock acquisition occurs *before any report/checkpoint mutation*;
two overlapping invocations cannot overwrite stage artifacts. A crash
may leave a lock; this is an intentional fail-closed condition, and the
operator must investigate the process before removing it. Original
reservation file SHA-256 is checked before every stage and after the last
stage, rejecting changes during processing.

The audit now masks reports for stages **not executed in the current
invocation**, even when old successful downstream reports remain on disk.
The completed-stage and frame/candidate coverage counts cannot be
inflated by previously cached later-stage observations after a new
upstream failure. Preflight uses a separate
`holdout-e2e-preflight.json`, preserving the most recent full-run
`holdout-e2e-audit.json`; cached stage artifacts still receive the
underlying pipelines' current SHA/policy/split rechecks on reuse.

The entire batch remains unreviewed and unqualified. These protections
do not certify editor accuracy, source/native frame PTS, completed source
mapping, event-level holdout independence or production model readiness.

## Increment 15: scripted audio/video jump and BGM fixture (#131)

The prior real-FFmpeg nine-stage integration scenario contained one continuous
45-second source excerpt and could not exercise even a known edit jump.
An additional *fully offline* test now actually decodes, trims and concatenates
the source intervals 9–31s and 44–66s into a 44-second finished video
with a deliberately scripted join at finished t=22s. A second variant adds
quiet continuous background music. Both feed the **real nine-stage**
source/audio/video/PTS/ORB/clock/partial-map functions.

The first execution revealed a real failure: a 44-second two-piece edit had
only three 6-second audio anchor sample starts, one straddled the cut,
and `PARTIAL_AUDIO_MATCH` was refused despite two independently
matching original sections. The **candidate sampling frequency** is now
increased to ~8-second cadence (still capped at 24), while the
individual 6-second acoustic fragment requirement, correlation >=0.79,
uniqueness margin >=0.065 and three-independent-anchor minimum remain
unchanged. A `match_sampling_version` on the saved record forces previous
20-second-cadence alignments to be recomputed on unchanged media rather
than silently reused; later source-video and mapping checkpoints recheck
their input provenance. This change increases observational coverage,
not evidence validity or production qualification.

The known edit script is test-only synthetic ground truth: checks can require
an acoustic candidate interval to **contain the scripted cut**, and that
all observed source intervals remain bounded. No such generated test fact
is allowed into the Hololive corpus, provider qualifications, or reviewer
gold. A passing test validates decoder and candidate-bracketing integration
for only those controlled conditions. It does not evaluate independent real
human-edited clips, noisy stream speech, overlays or the quality of
editorial selection and cannot switch any production readiness flag.

## Increment 16: refuse unverifiable audio islands even with matching source offset (#132)

A same-duration inserted mute/narration/BGM interval can yield uniquely
matching acoustic samples both before and after the insertion at the
**same** clip↔source offset. Earlier anchor assembly allowed up to
**25 seconds of unobserved edited audio** between sampled 6-second
anchors to be represented as one continuous `PARTIAL_AUDIO_MATCH` range.
This did not constitute verified acoustic evidence inside the missing
middle and could incorrectly broaden source video acquisition and
candidate cut evidence downstream.

The grouping tolerance is now **at most 4 seconds of unobserved audio**
between the two independently sampled matching fragments. The earlier
~8-second candidate sampling cadence, 6-second acoustic windows,
correlation >=0.79, uniqueness margin >=0.065, >=3 accepted matches
and the 24-anchor upper bound all remain unchanged. Missing inserted
audio stays outside every `ranges` interval and is not bridged merely
because the measured end segments happen to have the same offset.
The persisted `match_sampling_version` advances to
`bounded-observed-audio-islands-v3`, requiring recomputation of old
audio matches and subsequent provenance-dependent results.

Added tests for same-length foreign-audio replacement in a 48-second
clip while surrounding source offsets remain identical, pure three
disjoint 16-second audio passages, and a real FFmpeg 48-second
**three-piece edited video** whose two known scripted joins are
at output t=16s and t=32s. The complete nine-stage pipeline must
conservatively bracket both scripted boundaries; these artificial
known boundary times are never promoted to human reviewer gold.
Real Hololive stream capture with BGM, repeated scenes, multi-source
editing and actual editor intent still require independent evaluation.

## Increment 17: bounded unattended multi-batch reserved real-media trial (#133)

Single-batch `examples-validate-holdout-e2e --max-clips 3` is intentionally
limited, but repeated manual commands are inconsistent with the goal of
autonomously testing all locally reserved source/clip evidence.
The command now accepts `--all-batches --max-clips 3 --max-batches 8`.
A single invocation processes up to 8 sequential three-clip batches,
with a bounded `--max-batches` limit of 1..12. Clip selection for each
batch excludes all clips already attempted in the same invocation even
when previously cached; no clip may be counted twice. All existing
holdout corpus learning/event split, SHA integrity, source/clip policy,
rights, upper media-length, FFmpeg and proof checks remain per stage.

One per-root exclusive lock is held throughout the complete multi-batch
execution. Each finished batch is atomically checkpointed into a
separate `holdout-e2e-all-batches.json` alongside the per-batch
`holdout-e2e-audit.json`. The aggregate retains **per-batch** stage
statuses, selected clip identities, observed-only frame correspondences,
candidate acoustic cuts and partial local mapping coverage; it reports
remaining reservations. Any blocked or quarantined stage stops the
whole invocation before starting another batch, preserving the failed
batch and reason. Exhausting the configured invocation budget reports
`BOUNDED_BATCH_LIMIT_REACHED`, never fake successful completion.

The outer controller checks the original durable reservation state
against its SHA before each batch, while the nine-stage inner controller
checks the same state before each stage and on exit. Neither the
training data nor the durable holdout reservation state is modified.
All quality figures requiring genuine independent human gold remain
null; every production, event-heldout, frame-accurate and gold flag
stays false. Synthetic injected stage-runner tests cannot claim that
actual media was processed.

This allows unattended *trial execution*, not unattended **publication**
or ready-route promotion. Real edited Hololive media still must be
available locally, and any empirical failure must be investigated.

## Increment 18: no-media offline status and repair diagnosis (#134)

The bounded all-batch run now stamps SHA-256 of the original durable
reservation state in both per-batch and cumulative audit reports. The
new `examples-diagnose-holdout-e2e` command is a read-only metadata/audit
inspection workflow (except writing its separate JSON output). It does
**not** call a media provider, yt-dlp, FFmpeg, text/vision model, or
external API. Before trusting any diagnostic it re-evaluates the
train/holdout source-stream inventory and compares the exact reservation
state SHA-256 with that of the saved run. Missing hash from earlier audit
schemas, changed reservation content, malformed/tampered stage counts,
duplicate or unknown clip references and any apparent production,
event-heldout or reviewer-gold promotion fail closed.

The diagnosis prefers `holdout-e2e-all-batches.json` and does not mix
its observations with the last individual `holdout-e2e-audit.json`.
It retains the originally attempted public clip IDs, identifies still
unattempted reservations and aggregates **only saved stage-level
diagnostic status counts**, including downstream
`NOT_EXECUTED_THIS_RUN` markers. It classifies the first blocking
stage into fixed remediation codes for acoustic insufficiency, policy,
source identity and media integrity; raw provider/FFmpeg exception
messages, tokens and URLs are never copied. It also explicitly reports
`SIMULATED_TRIAL_ONLY` for injected test runner reports and
`TRIAL_INCOMPLETE_BATCH_BUDGET` for a finite batch limit.

A historical complete audit is only a reported finished test run;
offline diagnosis **does not recheck binary media bytes** and does not
supply independent editor cut-frame precision/recall, event isolation,
native source frame PTS or a full OutputSourceTimeMap. No model route is
marked production-ready or reviewed-gold.

## Increment 19: narrowly isolated audio insufficiency without unsafe bypass (#135)

A three-clip batch halts all later work when one finished clip has too
little independently unique audio to locate in its original. The
`--continue-on-insufficient-audio` **opt-in** for
`examples-validate-holdout-e2e --all-batches` now isolates source trials
to **one reserved clip per bounded batch** (`--max-batches` remains
1..12). The original strict default remains unchanged. Only measured
`INSUFFICIENT_AUDIO` and `INSUFFICIENT_UNIQUE_ANCHORS` reported
specifically by the verified `original_audio` stage are deferred.
This allows the next distinct reserved clip to be checked without
rerunning earlier selected clips, fabrication of matches or
unbounded retry.

Nothing is skipped for `QUARANTINED_POLICY`, source identity changes,
rights restrictions, split conflicts, `RETRYABLE_FAILURE`, corrupted
reports, inconsistent per-clip status counts or absent evidence:
these continue to block the whole invocation. Deferred audio failures
are only marked in a **separate diagnostic/aggregate report**; the
durable corpus reservation/learning history is not modified. The
cumulative audit retains `deferred_audio_clip_ids`,
`attempted_clip_ids`, successfully processed IDs, honest remaining
unattempted clip counts, and per-batch blocked stages. The media audit
never claims `UNREVIEWED_ALL_BATCHES_COMPLETE` when any clip is
deferred: it ends as `UNREVIEWED_INSUFFICIENT_AUDIO_RECORDED` (or
`SIMULATED_INSUFFICIENT_AUDIO_RECORDED` with injected test runners).
Failing to reach all reserved clips within the finite batch budget
still reports `BOUNDED_BATCH_LIMIT_REACHED`.

Offline diagnosis validates that every declared deferred clip has
exactly one selected ID, a real `original_audio` blocking stage
and exactly one unambiguous approved insufficiency status; it rejects
forged policy deferral. It reports incomplete/simulated evidence,
never promotes reviewer gold, production approval, native source PTS,
dense maps or cut-frame accuracy. For real Hololive accuracy, the
operator must still run the bounded trial on authorized local media.

## Increment 20: restart-safe fully revalidated bounded batch replay (#136)

A saved cumulative audit after each successful media batch was not
automatically reusable after a Windows reboot or interrupted run.
The `--resume-checkpoint` opt-in recovers the prior selection order,
**never** trusting the previous `status=success` by itself. The resumed
run recomputes **all nine** guarded production stage outcomes for each
previously successful batch with the same expected clip IDs, including
the same media bytes and source rights/policy checks already implemented
by each stage. All successfully rechecked old batches consume the same
finite batch count; later new reservations are processed only after
every historical batch is revalidated.

Current rights-policy fingerprints are also recorded on **single-batch**
audits and checked by standalone/automatic offline diagnosis; policy drift
or missing legacy policy digests blocks its claims about a current trial.

Every new cumulative run records the exact original reservation SHA-256,
ordered policy-rules SHA-256, stage schema SHA-256 and real-vs-simulated
execution mode. Replay rejects changed rights/policy, source reservations,
stage schema, execution kind, batch width, any forged/missing nine-stage
tallies, duplicated/nonreserved clip IDs, prior failed/quarantined/deferred
batches or invalid report flags. Unsafe prior checkpoints are **not
overwritten**: refusal is logged in a separate local JSON and the
operator can start a fresh safe run. The original multi-batch checkpoint
also survives a subsequent interruption *during historical replay*;
until previously successful batches have all been reverified, no
incomplete prefix is persisted over the original checkpoint.

Existing stage cache invalidation rules continue to govern expensive
media reuse; the checkpoint is merely an ordered set of previously
selected clip IDs, never proof of present video hashes or source
permissions. The same exclusive per-root lock, bounded max 12
total (replayed plus new) batches, quota constraints, no-Docker/no-paid
services and `ready=false` safeguards remain. No independently
reviewed editor accuracy or native original video PTS is inferred.

## Increment 21: validate audio evidence again at original-video boundary (#137)

Audit found a direct-entry safety gap: `align_reserved_holdouts` correctly
invalidates old audio records whenever the unique anchor matcher and
coverage policy changes, but `measure_reserved_source_pairs` accepted
audio records containing only the original `fingerprint_version`, not the
new `match_sampling_version`. An older sparse saved range spanning
up to 25 seconds of unobserved audio could therefore pass downstream
`_valid_ranges` when a source-pair command is invoked directly or
under unusual stale-cache conditions, even though the matcher has
explicitly refused such interpolation since the ~8-second sampling
and <=4-second grouping fixes.

The source-video boundary now requires exact
`match_sampling_version=bounded-observed-audio-islands-v3`, and
reconstructs every reported range from independently scored,
uniquely located six-second source/clip anchors: claimed range
first/last endpoints must be grounded, number of supporting anchors
must match exactly, anchors must not repeat, and every intervening
unobserved gap must stay <=4 seconds. Long unsupported narration,
mute or BGM replacements remain unknown instead of becoming assumed
video-time alignment or new source downloads. Saved source-video
pair records now also pin `audio_sampling_version`; earlier caches
without it are not reused even when both media SHA values are unchanged.

Regression tests include old-method report rejection **before any**
source-video acquisition, fabricated 25-second bridge, altered anchor
counts/endpoints, and forced remeasurement of an outdated source-pair
cache. Existing actual encoded FFmpeg cut/BGM and 3-piece nine-stage
regression suites remain mandatory. No synthetic fixture fact becomes
reviewer gold or production-ready status. Real human-edited Hololive
quality and native video absolute PTS remain unmeasured.

## Increment 22: explicitly scripted cut-candidate precision/recall (#138)

Previously real encoded FFmpeg integration cases checked whether each
test-authored cut time was enclosed by at least one acoustic candidate
interval. This is incomplete: a hugely broad candidate could enclose
multiple cuts, duplicates could inflate counts, and unwanted extra
candidates were not penalized.

The new standalone **media-free** `examples-score-scripted-cuts` scorer
uses strictly declared `scripted-ffmpeg-cut-reference-v1` construction
markers (clip ID, finished duration and strictly ordered known synthetic
cut times) with a version-checked `holdout-cut-brackets.json`. It
performs deterministic one-to-one maximal matching between known
scripted boundaries and eligible acoustic `CUT_INTERVAL_CANDIDATE`
intervals. Brackets wider than 8 seconds default to false positives
even if they enclose the scripted cut. Reported measurements include
matched count, unmatched candidates, missed known cuts,
scripted-interval precision/recall/F1, center-time absolute
error and bracket widths. `UNKNOWN_WIDE_GAP` and
`UNKNOWN_CONTRADICTORY_AUDIO` do not become positive candidates
or negative truth. Empty denominators remain `null`, not perfect
scores.

Tests run this measurement against three genuinely decoded,
independently reencoded *synthetic* FFmpeg cases: an unedited
45-second negative control, the 44-second two-piece hard edit
both without and with artificial BGM (one scripted cut at 22s),
and a 48-second three-piece edit (two known cuts at 16s and 32s).
All existing nine real decoder/FFT/image/mapping stages are retained.
The scorer rejects malformed references, wrong measured clip IDs,
unsupported evidence versions, promoted flags and attempts to
overwrite the original source reports.

These are **construction-defined synthetic interval metrics ONLY**.
They are not genuine Hololive editor intent labels, human-gold cut
precision/recall, a frame-accurate time map or proof of held-out
production reliability. Corresponding `editor_cut_precision` and
`editor_cut_recall` remain `null`, `ready=false`,
`qualification_allowed=false` regardless of numerical score.
Actual locally reserved Hololive source/finished media and independent
reviewer reference annotations remain the blocking next work.

## Increment 23: reference-free measured real-clip cut descriptions (#139)

The existing bounded `examples-validate-holdout-e2e` trial can collect
actual locally downloaded public human-edited clip/source evidence, but
the new #138 precision/recall/F1 scorer requires *synthetic FFmpeg
construction boundaries* and must not be used to rate genuine edited cuts.

The nine-stage real-media audit now automatically summarizes only the
acoustic cut reports and cut-side reports **executed for the same selected
clip in the current run**. New fields
`observations.real_cut_observations` and
`observations.real_cut_observation_totals` provide:
- per-clip candidate and UNKNOWN audio transition counts, median/P90
  bracket width, and number of brackets wider than 8 seconds;
- number of candidates with measured output-only visual change evidence,
  separately from candidates corroborated by two-sided original/finished
  acoustic/visual geometry;
- explicit UNKNOWN for unmeasured acoustic support or exhausted sampling
  budget (not zero detected candidates); missing cut schema, mismatched clip, original
  stream, finished media hash, source audio hash, unexecuted stages,
  unsafely promoted flags, missing side evidence, or exhausted budgets;
- schema and interval validation that rejects malformed acoustic
  transitions instead of writing plausible descriptive measurements.

Multi-batch runs preserve the per-batch records and accumulate only
fully completed nine-stage selected clips in
`holdout-e2e-all-batches.json`, with complete replay recomputing the
summary rather than trusting a previous checkpoint's quality claims.

No real-video true-positive/false-positive/missed cut metric is
fabricated, because a candidate can correspond to a natural scene
change, an inserted audio edit, or an actual editor cut. These
numbers are **reference-free diagnostics**, not independent human
editor gold or verified native video frame PTS. The report preserves
`editor_cut_precision=null`, `editor_cut_recall=null`,
`ready=false`, `qualification_allowed=false` and all
related non-promotion flags. Artificial/simulated run data never
becomes real media proof; actual Hololive clip calibration still
requires authorized local Windows media execution.

## Increment 24: fail-closed offline cut-signal anomaly triage (#140)

Real cut interval descriptions from #139 are useful for identifying
which *measurement signals* remain weak, but must not be mistaken for
true human editor reference labels. The existing automatically executed
`examples-diagnose-holdout-e2e` step now extracts these descriptions
from the saved single- or multi-batch trial audit **only after**
reservation SHA, rights policy fingerprint and all stage-count checks.

- Cut clip IDs must match the selected clip IDs of the currently
  reservation/policy-bound audit; every completed batch must contain each
  selected clip exactly once in its per-batch diagnostic map. This
  offline step does **not** re-prove media hashes or source identity.
- Cumulative diagnostics must be exactly the union of completed-batch
  diagnostics; totals are independently recomputed from checked rows.
  Bogus promoted gold, impossible candidate count/width, invented
  original+finished corroboration counts or unsupported audio counted as
  zero cuts are rejected as `BLOCKED_INVALID_AUDIT`.
- The saved `cut_signal_triage` reports fixed, independently coexisting
  signals such as `ACOUSTIC_SUPPORT_UNKNOWN`,
  `OVERBROAD_ACOUSTIC_INTERVALS`,
  `UNRESOLVED_ACOUSTIC_TRANSITIONS`,
  `OUTPUT_VISUAL_EVIDENCE_UNKNOWN`, and
  `SOURCE_SIDE_EVIDENCE_UNKNOWN`. A report can indicate a signal gap;
  **it does not prove a false edit, ground truth, or candidate accuracy**.
- Earlier version audits without these fields are still diagnosable,
  but report `LEGACY_NO_CUT_OBSERVATIONS`. A half-written or
  contradictory new audit fails closed, rather than being silently
  interpreted as legacy.
- The normal stage blocker, split/rights checks, original report and
  production routing are unchanged. The extra summary is read-only
  metadata inspection: no FFmpeg, downloads, manual URLs, labeling,
  automatic threshold training or paid providers.

All real-Hololive cut gold, source original absolute native frame PTS,
full time mapping, independent event/POV isolation, and production
quality calibration remain **unverified**; `ready=false` and
`editor_cut_precision/recall=null` by construction.

## Increment 25: preflighted full-range bounded acoustic sampling (#141)

When five or fewer independently supported acoustic ranges together
require more than the configured sampling-point cap (default 480),
the earlier v1 refinement sampled in chronological order, ran out of
points partway through the ranges and discarded every candidate as
`UNKNOWN_SAMPLING_BUDGET`. This was conservative but unnecessarily
lost useful complete-range observations for moderately long excerpts.

The v2 `bounded-acoustic-cut-brackets` method prevalidates all
coarse source/finished ranges before doing expensive correlation,
estimates required samples at 1-, 1.5-, and 2-second spacing, and
chooses the densest full-range plan that fits the existing configured
maximum. The final possible 2-second acoustic window of **every**
anchored range is included even when the stride is not divisible.
Only supported source regions are searched (unchanged `±3s` local
search budget and unique-match threshold), and a supported source
offset jump remains a non-frame-accurate candidate interval.
If the whole range cannot fit even at 2 seconds, the method returns
`UNKNOWN_SAMPLING_BUDGET`, `tested_points=0`, and **no partial
measurement**, rather than selecting the first few convenient ranges.

Every supported v2 measurement records actual sampling spacing,
planned/tested point counts, and `FULL_COVERAGE_BUDGET_ADAPTED`
strategy. Existing saved v1 cut reports are **not** reinterpreted as
v2: cut-side and partial source-time map direct stages require v2
method/version; their fingerprints force remeasurement. Real-video
descriptions and scripted FFmpeg regression scorer also require v2.

This is an algorithmic coverage change, **not** a claim that all
possible edits are found or that sampling resolution is 2-second
cut-time accuracy. It does not change source rights, approval,
reviewed editor gold, production route eligibility, native frame
PTS verification, or source/finished dense mapping.

## Increment 26: independent bounded output visual survey (#142)

Before this change, sampled visual discontinuity detection was used only
*after* acoustic cut candidates had been proposed. An editor cut masked
by BGM, a muted segment or transient audio could therefore have no
corresponding output-video observation even when it was visually obvious.

The reserved human clip's already-downloaded finished-video bytes are now
scanned independently in up to six bounded deterministic **output-only**
windows (six seconds each). Windows overlap when the finite per-video
budget permits; all windows and actual successfully decoded seconds are
recorded, and the unexamined duration remains explicit. The existing
FFmpeg grayscale isolated-change detector supplies the signals. Multiple
windows observing one timestamp-nearby peak deduplicate conservatively.
Peaks are classified as overlapping or **not overlapping** an acoustic
candidate interval; the latter is *not* evidence of an actual missed
human edit. The survey is performed even with no acoustic cut
candidates, provided the protected pipeline reached the audio-cut stage.

The `holdout-cut-brackets` record now has
`independent_output_visual_survey` and a bound survey method version.
The unreviewed real-cut observation summary and offline diagnosis also
report signal coverage and unmatched output-only peaks, but do **not**
add those peaks as `CUT_INTERVAL_CANDIDATE` or source-side
`OutputSourceTimeMap` segments. Strict numeric/count/version checks
reject unsupported promoted or contradictory survey statistics.
A decode error or inadequate samples are UNKNOWN, with measured peak
count NULL. Stage-cache identity includes the survey method, so older
audio-cut reports cannot silently stand in for this independent scan.

The scan can miss long-range or gradual transitions and natural
original-source scene changes can create false signals. Full source
video comparison, original native frame timestamps, independent editor
gold and Hololive accuracy remain **unverified**. All gold, production
and route qualification gates remain false; `ready=false`.

## Increment 27: bounded source-video checks of output-only changes (#143)

After the independent finished-video scan (#142), a visual change
could represent either original gameplay/camera content or an editor
transition. New protected source-side observations reduce that uncertainty
*only when there is strong local evidence for a source clock*.

During the existing two-sided cut stage, after the usual reservation,
rights-policy, finished/source checksum, original bounded section-clock
and frame-link provenance checks, output-only peaks are examined
against the original downloaded bounded video sections. Only a peak
strictly between **adjacent unique audio anchors in the same group**
with a continuous stable source offset (<=0.25s disagreement),
<=4-second unsupported gap, unique section mapping and sufficient
source-video margins may trigger an extra short original-video seek.
The approximate original-video check is additionally padded by
375 ms on each side to tolerate finite FFmpeg sampling and local clock
uncertainty; this is **not** an original native PTS or exact output cut
error bound. Other peaks remain `UNKNOWN_NO_TWO_SIDED_AUDIO_ANCHORS`. At most
six peaks may be probed per finished clip; crossing this cap leaves all
unprobed evidence UNKNOWN rather than privileging early peaks.

Results are observations, never independent cut truth:
`SOURCE_VISUAL_CHANGE_ALSO_OBSERVED` means a discontinuity
was also found near an *approximate* audio-derived original time,
not that an original camera event caused the finished visual change.
`SOURCE_NO_ISOLATED_CHANGE_OBSERVED` does not prove that the
editor inserted the cut, because sampling, transcoding, compression,
overlays or clock errors can hide original source transitions.
Original native frame timestamps remain unverified; no source-scene
exclusion, editorial positive/negative label or dense
OutputSourceTimeMap is established.

Evidence uses `bounded-output-source-visual-crosscheck-v1` and a new
source-side cache fingerprint dependency, with strict non-promotion,
per-peak interval, original/finished identity and cumulative count
validation. The automatic real-cut summary and offline diagnosis
surface separate **non-causal** source visual categories. Synthetic
bounded FFmpeg controls test a natural source transition and a
source without isolated change; they cannot certify actual Hololive
editor gold, true cut precision/recall or production readiness.
`ready=false` and all qualification gates remain unchanged.

## Increment 28: bounded visual-only source frame-sequence search (#144)

The protected two-sided cut-evidence stage now performs a second
independent analysis of output-only visual changes, using **only
already-download-bounded, checksum-verified measured frame manifests**.
It does not need a local audio offset or a calibrated AV origin to
rank source image matches *within those reserved sections*. The
original source section acquisition still depends on upstream
strongly matched audio; identifying totally unreserved source media
visually remains future work.

Per peak (maximum three peaks, five source sections, 20 sampled source
frames per section and 2 nearby output frames on each side), CPU
ORB/affine matching searches the permitted original sample images.
Total comparisons are capped at 2,400 and exceeded budgets fail
closed before feature extraction. A tentative association requires
two independent advancing original/output frames per side,
distinct frame bytes, sufficiently coherent sample-time progression
and rejection of repeated source imagery beyond one second.
Sparse coverage, missing transition-adjacent samples or competing
equally plausible original times yield UNKNOWN.

A visually supported temporal jump, continuity or source-section
switch is recorded as a **hypothesis** only. Group-local original
timestamps are from re-encoded bounded sections, not verified
livestream-native timestamps; two-sided sparse correspondences
cannot prove an edit cut was made or distinguish source-camera
cuts from edited overlays and gameplay events. The source-side
pipeline and the downstream partial-map stage both fingerprint
`bounded-visual-only-frame-search-v1` and reject stale evidence;
the reference-free diagnostic and offline triage recompute
per-peak counts with strict no-promotion guards. No source/finished
dense time map, reviewer gold, precision/recall, provider
qualification, rights override or production availability changes.
Production remains `ready=false`.

## Increment 29: unattended candidate discovery for missing originals (#145)

A critical blocker before local visual source-frame search is often the
absence of a uniquely identified original livestream URL. The
corpus-growth metadata resolver now records a capped, automatically
discovered *unverified source shortlist* instead of discarding
potential source leads or guessing a source identity. When a clip
description supplies multiple ambiguous explicit source URLs, the
scout probes those candidates first; when there is no source link, it
uses at most two bounded title-derived public video search queries.
At most six metadata calls are made, filtering out the same clip,
unavailable/live material, sources shorter than a plausible full
original and candidates not metadata-policy-allowed. The returned
shortlist is durable and includes per-candidate origin and
`visual_correspondence=NOT_MEASURED`.

This does **not** turn searchable metadata into verified video
correspondence. Even a single remaining candidate is only a
proposal, not source gold. The corpus item becomes `source_visual_pending` when a nonempty candidate
shortlist exists (otherwise `review`), while its canonical `source_url`
and `source_stream_id` remain null,
and it is never added to training or the heldout reservation. The
scout is a low-cost preparatory step for future *visual confirmation
of candidate originals* and does not bypass preexisting rights,
split, audio-anchor, frame-PTS or editorial quality gates.
All `visual_correspondence_tested`, `source_identity_verified`,
`reviewed_gold_available`, `event_split_verified` and
`qualification_allowed` flags are false; `ready=false`.

## Increment 30: bounded unattended verification of missing-source candidates (#146)

`source_visual_pending` now has a real bounded video evidence stage.
During ordinary corpus-growth cycles a single pending item (configurable
CLI cap 1..2) is checked without manual URLs or separate confirmation.
At most two shortlisted source candidates are tried per item, each
with three 12-second original-video windows (beginning, middle and end),
plus one 12-second centered finished clip. When more than two original
candidates are shortlisted, a persisted round-robin candidate cursor
moves through all entries across autonomous cycles. A persisted per-source
breadth-first cursor rotates to previously unsampled 12-second locations
on subsequent cycles. A completed coarse scan remains UNKNOWN, never
proof of source absence. Before each bounded fetch:
canonical clip/source ID, metadata/public-ended status, source and
finished scoped rights policy and candidate duration are checked.
Acquisition uses only `acquire_video_range` and checks returned
identities, ranges, measured duration, video presence and a strict
64-MiB per-file limit. SHA256 digests of sampled source video and
finished video are included in the persisted evidence.

Sampled decoded frames are compared by the existing CPU ORB geometric
matcher. A tentative sequence requires at least three different,
temporally advancing and mutually consistent original/finished image
pairs in *one* sampled source range, rejecting repeated time locations,
static content, sparse frames and multiple matching source windows.
Results remain `VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD` or
a clearly distinguished UNKNOWN/BLOCKED state. An absent sampled
match is not a negative editor label or proof a proposed original is
unrelated. If multiple candidate originals are visually corroborated,
the decision remains ambiguous.

All saved evidence is **non-promotable**: both `CorpusGrowthItem.source_url`
and `source_stream_id` remain unset, `source_visual_pending` persists,
no source video is imported into learning or heldout partitions,
and no verified original native PTS, exact editor cut, dense time map,
reviewed gold, capability qualification or production readiness
is asserted. Earlier metadata-only candidate discovery and later
source identity decisions remain separate. Sampled visual confirmation
is an improvement, not proof of complete original-stream recovery.
`ready=false` and all existing quality gates remain unchanged.

## Increment 31: adaptive coarse-to-fine original-video sampling (#147)

The previous breadth-first scan was broad but never acted on weaker
image correspondence. The bounded short-video worker now records
`weak_source_window` **only** for two distinct, unique, temporally
advancing output/source frame pairs in one source section (not the
three image pairs required even for unreviewed corroboration).
Competing source sections, repeated/stationary frames, implausible
timing and decoder errors cannot create these hints. This remains
`UNKNOWN_NO_VISUAL_SEQUENCE`, never proof of source identity.

The sample scheduler binds the tentative hint to the source stream
ID, measured original duration, original coarse-scan cursor, finished
video SHA256 and the exact verified coarse windows. On the next
eligible visit to that original candidate, it probes at most three
**non-overlapping neighboring 12-second windows** (±12/24/etc.).
The hint is consumed after a successful complete probe and the next
visit **resumes the existing broad breadth-first cursor**. A new
hint cannot recursively monopolize source-video downloads. Source
candidate rotation is preserved. The per-visit maximum number of
bounded media requests does not increase.

Outdated, forged, impossible or internally inconsistent refinement
hints are rejected before any original source-media acquisition.
Media rights/public status/range policy, checksum/duration guards and
holdout/training boundaries are rechecked for every request. The
new method `adaptive-source-visual-refinement-v1` only alters where
small source sections are probed; there is **no source truth,
exact native PTS, precision/recall, editorial gold, or
production-ready route**, and `ready=false` remains unchanged.

## Increment 32: persistent two-sided independent visual corroboration (#148)

Previously `source_visual_pending` stored only the most recent short
source-video probe. Every cycle then erased the prior matched images and
the completed clip itself was always sampled near the center, so
multiple original matches could not demonstrate independent output
contexts. A bounded, persistent `source_visual_ledger.py` now
retains up to 32 *unreviewed* source/finished matched-window events
with source stream identity, exact sampled 12s original and finished
requested ranges, media SHA256s and checked local image-frame times.

The worker automatically rotates through up to six *nonoverlapping*
finished-clip sample windows across unattended cycles, while preserving
the original source breadth-first sampling and fair candidate rotation.
An outstanding one-shot weak original-video match pins the same
finished-clip sampling interval until the follow-up is consumed,
preserving the SHA-bound hint from #147. No second full-original
download is introduced; each run remains bounded.

Only three or more distinct temporally consistent image correspondences
may enter the ledger. Two-frame weak matches, synthetic diagnostics
without real frame timestamps, missing checksums, repeated exact
source/finished ranges, or changed underlying media cannot increment
verified event counts. The **independent two-sided window count**
greedily includes a new match only if both its finished clip section
and original source section do not overlap any already counted match
for that candidate. Different original candidates with visual
support remain `UNKNOWN_DIFFERENT_CANDIDATES_SHARE_VISUAL_EVIDENCE`,
not a winner. Two independent sampled video sections on one candidate
earn only `UNREVIEWED_TWO_SIDED_DIVERSE_VISUAL_HYPOTHESIS`.

Persisted ledger versions, owner IDs, candidate IDs, sample ranges,
SHA256 and timestamps, no-gold flags, aggregate counts and summary
are revalidated **before video network acquisition**; stale or
promoted evidence is blocked. Because native unmodified original-video
PTS and human-reviewed source/event gold do not yet exist, no
source identity, source time-map, clipping truth, training/holdout
assignment, precision/recall, or production readiness is asserted.
The production router remains `ready=false`.

## Increment 33: strict candidate-side visual evidence assessment (#149)

A bounded `source_visual_assessment.py` evaluates PR #148's historical
source/finished frame evidence into **auditably reproducible,
reference-free candidate hypotheses**, not a source probability. It
requires the full checked evidence ledger and the unchanged eligible
shortlist, and returns for each original candidate:
observed positive window count, independently nonoverlapping
two-sided window lower bound, distinct sampled finished/original
positive contexts, unreviewed support tier and explicit missing
coverage/negative evidence disclaimers. The entire list remains
eligible for unattended source probing.

Candidate states are only `UNKNOWN_NO_SAVED_POSITIVE_SOURCE_EVIDENCE`,
`UNREVIEWED_SINGLE_CONTEXT_ORIGINAL_HYPOTHESIS`,
`UNREVIEWED_DIVERSE_SINGLE_ORIGINAL_HYPOTHESIS`, or
`UNKNOWN_COMPETING_VISUAL_ORIGINALS`. The presence of two or more
original candidates with *any* positive visual evidence forces
`UNKNOWN_COMPETING_VISUAL_ORIGINALS`, even when one candidate has
multiple independent windows. No match is not a negative label, and
no fabricated calibrated probability, winner or selected original ID
is emitted. Each status names a permissible **next autonomous
verification task**, never an editorial decision.

The saved assessment is derived deterministically from the checked
ledger's canonical event digest and exact candidate roster. On the
next run it must match that ledger *before any media download*;
stale, altered, schema-invalid or positive-promoted snapshots are
rejected. After each completed candidate probe, the ledger and its
assessment persist together. The ledger additionally verifies that
the same requested finished 12s section has identical SHA256 across
ALL candidate originals, and the same original 12s section has
identical bytes across ALL finished section comparisons (not only
exact duplicated observation pairs).

This is measured, sparse **positive-only** evidence; source video
completion, native absolute PTS, reviewer source identity, reviewed
editorial gold, event split, clip accuracy and production qualification
remain unverified. Source IDs remain unset and `ready=false`.

## Increment 34: independent pixel/temporal countercheck (#150)

The source visual scanner previously used only ORB geometry to
nominate all three temporally coherent source/output frames in a
positive result. The new CPU-only `source_visual_pixel_check.py`
independently loads each measured decoded frame (up to six pairs),
requires saved frame SHA256 equivalence, and computes 63-bit
low-frequency grayscale DCT fingerprints over bounded scale/crop
views. It requires each proposed source-output diagonal to remain
perceptually close, and each pair to be distinctly closer than the
other sampled-time alternatives; both source and finished trajectories
must exhibit measurable visual change. An identity-like cached still,
a repeated shot, incorrectly assigned source images, corrupt PNGs,
or more frames than the budget now yield `UNKNOWN` even if ORB
geometry proposed a positive sequence.

The independent gate runs **before** returning
`VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD` and therefore before
new observations can enter the historical ledger. It is not a source
probability or classifier; valid editor zoom, reframe, overlay or
aspect ratio edits can still cause false UNKNOWN (reduced recall).
No absence of an independent match is proof a candidate is wrong.

Preexisting historical ORB-only observations are retained as
unreviewed evidence, **not retroactively labeled as DCT-confirmed**.
Automated local synthetic video/image fixtures exercise true matching
content (PNG/JPEG with minor overlay), completely different frames,
stationary views and checksum tampering; such fixtures are regression
tests rather than actual Hololive editorial precision/recall.
`source_identity_verified=false`, no native stream PTS,
`reviewed_gold_available=false`, and production `ready=false`
remain mandatory.

## Increment 35: measured acquired-video constructed controls (#151)

A key missing bridge between synthetic code fixtures and independently
reviewed Hololive editor gold is direct measurement of the matching
engine on **actually acquired bounded public video**. During an
existing `source_visual_pending` cycle, and only when the provider
is the real `YtDlpSourceProvider`, the worker performs one limited
self-supervised control check on already acquired, policy-allowed
original video windows (no additional source download). Saved
re-encoded source video bytes and up to four saved decoded source
frames must match their SHA256 checksums before testing. The first
constructed example JPEG-recompresses those *actual video frames*
with a tiny generated corner overlay (known provenance-based
correspondence). The second reverses temporal frame order while
preserving ascending clock labels (known constructed timing decoy).
Both go through the full ORB plus independent DCT temporal checker.

Constructed controls test whether the code detects known aligned
frames and rejects a known reordered sequence *on the measured
acquisition*. These are synthetic transformations of real-acquired
content, not independent original-video identity labels and **cannot
estimate editing precision/recall**, real original-source false
positive rate, or human cut accuracy. Highly repetitive scenes or
unavailable decoders result in explicit UNKNOWN, not artificial
failures. The source video is not certified as an official Hololive
livestream solely by yt-dlp availability.

Results are saved as a bounded and source-SHA-deduplicated
`real_video_control_history` and prevalidated for ownership,
content hashes, no-gold fields and no fabricated numeric editor
accuracy before further media download. A new zero-preparation CLI
`examples-source-controls-report` summarizes real acquired-video
control sample counts, constructed JPEG positive detection fraction
and constructed reversed-order decoy rejection fraction. If no real
provider observations exist, the report says UNKNOWN, measured
sample count zero and fractions NULL. `editor_cut_precision`,
`editor_cut_recall` and
`real_source_match_false_positive_rate` remain NULL in ALL
cases; reviewed gold, source identity, event split and route readiness
remain false. The pytest synthetic MP4s are regression checks, not
the qualifying real-stream measurement itself.

## Increment 36: actual-video pattern diversity audit (#152)

The previous actual-video JPEG/reversed-time self-controls (#151) could
not describe matching robustness across different kinds of editing,
or distinguish hundreds of sampled windows from hundreds of independent
source streams. The new actual bounded-source `source_visual_pattern_controls.py`
adds five local, label-constructed patterns to the same SHA256-pinned
already-downloaded frames: JPEG overlay, brightness shift, 1.08x
central crop/zoom, reversed timeline, and pairwise-swapped adjacent-frame timeline.
No extra original stream media is fetched and the verifier remains the
same ORB + independent DCT temporal sequence scorer as production.

Each case records its **expected constructed behavior**, observed
matcher status and either correct/incorrect or UNEVALUABLE. Only
explicit correspondence/noncorrespondence outcomes get denominators;
codec failures, missing frames and unknown budgets cannot inflate a
negative rejection fraction. Observations with any missing media/hash
provenance never enter the durable history.

A new `source_visual_pattern_report.py` stores up to 24 SHA-bound
per-clip pattern records; stale schemas, fake human gold,
fabricated precision metrics, inconsistent per-pattern judgments
and duplicate source/media identities are rejected *before* new
video downloads. `examples-source-controls-report` now also produces
`constructed_pattern_audit` with each pattern's observed, evaluable,
correct, incorrect and unevaluable counts, an honest fraction with
null for zero evaluated samples, distinct original candidate source
IDs, per-source edit-pattern results with separate denominators,
and a deduplicated total across clips that share exactly the
same original video bytes. Distinct samples on the same stream do
not count as multiple independent streams. Source diversity is never
marked sufficient for real-world generalization using only these
constructed controls.

**No real current public-video samples have been run in GitHub
CI by this implementation.** Unit/regression tests use synthetic
encoded/decoded MP4 data. Production observations require real
public/ended videos to be acquired by the existing unattended worker,
subject to unchanged policy and access restrictions. These constructed
edits are not independent human editor labels, cannot establish
Hololive source identity or real video-match false-positive rate,
cannot certify precision/recall, and do not authorize learning,
heldout gold or `ready=true`.

## Not yet implemented / acceptance work

1. **#107 / #99:** acquire bounded *original-source* ranges for reserved finished
   edits, verify original-clip alignment and cross-source event isolation, make
   independent weak references, adjudicate disputed cases without user-provided
   data, and calibrate against *genuinely*
   reviewed independent gold. If no legitimate reviewed gold exists, keep the
   affected capability unqualified rather than forge it.
2. **#109:** broaden verified no-payment transports and resilient cross-vendor
   independent judgements, while keeping all account-bound evidence gates.
3. **#80 / #81-#89:** preserve finished video, learn source-to-finished edit
   transforms and implement deterministic RenderPlan, renderer, QA/repair.
4. Instrument unattended evidence coverage, split conflicts, source/event
   diversity, annotation uncertainty, independent grading accuracy, quality
   floors and clips/day under actual free quota.
5. Validate on real unseen Japanese zatsudan, game UI, Live2D, visual gags and
   cross-member sources; do not treat fixture-only passing CI as live quality.

**Readiness:** automatic metadata holdout reservations do not make any production
route ready. Do not reclassify preexisting learning media as held out.
