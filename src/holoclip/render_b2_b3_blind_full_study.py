"""Blinded full-length B2/B3 local playback study, not automated quality grading.

Reuses current SHA-verified *finished* video files and the independent
source/B2/B3 short-review receipt. A create-only offline reviewer folder
contains neutral video names and an optional local-only rating form.
The role mapping is stored outside the reviewer folder. No new downloads,
encodes, model calls, publishing, winner selection or fabricated ratings.
"""
from __future__ import annotations

import hashlib
import html
import os
from pathlib import Path

from holoclip.render_b2_b3_editorial_evidence import run_editorial_evidence
from holoclip.render_b2_b3_local_review_pack import _basis
from holoclip.render_human_edit_context import DEFAULT_GROWTH_STATE, DEFAULT_HUMAN_STORE
from holoclip.render_plan import _hash, _no_links
from holoclip.render_ready_queue import DEFAULT_PREVIEW_ROOT
from holoclip.render_same_source_geometry import DEFAULT_TRANSCRIPT_ROOT
from holoclip.render_same_source_semantic_review import save_semantic_review
from holoclip.render_source_queue import DEFAULT_QUEUE
from holoclip.render_three_way_existing_media_autopilot import _exclusive_run_lock

VERSION = "holoclip-b2-b3-blind-full-media-study-v1"
_MAX_MEDIA_BYTES = 1024 * 1024 * 1024
_ROLES = ("b2", "b3")
_DIMENSIONS = ("speech_naturalness", "narrative_continuity", "payoff_completion")


def _study_html(study_id: str) -> str:
    """Self-contained file:// reviewer UI. No network or external libraries."""
    title = html.escape(study_id)
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><title>B2/B3 Blind Review</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:sans-serif;max-width:1000px;margin:2rem auto;padding:1rem}}
video{{width:100%;max-width:800px}}section{{padding:1rem;border:1px solid #999;margin:1rem 0}}
label{{display:block;margin:.7rem 0}}input{{width:5rem}}button{{padding:.6rem}}</style>
</head><body><h1>ブラインド編集比較</h1>
<p>編集案の正体は伏せています。2本の完成動画を最後まで視聴して評価してください。
短い一部分だけでは、話の流れやオチを確定できません。</p>
<p>研究ID: <code>{title}</code></p>
<form id="review">
<section><h2>候補1</h2><video controls preload="metadata" src="candidate-1.mp4"></video>
<label>発話の自然さ (1〜5) <input name="one_speech" type="number" min="1" max="5" required></label>
<label>文脈のつながり (1〜5) <input name="one_story" type="number" min="1" max="5" required></label>
<label>オチ・リアクションの成立 (1〜5) <input name="one_payoff" type="number" min="1" max="5" required></label>
</section>
<section><h2>候補2</h2><video controls preload="metadata" src="candidate-2.mp4"></video>
<label>発話の自然さ (1〜5) <input name="two_speech" type="number" min="1" max="5" required></label>
<label>文脈のつながり (1〜5) <input name="two_story" type="number" min="1" max="5" required></label>
<label>オチ・リアクションの成立 (1〜5) <input name="two_payoff" type="number" min="1" max="5" required></label>
</section>
<label>全体の好み <select name="preference"><option value="undecided">判定保留</option>
<option value="candidate-1">候補1</option><option value="candidate-2">候補2</option>
<option value="tie">同等</option></select></label>
<button type="submit">評価JSONを端末へ保存（外部送信なし）</button>
</form>
<p>このフォームは独立評価者が実際に入力するためのものです。
未入力の評価・品質点・AIによる推定値は一切生成しません。</p>
<script>
document.getElementById("review").addEventListener("submit", function(e) {{
  e.preventDefault();
  const f = new FormData(e.target);
  const score = (n) => Number(f.get(n));
  const data = {{
    version: "holoclip-blind-human-rating-v1",
    study_id: "{title}",
    claimed_human_rated: true,
    ratings: {{
      "candidate-1": {{speech_naturalness:score("one_speech"),
        narrative_continuity:score("one_story"),payoff_completion:score("one_payoff")}},
      "candidate-2": {{speech_naturalness:score("two_speech"),
        narrative_continuity:score("two_story"),payoff_completion:score("two_payoff")}}
    }},
    preference: f.get("preference"),
    independent_human_gold_verified: false,
    automatic_adoption_allowed: false
  }};
  const blob = new Blob([JSON.stringify(data, null, 2)], {{type:"application/json"}});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = "blind-review-rating.json"; a.click();
  URL.revokeObjectURL(url);
}});
</script></body></html>"""


def _prepare(kwargs: dict) -> dict:
    evidence = run_editorial_evidence(**kwargs)
    if evidence.get("status") != "LOCAL_EDITORIAL_EVIDENCE_UNRATED_AUDIT_REQUIRED":
        return {"version": VERSION, "status": evidence["status"],
                "ready": False, "production_ready": False}
    state = _basis({key: value for key, value in kwargs.items()
                    if key != "target_index"}, kwargs["target_index"])
    if (state["status"] != "SUPPORTED"
            or state["basis"]["context"]["job_id"] != evidence["job_id"]):
        raise ValueError("BLIND_STUDY_LINEAGE_CHANGED")
    source = _no_links(state["receipt"])
    root = _no_links(source.with_name(f"{source.stem}.blind-study"))
    private = _no_links(source.with_name(f"{source.stem}.blind-map.json"))
    roles = state["basis"]["context"]["roles"]
    for role in _ROLES:
        item = roles[role]
        file = _no_links(Path(item["final"]))
        if (file.suffix.lower() != ".mp4" or not file.is_file()
                or file.is_symlink() or not 0 < file.stat().st_size <= _MAX_MEDIA_BYTES
                or _hash(file)[0] != item["final_sha256"]):
            raise ValueError("BLIND_STUDY_FINISHED_MEDIA_CHANGED")
    if roles["b2"]["final_sha256"] == roles["b3"]["final_sha256"]:
        return {"version": VERSION, "status": "BLIND_STUDY_VARIANTS_IDENTICAL",
                "ready": False, "production_ready": False}
    # Stable cryptographic assignment, not a user-selectable best edit.
    key = (evidence["job_id"] + "|" + evidence["b2_id"] + "|" +
           evidence["b3_id"] + "|" + str(kwargs["target_index"]) + "|" +
           roles["b2"]["final_sha256"] + "|" + roles["b3"]["final_sha256"])
    study_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    order = ("b3", "b2") if int(study_id[0], 16) % 2 else ("b2", "b3")
    result = {
        "version": VERSION, "status": "BLIND_STUDY_READ_ONLY_PLAN",
        "study_id": study_id,
        "comparison_candidates": ["candidate-1", "candidate-2"],
        "full_finished_media_not_short_excerpt": True,
        "one_shot_rating_form_available_after_execution": True,
        "evaluator_role_mapping_disclosed": False,
        "existing_videos_modified": False, "reencoded_videos": False,
        "additional_downloads": False, "new_provider_requests": False,
        "human_labels_obtained": False,
        "independent_human_gold_verified": False,
        "automatic_winner": None, "automatic_adoption_allowed": False,
        "publication_performed": False, "ready": False, "production_ready": False,
    }
    return {"status": "SUPPORTED", "report": result, "evidence": evidence,
            "state": state, "root": root, "private": private, "order": order,
            "paths": {role: _no_links(Path(roles[role]["final"])) for role in _ROLES}}


def run_blind_full_study(
    *, queue: Path = DEFAULT_QUEUE, example_store: Path = DEFAULT_HUMAN_STORE,
    growth_state: Path = DEFAULT_GROWTH_STATE,
    preview_root: Path = DEFAULT_PREVIEW_ROOT,
    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT,
    job_id: str | None = None, proposal_index: int = 0,
    refinement_index: int = 0, candidate_index: int = 0,
    target_index: int = 0, execute: bool = False,
    approve_local_preview: bool = False,
) -> dict:
    """Default read-only; opt-in creates neutral full-MP4 hardlinks and local HTML."""
    if (type(execute) is not bool or type(approve_local_preview) is not bool
            or type(target_index) is not int or not 0 <= target_index < 20):
        raise ValueError("BLIND_STUDY_ARGUMENT_INVALID")
    if execute and not approve_local_preview:
        raise ValueError("BLIND_STUDY_EXPLICIT_APPROVAL_REQUIRED")
    params = dict(
        queue=queue, example_store=example_store, growth_state=growth_state,
        preview_root=preview_root, transcript_root=transcript_root,
        job_id=job_id, proposal_index=proposal_index,
        refinement_index=refinement_index, candidate_index=candidate_index,
        target_index=target_index,
    )
    initial = _prepare(params)
    if initial["status"] != "SUPPORTED":
        return initial
    if not execute:
        return initial["report"]
    with _exclusive_run_lock(
        _no_links(Path(preview_root)), initial["evidence"]["job_id"],
    ):
        current = _prepare(params)
        if current["status"] != "SUPPORTED" or current["report"] != initial["report"]:
            raise ValueError("BLIND_STUDY_EVIDENCE_CHANGED")
        target, private = current["root"], current["private"]
        if target.exists() or target.is_symlink() or private.exists() or private.is_symlink():
            raise ValueError("BLIND_STUDY_OUTPUT_ALREADY_EXISTS")
        final_hashes = {
            role: _hash(current["paths"][role])
            for role in _ROLES
        }
        # A clean new directory, owned exclusively by this invocation.
        # Interrupted partial folders intentionally block reruns (fail closed).
        os.mkdir(target)
        mapping = {}
        for i, role in enumerate(current["order"], start=1):
            slot = f"candidate-{i}"
            destination = _no_links(target / f"{slot}.mp4")
            os.link(current["paths"][role], destination)
            if (_hash(destination) != final_hashes[role]
                    or _hash(current["paths"][role]) != final_hashes[role]):
                raise ValueError("BLIND_STUDY_INPUT_CHANGED_DURING_LINK")
            mapping[slot] = {
                "role": role, "filename": destination.name,
                "sha256": final_hashes[role][0],
                "size_bytes": final_hashes[role][1],
            }
        # Reviewer folder must not contain the B2/B3 identity mapping.
        html_file = _no_links(target / "review.html")
        with html_file.open("x", encoding="utf-8") as fp:
            fp.write(_study_html(current["report"]["study_id"]))
        reviewer_report = dict(current["report"])
        reviewer_report["status"] = "BLIND_STUDY_FULL_MEDIA_READY_UNRATED"
        save_semantic_review(reviewer_report, _no_links(target / "study.json"))
        again = _prepare(params)
        if again["status"] != "SUPPORTED" or again["report"] != initial["report"]:
            raise ValueError("BLIND_STUDY_EVIDENCE_CHANGED_BEFORE_MAP")
        for role in _ROLES:
            if _hash(current["paths"][role]) != final_hashes[role]:
                raise ValueError("BLIND_STUDY_ORIGINAL_CHANGED_BEFORE_MAP")
        private_map = {
            "version": VERSION, "status": "BLIND_STUDY_PRIVATE_MAPPING_UNRATED",
            "study_id": reviewer_report["study_id"],
            "job_id": current["evidence"]["job_id"],
            "b2_id": current["evidence"]["b2_id"],
            "b3_id": current["evidence"]["b3_id"],
            "playback_pack_receipt_sha256":
                current["evidence"]["playback_pack_receipt_sha256"],
            "role_mapping": mapping,
            "human_labels_obtained": False,
            "automatic_adoption_allowed": False,
            "publication_performed": False,
            "ready": False, "production_ready": False,
        }
        save_semantic_review(private_map, private)
        return reviewer_report
