"""AIImageUpscaler — upscale local de imágenes IA + nitidez (unsharp mask).

Problema: Pollinations (modelo flux) devuelve imágenes a su resolución
nativa 1024×576 aunque se pida 1920×1080. Local SD genera a 512×512.
Al escalarlas durante el render el resultado se ve borroso.

Solución en dos pasos:
  1. Upscale hasta la resolución mínima objetivo con super-resolución
     (ESPCN_x2 por defecto — rápido en CPU, ~1s/imagen).
  2. Unsharp mask para recuperar nitidez percibida. Un benchmark real
     mostró que el sharpening aporta +56% de nitidez (Tenengrad) en 0.1s,
     mientras que modelos más pesados (EDSR_x4, 313s/imagen) no mejoran
     porque la fuente 1024×576 no contiene más detalle que "inventar".

El modelo se descarga una sola vez (primer uso) a ``output/models/`` y se
reutiliza desde disco. Si el modelo o el módulo contrib de OpenCV no están
disponibles, el upscaler degrada a no-op — NUNCA rompe el pipeline.

Uso::

    from pipeline.ai_upscaler import AIImageUpscaler

    upscaler = AIImageUpscaler()
    ok = upscaler.upscale_to_min(Path("output/ai_images/scene.jpg"), 1920, 1080)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Modelos de super-resolución soportados por OpenCV dnn_superres ──
# ESPCN: ligero y rápido en CPU (~1s/imagen). Suficiente para alcanzar
#        la resolución mínima objetivo desde una fuente 1024×576.
#        El salto de nitidez real lo aporta el unsharp mask posterior.
# EDSR: mejor reconstrucción de detalle, pero ~300s/imagen en CPU y sin
#        ganancia medible de nitidez sobre LANCZOS para estas fuentes.
# LapSRN/FSRCNN: alternativas intermedias.
MODEL_REGISTRY: dict[str, dict] = {
    "espcn": {
        "name": "ESPCN",
        "scale": 2,
        "url": (
            "https://raw.githubusercontent.com/fannymonori/TF-ESPCN/master/export/ESPCN_x2.pb"
        ),
        "file": "espcn_x2.pb",
        "size_hint_mb": 0.09,
    },
    "edsr": {
        "name": "EDSR",
        "scale": 4,
        "url": (
            "https://github.com/Saafke/EDSR_Tensorflow/raw/master/models/EDSR_x4.pb"
        ),
        "file": "edsr_x4.pb",
        "size_hint_mb": 38,
    },
    "lapsrn": {
        "name": "LapSRN",
        "scale": 8,
        "url": (
            "https://github.com/fannymonori/TF-LapSRN/raw/master/export/LapSRN_x8.pb"
        ),
        "file": "lapsrn_x8.pb",
        "size_hint_mb": 5,
    },
    "fsrcnn": {
        "name": "FSRCNN",
        "scale": 4,
        "url": (
            "https://github.com/Saafke/FSRCNN_Tensorflow/raw/master/models/FSRCNN_x4.pb"
        ),
        "file": "fsrcnn_x4.pb",
        "size_hint_mb": 0.04,
    },
}

DEFAULT_MODEL = "espcn"
DOWNLOAD_TIMEOUT = 120  # segundos

# Máximo de píxeles que aceptamos upscalear. Por encima de esto el beneficio
# de la super-resolución es marginal y el coste de memoria crece.
MAX_INPUT_DIM = 2560

# Tope duro de salida del modelo: evita que modelos 4x/8x disparen imágenes
# gigantes en el bucle.
MAX_OUTPUT_DIM = 4096

# Tope "útil para el render" aplicado AL FINAL: el editor de video acota las
# fuentes Ken Burns a 2560 px de lado mayor (video_editor.py), así que una
# salida mayor se reduce a 2560 sin perder nitidez real.
FINAL_CAP_DIM = 2560

# ── Unsharp mask (nitidez percibida) ──────────────────────────
# sharpened = img * (1 + amount) - gaussian_blur(img) * amount
SHARPEN_ENABLED = True
SHARPEN_AMOUNT = 0.4      # 0 = desactivado; ~0.4 equilibrado
SHARPEN_SIGMA = 2.0       # radio del desenfoque gaussiano


class AIImageUpscaler:
    """Upscale imágenes IA + unsharp mask con OpenCV DNN superres.

    Singleton por proceso: la red se carga una única vez y se comparte
    entre todos los proveedores/videos. El modelo se descarga al primer
    uso y queda cacheado en disco.
    """

    _instance: Optional["AIImageUpscaler"] = None

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        model_dir: Optional[Path] = None,
        sharpen_enabled: bool = SHARPEN_ENABLED,
        sharpen_amount: float = SHARPEN_AMOUNT,
        sharpen_sigma: float = SHARPEN_SIGMA,
    ) -> None:
        if model not in MODEL_REGISTRY:
            logger.warning(
                "AIImageUpscaler: modelo '%s' no soportado — usando '%s'",
                model, DEFAULT_MODEL,
            )
            model = DEFAULT_MODEL
        self.model = model
        cfg = MODEL_REGISTRY[model]
        self.scale = cfg["scale"]
        self.model_url = cfg["url"]
        self.model_file = cfg["file"]

        self.sharpen_enabled = sharpen_enabled
        self.sharpen_amount = sharpen_amount
        self.sharpen_sigma = sharpen_sigma

        if model_dir is None:
            from config import settings
            model_dir = settings.OUTPUT_DIR / "models"
        self.model_dir = Path(model_dir)
        self.model_path = self.model_dir / self.model_file
        self._net = None          # lazy — cv2.dnn_superres net
        self._model_ok = None     # None = no verificado aún; bool después

    @classmethod
    def get_instance(cls, model: str = DEFAULT_MODEL) -> "AIImageUpscaler":
        """Devuelve el singleton compartido del proceso.

        Si se pide un modelo distinto del cargado, se crea una instancia
        nueva (la red se cachea por modelo).
        """
        if cls._instance is None or cls._instance.model != model:
            cls._instance = cls(model=model)
        return cls._instance

    # ── API pública ────────────────────────────────────────────────

    def upscale_to_min(
        self,
        image_path: Path,
        min_width: int,
        min_height: int,
    ) -> bool:
        """Upscalea *image_path* a ≥ (min_width, min_height) y aplica
        unsharp mask para nitidez.

        Sobrescribe el archivo in-place con la versión de mayor resolución.

        Returns:
            ``True`` si la imagen fue procesada (o ya cumplía el mínimo),
            ``False`` si el upscaler no está disponible (fallback degradado:
            la imagen queda como estaba).
        """
        image_path = Path(image_path)
        if not image_path.exists():
            return False

        try:
            from PIL import Image
            with Image.open(image_path) as im:
                w, h = im.size
        except Exception as exc:
            logger.debug("AIImageUpscaler: no se pudo leer %s: %s", image_path, exc)
            return False

        # Ya cumple el mínimo → no-op (evita re-sharpen en cache hits)
        if w >= min_width and h >= min_height:
            return True

        # Input demasiado grande para upscalear con beneficio
        if max(w, h) > MAX_INPUT_DIM:
            logger.debug(
                "AIImageUpscaler: %dx%d excede el máximo %d — sin upscale",
                w, h, MAX_INPUT_DIM,
            )
            return False

        net = self._get_net()
        if net is None:
            logger.warning(
                "AIImageUpscaler no disponible (modelo o dnn_superres ausentes) — "
                "imagen %s se usa sin upscale", image_path,
            )
            return False

        try:
            import cv2
            img = cv2.imread(str(image_path))
            if img is None:
                logger.warning("AIImageUpscaler: cv2 no pudo leer %s", image_path)
                return False

            logger.info(
                "AIImageUpscaler: %dx%d → %s (%s_x%d, mínimo %dx%d)",
                w, h, image_path.name, self.model, self.scale,
                min_width, min_height,
            )

            # ── Multi-pase: aplicar el modelo hasta alcanzar el mínimo ──
            upscaled = img
            passes = 0
            while True:
                uw, uh = upscaled.shape[1], upscaled.shape[0]
                if (uw >= min_width and uh >= min_height) or passes >= 2:
                    break
                if max(uw, uh) * self.scale > MAX_OUTPUT_DIM:
                    break
                upscaled = net.upsample(upscaled)
                passes += 1

            uw, uh = upscaled.shape[1], upscaled.shape[0]

            # Ajuste final LANCZOS si el modelo no llegó al mínimo pedido
            if uw < min_width or uh < min_height:
                from PIL import Image as PILImage
                import numpy as np
                pil_img = PILImage.fromarray(cv2.cvtColor(upscaled, cv2.COLOR_BGR2RGB))
                ratio = max(min_width / uw, min_height / uh)
                new_size = (int(uw * ratio), int(uh * ratio))
                pil_img = pil_img.resize(new_size, PILImage.LANCZOS)
                upscaled = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
                uw, uh = upscaled.shape[1], upscaled.shape[0]

            # ── Cap final útil para el render ────────────────────
            if max(uw, uh) > FINAL_CAP_DIM:
                from PIL import Image as PILImage
                import numpy as np
                pil_img = PILImage.fromarray(cv2.cvtColor(upscaled, cv2.COLOR_BGR2RGB))
                ratio = FINAL_CAP_DIM / max(uw, uh)
                new_size = (int(uw * ratio), int(uh * ratio))
                pil_img = pil_img.resize(new_size, PILImage.LANCZOS)
                upscaled = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
                uw, uh = upscaled.shape[1], upscaled.shape[0]

            # ── Unsharp mask (nitidez percibida) ─────────────────
            # El benchmark mostró +56% de nitidez en 0.1s; es el paso que
            # de verdad elimina el aspecto borroso de las fuentes 1024×576.
            if self.sharpen_enabled and self.sharpen_amount > 0:
                upscaled = self._unsharp_mask(upscaled)

            if not cv2.imwrite(str(image_path), upscaled):
                logger.warning("AIImageUpscaler: fallo al escribir %s", image_path)
                return False
            logger.debug(
                "AIImageUpscaler: %dx%d → %dx%d (%d pase(s), sharpen=%s)",
                w, h, uw, uh, passes, self.sharpen_enabled,
            )
            return True

        except Exception as exc:
            logger.warning("AIImageUpscaler: error de upscale en %s: %s", image_path, exc)
            return False

    # ── Interno ────────────────────────────────────────────────────

    def _unsharp_mask(self, img):
        """Aplica unsharp mask: img*(1+amount) - blur*amount."""
        import cv2
        blur = cv2.GaussianBlur(img, (0, 0), self.sharpen_sigma)
        sharpened = cv2.addWeighted(
            img, 1.0 + self.sharpen_amount,
            blur, -self.sharpen_amount,
            0,
        )
        return sharpened

    def _get_net(self):
        """Carga (lazy) la red de super-resolución. None si no hay modelo."""
        if self._net is not None:
            return self._net
        if not self._ensure_model():
            return None
        try:
            import cv2
            if not hasattr(cv2, "dnn_superres"):
                logger.warning(
                    "cv2 no incluye dnn_superres (falta opencv-contrib) — upscale desactivado"
                )
                return None
            sr = cv2.dnn_superres.DnnSuperResImpl_create()
            sr.readModel(str(self.model_path))
            sr.setModel(self.model, self.scale)
            self._net = sr
            return sr
        except Exception as exc:
            logger.warning(
                "AIImageUpscaler: no se pudo cargar %s_x%d: %s",
                self.model, self.scale, exc,
            )
            return None

    def _ensure_model(self) -> bool:
        """Descarga el modelo al primer uso. Idempotente."""
        if self._model_ok is True:
            return True

        if self.model_path.exists() and self.model_path.stat().st_size > 1000:
            self._model_ok = True
            return True

        try:
            self.model_dir.mkdir(parents=True, exist_ok=True)
            import requests
            logger.info(
                "AIImageUpscaler: descargando %s (%s) desde %s ...",
                self.model_file, self.model, self.model_url,
            )
            resp = requests.get(self.model_url, timeout=DOWNLOAD_TIMEOUT)
            resp.raise_for_status()
            # Protección: un 404/redirect devuelve HTML, no el modelo
            if resp.content[:4] in (b"\x0a\x0a\x0a\x0a", b"<htm", b"404:"):
                logger.error("AIImageUpscaler: descarga devolvió contenido no válido")
                return False
            tmp = self.model_path.with_suffix(".pb.tmp")
            tmp.write_bytes(resp.content)
            tmp.replace(self.model_path)
            self._model_ok = True
            logger.info("AIImageUpscaler: modelo guardado en %s", self.model_path)
            return True
        except Exception as exc:
            logger.error("AIImageUpscaler: fallo al descargar el modelo: %s", exc)
            return False
