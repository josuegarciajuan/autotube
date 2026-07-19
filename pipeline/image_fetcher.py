"""Image acquisition from Unsplash and Pexels APIs."""
import hashlib
import logging
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from types import SimpleNamespace

import requests

from config import canal2_config as _default_cfg
from config import settings
from pipeline.rate_limiter import TokenBucketRateLimiter

logger = logging.getLogger(__name__)

_SPANISH_TO_ENGLISH: dict[str, str] = {
    "calle": "street",
    "oscura": "dark",
    "oscuro": "dark",
    "lluvia": "rain",
    "lluvioso": "rainy",
    "bajo": "under",
    "ciudad": "city",
    "noche": "night",
    "día": "day",
    "dia": "day",
    "casa": "house",
    "bosque": "forest",
    "mar": "sea",
    "playa": "beach",
    "montaña": "mountain",
    "montañas": "mountains",
    "cielo": "sky",
    "habitación": "room",
    "habitacion": "room",
    "luz": "light",
    "sombra": "shadow",
    "sombras": "shadows",
    "ventana": "window",
    "puerta": "door",
    "camino": "road",
    "río": "river",
    "rio": "river",
    "lago": "lake",
    "árbol": "tree",
    "arbol": "tree",
    "árboles": "trees",
    "arboles": "trees",
    "gente": "people",
    "persona": "person",
    "hombre": "man",
    "mujer": "woman",
    "coche": "car",
    "edificio": "building",
    "atardecer": "sunset",
    "amanecer": "dawn",
    "desierto": "desert",
    "iglesia": "church",
    "hospital": "hospital",
    "oficina": "office",
    "escuela": "school",
    "parque": "park",
    "puente": "bridge",
    "nevado": "snowy",
    "nieve": "snow",
    "niebla": "fog",
    "tormenta": "storm",
    "sol": "sun",
    "luna": "moon",
    "estrellas": "stars",
    "fuego": "fire",
    "agua": "water",
    "piedra": "stone",
    "metal": "metal",
    "madera": "wood",
    "vidrio": "glass",
    "espejo": "mirror",
    "pared": "wall",
    "suelo": "floor",
    "techo": "ceiling",
    "escalera": "staircase",
    "callejón": "alley",
    "callejon": "alley",
    "túnel": "tunnel",
    "tunel": "tunnel",
    "sótano": "basement",
    "sotano": "basement",
    "ático": "attic",
    "atico": "attic",
    "castillo": "castle",
    "prisión": "prison",
    "prision": "prison",
    "cementerio": "cemetery",
    "fábrica": "factory",
    "fabrica": "factory",
    "aeropuerto": "airport",
    "estación": "station",
    "estacion": "station",
    "tren": "train",
    "avión": "airplane",
    "avion": "airplane",
    "barco": "boat",
    "submarino": "submarine",
    "campo": "field",
    "pradera": "meadow",
    "pantano": "swamp",
    "selva": "jungle",
    "isla": "island",
    "acantilado": "cliff",
    "cueva": "cave",
    "volcán": "volcan",
    "volcan": "volcano",
    "guerra": "war",
    "batalla": "battle",
    "soldado": "soldier",
    "ejército": "army",
    "ejercito": "army",
    "rey": "king",
    "reina": "queen",
    "corona": "crown",
    "trono": "throne",
    "espada": "sword",
    "sangre": "blood",
    "cadáver": "corpse",
    "cadaver": "corpse",
    "cadáveres": "corpses",
    "cadaveres": "corpses",
    "entierro": "burial",
    "funeral": "funeral",
    "accidente": "accident",
    "explosión": "explosion",
    "explosion": "explosion",
    "incendio": "fire",
    "terremoto": "earthquake",
    "inundación": "flood",
    "inundacion": "flood",
}


class ImageProvider(ABC):
    """Abstract base for image search providers."""

    @abstractmethod
    def search(self, query: str, n: int = 1, style_modifiers: str = "") -> list[dict]:
        """Search for images matching the query.

        Returns a list of dicts with keys:
            id, url, download_url, photographer, width, height, description
        """
        ...


class UnsplashProvider(ImageProvider):
    """Unsplash API — primary provider. 50 req/hr demo, 1000/hr approved.

    Auth: Authorization: Client-ID {key}
    Endpoint: https://api.unsplash.com/search/photos
    Supports: color=black, orientation=landscape
    """

    BASE_URL = "https://api.unsplash.com/search/photos"

    def __init__(self, access_key: str) -> None:
        if not access_key:
            raise ValueError("Unsplash access key is required")
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Client-ID {access_key}"
        self._rate_limiter = TokenBucketRateLimiter.get("unsplash")
        logger.info("UnsplashProvider initialized")

    def search(self, query: str, n: int = 1, style_modifiers: str = "", orientation: str = "landscape") -> list[dict]:
        if n < 1:
            return []

        # Non-blocking: if rate limit is exhausted, return empty so
        # the caller can immediately fall through to Pexels instead of
        # sleeping for minutes and blocking the entire pipeline.
        if not self._rate_limiter.try_acquire():
            return []

        params: dict = {
            "query": query,
            "per_page": min(n, 30),
            "orientation": orientation,
        }

        try:
            resp = self._session.get(self.BASE_URL, params=params, timeout=15)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "60")
                logger.warning(
                    "Unsplash rate limit hit (429). Retry-After=%s", retry_after
                )
                time.sleep(min(int(retry_after), 60))
                resp = self._session.get(self.BASE_URL, params=params, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Unsplash search failed: %s", exc)
            return []

        data = resp.json()
        results: list[dict] = []
        for photo in data.get("results", []):
            results.append({
                "id": photo.get("id", ""),
                "url": photo.get("links", {}).get("html", ""),
                "download_url": photo.get("urls", {}).get("regular", ""),
                "photographer": (
                    photo.get("user", {}).get("name", "Unknown")
                ),
                "width": photo.get("width", 0),
                "height": photo.get("height", 0),
                "description": (
                    photo.get("description")
                    or photo.get("alt_description", "")
                    or ""
                ),
            })

        logger.info("Unsplash returned %d results for query=%r", len(results), query)
        return results


class PexelsProvider(ImageProvider):
    """Pexels API — fallback. 200 req/hr default.

    Auth: Authorization: {key}
    Endpoint: https://api.pexels.com/v1/search
    """

    BASE_URL = "https://api.pexels.com/v1/search"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("Pexels API key is required")
        self._session = requests.Session()
        self._session.headers["Authorization"] = api_key
        self._rate_limiter = TokenBucketRateLimiter.get("pexels")
        logger.info("PexelsProvider initialized")

    def search(self, query: str, n: int = 1, style_modifiers: str = "", orientation: str = "landscape") -> list[dict]:
        if n < 1:
            return []

        # Non-blocking: skip to fallback if exhausted
        if not self._rate_limiter.try_acquire():
            return []

        params: dict = {
            "query": query,
            "per_page": min(n, 80),
            "orientation": orientation,
        }

        try:
            resp = self._session.get(self.BASE_URL, params=params, timeout=15)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "60")
                logger.warning(
                    "Pexels rate limit hit (429). Retry-After=%s", retry_after
                )
                time.sleep(min(int(retry_after), 60))
                resp = self._session.get(self.BASE_URL, params=params, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Pexels search failed: %s", exc)
            return []

        data = resp.json()
        results: list[dict] = []
        for photo in data.get("photos", []):
            results.append({
                "id": str(photo.get("id", "")),
                "url": photo.get("url", ""),
                "download_url": photo.get("src", {}).get("large2x", "")
                or photo.get("src", {}).get("large", ""),
                "photographer": photo.get("photographer", "Unknown"),
                "width": photo.get("width", 0),
                "height": photo.get("height", 0),
                "description": photo.get("alt", ""),
            })

        logger.info("Pexels returned %d results for query=%r", len(results), query)
        return results


class PixabayImageProvider(ImageProvider):
    """Pixabay Photos API — image provider. Uses same key as Pixabay videos.

    Auth: key is passed as query parameter (?key=...).
    Endpoint: https://pixabay.com/api/
    Rate limit: 100 req/min (authenticated).

    Image resolution strategy:
    - largeImageURL (up to 6000x4000) is the primary download_url —
      needed for high-quality Ken Burns pan/zoom at 1080p output.
    - webformatURL (640px) is kept as fallback_download_url in case
      largeImageURL CDN requires a Referer header and the download fails.
    - Uses imageWidth/imageHeight (full-resolution dimensions) for
      accurate reporting, falling back to webformatWidth/Height.
    """

    BASE_URL = "https://pixabay.com/api/"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("Pixabay API key is required")
        self._api_key = api_key
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        })
        logger.info("PixabayImageProvider initialized (largeImageURL preferred)")

    def search(self, query: str, n: int = 1, style_modifiers: str = "",
               orientation: str = "horizontal") -> list[dict]:
        if n < 1:
            return []

        params: dict = {
            "key": self._api_key,
            "q": query,
            "per_page": max(min(n, 200), 3),  # Pixabay requires 3-200
            "image_type": "photo",
            "orientation": orientation,
        }

        try:
            resp = self._session.get(self.BASE_URL, params=params, timeout=15)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "60")
                logger.warning(
                    "Pixabay rate limit hit (429). Retry-After=%s", retry_after
                )
                time.sleep(min(int(retry_after), 60))
                resp = self._session.get(self.BASE_URL, params=params, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Pixabay image search failed: %s", exc)
            return []

        data = resp.json()
        results: list[dict] = []
        for photo in data.get("hits", []):
            large_url = photo.get("largeImageURL", "")
            web_url = photo.get("webformatURL", "")

            # Prefer largeImageURL for high-resolution Ken Burns zoom.
            # Fall back to webformatURL if largeImageURL is unavailable.
            download_url = large_url or web_url
            fallback_download_url = web_url if large_url and web_url else None

            # Use imageWidth/imageHeight for full-resolution dimensions
            img_w = photo.get("imageWidth", 0)
            img_h = photo.get("imageHeight", 0)
            if not img_w or not img_h:
                img_w = photo.get("webformatWidth", 0)
                img_h = photo.get("webformatHeight", 0)

            results.append({
                "id": str(photo.get("id", "")),
                "url": photo.get("pageURL", ""),
                "download_url": download_url,
                "fallback_download_url": fallback_download_url,
                "photographer": photo.get("user", "Unknown"),
                "width": img_w,
                "height": img_h,
                "description": photo.get("tags", ""),
            })

        logger.info("Pixabay images returned %d results for query=%r",
                     len(results), query)
        return results


class ImageFetcher:
    """Orchestrates image fetching with primary/fallback providers."""

    def __init__(self, config: SimpleNamespace | None = None) -> None:
        self.primary: UnsplashProvider | None = None
        self.fallback: PixabayImageProvider | None = None
        self._config = config or _default_cfg

        if settings.UNSPLASH_ACCESS_KEY:
            self.primary = UnsplashProvider(settings.UNSPLASH_ACCESS_KEY)
        else:
            logger.warning(
                "UNSPLASH_ACCESS_KEY not set — primary provider disabled"
            )

        if settings.PIXABAY_API_KEY:
            self.fallback = PixabayImageProvider(settings.PIXABAY_API_KEY)
        else:
            logger.warning(
                "PIXABAY_API_KEY not set — fallback provider disabled"
            )

        if self.primary is None and self.fallback is None:
            logger.error("No image providers configured! Set UNSPLASH_ACCESS_KEY or PIXABAY_API_KEY")

    def fetch_for_scene(self, scene_description: str, n: int = None) -> list[Path]:
        """Convert scene description to search query, fetch images, download locally.

        Args:
            scene_description: e.g. "[ESCENA: calle oscura bajo lluvia]"
            n: Number of images to fetch (defaults to IMAGES_PER_SCENE from config).

        Returns:
            List of local Path objects to downloaded images.
        """
        if n is None:
            n = int(getattr(self._config, "IMAGES_PER_SCENE", 5))
        query = self._scene_to_query(scene_description)
        logger.info("Searching for scene: %r → query: %r (n=%d)", scene_description, query, n)
        results: list[dict] = []

        if self.primary is not None:
            try:
                results = self.primary.search(query, n, self._config.IMAGE_STYLE_MODIFIERS)
            except Exception as exc:
                logger.warning("Primary provider (Unsplash) failed: %s", exc)

        if not results and self.fallback is not None:
            try:
                results = self.fallback.search(query, n, self._config.IMAGE_STYLE_MODIFIERS)
            except Exception as exc:
                logger.error("Fallback provider (Pixabay) also failed: %s", exc)

        if not results:
            logger.error("No images found for scene: %r", scene_description)
            return []

        paths: list[Path] = []
        for img in results[:n]:
            download_url = img.get("download_url", "")
            if not download_url:
                logger.warning("No download_url for image id=%s — skipping", img.get("id"))
                continue
            img_id = str(img.get("id", hashlib.md5(download_url.encode()).hexdigest()[:12]))
            try:
                paths.append(self._download(download_url, f"{img_id}.jpg"))
            except Exception as exc:
                logger.error("Failed to download image %s: %s", img_id, exc)

        return paths

    def fetch_for_script(self, escenas: list[str]) -> list[list[Path]]:
        """Fetch images for each scene in a script.

        Args:
            escenas: List of scene descriptions.

        Returns:
            List of lists, where each inner list contains Path objects for that scene.
        """
        results: list[list[Path]] = []
        for i, escena in enumerate(escenas):
            logger.info("Fetching images for scene %d/%d", i + 1, len(escenas))
            images = self.fetch_for_scene(escena)
            results.append(images)
            if i < len(escenas) - 1:
                time.sleep(1.0)

        return results

    def _scene_to_query(self, scene_desc: str) -> str:
        """Convert Spanish scene description to a short, focused search query.

        Takes the first 6–8 meaningful words (skipping stopwords), does
        NOT translate (Unsplash/Pexels support Spanish), and appends only
        minimal style modifiers to avoid biasing results.
        """
        clean = re.sub(r"\[ESCENA:\s*", "", scene_desc, flags=re.IGNORECASE)
        clean = re.sub(r"\]", "", clean)
        clean = clean.strip()

        # Spanish stopwords to skip
        stopwords = {"un", "una", "el", "la", "los", "las", "de", "del", "en",
                     "con", "que", "y", "o", "a", "por", "para", "se", "su",
                     "al", "lo", "como", "más", "pero", "sus", "le", "ya",
                     "este", "esta", "entre", "muy", "hay", "vez", "todo",
                     "desde", "donde", "sobre", "tan", "si", "no", "me", "mi",
                     "es", "ha", "tu", "nos", "él", "ella", "sin", "tras"}

        meaningful = [w for w in clean.split() if w.lower().rstrip(".,;:!?¡¿") not in stopwords]
        # Take first 7 meaningful words for a focused query
        short = " ".join(meaningful[:7])

        # Minimal style suffix — just enough to get quality cinematic images
        style = "cinematic photography, dramatic lighting, 16:9"
        return f"{short}. {style}"

    def _download(self, url: str, filename: str) -> Path:
        """Download image to IMAGES_DIR. Skips if already cached.

        Args:
            url: Direct image download URL.
            filename: Local filename (e.g. "abc123.jpg").

        Returns:
            Path to the local image file.
        """
        filepath = settings.IMAGES_DIR / filename

        if filepath.exists():
            logger.info("Image already cached: %s", filepath)
            return filepath

        resp = requests.get(url, timeout=30, stream=True)
        resp.raise_for_status()

        settings.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        filepath.write_bytes(resp.content)
        logger.info("Downloaded: %s (%d bytes)", filepath, len(resp.content))
        return filepath
