"""AI Provider metadata — static characteristics + runtime benchmark data.

Each AI image provider exposes its capabilities via an ``AIProviderMetadata``
instance. The orchestrator reads these to decide which provider to use per scene.

Static fields are filled by the provider at init. Runtime fields are populated
by the benchmark script (``scripts/test_ai_providers.py``) after live testing.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class AIProviderMetadata:
    """Static characteristics and runtime performance data for an AI image provider.

    Static fields (filled at init):
        provider, display_name, auth_required, model, default_resolution,
        max_resolution, avg_latency_seconds, rate_limit_per_minute,
        rate_limit_per_day, quality_score, cost_per_image, supports_seed,
        supports_negative_prompt, uses_local_resources, ram_usage_mb,
        cpu_cores_used, disk_model_gb

    Runtime fields (filled by benchmark):
        actual_latency_samples, actual_success_rate, last_benchmark
    """

    # ── Identity ──────────────────────────────────────────
    provider: str                    # "pollinations", "local_sd", etc.
    display_name: str                # "Pollinations.ai (Flux)"

    # ── Auth & model ──────────────────────────────────────
    auth_required: bool
    model: str                       # "flux", "stable-diffusion-v1-5", etc.

    # ── Resolution ────────────────────────────────────────
    default_resolution: tuple[int, int]   # (1280, 720)
    max_resolution: tuple[int, int]       # (1920, 1080)

    # ── Performance estimates (pre-benchmark) ─────────────
    avg_latency_seconds: float            # estimated before benchmark
    rate_limit_per_minute: Optional[int]  # None = unlimited
    rate_limit_per_day: Optional[int]     # None = unlimited

    # ── Quality & cost ────────────────────────────────────
    quality_score: float                  # 1-10 subjective
    cost_per_image: float                 # 0.0 = free

    # ── Capabilities ──────────────────────────────────────
    supports_seed: bool
    supports_negative_prompt: bool
    uses_local_resources: bool

    # ── Local resource usage (0 if cloud) ─────────────────
    ram_usage_mb: int                     # RAM per worker
    cpu_cores_used: int                   # CPU cores per worker
    disk_model_gb: float                  # Model download size

    # ── Runtime metrics (filled by benchmark) ─────────────
    actual_latency_samples: list[float] = field(default_factory=list)
    actual_success_rate: float = 1.0
    last_benchmark: str = ""              # ISO timestamp

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict."""
        return {
            "provider": self.provider,
            "display_name": self.display_name,
            "auth_required": self.auth_required,
            "model": self.model,
            "default_resolution": list(self.default_resolution),
            "max_resolution": list(self.max_resolution),
            "avg_latency_seconds": self.avg_latency_seconds,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "rate_limit_per_day": self.rate_limit_per_day,
            "quality_score": self.quality_score,
            "cost_per_image": self.cost_per_image,
            "supports_seed": self.supports_seed,
            "supports_negative_prompt": self.supports_negative_prompt,
            "uses_local_resources": self.uses_local_resources,
            "ram_usage_mb": self.ram_usage_mb,
            "cpu_cores_used": self.cpu_cores_used,
            "disk_model_gb": self.disk_model_gb,
            "actual_latency_samples": self.actual_latency_samples,
            "actual_success_rate": self.actual_success_rate,
            "last_benchmark": self.last_benchmark,
        }

    def save(self, path: Path) -> None:
        """Persist metadata as JSON (for benchmark results)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False))

    @classmethod
    def load(cls, path: Path) -> Optional["AIProviderMetadata"]:
        """Load metadata from a JSON file. Returns None on error."""
        try:
            data = json.loads(path.read_text())
            data["default_resolution"] = tuple(data["default_resolution"])
            data["max_resolution"] = tuple(data["max_resolution"])
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except Exception:
            return None
