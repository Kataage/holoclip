# Live zero-cost provider smoke, 2026-10-04 JST

**NOT QUALIFIED.** This is live transport evidence, not a production quality
benchmark. The machine-readable, sanitized snapshot is
[provider-smoke-2026-10-04.json](evidence/provider-smoke-2026-10-04.json).
No provider received production admission from these results.

## Material and scope

The public human-made Japanese Hololive clip
[GnnCl92qJ_M](https://www.youtube.com/watch?v=GnnCl92qJ_M), already present in the
user's learning corpus as `77e729a8a3a9a368`, supplied the same 0–20 second window
for native video, contact sheets, shot-aware frames and individual-image fusion.
Its measured video is 17,757,220 bytes, SHA-256
`7d4921e2c5ef2dbb1b9a1c51db804cf30624875ce3056c0626433f313cce1e04`.
FFprobe/PySceneDetect measurement produced 10 frames and two deterministic sheets.
Existing transcript context accompanied the visual inputs; learned selection/payoff
answers were excluded from inference prompts. ASR used bounded audio from the same
window, and text/embedding tasks used its Japanese transcript.

This sample is from training, is talk-focused, and has one independent repeat.
It is explicitly `held_out=false`. No reviewed gold, game/UI coverage or consistency
qualification is claimed. The user's `D:\AI\holoclip` checkout, media and `.env`
were read-only; new acquisition, measurements, quota ledger and raw reports live in
the ignored `C:\work\holoclip\data\provider-benchmark` directory.

## Observations

The initial v1 run had 47 scheduled jobs and 19 actual inference attempts. Jobs
without credentials, current free proof or available models made no inference POST.
The report file SHA-256 is
`f38c5d2f74d47fe58bc061314145112d7d3d73fe2ff6ab95a8fb83f37de6b52f`.
Times below are single observed attempt durations, not statistical latency estimates.

| Provider/model | Task | Observed result |
| --- | --- | --- |
| Groq Qwen 3.8 27B | Transcript reasoning | Structured response, 0.515 s |
| Groq GPT-OSS 120B | Transcript reasoning | Structured response, 2.406 s |
| Groq Whisper large v3 | ASR | Segment/word lists, 0.922 s; accuracy ungraded |
| Groq Whisper large v3 turbo | ASR | Segment/word lists, 0.860 s; no quality admission |
| NVIDIA Nemotron Embed 1B | Embedding | Vector response, 0.594 s; usefulness ungraded |
| NVIDIA Nemotron Omni | Transcript reasoning | Structured response, 30.453 s; Japanese unqualified |
| NVIDIA GLM 5.3 | Transcript reasoning | 40 s timeout |
| NVIDIA GLM 5.3 Flash | Text/contact sheet/multi-image/fusion | 40 s timeouts |
| NVIDIA Nemotron Omni | Native/contact sheet/multi-image | 40 s timeouts |
| NVIDIA Nemotron Omni | Individual-image fusion | HTTP 200, invalid semantic output |
| Groq Qwen 3.8 27B | Contact sheet/multi-image | Partial cached responses, then HTTP 429 |

After reset handling and request batching repairs, the v2 Qwen run completed the
two-sheet contact-sheet job for the full window and the text job. Multi-image and
fusion have partial cached work and remain `BLOCKED_QUOTA`. All 10 measured frames
remain in the planned requests. This is transport completion only: v2 needs its own
semantic benchmark. Its snapshot report file SHA-256 is
`2e63e02d4bd83f70ce0d9507114ca6e6619f98256f361d2164f3b039c7cc0e54`.

The updated native Nemotron probe, using the documented polling header, returned
HTTP 503 on one POST. No real 202/poll success was observed. Restart-safe 202 polling
is proven by fault tests, which do not qualify native-video semantics.

At the snapshot, the complete ledger retained 50 inference attempts: 17 HTTP 200,
24 HTTP 429, one HTTP 503 and eight uncertain outcomes. A 200 count includes partial
units and invalid output; it is not a clip completion or quality score. Conservative
reservations are retained for failures/unknown outcomes. The Groq internal daily
token ceiling is 120,000; 119,951 estimated tokens are reserved for October 4 UTC.
The next internal daily reset is October 5 00:00 UTC / 09:00 JST. No ceiling was
raised, uncertain usage refunded, input discarded or paid fallback enabled.

## Contract and availability decisions

The user confirmed NVIDIA free access and a Groq Free account. Local account evidence
binds to credentials, fixed internal limits and that attestation; it does not claim
an inspected account dashboard or invoice. Official
[Groq billing](https://console.groq.com/docs/billing-faqs) requires a payment method
to upgrade from Free, and [Free rate limits](https://console.groq.com/docs/rate-limits)
stop excess requests with 429. Requests only used exact guarded free endpoints.
No paid tier, credit purchase, account modification or paid partner request occurred
in this validation. Server-side account/billing facts remain an external trust boundary.

NVIDIA's complete authenticated model discovery omitted Cosmos for this account;
its hosted schema also remains unverified. Llama 90B appeared in discovery while its
public hosted page was unavailable, so model presence did not override missing free
proof. Cerebras remained blocked because recurring free eligibility was unverified.
Gemini, Cloudflare and Mistral had no supplied credentials and were not live tested.
Nemotron Omni's [English-only model card](https://build.nvidia.com/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning/modelcard)
prevents unconditional Japanese admission despite a successful text response.

## Repairs and remaining gates

The v2 transport patch parses date/duration reset headers, distinguishes intrinsically
oversized requests from replenishable quota, saves only numeric error metadata,
retains failed-attempt statistics after resume, and splits Groq frame requests while
preserving all inputs. NVIDIA 202 state uses the official
[same-host NIM status endpoint](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-nano-omni-30b-a3b-reasoning-statuspolling).
Embedding inputs use `truncate=NONE` as specified by the
[embedding API](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-embed-1b-infer),
and malformed ASR/vector envelopes become controlled dependency/quality failures.

Production still has zero qualified routes. Required next evidence includes a small
reviewed original-clip holdout with Japanese/Live2D/reaction/OCR and game/small-UI/gag
coverage, independent repeats, automated weak-teacher grading with reviewed gold,
additional eligible free credentials, full mode A/B and measured quota efficiency.
The current fusion mode deterministically combines individual-image observations;
global evidence adjudication is not established. Editing Profile, source-to-finished
edit learning, RenderPlan, rendering and automatic semantic QA/repair remain their
own acceptance gates. Parent issues remain open.
