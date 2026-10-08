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

### Updating official free-model evidence without disabling the safety gate

The built-in model pricing evidence is intentionally dated and expires after
seven days. **Never** remove the expiry check, move its timestamp forward just
because model discovery or inference succeeds, or assume a consumer subscription
proves API pricing.

For an independently reviewed current official pricing page/model endpoint,
write `data/providers/free-model-evidence.json` (or set
`HOLOCLIP_FREE_MODEL_EVIDENCE` to a different private JSON file):

```json
[
  {
    "provider": "nvidia",
    "model": "z-ai/glm-5.3",
    "verified_at": "2026-10-08T09:00:00+09:00",
    "official_reference": "https://build.nvidia.com/z-ai/glm-5-3",
    "reviewed_by": "operator-current-free-pricing-review",
    "confirmed_no_charge": true,
    "paid_fallback_allowed": false
  }
]
```

The timestamp above is **an example only**, not a completed verification.
Replace it only with the actual time at which the stated official free model
pricing was reviewed. Repeat separately for each currently approved free model.
The referenced URL must exactly match one of that model's whitelisted official
references. This refresh can only update the dated proof for an existing allowed
model; it cannot change endpoint, model, operation, billing class, input limits,
quota limits or free/paid restrictions. Unknown, unavailable, future-dated and
expired proofs fail closed.

The local account's **separate credential-bound billing-disabled/free-plan
evidence** must also be current. A public pricing check can never renew the
account attestation. Neither model catalog success nor an HTTP 200 is proof
that a particular account cannot be billed.

This is a safe **operator-attested interim revalidation path**, not automated
authoritative provider/account billing verification. Issue #109 tracks
provider-specific authoritative discovery and automatic checks; if that evidence
is unavailable, production remains blocked. No hidden TTL extension is allowed.

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
