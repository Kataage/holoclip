# Real-media provider benchmark and qualification (#99)

`holoclip providers` compares the same measured human-made clip windows through
native video, multi-image, contact sheets and individual-image evidence fusion.
It also compares transcript reasoning, ASR segment/word timestamps and embeddings.
A successful HTTP response never grants production qualification. No real-media
production qualification bundle has been established yet.

**Unattended evidence policy:** [autonomous evidence and learning](autonomous-evidence-policy.md).
The operator is not expected to prepare gold cases or clip URLs. The corpus-growth
queue reserves source-stream-disjoint metadata for future held-out acquisition,
with an immutable split and leakage quarantine. These *reservations* are not
measured `held_out=true` cases or independently reviewed gold. Full automatic
source/event holdout finalization and calibrated independent gold are still
acceptance work in #107/#99; no synthetic human-review claims are permitted.

The current protocol is `holoclip-real-provider-v2`. Protocol, grounded prompt and
cloud-response cache versions changed after live transport faults. Historical v1
reports remain immutable diagnostics; they cannot be silently promoted under v2.
Unordered capability/dimension/training/fact sets serialize canonically so process
hash randomization cannot change resume/dataset IDs. Ordered frames/transcript/event
sequences retain their order. Legacy reports whose dataset identity does not match
the canonical manifest are rejected, never silently requalified.

## One command and resume

Install the media extras and FFmpeg. Keep the existing corpus and `.env` read-only.
The credential-bound free account evidence file described in
[the cost policy](zero-cost-runtime.md) is required.

```powershell
uv run --extra media --extra dev holoclip providers corpus-run D:\AI\holoclip `
  --env-file D:\AI\holoclip\.env `
  --accounts data/provider-benchmark/free-accounts.json `
  --output data/provider-benchmark --max-clips 3 --wait-for-quota
```

This probes public metadata/policy, acquires finished **video**, builds measured
bounded windows with existing transcript context, creates a manifest, discovers
models, and runs eligible models/modes twice. Audio-only acquisition cannot qualify
vision. It sends no learned corpus payoff/selection answers in the inference prompt.
Existing corpus clips remain `held_out=false`; the command cannot promote them.
Japanese titles select candidates, without proving semantic correctness.

Rerun the same command after quota reset. Successful work units are checksum cached;
failed/uncertain attempts retain reservations and history. `BLOCKED_QUOTA` preserves
the retry cursor. No account upgrade, paid endpoint or weaker-quality continuation
exists. Missing credentials and unverified contracts produce machine-readable blocks
without an inference request. Free/account evidence expiry independently blocks work:
refresh it only after checking the actual contract/account. An API success does not
renew a billing attestation. The original `.env` is never modified.

Prepared manifests can be resumed with `providers run <manifest> --env-file ...
--accounts ... --output ...`. Repeatable `--only provider:model` supports transport
diagnostics. A single-repeat smoke cannot qualify consistency. Benchmark provider
order is counterbalanced; native/contact-sheet modes precede dense batches so that
one provider does not consume all daily budget before other modes are sampled.
This diagnostic schedule is separate from capability-based production ranking.

## Output and evidence

`latest.json` points to an immutable checksum-named report. Each job retains a latest
resume cursor and immutable attempt files. Reports preserve original clip identity,
source provenance, measurement/file/frame/sheet hashes, protocol/input/dataset
fingerprints, exact model/mode, repeat number, parsed responses, state/error,
wall-clock p50/p95, success/JSON rates, 429/403/503 counts and requests per clip.
Attempt history retains timeout and malformed-output failures after eventual success.
`eventual_completion_rate` is separate from operational attempt success/JSON validity;
quota deferrals are counted separately, and wall latency includes quota waiting.

Per-job HTTP telemetry records reserved units, numeric provider-reported usage and
selected rate/reset headers. It excludes credentials, arbitrary headers and response
content. Reservations are estimates, not provider billing meters. A complete successful
model list marks missing IDs deprecated; partial/error/auth lists cannot. Model-list
presence never overrides pricing or quality evidence.

NVIDIA 202 acceptance persists its request ID and original quota reservation in
SQLite. Resumption polls only the verified same-host NIM `/v1/status/{requestId}`
endpoint, without issuing a second inference POST. GET polls are counted separately;
redirects and expired request IDs block rather than invoke a partner endpoint. This
behavior is fault-tested. The live native-video probe returned 503, so it does not
establish a real successful asynchronous video response. See the
[dated live smoke evidence](provider-smoke-2026-10-04.md).

Groq Free request token ceilings constrain frame batch width, not frame coverage.
All measured frames and full transcript/event context are retained, with smaller
requests when necessary. Oversized text/context blocks without truncation. This
execution change requires its own quality benchmark before production admission.
ASR and embedding benchmarks now use the same validated transports as production.

Quality and `clips_per_day` remain `null` without real qualified workloads and measured
effective account limits. Published daily maxima, unknown NVIDIA quotas and estimated
Cloudflare neurons do not establish production capacity. A talk-only corpus cannot
establish game UI, small UI events, visual gags or their grounding quality.

## Qualification

Reserve a small original-clip holdout excluded from Editorial/Editing Profile training.
Do not relabel trained examples as held out. Capability-specific `Assessment` records
bind to `make_input_fingerprint(result)` and cite reviewer/gold evidence. Free independent
judges may draft `AUTO_WEAK_TEACHER` records; those never grant production access.
Reviewed gold uses `HUMAN_GOLD` or `HUMAN_REVIEWED_TEACHER`. Import one reviewed batch:

```powershell
uv run holoclip providers qualify <immutable-report.json> <reviewed-assessments.json> `
  data/providers/qualifications.json
```

All relevant jobs need reviewed evidence; at least 30 distinct measured inputs from
five original clips, two actual independent repeats, quality >= .8, success >= .95,
JSON validity >= .99, hallucination <= .02, error <= .05, consistency >= .9 and zero
critical hallucinations. Reviewed assessments carry `consistency_facts`: canonical
gold fact IDs for the response's payoff/subject/event claims, shared across wording.
Matching scores alone do not establish semantic consistency; repeated reviewed
fact sets must agree. Missing fact evidence rejects promotion without repeating
cached inference. This is a reviewed evidence declaration, not a model's self-label.
Each dimension needs three distinct measured gold inputs, without counting repeats, and the
router checks stage-specific required dimensions again. Quality/capability/mode are
independent: text reasoning cannot qualify grounding or OCR. Missing, changed,
duplicated, weak, inconsistent and low-quality evidence cannot grant access.

Bundles pin the immutable assessed report hash. Loading verifies the hash and recomputes
every score before accepting declared rows; detached JSON lists and edited scores fail.
Local human-review declarations are an explicit evidence trust boundary, not cryptographic
proof someone watched a video. Independent diagnostic judges can now draft weak grades:

```powershell
uv run --extra media holoclip providers draft-grades <immutable-report.json> `
  --accounts data/provider-benchmark/free-accounts.json --env-file D:\AI\holoclip\.env `
  --output data/provider-benchmark --wait-for-quota
uv run holoclip providers review-pack <immutable-report.json> data/provider-benchmark/review `
  --max-cases 30
```

The judge receives anonymous output and the same reference evidence, uses a different
vendor, and retains quota/attempt/cache state. Visual judges see pinned contact sheets
stacked without rescaling or removing tiles. No provider priority or paid fallback is
introduced. Oversized reference/context defers. ASR/vector usefulness is not guessed
without suitable speech/retrieval references. Reports contain `AUTO_WEAK_TEACHER` only;
judge attempts to declare human review, invent measured IDs or claim untested dimensions
are rejected. Weak grades never grant access, even on held-out cases.

`draft-grades` also writes the local static HTML/JSON review pack in the same invocation;
`review-pack` is an offline export when no API work is needed. The pack is capped at 30 cases, spreads samples over original
clips, separates the provider identity key and provides one shared reference draft per case.
Training clips remain training and every draft starts `UNREVIEWED`. This reduces full-corpus
manual work; it does not fabricate gold or claim a calibrated grader. Gold reference review,
semantic-grade calibration and disagreement adjudication remain open in #107. The existing
reviewed-batch qualification path remains unchanged until those evidence gates pass.

## Contract/discovery audit, October 4 JST / October 3 UTC 2026

- [Groq billing](https://console.groq.com/docs/billing-faqs) requires a payment method
  for Free-to-Developer upgrade. [Rate limits](https://console.groq.com/docs/rate-limits)
  describe Free limits and 429, without automatic paid overage. Account evidence must
  attest the credential belongs to Free; paid spend limits are insufficient.
- [Nemotron Omni model card](https://build.nvidia.com/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning/modelcard)
  is English-only. Native-video metadata does not qualify Japanese reasoning/OCR.
- [NVIDIA Llama 90B](https://build.nvidia.com/meta/llama-3.2-90b-vision-instruct)
  redirected to unavailable. It appeared in authenticated `/models`, but that alone
  does not establish a currently free callable endpoint. It stays policy blocked.
- Cosmos was absent from this account's complete authenticated list. Its hosted video
  schema remains unverified. No paid deployment replaces it.
- [Cerebras catalog](https://inference-docs.cerebras.ai/models/overview) describes Free
  Trial access; recurring zero-cost eligibility remains unverified.
- Gemini, Cloudflare and Mistral have no locally configured credentials. Their real
  comparisons remain pending; mock/fault-test success is not benchmark evidence.

Real native/multi-image/contact-sheet A/B, a game/UI/reaction/OCR holdout, independent
semantic grading, Cosmos/Active Speaker verified transports, contract refresh and
measured capacity remain acceptance gates. Editorial Profile is not Editing Profile.
This does not establish source/finished edit transforms, RenderPlan, renderer QA/repair
or publishing readiness. Parent issues must stay open until evidence gates pass.
