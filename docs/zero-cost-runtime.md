# ZERO_COST_ONLY runtime

All AI inference is guarded, including the legacy NVIDIA text/embedding and Groq
ASR clients. Unregistered models, endpoints, operations, redirects, private data,
paid accounts, stale verification and missing quota estimates fail before dispatch.
Every retry reserves quota. Unknown outcomes are not refunded. SQLite reservations
are account-scoped and atomic across processes; all models share the account budget.
429 persists a block, and StageRun records `blocked_quota`. Successful upstream
artifacts remain reusable. Re-run after reset without deleting the ledger/cache.

Public model evidence expires after seven days. Account evidence expires after
24 hours and is bound to SHA-256 of the API credential. No request or model-list
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
Do not share credentials across paid and free projects. The next capability-mesh
gate will additionally require real, per-capability benchmark qualification before
production selection. Free metadata alone never establishes editing quality.
