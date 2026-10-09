"""Offline local-byte cache validation never confers source/editor gold."""
import hashlib
import json

import pytest

from holoclip.corpus_growth import CorpusGrowthState
from holoclip.source_video_cache_audit import (
    audit_cached_source_video_sections, MAX_CLIPS,
)
from test_source_candidate_visual import A, make_state


def _arrange(tmp_path):
    state_path = tmp_path / "state.json"
    root = tmp_path / "video"
    make_state(state_path)
    state = CorpusGrowthState.model_validate_json(
        state_path.read_text(encoding="utf-8"),
    )
    entry = state.items["candidate"]
    start, end = 0., 12.
    payload = b"bounded-source-test-only-media-bytes" * 100
    digest = hashlib.sha256(payload).hexdigest()
    source_id = A.split("=")[-1]
    entry.source_scout["visual_probe"] = {
        "status": "UNKNOWN_NO_VISUAL_MATCH_IN_BOUNDED_SAMPLES",
        "candidate_observations": [{
            "source_stream_id": source_id,
            "status": "UNKNOWN_NO_VISUAL_SEQUENCE",
            "sampled_source_windows": [{
                "requested_range": [start, end],
                "media_sha256": digest,
                "source_native_pts_verified": False,
            }],
        }],
        "finished_video_sha256": "e" * 64,
    }
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    identity = hashlib.sha256(
        f"yt-section-v1:{source_id}:{start:.3f}:{end:.3f}".encode()
    ).hexdigest()[:24]
    folder = (root / "original" / source_id /
              "editing_video_sections" / identity)
    folder.mkdir(parents=True)
    media = folder / "media.mkv"
    media.write_bytes(payload)
    receipt = {
        "version": "yt-range-cache-v1",
        "source_id": source_id,
        "source_url": A,
        "requested_start": start,
        "requested_end": end,
        "filename": media.name,
        "sha256": digest,
    }
    (folder / "section-integrity.json").write_text(
        json.dumps(receipt), encoding="utf-8",
    )
    return state_path, root, media, receipt


def _audit(state_path, root):
    return audit_cached_source_video_sections(
        state_path=state_path, output_root=root,
    )


def test_local_cache_bytes_and_receipt_can_be_confirmed_but_not_promoted(tmp_path):
    state_path, root, _, _ = _arrange(tmp_path)
    result = _audit(state_path, root)
    assert result["status"] == "VERIFIED_ONLY_LOCAL_SOURCE_SECTION_BYTES_NOT_GOLD"
    assert result["cache_section_status_counts"] == {
        "VERIFIED_LOCAL_SOURCE_SECTION_BYTES_AND_RECEIPT_NOT_GOLD": 1,
    }
    assert result["deduplicated_section_records_checked"] == 1
    assert result["complete_pending_queue_audited"] is True
    assert result["actual_public_video_verified"] is False
    assert result["source_identity_verified"] is False
    assert result["ready"] is False


@pytest.mark.parametrize("damage,expected", [
    ("remove_receipt", "UNKNOWN_SOURCE_CACHE_MISSING"),
    ("truncate_media", "BLOCKED_CACHE_MEDIA_BYTES_MISMATCH"),
    ("receipt_wrong_source", "BLOCKED_CACHE_IDENTITY_OR_RANGE_MISMATCH"),
    ("receipt_wrong_sha", "BLOCKED_CACHE_SHA_OR_FILENAME_MISMATCH"),
    ("traversal_filename", "BLOCKED_CACHE_SHA_OR_FILENAME_MISMATCH"),
])
def test_cache_damage_and_receipt_tampering_is_not_verified(tmp_path, damage, expected):
    state_path, root, media, receipt = _arrange(tmp_path)
    receipt_path = media.parent / "section-integrity.json"
    if damage == "remove_receipt":
        receipt_path.unlink()
    elif damage == "truncate_media":
        media.write_bytes(b"tampered")
    else:
        if damage == "receipt_wrong_source":
            receipt["source_id"] = "other"
        elif damage == "receipt_wrong_sha":
            receipt["sha256"] = "1" * 64
        else:
            receipt["filename"] = "../media.mkv"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    result = _audit(state_path, root)
    assert result["status"] == "UNKNOWN_SOURCE_CACHE_GAPS_OR_INTEGRITY_FAILURES"
    assert result["cache_section_status_counts"] == {expected: 1}
    assert result["ready"] is False


def test_symlink_to_external_media_is_never_hashed(tmp_path):
    state_path, root, media, _ = _arrange(tmp_path)
    elsewhere = tmp_path / "different-video"
    elsewhere.write_bytes(media.read_bytes())
    media.unlink()
    try:
        media.symlink_to(elsewhere)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this host")
    result = _audit(state_path, root)
    assert result["cache_section_status_counts"] == {
        "BLOCKED_UNSAFE_CACHE_LINK": 1,
    }
    assert result["source_identity_verified"] is False


def test_missing_state_and_bounded_partial_audits_are_explicit(tmp_path):
    result = _audit(tmp_path / "missing.json", tmp_path / "video")
    assert result["status"] == "UNKNOWN_NO_SOURCE_CACHE_EVIDENCE"
    assert result["complete_pending_queue_audited"] is False
    path, root, _, _ = _arrange(tmp_path)
    state = CorpusGrowthState.model_validate_json(path.read_text(encoding="utf-8"))
    template = state.items["candidate"]
    for i in range(MAX_CLIPS):
        copy = template.model_copy(deep=True)
        copy.clip_url = f"https://www.youtube.com/watch?v={i:011d}"
        copy.source_scout["visual_probe"] = None
        state.items[f"other-{i}"] = copy
    path.write_text(state.model_dump_json(), encoding="utf-8")
    result = _audit(path, root)
    assert result["total_pending_clips"] == MAX_CLIPS + 1
    assert result["inspected_clips"] == MAX_CLIPS
    assert result["complete_pending_queue_audited"] is False
    assert result["status"] == "UNKNOWN_PARTIAL_BOUNDED_CACHE_AUDIT"
    assert result["ready"] is False


def test_bad_source_range_record_fails_closed(tmp_path):
    path, root, _, _ = _arrange(tmp_path)
    state = CorpusGrowthState.model_validate_json(path.read_text(encoding="utf-8"))
    observation = state.items["candidate"].source_scout[
        "visual_probe"
    ]["candidate_observations"][0]
    observation["sampled_source_windows"][0]["requested_range"] = [8., 1.]
    path.write_text(state.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="range"):
        _audit(path, root)



def test_exact_current_run_clip_selection_avoids_sorted_backlog_starvation(tmp_path):
    path, root, _, _ = _arrange(tmp_path)
    state = CorpusGrowthState.model_validate_json(path.read_text(encoding="utf-8"))
    template = state.items["candidate"]
    second = template.model_copy(deep=True)
    second.clip_url = "https://www.youtube.com/watch?v=ZZZZZZZZZZZ"
    second.source_scout["visual_probe"] = None
    state.items["other"] = second
    path.write_text(state.model_dump_json(), encoding="utf-8")

    latest = audit_cached_source_video_sections(
        state_path=path, output_root=root, clip_keys=["other"],
    )
    assert latest["clip_selection"] == "CURRENT_RUN_ATTEMPTED_CLIPS"
    assert latest["inspected_clips"] == 1
    assert latest["total_pending_clips"] == 2
    assert latest["deduplicated_section_records_checked"] == 0
    assert latest["complete_pending_queue_audited"] is False
    assert latest["ready"] is False
    with pytest.raises(ValueError, match="Attempted"):
        audit_cached_source_video_sections(
            state_path=path, output_root=root, clip_keys=["not-pending"],
        )
    with pytest.raises(ValueError, match="audit keys"):
        audit_cached_source_video_sections(
            state_path=path, output_root=root, clip_keys=["other", "other"],
        )

def test_audit_budget_cannot_be_changed_unbounded(tmp_path):
    with pytest.raises(ValueError, match="clip limit"):
        audit_cached_source_video_sections(
            state_path=tmp_path/"none", output_root=tmp_path, max_clips=999,
        )
