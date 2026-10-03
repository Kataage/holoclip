# Corpus editorial learning

Holoclip induces an explicit, evidence-backed editorial model from trusted human clips.
The model is stored as a versioned JSON artifact, rather than retrieved example snippets.
This is corpus-level mechanism induction; it does not update LLM parameters or claim that
nine examples establish generalization.

## Evidence and ownership

`editorial_cards.py` deterministically builds cards after the existing learning-quality gate.
Review/rejected clips do not produce learning cards. Historical example IDs remain unchanged;
canonical YouTube video identity prevents URL aliases from inflating the trusted corpus.

Each persisted card retains setup/development/payoff, selected content, exact clip/source
alignment ranges, full available source excerpts, weak-negative ranges/excerpts/reasons,
source/clip transcript fingerprints, quality decisions, alignment and extraction versions,
clip duration, union of selected source duration, source bounding span and compression.
Person/topic/editor metadata is retained in provenance and excluded from mechanism prompts.

The coverage-aware contract distinguishes observed editorial source cuts from uncertain gaps:
both endpoints must be trustworthy, source time must have a gap, and clip time must remain
continuous within two seconds. Missing human-clip coverage is not an editorial cut. Adjacent
and internally omitted material is contrast evidence about this selected positive, not a
universal negative label. Legacy weak negatives are suppressed.

Cards retain full available evidence on disk. Prompts use explicitly marked balanced excerpts
including beginnings and endings. Missing observations are unknown. The fast model identifies
development/payoff transcript segment IDs; code resolves measured lead-in and post-payoff time.
These measurements are model-identified beats, not manually verified editor intent. Unknown or
invalid IDs cannot produce timing claims. A multi-story clip can retain unknown beat timing.

## Scalable induction and cache

1. Cached per-card contrast/beat observations use the fast NIM model.
2. Batches of at most 12 cards induce partial patterns using the strong NIM model.
3. Incoming mechanisms are compared against catalog pages of at most seven existing patterns.
   Each merge partitions bounded input indices. Code unions the actual supporting examples;
   the LLM never needs an unbounded list of cards or support IDs in a consolidation prompt.
4. Cached automatic support assessments test the proposed mechanism against each cited example.
   Invalid/missing/negative assessments remove that support. Assessment is model-based evidence
   validation, not an independent human quality judgment.
5. Code rechecks provenance, maturity, readiness and observed duration before saving the profile.

All heavy calls are sequential through the existing NIM limiter. Prompts have explicit character
budgets; partial/consolidation generation has a bounded token budget. Pattern names and mechanisms
are open text discovered from data, not a fixed list of editorial archetypes.

Stage identities depend on their own inputs, method/prompt versions, model, reasoning and
generation settings, not the whole corpus fingerprint. Saved batch membership appends new
examples without reshuffling previous successful batches. A changed card rebuilds its own
observation/batch and affected merges. An interrupted run preserves all successful upstream
artifacts. Invalid merge partitions are not cached as successes.

Consolidation preserves distinct mechanisms and merges equivalent ones in bounded comparisons.
Its worst-case call count grows quadratically in the number of genuinely distinct patterns;
it avoids unbounded prompts but is not constant-cost learning. Progress and cache reuse are
visible, and very diverse future corpora may need a separate pattern-routing index.

## Provenance and maturity

The profile includes full deterministic cards, observations, batch membership, model/method
versions, exact trusted IDs, validated patterns, global/rejection/boundary principles and their
supporting IDs, assessment results, corpus fingerprint, settings and readiness reasons.

Only real trusted IDs from the relevant batch can support partial patterns. Weak-negative refs
must belong to a supporting positive. Empty/fabricated patterns are discarded. Consolidation
uses validated input indices, with exact support unions in code. Stream counts come from the
cards. Typical duration comes from actual supporting human-clip durations. Profile loading
recalculates these facts instead of trusting serialized counts, maturity or `ready=true`.

A core pattern needs at least three trusted human clips from at least two distinct source
streams. It also needs actual mechanism/cues/requirements/anti-pattern/boundary information and
automatic support assessment. Singleton hypotheses remain emerging. Stored principles lacking
multi-example/multi-stream support are not included in production prompts.

The default readiness floor is:

| Requirement | Minimum |
| --- | ---: |
| Trusted human clips | 30 |
| Trusted distinct source streams | 15 |
| Core mechanisms | 5 |
| Trusted examples covered by core mechanisms | 80% |
| Core support per mechanism | 3 clips / 2 streams |

Thresholds are explicit Pydantic configuration and may be made stricter. Additional gates reject
missing principles/provenance, incomplete core patterns and unassessed core support. A diagnostic
fast-model induction is always ineligible for production even if the numerical gate would pass.
These floors are engineering safeguards, not a claim of statistical sufficiency.

## Runtime and editing

Normal `analyze`/`discover` refresh missing/stale profiles when trusted corpus/model settings
change. After readiness they also run and persist a cached ON/OFF diagnostic automatically when
the input passes the unseen-stream leakage check. Unready or stale profiles do not affect candidate discovery. Ready profiles supply core
patterns and mature principles to broad discovery, optional guided discovery and final selection.
Retrieval remains supporting evidence. Entire profile content, not only its corpus fingerprint,
changes discovery/selection cache identities.

A bounded shortlist receives a strong-model boundary plan from measured source transcript IDs.
Code rejects invented/reversed/overlapping spans. The final selector sees retained spans and
compression. `candidate_to_clip_timeline()` converts plans into the existing canonical timeline;
unready profiles and unsupported plans preserve ordinary candidate boundaries. Very long stories
over the planner prompt budget retain their original span. Video rendering/publishing remains
a later consumer of this timeline and is not implemented by this learning layer.

## Autonomous corpus growth

```powershell
uv run holoclip examples-grow-corpus --cycles 20
uv run holoclip examples-grow-corpus --target-trusted 100 --target-streams 50 --cycles 100
```

Public metadata discovery searches broad clip queries and underrepresented member metadata,
without requiring operator URLs. Persistent search cursors advance across cycles. The queue
resolves source descriptions conservatively; ambiguous, inaccessible or policy-review items
remain quarantined. Acquisition selection favors less represented source streams, source channels
and clip editors, and balances discovery queries as a coarse topic proxy. This reduces obvious
bias; it does not prove a representative corpus.

For eligible queued items the existing audio/transcription/alignment/quality/enrichment/index
pipeline runs sequentially, with its caches and unchanged quality thresholds. The queue records
the actual learning-quality decision, not merely successful enrichment. Review examples never
auto-promote to trusted. Profile refresh follows each cycle. Increase the target to continue
collecting beyond the initial corpus floor. `--discover-only` resolves metadata without audio.

Acquisition follows existing source policy gates and requires publicly available, ended content.
The existing rules for prohibited paid/member/concert sources and source-specific restrictions
remain authoritative. Public availability is not blanket rights clearance. COVER requires source
attribution and excludes paid content unless separately authorized; third-party/platform rules
also apply. See [COVER clip guidelines](https://hololivepro.com/en/terms/) and the existing
`clip_policy.py` implementation. Discovery uses the provider's metadata-only search interface;
see the [yt-dlp project documentation](https://github.com/yt-dlp/yt-dlp).

## Evaluation and operator effort

Existing accept/maybe/reject records are preserved for later validation/calibration and are never
an induction input. `label` and `review` refuse new feedback before current corpus readiness.
Normal discovery no longer ends with a mandatory operator labeling step.

```powershell
uv run holoclip editorial-ab unseen.json --stream-id UNSEEN_SOURCE_ID
```

After readiness, ON/OFF uses the same models, temperatures, transcripts and selector settings,
with individual example retrieval disabled to isolate the corpus prior. Existing transcript leak
checks reject training overlap, missing comparison material and insufficient transcript evidence.
A cached automatic diagnostic sees anonymized arms. It is not human acceptance or independent
proof of generalization. No new human sample is requested automatically. If later needed, reserve
only a small blind final sample. Boundary planning is a separate measured evaluation dimension.

## Local artifact verification

The original local corpus has 11 clips: 9 trusted and 2 review. Its ten total source streams
include the review material; the nine trusted clips cover eight distinct source streams. Both
interpretations remain below readiness, and only the eight trusted streams count toward the gate.

Use `examples-learning-status` for actual generated pattern counts, coverage, model and readiness.
The profile, learning cards, stage outputs, acquisition queue and operator labels are local data
under ignored `data/`; CI uses deterministic fixture corpora and mocked external services.

### Verified local run, 2026-10-04 JST

The configured `z-ai/glm-5.3` strong endpoint timed out on bounded induction and on a 64-token
smoke request. An advertised alternate Kimi endpoint also timed out; Kimi-K2.6 returned HTTP 404.
No strong-model corpus result is claimed. Successful per-card observations were retained.

`examples-learn-profile --diagnostic-fast --batch-size 4` completed actual induction and support
assessment using `z-ai/glm-5.3-flash`. This diagnostic profile is explicitly production-ineligible.
It stores 9 global principles, 11 rejection principles, 12 boundary principles, 6 patterns,
29 valid contextual weak-negative refs and 10 automatic support assessments. One pattern has
four supporting clips from three streams; five remain emerging. Core coverage is 4/9 (44.4%).

Readiness is false on every numerical floor and on the diagnostic-model eligibility gate.
The corpus fingerprint is `b5dfb8de248637e8d48f78eadd182e15be1e26d39624788bc200ae1428a7339d`.
Re-induction with a client that raises on **any API call** reproduced the complete profile and
provenance exactly from stage caches. Runtime validation returned no production prompt payload,
and CandidateDetector dropped the unready profile.

Live metadata discovery resolved five new public clips to five distinct source streams and
five clip editors, saving all five to the acquisition queue. No additional audio was acquired
in this metadata-only check. The full acquisition path is exercised with mocked providers in
CI and delegates to the existing ingestion pipeline. The original 11 examples remain unchanged.

The existing feedback log stayed byte-for-byte unchanged, with SHA-256
`2009db8d8903bb9086214499a96122aabdc7e1527ba06b256b881d0f5f243ae1`.
Actual unseen-stream ON/OFF and human validation are **NOT RUN**, because readiness is false.
