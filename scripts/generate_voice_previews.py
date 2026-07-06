"""Generate short audio preview clips for all available TTS voices.

Produces ~10-second MP3 samples in output/voice_previews/ for:
  - Kokoro-82M (local): ef_dora, em_alex, em_santa
  - Edge-TTS (cloud):  es-MX-DaliaNeural, es-MX-JorgeNeural, es-CO-GonzaloNeural

Usage:  python3 scripts/generate_voice_previews.py
"""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pydub import AudioSegment

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("previews")

OUTPUT_DIR = Path(__file__).parent.parent / "output" / "voice_previews"
SAMPLE_TEXT = "Esta es una muestra de la voz narradora para los videos documentales del canal."

# ── Voice metadata ────────────────────────────────────────────
VOICES = {
    "kokoro": [
        {"key": "ef_dora", "name": "Dora", "gender": "female"},
        {"key": "em_alex", "name": "Alex", "gender": "male"},
        {"key": "em_santa", "name": "Santa", "gender": "male"},
    ],
    "edgetts": [
        {"key": "es-MX-DaliaNeural", "name": "Dalia", "gender": "female"},
        {"key": "es-MX-JorgeNeural", "name": "Jorge", "gender": "male"},
        {"key": "es-CO-GonzaloNeural", "name": "Gonzalo", "gender": "male"},
    ],
}


async def generate_edgetts(voice_key: str, out_path: Path) -> bool:
    """Generate a preview clip using Microsoft Edge TTS (cloud)."""
    import edge_tts

    communicate = edge_tts.Communicate(SAMPLE_TEXT, voice_key)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        await communicate.save(str(out_path))
        logger.info("  ✓ Edge-TTS: %s → %s", voice_key, out_path.name)
        return True
    except Exception as exc:
        logger.error("  ✗ Edge-TTS %s failed: %s", voice_key, exc)
        return False


def generate_kokoro(voice_key: str, out_path: Path) -> bool:
    """Generate a preview clip using Kokoro-82M (local)."""
    import numpy as np
    import soundfile as sf
    from kokoro import KPipeline

    pipeline = KPipeline(lang_code="e", repo_id="hexgrad/Kokoro-82M")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        generator = pipeline(SAMPLE_TEXT, voice=voice_key, speed=1.0)
        all_audio = []
        for _gs, _ps, audio in generator:
            all_audio.append(audio)

        if not all_audio:
            logger.error("  ✗ Kokoro %s: no audio generated", voice_key)
            return False

        combined = np.concatenate(all_audio)

        # Save as WAV first, then convert to MP3 via pydub
        wav_path = out_path.with_suffix(".wav")
        sf.write(str(wav_path), combined, 24000)

        audio_seg = AudioSegment.from_wav(str(wav_path))
        audio_seg.export(str(out_path), format="mp3", bitrate="128k")
        wav_path.unlink()  # clean up temp WAV

        logger.info("  ✓ Kokoro: %s → %s (%.1fs)", voice_key, out_path.name, len(combined) / 24000)
        return True

    except Exception as exc:
        logger.error("  ✗ Kokoro %s failed: %s", voice_key, exc)
        return False


async def main():
    logger.info("Generating voice preview clips…")
    logger.info("Sample text: \"%s\"", SAMPLE_TEXT)

    # ── Edge-TTS (cloud) ───────────────────────────────────────
    logger.info("\n── Edge-TTS voices ──")
    for v in VOICES["edgetts"]:
        out = OUTPUT_DIR / "edgetts" / f"{v['key']}.mp3"
        if out.exists():
            logger.info("  - %s already exists, skipping", out.name)
        else:
            await generate_edgetts(v["key"], out)

    # ── Kokoro (local) ─────────────────────────────────────────
    logger.info("\n── Kokoro voices ──")
    for v in VOICES["kokoro"]:
        out = OUTPUT_DIR / "kokoro" / f"{v['key']}.mp3"
        if out.exists():
            logger.info("  - %s already exists, skipping", out.name)
        else:
            generate_kokoro(v["key"], out)

    logger.info("\n✅ Done. Previews in %s", OUTPUT_DIR)


if __name__ == "__main__":
    asyncio.run(main())
