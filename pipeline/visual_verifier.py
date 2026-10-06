"""Fase 4b — Verificador visual de assets en modo observación (fail-open).

Objetivo: detectar candidatos evidentemente malos (logo/overlay de marca
confirmado en una esquina, resolución claramente insuficiente) *antes* de
gastarlos en el render, sin fingir una verificación de acción que hoy no existe.

Modos (``VISUAL_VERIFY_MODE``):

* ``off``     → no-op absoluto.  Este módulo no mira ni un píxel.
* ``observe`` → extrae 1-3 fotogramas del tramo que se usará, calcula
  heurísticas baratas (overlay en esquinas, nitidez aproximada, bordes de
  texto) y REGISTRA una observación estructurada.  Nunca descarta ni bloquea.
* ``enforce`` → además descarta un candidato con logo confirmado o resolución
  claramente insuficiente y deja que el llamador pruebe el siguiente dentro
  del presupuesto.  NO activar por defecto.

Principios:

* **Fail-open**: cualquier error (PIL/numpy/ffmpeg ausente, fichero ilegible,
  fallo de extracción) se traduce en "sin verificación" y el asset se acepta.
* **Sin dependencias nuevas**: PIL es opcional para imágenes; numpy es opcional
  para la varianza del laplaciano; ffmpeg es opcional para vídeo (si falta, se
  degrada y no se extraen fotogramas).
* **Heurísticas, no modelo**: ``_classify_frames_with_model`` es el punto de
  extensión documentado para un futuro modelo local de análisis de imagen.
  Hoy devuelve ``None`` de forma explícita para no fingir una verificación de
  acción/coherencia que la heurística barata no puede dar.
* **Tramo real**: los fotogramas se muestrean uniformemente sobre la ventana
  de duración que se usará (hasta ``target_dur``), empezando en
  ``segment_start``/``offset`` cuando el asset lo declare.  ``media_fetcher``
  hoy no adjunta ese offset al dict del asset (el render calcula el segmento
  aparte), así que en la práctica se muestrea desde el inicio del clip.  La
  observación es heurística y de solo lectura, por lo que esto no cambia el
  contrato fail-open.

La detección de overlay en esquinas está extraída de
``scripts/check_asset_logos.py`` (que ahora importa estas funciones) para no
duplicar la lógica.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable, Sequence

logger = logging.getLogger(__name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")

VALID_MODES = ("off", "observe", "enforce")
DEFAULT_MODE = "off"

# Defaults for the corner-overlay heuristic (same as check_asset_logos).
CORNER_FRAC = 0.20
# oct 2026: thresholds raised.  The old corner edge-density heuristic flagged
# ~40-55% of legitimate stock assets as "logo" (textured bottom-left/right
# corners), exhausting the candidate budget and firing
# `visual_verify_over_rejection` alerts with no real gain.
LOGO_RATIO_THRESHOLD = 2.6
LOGO_EDGE_FLOOR = 25.0
LOGO_STD_FLOOR = 12.0

# Logo-overlay rejection is DISABLED by default (oct 2026).  The heuristic was
# producing mostly false positives on real stock media, so `enforce` no longer
# discards on a corner suspicion unless explicitly re-enabled once calibrated:
#   VISUAL_VERIFY_REJECT_LOGO=true
# Resolution (`low_res`) rejection stays active — that signal is reliable.
REJECT_ON_LOGO = os.getenv(
    "VISUAL_VERIFY_REJECT_LOGO", "false"
).strip().lower() in {"1", "true", "yes", "on"}

# Absolute floor below which an image is "clearly insufficient".  Deliberately
# lower than `min_stock_image_width` (1280) so blurry-but-usable assets are not
# thrown away by the enforce mode.
_MIN_ACCEPTABLE_WIDTH = 640
# Advisory sharpness floor (Laplacian variance).  Never used to reject alone:
# the proxy is noisy across genres (grainy archival vs sharp studio footage).
_MIN_SHARPNESS = 20.0


# ── Corner overlay detection (extracted from scripts/check_asset_logos.py) ──

def _numpy_available() -> bool:
    try:
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


def _edge_image(gray):
    """Return (edge_image, mode). numpy gradient if available, else PIL."""
    try:
        import numpy as np
        from PIL import Image

        arr = np.asarray(gray, dtype=np.float32)
        gy, gx = np.gradient(arr)
        mag = np.hypot(gx, gy)
        # Normalize to a 0-255 range using the 99th percentile (robust to a
        # single hard edge) so comparisons are stable across images.
        if mag.size:
            p99 = float(np.percentile(mag, 99.0))
            if p99 > 1e-6:
                mag = mag / p99 * 255.0
        mag = np.clip(mag, 0, 255).astype("uint8")
        return Image.fromarray(mag), "numpy"
    except Exception:
        from PIL import ImageFilter
        return gray.filter(ImageFilter.FIND_EDGES), "pil"


def _corner_boxes(w: int, h: int, frac: float) -> dict[str, tuple[int, int, int, int]]:
    cw = max(8, int(w * frac))
    ch = max(8, int(h * frac))
    return {
        "top-left": (0, 0, cw, ch),
        "top-right": (w - cw, 0, w, ch),
        "bottom-left": (0, h - ch, cw, h),
        "bottom-right": (w - cw, h - ch, w, h),
    }


def analyze_image(
    path: Path,
    frac: float = CORNER_FRAC,
    ratio_threshold: float = LOGO_RATIO_THRESHOLD,
    edge_floor: float = LOGO_EDGE_FLOOR,
    std_floor: float = LOGO_STD_FLOOR,
) -> dict:
    """Analyse one image; returns a report dict. Never raises for IO issues."""
    from PIL import Image, ImageStat

    report: dict = {"path": str(path), "ok": False, "corners": {}}
    try:
        with Image.open(path) as img:
            img = img.convert("L")
            w, h = img.size
            if w < 32 or h < 32:
                report["error"] = "image too small to inspect"
                return report
            edge, mode = _edge_image(img)
            boxes = _corner_boxes(w, h, frac)
            metrics: dict[str, dict] = {}
            for name, box in boxes.items():
                edge_patch = edge.crop(box)
                gray_patch = img.crop(box)
                edge_mean = float(ImageStat.Stat(edge_patch).mean[0])
                gray_std = float(ImageStat.Stat(gray_patch).stddev[0])
                metrics[name] = {
                    "edge_mean": round(edge_mean, 2),
                    "std": round(gray_std, 2),
                }
            edge_vals = [m["edge_mean"] for m in metrics.values()]
            med = median(edge_vals) if edge_vals else 0.0
            suspicious = []
            for name, m in metrics.items():
                ratio = m["edge_mean"] / (med + 1e-6)
                m["ratio_vs_median"] = round(ratio, 2)
                m["suspicious"] = bool(
                    ratio >= ratio_threshold
                    and m["edge_mean"] >= edge_floor
                    and m["std"] >= std_floor
                )
                if m["suspicious"]:
                    suspicious.append(name)
            report.update({
                "ok": True,
                "width": w,
                "height": h,
                "mode": mode,
                "median_corner_edge": round(med, 2),
                "suspicious_corners": suspicious,
                "corners": metrics,
            })
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
    return report


def extract_video_frame(
    path: Path, tmp_dir: Path, ss: str = "00:00:01", tag: str = ""
) -> Path | None:
    """Extract one representative frame (read-only w.r.t. the asset).

    ``tag`` disambiguates multiple frames from the same clip (default keeps the
    legacy ``<stem>_frame.jpg`` name used by the CLI script).
    """
    if shutil.which("ffmpeg") is None:
        return None
    suffix = f"_{tag}" if tag else "_frame"
    out_path = tmp_dir / (path.stem + suffix + ".jpg")
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-ss", str(ss), "-i", str(path),
                "-frames:v", "1", "-q:v", "2", str(out_path),
            ],
            capture_output=True, text=True, timeout=45,
        )
        if proc.returncode == 0 and out_path.exists():
            return out_path
    except Exception as exc:  # noqa: BLE001
        logger.debug("frame extraction failed for %s: %s", path, exc)
    return None


# ── Cheap sharpness / text heuristics ─────────────────────────────────

def measure_sharpness(path: Path | str) -> float | None:
    """Approximate sharpness as the variance of the Laplacian.

    Returns ``None`` when numpy/PIL are unavailable or the file cannot be read
    (degrade clean, never raise).
    """
    try:
        import numpy as np
        from PIL import Image

        with Image.open(path) as img:
            gray = img.convert("L")
            # Downscale for a stable, fast metric.
            gray.thumbnail((512, 512))
            arr = np.asarray(gray, dtype=np.float32)
        if arr.ndim != 2 or min(arr.shape) < 3:
            return None
        lap = (
            -4.0 * arr[1:-1, 1:-1]
            + arr[:-2, 1:-1] + arr[2:, 1:-1]
            + arr[1:-1, :-2] + arr[1:-1, 2:]
        )
        return float(lap.var())
    except Exception:
        return None


def detect_text_edges(path: Path | str) -> bool | None:
    """Cheap "possible text/overlay" proxy from concentrated edges.

    Looks at the central horizontal band; a high edge density there is a weak
    signal of subtitles/lower-thirds/captions baked into the asset.  Returns
    ``None`` when it cannot be computed (fail-open).
    """
    try:
        from PIL import Image, ImageStat

        with Image.open(path) as img:
            gray = img.convert("L")
            w, h = gray.size
            if w < 32 or h < 32:
                return None
            edge, _mode = _edge_image(gray)
            box = (int(w * 0.20), int(h * 0.40), int(w * 0.80), int(h * 0.62))
            patch = edge.crop(box)
            mean_edge = float(ImageStat.Stat(patch).mean[0])
            std_edge = float(ImageStat.Stat(patch).stddev[0])
            return bool(mean_edge >= 18.0 and std_edge >= 12.0)
    except Exception:
        return None


# ── Frame extraction for the ACTUAL segment that will be used ─────────

def _asset_width(asset: dict) -> int:
    for key in ("width", "original_width"):
        try:
            value = int(asset.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def _asset_segment_start(asset: dict) -> float:
    for key in ("segment_start", "offset", "start_offset"):
        try:
            return max(0.0, float(asset.get(key) or 0.0))
        except (TypeError, ValueError):
            continue
    return 0.0


def _effective_span(asset: dict, target_dur: float) -> float:
    try:
        asset_dur = float(asset.get("duration") or 0.0)
    except (TypeError, ValueError):
        asset_dur = 0.0
    try:
        target = float(target_dur or 0.0)
    except (TypeError, ValueError):
        target = 0.0
    if asset_dur > 0 and target > 0:
        return max(0.2, min(asset_dur, target))
    return max(0.2, asset_dur or target or 5.0)


def _frame_timestamps(start: float, span: float, max_frames: int) -> list[str]:
    n = max(1, int(max_frames))
    if n == 1:
        points = [start + span / 2.0]
    else:
        # Evenly spaced, slightly inset so we never sample past the clip end.
        end = start + span
        usable = max(span - 0.2, 0.1)
        points = [start + 0.1 + usable * i / (n - 1) for i in range(n)]
        points = [min(p, end) for p in points]
    out = []
    for p in points:
        total = max(0.0, float(p))
        out.append(
            f"{int(total // 3600):02d}:{int((total % 3600) // 60):02d}:{total % 60:05.2f}"
        )
    return out


def extract_asset_frames(
    asset: dict,
    target_dur: float = 5.0,
    max_frames: int = 3,
    tmp_dir: Path | None = None,
    frame_extractor: Callable[..., Path | None] | None = None,
) -> list[Path]:
    """Frames from the real segment that will be used for *asset*.

    Images return themselves as a single "frame".  Videos yield up to
    ``max_frames`` extracted frames.  Degrades to an empty list when the asset
    is missing, the extension is unsupported, or ffmpeg is unavailable.
    """
    if not isinstance(asset, dict):
        return []
    raw_path = asset.get("path")
    if not raw_path:
        return []
    path = Path(str(raw_path))
    if not path.exists():
        return []
    atype = str(asset.get("type") or "").lower()
    suffix = path.suffix.lower()
    if atype == "image" or suffix in IMAGE_EXTS:
        return [path]
    if atype != "video" and suffix not in VIDEO_EXTS:
        return []
    if tmp_dir is None:
        return []
    extractor = frame_extractor or extract_video_frame
    start = _asset_segment_start(asset)
    span = _effective_span(asset, target_dur)
    frames: list[Path] = []
    for i, ts in enumerate(_frame_timestamps(start, span, max_frames)):
        try:
            frame = extractor(path, tmp_dir, ss=ts, tag=str(i))
        except TypeError:
            # Test/legacy extractors may not accept ``tag``.
            try:
                frame = extractor(path, tmp_dir, ss=ts)
            except Exception:
                frame = None
        except Exception:
            frame = None
        if frame:
            frames.append(Path(frame))
    return frames


# ── Model extension point (deliberately a no-op today) ─────────────────

def _classify_frames_with_model(
    frames: Sequence[Path], scene_context: Any = None
) -> Any:
    """Extension point for a future LOCAL image-analysis model.

    Today returns ``None`` on purpose: the cheap heuristics above cannot judge
    whether a clip depicts the narrated action, and pretending otherwise would
    be a false verification.  A future implementation (e.g. a small CLIP/OWL-ViT
    classifier running locally) should return a structured dict — e.g.
    ``{"action_match": float, "labels": [...]}`` — and callers may then use it.

    Kept dependency-free and side-effect-free so wiring a model later does not
    change the fail-open contract.
    """
    return None


# ── Observation & verification ────────────────────────────────────────

@dataclass
class VisualObservation:
    """Structured result of verifying ONE candidate asset."""

    scene_idx: int = 0
    path: str = ""
    asset_type: str = ""
    mode: str = DEFAULT_MODE
    frames_checked: int = 0
    logo_suspected: bool = False
    logo_corners: list[str] = field(default_factory=list)
    sharpness: float | None = None
    sharpness_ok: bool = True
    has_text_edges: bool = False
    resolution_ok: bool = True
    width: int = 0
    rejected: bool = False
    # Normalised rejection category for aggregated alerting:
    # ``"logo"``, ``"low_res"``, ``"error"`` or ``""`` (not rejected).
    # ``reason`` keeps the human-readable detail; this field is the stable key
    # used to aggregate counts per video without changing accept/reject logic.
    reject_reason: str = ""
    reason: str = ""
    error: str | None = None
    model_result: Any = None

    def to_dict(self) -> dict:
        return asdict(self)


def _reject_category(obs: "VisualObservation") -> str:
    """Return a stable category for a rejected observation (never raises)."""
    try:
        value = str(getattr(obs, "reject_reason", "") or "").strip().lower()
        if value:
            return value
        reason = str(getattr(obs, "reason", "") or "").strip().lower()
        if reason.startswith("logo"):
            return "logo"
        if reason.startswith("low_res"):
            return "low_res"
        if getattr(obs, "error", None):
            return "error"
        return reason or "unknown"
    except Exception:  # pragma: no cover - defensive
        return "unknown"


def verify_asset(
    asset: dict,
    scene_idx: int = 0,
    mode: str = DEFAULT_MODE,
    target_dur: float = 5.0,
    max_frames: int = 3,
    min_acceptable_width: int = _MIN_ACCEPTABLE_WIDTH,
    image_analyzer: Callable[..., dict] | None = None,
    frame_extractor: Callable[..., Path | None] | None = None,
    scene_context: Any = None,
    reject_on_logo: bool | None = None,
) -> VisualObservation | None:
    """Verify one chosen asset.  Returns ``None`` when mode is ``off``.

    In ``observe`` the observation is advisory (``rejected`` stays False).
    In ``enforce``, a confirmed corner logo or a clearly insufficient width
    sets ``rejected=True``.  Any internal error is captured in ``error`` and
    leaves the asset accepted (fail-open).
    """
    if mode not in VALID_MODES or mode == "off":
        return None
    if not isinstance(asset, dict) or not asset.get("path"):
        return None

    obs = VisualObservation(
        scene_idx=int(scene_idx or 0),
        path=str(asset.get("path") or ""),
        asset_type=str(asset.get("type") or ""),
        mode=mode,
        width=_asset_width(asset),
    )

    tmp_dir: Path | None = None
    try:
        if str(asset.get("type") or "").lower() == "video" or (
            Path(obs.path).suffix.lower() in VIDEO_EXTS
        ):
            tmp_dir = Path(tempfile.mkdtemp(prefix="visual_verify_"))
        frames = extract_asset_frames(
            asset, target_dur=target_dur, max_frames=max_frames,
            tmp_dir=tmp_dir, frame_extractor=frame_extractor,
        )
        obs.frames_checked = len(frames)
        if not frames:
            obs.error = "no_frames_extracted"
            return obs

        analyzer = image_analyzer or analyze_image
        corners: set[str] = set()
        sharp_vals: list[float] = []
        text_hits = 0
        widths: list[int] = []
        for frame in frames:
            report = analyzer(frame) or {}
            if report.get("ok"):
                for corner in report.get("suspicious_corners") or []:
                    corners.add(corner)
                try:
                    fw = int(report.get("width") or 0)
                except (TypeError, ValueError):
                    fw = 0
                if fw > 0:
                    widths.append(fw)
                value = measure_sharpness(frame)
                if value is not None:
                    sharp_vals.append(value)
                text = detect_text_edges(frame)
                if text:
                    text_hits += 1

        obs.logo_corners = sorted(corners)
        obs.logo_suspected = bool(corners)
        if sharp_vals:
            obs.sharpness = sum(sharp_vals) / len(sharp_vals)
            obs.sharpness_ok = obs.sharpness >= _MIN_SHARPNESS
        obs.has_text_edges = text_hits > 0
        if not obs.width and widths:
            obs.width = max(widths)
        obs.resolution_ok = not (
            obs.width > 0 and obs.width < max(1, int(min_acceptable_width))
        )

        obs.model_result = _classify_frames_with_model(frames, scene_context)

        _reject_logo = REJECT_ON_LOGO if reject_on_logo is None else bool(reject_on_logo)
        if mode == "enforce":
            if obs.logo_suspected and _reject_logo:
                obs.rejected = True
                obs.reject_reason = "logo"
                obs.reason = "logo_overlay:" + ",".join(obs.logo_corners)
            elif not obs.resolution_ok:
                obs.rejected = True
                obs.reject_reason = "low_res"
                obs.reason = f"low_resolution:{obs.width}px"
            elif obs.logo_suspected:
                # Advisory now (see REJECT_ON_LOGO): recorded, never discarded.
                obs.reason = "logo_suspected_advisory:" + ",".join(obs.logo_corners)
            elif obs.sharpness is not None and not obs.sharpness_ok:
                # Advisory only — the proxy is too noisy to reject on.
                obs.reason = "low_sharpness_advisory"
    except Exception as exc:  # noqa: BLE001
        obs.error = str(exc)
        obs.rejected = False
        # Fail-open keeps the asset, but the failure mode is tagged so the
        # per-video alerting can distinguish errors from clean verifications.
        obs.reject_reason = "error"
        logger.debug("visual verify failed (fail-open): %s", exc)
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    # Structured observability (fail-open): what was discarded and why.
    try:
        from pipeline.observability import obs_event
        try:
            _path_name = Path(obs.path).name if obs.path else ""
        except Exception:
            _path_name = ""
        obs_event(
            "visual_verify",
            scene_idx=obs.scene_idx,
            mode=mode,
            asset_type=obs.asset_type,
            path_name=_path_name,
            frames=obs.frames_checked,
            logo=obs.logo_suspected,
            corners=obs.logo_corners,
            width=obs.width,
            resolution_ok=obs.resolution_ok,
            rejected=obs.rejected,
            reject_reason=obs.reject_reason,
            reason=obs.reason,
            error=obs.error,
        )
    except Exception:
        pass
    return obs


class CandidateGate:
    """Stateful candidate budget for one scene (Fase 4b).

    ``consider(asset)`` returns ``True`` to accept and ``False`` to discard
    (enforce mode only).  The budget limits how many candidates are verified;
    once exhausted, everything is accepted without verification (fail-open).
    """

    def __init__(
        self,
        scene_idx: int = 0,
        mode: str = DEFAULT_MODE,
        budget: int = 3,
        target_dur: float = 5.0,
        verify: Callable[..., VisualObservation | None] | None = None,
        on_observation: Callable[[VisualObservation], None] | None = None,
        **verify_kwargs: Any,
    ) -> None:
        self.scene_idx = int(scene_idx or 0)
        self.mode = mode if mode in VALID_MODES else DEFAULT_MODE
        try:
            self.budget = max(1, int(budget))
        except (TypeError, ValueError):
            self.budget = 3
        try:
            self.target_dur = float(target_dur)
        except (TypeError, ValueError):
            self.target_dur = 5.0
        self._verify = verify or verify_asset
        self._on_observation = on_observation
        self._verify_kwargs = verify_kwargs
        self.attempts = 0
        self.observations: list[VisualObservation] = []
        self.accepted_path: str | None = None
        # Aggregated counters for per-video alerting (never affect semantics).
        self.rejected_count = 0
        self.accepted_count = 0
        self.rejected_reasons: dict[str, int] = {}

    def consider(self, asset: dict, scene_context: Any = None) -> bool:
        if self.mode == "off" or not isinstance(asset, dict):
            return True
        if self.attempts >= self.budget:
            return True  # budget spent → accept without further verification
        self.attempts += 1
        obs: VisualObservation | None
        try:
            obs = self._verify(
                asset,
                scene_idx=self.scene_idx,
                mode=self.mode,
                target_dur=self.target_dur,
                scene_context=scene_context,
                **self._verify_kwargs,
            )
        except Exception as exc:  # fail-open
            logger.debug("CandidateGate verify failed (fail-open): %s", exc)
            return True
        if obs is not None:
            self.observations.append(obs)
            if self._on_observation is not None:
                try:
                    self._on_observation(obs)
                except Exception:
                    pass
        if obs is None:
            return True
        if obs.rejected and self.mode == "enforce":
            self.rejected_count += 1
            category = _reject_category(obs)
            self.rejected_reasons[category] = self.rejected_reasons.get(category, 0) + 1
            return False
        if obs.path:
            self.accepted_path = obs.path
        self.accepted_count += 1
        return True


def select_verified_asset(
    candidates: Iterable[dict],
    scene_idx: int = 0,
    mode: str = DEFAULT_MODE,
    budget: int = 3,
    target_dur: float = 5.0,
    **verify_kwargs: Any,
) -> tuple[dict | None, list[VisualObservation]]:
    """Pick the first acceptable candidate honouring the verification budget.

    ``off`` returns the first candidate untouched (no verification).  ``observe``
    verifies the first candidate and always accepts it.  ``enforce`` walks the
    list, discarding rejected candidates, until one passes or the budget runs
    out (then the next candidate is accepted fail-open).
    """
    gate = CandidateGate(
        scene_idx=scene_idx, mode=mode, budget=budget,
        target_dur=target_dur, **verify_kwargs,
    )
    last: dict | None = None
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        last = candidate
        if gate.consider(candidate):
            return candidate, gate.observations
    return (last if mode == "off" else None), gate.observations
