"""Guarded single-pass video style rules for local FFmpeg clip rendering.

No inferred face positions, creative suitability, LLM/ASR guesses or changes
to the ClipTimeline clock. The default remains a plain hard cut; only
high-confidence semantic markers permit a *small* center zoom or an explicitly
hinted dip-to-black between two different source videos.
"""
from __future__ import annotations

from dataclasses import dataclass

from holoclip.domain import ClipTimeline, TimelinePurpose

VISUAL_STYLE_VERSION = "holoclip-visual-style-v1"
_FADE_SECONDS = 0.08
_ZOOM_ROLES = frozenset({TimelinePurpose.REACTION, TimelinePurpose.PAYOFF})


@dataclass(frozen=True)
class CutStyle:
    """Finite presentation choices only; never accept filter strings as data."""
    zoom: bool = False
    fade_in: bool = False
    fade_out: bool = False


def select_cut_styles(timeline: ClipTimeline) -> list[CutStyle]:
    """Compile deterministic index-aligned video/audio styles with no IO.

    A dip-to-black is *opt-in* through a vetted transition_hint on the
    incoming cut. A hint on a lone clip or same-source edit has no effect.
    This prevents automatic fades from hiding spoken words or jokes.
    """
    timeline = ClipTimeline.model_validate(timeline.model_dump(mode="json"))
    cuts = sorted(timeline.segments, key=lambda segment: segment.output_order)
    zoom = [
        cut.purpose in _ZOOM_ROLES
        and cut.confidence >= 0.92
        and cut.source_end - cut.source_start >= 2.0
        for cut in cuts
    ]
    ins = [False] * len(cuts)
    outs = [False] * len(cuts)
    for index in range(1, len(cuts)):
        prior, current = cuts[index - 1], cuts[index]
        if (
            current.transition_hint == "dip_to_black"
            and current.confidence >= 0.95
            and prior.confidence >= 0.95
            and current.source_asset_id != prior.source_asset_id
            and current.source_end - current.source_start >= 1.5
            and prior.source_end - prior.source_start >= 1.5
        ):
            outs[index - 1] = True
            ins[index] = True
    return [
        CutStyle(zoom=zoom[i], fade_in=ins[i], fade_out=outs[i])
        for i in range(len(cuts))
    ]


def video_suffix(style: CutStyle, duration: float) -> str:
    """FFmpeg filter fragment applied after 30fps 1280x720 aspect-safe pad."""
    filters = []
    if style.zoom:
        # Small (<=3.5%) slow center zoom; cannot guarantee a subject-safe crop.
        # d=1 preserves one output frame per normalized input frame.
        filters.append(
            "zoompan=z='min(zoom+0.00018,1.035)':"
            "x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':"
            "d=1:s=1280x720:fps=30"
        )
    if style.fade_in:
        filters.append("fade=t=in:st=0:d=0.08")
    if style.fade_out:
        filters.append(f"fade=t=out:st={duration - _FADE_SECONDS:.6f}:d=0.08")
    if not filters:
        return ""
    return "," + ",".join(filters)


def audio_suffix(style: CutStyle, duration: float) -> str:
    """Sample-synchronous short transition without shifting speech clocks."""
    filters = []
    if style.fade_in:
        filters.append("afade=t=in:st=0:d=0.08")
    if style.fade_out:
        filters.append(f"afade=t=out:st={duration - _FADE_SECONDS:.6f}:d=0.08")
    return "," + ",".join(filters) if filters else ""


def style_evidence(timeline: ClipTimeline) -> dict[str, object]:
    """Metadata alone is NOT proof FFmpeg rendered or editorially improved."""
    styles = select_cut_styles(timeline)
    return {
        "version": VISUAL_STYLE_VERSION,
        "zoom_cut_count": sum(style.zoom for style in styles),
        "dip_transition_count": sum(style.fade_in for style in styles),
        "total_cut_count": len(styles),
    }
