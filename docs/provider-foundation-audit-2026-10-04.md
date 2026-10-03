# Provider foundation audit — 2026-10-04

Audited base: `8e540de` on `Kataage/holoclip/main`. Scope: #80, #92, #94, #97, #99.

Read the Editing Intelligence design, provider matrix, README, evaluation, roadmap,
end-to-end blueprint, Editorial Profile/cards, corpus growth, candidate discovery,
measured boundary planning, execution/cache and all three external AI adapters.
Baseline: Ruff clean, 186 tests passed with Python 3.12.11.

## Findings and implementation sequence

1. **#94:** adapters accepted arbitrary base URLs and model overrides. README's
   "no paid fallback" was a convention, not an enforced invariant. A credential on
   a paid account could use the same public inference host. Implement exact model,
   endpoint and operation checks, separate credential-bound account evidence,
   expiring free evidence, public-data declaration, persistent atomic quota ledger,
   redirect rejection, and blocked-stage classification at the HTTP boundary.
2. **#97:** no capability router, health store, per-capability qualification or
   alternative execution mode exists. Production must require qualification;
   metadata alone only admits a benchmark candidate. Preserve the quality floor
   across provider/mode changes. Persist exact successful route provenance.
3. **#92 foundation:** no semantic video adapter or measured-frame contract exists.
   Implement bounded native video, shot-aware frames, contact sheets and evidence
   fusion with strict measured-ID validation. Pixel/time authority stays outside
   semantic output. Training and timeline consumers must use the same contract.
4. **#99:** no held-out provider benchmark corpus/report exists in Git. Build a
   resumable run/score/promote pipeline; weak human references cannot stand in for
   independent gold evaluation. Missing provider keys/media/gold remain blocked.
5. The #80 Editing Profile, RenderPlan, renderer and repair pipeline are still
   future gates. Provider work does not complete #80 or prove finished-video quality.

## Live research corrections

Checked official sources on 2026-10-04, independently of the old matrix:

- [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing) lists free
  standard input/output for `gemini-3.8-flash` and `gemini-3.5-flash-lite`. Billing
  project entitlement must be checked separately; batch/grounding are not admitted.
- [Groq limits](https://console.groq.com/docs/rate-limits) and
  [vision](https://console.groq.com/docs/vision) confirm Qwen 3.8 (three images),
  GPT-OSS 120B and Whisper free-plan candidates. Turbo's availability is not
  evidence of equivalent Japanese quality; default ASR becomes large-v3.
- NVIDIA GLM URLs in the matrix used dots, but catalog links resolve to
  [glm-5-3](https://build.nvidia.com/z-ai/glm-5-3) and
  [glm-5-3-flash](https://build.nvidia.com/z-ai/glm-5-3-flash). The API model IDs
  remain candidates until authenticated model discovery confirms them.
- [Nemotron Omni model card](https://build.nvidia.com/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning/modelcard)
  states English-only. Japanese capability remains unqualified.
- The [Llama 90B candidate page](https://build.nvidia.com/meta/llama-3.2-90b-vision-instruct)
  redirects to `experience-unavailable` in this audit. Disable until reverified.
- [Cloudflare pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)
  confirms 10,000 neurons/day, shared across models, and explicitly lists paid-only
  models. A free model on a paid Workers account is still a charge risk.
- [Mistral quickstart](https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key)
  permits no-card Free mode; [usage limits](https://docs.mistral.ai/admin/billing-usage/usage-limits)
  distinguish included usage from pay-as-you-go. Require account evidence and limits.
- [Cerebras catalog](https://inference-docs.cerebras.ai/models/overview) now says
  Free Trial rather than proving a recurring free contract. Keep diagnostic,
  unavailable for automatic inference until account/contract verification.
- [SambaNova plans](https://cloud.sambanova.ai/plans) require payment/credit purchase;
  [HF pricing](https://huggingface.co/docs/inference-providers/main/pricing) offers
  small credits. Neither is an automatic production fallback.

## Local evidence and limits

`D:/AI/holoclip` is an existing user checkout on `issue-78-editorial-profile`.
Do not alter its branch, artifacts, corpus or secrets. It has NVIDIA/Groq key
configuration, 11 human-clip records, mostly audio-only acquisition, a 71.008-second
1920x1080 human clip and a 7820.848-second 1920x1080 source video. No free-account
billing evidence or cross-provider gold qualification is present yet.

ZERO_COST_ONLY removes all software-controlled paid/tier/host transitions. The
external service controls its account contract: code cannot prove billing-disabled
status from an inference response. A short-lived, credential-bound account attestation
or an authoritative billing API check is required. Do not call an unverified account
and then infer that the successful call was free. Account changes outside Holoclip
invalidate the attestation and require re-verification. This remaining external
trust boundary must be reported rather than described as a mathematical no-charge
guarantee for arbitrary credentials.
