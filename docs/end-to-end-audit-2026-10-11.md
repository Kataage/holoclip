# Holoclip — end-to-end implementation audit (2026-10-11)

Scope: repository **main at PR #201**, with this PR's editorial-boundary patch
evaluated separately. This is a source/design/test-path audit, **not** a live
Windows, NVIDIA/Groq or YouTube run, and not an editorial quality certification.

## Honest end-to-end gate inventory

| Stage | Concrete code / evidence | Result / remaining blocker |
| --- | --- | --- |
| Discovery / acquisition | \`providers/source/yt_dlp.py\`, \`source_pattern_collection.py\`, LAN coordinator / scheduler | Rate-limited, fail-closed local acquisition controls exist. Safe concurrent access and task config tested mostly with mocks. Public YouTube media, 2-PC host identity and effective Windows firewall behavior **not live-qualified**. |
| Transcription / timestamps | \`media.py\`, \`transcription.py\`, \`providers/transcription/\` | Audio segmentation, transcription adapters and measured transcript contracts exist. Real long Japanese Hololive streams and speech/speaker coverage are not certified. |
| Candidate discovery | \`analysis/candidate_detector.py\`, \`editorial_profile.py\`, \`editorial_planning.py\` | Broad/strong candidate logic, retrieval and learned editorial boundary plan exist. Real unseen human-grounded accepted-clip quality remains unproven. The provider router currently has **no production-qualified bundle** (see \`production-provider-runtime.md\`). |
| Source alignment / editor example measurements | \`alignment.py\`, \`holdout_e2e_trial.py\`, \`source_visual_*\`, \`scripted_cut_metrics.py\` | Local source/finished-example measurements and a reserved holdout diagnostic pipeline exist. Synthetic or unreviewed measurements do not establish accurate cut detection on unknown real footage. |
| Editorial cut timeline | \`editorial_planning.py\`, \`domain_adapters.py\`, \`domain.py\` | Measured transcript-ID edit plan to \`ClipTimeline\` exists. **This PR** fixes acceptance of malformed, reversed or unrelated model spans and rejects tampered persisted candidate/edit envelopes and durations. A clip timeline is still not a fully specified RenderPlan. |
| Finished media rendering | \`media.py\`, \`docs/editing-intelligence-design.md\`, \`docs/roadmap.md\` | Low-level FFmpeg audio extraction and preview playback exist. The general \`NarrativePlan → multi-source RenderPlan → deterministic FFmpeg final encode\` workflow remains a **design target, not a completed production path**. |
| Subtitle / motion / edit craft | \`editing-intelligence-design.md\` | Full editing-profile learning, subtitle emphasis, crop, audio mix and transitions do not yet have an accepted, measured production pipeline. |
| Output quality / publish | \`evaluation.py\`, \`editorial_evaluation.py\`, \`clip_policy.py\` | Evaluation and content-policy inspection primitives exist. Automatically rendered final-clip quality QA, automatic repair, and publish approval integration are not production-qualified. Publishing must remain independently approval-gated. |

## Reproduced static boundary risks fixed by this PR

Before this patch, \`validate_edit_plan\` accepted any dict with matching
pattern IDs and span endpoint IDs. It did **not** require:
- span IDs to map to strictly ordered source-row indexes;
- unique, ordered, finite measured source rows;
- a bounded/list-shaped LLM response rather than arbitrary types;
- any selected span to overlap the actual candidate rather than only
  ±60s *context supplied to the model*.

When a candidate with an existing \`editorial_edit_plan\` was converted
to \`ClipTimeline\`, neither plan span chronology, measured duration totals,
nor the candidate's envelope were checked again. Corrupted or stale plan
metadata could therefore be passed into downstream timeline work without
detection.

The fix rejects such model/planned input and keeps the original unedited
candidate when validation fails. The timeline adapter additionally rejects
mismatched already-persisted plans. Deterministic tests exercise legitimate
multi-cut structure, invalid span IDs and ordering, malformed JSON-shaped
values, non-finite source measurements, unrelated context, stored plan drift,
and unchanged legacy continuous candidates. No real media or paid service is
required for these regressions.

## Post-PR #204 local source auto-mapping

The local RenderPlan can now resolve existing canonical YouTube source IDs into complete editing_video cache files. Handwritten source mapping is not required when valid local complete-video caches already exist. Missing/ambiguous audio-only or bounded-section caches are blocked without redownloading. This does not establish clipping rights, real cross-stream semantic coherence, final editorial quality or production readiness.

## Post-PR #205 bounded missing-source acquisition

The local renderer can inventory missing complete full-source caches offline and,
with explicit one-shot execution and local preview permission, attempt one
bounded public video acquisition using the existing authenticated shared-LAN
and per-host yt-dlp pacing controls. Strict policy/metadata eligibility is
checked before attempting full media, and old caches are never overwritten.
This is a bounded on-demand integration, not yet a fully unattended collection
queue, true site-side rate-limit guarantee, production render QA or permission
to publish; ready=false remains unchanged.

## Post-PR #206 unattended acquisition queue

The missing-source one-shot acquisition now has a SQLite-backed queue,
explicit one-time timeline enrollment and a preview-first Windows current-user
scheduled worker. Oldest running/paused jobs block further attempts; transient
pacing waits can be deferred and unknown/rate-limited failures pause without
silently bypassing shared-IP controls. Interrupted claims require offline
reconciliation after a conservative active-worker grace period; no unmeasured
partial media is considered complete. This still is not verified unattended
Windows uptime or a complete finished-clip editing pipeline; ready=false.

## Post-PR #207 automatic evaluated-candidate enqueue

The regular `analyze` and `discover` paths can now enqueue bounded eligible
candidate timelines into the persistent source-acquisition queue after
**one-time** local-preview opt-in; no handwritten ClipTimeline is needed.
Enrollment refuses unknown stream IDs, noncanonical/nonpublic sources,
flagged or low-confidence candidates, malformed cut plans and oversized
selection windows. It does not make raw LLM confidence a human quality
assessment, initiate a download during candidate inference, certify
source rights or perform output video QA. The remaining connection is
**completed source queue → technical render → editorial QA**, without
any automatic publication or readiness promotion (`ready=false`).

## Priority order for subsequent delivery## Post-audit implementation note (PR #203)

A first local, deterministic A/V-only `ClipTimeline` renderer and basic ffprobe
integrity QA are now implemented; see [local render guide](local-render-plan.md).
This narrows the initial gap but does **not** finish the editing-intelligence
pipeline, subtitle/framing/audio-mix synthesis, finished-clip editorial QA,
permission checks, publishing approval or production-grade quality evaluation.
The inventory above captures the state audited before this implementation.



1. **Production capability and editorial quality gates:** collect genuine
   no-cost service/account evidence and held-out Japanese stream reviews,
   without silently loosening \`ready=false\` or charging paid services.
2. **Render-plan contract:** compile selected, policy-eligible \`ClipTimeline\`
   with canonical source IDs and exact measured media trims into a
   deterministic no-AI execution plan; assert source ownership, source
   availability, clip rights and output identity.
3. **Offline local renderer + QA:** only after plan validation, render
   preview/final clips with FFmpeg, confirm duration, source range, audio
   integrity, subtitle legibility and output provenance, then deterministic
   repair for measured failures. Cache each expensive result.
4. **Cross-stream/multi-member work:** validate multi-source source-asset
   provenance and ordered timeline rendering rather than assuming one
   source clip. Use reserved unknown-stream holdouts for regression.
5. **Operator burden and final gate:** no new repetitive manual labeling or
   per-video preparation. Do not add auto-upload until policy checks,
   trusted final QA and **explicit publication approval** are enforced.

None of the above is evidence of current production eligibility. The
protected router and all LAN diagnostic reports remain \`ready=false\`.
