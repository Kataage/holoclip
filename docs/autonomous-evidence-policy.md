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
