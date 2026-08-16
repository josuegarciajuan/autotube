"""Test and benchmark all AI image providers.

Generates a sample image with each configured provider and records:
  - Success/failure status
  - Actual latency (wall-clock time)
  - Output file size
  - Any errors encountered

Usage::

    python3 scripts/test_ai_providers.py               # test all providers
    python3 scripts/test_ai_providers.py --provider pollinations  # test single
    python3 scripts/test_ai_providers.py --prompt "a red apple on a table"  # custom prompt
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Add project root to path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.ai_provider_metadata import AIProviderMetadata
from pipeline.providers.pollinations_provider import PollinationsProvider
from pipeline.providers.local_sd_provider import LocalSDProvider


TEST_PROMPT = (
    "cinematic landscape, mountains at golden hour sunset, "
    "dramatic lighting, photorealistic, 16:9, no text no watermark, "
    "professional documentary photography"
)
TEST_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output" / "ai_scenes" / "test"
BENCHMARK_DIR = Path(__file__).resolve().parent.parent / "output" / "ai_scenes" / "benchmarks"

# Providers to test (add more as they are implemented)
ALL_PROVIDERS = {
    "pollinations": lambda: PollinationsProvider(
        cache_dir=str(Path(__file__).resolve().parent.parent / "output" / "ai_scenes" / "cache"),
    ),
    "local_sd": lambda: LocalSDProvider(
        num_inference_steps=20,
    ),
}


def test_provider(
    provider_name: str,
    prompt: str = TEST_PROMPT,
    seed: int = 42,
) -> dict:
    """Test a single provider and return a result dict."""
    factory = ALL_PROVIDERS.get(provider_name)
    if factory is None:
        return {"provider": provider_name, "status": "SKIP", "error": "Unknown provider"}

    print(f"\n{'='*60}")
    print(f"  Testing: {provider_name}")
    print(f"  Prompt:  {prompt[:80]}...")
    print(f"{'='*60}")

    try:
        provider = factory()
    except Exception as exc:
        return {
            "provider": provider_name,
            "status": "ERROR",
            "error": f"Init failed: {exc}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    output_path = TEST_OUTPUT_DIR / f"{provider_name}_test.jpg"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Pre-flight check for local providers
    if hasattr(provider, "is_available"):
        print("  Checking availability...")
        if not provider.is_available():
            return {
                "provider": provider_name,
                "status": "UNAVAILABLE",
                "error": "Provider reported unavailable (dependencies missing?)",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    print("  Generating image...")
    t0 = time.monotonic()

    try:
        result_path = provider.generate(
            prompt=prompt,
            output_path=output_path,
            seed=seed,
        )
        elapsed = time.monotonic() - t0

        if result_path and result_path.exists():
            size_kb = result_path.stat().st_size / 1024
            print(f"  ✓ SUCCESS — {elapsed:.1f}s, {size_kb:.0f} KB, saved to {result_path}")

            # Save benchmark data
            _save_benchmark(provider_name, provider.metadata, elapsed, size_kb)

            return {
                "provider": provider_name,
                "status": "OK",
                "latency_seconds": round(elapsed, 2),
                "file_size_kb": round(size_kb, 1),
                "output_path": str(result_path),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        else:
            elapsed = time.monotonic() - t0
            print(f"  ✗ FAILED — {elapsed:.1f}s, provider returned None")
            return {
                "provider": provider_name,
                "status": "FAIL",
                "error": "generate() returned None",
                "latency_seconds": round(elapsed, 2),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"  ✗ ERROR — {elapsed:.1f}s, {type(exc).__name__}: {exc}")
        return {
            "provider": provider_name,
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
            "latency_seconds": round(elapsed, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def _save_benchmark(
    provider_name: str,
    metadata: AIProviderMetadata,
    elapsed: float,
    size_kb: float,
) -> None:
    """Save benchmark data for this provider run."""
    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    bench_path = BENCHMARK_DIR / f"{provider_name}.json"

    # Load existing benchmarks if any
    existing_samples: list[float] = []
    if bench_path.exists():
        try:
            data = json.loads(bench_path.read_text())
            existing_samples = data.get("latency_samples", [])
        except Exception:
            pass

    existing_samples.append(round(elapsed, 2))

    avg = sum(existing_samples) / len(existing_samples) if existing_samples else elapsed

    result = {
        "provider": provider_name,
        "display_name": metadata.display_name,
        "model": metadata.model,
        "latency_samples": existing_samples,
        "avg_latency_seconds": round(avg, 2),
        "last_result": {
            "latency_seconds": round(elapsed, 2),
            "file_size_kb": round(size_kb, 1),
            "status": "OK",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    }
    bench_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))


def print_summary(results: list[dict]) -> None:
    """Print a formatted summary table."""
    print(f"\n{'='*80}")
    print("  TEST SUMMARY")
    print(f"{'='*80}")
    print(f"  {'Provider':<18} {'Status':<12} {'Latency':>10} {'Size':>10}")
    print(f"  {'─'*18} {'─'*12} {'─'*10} {'─'*10}")

    for r in results:
        provider = r.get("provider", "?")
        status = r.get("status", "?")
        latency = f"{r.get('latency_seconds', 0):.1f}s" if r.get("latency_seconds") else "—"
        size = f"{r.get('file_size_kb', 0):.0f} KB" if r.get("file_size_kb") else "—"
        print(f"  {provider:<18} {status:<12} {latency:>10} {size:>10}")

    # Print errors
    errors = [r for r in results if r["status"] not in ("OK", "SKIP")]
    if errors:
        print(f"\n  Errors:")
        for r in errors:
            print(f"    [{r['provider']}] {r.get('error', 'unknown')}")

    print(f"{'='*80}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test AI image providers")
    parser.add_argument(
        "--provider", type=str, default=None,
        help="Test a single provider (default: all)",
    )
    parser.add_argument(
        "--prompt", type=str, default=TEST_PROMPT,
        help="Custom test prompt",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for reproducibility",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List available providers and exit",
    )
    args = parser.parse_args()

    if args.list:
        print("Available providers:")
        for name in sorted(ALL_PROVIDERS.keys()):
            factory = ALL_PROVIDERS[name]
            try:
                p = factory()
                meta = p.metadata
                print(f"  {name:<20} — {meta.display_name} (quality: {meta.quality_score}/10)")
            except Exception as exc:
                print(f"  {name:<20} — ERROR: {exc}")
        return

    if args.provider:
        providers_to_test = [args.provider]
    else:
        providers_to_test = list(ALL_PROVIDERS.keys())

    results: list[dict] = []
    for name in providers_to_test:
        result = test_provider(name, prompt=args.prompt, seed=args.seed)
        results.append(result)

    print_summary(results)

    # Exit code
    failures = [r for r in results if r["status"] not in ("OK", "SKIP")]
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
