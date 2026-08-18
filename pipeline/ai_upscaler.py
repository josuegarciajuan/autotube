"""AIImageUpscaler — upscale local de imágenes IA con OpenCV DNN superres.

Problema: Pollinations (modelo flux) devuelve imágenes a su resolución
nativa 1024×576 aunque se pida 1920×1080. Al escalarlas durante el render
el resultado se ve borroso.

Solución: upscale 2× con el modelo ESPCN_x2 (~86 KB, Red neuronal de
super-resolución de OpenCV) justo después de generar cada imagen, antes de
guardarla en caché. Así el render ya recibe imágenes ≥ 1920×1080.

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

# Modelo ESPCN_x2 (factor 2) — pequeño y rápido en CPU (~1-3 s por imagen).
# Fuente oficial del repo usado por los tutorials de OpenCV dnn_superres.
DEFAULT_MODEL_URL = (
    "https://raw.githubusercontent.com/fannymonori/TF-ESPCN/master/export/ESPCN_x2.pb"
)
DEFAULT_MODEL_FILE = "espcn_x2.pb"
DOWNLOAD_TIMEOUT = 60  # segundos

# Máximo de píxeles que aceptamos upscalear. Por encima de esto el beneficio
# de la super-resolución es marginal y el coste de memoria crece.
MAX_INPUT_DIM = 2560


class AIImageUpscaler:
    """Upscale imágenes con ESPCN_x2 de OpenCV DNN superres (2×).

    Singleton por proceso: la red se carga una única vez y se comparte
    entre todos los proveedores/videos. El modelo se descarga al primer
    uso y queda cacheado en disco.
    """

    _instance: Optional["AIImageUpscaler"] = None

    def __init__(
        self,
        model_url: str = DEFAULT_MODEL_URL,
        model_dir: Optional[Path] = None,
    ) -> None:
        self.model_url = model_url
        if model_dir is None:
            from config import settings
            model_dir = settings.OUTPUT_DIR / "models"
        self.model_dir = Path(model_dir)
        self.model_path = self.model_dir / DEFAULT_MODEL_FILE
        self._net = None          # lazy — cv2.dnn_superres net
        self._model_ok = None     # None = no verificado aún; bool después

    @classmethod
    def get_instance(cls) -> "AIImageUpscaler":
        """Devuelve el singleton compartido del proceso."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── API pública ────────────────────────────────────────────────

    def upscale_to_min(
        self,
        image_path: Path,
        min_width: int,
        min_height: int,
    ) -> bool:
        """Upscalea *image_path* a ≥ (min_width, min_height) si es necesario.

        Sobrescribe el archivo in-place con la versión de mayor resolución.

        Returns:
            ``True`` si la imagen fue upscaleada (o ya cumplía el mínimo y
            no hizo falta), ``False`` si el upscaler no está disponible
            (fallback degradado: la imagen queda como estaba).
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

        # Ya cumple el mínimo → no-op
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
                "AIImageUpscaler: %dx%d → %s (ESPCN_x2, mínimo %dx%d)",
                w, h, image_path.name, min_width, min_height,
            )
            upscaled = net.upsample(img)  # 2× exacto

            # Ajuste final LANCZOS si el 2× no llega al mínimo pedido
            uw, uh = upscaled.shape[1], upscaled.shape[0]
            if uw < min_width or uh < min_height:
                from PIL import Image as PILImage
                import numpy as np
                pil_img = PILImage.fromarray(cv2.cvtColor(upscaled, cv2.COLOR_BGR2RGB))
                ratio = max(min_width / uw, min_height / uh)
                new_size = (int(uw * ratio), int(uh * ratio))
                pil_img = pil_img.resize(new_size, PILImage.LANCZOS)
                upscaled = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

            if not cv2.imwrite(str(image_path), upscaled):
                logger.warning("AIImageUpscaler: fallo al escribir %s", image_path)
                return False
            return True

        except Exception as exc:
            logger.warning("AIImageUpscaler: error de upscale en %s: %s", image_path, exc)
            return False

    # ── Interno ────────────────────────────────────────────────────

    def _get_net(self):
        """Carga (lazy) la red ESPCN. None si no hay modelo o módulo contrib."""
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
            sr.setModel("espcn", 2)
            self._net = sr
            return sr
        except Exception as exc:
            logger.warning("AIImageUpscaler: no se pudo cargar ESPCN: %s", exc)
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
            logger.info("AIImageUpscaler: descargando ESPCN_x2 desde %s ...", self.model_url)
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
