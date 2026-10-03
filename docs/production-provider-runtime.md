# Production capability routing (#97)

All existing CLI learning/discovery/retrieval/transcription commands construct mesh
protocol adapters. Business logic requests strong/fast reasoning, JSON, embeddings
or ASR timestamp capabilities. It no longer chooses NVIDIA/Groq model order.
Vendor adapters remain available for explicit compatibility/transport diagnostics,
and still enforce the monetary guard. The benchmark's diagnostic bypass is confined
to benchmark stages; production never grants quality on a free HTTP success.

`MeshLLMClient` fulfills `LLMProvider`. `MeshEmbeddingClient` fulfills the embedding
protocol. `MeshTranscriber` fulfills the segment interface and independently gates
word timestamp observations. Every request needs current free contract/account
evidence, public-data classification and per-capability/mode reviewed qualification.
Each route applies the same production floor. A timeout/removed model can move to a
different qualified zero-cost route; insufficient quality/quota causes a persisted
block. No account upgrade or paid partner/serverless path exists.

The existing corpus does not yet supply a production-qualified bundle. On the real
local settings, `holoclip doctor` therefore reports `BLOCKED_DEPENDENCY` and empty
qualified routes. That is a readiness failure, not successful end-to-end editing.
Doctor performs metadata discovery only, without inference. Source/media artifact
preparation and cached results are preserved for later qualified resumption.

```powershell
$env:HOLOCLIP_PROVIDER_ENV = 'D:\AI\holoclip\.env'
$env:HOLOCLIP_FREE_ACCOUNTS = 'C:\work\holoclip\data\provider-benchmark\free-accounts.json'
$env:HOLOCLIP_QUOTA_LEDGER = 'C:\work\holoclip\data\provider-benchmark\quota.db'
$env:HOLOCLIP_QUALIFICATIONS = 'C:\work\holoclip\data\providers\qualifications.json'
$env:HOLOCLIP_DATA_CLASS = 'public'
uv run --extra media holoclip doctor
```

The optional key-file path reads only recognized key assignments. It does not execute
`.env`, copy keys into output, overwrite environment values or modify the user's file.
Existing vendor-specific model/base overrides cannot select production endpoints.
`HOLOCLIP_AI_TIMEOUT_SECONDS` controls the bounded stage limit (1..120 seconds).

Complete successful model discovery is cached for six hours and rechecked on startup.
Missing IDs invalidate that provider/model endpoint; new IDs do not inherit scores.
Discovery errors/pagination cannot invalidate the entire provider. A returned ID does
not prove free status. Expired pricing/account proof remains independently blocked.

Legacy business artifact namespaces are separated from mesh inference. Actual selected
provider/model, mode and benchmark versions remain in router provenance. An outer timeout
marks an interrupted stage cancelled; uncertain requests retain quota reservations.
Quota failures are mapped to `BLOCKED_QUOTA`, including failures surfaced through legacy
business stage wrappers. Input/schema/policy failures cannot silently become a weaker
production ranking. Default visual upload interfaces also require explicit public-data
classification; private local paths are not assumed public.

Embedding indexes use `mesh:<provider>:<model>` namespaces. Once a vector space is pinned,
model deprecation cannot silently mix another model into it. A separately qualified model
requires rebuilding a separate index. ASR segment qualification does not grant word
qualification, and turbo is eligible only where its actual benchmark clears the floor.

`providers run --wait-for-quota` optionally saves the cursor and automatically sleeps in
bounded intervals until the stored reset, resuming only quota-blocked jobs. It does not
repeatedly hammer timeout/schema/auth failures or ask the user to relaunch each batch.
Status/reset interpretation and asynchronous native provider transports are still being
validated against real services. Neither this wiring nor a passed mock/fault suite proves
real Japanese/game UI/Live2D/OCR editing quality. See [benchmark gates](provider-benchmark.md).
