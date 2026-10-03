# Zero-Cost AI Provider Matrix — 2026-10-04

Status: research baseline  
Revalidation: see [2026-10-04 implementation audit](provider-foundation-audit-2026-10-04.md).
Its unavailable/diagnostic findings supersede candidate availability below; no
PRIMARY/SECONDARY wording in this research snapshot grants production qualification.
Applies to: #80, #92, #94, #97  
Policy: ZERO_COST_ONLY  
Constraint: local AI inference is excluded from the initial provider set. Local deterministic media tooling remains allowed.

## 1. Goal

Holoclip must not merely "have a free fallback".

It must preserve production quality while spending zero monetary API cost, even when:
- a preferred provider changes its free tier,
- a model is deprecated,
- a free native-video model becomes unavailable,
- a provider is rate-limited,
- a provider returns 403/429/503,
- one model is strong at Japanese but weak at visual grounding,
- one model supports video but has weak Japanese semantics.

The stable architecture is therefore:
- capability-first,
- provider-agnostic,
- benchmark-qualified,
- quota-aware,
- resumable,
- able to combine multiple zero-cost providers.

No single provider is the product architecture.

## 2. Qualification classes

Each provider/model is qualified per capability, never globally.

Production status:
- PRIMARY: preferred for a capability after benchmark.
- SECONDARY: qualified fallback.
- CROSS_CHECK: used only for high-impact ambiguity.
- DIAGNOSTIC: useful for comparison but not production.
- REJECTED: violates zero-cost/reliability/privacy/quality requirements.
- PENDING: official free access exists but real Holoclip benchmark is not complete.

## 3. Native-video / omni-modal candidates

### Google Gemini Developer API

Current official state on 2026-10-04:
- Gemini Developer API has a Free Tier.
- Gemini 3.8 Flash Standard currently lists free input/output in the API pricing table.
- Gemini 3.5 Flash and Gemini 3.5 Flash-Lite also currently list Free Tier access.
- Gemini 3.5 Flash-Lite accepts text/image/video/audio.
- Free-tier data may be used to improve Google products.
- Rate limits are project/model specific and can be restrictive.

Important:
- consumer Gemini app plan changes are NOT API entitlement evidence.
- API free status must be reverified before every production learning cycle.
- never bind architecture to Gemini Flash/Pro remaining free.

Candidate status:
- video_semantics_native: PENDING PRIMARY
- japanese_text_reading: PENDING PRIMARY
- semantic_render_qa: PENDING PRIMARY
- strong text reasoning: SECONDARY while free

Failure mode:
- 429/403/503 => free fallback/defer, never billing.

Official references:
- https://ai.google.dev/gemini-api/docs/pricing
- https://ai.google.dev/gemini-api/docs/rate-limits
- https://ai.google.dev/gemini-api/docs/models

### NVIDIA Nemotron 3 Nano Omni 30B A3B Reasoning

Model:
- nvidia/nemotron-3-nano-omni-30b-a3b-reasoning

Official state:
- NVIDIA API Catalog Free Endpoint available.
- inputs: video, audio, image, text.
- output: text.
- video: MP4, up to 2 minutes.
- supports OCR.
- supports JSON output.
- supports word-level timestamps for transcription.
- model card states language support: English only.

Implication:
This is a very important independent native-video path, but English-only language support means it cannot be trusted alone for Japanese semantic interpretation.

Best use:
- visual event understanding
- motion/action/event semantics
- audio/visual cross-check
- OCR/layout diagnostics where language is not central
- independent native-video second opinion

Candidate status:
- video_semantics_native: PENDING SECONDARY
- image_semantics: PENDING SECONDARY
- audio_semantics: PENDING SECONDARY
- Japanese dialogue semantics: NOT QUALIFIED until benchmark proves otherwise

Official reference:
- https://build.nvidia.com/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning

### NVIDIA Cosmos3 Nano Reasoner

Model:
- nvidia/cosmos3-nano-reasoner

Official state:
- Free Endpoint available.
- accepts video or image.
- built for structured reasoning over physical-world visual/video inputs.
- hosted free API is rate-limited.

Implication:
It is not designed specifically for VTuber streams or Japanese dialogue, but it provides another vendor-independent native-video reasoning path.

Best use:
- visual event/change reasoning
- game/action event second opinion
- motion causality
- visual continuity

Candidate status:
- video_semantics_native: PENDING SECONDARY/CROSS_CHECK
- Japanese dialogue semantics: not assumed

Official reference:
- https://build.nvidia.com/nvidia/cosmos3-nano-reasoner

## 4. Multi-image / frame reasoning candidates

### Groq Free Plan — Qwen 3.8 27B

Model:
- qwen/qwen3.8-27b

Official state:
- Free Plan: 30 RPM, 1,000 RPD, 8K TPM, 200K TPD.
- multimodal text + image.
- maximum 3 images/request.
- 20 MB max image request size.
- JSON mode/schema support.
- OCR and visual QA use cases documented.
- multilingual model.
- preview lifecycle; Groq does deprecate models aggressively.

This is one of the strongest zero-cost multi-image candidates because it combines:
- Japanese-capable text reasoning
- image understanding
- OCR
- structured output
- high request/day allowance
- very fast inference

Candidate status:
- video_semantics_multiframe: PENDING PRIMARY
- japanese_text_reading: PENDING PRIMARY
- image_semantics: PENDING PRIMARY
- text_reasoning_fast: PENDING PRIMARY
- text_reasoning_strong: PENDING SECONDARY
- semantic_render_qa: PENDING SECONDARY

Required protection:
- discover active model through Groq Models API.
- deprecation-aware registry.
- if model is preview/deprecated, qualification expires automatically.

Official references:
- https://console.groq.com/docs/vision
- https://console.groq.com/docs/rate-limits
- https://console.groq.com/docs/deprecations

### NVIDIA GLM 5.3 Flash

Model:
- z-ai/glm-5.3-flash

Official state:
- NVIDIA Free Endpoint.
- multimodal text/image.
- reasoning/tool use.
- image-to-text.

Known Holoclip fact:
- existing NVIDIA endpoints can time out under current workloads.
- therefore this must never be the only path.

Candidate status:
- image_semantics: PENDING SECONDARY
- text_reasoning_fast: PENDING SECONDARY
- multi-image/contact-sheet reasoning: PENDING SECONDARY

Official reference:
- https://build.nvidia.com/models?label=reasoning%2Ctext

### NVIDIA Llama 3.2 90B Vision

Model:
- meta/llama-3.2-90b-vision-instruct

Official state:
- Free Endpoint available.
- text + image input.
- 131K context.
- documented capabilities include Visual Grounding, Visual QA, image captioning, image-to-text.

Candidate status:
- image_semantics: PENDING SECONDARY
- visual_grounding_hint: PENDING PRIMARY/SECONDARY
- cross-check: strong candidate

Official reference:
- https://build.nvidia.com/meta/llama-3.2-90b-vision-instruct

### Cloudflare Workers AI — Llama 3.2 11B Vision

Model:
- @cf/meta/llama-3.2-11b-vision-instruct

Official state:
- Workers Free has 10,000 Neurons/day at no charge.
- Llama 3.2 11B Vision is available in Workers AI.
- vision support documented.
- if free allocation is exceeded on Workers Free, operations fail rather than bill.
- some high-end Cloudflare models require Workers Paid and must be excluded.

This is valuable because it is a completely separate hosting/provider path from NVIDIA/Groq/Google.

Candidate status:
- image_semantics: PENDING SECONDARY
- video_semantics_multiframe: PENDING SECONDARY
- emergency provider diversity: high value

Official references:
- https://developers.cloudflare.com/workers-ai/platform/pricing/
- https://developers.cloudflare.com/workers-ai/models/llama-3.2-11b-vision-instruct/

### Mistral Studio Free mode

Official state:
- Free mode is enabled by default.
- API keys can be created with no credit card in the normal quickstart flow.
- Free mode provides included monthly usage with rate/usage limits.
- Mistral offers multiple vision-capable models via Chat Completions.
- exact included allowance is account/limits-page dependent.
- pay-as-you-go behavior must be kept disabled.

Caveat:
Mistral docs around workspace/billing are not perfectly uniform. This provider must pass an account-level no-payment live probe before production qualification.

Candidate status:
- image_semantics: PENDING
- japanese_text_reading: PENDING
- text_reasoning: PENDING
- not eligible for production until zero-cost account behavior is verified.

Official references:
- https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key
- https://docs.mistral.ai/admin/billing-usage/usage-limits
- https://docs.mistral.ai/studio/conversations/vision

## 5. Text reasoning candidates

### NVIDIA GLM 5.3

Model:
- z-ai/glm-5.3

Official:
- NVIDIA Free Endpoint.
- 1M context.
- strong reasoning/tool use.

Known Holoclip issue:
- repeated timeout on existing strong-model calls.

Status:
- text_reasoning_strong: PENDING SECONDARY
- must not block corpus growth.

### Groq Free Plan — GPT-OSS 120B

Model:
- openai/gpt-oss-120b

Official Free Plan:
- 30 RPM
- 1,000 RPD
- 8K TPM
- 200K TPD

Advantages:
- separate provider from NVIDIA.
- production model on Groq.
- useful as strong/medium reasoning fallback.

Status:
- text_reasoning_strong: PENDING PRIMARY/SECONDARY

Official references:
- https://console.groq.com/docs/rate-limits
- https://console.groq.com/docs/models

### Groq Qwen 3.8 27B

See multimodal section.

Status:
- text_reasoning_fast: strong candidate
- text_reasoning_strong: benchmark candidate
- especially useful because image + text tasks can stay on one provider when quota allows.

### Cloudflare Workers AI

Workers Free offers 10,000 Neurons/day.
Cloudflare documents multiple models remaining available on Workers Free, including examples such as Gemma and Nemotron families; however resource-intensive models can be moved to Workers Paid.

Status:
- text_reasoning_fast: PENDING SECONDARY
- text_reasoning_strong: model-dependent PENDING
- registry must reject any model requiring Workers Paid.

### Cerebras Free

Official pricing:
- Free tier: $0, lower rate limits.
- free API key available.

Potential:
- very fast text reasoning
- independent second-opinion provider
- useful for consolidation/judging where the current free model list passes benchmark.

Status:
- text_reasoning_fast: PENDING SECONDARY
- text_reasoning_strong: PENDING
- not a visual provider.

Official reference:
- https://inference-docs.cerebras.ai/support/pricing

### OpenRouter Free

Official:
- 25+ free models.
- 4 free providers.
- 50 requests/day on a free account.
- free model pool is explicitly dynamic.

Use only for:
- rare high-value cross-checks
- provider diversity
- benchmark testing

Do not use for bulk corpus processing.

Status:
- CROSS_CHECK / TERTIARY

Official references:
- https://openrouter.ai/pricing/
- https://openrouter.ai/collections/free-models

### SambaNova

Current official plan page says the Free plan requires adding a payment method and purchasing credits to run requests.

Therefore it does not satisfy the clean ZERO_COST_ONLY invariant for automatic production.

Status:
- REJECTED as automatic zero-cost production provider.
- may be manually reevaluated if SambaNova restores an unambiguous no-payment recurring free API.

Official reference:
- https://cloud.sambanova.ai/plans

### Hugging Face Inference Providers

Official:
- Free users receive only $0.10/month credits, subject to change.
- extra usage is pay-as-you-go/credit based.

Status:
- DIAGNOSTIC/BENCHMARK_ONLY.
- not production fallback.

Official reference:
- https://huggingface.co/docs/inference-providers/main/pricing

## 6. ASR / word timing

### Groq Free Plan Whisper

Models:
- whisper-large-v3
- whisper-large-v3-turbo

Official Free Plan:
- 20 RPM
- 2,000 RPD
- 7,200 ASH
- 28,800 ASD

Holoclip preference:
- whisper-large-v3 for quality.
- turbo only if benchmark says Japanese quality is sufficient for a specific lower-risk stage.

Status:
- asr_segment_timestamps: PRIMARY candidate
- asr_word_timestamps: PRIMARY candidate

Official reference:
- https://console.groq.com/docs/rate-limits

### NVIDIA Nemotron 3 Nano Omni

Supports audio and word-level timestamps, but model card states English-only language support.

Status:
- ASR cross-check for non-Japanese/technical verification only.
- not Japanese production ASR.

## 7. Embeddings

### NVIDIA Nemotron 3 Embed 1B

Model:
- nvidia/nemotron-3-embed-1b

Official:
- Free Endpoint.
- text embedding / retrieval / RAG.

Status:
- embedding: PRIMARY candidate

Official reference:
- https://build.nvidia.com/models?q=embed

Architecture requirement:
- embedding artifacts must store model/version/dimension.
- index rebuild must be possible if provider disappears.
- never make one embedding space irreversible.

## 8. Specialized zero-cost media services

### NVIDIA Active Speaker Detection

Official:
- Downloadable Free Endpoint.
- accepts video, optional isolated audio, and diarization input.
- detects/tracks speaker identities across frames.
- commercial/non-commercial use documented.

Potential Holoclip use:
- multi-person/collab videos
- speaker-to-visible-subject mapping
- source-switch validation
- active-speaker focus evidence

Status:
- PENDING specialized provider.

Official reference:
- https://build.nvidia.com/nvidia/active-speaker-detection

### NVIDIA Background Noise Removal / Studio Voice

Free endpoints appear in the NVIDIA model catalog.

Potential:
- optional audio-restoration/render preprocessing.

Policy:
- do not automatically "improve" source audio until A/B proves it preserves intentional vocal characteristics.
- not part of Editing Profile learning signal.

Status:
- OPTIONAL / PENDING.

## 9. Recommended provider topology

Do not pick one global provider stack.

### Native video semantic understanding
Benchmark pool:
1. Gemini current Free Tier video-capable model
2. NVIDIA Nemotron 3 Nano Omni
3. NVIDIA Cosmos3 Nano Reasoner

Production selection:
- strongest benchmark-qualified currently-free provider for the event type.
- Japanese semantic tasks may prefer Gemini if it materially beats English-only NVIDIA Omni.
- visual-motion event tasks may use NVIDIA as primary or second opinion.

### Shot-aware multi-image understanding
Benchmark pool:
1. Groq Qwen 3.8 27B
2. NVIDIA GLM 5.3 Flash
3. NVIDIA Llama 3.2 90B Vision
4. Cloudflare Llama 3.2 11B Vision
5. Mistral Free vision model

This path is REQUIRED and must be able to replace native video entirely.

### Japanese text/OCR
Benchmark pool:
1. Gemini free vision model
2. Groq Qwen 3.8 27B
3. Mistral Free vision
4. NVIDIA GLM 5.3 Flash / Llama Vision where appropriate

Region geometry/timing always stays deterministic in OpenCV.

### Strong editorial reasoning
Benchmark pool:
1. Groq GPT-OSS 120B
2. NVIDIA GLM 5.3
3. Gemini currently free strong/Flash model
4. Cerebras current Free model
5. Mistral Free text model
6. Cloudflare qualifying Free model

Do not let known NVIDIA timeout behavior block learning when an independent qualified free text provider exists.

### Fast reasoning
Benchmark pool:
1. Groq Qwen 3.8 27B
2. NVIDIA GLM 5.3 Flash
3. Gemini Flash-Lite/current free low-cost model
4. Cloudflare free fast model
5. Cerebras free model

### ASR
Primary:
- Groq whisper-large-v3

Future free candidates may be added only after Japanese held-out WER/segmentation benchmark.

### Embedding
Primary:
- NVIDIA nemotron-3-embed-1b

Fallback embedding spaces are allowed, but never mix vectors from different spaces in one index.

## 10. Quality-preserving fallback ladder

A fallback is not allowed to silently reduce quality.

For each capability define:
- production_quality_floor
- provider benchmark score
- confidence calibration

On provider failure:

1. use another provider that independently clears the SAME quality floor.
2. if no provider clears the floor, try a different execution mode (native video -> multi-image -> contact sheet) that still clears the floor.
3. if none clears the floor, BLOCK/DEFER.
4. never lower the quality threshold to keep the pipeline moving.

This is the meaning of "free without compromise".

## 11. High-impact consensus policy

Use two independent providers only when the edit decision is high impact.

Examples:
- deleting or retaining payoff/context
- strong face/game crop
- multi-source identity/source-switch assertion
- factual context overlay
- semantic QA that would trigger a re-render

Consensus states:
- AGREE
- SOFT_DISAGREE
- HARD_DISAGREE
- INSUFFICIENT_EVIDENCE

HARD_DISAGREE:
- suppress aggressive edit,
- retain safer framing/context,
- optionally queue another free provider when quota resets.

## 12. Provider health and qualification

Provider availability is a runtime state, not a static config.

Track:
- VERIFIED_FREE
- HEALTHY
- DEGRADED
- QUOTA_LOW
- BLOCKED_QUOTA
- AUTH_FAILED
- MODEL_DEPRECATED
- FREE_STATUS_STALE
- DISQUALIFIED_QUALITY

Each provider/model/capability has:
- verified_at
- last_success_at
- last_failure_at
- rolling success rate
- p50/p95 latency
- 429/403/503 rate
- remaining quota where observable
- benchmark version/score
- data-use class

Routing uses this state.

## 13. Periodic requalification

Before a major corpus-learning cycle:
- re-check official free status
- list active provider models where an API permits it
- run cheap canary requests
- reject any route that requires payment method/billing beyond the approved policy
- refresh provider health

On model change/deprecation:
- do not silently inherit old benchmark scores.
- run minimum regression benchmark before production use.

## 14. Completion standard

The zero-cost provider system is production-ready only when all of these are demonstrated:

1. preferred native-video provider removed -> multi-image path still produces valid observations.
2. Gemini unavailable -> NVIDIA/Groq/Cloudflare/Mistral path can cover required visual capabilities at the same quality floor or the job safely defers.
3. NVIDIA strong text timeout -> another qualified free text provider completes the reasoning stage.
4. Groq quota exhausted -> completed ASR chunks persist and resume after reset.
5. provider deprecation -> registry invalidates qualification automatically.
6. provider becomes paid-only -> production router rejects it before a chargeable request.
7. two providers disagree on a high-impact edit -> aggressive edit is suppressed.
8. no paid endpoint is reached during fault-injection tests.
9. no quality threshold is lowered to preserve throughput.
10. a real unseen Japanese Hololive clip reaches RenderPlan and edited preview through zero-cost-qualified routes.

"Some free API answered" is not completion.
