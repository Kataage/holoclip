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
