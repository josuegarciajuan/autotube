#!/usr/bin/env python3
"""Regenerate all intro/CTA/outro template MP4s for all channels with voice-over audio.

Usage: python3 scripts/regen_templates.py [--channel canal2]
"""

import sys
import os
import logging
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s [template-regen] %(message)s")
logger = logging.getLogger("template-regen")

CHANNELS = {
    "canal2": {"slug": "canal2", "config_module": "config.canal2_config"},
    "canal3": {"slug": "canal3", "config_module": "config.canal3_config"},
    "canal4": {"slug": "canal4", "config_module": "config.canal4_config"},
}


def regen_channel(slug: str, config_module: str) -> dict:
    """Regenerate templates for a single channel. Returns dict of results."""
    import importlib
    mod = importlib.import_module(config_module)
    config = mod  # Module itself acts as config object

    from pipeline.template_generator import TemplateGenerator
    gen = TemplateGenerator(slug, channel_config=config)
    results = gen.generate_all()
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Regenerate intro/CTA/outro templates with voice")
    parser.add_argument("--channel", type=str, default=None, help="Single channel slug to regenerate")
    args = parser.parse_args()

    channels_to_run = (
        {k: v for k, v in CHANNELS.items() if k == args.channel}
        if args.channel
        else CHANNELS
    )

    if not channels_to_run:
        logger.error("Unknown channel: %s. Available: %s", args.channel, list(CHANNELS))
        sys.exit(1)

    success = 0
    fail = 0

    for slug, info in channels_to_run.items():
        logger.info("━━━ Regenerating templates for %s ━━━", slug)
        try:
            results = regen_channel(info["slug"], info["config_module"])
            for seg_type, path in results.items():
                if path and Path(path).exists():
                    size_kb = Path(path).stat().st_size / 1024
                    logger.info("  ✅ %s → %s (%.0f KB)", seg_type, path, size_kb)
                    success += 1
                else:
                    logger.warning("  ❌ %s → FAILED", seg_type)
                    fail += 1
        except Exception as exc:
            logger.exception("  💥 %s: %s", slug, exc)
            fail += 3  # 3 segments failed

    logger.info("━━━ Done: %d OK, %d failed ━━━", success, fail)
    sys.exit(0 if fail == 0 else 1)


if __name__ == "__main__":
    main()
