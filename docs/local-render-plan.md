# Local multi-source RenderPlan v1 — offline FFmpeg preview

Status: **technical A/V assembly baseline only**; production readiness remains false.

## Inputs and preview workflow

The new Holoclip command takes a canonical ClipTimeline JSON file and a JSON
array of explicit source mappings. Each source mapping has:
- "asset": SourceAsset JSON with matching "id", "platform", "external_id",
  "availability": "available", "access_class": "local"/"public"/"authorized".
- "path": full path to an existing local video file.
- "local_render_approved": true, explicitly asserted by the operator.

Example mapping (one entry per distinct source_asset_id):

    [
      {
        "asset": {"id":"asset-a","platform":"local",
                  "external_id":"source-a","availability":"available",
                  "access_class":"local"},
        "path":"D:\\AI\\holoclip\\data\\sources\\a.mp4",
        "local_render_approved":true
      },
      {
        "asset": {"id":"asset-b","platform":"local",
                  "external_id":"source-b","availability":"available",
                  "access_class":"local"},
        "path":"D:\\AI\\holoclip\\data\\sources\\b.mp4",
        "local_render_approved":true
      }
    ]

For a timeline whose ordered segments reference asset-a and asset-b:

    uv run holoclip render-local-preview --timeline "D:\AI\holoclip\data\timeline.json" --sources "D:\AI\holoclip\data\local-sources.json" --output "D:\AI\holoclip\data\preview.mp4"

This performs read-only SHA-256 hashing, ffprobe source validation, segment
bounds inspection and deterministic plan compilation. It does not create
an output or access the network. Explicit execution:

    uv run holoclip render-local-preview --timeline "D:\AI\holoclip\data\timeline.json" --sources "D:\AI\holoclip\data\local-sources.json" --output "D:\AI\holoclip\data\preview.mp4" --execute

Requires local FFmpeg and ffprobe on PATH, with no Docker, yt-dlp, NIM
API, ComfyUI or cloud resource.

## Resolve existing full-video caches without a manual source manifest

Existing canonical SourceAsset IDs can be matched to already-acquired
local complete yt-dlp editing_video caches:

    uv run holoclip render-local-preview --timeline data/clip-timeline.json --auto-sources --database data/holoclip.db --cache-root data/sources --approve-local-preview --output data/preview.mp4

The default still validates without output. To produce an MP4 add
--execute. The manual --sources and --auto-sources modes are exclusive.

Source identity comes from read-only canonical SQLite: the exact
source_asset_id must exist, be AVAILABLE, have a public/authorized
YouTube SourceAsset, and have a canonical 11-character video ID.
Supported fixed local cache layouts relative to --cache-root:

    <root>/<video-id>/editing_video/media.*
    <root>/original/<video-id>/editing_video/media.*
    <root>/examples/sources/<video-id>/editing_video/media.*

Each directory requires one full video with a media.info.json whose
id and Youtube extractor identity match the canonical asset.
training_audio and editing_video_sections must never substitute
for a full original: bounded range PTS does not guarantee absolute
source coordinates. Missing or ambiguous caches, wrong identity,
unknown assets and linked paths fail closed. No downloads, cleanup,
SSH or metadata web probes take place.

--approve-local-preview is an explicit local technical-use assertion,
NOT clipping rights, copyright clearance, or publication approval.
Source bytes and A/V durations are separately hashed and checked
by the preexisting RenderPlan compiler/executor. A successful preview
still retains ready=false and has no upload path.

## Missing source acquisition through the existing shared yt-dlp gate

If the automatic source mapper reports a missing full stream, inspect the
existing caches and the canonical timeline *offline* with:

    uv run holoclip render-local-source-acquire --timeline data/timeline.json --database data/holoclip.db --cache-root data/sources

This command never probes YouTube and never creates/overwrites media. It
reports missing source asset IDs in timeline order and cache counts. Existing
corrupt, ambiguous, linked or untrusted caches are blockers, NOT invitations
to overwrite them or guess another candidate. Audio-only caches and bounded
video sections never substitute for complete original video timestamps.

To explicitly try acquiring **one** missing complete public source video,
after installing and configuring the authenticated PC-A LAN coordinator
and existing conservative local yt-dlp pacing gate:

    uv run holoclip render-local-source-acquire --timeline data/timeline.json --database data/holoclip.db --cache-root data/sources --execute --approve-local-preview

Only the first missing stream by timeline order is attempted per command.
The default max recorded archive duration is 14400 seconds (4 hours), and
yt-dlp's max file size request is 512 MiB; change using
--max-duration-seconds (60..14400) and --max-filesize-mb (64..1024).
These are *requested acquisition limits*, not guarantees that all protocols
can stop downloads at exactly the limit. After download, observed file
size is rechecked and the media/receipt are validated before reuse.
Access may take substantial time and network bandwidth; do not automate
rapid loops. A configured shared-LAN coordinator is mandatory to execute
this command even when only one PC is actually downloading.

Before downloading, one yt-dlp metadata probe is made **through the same
shared lease/local pacing**. Live/upcoming sources, unknown or oversized
durations, mismatched IDs and blocked/review clip rules are rejected.
The metadata policy registry is read from data/policy/clip_rules.jsonl by
default; optional --policy-file points to a different local registry.
Then a single bounded full-video acquisition uses the existing provider
with its rate-limit cooldown and conservative request pacing. Neither
stage bypasses the LAN gate, resets cooldowns or silently tries another
source after a failed request. No publishing is performed.

The approval flag is **not proof of ownership or a platform-policy grant**.
Only locally approved technical previews should use this path; apply
applicable Hololive source restrictions and review finished edits separately.
The output status remains ready=false even when an archive is acquired.

## Same-source generated-vs-human edit geometry (PR #224)

The command \`uv run holoclip render-same-source-geometry\` is **read-only by
default** and requires no new operator dataset, media download or video decode.
It uses existing \`data/runtime/render-source-queue.sqlite3\`, the canonical
source-asset database referenced by those queue jobs,
\`data/examples/human_clips.jsonl\`, and the persisted
\`data/examples/corpus_growth.json\` holdout reservations. An optional
\`--execute --output <new-file>.json\` creates an immutable standalone report.
Use \`--after-job-id <next_cursor>\` to paginate 40 completed preview jobs at
a time. The exporter never overwrites a prior result.

Only completed source jobs with \`QA_PASSED\` render evidence are eligible.
The render timeline's \`source_asset_id\` is dereferenced through the
read-only canonical SQLite source repository and must identify one unique
YouTube video ID. The human example must pass the existing trustworthy
alignment checks **and** have the exact same source-stream ID and canonical
source YouTube URL. Multiple-source compilation jobs are skipped rather
than falsely matched to one source. Unknown sources, stale previews,
misleading URLs, unsupported/ambiguous human identity, and duplicated
canonical clip IDs never become reference cases.

For same-source pairs, the report describes unique selected source
durations, intersection seconds, generated-selected overlap fraction,
human-aligned reference coverage, selected-source Jaccard, intervals unique
to each side, positive source gaps, and median distance from generated
gap starts to the nearest human source-gap start. Original-source ranges
are merged before calculating coverage so repeated segments cannot
inflate it. These numbers measure **time interval geometry only**.
Human audio/transcript alignment boundaries can differ from actual
visual edit frames and cannot independently determine the artistic best
cut, transition, joke timing or subtitle legibility.

All human examples currently available through this path are **training
references**, so each match says \`TRAINING_REFERENCE_IN_SAMPLE\`, never
independent holdout or human gold. Persistent reserved holdout streams are
excluded; any overlap between reserved and stored training streams blocks
all comparison results. A missing reservation state is explicitly reported
as unverified, not silently considered a clean split. Neither this metadata
pass nor its human alignment references qualify real-world editing quality.
Original MP4 hashes originate from previous technical QA: this command
checks the preview file exists but does **not** decode or independently
rehash large media. It does not edit, repair, publish or contact YouTube.

\`ready=false\`, \`production_ready=false\`,
\`heldout_qualification_allowed=false\`,
\`auto_adoption_allowed=false\`, human creative-quality labels missing,
and precision/recall unavailable remain mandatory. Future work must use
independently source-grounded held-out real-media cut maps before any
automated quality gate is considered.



### Source-selection differences and replay-order evidence (v2)

The same read-only command additionally reports
\`selection_difference_diagnostics\` for each matched human/trial pair:
bounded top-three **source-clock windows** selected only by the generated
timeline and only by the human alignment, total difference-window counts,
and the number of source-time backtracks in the **actual output order** on
each side. The human-side order follows aligned clip order, not sorted source
timestamps. A 0.5-second tolerance avoids flagging negligible alignment
clock jitter as a backtrack.

These data explain why two timelines can have full set overlap but different
playback sequences; they do not identify the best order. Replayed jokes,
callbacks, or flashbacks may be intentionally nonchronological. Missing
human-reference windows are not false-negative classifications or commands
to insert a cut. Source gaps and comparison windows use unioned source
ranges to avoid double-counting repeated material. Maximum serialized report
size is 2 MiB; exceeding the limit fails closed without overwriting output.

As before, this is **in-sample descriptive evidence only**: human alignments
are approximate, never a frame-accurate edit gold; all report-level
\`ready\`, \`auto_adoption_allowed\` and held-out qualification guards remain
false. No network, yt-dlp, FFmpeg, re-encode or automatic publication.

### Page-local inspection queue (PR #226)

The same \`render-same-source-geometry\` read-only command now returns
\`inspection_queue\` automatically. It lists **at most ten render jobs**
from the current \`--after-job-id\` / \`--max-jobs\` page. Each job contributes
only its largest observable discrepancy against one trusted human example,
so many human clips for a single original cannot multiply inspection votes.
The ordering first uses the sum of source seconds selected by only one side.
Ties are resolved by verified exact-span playback-order differences, source
clock backtrack-count differences and stable anonymized identifiers.

The queue includes source-clock intervals and reason codes for manual or
subsequent independent **inspection only**. Exact playback-order difference
can be stated only if both ordered lists contain precisely the same source
interval multiset; otherwise its value is null, not a guessed result.
Training examples are never independent validation, absence of a gap is not
a creative-quality pass, and high disagreement is not an editing error.
The original \`QA_PASSED\` means A/V technical QA, not human editorial QA.
If training examples overlap reserved holdout sources, no inspection entries
survive. Missing holdout state remains \`source_reservation_metadata_verified=false\`.

No extra command, manual datasets, source download or FFmpeg process is
required. Opt-in \`--execute --output <new>.json\` saves a create-only immutable
report, including the queue. \`ready=false\` and \`auto_adoption_allowed=false\`
are unchanged. This is bounded per page, **not a global all-stream ranking**.

### Local transcript context for prioritized source disagreements (PR #227)

The same read-only \`render-same-source-geometry\` command optionally reads
timed local source ASR under \`data/transcripts\`, with
\`--transcript-root <local-folder>\` for an existing alternate cache. Only the
ten already holdout-screened inspection jobs are considered. Fixed recognized
paths are \`<root>/<video-id>.json\`, \`<root>/sources/<video-id>.json\`,
and \`<root>/examples/sources/<video-id>.json\`. Multiple files for one ID,
identity mismatch, invalid timing, oversized data and linked paths fail
closed. Missing files report \`TRANSCRIPT_UNAVAILABLE\`, not invented text.

For each job's top generated-only and human-only source window, record up to
three short ASR utterances from the preceding eight seconds, an at-most-eight
second window within the discrepancy, and the following eight seconds.
Each excerpt is capped to 160 characters; unsafe controls are excluded.
The transcript's SHA-256 and the basis of source identity are recorded.
Filename-only association is explicitly **unverified**; matching metadata
does not independently verify the audio or word timing.

This is **unverified ASR context**, not a judgment about humor, intentions,
or the correct cut. No network, yt-dlp, new ASR, LLM inference, FFmpeg,
automatic edit, approval, publishing, or manual dataset is required. Local
reports can contain verbatim speech; review them before sharing. Normal
\`ready=false\`, holdout exclusion, no overwrite, and in-sample status remain.

### Source-clock handoff and speech-boundary observations (PR #228)

On the same \`render-same-source-geometry\` command, each holdout-screened
inspection item with an existing local ASR transcript now includes
\`transcript_context.playback_handoff_evidence\`. The source windows are
ordered by the actual generated output timeline, **not** by source
timestamp. The evidence records opening/ending source boundaries,
forward source-time skips, replay/overlap joins and whether an unverified
ASR utterance spans either side of a discontinuous join. At most three
representative joins are included, selected deterministically by evidence
priority and displayed in playback order. Full join counts remain visible.

A discontinuity or ASR segment crossing a boundary is **not** a verified
audible speech cut; word-level ASR timing has not been measured, and an
intentional callback or flashback may benefit from a source-time jump.
This pass **does not** assign source timestamps to setup, development,
payoff or reaction. Existing human story-description fields do not provide
reliable role-time labels. Accordingly \`narrative_setup_development_payoff_reaction_inferred=false\`,
\`editorial_quality_label=null\`, \`ready=false\`, and no auto-edit
approval apply. Where the local transcript is absent or not trustworthy,
there is no handoff speech evidence rather than a fabricated default.

No new ASR, remote inference, video playback or downloads, yt-dlp,
network calls, new operator data, destructive writes or publishing.

### In-sample story annotation and human-aligned position coverage (PR #229)

The same read-only \`render-same-source-geometry\` report now attaches
\`narrative_reference\` to **only the existing top-ten, identity-matched and
holdout-screened inspection jobs**. The chosen trusted human example is
looked up by the exact source stream and hashed canonical human clip
identity, never by loosely similar text or a different channel/stream.

Each reference includes bounded, sanitized **clip-level** human annotations
for \`setup\`, \`development\`, \`payoff\` and \`why_selected\`, plus overlap
coverage of generated source selections against the human example's
**first**, **intermediate**, and **last** aligned clip segments. These are
*positions in the human clip alignment order*, NOT story-role timestamps:
the first aligned segment may not contain the actual setup, and the last
may not contain a payoff. Missing middle group coverage is null, not zero.
Repeated generated source material is unioned before measuring shared
seconds, so a replay cannot inflate source coverage.

The report explicitly sets
\`clip_level_story_labels_time_anchored=false\`,
\`setup_development_payoff_in_generated_clip_verified=false\` and
\`text_matches_source_asr_verified=false\`.
There is no annotation-to-speech semantic matching, narrative completion
claim, or human creative-quality gold. Unsafe annotations do not enter
the report; holdout collisions wipe all inspection entries. Since this
output includes original human clip descriptions and potentially local ASR
text, review JSON before sharing.

No new operator data, download, ASR, LLM request, paid service, Docker,
FFmpeg, media rewrites, automatic cut adjustment or publishing are needed.
The unchanged \`ready=false\` and create-only opt-in report gate apply.


### Evidence-cited semantic role hypotheses (PR #230)

New command (default **strictly local dry-run**, no provider requests):
\`uv run holoclip render-same-source-semantic-review\`.

It reuses the same source-ID, holdout, trusted human alignment, QA-passed
render and source-time ASR gates as \`render-same-source-geometry\`. Only the
top **three** inspected jobs with matching in-file source metadata and
complete clip-level setup/development/payoff descriptions can qualify.
The bounded request includes up to twelve actual source-time ASR excerpts
per job, with unique evidence IDs and each excerpt's original source
clock and whether its context window is \`generated_only\` or
\`human_only\`. Excerpts before/after a selected difference window may
still fall outside the generated output; **none** of these context windows
alone certify presence in the finished clip.

To explicitly request semantic hypotheses from the existing *qualified*
capability router and immutably persist a report, use
\`HOLOCLIP_DATA_CLASS=public\` plus
\`uv run holoclip render-same-source-semantic-review --execute --output <new-report>.json\`.
The operator must verify that the human descriptions and ASR are lawful
public data **before setting this flag**. No new paid service, Docker,
model install, external dataset or API key is added. It uses existing
provider credentials, cost/quota policy, and the production qualification
router; unavailable routes defer, not bypass qualification. At most three
qualified JSON requests are made. This opt-in is **not** used by ordinary
offline rendering/CI, and must not be confused with automatic publishing.

The model may report only \`POSSIBLE_TEXTUAL_REFERENCE\` citing an **exact
verbatim substring** of a supplied ASR snippet, or
\`INSUFFICIENT_EVIDENCE\` with no citation. A missing role, duplicated role,
invented evidence ID, fabricated quote or malformed schema rejects the
result and writes no report. For each cited hypothesis, independently
supplied source timestamps and window provenance are retained. These
are **unverified model hypotheses** about local ASR and in-sample clip-level
annotations, not source-truth, verified semantic matches, exact narrative
role-time anchors, or evidence of edit quality, completeness, humor or
human preference. The report and its model provenance remain
\`ready=false\`, \`quality_qualification_allowed=false\`, and have no
auto-edit/publication effect. Only a new JSON output is created, never
overwriting an existing report or video.

### Fully offline re-audit of saved AI citations (PR #231)

Use \`uv run holoclip render-same-source-semantic-audit --receipt <existing-semantic-review>.json\`
to recheck a PR #230 **saved** model receipt. This command **never**
calls NIM, yt-dlp, another API, FFmpeg, or new ASR. It re-reads the
original local queue, trusted human example JSONL, holdout state and local
source-time ASR through the existing same-source safety checks, then
recomputes the prepared evidence. The fresh evidence SHA and exact bounded
case list must match the saved receipt, and every model-cited utterance,
quoted substring, source-clock timestamps, evidence ID and
\`generated_only\`/\`human_only\` context side must reproduce from that
same locally rederived evidence. Every role must still be present, with
\`INSUFFICIENT_EVIDENCE\` entries retaining null citations. Tampering,
a stale transcript, a changed source or holdout reservation, and an
unsafe/ambiguous receipt are rejected with **reason codes**, not untrusted
speech text. A missing or altered input is never guessed from model text.

The default is read-only JSON stdout; \`--output <new>.json\` optionally
writes an immutable audit file without touching the saved AI receipt.
On rejection the command uses nonzero exit status, but can still write a
rejection diagnostic if requested. The audit output includes counts of
syntactically reproducible verbatim quotes and explicit model
\`INSUFFICIENT_EVIDENCE\` responses **only**. These counts are
not narrative classification accuracy, semantic agreement, ASR
correctness, independent editorial human ratings, precision/recall,
creative quality or permission to auto-edit/publish. Even a consistent
receipt remains \`ready=false\` and
\`LOCAL_SEMANTIC_AUDIT_CITATIONS_CONSISTENT_UNQUALIFIED\`.

No additional accounts, tools, datasets or user-supplied labels are
required. This check is safe to run in CI with synthetic local fixtures.

### Built-in synthetic source-clock story-span evaluation (PR #232)

Run \`uv run holoclip render-semantic-fixture-eval\` without downloads,
credentials, Docker, NIM or user-created labels. This local deterministic
harness provides synthetic, explicitly time-anchored ground truth spans for
**context / setup / development / payoff / reaction**. Nine synthetic
timelines cover an intact narrative, missing setup, missing development,
missing payoff, missing reaction, missing preceding context, partly cut
payoff, repeated clips and non-chronological reordering. Source-time
intersection is union-corrected; a repeated source interval cannot increase
coverage. This measures *source-clock selection*, not the semantic content
or editorial merits of a real video. No human quality precision/recall is
derived from the synthetic counts.

The main \`render-same-source-geometry\` inspection queue now carries the
actual **ordered generated selection source intervals** after exact source
identity and holdout exclusion. The separate opt-in
\`render-same-source-semantic-review\` evidence request uses these intervals
to add each ASR excerpt's source-clock coverage status:
\`SOURCE_CLOCK_FULLY_SELECTED\`, \`SOURCE_CLOCK_PARTIALLY_SELECTED\`, or
\`SOURCE_CLOCK_NOT_SELECTED\`. This is computed independently of whether the
excerpt was collected from a \`generated_only\` or \`human_only\` *difference*
window; the preceding/following context may be outside the final output.
Model citations and subsequent offline audits retain this grounded status
and source-clock selected duration. A correct quotation that is not selected
by the generated edit **cannot** be interpreted as evidence that that
speech is present in the generated clip.

The harness intentionally includes two honesty controls: a fabricated
source quote is rejected, but a verbatim quote attributed to the wrong
semantic role **can still pass citation verification**. The latter is a
known limit, not a passing creative evaluation. Even fully selected source
times do not prove the speech is audible after audio processing or that
the AI's story-role interpretation is correct. Old semantic receipts
using the earlier evidence schema are not promoted to the new v2 status;
regenerate fresh opt-in evidence instead of silently reusing stale receipts.
No automatic edit or publication is enabled; \`ready=false\`.

### Real human-aligned omission probes from existing local ASR (PR #233)

\`uv run holoclip render-real-source-omission-probe\` analyzes **existing
trusted human-made clip alignment segments** and any already-cached
original-source timed ASR. It does not depend on the output render queue,
so human-made examples can be evaluated *before* Holoclip has rendered a
corresponding candidate. Source metadata and the trusted learning-quality
decision are rechecked against canonical YouTube IDs; training/holdout
collisions or absent holdout reservations block the experiment globally.
No source, audio, clip or model files are downloaded.

For each automatically eligible clip, reconstruct the human clip's first,
intermediate and final aligned source segments. Build virtual
counterfactual edits removing each positional group, preserving the other
groups in playback order. Measure exact union-corrected **source seconds
lost** from the reference selection, even when the original alignment
replays a source interval. At most three local-ASR utterances per group
are hashed and clock-checked to record how much of their source-time
coverage survives each removal. Original human clip text, ASR speech and
raw source video IDs are omitted from the report; corpus/transcript/
holdout evidence digests and hashed references remain available.
The default is read-only JSON on stdout; \`--output <new>.json\`
optionally creates a report without overwriting an existing file.
Use \`--max-examples\` (maximum 20) to bound the scan output.

**Interpretation is intentionally limited:** \`opening_aligned\` is the
first aligned part of a human clip, NOT a verified story setup.
\`closing_aligned\` is NOT a guaranteed payoff/reaction. Source timing
from local ASR is unverified audio, and these observations use **in-sample
training references**, never independent validation gold. Missing,
ambiguous, mismatched, symlinked or invalid transcripts are excluded
with explicit counts, not guessed. There is no model inference,
semantic role accuracy score, precision/recall, autonomous repair,
new operator labels, yt-dlp, FFmpeg, Docker, paid service or upload.
The report is always \`ready=false\`.

The next legitimate accuracy step will need independent narrative
role-time annotations or blind judgments on unseen streams; a high rate
of agreement with these human selection intervals alone is not evidence
that Holoclip understands jokes or outputs better videos.

### Native ingest transcript provenance bridge (PR #234)

The actual \`examples-ingest\` workflow writes source ASR to
\`data/transcripts/sources/<11-character-video-id>.json\` via
\`save_transcript()\`. That JSON normally contains just \`segments\` and
does **not** contain \`video_id\`, \`source_id\` or \`stream_id\`. Earlier
PR #227–#233 read the file, but downstream semantic/real-example probes
required matching embedded source metadata. Consequently valid, previously
ingested clips were incorrectly excluded as filename-only and their
semantic evidence/real omission measurements often remained empty.

We now derive a **second, explicitly qualified association basis** from
the *already trusted and canonical-YouTube-ID-checked* human example
\`source_transcript_path\` recorded at ingestion. For each stream, all
eligible human examples must unanimously identify the **same exact
ingest source artifact path**, equal to
\`<transcript-root>/sources/<video-id>.json\`. The path and its parent
must be symlink/junction free. The file still passes 8 MiB, JSON
structure, bounded segment count, finite source timing, unique-location
checks and source metadata mismatch checks. The source ID must also
have been confirmed against the trusted human example's canonical
\`source_url\` by the existing corpus gate. This does NOT authenticate
the media bytes, recognizer output, speaker or Japanese semantics;
the provenance label is
\`TRUSTED_EXAMPLE_MANIFEST_AND_SOURCE_URL_ID_MATCH_AUDIO_UNVERIFIED\`.
The original \`FILENAME_ONLY_IDENTITY_UNVERIFIED\` basis still cannot
qualify model requests or real ASR probes. If two trusted examples
disagree on the transcript path, the upgrade does not apply.

This automatic bridge is used by both the render inspection context
and \`render-real-source-omission-probe\`, and enables the same conservative
basis for the **opt-in** \`render-same-source-semantic-review\` (which still
requires explicit public-data authorization, an available qualified
provider route and source/holdout identity checks). Existing transcripts
and human example JSONL are reused without file migration, new ASR,
yt-dlp, API/network requests or manual dataset preparation. This change
only lifts the false-negative artifact association blocker; it makes
**no** claims of semantic model accuracy, editing quality, validated
transcription content, production readiness or human preference.

### Existing local corpus readiness inventory (PR #235)

Run \`uv run holoclip render-real-corpus-readiness\` on the normal
Windows/uv machine with an already-ingested corpus. **No downloads,
NIM/API calls, FFmpeg, manual labeling, Docker or new ASR** are invoked.
The command performs an actual, deterministic bounded scan of existing
trusted human clip examples and cached source transcripts, respecting
the existing full training/holdout collision blocker and the exact
canonical source/ingest-manifest checks from PR #234.

The report separates sources with **embedded metadata-based ASR
association**, **matching original ingest manifest association**, **missing
transcript**, **filename-only unverified association**, and **invalid or
ambiguous source ASR**. On valid ASR it counts examples with at least
two clip-aligned segments, any source-timed ASR snippets observed in both
the human clip's opening and closing aligned positions, and complete
clip-level \`setup\`/\`development\`/\`payoff\` descriptions. It also
reports the number of *distinct source streams* represented. This
identifies potential next-stage semantic probing material **without
pretending the story descriptions are ground-truth role timestamps**.

The scan is capped at 128 distinct streams, with \`--max-streams\` to
lower the bound. The output declares whether the inventory is
exhaustive and how many streams remained unscanned. Up to 12 candidate
**in-sample** references are shown, at most one per source, sorted by
available ASR positional evidence and alignment completeness. There is
no cross-stream leak into independent heldout evaluation.

No raw YouTube IDs/URLs, file paths, human descriptions or ASR text are
included in the output; stable SHA-256 references, aggregate counts,
limited per-position utterance counts and source transcript digests
are recorded. The default prints JSON without writing files; an optional
\`--output <new>.json\` creates an immutable sidecar. A blocked holdout
state returns a nonzero exit code. The inventory's \`ready=false\`,
\`real_video_role_accuracy=null\` and
\`independent_semantic_gold_available=false\` remain regardless of
the number of candidates discovered.

GitHub does not contain users' local corpus data. CI can verify the
algorithm with synthetic offline fixtures, **not** truthfully report the
number or semantic accuracy of user-owned real clips. Real counts must
come from execution against the user's original \`data/\` directory.

### Source-cited comparison of human edits with virtual omissions (PR #236)

\`uv run holoclip render-real-semantic-contrast\` uses the existing
source-diverse real-corpus readiness inventory to select up to three
**in-sample trusted human-made clips from different source streams**,
then independently rechecks each video's canonical ID, stored ASR
manifest/metadata identity, holdout reservations, 2–200 ordered
alignment segments and local transcript SHA. It constructs three
possible counterfactual selections by dropping the first, intermediate,
or final human-aligned clip segments (no middle variant for two-segment
examples). These are **virtual** removals, not rendered edits.

For every variant, the report distinguishes existing source-timed
ASR excerpts that disappear *entirely* from the selected source-clock
timeline, remain fully selected, or are only partially present. At most
nine short ASR excerpts are sampled per source and only counterfactuals
with a demonstrably lost excerpt and a retained excerpt qualify.
The default runs locally with **no network/AI request** and prints a
JSON preview containing source-time positions, SHA-256 of each ASR
utterance, precise source-selection loss, and no verbatim ASR text.
There is no new data preparation, yt-dlp, FFmpeg, paid service or
Docker dependency.

For optional actual textual comparison, **only when the user has
confirmed that the local ASR content is public**, set
\`HOLOCLIP_DATA_CLASS=public\` and add \`--execute --output <new>.json\`.
The existing capability-qualified \`MeshLLMClient\` is reused, with at
most three requests and no unqualified fallback. The model must
either express \`POSSIBLE_COMPREHENSION_LOSS\` while citing an **exact**
lost ASR substring AND an exact retained substring from the appropriate
virtual version, or report \`NO_CLEAR_LOSS_FOUND\` or
\`INSUFFICIENT_EVIDENCE\` with null citations. Unsupported statuses,
fabricated quotes/evidence IDs, references to still-retained speech as
"lost", wrong variants, incomplete/malformed outputs or extra approval
fields are rejected. The saved create-only JSON contains only hashed
source identifiers, excerpt times/digests and verified model quotations,
not the full raw local ASR. Review quoted phrases before sharing.

The model output is **NOT** an independently verified factual,
audible or semantic assessment; a surviving ASR excerpt is not proof
that a rendered video contains audible speech. First/middle/last
aligned clip segments are **not** verified setup/development/payoff
labels. Even matching real human training edits cannot establish
humor, completion, human preference or actual precision/recall. Neither
dry-run nor model hypothesis can automatically edit, qualify, accept or
publish a clip. \`real_semantic_accuracy=null\`, no gold, \`ready=false\`.

### Real-ASR omission hypothesis ordering-sensitivity probe (PR #237)

\`uv run holoclip render-real-semantic-order-sensitivity\` performs
an **offline, ASR-text-redacted default preview** of the existing,
source-diverse real human-vs-omission comparison cases (PR #236).
It reports no arbitrary fabricated role-time labels or quality scores.
By default only one existing source is selected to avoid consuming quota.
This command does not download media, transcribe, render, or call models
unless explicitly opted in.

With \`HOLOCLIP_DATA_CLASS=public\` and
\`--execute --output <new-filename>.json\`, the existing qualified
\`MeshLLMClient\` is asked **twice per selected source**, at most three
sources / **six total requests**. The second prompt reverses the
presentational order of both source-timed ASR excerpts and omission
variants, leaving the evidence IDs, text, original timestamps,
source-clock LOST/RETAINED/PARTIAL/UNSELECTED classifications and
in-sample content otherwise unchanged. Both answers are independently
validated using PR #236's strict verbatim lost-and-retained quotation,
variant completeness, correct source-clock effect and no approval
field checks. If either response is malformed, there is **no partial
report**, preserving the create-only safety boundary.

The result compares statuses and cited evidence IDs plus SHA-256 hashes
of exact accepted quotes for each original positional omission.
Provider and model identity are captured from the existing route
provenance on **both** calls; when missing or different, the item is
\`MODEL_ROUTE_NOT_COMPARABLE\` and excluded from comparable counts.
Comparable pairs are classified as
\`ORDER_INVARIANT_WITHIN_TWO_RUNS\`,
\`ORDER_SENSITIVE_CITATION_CHANGE\`, or
\`ORDER_SENSITIVE_STATUS_CHANGE\`. Source ASR excerpts, literal
validated quotations, raw YouTube IDs, original file paths and the
model's unconstrained output are **not** copied into the report.
Existing report paths are preflighted **before provider calls** and
output files are immutable/create-only.

These are at most two order-dependent samples of an **unverified model
hypothesis**. Agreement does not establish semantic correctness,
narrative comprehension, ASR accuracy, human edit preference, funny
videos, reliability under other prompts, or production fitness.
Disagreement is useful diagnostic evidence; model-route changes are
not counted as valid consistency comparisons. The gate always keeps
\`semantic_accuracy=null\`, \`independent_gold_available=false\`,
\`ready=false\`, and does not edit or publish any video.

### Offline audit of two-order AI hypothesis receipts (PR #238)

\`uv run holoclip render-real-semantic-order-audit --receipt <existing-PR237>.json\`
rebuilds the **same existing trusted source clips, counterfactuals and
source-timed local ASR** through the PR #236/#237 read-only provenance,
holdout and canonical source checks. Use the *same* \`--max-streams\`
and \`--max-cases\` as when the original report was created; the
default is one source. No AI, API, FFmpeg, yt-dlp, new transcription,
download or operator-supplied dataset is required.

The audit demands byte-equivalent regenerated *redacted evidence*,
matching SHA-256 of raw evidence, source transcript/holdout identities,
an exact two-call-per-case request count, the same bounded model
comparison structure, truthful provider/model-route comparability,
exactly the right original source variants, safe failure/quality flags,
and independently recomputed per-variant comparisons and summary counts.
Because the original model quote text was **correctly redacted** in
PR #237, the audit checks each SHA-256 quote hash against the bounded
set of **actual contiguous, valid short quotations** of the current
local source ASR, matching the cited LOST or RETAINED evidence ID.
Hashes for absent quotes or fabricated/invalid source selections fail.
It does **not** copy any human story text, full ASR, validated quote,
raw provider output, source URLs or raw video IDs into the result.

The command defaults to stdout only and returns nonzero on rejection.
\`--output <new>.json\` optionally writes a safe create-only audit
sidecar with only a receipt hash, evidence hash, counts, fixed
rejection codes and explicit non-qualification flags. Changing the
source ASR, saved evidence, model route, cited quote hash, status,
pairwise agreement or aggregate counters invalidates the receipt;
holdout reservation conflicts also block validation.

**Important limitations:** A reproducible quotation hash and internal
receipt consistency do not authenticate the original model's identity,
establish that the model actually produced the recorded answer, or
verify Japanese meaning, audible speech, creative editing success
or human quality ratings. SHA-256 on a JSON file is **not** a
signature. This is a consistency audit of untrusted in-sample
observations, not a production/auto-edit/publication gate.
\`ready=false\` is unchanged.

### Source-ASR-backed trial cut extensions (PR #239)

\`uv run holoclip render-cut-extension-trials\` derives **alternative
candidate cut boundaries** for existing QA-passed generated preview
timelines. This advances beyond observation-only differences: the
output has both the **original complete ordered source-cut timeline**
and a concrete **trial ordered timeline** that extends one existing
cut's start or end. It still does NOT execute FFmpeg, alter the queued
render, replace the original MP4, or enqueue automatic publication.

The conservative search is restricted to the **single longest
human-only source-time disagreement window actually sampled by the
local transcript context**, when all source URL, original media
identity, holdout reservation and trusted in-sample human-reference
gates passed. The potential extension must add at most 8 seconds and
leave at most 4 seconds of adjacent source-time gap to bridge;
it may not overlap a different selected source cut, reorder a
segment, exceed 600 seconds of edited playback, or cross the
original 0–24h source clock bounds. A candidate must recover
at least 0.25 seconds of previously missing source-time ASR,
matched against original and trial selections. Reports include SHA-256
of observed ASR text, source start/end times, seconds gained,
added playback duration and original/trial source intervals; **no
verbatim speech, raw original YouTube video IDs or file paths**.
At most two trials are suggested per job, 12 per 40-job page.

The command is offline and read-only by default; optional
\`--output <new>.json\` saves an immutable create-only report.
These are **hypothetical edit alternatives** rather than executable
RenderPlans: actual source file duration/decoding, audible speech,
sentence completeness, setup, jokes, payoff, human preference and
creative improvement remain **unverified**. A trusted human training
cut is NOT a heldout gold edit, and even source-clock ASR coverage
does not show that extending the edit is desirable. Future work
can perform distinct local MP4 trial renders plus genuine before/after
blind review, with rollback and independently measured quality.
\`ready=false\` and no auto-edit/auto-publication remain mandatory.

### Local MP4 A/B renders for ASR-backed cut extensions (PR #240)

Use \`uv run holoclip render-cut-extension-mp4-trial\` to automatically
select the best source-ASR-grounded edit extension from the current
page of existing completed jobs (or select a specific \`--job-id\`,
\`--proposal-index 0|1\`). The default is **read-only**, prints the
original/source and trial ordered timeline, filenames, baseline SHA
from earlier QA, reason/evidence source hashes, and explicitly reports
\`LOCAL_EXTENSION_AB_TRIAL_NOT_RENDERED\`.

To perform a **new local-only trial B**:
\`uv run holoclip render-cut-extension-mp4-trial --execute --approve-local-preview\`.
This re-derives the candidate from today's original QA-passed queue,
trusted in-sample same-source human alignment, current source ASR and
holdout protection; it **does not ingest a user-supplied trial
manifest**. It loads the exact original stored \`ClipTimeline\`,
verifies that every candidate baseline interval matches the stored
output-ordered segment and changes exactly one existing segment.
That segment gets a fresh deterministic ID, confidence zero, no
invented source-segment IDs/narrative role; all other segments stay in
original output order. The trial remains capped at 180 seconds.

Before FFmpeg, the command checks the **original QA-passed MP4**
in the exact preview directory, rehashes its bytes against the
\`render_jobs\` SHA, probes its existing audio/video streams and
measured duration, resolves only a verified **full original
editing_video local cache**, re-evaluates the original stream's
current \`RENDER\` policy against the **extended** source range, and
compiles a normal safe \`RenderPlan\`. FFmpeg runs through the
existing local renderer, producing a separate,
deterministically named \`renderqueue_<id>-extension-<id>.mp4\`
in the same preview directory. The renderer already checks source
hash/size/ffprobe before/during generation and uses a no-overwrite
hardlink commit. The original MP4 is hashed again after the trial;
a changed original is not reported as a valid comparison.
A separate create-only JSON receipt records both A and B checksums,
measured durations, source evidence hashes and technical-only QA.
An existing trial MP4 or receipt blocks reruns (no replacement or
automatic retry).

**These are real A/B MP4 files**, but not yet visually/editorially
equivalent treatments: A may have captions or post-audio processing
that the basic new B does not. The receipt expressly marks
**subtitle, audio quality, human quality and creative improvement as
unmeasured**. Technical audio/video QA is not editing-quality QA.
The result never becomes the active render or modifies the database,
original, source cache, user subtitles, or publishing state.
There are **no external API, yt-dlp, download, new ASR or Docker
requirements**. The additional candidate is in-sample training
reference, never independently rated human gold. Keep \`ready=false\`.

### Matched optional audio and caption finishing for extension MP4s (PR #241)

\`uv run holoclip render-cut-extension-finished-ab\` re-derives the
original QA-passed A and bounded ASR-grounded B extension candidate,
then reads the **original queue's recorded audio leveling, source-SRT
and burned-caption evidence** (read-only SQLite). The default command
only previews the stage-presence plan and does not start FFmpeg or
create files.

Use \`--execute --approve-local-preview\` for a **single local-only
flow**: first create PR #240's separate trial MP4 if it does not yet
exist (or rehash and verify its original PR #240 JSON receipt), then
apply exactly the optional finishing stages recorded for baseline A.
Every successful A artifact must exist at the canonical sibling path
with its original SHA-256; undocumented orphan outputs or untrusted
queue evidence are rejected rather than silently substituted. The
baseline full A/V final is probed before running any trial finishing.
Trial captions come from the *current canonical source* and revised
trial edit clock; audio uses the established cut-aware bounded dBFS
gain/limiter; subtitles burn into the leveled or base trial following
A's recorded choice. Stage processing uses existing no-overwrite
and rehash checks. A sidecar named \`*.matched.json\` records original
and trial finishing checksums, stage outcomes and duration evidence.
Both the original base MP4 and its optional QA-anchored sibling artifacts
are rehashed after B finishing; any drift blocks a comparable receipt.
`trial_final_seconds` is the decoded FFprobe measurement of the final B
artifact, while `trial_expected_seconds` is the edit timeline estimate
(they may differ within the bounded technical QA tolerance).

This produces more **technically comparable finishing pipelines**, not
identical captions (B contains extra source time), equal narrative
content, or the same codec artifacts. Trial stage failure creates a
clearly marked **NOT COMPARABLE** receipt rather than claiming success;
missing/ambiguous baseline stage records fail closed. The original
MP4, finished A files and original queue remain unchanged. Existing B
media/receipt are only accepted after exact hash and provenance checks;
existing finishing output/receipt names cannot be overwritten.
No new ASR, external model, YouTube/yt-dlp access or Docker required.

Crucially, matching processing stages is **not** a blind listening
or audience-preference comparison. This does not score comedy,
setup/payoff, subtitle linguistic correctness, audio perceptual quality,
or editor preference; no editing is auto-adopted or published and all
\`ready=false\` protections remain. A later A/B evaluation can measure
time-localized audio and caption characteristics on both final outputs
while treating the extra clip content as a confounder.

### Source-ASR speech boundary and real audio evidence for finished A/B (PR #243)

`uv run holoclip render-cut-extension-speech-audit` re-derives the
current source-grounded proposal and uses ONLY the existing successful
`*.matched.json` receipt from PRs #241/#242. It rechecks all original
QA-pinned stages, the unmodified base A MP4 and the precise final trial
B MP4 SHA-256 before returning any diagnosis. Existing manually
rendered A/B assets are reused, never silently generated or replaced.
No user-supplied sample labels or separately prepared dataset required.

The default command is read-only and performs **no FFmpeg decode**. It
checks that the local source ASR's current raw bytes still match the
source-time provenance hash in the verified cut-extension proposal.
It counts fully contained utterances vs utterances straddling the added
source-clock region, plus ASR utterances crossing the old and new cut
boundaries. The text is not exported. Source-clock ASR and metadata are
**not** proof that speech was audible, correctly transcribed, naturally
timed, or funny.

Use `--execute` on the owner's Windows/uv computer to measure at most
one second of the A adjacent region and the newly added B region,
using FFmpeg's audio mean/peak signal measurement. This creates a
new-only `*.speech-audit.json` SHA-anchored receipt; concurrent changes
to A/B or the matched receipt block report completion. Decoder failure
is explicitly marked unavailable; partial measurements are discarded.
No YouTube/yt-dlp/NIM/model requests, new ASR, video rendering, Docker,
automatic cut adoption or publishing are used. There is no
content-preference comparison because these two audio samples contain
different source content; measured dBFS and ASR edges are inspection
signals only. No real owner media are accessible in hosted CI, which
uses offline fakes. `ready=false` always.

### Conservative source-ASR speech-boundary alternative suggestions (PR #244)

`uv run holoclip render-cut-extension-speech-refine` reuses the current
finished extension A/B and #243 speech audit to identify **only cut edges
that interrupt a source-ASR-timed utterance**. It never edits MP4s, source
audio, queue state or ASR. No manual dataset or new YouTube download is
required. Existing local media, trained-alignment holdout gates, canonical
source identity, QA evidence, SHA-256 media hashes and transcript bytes must
still agree with the verified finished trial; missing/stale evidence blocks.

When the outer edge of the extension clips an ASR utterance, a preview may
propose extending that same edge by **at most 2 seconds beyond current B**
and at most **8 seconds total beyond original A**, while not overlapping
any other selected same-source cut or exceeding the bounded edit duration.
The new edge must match a real ASR utterance boundary, reduce the number
of straddling ASR intervals, and increase the number of fully contained
ASR utterances vs original A. Proposals use the existing immutable
ClipTimeline validation, capped at two per checked pair. When no qualifying
edge exists, the tool abstains rather than claiming a quality improvement.

Default output is read-only JSON to stdout. Optional `--output path.json`
saves create-only evidence without overwriting any existing report.
The report contains bounded source-clock timings, source/transcript hashes,
alternative timeline digests and explicit restrictions, but no spoken
text, raw video IDs or arbitrary local file paths. It is **not an actual
audio/video edit**, and source-ASR intervals are not trustworthy word
timestamps, independently listened-to sound, verified comedy context,
permission evidence or proof of an improved final clip. Proposed ranges
require fresh cache/policy/FFmpeg validation before any future rendering.
`ready=false`, no automatic adoption and no publishing remain mandatory.
Hosted CI only uses fake local media and no external APIs.

### Distinct local B2 MP4 from speech-completion candidates (PR #245)

`uv run holoclip render-cut-extension-speech-b2` is a **preview-first**
local A/B/B2 experiment. It reconstructs the current canonical A and
finished B from queue, source manifest, holdout, local-ASR identity,
FFprobe A/V checks and SHA-pinned `*.matched.json`. It invokes #244's
bounded source-ASR extension refiner again: no fresh suitable alternative
means **no B2 can be rendered**. It never accepts an arbitrary caller
supplied edited timeline or external file path.

The command defaults to a read-only summary, without starting FFmpeg.
`--execute --approve-local-preview` explicitly authorizes one new B2
MP4 made by the existing offline local FFmpeg preview renderer.
B2 is named separately from both A and B, is created only if absent,
and still requires fresh canonical source cache mapping, source permission,
current RENDER policy and measured source A/V ranges. The original A,
finished B, source cache, QA rows and edit queue are never overwritten.
The isolated B2 must pass basic video+audio and bounded duration QA.
Before writing its create-only JSON receipt, both A/B and relevant
base files are rehashed and their original QA/ASR lineage re-evaluated
after the render. The receipt records A/B/B2 exact media hashes,
measured runtimes, source-timeline provenance, technical QA and
differences in *duration only*.

B2 is a **basic encoded preview**, while A and B refer to their
previously completed optional leveling/caption finishing. Thus B2 does
**not** have equivalent postprocessing, even if all three contain A/V.
`b2_caption_audio_finishing_matched_to_a_b=false` explicitly forbids
claiming postprocessing parity. The three-way result is not listening,
narrative or human preference evaluation; it does not claim that the
new cut is interesting or publishable. Applying matched optional audio
and subtitle finishing to B2 requires a separate follow-up.
`ready=false` with no adoption or publication. GitHub CI uses synthetic
local media and never downloads YouTube streams or probes user PCs.

### Matched optional local finishing for the distinct B2 trial (PR #246)

`uv run holoclip render-cut-extension-speech-b2-finish` continues
the existing source-ASR-grounded A/B/B2 work *without a new download or
manual dataset*. It requires that both original A and completed B have
their existing SHA-verified optional audio/SRT/burn-in evidence and
`*.matched.json` receipt, and that an already-rendered B2 MP4 has a
matching `*.json` technical receipt. No missing B2 is silently rendered
by this command. The current queue, canonical source, holdout reservations,
source policy and local ASR must still agree.

By default it only reports the expected optional stages without modifying
media. `--execute --approve-local-preview` explicitly runs the
**same available optional processes** as original A: cut-aware audio
leveling, cut-aligned Japanese SRT, and caption burn-in when A actually
has those stages. Existing B2 siblings and the matched receipt are never
overwritten. Optional failure yields
`LOCAL_B2_FINISH_INCOMPLETE_NOT_COMPARABLE`, **not** a success claim.
On success a separate create-only
`<B2 basename>.matched.json` receipt records a SHA-pinned B2 final
MP4, stage statuses and actual decoded final runtime distinct from
timeline estimate. A and B media, original B2, stage files and queued
jobs remain untouched; existing A/B hashes, source-ASR proposal, B2
receipt, A/B optional stage bytes and B2 source bytes are rechecked
after potentially lengthy FFmpeg work.

`stage_comparable_to_a_and_b=true` means only that the **same types**
of audio and caption postprocessing were applied. It does not imply
identical dialog, content, editing quality, SRT accuracy, viewer
preference, loudness perception or publication rights. No independent
real-world video/creative evaluation was conducted by CI; synthetic
offline tests are used. Auto-adoption and publication stay disabled;
`ready=false` even on successful technical matching.

### Real-local three-way technical quality diagnostics for finished A/B/B2 (PR #247)

`uv run holoclip render-cut-extension-three-way-diagnostics` requires
**all three existing finished local videos** and their SHA-pinned A/B
matched-stage receipt, B2 technical-render receipt, B2 matched-stage
receipt, current queue and source-ASR evidence. It reconstructs the
trusted local three-way context itself; operator-supplied JSON never
substitutes for a canonical source, permission, holdout or QA record.

Without flags the command is **read-only and decoder-free**, reporting
the three pinned video identities and source-clock-only hints.
`--execute` is an explicit read-only FFmpeg inspection of A, B and B2
plus a new, immutable local JSON report (no modification of input MP4,
subtitle files, queue, source cache or ASR). Each video is independently
checked for decoded A/V and bounded duration, with a scan of blackout
intervals, frozen visuals, a bounded audio dBFS sample, SRT cue speed
and width flags. All input media and stage receipts are SHA-checked
before *and after* scanning. Decode failure, incomplete B2 finishing,
missing captions when expected, changed original A or stale source-ASR
evidence block recording a three-way result. No YouTube contact or
external model/provider is required.

The **baseline A SRT** is pinned by its canonical stage evidence hash.
Older **B SRT** provenance was not independently signed by its earlier
matched receipt, and **B2 SRT** similarly lacks a dedicated persisted
stage-content hash. For these, current SRT bytes are recorded in the
in-process snapshot and must remain unchanged until scanning finishes;
this is *continuity, not proof of correct transcript contents*. The
caption parser detects invalid, fast and wide cues only; the ASR's
linguistic correctness must be evaluated separately. Reports contain
no speech text or arbitrary local paths.

The sidecar records measured per-version local technical warning counts,
timestamps, caption flags and audio signals, along with numeric flag
*differences*. Fewer flags **do not** establish better comedic timing,
sound intelligibility, narrative cohesion, faithful subtitles, actual
viewer preference or publishing rights; different source footage may
naturally change flag counts. The tool never picks a winning A/B/B2
variant, never promotes one into the render queue and never sets
`ready=true`. Hosted CI tests use synthetic local media and mocked
decoder results, not a real Hololive stream. Owner-local real video
evidence and later semantic/human evaluations remain unqualified.

### Three-way edit review triage from verified local measurements (PR #248)

`uv run holoclip render-cut-extension-three-way-review` derives a short
review **worklist**, not a creative score, from an already measured,
immutable `*.three-way-diagnostics.json` (PR #247) and current
canonical source-ASR and SHA-verified A/B/B2 finished videos.
The command **does not decode again**, consult an LLM, download sources,
modify an MP4, alter an SRT or update any editing queue. By default it
prints JSON to stdout; `--save` optionally writes one create-only
`*.three-way-review.json` alongside the existing three-way evidence.

The most useful first checks are 2 seconds before through 3 seconds
after the **same source-grounded edit transition in A, B and B2**.
Their output timestamps differ when a source interval was extended;
absolute output clocks must not be used to imply content alignment.
Each review target records variant, source-cut index, output-clock
interval, evidence class and a fixed question about complete speech
and natural pacing. Valid time-coded FFmpeg/SRT observations from the
saved technical scan become additional concrete audio, video and
subtitle checks. Existing `candidates_for_review` maps technical
observations to canonical segment IDs, rejects out-of-range and
non-finite time stamps, and never treats anomalies as objectively
bad comedy. Review target order is deterministic and intended to
save inspection time, not predict the winning clip.

Before displaying or writing targets, the system re-derives the
job/source/holdout/ASR context, A/B/B2 MP4 hashes, optional-stage
state and source policy lineage. It requires completed measured
three-way evidence and matching source/receipt SHA-256. The persisted
diagnostic JSON is **not cryptographically signed**; ordinary local
file tampering cannot generally be ruled out by a matching self-report
alone. Explicit structural and cross-field consistency checks reject
stale/contradictory entries; manual and real-media verification remain
necessary. `--save` performs one more evidence comparison before
creating the report and never overwrites an existing output.

The worklist explicitly leaves these questions **unanswered** until
someone listens/watches: whether the setup is understandable, the
payoff and reaction finish, pauses are intentional, and subtitles
faithfully match speech. A training-set human-made clip and source-ASR
times are **not** independent proof of those answers. No variant
receives an entertainment score or automatic promotion; `ready=false`
and publication remain blocked. CI uses synthetic evidence only,
without YouTube/yt-dlp or owner-local footage.

### Cited source-ASR hypotheses for A/B/B2 narrative review (PR #249)

`uv run holoclip render-cut-extension-three-way-dialogue` builds a
source-clock-grounded A/B/B2 **text evidence preview** after the
#248 review plan and #247 measured diagnostics already exist. It rechecks
the original canonical source database, holdout protections, SHA-verified
A/B/B2 videos, matching finishing receipts and local source transcript
without any new downloads or manual inputs. It uses only source-ASR
utterances **fully contained** in a selected clip interval, maps their
timestamps to each edit's output clock and samples a bounded region
around the equivalent cut across A/B/B2. Partially cut utterances
are *counted* as warnings but their truncated words are not invented.
The current pilot is fail-closed for multi-source compilation clips until
per-source identity and matching provenance are supported.

The default command reads metadata and returns **no ASR text**, invokes
no model, writes no files and does not decode MP4 again. `--save`
writes a separate create-only, text-redacted evidence metadata JSON
without inference. `--execute` is a **different explicit opt-in** and
requires `HOLOCLIP_DATA_CLASS=public`; it sends a limited set of
existing source-ASR excerpts to the configured **zero-cost-only**
provider route, never to paid fallback. With no qualified quota or
model, inference fails closed. Its response is rigorously checked:
exactly nine variant/question hypotheses for `CONTEXT`, `PAYOFF`
and `SPEECH_EDGE`, only `POSSIBLE_TEXTUAL_SIGNAL` or
`INSUFFICIENT_EVIDENCE`, with every positive hypothesis quoting a
literal contiguous substring of an existing same-variant, fully
contained ASR excerpt. Invented evidence IDs, duplicate question
claims, unrelated variants, fictional quotes or unverified labels
are rejected. The current ASR/source/media provenance is checked
again after model inference, before immutable receipt creation.

**This is NOT actual narrative quality assessment**: it detects only
*possible textual signals* in unverified ASR. It does not listen to
video/audio, recognize speakers, validate captions or decide that a
setup is understandable, an actual punchline lands, or speech is
natural. NIM-generated hypotheses cannot be used as human feedback,
holdout gold, creative scores, clip rankings, automatic editing,
production readiness or publishing authorization. `ready=false`.
Hosted CI tests use deterministic offline simulated ASR and mock
model responses only; no actual model or YouTube streams are touched.

### Source-timed local acoustic samples from finished A/B/B2 (PR #250)

`uv run holoclip render-cut-extension-three-way-acoustic` continues
the SHA-linked PR #247–#249 review work without new downloads, manual
reference media, external AI services or a GPU. It requires original
local A/B/B2 rendered MP4s, their finishing and technical receipts,
current source-ASR provenance, stored three-way decoded video diagnostics
and an unchanged canonical review plan. Only a *single, consistently
identified source asset* is supported by this pilot.

The default invocation is decoder-free and read-only. It prints
the expected *output-clock* acoustic sample windows for each A/B/B2
variant (the shared changed edit, and up to three source-ASR utterances
fully included in that version), without transcript text, arbitrary
file paths or detected content. `--execute` explicitly samples the
actual **finished** MP4 audio on the local computer using FFmpeg
`volumedetect` over small bounded windows. There is no upload or
network activity. Samples report mean dBFS, peak dBFS and a conservative
near-silent-energy *hint*, together with prior local video blackout/freeze
counts from PR #247. The current media, optional SRT, ASR, all receipts
and saved diagnostic evidence are rechecked after audio processing.
The result is create-only
`*.three-way-acoustic.json`; missing tools, invalid acoustic values,
stale evidence or any changed input fails before the receipt is saved.
Original MP4s, subtitle files, ASR and render queue are unchanged.

This is **acoustic presence evidence, not speech recognition**:
BGM, sound effects, long pauses and compressed audio can all affect dBFS.
Non-silent audio does not verify words or a speaker, and near-silence
does not imply an editorial mistake. The previous video scan similarly
finds only technical black/frozen frames; no new vision understanding
is performed here. ASR/source-clock comparisons are approximate, and
model claims from PR #249 are not treated as audio-verified. No A/B/B2
"winner", actual narrative completion, payoff success or viewer
preference is asserted; `ready=false`, no adoption/publishing.
Hosted CI mocks bounded FFmpeg samples and tests local SHA consistency
only, not a real Hololive stream. Actual listening/visual inspection
and creative qualification are still outstanding.

### Second-pass ASR on actual A/B/B2 edited audio, not missing-speech proof (PR #251)

`uv run holoclip render-cut-extension-three-way-clip-asr` adds the first
**independently transcribed edited-audio** observations to the A/B/B2
source-ASR and local MP4 evidence chain. Prerequisites are completed
A/B/B2 matched finishing (PR #246), all three decoded technical scans
(PR #247), source-clock editorial worklist (PR #248), source-ASR context
(PR #249) and the canonical current queue/source data. The command
re-derives each identity/holdout decision and original/final MP4 and
stage SHA-256; no uploaded JSON or user-supplied reference data is needed.

The default command is fully local, read-only and transcript-text-free.
`--execute` requires **`HOLOCLIP_DATA_CLASS=public`**, an existing
zero-cost-qualified ASR route and working local FFmpeg. It extracts a
short, fixed **at most 8-second** mono MP3 sample from the same edited
source-time boundary in each completed A/B/B2 MP4. It uses isolated
temporary files only; each is deleted after its provider response,
including on failures. Exactly these edited audio crops—not source
archive media—are sent to the existing cost-controlled `MeshTranscriber`
ASR provider. If no zero-cost route, quota or consent is available,
nothing is uploaded or silently diverted to a paid option. There is
no Docker, GPU prerequisite or yt-dlp call.

Each provider transcript is length- and timestamp-bounded and compared
with **only ASR utterances fully contained** in that version's edited
focus window. Unicode normalization supports Japanese; exact normalized
containment and a SequenceMatcher text-similarity *proxy* are reported
per cited source-ASR segment. Low overlap prompts review and can reflect
truncated speech, missing speech, inaccurate source or edited-ASR text,
BGM, laughter, a short crop or ordinary recognition noise. Unmatched
utterances and repetitions are **not independently proved**: neither
Whisper nor source-ASR is human-verified speech truth. No ASR words,
local paths or audio bytes are emitted in the new immutable report.

The original source ASR, source reservation/holdout, technical
diagnostic receipt, MP4s, SRTs and all reference receipts are verified
again **after** provider inference. Any change, quota/decode error,
invalid response or existing output file blocks committing the report.
`ready=false`, no winner, auto-adoption or publication. Hosted
Linux/Windows CI checks synthetic videos with mock FFmpeg/ASR, not real
Hololive data or provider access. Production creative and audible
qualification remains unfinished.

### Time-anchored Japanese ASR discrepancy review for A/B/B2 (PR #252)

The existing `uv run holoclip render-cut-extension-three-way-clip-asr
--execute` (PR #251) now attaches a **time-local, source-evidence-cited
review** for each independently recognized finished-clip utterance.
The input remains the same limited crop of each already finished A/B/B2
MP4, the same zero-cost-only public-data-gated ASR route, and the
source-ASR segments that are fully contained within the source-aligned
changed-cut focus. No additional upload, downloader, GPU, Docker or
human-prepared annotation data is required.

Instead of assessing only a concatenated transcript, Holoclip converts
each ASR observation's crop-relative timestamp to the video's **output
clock**, then compares it to each source-ASR citation's expected output
start/end. A phrase split by the ASR into two adjacent observations may
be matched as a bounded two-segment window. A single observation may
explain two distinct source citations; it is never force-matched
one-to-one. The result records source evidence ID, both clock
intervals, segment number(s), an approximate character-level similarity
and midpoint time skew. Its suggested review statuses distinguish:
- approximate text agreement at the expected time;
- *possible* utterance truncation/partial text;
- text agreement at a surprisingly distant playback time;
- repeated plausible text occurrences (ambiguous);
- short Japanese acknowledgments that cannot be reliably resolved;
- low textual overlap, absent recognized text, or independent ASR
  segments without a corresponding sampled source reference.

The bounded algorithm is deterministic and exports **no spoken words**
or arbitrary local file paths. It tolerates Japanese without spaces,
punctuation/width differences and segmentation discrepancies. Every
timestamp is checked to stay within the canonical cropped video clock.
The original #251 immutable evidence, holdout/ASR lineage, output MP4
hashes and source-SRT proofs still fail closed if stale. The preview
remains fully offline and provider-free. A report created by an earlier
#251 version is not silently rewritten; the new result uses the
version 2 schema and retains create-only output semantics.

None of the flags prove an actual dropped or repeated spoken phrase.
Two ASR passes can hallucinate the same words or disagree because
of laughter, BGM and compression; ASR time stamps are segment clocks,
**not word-level forced alignment**. Observed segment count, warning
count and text similarity are never used to select the "best" A/B/B2,
auto-edit, publish or claim that a joke landed. `ready=false`
remains mandatory. CI uses offline mocked ASR/media and Japanese
synthetic examples; actual footage listening and independent validation
are still required.

### Batch pilot from existing finished local A/B/B2 media (PR #253)

`uv run holoclip render-three-way-existing-media-pilot` is the
**no-manual-reference-data** route for owner-local measurements already
supported by PR #247–#252. It discovers up to **2** (at most 4 via
`--max-jobs`) existing `COMPLETE` source jobs in the original
SQLite queue. Only source-identity/holdout-approved, fully finished
and SHA-pinned A/B/B2 triplets can be used. No actual media is downloaded
or rendered. The discovery command is default read-only: it reports
which completed triplets could support a technical/voice-energy scan.

With explicit `--execute`, the pilot uses the existing FFmpeg technical
diagnostics for A/B/B2, followed by bounded **finished-MP4 audio energy
samples** and preserves their previous create-only local JSON receipts.
Existing receipts are revalidated against original source ASR, current
MP4/SRT hashes and matched A/B/B2 lineage; there is no overwrite,
silent repair or automatic fresh scan of an untrusted existing receipt.
Partial runs with a valid technical report can resume the acoustic step.
Results are concise counts and opaque job IDs; **no ASR words, local
paths or media bytes** are returned. Automatic discovery is bounded
and does not initialize a missing queue. The command never runs yt-dlp,
requests remote ASR/LLM, uploads media, installs tools, publishes or
chooses a variant. For a specific existing source job use `--job-id`.
`--proposal-index` and `--refinement-index` can select an existing
alternate (both default to zero); no new B/B2 renders are generated.

**Distinguish real host media from CI fixtures:** GitHub Actions has
no owner-local footage. Its offline deterministic fixture runs verify
the orchestration and failure modes, not perceptual or visual/audio
content accuracy. Running `--execute` on a machine with a genuine
finished triplet measures that actual local media and adds source-linked
evidence, but black frames, audio dBFS and subtitle flags are **technical
hints only**, never proof of creative quality. The independent public
ASR flow in PR #251 remains separate and explicit to avoid involuntary
uploads. Real footage may not yet be locally available; such jobs
are skipped rather than invoking yt-dlp or fabricating results.
There is no automatic winner, edit adoption or upload; `ready=false`.
See `docs/roadmap.md` for pending real-world listening, context and
viewer-quality qualification.

### Read-only prerequisite doctor for existing A/B/B2 footage (PR #254)

Run `uv run holoclip render-three-way-existing-media-doctor` on
the local Windows machine. The command scans at most two completed queue
jobs by default (up to four with `--max-jobs`), using the existing
trusted local human-reference/holdout/source-ASR records and the current
SHA-linked media/receipt chain. Unlike the #253 pilot, it explains the
**first missing verified stage** instead of returning a generic
"triplet not qualified" result.

Its possible outcomes are `RENDER_B`, `FINISH_AB`,
`NO_VERIFIED_B2_REFINEMENT`, `RENDER_B2`, `FINISH_B2`,
`MEASURE_TECHNICAL`, `MEASURE_ACOUSTIC`,
`EVIDENCE_COMPLETE`, `NO_VERIFIED_AB_PROPOSAL`, and
`BLOCKED_UNVERIFIED`. A known safe next-stage command name is included
only if the entire preceding chain is present, current, identity-pinned,
and valid; no options or local paths are automatically interpolated.
The B and B2 steps require the existing command's explicit local-preview
approval. Stale/partial output files, including descendants left behind
after an interrupted render or finishing attempt, are blocked rather than
described as "not yet generated." Missing/unsafe source data and absent
speech-completion proposals are **not** manufactured as valid candidates.

The doctor does not execute any recommended command: no yt-dlp,
local rendering, media frame decoding, network APIs, file repair or
publishing. It uses the same bounded queue discovery as #253; it only
reports verified stage names and counts, opaque job IDs, and bounded
fixed reason codes. Unmodified local reports remain unsigned measurement
records, not independent editorial/semantic truth. Even
`EVIDENCE_COMPLETE` means **technical+acoustic sidecar evidence
only**, not that a joke landed or the cut is ready to publish.
`ready=false` and blocked auto adoption/publication are invariant.
Run each recommended local command explicitly (see above), then rerun
the doctor. The #253 one-command `--execute` automatically handles
the final technical/acoustic measurement stages when a finished triplet
is available. GitHub CI exercises only offline synthetic files.

### Guarded offline A/B/B2 one-command stage executor (PR #255)

`uv run holoclip render-three-way-existing-media-autopilot` uses the
read-only PR #254 prerequisite doctor to choose the **next canonical
already supported offline step**, and defaults to **preview-only**
inventory. Its output gives each completed job's first/final stage,
executed stage names and a bounded step count without raw source ASR,
local paths, or media bytes.

`--execute` permits at most six new-only local processing stages in
total (by default one selected job, `--max-jobs` max four). This
includes existing `RENDER_B`, `FINISH_AB`, `RENDER_B2`,
`FINISH_B2`, `MEASURE_TECHNICAL`, and `MEASURE_ACOUSTIC`
actions. **Any rendering or finishing** requires the further explicit
`--approve-local-preview`; measurement-only stages do not.
Both options are required for an end-to-end production of isolated
*unqualified preview videos* from an already finished original stream.
Use `--max-steps 1` to execute one bounded step at a time.

Each stage is re-derived from the existing queue, verified source ASR,
holdout/reference evidence and SHA-256-linked output before and after.
An exclusive create-only per-job local lock blocks competing autopilot
runs; a stale lock is not silently removed. The executor never repairs
an existing file or overwrites a prior sidecar, and stopped or partial
stages cannot be skipped. Restarting the command after a safe stage
reuses the completed evidence automatically. If no verified source
proposal/B2 refinement exists, or if a file is stale/ambiguous, it
stops rather than inventing a candidate. It never runs yt-dlp, contacts
external LLM/ASR, mutates the original baseline or publishes.

**This is offline workflow completion, not autonomous editorial
qualification.** B and B2 are only render/QA experiments,
independent source text semantics and human-quality judgments remain
unverified, `ready=false` even when technical/acoustic evidence is
complete. GitHub Linux/Windows CI exercises only synthetic fixtures
and never renders owner footage. Use the PR #254 doctor and #253 pilot
to audit any locally remaining prerequisites and measured sidecars.

### B2/B3 source-context visual + audio review pack (PR #266)

After the locally held B2/B3 ASR discrepancy receipt and PR #265's
source-pinned priorities are available, inspect the first/highest priority
unrated target without any media operation:

`uv run holoclip render-b2-b3-local-review-pack`

This is strictly **read-only**. It maps the prioritized source-ASR clock
target into both B2/B3 output clocks using each canonical cut timeline.
An interval absent from a version is explicitly marked
`selected_source_clock_visible=false` and the candidate
uses that version's local edit seam for comparison, **not** fake
corresponding utterances. Duplicate source-clock appearances are similarly
not treated as uniquely aligned. This stage uses local evidence/ASR, no
source video download, FFmpeg or model/provider request.

To create short local videos to inspect side-by-side, approve exactly one
target (zero by default):

`uv run holoclip render-b2-b3-local-review-pack --execute --approve-local-preview`

Optionally `--target-index 1` etc (0–19, bounded by the
available priority list). This requires the **pre-existing full** locally
cached original stream, whose YouTube ID/source asset identity is
independently verified. It creates exactly three new independent MP4s:
source context (~6 seconds), B2 context (~5 seconds) and B3 context
(~5 seconds), each with original video and audio, at the mapped
source clock where present or the edit seam where absent. Encoder and
decoder are fully local. FFmpeg stream decoding may be required, but
neither source nor B2/B3 original files are rewritten. Completed
A/B/B2/B3 lineage, source ASR, saved edited ASR and four-way QA are
rederived/rehash-checked before and after extraction, and each new MP4
must pass video+audio stream, SHA and duration checks. All three outputs
and a text-free QA receipt are **create-only** and named per review
target. In case of a failed write, the command attempts to roll back
only outputs it created; an existing or orphaned output blocks reruns
rather than being overwritten.

The outputs are intended for **viewing/listening**, not independent
proof of which edit sounds more natural, whether words are missing,
whether an emotionally important setup/payoff is preserved, or which
variant is more entertaining. A passing pack means only three short
files were technically created. Nothing is automatically adopted,
uploaded or published and `ready=false`. Hosted CI runs
synthetic media/stub encoders only; actual Holo source footage still
needs independent owner-PC evaluation and permission review.

### Source-pinned B2/B3 audio review priorities (PR #265)

After existing PR #264's **explicitly approved** short ASR observation is
saved, inspect the already finished B2/B3 evidence using:

`uv run holoclip render-b2-b3-audio-review`

This is **read-only**: no decoding, additional ASR, model/provider call,
media creation, network access or uploaded words. It rechecks the current
source transcript, finished-media hashes, four-way technical/acoustic receipt,
and the saved B2/B3 ASR alignment receipt. If the ASR observation does not
exist, it reports `ASR_NOT_MEASURED`/unknown rather than
zero findings or a passing quality decision. Unsupported/tampered/stale
alignments fail closed. A saved ASR receipt has integrity-bound lineage
but is **not cryptographically signed** and cannot establish ground truth.

When ASR evidence exists, the command produces **unrated, prioritized
review targets** for: new B3 ASR textual warning on a shared source-ID
utterance, warnings in both variants, only-in-B2/only-in-B3 source-ASR
intervals, decreased warnings still requiring listening, more B3 technical
flags, and B3 edit-boundary near-silent energy compared to B2. Every target
has an unverified evidence ID and source/output clock where available,
with no transcript text. Priority is *triage order only*, never a
clip ranking or proof of audible speech, naturally preserved sentence
endings, continuity or comedy.

To persist a **new immutable** JSON review plan, use:

`uv run holoclip render-b2-b3-audio-review --save`

It re-verifies the source, four-way QA, ASR receipt and all media lineage
again before create-only writing. Existing files are never overwritten.
This step never requests public-data classification because no audio is
sent; actual independent ASR continues to require the separate opt-in
command from PR #264.

Always `ready=false`, no auto winner, edit adoption or
publication. Real footage and a hearing-based/editorial evaluation remain
unqualified.

### Independent finished B2/B3 audio-ASR cross-check (PR #264)

After PR #262 has measured **all four** finished videos and saved an immutable
four-way technical/audio diagnostic, use this single existing-job command:

`uv run holoclip render-b2-b3-finished-audio-asr`

By default, it **only checks local saved files**: A/B/B2 source/edit
lineage, B3 matched-finishing SHA, completed four-way diagnostic receipt,
current source transcript, and exact finished B2/B3 MP4s. It reports the
source-clock fully contained utterances and bounded focus clocks for each
variant without decoding audio, uploading any file, exporting words, or
creating a receipt. A missing, stale, corrupted or unsigned/untrusted prior
artifact fails closed; a JSON receipt cannot by itself prove real hearing.

**Opt-in execution** requires both an explicit flag and data classification:

`HOLOCLIP_DATA_CLASS=public uv run holoclip render-b2-b3-finished-audio-asr --execute --approve-audio-asr`

On Windows PowerShell set the environment variable in the current process
first. The owner must explicitly classify the source as shareable public data:
finished **audio snippets may be transmitted to the configured no-cost
ASR provider**. A 3–5 second bounded local crop around the matching
edited source seam of **each** finished B2/B3 clip is temporarily
extracted; crops are removed on return or failure. There are no source
downloads, no change to original MP4s, no LLM creative judge and no
automatic retries on quota failures. Only a new immutable text-free JSON
receipt is saved after both ASR requests, source/MP4 hashes and the
four-way QA evidence are rechecked.

The comparison records (a) source-ASR clock utterances **newly fully
included in B3** rather than B2, and (b) increased/decreased time-local
**ASR-to-ASR textual warning hints for utterances present in both**.
Different recognition or ASR segmentation can change these counts.
Newly included *source-ASR* words are not proven audible, no missing
words/repeated words are independently demonstrated and fewer warnings
do not certify better dialogue, humor, continuity or technical/creative
readiness. `ready=false`; no winning variant, adoption
or automatic publication. Real owner-host video still needs listening
and held-out editorial-quality qualification.

### Four-way video/audio technical comparison (PR #262)

With finished A/B/B2 **and separately approved B3 matched finishing**
already present, inspect one selected existing job using:

`uv run holoclip render-four-way-existing-media-diagnostics`

The default is **read-only** and does not start any video or audio decoders.
It checks all four existing versions, hash-pinned source ASR, A/B/B2 technical
and acoustic prerequisites, independent edited-audio ASR source lineage,
the guarded B3 render receipt, and the SHA-verified B3 matched processing
receipt. Missing or tampered evidence blocks the comparison.

To **explicitly authorize a bounded local technical and audio scan**:

`uv run holoclip render-four-way-existing-media-diagnostics --execute --approve-local-preview`

The four already finished MP4s are analyzed with the same existing
frame-event, subtitle-rate and volume-diagnostic backend. Additional
bounded (at most 0.6 seconds each) audio-energy probes are measured just
before/after the changed cut boundary in each variant's *output clock*.
The resulting immutable, create-only, text-free
`.four-way-diagnostics.json` records media/receipt hashes,
the measured flags and local energy observations, and explicit B3-minus-B2
technical-count differences. Every parent video/stage/receipt and the source
ASR is rechecked after decoding; an altered or missing asset fails closed.
No original, prior output or B3 file is overwritten.

**Important:** These output-clock windows are relative edit-boundary
positions, **not** a claim of the same phoneme, spoken words, or source-clock
context in each variant. Lower energy or fewer black/freeze/video/subtitle
flags alone are not proof of speech completeness or improved comedy.
This analysis does not rank clips, automatically adopt B3, perform remote
inference or publish. Independent listening, actual spoken-words review,
creative quality and real owner-PC media qualification remain pending.
Always `ready=false`.

### B3 matched optional audio and caption finishing (PR #261)

After the guarded B3 MP4 render in PR #260, use:

`uv run holoclip render-three-way-b3-matched-finishing`

This is a **read-only** examination of the existing B3 technical QA receipt,
current source-ASR and edited-audio ASR hashes, canonical A/B/B2 source
and finished MP4s, and already observed baseline A/B/B2 finishing stages.
It does not start FFmpeg or rewrite any video.

`uv run holoclip render-three-way-b3-matched-finishing --execute --approve-local-preview`

This separately authorizes B3-only audio leveling, source-ASR SRT export,
and subtitle burn-in **only for stages that were successful on the
existing A/B/B2 comparison**. B3 follows identical optional processing
types, not a bespoke or favored variant. Newly created B3 sibling assets
(`.leveled.mp4`, `.srt`,
`.captioned.mp4`) and a distinct
`.matched.json` receipt are create-only. Any pre-existing
target or stale lock blocks execution; no user files are overwritten.

Before and after optional stage execution, the source, finished A/B/B2
and B3 technical receipt are verified again and sibling stage hashes are
checked. The matched receipt is issued only if predecessor assets remain
unchanged and the output is checked for SHA, stream and duration. Optional
stage failures are recorded as **incomplete, not comparable**, never as
an improved edit. A/B/B2, the source MP4, and raw B3 all remain untouched.

This ensures *treatment parity* with A/B/B2, **not** timing or linguistic
correctness, real speech completeness, a humorous payoff, or an independent
four-way acoustics/visual assessment. Actual B3 vs B2 acoustic measurement
and creative human/independent evaluation remain pending. `ready=false`
and publication never occurs. CI uses synthetic assets and mock encoding;
owner-PC video must still be qualified.

### Guarded one-candidate B3 local render and basic QA (PR #260)

After PR #259 identifies a supported unrendered B3 boundary hint, use:

`uv run holoclip render-three-way-b3-local-preview`

By default this is read-only: it checks A/B/B2 completed technical and
acoustic evidence, SHA-pinned original source, the existing opt-in edited
audio-ASR receipt, and the supported B3 cut hint. It returns the isolated
B3 output filename and expected duration. If the source/edited-audio
evidence does not justify a B3 proposal, it reports no eligible candidate
and creates no output.

To **explicitly authorize exactly one new local MP4** (no automatic
publishing or edit adoption):

`uv run holoclip render-three-way-b3-local-preview --execute --approve-local-preview`

`--job-id`, `--proposal-index`,
`--refinement-index`, and `--candidate-index 0|1`
are supported for selecting an existing source job and a proposed
alternative. Every execution acquires the same create-only per-job
lock as the A/B/B2 autopilot. Stale locks are not stolen. The candidate
is reconstructed from current evidence both before and after local render.
FFmpeg produces a *new* deterministic B3-named file via the existing
source-permission-gated renderer, followed by basic A/V stream,
duration and hash checks. A separate immutable JSON receipt records
A/B/B2/B3 durations and the current canonical lineage, without
transcript text or local machine paths.

A/B/B2 assets are never replaced. A previous B3 MP4 or sidecar is
never overwritten or silently resumed. An unexpected interruption may
leave a B3-only technical preview without a completed receipt; it must
be reviewed manually rather than trusted or automatically deleted.

**Important limitations:** this is a technically valid, unprocessed
B3 trial, **not** matched B3 caption/audio finishing, four-way
visual or acoustic quality evaluation, audible speech verification,
creative quality improvement, or production approval. The detailed
B3-versus-B2 audio/visual comparison and independently listened
evaluation remain future stages. Always `ready=false`
and no automatic variant selection, publication or network retrieval.
Hosted CI uses synthetic source files and mocked render where required,
not real owner-PC footage.

### B3 boundary hypotheses from corroborated source-clock and edited ASR hints (PR #259)

`uv run holoclip render-three-way-existing-media-review-batch` now includes
`b3_boundary_repair_hints` for each SHA-verified complete A/B/B2 case.
This is an additional **preview-only** field in the existing command, not a
render command, revision of the original video, or a new operator setup step.

A B3 hint is considered **only when both** of these hold: the active B2
editing boundary cuts across a timed source-ASR utterance, and an independently
re-observed *finished B2 audio* ASR text/timing warning is close to that
boundary. The latter is a contextual alert, **not proof** that it corresponds
to the particular source-ASR utterance crossing the boundary.

The proposed outer boundary is the source-ASR utterance start/end, subject
to strict limits: outward extension only, no overlap with other cuts in the
same source, at most 2 seconds beyond B2 and at most 8 seconds total
extension over A. A canonical single-cut timeline is reconstructed and
fingerprinted; at most two bounded alternatives are reported. The existing
A/B/B2 canonical media/evidence gates still apply. Nothing is downloaded,
remixed, rendered, transcribed or published.

If the independent edited-audio ASR receipt is not already present, the
field reports `LOCAL_B3_EDITED_AUDIO_ASR_UNMEASURED` with
zero proposals rather than inventing confidence. If no source-utterance cut
and nearby warning support an outward extension, it similarly abstains.
Proposals remain explicitly **unrendered**, with no quality, audible-speech
completeness, humor, or safety certification and `ready=false`.
Any real B3 render, listening comparison, and adoption will require a
separate guarded workflow and verified real-video evaluation.

### Read-only join of existing edited-audio ASR evidence (PR #258)

`uv run holoclip render-three-way-existing-media-review-batch` now
also returns `existing_edited_audio_asr` for each complete
job. The existing #251/#252 opt-in, three-way edited-audio second-pass ASR
receipt is **read only**. If absent, the new field reports **not measured**
with unknown warning counts, rather than treating missing evidence as good
speech or automatically contacting an ASR service.

If the receipt exists, its source-ASR checksum, A/B/B2 finalized video
checksums, review-plan/diagnostic lineage, original source evidence IDs,
crop windows, per-utterance ASR timing references, status flags and counters
are rechecked. Only bounded text-free warning hints are joined with #257's
source-clock intervals. A malformed or stale saved receipt blocks the job
without repairing/overwriting the file. Re-running the batch command
does not decode video, run FFmpeg, request a provider or transmit clips.
Existing saved review-plan JSON is not rewritten.

These are **possible transcript or timing discrepancies**, not verified
missing/duplicated words, natural dialogue quality or a ranking of A/B/B2.
Even a structurally verified old JSON sidecar is not a signed/authenticated
attestation of actual listening. Human hearing and real unseen-stream
creative qualification remain outstanding; `ready=false`.

### Same-source ASR-clock preservation comparison (PR #257)

The same `render-three-way-existing-media-review-batch` command
now includes `source_asr_coverage` per fully verified job,
without needing another CLI invocation. It inspects the **same original
source-clock utterance intervals** around the altered edit boundary for A,
B and B2. It counts intervals wholly included by one edit segment, partially
included, excluded, and ambiguous when multiple cuts overlap. The paired
deltas expose newly wholly included intervals and possible B2 regressions,
using SHA-tied pseudonymous evidence identifiers, bounded source timecodes
and no transcript text.

This is an **offline timing signal**, not independent listening verification,
a missing-words verdict, natural pacing, payoff success, or a quality
ranking. Split/repeated intervals stay ambiguous; no variant is adopted or
published. The local ASR/source/MP4 lineage is checked before and after
collection; missing or changed evidence stops the case. Existing immutable
review receipts retain their schema; this read-only signal is computed from
the present canonical assets each time, and rechecked when saving.

### Batch post-QA editorial review target preparation (PR #256)

After PR #255's six local stages reach "EVIDENCE_COMPLETE", run:

`uv run holoclip render-three-way-existing-media-review-batch`

This default is **read-only**: it enumerates one already-completed queue job
(or up to four with `--max-jobs 4`) without downloading sources,
calling models, decoding media, or creating a receipt. It requires canonical
SHA-verified A/B/B2 finished video plus both measured technical and acoustic
reports. Incomplete/stale jobs are listed as blockers, not silently repaired.

`--save` creates **only missing** per-job unrated review-target
JSON files, using the existing source-aligned triage implementation. Each job
is rechecked under the same owner-only exclusive lock used by the autopilot.
Repeated runs independently reconstruct the canonical review plan and verify
the **entire** existing saved receipt, rejecting tampered or stale reports
without overwriting them. Returned data contains bounded A/B/B2 time windows,
technical warning types and counts, but never raw transcript content,
machine-local paths or media bytes.

This reduces repeated CLI invocations and makes practical human/editorial
review windows available after local qualification scans. It **does not**
prove setup/payoff comprehension, dialogue correctness or comedy quality.
The returned targets are questions, not ratings; no automatic winner, upload
or publishability claim is made. CI validates synthetic local fixtures only;
actual owner-PC footage remains unqualified until run and evaluated there.

## Trusted human-edit structural references versus A/B technical evidence (PR #223)

\`uv run holoclip render-ab-human-context\` is **preview-only by default**.
It automatically reads the existing \`data/examples/human_clips.jsonl\`,
\`data/examples/corpus_growth.json\`, and the local
\`data/render-previews/renderqueue_*.ab-metrics.json\` evidence. No new
datasets, cloud services, FFmpeg runs or YouTube downloads are required.
Pass \`--execute --output <new-path>.json\` only to save a **create-only**
offline report, and use \`--after-job-id <next_cursor> --max-jobs 40\`
to read additional signal-result pages without loading every MP4.

The human-clip reference uses only alignment records passing the existing
\`evaluate_human_example_learning_quality\` policy. It rejects duplicate
clip identities, excludes untrusted/no-source-stream examples, checks the
persistent corpus-growth holdout reservations, and reports any training
versus reserved-stream conflict as a **blocking provenance error**.
Multiple clips from one source stream receive just one vote in the
stream-level clip-duration median.

The statistics are explicitly **structural**: measured aligned clip-span
durations, positive source gaps between aligned segments (observed source
material omitted in the human example), and source-stream-level clip
duration. These are **not** frame-accurate edit points and cannot describe
humor, timing, overlay quality, cut smoothness, or human preference.
The corpus and growth-state input bytes are SHA-256 recorded without
exposing original URLs, transcript excerpts or private local paths.

The other half of the report scans up to 40 already measured and
SHA-pinned A/B technical result receipts on each page. It independently
checks the upstream triage and individual A/B receipt byte hashes before
aggregating decoded SSIM and audio mean delta. Untrusted/stale pairs are
counted as stale, not negative human feedback. The small MP4 snippets are
**not decoded or rehashed on this subsequent metadata pass**; PR #222 is
the stage that does the actual media remeasurement. Report keys explicitly
state this distinction, including that source overlap between a generated
clip and the learning examples has **not** been verified by this report.

The human alignment stats and A/B technical metrics use different
quantities and **must never be subtracted, ranked against one another or
treated as an editorial preference classifier**. The report keeps
\`metric_domains_directly_comparable=false\`,
\`human_editorial_quality_labels_available=false\`,
\`production_qualification_allowed=false\`,
\`auto_adoption_allowed=false\`, \`ready=false\` and
\`production_ready=false\`. Only an independent, source-grounded and
human-rated holdout validation could permit a future quality gate.


## Independent technical signal audit of local A/B experiments (PR #222)

After the optional audio, subtitle, and cut A/B snippets finish, an additional
**create-only** read of up to three successful pairs can produce
\`renderqueue_<id>.ab-metrics.json\`. The worker SHA-verifies each existing
A/B receipt and both **entire short MP4 samples**, probes A/V playback and
duration, then independently decodes the actual output to collect:

- **All three A/B kinds:** FFmpeg decoded-frame SSIM (0–1 visual similarity),
  before/after mean audio dBFS and peak dBFS, measured signal deltas and the
  original individual sample hashes
- **Audio-peak A/B:** checks that the previously claimed -1.5 dB trial is
  reproduced by independent audio analysis, without a substantial peak rise
- **Subtitle/cut A/B:** checks that video signal differences can be measured;
  no inference that font changes improve reading speed or that a darker seam
  improves narrative flow

Every scan is fixed and **local-only**; video SSIM subprocesses have 25-second
timeouts, sample duration is capped at 3–4 seconds per pair, and media
content/metadata is never sent to a service. Missing, tampered, incomplete,
unexpectedly silent or otherwise unmeasurable pairs are **unavailable**,
never a false quality pass. Absent A/B snippets are **not applicable**.
No novel A/B movies or full-length modified output are created here.

The audit explicitly states that the original potentially large MP4 is
**not independently rehashed again** in this bounded step; earlier render,
review and A/B stages establish its SHA-256 provenance. By contrast, each
small A/B MP4's full byte hash is independently reverified before and after
measurement. The output JSON is create-only, tied to the exact prior A/B
receipts and triage SHA-256. The SQLite
\`render_ab_metrics_evidence\` table and read-only \`ab_metrics_counts\`
expose optional status without changing technical video QA.

**SSIM, audio peaks and mean gain are technical signal metrics, not
independent human labels or creative-quality metrics.** A near-perfect SSIM
does not mean a good edit, and a large image difference does not mean a
bad edit. No human-made clipping data has been rated against these variants
by this stage, and there are no earned precision, recall, preference rates
or editorial improvement claims. All output keeps
\`human_quality_qualified=false\`, \`auto_adoption_allowed=false\`,
\`ready=false\`, \`production_ready=false\` and publication disabled.


## Cut-boundary visual A/B experiments (PR #221)

The completed-source offline renderer can optionally create a **maximum
three-second visual A/B sample** centered on one already-rendered, verified
two-cut boundary. This does **not** move cut timestamps or create a new
full-length sequence. It is a temporary style comparison, not a repair.

The deterministic selector first considers a boundary mentioned by a
previously SHA-pinned review-ledger \`cut_boundary\` warning; otherwise
it chooses the earliest eligible hard cut. A seam must have adequate
sample duration on **both** sides. An existing opt-in fade/dip style
is excluded instead of adding another dark effect over it. The full
ClipTimeline and review/triage identity are validated, and nothing
runs if the bounded evidence is absent or tampered.

The worker creates a new-only
\`renderqueue_<id>.cut-ab/\` folder containing:

- \`before.mp4\`: the original A/V join as already rendered, shown in a
  fixed short excerpt
- \`after.mp4\`: exactly the same excerpt with a mild fixed 160ms, 40%
  darkening overlay across the seam **on video only**, never a clip recut
- \`receipt.json\`: SHA-256 of both samples, baseline MP4 and triage
  evidence; original render-plan ID, adjacent cut IDs, output seam time,
  fixed trial parameters and original-MP4 rollback evidence

Both samples are independently decoded/probed after local-only FFmpeg
with hardcoded safe filters and a 35-second subprocess timeout each.
There are no shell commands, remote media, source downloads, model
inferences, user-supplied filter expressions or full-length output
substitutions. Audio is re-encoded equivalently for sample playback
but no gain, delay, trims *within the sample* or audio transition
effect is separately applied to the experimental video.

Source media, complete base/leveled/captioned MP4s, SRT and review
ledgers remain intact. The original MP4 SHA-256 is verified **before
and after** trial rendering; the baseline file is the recovery reference
rather than a destructive restore or copy. An output directory is reserved
create-only with \`mkdir\`, and the receipt is placed last; an interrupted
or preexisting directory is never overwritten and cannot qualify as
complete without the receipt. SQLite stores the outcome separately
in \`render_cut_ab_evidence\`, with read-only \`cut_ab_counts\`.

**Caution:** A cut-dimming experiment can obscure a joke, reaction or
important visual detail. A successful sample means only that two
technically valid, different versions can be compared; it cannot
establish that the edited version is better. The test does NOT apply the
alternative to the master, shift spoken words, delete any frames from
the original or allow publishing. The untrusted/absent stage cannot
revoke successful base A/V QA. \`ready=false\`,
\`production_ready=false\` and independent actual creative-quality
qualification remain required.

## Non-destructive fast-subtitle style A/B samples (PR #220)

For the first **verified fast_subtitle cue** previously identified in the
immutable review and triage ledgers, the completed Windows worker can make
one additional short local Japanese-caption style comparison. The experiment
is for visual inspection only, not a claim to fix too-short cue duration.

The new, create-only folder
\`data/render-previews/renderqueue_<id>.subtitle-ab/\` contains:

- \`before.mp4\`: the corresponding small excerpt of the original/leveled,
  *not-yet-captioned* MP4 with the existing standard 43pt ASS style
- \`after.mp4\`: the **same** excerpt and SRT text/clock with an experimental
  48pt, slightly stronger outlined ASS style
- \`receipt.json\`: SHA-256 of both samples, original MP4, uncaptioned
  input MP4, immutable prior triage and SRT; one cue ID and relative source
  clock; and original-MP4 SHA verification as the immutable fallback

The real SRT is parsed by the existing bounded safe Japanese caption parser,
and the exact stored cue number/clock is checked against the immutable review
ledger. No cue's actual text, time range, source URL or absolute local path
is included in the output receipt. The sample is at most 4 seconds and uses
fixed file-only FFmpeg, without network access or shell expressions.

Oversized text that violates the existing two-line wrapping rule is
**not silently shrunk into an illegible sample**. This first experiment is
limited to fast subtitle cues; overwide/overflowing captions, video-cut
warnings and black/frozen-frame events retain inspection-only guidance.

The output is never promoted or used in the full-length video. Both A/B
variants must be valid local MP4s with video/audio and distinct SHA-256
proofs. Their visible quality is **not objectively certified**; changing
font size/outline cannot repair an unreadably fast subtitle or an ASR
mistake. If encoding, font discovery, source hash checks or receipt binding
fails, optional status is unavailable and successful original video QA is
preserved. Interrupted output without final \`receipt.json\` is unqualified
and never automatically reused.

Read-only render-queue inspection exposes \`subtitle_ab_counts\` from a
separate SQLite \`render_subtitle_ab_evidence\` table; the original,
leveled/captioned MP4s and SRT are never overwritten/deleted. The immutable
original base SHA remains the fallback, not a destructive undo command.
\`ready=false\`, no autonomous repair, no publication and no invented
editorial quality metrics remain mandatory.

## Non-destructive audio A/B listening preview and verified fallback (PR #219)

For a previously measured **audio_peak** event in the first ten seconds,
the normal completed-source renderer can optionally make ONE short, capped
**A/B listening comparison**. The pair is placed under
\`data/render-previews/renderqueue_<id>.ab-preview/\`:

- \`before.mp4\`: up to four seconds from the selected final local preview
  (the original, leveled or captioned version), with no trial audio gain
- \`after.mp4\`: the **same time range** and video encoder profile with an
  explicitly fixed **-1.5 dB experimental audio attenuation**
- \`receipt.json\`: SHA-256 for the review triage and full-length baseline,
  both short A/B files, before/after measured audio dBFS, the bounded
  filter parameters and the verified unmodified original-MP4 fallback

All FFmpeg inputs are local SHA-verified source files; no shell execution,
downloads, model requests, or output path supplied by untrusted text.
The trial uses file-only FFmpeg, fixed downscaled 15fps diagnostic encodes,
no video retiming/cutting beyond the **short comparison window**.
It measures both results independently and requires a bounded reduction
in **measured sample mean dBFS** without an unsafe peak increase.
AAC encoder priming can shift instantaneous peaks despite a lower mean,
so a strict 1.5 dB peak reduction is not asserted. If measured attenuation
cannot be verified, no valid comparison is published. The
large baseline is hashed before and after test generation to ensure the
original is unchanged.

**Fallback/rollback safety:** No full-length MP4, intermediate leveled
MP4, captioned MP4 or subtitle is replaced, renamed or deleted. The
rollback destination is always the **unchanged original render path** with
its verified SHA-256 (not a mutable copy of a repaired output). A/B
results and their \`receipt.json\` are stored only in a newly reserved
directory. An existing directory is never overwritten, even with
concurrent workers. Receipt is committed last: without it, a partially
created A/B directory must be treated as **unqualified** and never used
for delivery. There is no automatic promotion of either A/B output.

This is **not automatic audio repair**. Lower volume does not reconstruct
already clipped dialogue and may impair an intentionally loud reaction.
The attenuated file is only a proposed before/after listening reference.
Other anomalies (blackout/freeze/subtitle/low-average-audio) still follow
the existing non-executable inspection checklist without alternate clips.
No perceptual listening quality score or human-approved repair judgment is
asserted.

The optional worker result and separate SQLite
\`render_audio_ab_preview_evidence\` rows are exposed through read-only
\`ab_preview_counts\` and individual hashes/rollback-proof flags. A
missing/failed/skipped A/B stage cannot demote successful technical MP4
QA. \`ready=false\`, \`production_ready=false\`, no automatic correction,
and no publication remain unchanged. This step also respects the
existing Windows worker runtime reserve.

## Non-executing review priorities and inspection proposals (PR #218)

The completed local renderer now builds a **create-only**
\`renderqueue_<id>.triage.json\` after its existing review ledger and
optional source-event comparison. It reads only those *small, SHA-256-verified
JSON receipts*: **no extra FFmpeg, no video downloads, no user-provided labels,
no LLM, no transcript parsing and no media re-encode**.

Each existing black/freeze/subtitle/audio warning is preserved and receives
a stable, deterministic *inspection queue* entry with a small fixed
\`inspection_action\` and \`followup_action\`. The three buckets are:

- \`REVIEW_FIRST\`: the original-source sample did **not** show the same
  coarse black/freeze signal. Inspect first, but this is **not evidence
  that the render introduced an error**.
- \`REVIEW_NEEDED\`: original comparison was missing, skipped, unavailable
  or the event is subtitle/audio related. No missing signal is ever
  interpreted as a safe or defective edit.
- \`REVIEW_WITH_SOURCE_CONTEXT\`: similar black/freeze was also observed
  in the original. Review whether the held/dark moment was artistically
  intended; do not discard or auto-approve it.

The \`repair_draft\` object is **only a non-executable inspection checklist**:
it requires independent A/V judgment, future before/after previews and
policy/continuity review *before* any proposed automatic edit. It contains
no FFmpeg arguments, patches, executable instructions, timing changes,
copy/move/delete operation or setting that can authorize execution.
\`repair_execution_performed=false\` and
\`repair_preview_generated=false\` mean that this PR does not generate
an alternate or repaired video. Confidence scores, precision, recall and
subjective edit-success claims are intentionally absent.

The input SHA-256 values, source comparison status and original media
hashes from the earlier evidence are pinned. The system does **not**
independently rehash multi-gigabyte MP4s during this lightweight step and
states \`media_sha256_independently_reverified=false\`. A changed or
contradictory review/source comparison prevents triage creation, never
producing fabricated confidence. Existing triage files are not overwritten.

A separate SQLite table, \`render_review_triage_evidence\`, records triage
status, SHA and candidate counts; read-only queue inspection exposes
\`triage_counts\`. A skipped or invalid triage leaves prior MP4 technical
QA unchanged. \`ready=false\`, \`production_ready=false\`,
\`auto_repair_allowed=false\` and the need for independent human-rated
real-footage calibration all remain in force.


## Bounded original-stream anomaly context (PR #217)

After the local MP4 render is complete, the existing worker can **optionally**
inspect the *cached original source footage* corresponding to up to three
previously detected black/frozen-frame events. It maps output seconds back
through the ClipTimeline cut source clocks and runs a fixed, 2fps 160x90
FFmpeg blackdetect/freezedetect pass on only the selected original-media
windows (up to six seconds each, max 15 seconds per subprocess). It does not
download source material, call a service, upload, make new edits, use OCR/AI,
or read the source transcripts. The original stream and finished MP4 are
never modified.

Events that cross a cut boundary or are too short for a meaningful
single-source comparison are marked **skipped** instead of being attributed
to the wrong original. Missing/corrupt original files, ffmpeg errors and
source changes while scanning are **unavailable**, not a false negative.
The source snapshot SHA-256 was already verified twice during rendering.
This optional event-window scan checks the local file's size and stat identity
before/after reading, but **does not independently rehash the entire source**
again; the sidecar states this limitation plainly.

The separate immutable output
\`data/render-previews/renderqueue_<id>.source-context.json\` is bound to
the generated review ledger SHA and RenderPlan ID. It contains limited
timings and pseudonymous asset/segment identifiers, comparison status and
original source SHA-256 from the render snapshot, without local paths or
transcript text. Its outcomes are:

- \`SIMILAR_SIGNAL_OBSERVED_IN_SOURCE_WINDOW\`: the corresponding source
  sample also exhibited the type of signal seen in the render
- \`NO_SIMILAR_SIGNAL_IN_SAMPLED_SOURCE_WINDOW\`: it was not observed at
  the coarse chosen thresholds in that small source sample
- \`SKIPPED_BOUNDARY_OR_SHORT_OBSERVATION\` or
  \`SOURCE_WINDOW_UNAVAILABLE\`: the source comparison cannot establish
  a sound observation

Even a matching source signal is **not proof that the edit is good**. An
absent source signal is **not proof the renderer introduced a defect**:
the coarse sampler, zoom, temporal resampling, keyframes and boundaries can
change detector behavior. No flag is removed or automatically fixed.

\`render_source_context_evidence\` tracks status, SHA and aggregate
comparison counts independently in SQLite; read-only queue status exposes
\`source_context_counts\`. When the worker approaches its Windows runtime
budget, this step is skipped. Lack of a source comparison cannot revoke
technical A/V QA or mark the video production-ready. Existing
\`ready=false\`, \`auto_repair_allowed=false\` and human-editorial
qualification requirements remain enforced.

## Rolling review backlog and paged sampling (PR #216)

The existing unattended local render worker now appends a tiny, create-only
**per-job** evidence snapshot to
\`data/render-previews/edit-review-queue/renderqueue_<id>.json\` when the
review sidecar passed the previous stage. This requires **no additional
per-video operator setup**: the same rendering worker that produces the
MP4 and review ledger creates the backlog receipt. It rechecks only the
small review-ledger SHA, does not decode media or rehash gigabytes of video,
and records the original and selected final MP4 SHA-256 plus anomaly counts.
Its \`media_hash_reverified_for_calibration=false\` flag is deliberate:
a staged snapshot is an **unlabelled observation**, not verified media gold.
The full calibration pack still performs independent hash checks before
using a sample.

The worker persists the new archive outcome independently in SQLite
(\`render_review_archive_evidence\`), exposed by read-only
\`archive_counts\`. A missing, tampered or pre-existing receipt is
\`LOCAL_EDIT_ARCHIVE_UNAVAILABLE\` and does not revoke successful MP4 QA
or allow publication. The receipts are immutable, not overwritten, and
do not contain source paths, transcript text or unbounded video data.

The pack command no longer stalls forever after 40 review sidecars.
Instead it reads **at most 40 SHA-verified jobs per invocation** and
returns \`next_cursor\`, \`has_more\`, \`window_job_count\` and
\`inventory_complete\` separately. Continue using a different output
filename and \`--after-job-id\`:

\`\`\`powershell
uv run holoclip render-review-calibration-pack --execute --output data/render-previews/calibration-first.json
uv run holoclip render-review-calibration-pack --after-job-id renderqueue_<cursor-from-first-pack> --execute --output data/render-previews/calibration-second.json
\`\`\`

A single pack's \`anomaly_counts\` are **page-local** and must never be
represented as comprehensive when \`has_more=true\`. A non-null cursor
represents a resumed page, so \`inventory_complete=false\` even for the
last resumed page; \`has_more=false\` indicates the cursor reached the end
of the current inventory. A global safety cap of 10,000 ledger filenames
prevents unbounded discovery. There is no relabeling of real human-cut
examples into human edit-quality gold and still no auto-repair or
production qualification (\`ready=false\`).

## Offline unlabelled edit-review calibration pack (PR #215)

The new preview-first CLI takes **already collected** local edit review ledgers
from \`data/render-previews\` and the existing human clipping examples from
\`data/examples/human_clips.jsonl\`. It does **not** call yt-dlp, GitHub media,
the LLM, the internet, Docker or any paid API. The worker's normal rendering
already generates \`renderqueue_<id>.review.json\` where its diagnostics
succeed, so no new per-video data preparation is needed.

\`\`\`powershell
uv run holoclip render-review-calibration-pack
uv run holoclip render-review-calibration-pack --execute
\`\`\`

The first command is **read-only**. \`--execute\` creates
\`data/render-previews/edit-review-calibration.json\` only if that path does
not already exist. To keep multiple snapshots, specify a distinct
\`--output\` path; existing files are never overwritten.

The report requires each ledger's previously recorded diagnostic version,
status, false production-ready flags, and original/final SHA-256. It rehashes
the local original and whichever allowed final MP4 is present and rejects
stale/mismatched evidence. Up to 40 ledger records are processed per pass;
more than that reports a bounded \`TOO_MANY_REVIEWS\` state rather than
silently sampling an incomplete backlog. Candidate samples are balanced
round-robin by signal type, max 30 total / 6 per signal. Successful
zero-warning scans form *unlabelled controls*, never "human approved"
negative examples. The report contains job IDs, hashes, coarse output
timestamps, event kinds and segment counts only: no transcript excerpts
or source file paths.

The existing human-cut store is used strictly to report how many distinct
clips have **trusted temporal alignment** under the existing learning gate
and how many source streams are represented. Trusted alignment proves
neither that each frame or subtitle was good nor that a particular
blackout was incorrect. That store currently provides no independent
per-anomaly human labels for this pipeline, so \`precision\`,
\`recall\`, \`false_positive_rate\` and
\`calibration_metrics_available\` stay null/false.
No scoring threshold, auto-repair, forced rejection or publication change
is derived from these examples. \`ready=false\` remains mandatory.

This prepares deterministic evaluation cases without asking the operator
to supply files. Real calibration still requires **independent, withheld,
human-rated judgments** of the specific technical event types; none are
fabricated from the existence of professionally cut videos.

## Localized non-destructive edit review ledger (PR #214)

The same bounded, offline PR #213 diagnostic scan now retains **where** an
abnormality was seen, not only how often. Black intervals include the measured
start/end seconds. Frozen frames include the onset (their end can be unknown).
Fast and overly wide subtitles include their own SRT cue number and start/end
timestamps, but **never subtitle text**. Audio sampled-peak/low-mean warnings
refer only to the first at-most-10 seconds sampled: do not assume an entire
stream or later reaction was measured.

Each detected event is mapped to all ordered ClipTimeline segments it overlaps.
A blackout crossing a cut joins the two adjacent segment IDs so the next
reviewer does not inspect only one source video. A freeze without an FFmpeg
end marker is treated as an onset only; it does not accuse every later cut.
Every event receives a fixed, bounded inspection action and
\`auto_repair_allowed=false\`. The ledger makes **no inference** that dark or
static illustration segments are an error, nor any unvalidated estimate of
quality or probability.

For successful measured scans, the local worker creates an immutable sibling
\`renderqueue_<id>.review.json\` atomically on the same filesystem. It has
strict event count and size limits, no shell/network/LLM/OCR access, no source
paths, no transcript text, and includes the original/final MP4 content hashes
plus the scan status and explicit \`ready=false\`. A second run cannot
overwrite an existing manifest. The worker commits an independent
\`render_review_evidence\` status/hash/candidate count to the existing SQLite
transaction and exposes read-only aggregate \`review_counts\`. If the
optional diagnostic was skipped or a manifest could not be safely written,
its review status is UNAVAILABLE; the original video technical QA is intact.

This is **instrumentation for future calibration**, not learned calibration
itself. Withheld, real human-rated examples are still required to measure
which alarms indicate genuine faults and which represent stylistic intent.
Automatic edits, deletion, retiming and publication remain blocked pending
such qualification. Never claim a perfect clip just because it has zero
detected candidates.

## Read-only edit-quality diagnostics (PR #213)

After the original MP4 technical QA, optional audio leveling and caption
burn-in, the existing local scheduled worker runs a **bounded read-only**
FFmpeg diagnostic pass. It never modifies source media, subtitles or finished
MP4s, does not use YouTube, LLMs or OCR, and never uploads or publishes.

The uncaptioned local MP4 is scanned at 2fps and 160x90 with FFmpeg
\`blackdetect\` (>=1 second, about 98% very dark) and \`freezedetect\`
(>=2 seconds), capped at 100 events each. The last selected audio track
(base, leveled or captioned MP4) is inspected using \`volumedetect\` on up to
the first 90 seconds, recording its mean/peak sample dBFS. The SHA-256
verified *generated* sibling SRT is checked for unusually fast lines
(over 20 Unicode characters/second), unsafe wrapping over the two-line
baseline, and duration clock consistency. No captions means a **partial**
diagnostic, not a fake subtitle success.

All tool invocations are offline, use fixed validated FFmpeg arguments,
have strict time and stderr limits (including a 60-second ceiling for the
video scan and at most 30 seconds for the audio sample) and verify local
input content SHA-256
both before and after scanning. No operator SRT or unrelated MP4 is scanned
on untrusted provenance. Only bounded counters and numeric diagnostics
are persisted; no sensitive transcript lines or paths are logged.

\`render_quality_evidence\` preserves the diagnostic version, source/final
MP4 identities, frame anomaly counts, caption observation state and flags,
audio mean/peak and combined warning count. Its statuses mean exactly
\`MEASURED_FLAGS\`, \`MEASURED_NO_FLAGS\`, \`MEASURED_PARTIAL\` or
\`UNAVAILABLE\` under the \`LOCAL_EDIT_DIAGNOSTIC_*\` prefix.
Read-only worker status adds aggregate quality counts. The scheduled worker skips the optional scan entirely once earlier work
has consumed 50 minutes, reserving time under the 55-minute Windows task
deadline. An unavailable diagnostic never revokes existing technical MP4 QA.

**Important:** Dark scenes, still illustrations, intentional holds and
strong reactions can be perfectly good editing choices. Thresholds are
screening heuristics, not human-labeled calibration. Absence of flags is
not evidence of engaging content, correct Japanese ASR, legible text on
complex backgrounds, reliable perceptual LUFS or source rights.
The tool does not silently "repair" a suspected artistic element.
Automatic repair is deferred until data supports a safe, reversible
rule and real held-out footage can distinguish bugs from style.

\`ready=false\` / \`production_ready=false\` and manual publishing
approval remain unchanged. CI runs offline synthetic/mocked checks, never
tests live YouTube media.

## Conservative visual-style baseline (PR #212)

The initial local FFmpeg render now compiles a **finite, deterministic** visual
style directly into each ordered ClipTimeline segment. It adds no second
video encode, user operation, Docker, network or new paid service, and does not
change source trim positions, output order, duration, A/V cut clocks or SRT
timing. Any existing completed MP4 is never overwritten or re-rendered.

The safe default is **the original 1280x720 aspect-preserving letterbox and
hard cut**. A subtle progressive center zoom (capped at 1.035x) applies only
to clips marked REACTION or PAYOFF with segment confidence >= 0.92 and at least
two seconds of footage. Because the pipeline does not yet verify face position
or game UI safety, a center zoom is *not* proof of good composition; it may
still trim a small part of the visible frame and requires real-video review.

A brief 80 ms dip-to-black at a source boundary (synchronized on both audio
and video) is allowed **only** when the incoming segment explicitly requests
the fixed hint \`dip_to_black\`, both segments have confidence >= 0.95,
are different SourceAssets, and each segment is at least 1.5 seconds.
Unknown hints and free-form LLM filter strings are ignored, not executed.
No default dissolve or fade is invented; the duration and cut clock are
preserved. Speech near those deliberate boundaries may still be affected.

The deterministic \`VISUAL_STYLE_VERSION\` forms part of the RenderPlan ID
and is verified before FFmpeg starts, preventing an old fingerprint from
silently producing different style results. A successful local technical MP4
receives a \`visual_style\` summary (zoomed cut count, hinted transition
count and style version). The auto queue saves this as separate
\`render_visual_evidence\` in the final technical QA transaction. Status
\`LOCAL_RENDER_VISUAL_ENCODED_TECHNICAL_ONLY\` means the renderer returned
the expected style summary *and* its original A/V QA succeeded; it **does not**
certify natural composition, continuity or real-frame quality. A missing or
mismatched summary is recorded as UNVERIFIED, without inventing success.
The read-only preview status displays aggregate visual evidence.

CI exercises bounded filter decisions and synthetic real FFmpeg A/V
roundtrips only; real unseen streams, actual reaction crops and scene-aware
transitions remain unqualified. Publication remains blocked and
\`ready=false\` is unchanged.

## Offline cut-aware audio leveling (PR #211)

After the existing deterministic original MP4 technical QA, the local worker
attempts an **optional** new-only audio finishing stage. This is automatic;
it does not require additional manual files, paid APIs, Docker, yt-dlp or cloud
providers. The original MP4/SRT and original source cache are never modified.

For each ordered \`ClipTimeline\` cut (up to 32, total no more than 180 seconds)
the offline FFmpeg \`volumedetect\` filter measures mean and sample-peak dBFS.
The correction targets approximately -22 dBFS **mean per cut** (not perceived
LUFS), capped at -6 to +6 dB, with positive gain further bounded so measured
peaks would not exceed -1.5 dBFS. Quiet/silent cuts are **not amplified**.
The worker splits the already-rendered local output audio by the recorded
cut-clock, applies these deterministic numeric gains, concatenates audio,
then applies a final sample limiter without automatic gain-up.
Video is stream-copied, audio is re-encoded at 48 kHz / 192 kbps AAC.

Output is \`renderqueue_<id>.leveled.mp4\` only after a fresh ffprobe A/V and
duration check, per-cut second-pass volume/peak measurements, a bounded
gain-agreement check (accounting for AAC and frame boundaries), and source
hash re-validation. The final no-overwrite commit uses a create-only hardlink;
an existing output, malformed timeline, FFmpeg error, unsupported signal or
unexpected gain/peak produces optional failure without invalidating the base
MP4 technical QA. The stage is bounded to a 420-second encode timeout plus
short bounded per-cut measurements. It never automatically retries failed
work or replaces an existing file.

If the audio output passes, the existing subtitle burn-in uses the measured
\`*.leveled.mp4\` audio and original corresponding \`*.srt\`, creating the
same \`renderqueue_<id>.captioned.mp4\` as before. If not, subtitle burn-in
falls back to the unmodified technical MP4 as in PR #210. When SRT is missing,
audio leveling may still produce a standalone audio-leveled MP4.

\`render_audio_evidence\` records status, hash, cut count, maximum applied
absolute gain and the spread of cut mean levels before and after correction,
alongside technical and caption evidence in SQLite. The read-only status
reports aggregate audio successes/failures. These diagnostics are **not**
listening-quality certification: dBFS isn't LUFS, music/game/audio mixing
cannot be separated automatically in this baseline, true-peak intersample
overshoot isn't fully qualified, and output quality on unseen Hololive
recordings remains unknown. Original \`QA_PASSED\` is unchanged; all routes
remain \`ready=false\`, with no publication.

## Optional Japanese subtitle burn-in (after PR #209)

For a newly completed source-acquisition job, the existing offline scheduled
renderer now *also* attempts a second local-only FFmpeg encode when PR #209
generated a trustworthy SRT. This produces a new sibling
`renderqueue_<id>.captioned.mp4`, leaving the original QA-verified MP4 and
`renderqueue_<id>.srt` unchanged. No video is overwritten or uploaded.

A strict parser checks the SRT sequence, ordering, time ranges, text controls
and maximum cue length. Caption text is safely rendered into a fixed 1280x720
ASS style (white bold Japanese text, dark outline, bottom center, at most two
lines). This is a deterministic **baseline**, not learned, speaker-aware or
editorially reviewed styling. It does not guess missing ASR or word timing.
The input MP4 and SRT must match their measured SHA-256 receipts; FFmpeg uses
local-only input with libass and checks the new MP4's video, audio and measured
duration before a create-only hardlink commit.

Japanese font discovery is automatic and conservative: Windows Meiryo / Yu
Gothic, or Linux Noto Sans CJK JP / TakaoGothic. FFmpeg with the `ass`
filter and a recognized local font are required. If missing, if SRT is
untrusted, or if the second encode fails, the original technical MP4/SRT are
still valid and the optional burn-in is recorded as unavailable/failed.
No silent font fallback to unreadable Japanese is considered a quality pass.
The second encode has a 10-minute ceiling so that a full-length automatic
180-second preview plus burn-in stays inside the existing bounded Windows
scheduled-task budget. Slow hosts may need a later separate manual retry;
there is no unbounded or silent retry.

`render_burnin_evidence` saves the burn-in status, output and SRT hashes
transactionally with the original queue's technical QA row. The read-only
`render-ready-preview` command exposes burn-in counts. The successful status
is `LOCAL_SUBTITLE_BURNED_PREVIEW_UNVERIFIED_EDITORIAL`: it certifies
neither Japanese transcription correctness, visual legibility on every scene,
creative timing, policy rights, nor publication readiness. The original
`QA_PASSED` means only independent A/V technical QA. Both remain
`ready=false`. The existing Windows worker needs no extra per-clip setup.

## Deterministic cut-aligned subtitle sidecar (PR #209)

The completed-source offline renderer now also attempts to create an
**optional UTF-8 SRT subtitle sidecar** next to the verified technical
preview MP4. This is a transcript timing aid, not burned-in graphics.
For example, data/render-previews/renderqueue_....mp4 gains a sibling
renderqueue_....srt when usable source ASR already exists.

No input preparation is needed when Holoclip's existing discover or
human-example ingestion has saved timed source ASR under one of:

    data/transcripts/<youtube-video-id>.json
    data/transcripts/examples/sources/<youtube-video-id>.json

The default root can be changed using `--transcript-root` on
`uv run holoclip render-ready-preview --execute`. The normal Windows
scheduled pipeline uses the same default source transcript directory.
Missing source transcripts cause no network ASR call and no invented
captions. Ambiguous duplicates, mismatched transcript metadata,
linked paths, oversized or invalid records fail closed without
modifying the MP4 or an existing operator-provided SRT.

The compiler maps each canonical source transcript interval to each
selected original-stream cut, then shifts it into **output clock**
according to `output_order`, including disjoint cuts and multiple
sources. A complete utterance must fit inside a selected source cut;
utterances crossing a cut boundary are dropped instead of assigning
guessed word-by-word timestamps. Ineligible overly long/malformed
utterances and ambiguous overlaps are also skipped. No ASR text is
rewritten or hallucinated. Existing caption text is HTML-escaped
for safe SRT output.

Rendered sidecars are created using a no-clobber filesystem commit;
a matching SRT already present is preserved unmodified. The
optional output SHA-256, cue count, dropped cue count and reason
code are persisted in render_caption_evidence alongside technical
MP4 QA, and aggregate counts are visible in the read-only
`render-ready-preview` status. Neither ASR text nor transcript paths
are copied into the public worker result.

**Current limitation:** The SRT is a sidecar, not visual captions
burned into the video. It does not identify speakers, style text,
accurately align words, or confirm recognition/caption semantics.
ASR may be inaccurate, and a no-subtitles result does not block
the independent A/V QA_PASSED state. A production-grade subtitle
style and editorial QA/burn-in stage remains separate. All reports
retain ready=false and no publication occurs.

## Automatic technical MP4 after complete-source acquisition (PR #208)

The existing Windows periodic source queue worker now has a local-only
second stage. It attempts at most one missing video download per scheduled
tick. **Only when the source queue is fully complete and no download
was attempted in this tick** does the same worker invoke an offline
one-clip MP4 renderer on a later tick. This protects the scheduled
task's resource/time budget; it neither contacts YouTube nor makes
an upload.

Inspect completed-source preview eligibility without starting FFmpeg:

    uv run holoclip render-ready-preview

To explicitly process one locally ready timeline outside Task Scheduler:

    uv run holoclip render-ready-preview --execute

Existing --queue and --preview-root options allow configurable SQLite
state and preview output directories. The default output root is
data/render-previews; outputs are named after the stable acquisition
job ID, creating a local MP4 only if the filename does not exist.

Every run rechecks the source queue COMPLETE status, the enrolled
measured ClipTimeline, original archive identities and full-duration
ffprobe AV measurements, and the cached yt-dlp receipt plus local
RENDER source/range policy rules. A source without trustworthy
title/description metadata, a no-clipping marker, or a policy
BLOCK/REVIEW must not render. Source hashes and decode time ranges
are again validated by the existing local RenderPlan.

Render progress is stored durably in a separate render_jobs table
within the original SQLite source queue. State flow:

    pending complete source -> RENDERING -> QA_PASSED
                                      -> PAUSED on validation/QA error

Claims are serialised with SQLite BEGIN IMMEDIATE. A crashed
RENDERING job or a PAUSED job blocks additional auto renders.
Neither will be retried or overwritten automatically. An interrupted
render can leave a new untracked output: investigate manually;
do not treat file existence as proof of success.

A successful QA_PASSED row records plan ID, MP4 path, output SHA-256
and measured duration. It means only ffprobe A/V tracks, output
duration and basic source/plan checks passed, **not** verified
subtitles, transitions, sync accuracy, creative quality, policy
clearance or publishing. The current scheduled integration permits
only clips up to **180 seconds** automatically, to keep rendering
separate from a long download within a bounded 55-minute scheduler
task. Larger clips must go through explicitly controlled offline
`render-local-preview --execute` with adequate resource budgeting.

No changes promote production readiness. `ready=false` and
`human_editorial_qa_completed=false` remain mandatory, and
publishing always requires separate policy/quality approval.

## Automatic source-queue enrollment after candidate analysis (PR #207)

After creating the PR #206 durable queue, the next manual step—the
per-candidate ClipTimeline file and enqueue command—can be removed with
**one-time** operator opt-in. Preview the settings with no file writes:

    uv run holoclip render-candidate-autoqueue-setup

Then explicitly enable *local technical preview acquisition only*:

    uv run holoclip render-candidate-autoqueue-setup --apply --approve-local-preview

Settings are saved to data/runtime/render-candidate-autoqueue.json; the
file is never overwritten by setup. --config, --queue, --database,
--cache-root and --policy-file are configurable. Configure before
analysis; there is no need to repeat flags for each stream.

Subsequent normal `holoclip analyze` and `holoclip discover` calls
automatically offer their in-process evaluated candidate runs to the
existing SQLite source queue. No separate timeline JSON or sources.json
is written. If autoqueue is disabled, existing analysis output stays
unchanged; if it is configured but blocked, candidate results remain
available and a fixed nonsecret warning code is printed.

Enrolled candidates must have an exact canonical YouTube source asset ID
(their --stream-id, or derived ID, must match its 11-character YouTube ID),
an existing available public/authorized source of type stream/video,
confidence >= 0.85 by default, no candidate warnings, consistent
candidate IDs and measured cut boundaries, and <= 600 seconds of
selected video. Default max enrollment is one candidate per analysis run,
configurable up to three. This is only a conservative prefilter;
confidence or a low warning count is **not** ground-truth editorial
quality proof. Unknown/mismatched stream IDs, private/unavailable
source assets and malformed model plans are skipped, not guessed.

Only local SQLite queue writes happen during analyze/discover: yt-dlp,
media downloads, FFmpeg, external APIs beyond the existing analysis,
or actual Windows Task Scheduler operations are not performed by
this bridge. The previously approved queue worker still independently
checks metadata, restrictions, LAN-wide access pacing and cooldown
before requesting at most one archive per scheduled iteration.

A normal `discover` run already saves its evaluation run to disk;
`analyze` enrolls from the current candidate run without requiring
`--save-run`. No automatic publication occurs. This stage does not yet
perform post-download FFmpeg rendering or completed-clip editorial QA;
`ready=false` remains mandatory.

## Durable unattended missing-source queue (Windows, preview-first)

The approved missing-video acquisition can now be enrolled in a local
SQLite queue and advanced by a bounded Windows Task Scheduler worker.
One saved job snapshot records the measured ClipTimeline, canonical
database/cache/policy paths and acquisition budgets, with no passwords,
tokens, OAuth cookies or yt-dlp stderr. Store this local runtime DB
privately; it contains source asset IDs and machine-local paths.

Step 1, preview offline (no queue file created or network request):

    uv run holoclip render-source-queue-enqueue --timeline data/timeline.json

To enroll once, explicitly authorize local preview acquisition:

    uv run holoclip render-source-queue-enqueue --timeline data/timeline.json --apply --approve-local-preview

Step 2, preview the current-worker state without network:

    uv run holoclip render-source-queue-status
    uv run holoclip render-source-queue-run

Step 3, optionally execute a *single* bounded queue worker iteration:

    uv run holoclip render-source-queue-run --execute

One attempt per invocation means no rapid bulk downloads. The worker
uses the existing authenticated PC-A LAN coordinator, local SQLite
yt-dlp pacing, conservative native yt-dlp sleeps, source policy and
full-video identity checks. First source in a timeline is acquired
and the durable job stays QUEUED if another source remains missing.
An existing valid complete cache is recognized without new network.

Step 4, to schedule recurring single-item checks on Windows, inspect
the preview then explicitly install the current-user task:

    uv run holoclip render-source-queue-task-prepare
    uv run holoclip render-source-queue-task-prepare --apply

These steps register, but do not immediately start, a non-elevated
interactive Task Scheduler job that repeats approximately hourly
(configurable --interval-minutes 30..1440). It requires the existing
uv environment and an already enrolled queue. It never overwrites
an existing task or resets yt-dlp cooldown. Windows user sign-in
is required for interactive tasks; this alone does not guarantee
24-hour service uptime or automatic wake after a PC shutdown.

**Fail-closed recovery:** an interrupted RUNNING attempt stays RUNNING
and blocks later jobs. Do not assume unfinished media is safe.
After 2 hours, inspect the job ID from render-source-queue-status,
and run a read-only reconciliation:

    uv run holoclip render-source-queue-reconcile --job-id JOB_ID

Add --apply only after checking the result. If all caches are
fully source-duration-matched and FFprobe verifies both A/V tracks,
reconciliation can mark COMPLETE. Otherwise it moves the job to
PAUSED. It never downloads while reconciling. Unknown errors,
rate-limit/bot checks, timeouts and policy restrictions also PAUSE
without retry. Only explicit --apply --resume on a *PAUSED* job
rearms it after fixing the cause; the queue does not silently
re-enroll or reset a block. Shared pacing contention may become
DEFERRED with a minimum 30-minute delay, at most three total tries;
after that the job requires manual intervention. A paused or running
oldest job blocks later jobs to avoid shared-IP bursts.

Queue preview and hosted CI never contact YouTube, owner Windows PCs
or Task Scheduler. The scheduler tests replace Windows cmdlets with
mocks. No source policy or copyrights are automatically certified,
and neither acquired media nor a generated preview is publishable
without separate verification. All results remain ready=false.

## Deterministic technical behavior and safety

- Supports continuous, same-source multi-cut and multiple-source ordered
  ClipTimelines, up to 32 segments and 1800 seconds of output.
- All source assets must be available and each locally approved; all required
  source IDs must map exactly once. Approval assertion does not establish
  source copyright, no-clipping policy compliance or permission to publish.
- Local source inputs only, 50 GiB maximum per input, .mp4/.mkv/.mov/.webm.
  No symlink/junction path, missing input or duplicate media path accepted.
  Measured ffprobe A/V presence and duration and input bytes are checked.
- Explicit video and audio are mandatory: missing track blocks processing.
  No fabricated silent soundtrack. FFmpeg uses measured trim/atrim,
  timestamps reset, constant 1280x720 at 30fps, H.264 yuv420p and
  48kHz stereo AAC. No shell execution or network protocol whitelist.
- Input hashes are verified before and after FFmpeg. On output, ffprobe
  confirms positive audio/video and duration within 0.5 seconds of the
  plan. QA or FFmpeg failures leave no committed output.
- Renders to a temporary sibling directory; hardlink create-only commit
  prevents overwriting an existing output, including a concurrent writer.
  The output parent directory must exist and the filename must be new .mp4.
- Outputs include stable plan ID and technical measurements, but suppress
  FFmpeg error logs that could expose private local file details.

Successful result is LOCAL_RENDER_BASIC_AV_QA_PASSED_NOT_PRODUCTION_READY.
This means basic A/V integrity, NOT verified visual cut continuity,
subtitles, audio balance, localization, source permissions, clip quality
or publishing eligibility. The full Editing Profile, more sophisticated
RenderPlan, subtitle/zoom/audio-mix strategies, quality/repair loop and
approval-gated publishing remain outstanding. Real Hololive footage was
not run in hosted CI; only synthetic local media is used for integration.
No production or editor quality readiness is promoted: ready=false.


### Three-way editorial evidence triage after local playback (PR #267)

First generate source/B2/B3 short review clips using the existing
`render-b2-b3-local-review-pack --execute --approve-local-preview` flow,
then inspect them **offline and read-only** using:

```
uv run holoclip render-b2-b3-editorial-evidence
```

Optional `--job-id`, `--proposal-index`, `--refinement-index`,
`--candidate-index` and `--target-index` select the same deterministic
target as the playback pack. To save a new immutable evidence receipt,
explicitly request:

```
uv run holoclip render-b2-b3-editorial-evidence --save
```

The command rederives the current source ASR, existing edited-ASR and
four-way technical lineage, checks every static playback receipt field,
and separately rehashes all **three short local MP4s**. Missing playback
clips are reported as not rendered; changed bytes, unsupported receipt
fields, changed ASR lineage, linked paths, missing or mismatched clips
and non-create-only destinations fail closed. No new video decoding,
FFmpeg encode, provider inference, download, transcript export or publish.
Only the optional `--save` writes a create-only JSON sidecar.

The three evidence dimensions are *speech naturalness* (unverified edited
ASR hints), *narrative continuity* (source-clock projection / whether
the selected clock was replaced with an edit-boundary fallback), and
*payoff completion* (short excerpt durations). Each provides questions
for a later independent full-video/listening review, and explicitly
sets `score=null`. The short clips have no verified semantic-story labels.
The full source video's hash is **reported from the original playback
receipt but not independently rehashed** by this step. Local clip
content hashes *are* independently rechecked.

The resulting evidence is a reliable **inspection handoff**, not an
automated creative-quality evaluation: `automatic_winner=null`,
`automatic_adoption_allowed=false`, `ready=false` and
`production_ready=false` remain unconditional. Real finished footage,
independent human-rated speech/continuity/payoff labels, source-diverse
holdout checks and calibrated LLM/VLM assessments are still needed
before automatic ranking or publishing could be contemplated.
