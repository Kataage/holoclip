# holoclip

Zero-cost provider comparisons and evidence-bound qualification are documented in
[the real-media benchmark workflow](docs/provider-benchmark.md). A free API smoke
response does not grant production quality qualification.

Holoclip aims to learn selection and editing from human-made hololive clips, understand
unseen streams through audio and video, and drive deterministic rendering with automatic
QA and repair. Its cloud AI requests must stay within verified zero-cost routes.

The first milestone is deliberately narrow:

> Given one long stream, surface a small set of moments that a human would genuinely consider worth clipping, and explain the story structure behind each one.

Video editing, thumbnails and publishing are intentionally deferred until this ranking problem is proven.

## Current v0.1 flow

    local media file
        -> FFmpeg audio chunks
        -> qualified ASR segment timestamps
        -> overlapping transcript chunks
        -> broad discovery with qualified text_reasoning_fast + structured_json
        -> overlap dedupe
        -> editorial selection with qualified text_reasoning_strong + structured_json
        -> measured boundary / multi-cut plan when learning is ready
        -> saved candidates and automatic evaluation artifacts

Production AI access uses capability-based, quality-qualified routing with a persistent
quota ledger and bounded stage timeouts. Missing qualified/free routes stop processing
with preserved artifacts. See [production provider setup](docs/production-provider-runtime.md).

## Unattended data and evaluation policy

For measured clip-to-original correspondence (no cloud AI inference),
the optional `media` extra provides NumPy alongside PySceneDetect and OpenCV:

```powershell
uv run --extra media holoclip examples-grow-corpus --discover-only
uv run --extra media holoclip examples-prepare-holdout --max-clips 3
uv run --extra media holoclip examples-align-holdout --max-clips 3
```

The next optional holdout diagnostic can narrow acoustic edit transitions
to **conservative candidate time brackets**, not exact cut frames:

```powershell
uv run --extra media holoclip examples-refine-cut-candidates
```

The command uses earlier source-audio and measured source/finished video
evidence, without additional downloads or paid AI inference. Acoustic
silence, ambiguous matches, visual-only edits and unavailable absolute
source timestamps remain explicit unknowns.

### Real-media reserved holdout validation (one command)

Instead of manually invoking every 9-stage diagnostic, run the existing
reserved public human-made clip/source evidence pipeline in one bounded,
checkpointed local command (Windows `uv`, FFmpeg, local CPU; no paid AI):

```powershell
uv run --extra media holoclip examples-validate-holdout-e2e --preflight-only
uv run --extra media holoclip examples-validate-holdout-e2e --max-clips 3
# Autonomous bounded multi-batch validation, for reserved real clip/source evidence:
uv run --extra media holoclip examples-validate-holdout-e2e --all-batches --max-clips 3 --max-batches 8
# After interruption or reaching a finite batch budget, replay all previously
# successful reserved-video batches with full nine-stage validation, then
# continue up to the NEW total max-batches budget:
uv run --extra media holoclip examples-validate-holdout-e2e --all-batches --resume-checkpoint --max-clips 3 --max-batches 12
# Optional: examine other reserved clips even when one original audio cannot
# be uniquely matched. Other failures (policy, identity, media, exceptions)
# ALWAYS stop. Bounded to one clip per batch, up to 12 clips per invocation.
uv run --extra media holoclip examples-validate-holdout-e2e --all-batches --continue-on-insufficient-audio --max-batches 12
```

The first invocation checks local holdout reservations and learning-stream
conflicts **without downloading anything**. The second sequentially runs
finished-video preparation, source audio alignment, bounded source video
measurement, visual geometry, acoustic cut-bracket observations, original
section audio clock calibration, source↔finished frame links, candidate
two-sided cut evidence, and partial time-map candidates. It automatically
reuses each stage's verified cached media, but cannot continue through an
upstream stage without successful measurements. Real public media downloads
may take significant time; the input reservations must already exist.

If the reservation list contains more clips than `--max-clips`, each
invocation automatically processes one bounded batch, prioritizing clips
without a completed final-stage record. Re-run the same command to advance
through later batches; original corpus reservations and learning history
are not edited. The projected batch state is kept locally at
`data/provider-benchmark/holdout/holdout-e2e-batch-state.json`.
`--preflight-only` writes a **separate** `holdout-e2e-preflight.json`
without overwriting the latest real run audit. Overlapping runs fail
closed behind a local `.holdout-e2e.lock`; if an unclean crash leaves
that lock, inspect the process before removing it. If the original
reservation state changes during processing, the batch stops instead of
claiming that the old source assignment was validated.

For minimal intervention, use `--all-batches` to process distinct
reserved clips sequentially in one invocation, at most `--max-batches`
(default 8; maximum 12) times the `--max-clips` per-batch budget.
If a *single* clip lacks independently unique original audio evidence,
the optional `--continue-on-insufficient-audio` flag can defer **only**
`INSUFFICIENT_UNIQUE_ANCHORS` or `INSUFFICIENT_AUDIO` and examine another
reserved clip without any automatic retry. For truthful clip attribution,
this mode forces **one clip per batch**, regardless of `--max-clips`,
and the finite `--max-batches` budget still applies (maximum 12).
Source/clip rights restrictions, train/holdout split collision, original
identity mismatches, corrupt files, missing reports, unexpected exceptions,
and other failures remain immediate hard stops. Deferred clips remain
**unverified**, appear by ID in both run and offline diagnosis audits, and
never count as successful nine-stage measurements. The final status is
`UNREVIEWED_INSUFFICIENT_AUDIO_RECORDED`, not a complete/ready state,
if the bounded invocation examined every reservation but some audio
correspondence was insufficient. For ordinary use, leave this opt-in
disabled to retain the original fail-on-first-mismatch behavior.

If Windows restarts or a finite `--max-batches` budget is reached,
`--resume-checkpoint` can recover the last completed-batch selection
order from the prior `holdout-e2e-all-batches.json`. Crucially, it
**replays all nine real stage checks for every historical completed
batch**, including media SHA/policy/source guardrails, before attempting
a new reservation. Successful cached expensive work may be reused by
individual stages only after their own integrity checks. This is
**not** blind trust of the JSON audit or a shortcut to reviewer gold.

Checkpoint replay refuses changed reservation-state SHA, changed ordered
rights/policy rules, mismatched stage schema/execution mode, forged stage
tallies, duplicate clip IDs, or previous failed/deferred batches. Existing
audits from before this feature lack the policy digest and therefore must
be rerun normally; unsafe resume does **not** overwrite the saved audit
and writes `holdout-e2e-resume-refusal.json` instead. During replay of
prior successful batches, the full original checkpoint stays intact so
an unexpected second interruption does not erase it. The maximum of
12 batches applies to the *total revalidated + newly executed* batch
count; increase `--max-batches` only within that fixed upper bound.
When the checkpoint has failed/deferred clips, use normal diagnosis and
a fresh strict trial rather than skipping over them.

The run holds one exclusive lock across all batches, fails closed
at the first blocked/quarantined/insufficient step, and never retries a
failed batch indefinitely. It writes a **separate cumulative**
`data/provider-benchmark/holdout/holdout-e2e-all-batches.json`
checkpoint after every successful batch, recording all selected clip IDs,
remaining reservations, each batch's stage statuses and candidate-only
observed totals. If the configured invocation budget cannot cover all
reservations, the status is `BOUNDED_BATCH_LIMIT_REACHED` (not success).
Use `--preflight-only` separately, before full media execution.
This is an evidence/diagnostic trial, not a production qualification.

The `--all-batches` validation command now **automatically** runs offline
triage after success or a blocked batch, printing the first blocker and
next-action code without another media download or manual command.

After the real-media trial, get a source-state-checked offline failure diagnosis
without re-downloading or re-encoding any files:

```powershell
uv run --extra media holoclip examples-diagnose-holdout-e2e
```

This writes `data/provider-benchmark/holdout/holdout-e2e-diagnosis.json`
with the first failed stage, fixed next-action code, stage status totals,
original reserved clip IDs not yet attempted, and whether the saved audit
still matches the exact reservation-state SHA-256. When invoked from the CLI, offline diagnosis also checks the saved
rights-policy SHA-256 against the current `--policy-file` (default
`data/policy/clip_rules.jsonl`). A changed policy or legacy audit without
this fingerprint is flagged rather than presented as a current successful
measurement. The diagnostic prefers
the cumulative multi-batch report, never double-counts the latest
individual batch, and refuses missing, stale, tampered or older unauthenticated
audit schema. `SIMULATED_TRIAL_ONLY` explicitly differentiates fixture
tests from actual media runs. Diagnosis **only reads saved reports and
metadata**; it does not re-verify media files or imply reviewer/editor gold.
A diagnostic result is never production qualification.

The local `data/provider-benchmark/holdout/holdout-e2e-audit.json` records
each stage's *actual* per-clip status, observed candidate coverage, frame
links, candidate edit discontinuities and blockers. Precision/recall,
native timestamp error and event-independent quality remain null until
there is independently reviewed real-media gold. Successful stage
execution is **not** production acceptance. The Git repository includes
example metadata but does **not** include the user's locally downloaded
real source/video files or completed private holdout checkpoints.

Downstream bounded-original-video acquisition now additionally validates
the exact current acoustic sampling method (`bounded-observed-audio-islands-v3`)
and independently **reconstructs each claimed match range from its
underlying strong six-second anchors**. It rejects an old 25-second
unsupported gap even if saved JSON claims a continuous match, as well
as forged range endpoints/counts, before any source-video download.
Prior source-video pair caches lacking the sampling-method stamp are
invalidated and remeasured under current source rights and hash checks.
This protects direct CLI stage execution, not just the nine-stage runner.

The acoustic cut refinement used by the same nine-stage holdout trial now
preflights **all** coarse audio-supported regions against its bounded
sampling-point budget. When 1-second spacing would exceed the budget, it
can use 1.5- or 2-second spacing across *every* supported section, always
sampling each section's final eligible audio window. If even 2-second
sampling cannot cover all ranges, the entire refinement remains
`UNKNOWN_SAMPLING_BUDGET` with no partial cut claims. The saved
`refinement.sample_spacing_seconds`, `planned_points`, and
`sampling_strategy` make the tradeoff visible; wider spacing can still
miss short transitions. This is not native frame-accurate editing gold.

The refinement schema is now `bounded-acoustic-cut-brackets-v2`.
Previously measured v1 cut/source-side/time-map cache chains do not
qualify as v2 measurements; rerun the already documented bounded holdout
command to recompute them with current provenance guards.

For **actual unreviewed human-edited holdout clips**, the existing
`examples-validate-holdout-e2e` nine-stage command now also emits
`observations.real_cut_observations` (per selected clip) and
`observations.real_cut_observation_totals` inside its saved
`holdout-e2e-audit.json`; `--all-batches` also preserves every
successfully measured clip and cumulative reference-free totals in
`holdout-e2e-all-batches.json` instead of keeping only the latest batch.
This describes *measured candidate signals*:
acoustic candidate counts, unknown audio transitions, median/P90 bracket
width, overbroad brackets (>8s), output-video change corroboration, and
independently matched original/finished two-sided visual candidates.
Nonunique/missing acoustic support or an exhausted audio-sampling budget
is **unmeasured** (null counts), not evidence of zero detected cuts.
A missing/old method or stale/mismatched stage provenance gives a
separate unverified/null result, **never a zero or a perfect score**.
Only reports for stages executed in the current invocation are included,
so an old successful visual result cannot validate a new cut result.

These real-video descriptive counts are **not** measured human-editing
precision/recall, independent reviewer gold or frame-accurate cut time;
they leave `editor_cut_precision`, `editor_cut_recall`,
`ready`, and all production/gold claims null or false. They do not
require a user-provided URL, manually labeled cut list, additional
download, extra model call, or a new Windows command. See the
[autonomous evidence policy](docs/autonomous-evidence-policy.md).

After a real or interrupted `--all-batches` run, the existing automatic
offline diagnosis now includes `cut_signal_triage` in
`holdout-e2e-diagnosis.json`: bounded, deterministic per-clip codes for
unsupported audio, >8-second candidate intervals, unknown transitions,
missing output-video support and missing original/finished two-sided
corroboration. It validates recorded clip IDs, non-promotion flags,
per-batch vs cumulative counts, and plausible measurements **before**
reporting the codes. Mutated or incomplete #139 diagnostics fail closed
as `BLOCKED_INVALID_AUDIT`; older audit files without #139 fields say
`LEGACY_NO_CUT_OBSERVATIONS`. Simulated tests remain labeled simulated.
This is inspection of saved JSON only; it does **not** reopen media,
self-calibrate thresholds, change original audit files or provide
human-cut precision/recall. The next-action code for earlier stage
blockers retains precedence over descriptive cut-signal triage.

For quantitative validation of known **synthetic FFmpeg edit scripts**,
the offline `examples-score-scripted-cuts` command now compares scripted
hard-cut output times with saved unreviewed acoustic cut intervals. It
counts one-to-one matched scripted cuts, unmatched candidate
**false positives**, missed scripted **false negatives**, per-candidate
bracket width and absolute midpoint error. A broad interval over 8s is
counted as an **unmatched false positive**, even if it covers a true
scripted boundary; a single interval may never count as multiple hits.
A continuous, unedited synthetic excerpt provides a zero-cut negative
control. A missing denominator is reported as `null`, not perfect
precision or recall.

The reference file must explicitly declare its **synthetic construction**
origin. For example, on an actual locally executed 44-second two-piece
FFmpeg fixture with its scripted join at output t=22s:

```json
{
  "version": "scripted-ffmpeg-cut-reference-v1",
  "reference_kind": "SCRIPTED_FFMPEG_ONLY",
  "clip_id": "youtube:aaaaaaaaaaa",
  "duration_seconds": 44,
  "cut_seconds": [22]
}
```

Save that exact file separately as a `scripted-reference.json` only when
the media was in fact constructed by that script, then run:

```powershell
uv run holoclip examples-score-scripted-cuts --reference scripted-reference.json --cut-report data/provider-benchmark/holdout/cut_brackets/holdout-cut-brackets.json --output data/provider-benchmark/holdout/scripted-cut-scores.json
```

This scorer performs **no downloads, model inference, FFmpeg or media**
processing, does not mutate the saved media audit or training corpus, and
**never reports human editor-gold precision/recall, production readiness
or native frame-exact cut positions**. Existing real Hololive clips still
require independent manual/editor-reference annotations for genuine
held-out quality estimation.

The acoustic matcher refuses to call an entire passage supported merely
because two 6-second independently matching fragments have the same
original-source offset: gaps exceeding four seconds without a verified
anchor form explicit separate acoustic islands. This guards against
equally long silence/narration/BGM replacement falsely appearing as a
complete uninterrupted original source range; source audio caches using
the previous 25-second gap allowance are invalidated.

The integration suite also exercises a reencoded **three-piece**
48-second video with test-only known edited boundaries at 16s and 32s.
This validates synthetic cut bracketing, not real Hololive accuracy.

The offline FFmpeg integration suite also covers a real re-encoded
**two-piece** source↔finished edit (original 9–31s followed by 44–66s)
with a known scripted cut at edited t=22s, with and without quiet BGM.
That controlled test must keep edit-cut hypotheses and timing-map outputs
non-promotable; it is **not** evidence about real human-edited Hololive
videos or production acceptance.

After gathering grounded original↔finished frame and cut evidence,
create a **strictly diagnostic** partial output/source time map. Short
interval candidates must be supported by independently unique audio
and two ordered visual frame links, while uncertain gaps are retained:

```powershell
uv run --extra media holoclip examples-build-partial-time-map --max-clips 3
```

The output is not a verified dense OutputSourceTimeMap and is never
used as frame-accurate edit instructions, learning gold or production
provider evidence. No manual clip URL/timestamps or paid AI models.

To distinguish strong candidate edit discontinuities from unsupported
scene-change guesses, inspect the *actual bounded original and finished
video pixels* on both sides of the previously observed acoustic cut:

```powershell
uv run --extra media holoclip examples-compare-cut-sides --max-clips 3
```

This uses only previously downloaded section media and checks independent
frame matches, repeated content, rights and time-consistency. It produces
candidate observations, **not** verified edit-cut frames, human editorial
intent, a dense OutputSourceTimeMap or production-quality gold.

After the bounded source-section clock has been checked, local ORB/RANSAC
can compare real original/finished **sampled image frames** against the
independent audio timeline and reject repeated visual content or time
contradictions:

```powershell
uv run --extra media holoclip examples-correlate-source-frames --max-clips 3
```

Partial links retain exact measured frame IDs and *approximate* source/
clip time evidence, not a dense source-to-output time map, confirmed cut
frames, human-reviewed gold or production readiness.

After original-video sections are captured and measured, their **actual
audio** can be independently matched against the source stream's cached
full-audio fingerprint, with bounded section native-frame PTS inspection:

```powershell
uv run --extra media holoclip examples-calibrate-source-clocks --max-clips 3
```

This removes blind reliance on yt-dlp's requested source seek position when
building *approximate* source-to-finished time provenance. Audio/video codec
offsets, absolute original native frame PTS, exact edit-cut time and editor
intent still require independent validation. The command does not certify
them or create semantic gold.

The audio cut-interval command now additionally performs **bounded,
finished-video visual jump checks** (8 fps, max 10-second padded interval)
as weak corroboration. Results include explicit unknowns for natural source
scene changes, exact frame PTS and unmatched or visually continuous edits;
no verified gold or edit-frame timestamp is asserted.

After the sparse source/finished visual observations, candidate cut
intervals can be refined using denser local acoustic correlations without a
paid inference service, manually provided timestamps, or full-source video:

```powershell
uv run --extra media holoclip examples-refine-cut-candidates --max-clips 3
```

This records **time ranges** where uniquely matched audio changes source
offset; it does not certify exact-frame edit cuts, time-map completeness,
visual confirmation or human-review quality.

Source-vs-finished visual geometry can now be inspected after bounded
source-pair preparation without a cloud AI key, user-authored labels or
manually selected frame IDs:

```powershell
uv run --extra media holoclip examples-inspect-visual-edits --max-clips 3
```

This uses measured ORB/RANSAC frame correspondences to propose zoom/reframe
candidates with geometry/inlier diagnostics. It **does not** certify an exact
cut map, text/subtitle recognition, verified edit intent, or production gold.

After audio anchors have been established, the optional
`examples-measure-source-pairs` stage requests only the identified bounded
original-video sections and saves checksum-verified frame/shot evidence paired
with measured finished-clip windows:

```powershell
uv run --extra media holoclip examples-measure-source-pairs --max-clips 3
```

Unverified absolute seek offsets, exact cuts, visual edit semantics and semantic
gold remain explicit unknowns. The paired measurements cannot promote a
production AI route.

`examples-align-holdout` downloads original-stream **audio**, rechecks
rights/source identity, then records unique FFT-matched audio anchors and
approximate clip-to-source ranges. It rejects ambiguous matches; it does
not claim frame-accurate edit cuts, visual truth or reviewed gold.

Once new holdout source streams have been reserved by
`examples-grow-corpus --discover-only`, run:

```powershell
uv run holoclip examples-prepare-holdout --max-clips 3
```

This automatically retrieves permitted public finished clips and creates
resumable, checksum-verified video/scene/frame measurements. No cloud AI key or
manual reference annotations are required for this stage. Measurements remain
unreviewed and cannot qualify a production model; corresponding original-source
edit mapping, cross-stream event isolation and calibrated gold still remain.


Holoclip discovers public human-made clips without requiring the operator to
supply training URLs. New source streams are automatically reserved for later
holdout evaluation **before** learning ingestion; clips from the same source
stream cannot be split across train and reserved holdout. The status can be
inspected offline with `uv run holoclip examples-holdout-status`.

Reservations are not reviewed gold and cannot enable production inference.
The source/event-leakage checks, real-media gold calibration and full autonomous
evaluation remain under development. See
[autonomous evidence policy](docs/autonomous-evidence-policy.md). If the
evidence is uncertain, Holoclip blocks rather than asking the operator to
fabricate labels or lowering the quality floor.

## Corpus editorial learning

Holoclip now learns an explicit corpus-level editorial model from trusted aligned human-made clips.
Individual example retrieval remains supporting evidence and is not called learned editorial intelligence.

The autonomous flow is:

    human-made clips -> source alignment / unchanged quality gate
        -> deterministic full learning cards and contextual omissions
        -> cached per-card contrast / measured beat observations (qualified fast reasoning)
        -> bounded batch pattern induction (qualified strong reasoning)
        -> bounded cross-batch mechanism consolidation (qualified strong reasoning)
        -> automatic support evidence assessment
        -> persisted Editorial Profile and recomputed readiness
        -> profile-guided broad discovery / final selection / measured edit plan

```powershell
uv run holoclip examples-learn-profile
uv run holoclip examples-learning-status
uv run holoclip examples-grow-corpus --cycles 20
```

The last command searches public human clips without requiring clip URLs, resolves their sources,
quarantines uncertain/policy-review examples, acquires audio, transcribes, aligns, enriches, indexes,
and refreshes the profile. Each cycle is bounded and resumable. Continue beyond the initial target
with `--target-trusted 100 --target-streams 50`. `--discover-only` performs metadata discovery only.

Normal `analyze` and `discover` refresh missing/stale profiles using stage caches after trusted
examples change. Profiles that are unready or have stale provenance are ignored automatically.

The minimum production gate is 30 trusted clips, 15 trusted source streams, 5 core patterns and
80% trusted-example coverage by core patterns. Core requires 3 trusted clips from 2 distinct streams;
code derives counts, checks cited IDs and automatically assesses actual supporting evidence.
Emerging mechanisms and unsupported principles do not drive production discovery.

The initial local corpus has **11 clips, 9 trusted, 2 review, 10 total source streams and 8 trusted
source streams**. It is **not ready**. Existing accept/maybe/reject labels are frozen validation
records; `label` and `review` refuse new labels while readiness is false.

Once learning is ready, run an automated comparison on an unseen transcript:

```powershell
uv run holoclip editorial-ab unseen.json --stream-id unseen-source-id
```

This checks training leakage and compares identical discovery/selector settings with profile ON/OFF.
Its blinded LLM diagnostic is not human acceptance or proof of generalization. Optional human
confirmation belongs at the end and should use only a small blind sample.

See [editorial learning](docs/editorial-learning.md) for provenance, cache, boundary and scaling contracts.

## Setup

Python 3.12+ and FFmpeg on PATH are required.

~~~powershell
git clone https://github.com/Kataage/holoclip.git
cd holoclip
uv sync --python 3.12 --extra dev
copy .env.example .env
~~~

Set these values in .env:

~~~text
NVIDIA_API_KEY=...
GROQ_API_KEY=...
~~~

AI requests enforce [ZERO_COST_ONLY](docs/zero-cost-runtime.md). Supply current
credential-bound free-account evidence and declare public workflow data before
running inference. Model/endpoint overrides must match the verified registry.
Missing evidence blocks safely; no billing activation or paid fallback exists.

## One-command discovery

For local media:

~~~powershell
uv run holoclip discover path\to\stream.mp4
~~~

This automatically:

1. extracts lightweight audio chunks with FFmpeg
2. transcribes them through a qualified ASR capability route
3. writes a timed transcript under data/transcripts/
4. runs NIM candidate discovery
5. writes an evaluation run under data/evaluation/runs/

Operator review is deferred until corpus learning is ready and an automatic unseen-stream ON/OFF comparison has run. Existing labels are retained for later validation. Optional final review commands are described below:

~~~powershell
uv run holoclip review data\evaluation\runs\stream.json
~~~

During review:

- `p`: play only the proposed source segment (with a small context pad) using FFplay
- `c`: show surrounding transcript
- `a`: accept
- `m`: maybe
- `r`: reject
- `s`: skip
- `q`: stop

New evaluation runs created by `discover` remember the source media path. For an older run, supply it explicitly:

~~~powershell
uv run holoclip review data\evaluation\runs\stream.json --media path\to\stream.mkv
~~~

## Transcription only

~~~powershell
uv run holoclip transcribe path\to\stream.mp4
~~~

By default, long media is converted to 15-minute 16 kHz mono MP3 chunks before upload. This keeps local compute low and keeps individual uploads comfortably small.

## Analyze an existing transcript

~~~powershell
uv run holoclip analyze path\to\transcript.json --top-k 5
~~~

The transcript format is:

~~~json
{
  "segments": [
    {
      "start": 0.0,
      "end": 5.2,
      "text": "今日はちょっと話したいことがあって"
    }
  ]
}
~~~

Timestamps are seconds from the start of the source.

## Evaluation

Calculate current metrics:

~~~powershell
uv run holoclip metrics --runs-dir data\evaluation\runs
~~~

See docs/evaluation.md for the validation protocol.

## What this does not do yet

- acquire arbitrary third-party YouTube media
- build the existing-clip human-sense database
- render videos
- publish videos

Source acquisition remains isolated from the ranking core so the project does not depend on one platform-specific extraction method.

See docs/architecture.md, docs/evaluation.md and docs/roadmap.md.


## Human-made clip example store

Existing human-made clips are used as weak supervision rather than model fine-tuning.

Import a JSON manifest:

~~~powershell
uv run holoclip examples-import data\examples\human_clips.example.json
~~~

The validated examples are stored by default in:

~~~text
data\examples\human_clips.jsonl
~~~

Inspect coverage:

~~~powershell
uv run holoclip examples-stats
~~~

The schema keeps clip/source provenance, resolved source timestamps, abstract story structure,
members/topics/traits, relationship context, normalized performance metadata, and alignment
confidence. Source timestamps may be omitted initially and filled by the planned automatic
clip-to-source transcript alignment stage.


## Align a human-made clip back to its source stream

For the first local learning test, supply a local human-made clip file and an already-created
transcript of the original source stream:

~~~powershell
uv run holoclip examples-align path\to\clip.mp4 data\transcripts\source-stream.json
~~~

The command:

1. transcribes the clip through a qualified ASR capability route (and caches that transcript)
2. matches multiple spoken-text anchors against the source transcript
3. estimates the original source start/end timestamps
4. prints confidence and a few anchor matches
5. does **not** write to the learning store by default

After the alignment looks correct, rerun with provenance and `--save`:

~~~powershell
uv run holoclip examples-align path\to\clip.mp4 data\transcripts\source-stream.json ^
  --clip-url "https://www.youtube.com/watch?v=CLIP_ID" ^
  --clip-title "clip title" ^
  --source-url "https://www.youtube.com/watch?v=SOURCE_ID" ^
  --stream-id "SOURCE_ID" ^
  --save
~~~

If confidence is below the default threshold (0.55), the example is stored without accepted
source timestamps so it cannot silently become trusted training data. The threshold can be
changed with `--min-confidence`.

This alignment stage is intentionally local-first and independent of YouTube acquisition.
A future SourceProvider can supply the same media/transcript inputs without changing the
alignment or retrieval logic.


## Enrich an aligned human clip into a learning example

After a real human-made clip has been aligned and saved, enrich it with the strong editorial
model:

~~~powershell
uv run holoclip examples-enrich <EXAMPLE_ID>
~~~

This adds:

- abstract setup / development / payoff
- why a human editor likely selected the event
- reusable structural traits
- members and topics when supported by the transcript
- relationship context
- context requirement
- adjacent source windows as **weak negatives**

Weak negatives are deliberately labeled weak: a nearby region not selected by this particular
clip editor is useful contrast, but it is not treated as proof that the region is objectively
uninteresting.

The enrichment records the model and extraction method so later retrieval decisions remain
auditable.


## Multi-cut alignment

Human-made clips often remove long stretches from the original source or join several source
moments into one clip. Alignment v2 therefore stores an explicit edit map instead of assuming
one continuous source range:

~~~text
clip 00:00-00:30 -> source 10:00-10:30
clip 00:30-01:00 -> source 20:00-20:30
~~~

`examples-align` prints every detected segment and, when saved, writes them to
`alignment_segments`. The legacy `source_start/source_end` fields remain as a bounding range
for backward compatibility.

During enrichment, source gaps between selected segments are preserved as
`removed_between_selected_segments` weak negatives. This lets later retrieval learn not only
what human editors selected, but also what they deliberately skipped between retained moments.


## Migrate existing local data to the canonical store

The current JSON/JSONL evaluation and human-example files remain valid during the migration
period. Import them into the canonical SQLite store without deleting or rewriting the originals:

~~~powershell
uv run holoclip migrate-legacy
~~~

Defaults:

~~~text
evaluation runs: data/evaluation/runs/
human examples: data/examples/human_clips.jsonl
canonical DB:   data/holoclip.db
~~~

Use `--strict` when migration diagnostics should fail the command on ambiguous or malformed
legacy rows. Migration is idempotent: rerunning the same import updates the same canonical IDs
rather than creating duplicate entities.

Legacy feedback JSONL remains in active use for the current review/metrics workflow because old
feedback records do not contain a trustworthy creation timestamp. Holoclip does not invent one
during migration.


## Structural retrieval from human-made clips

After an aligned human clip has been enriched, Holoclip automatically refreshes the derived
structural retrieval index. Positive examples are embedded from abstract editorial structure
(setup, development, payoff, selection reason, traits, topics and relationship context) rather
than clip title wording. Weak negatives are indexed separately.

Production selects a benchmark-qualified `embedding` capability route and pins the
`mesh:<provider>:<model>` vector-space namespace. The NVIDIA Nemotron Embed 1B endpoint
is a diagnostic candidate; a returned vector does not qualify it. Changing the model
requires a separate index rebuild. Vendor-specific model overrides cannot bypass
the production router or mix vector spaces.

Manual rebuild/debug commands:

~~~powershell
uv run holoclip examples-index
uv run holoclip examples-retrieve "unexpected confession with setup and payoff"
~~~

The index is derived state in `data/holoclip.db`; the HumanClipExample store remains the
source material for rebuilding it. Re-indexing unchanged examples reuses their existing
vectors, changed structures are re-embedded, and documents belonging to removed examples are
removed from the active derived index.

## URL-based human clip ingestion with yt-dlp

Holoclip can now use yt-dlp through the replaceable SourceProvider boundary to turn a public
human-made clip URL into a weak-supervision example.

For the common case:

~~~powershell
uv run holoclip examples-ingest-url "https://www.youtube.com/watch?v=CLIP_ID"
~~~

The command:

1. reads clip metadata with yt-dlp
2. looks for an unambiguous original/source YouTube link in the clip description
3. downloads the clip and original source using the training-audio profile
4. transcribes both with the existing resumable ASR capability pipeline
5. aligns the human clip back to source timestamps
6. writes/updates HumanClipExample
7. extracts setup/development/payoff/selection reason and weak negatives with the strong LLM
8. refreshes the structural retrieval index

If the description has no unique source, specify it explicitly:

~~~powershell
uv run holoclip examples-ingest-url "https://www.youtube.com/watch?v=CLIP_ID" ^
  --source-url "https://www.youtube.com/watch?v=SOURCE_ID"
~~~

Training acquisition intentionally downloads best audio rather than full-resolution video because
transcription/alignment do not need the full video stream. The source cache is separated by
profile:

~~~text
data/sources/examples/clips/<id>/training_audio/
data/sources/examples/sources/<id>/training_audio/
~~~

A future editing-video acquisition can coexist under editing_video/ without mistaking audio
for a completed video download.

Holoclip invokes yt-dlp with --ignore-config so user-global yt-dlp settings cannot silently
change the deterministic training pipeline. It does not enable browser cookies or authenticated
access by default.


### yt-dlp runtime freshness policy

yt-dlp is intentionally **not pinned as a normal Holoclip Python dependency**.

For normal source acquisition Holoclip uses uv/uvx as a managed tool runtime:

- default channel: `nightly`
- each new Holoclip source-ingest process refreshes yt-dlp package metadata
- the selected-channel yt-dlp requirement is then run from uv's offline warmed cache for the lifetime of that provider/job
- if extraction fails, Holoclip performs one forced runtime refresh and retries once
- the exact yt-dlp version, channel, and JavaScript runtime are stored in human-example acquisition provenance

This avoids keeping a stale downloader in a long-lived project environment while still making one
ingest job reproducible.

Check the source runtime without downloading media:

~~~powershell
uv run holoclip source-doctor
~~~

The default can be changed for debugging/reproducibility:

~~~powershell
set HOLOCLIP_YTDLP_CHANNEL=stable
uv run holoclip source-doctor
~~~

For reliable current YouTube extraction, Holoclip also checks for a supported JavaScript runtime.
Deno >= 2.3 is preferred by yt-dlp; Node >= 22 is also supported and is enabled explicitly by
Holoclip when selected. The managed yt-dlp package uses the `default` extra so the EJS component
is present.


A nightly build's displayed `yt-dlp --version` string is recorded as provenance only. Holoclip
does **not** feed that display string back into a `yt-dlp==...` package requirement, because
nightly display identifiers are not guaranteed to be valid registry version specifiers.


## Audit one human-made clip example

Inspect the exact alignment and learning state without calling any external model:

~~~powershell
uv run holoclip examples-show <EXAMPLE_ID>
~~~

For the second real validation example:

~~~powershell
uv run holoclip examples-show 2002038236448bab
~~~

The report shows:

- clip/source provenance
- aggregate alignment confidence and bounding range
- every clip -> source alignment segment
- transcript excerpts from both sides of each aligned segment
- source gaps between retained segments
- setup / development / payoff / why-selected structure
- weak negatives
- retrieval documents currently stored for that example
- acquisition/runtime provenance

Use JSON when the report needs to be inspected by another tool:

~~~powershell
uv run holoclip examples-show 2002038236448bab --json
~~~

New URL ingests also preserve the original resolved source URL (including a YouTube `?t=`
timestamp when present), source-resolution reason/score/context, and whether the source was
auto-detected. The audit report compares that declared timestamp hint with the first aligned
source segment when available.


## Clip policy gates

Holoclip treats clipping restrictions as first-class data rather than a hard-coded
member blacklist. Restrictions can be scoped to a source video, a channel, or an
exact time range, and each rule keeps a reason and optional evidence URL/text.

Before learning from a human-made clip, Holoclip now checks:

- explicit no-clipping text in the original source title or description
- yt-dlp availability metadata that indicates restricted/paid content
- source/channel rules in `data/policy/clip_rules.jsonl`
- range rules after transcript alignment
- possible spoken restrictions such as `ここは切り抜かないで` near an aligned range

Hard `block` findings cannot be overridden. Automatic spoken-marker findings are
`review`: they stop unattended processing, but can be continued after manual
inspection with `--allow-policy-review`.

Check a source before acquisition:

~~~powershell
uv run holoclip policy-check "https://www.youtube.com/watch?v=VIDEO_ID"
~~~

Add a source-level rule:

~~~powershell
uv run holoclip policy-add ^
  --id "example-source-ban" ^
  --decision block ^
  --reason "Talent explicitly requested that this stream not be clipped" ^
  --source-id "VIDEO_ID" ^
  --evidence-url "https://..."
~~~

Add an exact prohibited range:

~~~powershell
uv run holoclip policy-add ^
  --id "example-range-ban" ^
  --decision block ^
  --reason "Talent requested this section not be clipped" ^
  --source-id "VIDEO_ID" ^
  --start 1234 ^
  --end 1380 ^
  --evidence-url "https://..."
~~~

Rules should record the narrowest scope supported by the evidence. A stream-specific
request must not be promoted into a permanent member-wide ban without evidence for
that broader scope.


## Batch-ingest human-made clips

Put one JSON object per line in a manifest:

~~~jsonl
{"clip_url":"https://www.youtube.com/watch?v=CLIP_A"}
{"clip_url":"https://www.youtube.com/watch?v=CLIP_B","source_url":"https://www.youtube.com/watch?v=SOURCE_B"}
{"clip_url":"https://www.youtube.com/watch?v=CLIP_C","enabled":false,"note":"hold for later"}
~~~

Run the batch sequentially:

~~~powershell
uv run holoclip examples-ingest-batch data\examples\batch.jsonl
~~~

The batch intentionally does not parallelize model calls. Each item goes through the
same source resolution, clip-policy, alignment, enrichment and retrieval-index path as
`examples-ingest-url`.

A state file is written after every item (by default
`batch.jsonl.state.json`). Status values include:

- `succeeded`
- `needs_source`
- `policy_blocked`
- `policy_review`
- `alignment_rejected`
- `retryable`
- `failed`

Re-running the same command skips successful rows whose input fingerprint is unchanged.
If a row's `source_url` is corrected, that row becomes eligible automatically.
Use `--force` to re-run successful rows deliberately.

Source ambiguity, policy review/block, alignment rejection and transient provider
failures do not stop later rows. Authorization/configuration failures stop the batch
because they are likely to affect every remaining item.

Use `--strict` when CI/automation should receive a non-zero exit code if unresolved
items remain.
