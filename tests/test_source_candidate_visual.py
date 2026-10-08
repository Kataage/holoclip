"""Automatic bounded video probes remain unreviewed despite pixel matches."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from holoclip.corpus_growth import CorpusGrowthItem, CorpusGrowthState
from holoclip.providers.source.base import SourceMetadata
from holoclip.source_candidate_visual import (
    VERSION, _NO_GOLD, compare_sparse_visual_sequences,
    verify_pending_source_visual,
)


CLIP = "https://www.youtube.com/watch?v=AAAAAAAAAAA"
A = "https://www.youtube.com/watch?v=BBBBBBBBBBB"
B = "https://www.youtube.com/watch?v=CCCCCCCCCCC"


def meta(url, *, public=True, duration=None):
    ident = url.split("=")[-1]
    return SourceMetadata(
        provider="fixture", url=url, external_id=ident, title="public archive",
        channel="hololive", channel_id=ident,
        duration=duration if duration is not None else (40. if url == CLIP else 3600.),
        availability="public" if public else "subscriber_only",
        live_status="was_live",
    )


def frame(prefix, i, *, pt=None, hash_override=None):
    return SimpleNamespace(
        id=f"{prefix}-{i}", locator=f"{prefix}-{i}",
        pts_seconds=float(2*i if pt is None else pt),
        checksum=hash_override or f"{prefix}-image-{i}",
    )


def measured(prefix):
    return SimpleNamespace(frames=[frame(prefix, i) for i in range(5)],
                           source_start=0.)


def compare(src, dst):
    # The content is the same across encoders, but the file checksums differ.
    score = .87 if src.split("-")[-1] == dst.split("-")[-1] else 0.
    return {"score": score} if score else None


def run_images(finished=None, originals=None):
    return compare_sparse_visual_sequences(
        finished or measured("clip"),
        originals if originals is not None else [measured("source")],
        extract=lambda locator: locator, compare=compare,
    )


def test_visual_sequence_requires_three_independent_advancing_frame_matches():
    found = run_images()
    assert found["status"] == "VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD"
    assert found["matched_frames"] == 3
    assert found["matched_source_window"] == 0
    assert found["examined_frame_comparisons"] == 25
    for flag in _NO_GOLD:
        assert found[flag] is False


def test_repeated_video_content_in_multiple_source_ranges_is_unknown():
    original = measured("source")
    alias = measured("repeat")
    result = run_images(originals=[original, alias])
    # Scores are identical in two disjoint source ranges: no unique source
    # range can be established, even with compelling single-frame matches.
    assert result["matched_frames"] == 0
    assert result["status"] == "UNKNOWN_NO_VISUAL_SEQUENCE"


def test_static_finished_and_source_do_not_form_temporal_correspondence():
    same = measured("clip")
    for f in same.frames:
        f.checksum = "same-output-still"
    result = run_images(finished=same)
    assert result["status"] == "UNKNOWN_NO_VISUAL_SEQUENCE"
    assert result["matched_frames"] == 0


def test_sparse_and_budgeted_image_search_cannot_succeed_by_truncation():
    result = run_images(
        finished=SimpleNamespace(frames=[frame("clip", 0), frame("clip", 1)]),
    )
    assert result["status"] == "UNKNOWN_NO_VISUAL_SEQUENCE"
    huge = SimpleNamespace(frames=[frame("clip", n) for n in range(20)])
    sources = [SimpleNamespace(frames=[frame(f"source{k}", n)
                                      for n in range(30)]) for k in range(8)]
    result = run_images(finished=huge, originals=sources)
    assert result["status"] in {
        "UNKNOWN_VISUAL_COMPARISON_BUDGET", "UNKNOWN_NO_VISUAL_SEQUENCE",
    }
    assert result["source_identity_verified"] is False


def make_state(path: Path, *, candidates=None, status="source_visual_pending"):
    rows = candidates if candidates is not None else [A]
    scout = {
        "version": "unresolved-public-source-shortlist-v1",
        "status": "UNVERIFIED_PUBLIC_SOURCE_SHORTLIST",
        "candidate_sources": [{
            "source_url": url, "source_stream_id": url.split("=")[-1],
            "duration_seconds": 3600.,
            "source_identity_verified": False,
            "visual_correspondence": "NOT_MEASURED",
        } for url in rows],
    }
    path.write_text(CorpusGrowthState(items={
        "candidate": CorpusGrowthItem(
            clip_url=CLIP, query="test", status=status, source_scout=scout,
        )
    }).model_dump_json(), encoding="utf-8")


class Provider:
    def __init__(self, *, second_public=True):
        self.calls = []
        self.second_public = second_public

    def probe(self, url):
        if url not in (CLIP, A, B):
            raise AssertionError("Unexpected original video")
        return meta(url, public=(url != B or self.second_public))

    def acquire_video_range(self, url, root, *, start, end):
        self.calls.append((url, start, end))
        assert 5 <= end-start <= 12.001
        path = root / url.split("=")[-1] / f"{start:.3f}.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"video data {url} {start} {end}".encode())
        return SimpleNamespace(
            media_path=path, metadata=self.probe(url),
            requested_start=start, requested_end=end,
        )


def fake_probe(_):
    return {"streams": [{"codec_type": "video"}],
            "format": {"duration": "12"}}


def fake_measure(media, root, *, asset_id, start, end, transcript, dense_times):
    assert start == 0
    assert end <= 14
    return measured("clip" if asset_id.endswith("finished") else asset_id)


def verify(provider, tmp_path, **kwargs):
    return verify_pending_source_visual(
        provider, store=tmp_path/"unused.jsonl",
        state_path=tmp_path/"state.json",
        output=tmp_path/"sampled",
        policy_rules=[], measure=fake_measure, probe=fake_probe,
        **kwargs,
    )


def test_pending_shortlist_automatically_probes_actual_bounded_video_and_persists(tmp_path):
    make_state(tmp_path/"state.json")
    provider = Provider()
    def measured_good(finished, source_windows):
        assert len(source_windows) == 3
        return {
            "status": "VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD",
            "matched_frames": 3, **_NO_GOLD,
        }
    result = verify(provider, tmp_path, compare_sequences=measured_good)
    assert result["version"] == VERSION
    assert result["counts"] == {"UNREVIEWED_VISUAL_CANDIDATE_CORROBORATION": 1}
    assert len(provider.calls) == 4
    assert provider.calls[0] == (CLIP, 14., 26.)
    assert provider.calls[1:] == [
        (A, 0., 12.), (A, 1794., 1806.), (A, 3588., 3600.)
    ]
    state = CorpusGrowthState.model_validate_json(
        (tmp_path/"state.json").read_text(encoding="utf-8")
    )
    item = state.items["candidate"]
    assert item.status == "source_visual_pending"
    assert item.source_url is None and item.source_stream_id is None
    probe = item.source_scout["visual_probe"]
    assert probe["status"] == "UNREVIEWED_VISUAL_CANDIDATE_CORROBORATION"
    assert probe["candidate_observations"][0][
        "visual_correspondence"
    ] == "VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD"
    assert all(len(x["media_sha256"]) == 64 for x in probe[
        "candidate_observations"
    ][0]["sampled_source_windows"])
    for flag in _NO_GOLD:
        assert probe[flag] is False


def test_failed_source_policy_blocks_download_and_cannot_turn_no_match_into_gold(tmp_path):
    make_state(tmp_path/"state.json", candidates=[A, B])
    provider = Provider(second_public=False)
    results = verify(provider, tmp_path)
    assert results["attempted"] == 1
    # The first candidate's sample may be non-matching; private second
    # candidate must not be acquired.
    assert all(x[0] != B for x in provider.calls)
    state = CorpusGrowthState.model_validate_json(
        (tmp_path/"state.json").read_text(encoding="utf-8")
    )
    record = state.items["candidate"].source_scout["visual_probe"]
    assert record["candidate_observations"][1]["status"] == (
        "BLOCKED_SOURCE_IDENTITY_OR_POLICY"
    )
    assert record["status"] == "UNKNOWN_NO_VISUAL_MATCH_IN_BOUNDED_SAMPLES"
    assert record["source_identity_verified"] is False


def test_ambiguous_two_visual_candidates_are_not_automatically_selected(tmp_path):
    make_state(tmp_path/"state.json", candidates=[A, B])
    def positive(*_):
        return {"status": "VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD",
                **_NO_GOLD}
    result = verify(
        Provider(), tmp_path, compare_sequences=positive,
    )
    assert result["counts"] == {
        "UNKNOWN_AMBIGUOUS_MULTIPLE_SOURCE_CANDIDATES": 1
    }
    state = CorpusGrowthState.model_validate_json(
        (tmp_path/"state.json").read_text(encoding="utf-8")
    )
    assert state.items["candidate"].source_stream_id is None


@pytest.mark.parametrize("tamper", ["stale_version", "claimed_source"])
def test_forged_or_stale_shortlist_cannot_download(tmp_path, tamper):
    make_state(tmp_path/"state.json")
    raw = json.loads((tmp_path/"state.json").read_text(encoding="utf-8"))
    row = raw["items"]["candidate"]
    if tamper == "stale_version":
        row["source_scout"]["version"] = "obsolete"
    else:
        row["source_stream_id"] = "BBBBBBBBBBB"
    (tmp_path/"state.json").write_text(json.dumps(raw), encoding="utf-8")
    provider = Provider()
    result = verify(provider, tmp_path)
    assert result["counts"] == {"BLOCKED_UNVERIFIED_SOURCE_QUEUE": 1}
    assert provider.calls == []


def test_nonpending_or_missing_queue_skips_without_network(tmp_path):
    make_state(tmp_path/"state.json", status="review")
    provider = Provider()
    assert verify(provider, tmp_path)["attempted"] == 0
    assert provider.calls == []
