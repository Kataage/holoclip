# ZERO_COST_ONLY runtime

All AI inference is guarded, including the legacy NVIDIA text/embedding and Groq
ASR clients. Unregistered models, endpoints, operations, redirects, private data,
paid accounts, stale verification and missing quota estimates fail before dispatch.
Every retry reserves quota. Unknown outcomes are not refunded. SQLite reservations
are account-scoped and atomic across processes; all models share the account budget.
429 persists a block, and StageRun records `blocked_quota`. Successful upstream
artifacts remain reusable. Re-run after reset without deleting the ledger/cache.

Public model and account evidence expire within seven days; operators can shorten
account validity with `evidence_ttl_seconds`. This allows a next-day quota resume
within the original attestation's validity, without renewing it from an API response.
Evidence binds to SHA-256 of the API credential. No request or model-list
response refreshes free evidence. Official pricing and billing-disabled entitlement
are distinct checks. See the dated audit for the external account trust boundary.

Set `HOLOCLIP_DATA_CLASS=public` only for the intended public media workflow.
Set `HOLOCLIP_FREE_ACCOUNTS` to a local JSON list following this shape:

```json
[
  {
    "provider": "groq",
    "credential_sha256": "SHA256_OF_KEY_COMPUTED_LOCALLY",
    "account_id": "ACCOUNT_OR_PROJECT_ID",
    "plan": "FREE",
    "billing_enabled": false,
    "paid_fallback_allowed": false,
    "verified_at": "ACTUAL_VERIFICATION_TIME_WITH_TIMEZONE",
    "evidence_reference": "LOCAL_ACCOUNT_LIMITS_AND_BILLING_EVIDENCE",
    "daily_limits": {
      "requests": 100,
      "tokens": 50000,
      "audio_seconds": 3600,
      "uploaded_bytes": 50000000
    }
  }
]
```

Do not fill false/Free values without checking the actual account. Limits above are
illustrative internal ceilings, not provider allowances. Unknown units block; public
limits impose an additional 80% ceiling where known. Shorter windows remain enforced
by the provider's rate gates and 429/reset state. External usage can consume quota;
the ledger is a conservative local scheduler, not an authoritative provider meter.

Default ledger: `data/providers/quota.db`; override via `HOLOCLIP_QUOTA_LEDGER`.
Do not share credentials across paid and free projects. The capability-mesh
gate additionally requires real, per-capability benchmark qualification before
production selection. Free metadata alone never establishes editing quality.

`Retry-After` seconds/dates and known rate-reset duration headers schedule quota
resumption. Only numeric quota error metadata is retained; provider error prose and
organization identifiers are excluded. A request larger than the Free per-minute
ceiling is a dependency block, not a promise that tomorrow's quota will make it fit.
Measured frame batches retain every frame and full transcript context. Inputs are
never truncated to fit; NVIDIA embeddings explicitly use `truncate=NONE`.
