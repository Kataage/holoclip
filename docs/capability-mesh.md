# Capability mesh contracts

`StageContract` declares alternative execution modes, required/optional capabilities,
one unchanged `QualityFloor`, required benchmark dimensions, public-data eligibility,
quota estimate, timeout and whether an independent second opinion is required.
`CapabilityRouter` filters current free/account evidence, health and per-capability,
per-mode qualifications. Ranking uses the weakest required quality, reliability,
hallucinations, quota efficiency and p95 latency. No vendor order is encoded.

Native video, shot-aware multiframe, contact sheet and evidence fusion are independent
modes. A stage can change mode only if that mode independently clears the same floor.
This contract does not call contact sheets equivalent to native video without evidence.
Every required capability must pass; Japanese reasoning never confers grounding ability.

Qualification requires held-out real-media reports, a reviewed gold subset, benchmark
version and dataset/report checksums. Production floors cannot be configured below
0.8 quality / 0.95 success / 0.99 structured validity / at most 0.02 hallucination rate,
zero critical hallucinations and 30 reviewed cases. These are conservative engineering
floors, not a claim of product perfection. Required dimensions must be covered.
Qualification expires after 14 days; current free evidence after seven days. Replacing
a model or changing execution mode cannot inherit another route's scores.

The explicit `provider_benchmark*` diagnostic path bootstraps measurement without
quality qualification. It retains all cost/data/account checks, records
`diagnostic_only=true`, and cannot be selected by ordinary production stages.

SQLite health records survive restart. Timeout/network faults use a five-minute
cooldown; 401/403 disable authentication; 404/410 disable the model; 429 persists quota
block/reset hints. Complete successful model discovery invalidates missing IDs;
partial/error lists cannot mark every model deprecated. Invalid structured output
disqualifies only the affected capability/mode, rather than the whole model's other
capabilities. An in-flight call has a router-enforced wall-clock deadline.

Stage artifacts persist the actual provider/model/mode, benchmark versions, input
fingerprint, free verification and latency. Successful artifacts can be reused even
after a provider disappears. Failure preserves successful upstream and sibling work.

For high-impact uncertain edits, request the second-opinion contract. The second route
must be from another hosting vendor and clear the same floors. Persist AGREE,
SOFT_DISAGREE, HARD_DISAGREE or INSUFFICIENT_EVIDENCE with both opinions/evidence IDs.
Only confident AGREE permits an aggressive action. Missing independent evidence or
disagreement suppresses the action; it never authorizes a cheaper low-quality route.

At this stage no real provider is production-qualified. The adapters and #99 runner
must produce qualification evidence before production routing can execute. Mocked
fault tests prove routing mechanics; they are not Japanese/Live2D quality evidence.
