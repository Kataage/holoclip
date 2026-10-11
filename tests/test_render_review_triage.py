"""Adversarial offline proof that triage never creates executable repair actions."""
from __future__ import annotations

import hashlib
import json

import pytest

from holoclip import render_review_triage as triage


def _case(tmp_path):
    name = "renderqueue_" + "c" * 32
    base = tmp_path / f"{name}.mp4"
    base.write_bytes(b"verified preview, never edited")
    digest = hashlib.sha256(base.read_bytes()).hexdigest()
    candidates = [
        {"kind": "blackout", "start_seconds": 1.0, "end_seconds": 2.5,
         "segment_ids": ["a"], "position": "inside_cut",
         "auto_repair_allowed": False, "human_editorial_qa_completed": False},
        {"kind": "freeze", "start_seconds": 3.0, "end_seconds": None,
         "segment_ids": ["b"], "position": "inside_cut",
         "auto_repair_allowed": False, "human_editorial_qa_completed": False},
        {"kind": "wide_subtitle", "start_seconds": 0.4, "end_seconds": 1.0,
         "cue_number": 1, "segment_ids": ["a"], "position": "inside_cut",
         "auto_repair_allowed": False, "human_editorial_qa_completed": False},
        {"kind": "audio_peak", "start_seconds": 0, "end_seconds": 5,
         "segment_ids": ["a", "b"], "position": "cut_boundary",
         "auto_repair_allowed": False, "human_editorial_qa_completed": False},
    ]
    review = {
        "version": triage.REVIEW_VERSION,
        "base_sha256": digest, "final_sha256": digest,
        "candidate_count": len(candidates), "candidates": candidates,
        "ready": False, "production_ready": False,
        "human_editorial_qa_completed": False, "publication_performed": False,
    }
    review_path = base.with_name(f"{name}.review.json")
    review_path.write_text(json.dumps(review), encoding="utf-8")
    review_sha = hashlib.sha256(review_path.read_bytes()).hexdigest()
    source = {
        "version": triage.SOURCE_CONTEXT_VERSION,
        "review_sha256": review_sha,
        "base_sha256_from_review": digest,
        "sampled_event_count": 2,
        "source_bytes_independently_rehashed": False,
        "comparisons": [
            {"observation_index": 0, "kind": "blackout",
             "start_seconds": 1.0, "segment_id": "a",
             "status": "NO_SIMILAR_SIGNAL_IN_SAMPLED_SOURCE_WINDOW",
             "auto_repair_allowed": False,
             "human_editorial_qa_completed": False},
            {"observation_index": 1, "kind": "freeze",
             "start_seconds": 3.0, "segment_id": "b",
             "status": "SIMILAR_SIGNAL_OBSERVED_IN_SOURCE_WINDOW",
             "auto_repair_allowed": False,
             "human_editorial_qa_completed": False},
        ],
        "ready": False, "production_ready": False,
        "auto_repair_allowed": False,
        "human_editorial_qa_completed": False, "publication_performed": False,
    }
    context_path = base.with_name(f"{name}.source-context.json")
    context_path.write_text(json.dumps(source), encoding="utf-8")
    context_sha = hashlib.sha256(context_path.read_bytes()).hexdigest()
    return base, review_path, review_sha, context_path, context_sha


def test_triage_sorts_priority_with_verified_context_without_invented_quality(tmp_path):
    base, review, rsha, context, csha = _case(tmp_path)
    original = base.read_bytes()
    output = triage.store_local_review_triage(
        mp4_output=base, review_sha256=rsha,
        source_context_sha256=csha,
    )
    assert output["status"] == "LOCAL_EDIT_TRIAGE_CREATED_UNVERIFIED_EDITORIAL"
    assert output["candidate_count"] == 4
    assert output["review_first_count"] == 1
    target = base.with_name(f"{base.stem}.triage.json")
    blob = target.read_bytes()
    assert hashlib.sha256(blob).hexdigest() == output["sha256"]
    doc = json.loads(blob)
    assert [c["review_index"] for c in doc["candidate_queue"]] == [0, 3, 2, 1]
    assert doc["candidate_queue"][0]["inspection_bucket"] == "REVIEW_FIRST"
    assert doc["candidate_queue"][-1]["inspection_bucket"] == (
        "REVIEW_WITH_SOURCE_CONTEXT"
    )
    assert doc["candidate_queue"][-1]["source_context"] == "SOURCE_SIGNAL_ALSO_OBSERVED"
    assert doc["context_mapped_count"] == 2
    assert doc["repair_execution_performed"] is False
    assert doc["repair_preview_generated"] is False
    assert doc["media_sha256_independently_reverified"] is False
    assert not doc["editorial_quality_metrics_available"]
    assert doc["ready"] is False and doc["production_ready"] is False
    assert all(not x["auto_repair_allowed"] for x in doc["candidate_queue"])
    assert all(not x["repair_draft"]["operation_authorized"]
               for x in doc["candidate_queue"])
    assert not any("command" in x["repair_draft"] for x in doc["candidate_queue"])
    assert original == base.read_bytes()
    assert str(tmp_path) not in target.read_text()
    assert triage.store_local_review_triage(
        mp4_output=base, review_sha256=rsha, source_context_sha256=csha,
    )["status"] == "LOCAL_EDIT_TRIAGE_UNAVAILABLE"
    assert target.read_bytes() == blob


def test_review_without_context_keeps_all_candidates_needing_review(tmp_path):
    base, review, rsha, context, csha = _case(tmp_path)
    result = triage.store_local_review_triage(
        mp4_output=base, review_sha256=rsha,
    )
    assert result["candidate_count"] == 4
    assert result["review_first_count"] == 0
    doc = json.loads(base.with_name(f"{base.stem}.triage.json").read_text())
    assert all(x["source_context"] == "SOURCE_CONTEXT_NOT_ESTABLISHED"
               for x in doc["candidate_queue"])
    assert all(x["inspection_bucket"] == "REVIEW_NEEDED"
               for x in doc["candidate_queue"])
    assert doc["source_context_sha256"] is None


@pytest.mark.parametrize("alter", [
    "bad_review_hash", "bad_context_hash", "context_review_mismatch",
    "context_index_duplicate", "context_kind_mismatch", "context_auto_edit",
    "review_auto_edit", "review_gold_claim", "context_asset_confusion",
])
def test_bad_evidence_fails_closed_without_writing_repair(tmp_path, alter):
    base, review, rsha, context, csha = _case(tmp_path)
    kwargs = {"mp4_output": base, "review_sha256": rsha,
              "source_context_sha256": csha}
    if alter == "bad_review_hash":
        kwargs["review_sha256"] = "a" * 64
    elif alter == "bad_context_hash":
        kwargs["source_context_sha256"] = "f" * 64
    elif alter == "context_review_mismatch":
        doc = json.loads(context.read_text())
        doc["review_sha256"] = "a" * 64
        context.write_text(json.dumps(doc))
        kwargs["source_context_sha256"] = hashlib.sha256(context.read_bytes()).hexdigest()
    elif alter == "context_index_duplicate":
        doc = json.loads(context.read_text())
        doc["comparisons"][1]["observation_index"] = 0
        context.write_text(json.dumps(doc))
        kwargs["source_context_sha256"] = hashlib.sha256(context.read_bytes()).hexdigest()
    elif alter == "context_kind_mismatch":
        doc = json.loads(context.read_text())
        doc["comparisons"][0]["kind"] = "freeze"
        context.write_text(json.dumps(doc))
        kwargs["source_context_sha256"] = hashlib.sha256(context.read_bytes()).hexdigest()
    elif alter == "context_auto_edit":
        doc = json.loads(context.read_text())
        doc["comparisons"][1]["auto_repair_allowed"] = True
        context.write_text(json.dumps(doc))
        kwargs["source_context_sha256"] = hashlib.sha256(context.read_bytes()).hexdigest()
    elif alter == "review_auto_edit":
        doc = json.loads(review.read_text())
        doc["candidates"][0]["auto_repair_allowed"] = True
        review.write_text(json.dumps(doc))
        kwargs["review_sha256"] = hashlib.sha256(review.read_bytes()).hexdigest()
    elif alter == "review_gold_claim":
        doc = json.loads(review.read_text())
        doc["human_editorial_qa_completed"] = True
        review.write_text(json.dumps(doc))
        kwargs["review_sha256"] = hashlib.sha256(review.read_bytes()).hexdigest()
    elif alter == "context_asset_confusion":
        doc = json.loads(context.read_text())
        doc["comparisons"][1]["segment_id"] = "not-matched"
        context.write_text(json.dumps(doc))
        kwargs["source_context_sha256"] = hashlib.sha256(context.read_bytes()).hexdigest()
    result = triage.store_local_review_triage(**kwargs)
    assert result["status"] == "LOCAL_EDIT_TRIAGE_UNAVAILABLE"
    assert not base.with_name(f"{base.stem}.triage.json").exists()


def test_unavailable_source_context_is_not_negative_evidence(tmp_path):
    base, review, rsha, context, csha = _case(tmp_path)
    source = json.loads(context.read_text())
    source["comparisons"][0]["status"] = "SOURCE_WINDOW_UNAVAILABLE"
    context.write_text(json.dumps(source))
    sha = hashlib.sha256(context.read_bytes()).hexdigest()
    report = triage.store_local_review_triage(
        mp4_output=base, review_sha256=rsha, source_context_sha256=sha,
    )
    assert report["status"] == "LOCAL_EDIT_TRIAGE_CREATED_UNVERIFIED_EDITORIAL"
    doc = json.loads(base.with_name(f"{base.stem}.triage.json").read_text())
    first = next(x for x in doc["candidate_queue"] if x["review_index"] == 0)
    assert first["source_context"] == "INCONCLUSIVE_SOURCE_SAMPLE"
    assert first["inspection_bucket"] == "REVIEW_NEEDED"


def test_missing_review_or_invalid_hash_gives_no_output(tmp_path):
    base, review, rsha, context, csha = _case(tmp_path)
    review.unlink()
    result = triage.store_local_review_triage(
        mp4_output=base, review_sha256=rsha, source_context_sha256=csha,
    )
    assert result["status"] == "LOCAL_EDIT_TRIAGE_UNAVAILABLE"
    assert not base.with_name(f"{base.stem}.triage.json").exists()
