from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None else int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None else float(raw)


@dataclass(frozen=True, slots=True)
class NimSettings:
    api_key: str
    base_url: str = "https://integrate.api.nvidia.com/v1"
    model: str = "z-ai/glm-5.3"
    fast_model: str = "z-ai/glm-5.3-flash"
    max_concurrency: int = 2
    rpm: int = 20
    max_retries: int = 6
    timeout_seconds: float = 180.0

    @classmethod
    def from_env(cls) -> "NimSettings":
        return cls(
            api_key=os.getenv("NVIDIA_API_KEY", ""),
            base_url=os.getenv(
                "NVIDIA_NIM_BASE_URL", "https://integrate.api.nvidia.com/v1"
            ).rstrip("/"),
            model=os.getenv("NVIDIA_NIM_MODEL", "z-ai/glm-5.3"),
            fast_model=os.getenv("NVIDIA_NIM_FAST_MODEL", "z-ai/glm-5.3-flash"),
            max_concurrency=_env_int("NVIDIA_NIM_MAX_CONCURRENCY", 2),
            rpm=_env_int("NVIDIA_NIM_RPM", 20),
            max_retries=_env_int("NVIDIA_NIM_MAX_RETRIES", 6),
            timeout_seconds=_env_float("NVIDIA_NIM_TIMEOUT_SECONDS", 180.0),
        )

    def validate(self) -> None:
        if not self.api_key:
            raise ValueError("NVIDIA_API_KEY is not set")
        if self.max_concurrency < 1:
            raise ValueError("NVIDIA_NIM_MAX_CONCURRENCY must be >= 1")
        if self.rpm < 1:
            raise ValueError("NVIDIA_NIM_RPM must be >= 1")
