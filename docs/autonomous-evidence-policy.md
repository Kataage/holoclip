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
