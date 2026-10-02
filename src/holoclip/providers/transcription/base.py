from __future__ import annotations

from pathlib import Path
from typing import Protocol

from holoclip.models import TranscriptSegment


class TranscriptionProvider(Protocol):
    async def transcribe_file(
        self,
        path: Path,
        *,
        offset_seconds: float = 0.0,
    ) -> list[TranscriptSegment]: ...
