"""P04-N — perceptual + canonical duplicate detection.

Extends dedup beyond provider URLs/IDs:

* canonical provider key (existing P01);
* normalised media URL (catches the same CDN file under tracking params);
* dHash (fast, robust to resizing/compression);
* pHash (DCT-based, handles crops/colour shifts better);
* optional sampled frame hashes for video.

The implementation depends only on Pillow + numpy (already in the runtime), so
there is no new production dependency and the offline tests stay cheap.
Thresholds are configurable; a duplicate is a hard veto, a near-duplicate can
be rejected or merely penalised by the coherence layer.
"""

from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:  # Pillow/numpy are existing runtime dependencies.
    import numpy as np
    from PIL import Image
except Exception:  # pragma: no cover - import guard for minimal environments
    np = None  # type: ignore
    Image = None  # type: ignore


HASH_SIZE = 8
PHASH_SIZE = 8
PHASH_FACTOR = 4


def _open_image(source: Any):
    if Image is None:
        return None
    try:
        if isinstance(source, (str, Path)):
            return Image.open(source).convert("L")
        if isinstance(source, (bytes, bytearray)):
            return Image.open(io.BytesIO(source)).convert("L")
        if hasattr(source, "convert"):
            return source.convert("L")
    except Exception:
        return None
    return None


def _bits_to_hex(bits: Iterable[bool]) -> str:
    value = 0
    count = 0
    out: list[str] = []
    for bit in bits:
        if bit:
            value |= 1 << (7 - count)
        count += 1
        if count == 8:
            out.append(f"{value:02x}")
            value = 0
            count = 0
    if count:
        out.append(f"{value << (8 - count):02x}")
    return "".join(out)


def dhash(source: Any, hash_size: int = HASH_SIZE) -> str:
    """Difference hash: ``hash_size*hash_size`` bits (64 by default)."""
    image = _open_image(source)
    if image is None:
        return ""
    try:
        resized = image.resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
        pixels = np.asarray(resized, dtype=np.int16)
        diff = pixels[:, 1:] > pixels[:, :-1]
        return _bits_to_hex(diff.flatten().tolist())
    except Exception:
        return ""


def ahash(source: Any, hash_size: int = HASH_SIZE) -> str:
    image = _open_image(source)
    if image is None:
        return ""
    try:
        resized = image.resize((hash_size, hash_size), Image.Resampling.LANCZOS)
        pixels = np.asarray(resized, dtype=np.float32)
        threshold = float(pixels.mean())
        return _bits_to_hex((pixels.flatten() > threshold).tolist())
    except Exception:
        return ""


def phash(source: Any, hash_size: int = PHASH_SIZE, highfreq_factor: int = PHASH_FACTOR) -> str:
    """DCT-based perceptual hash (no scipy dependency)."""
    image = _open_image(source)
    if image is None:
        return ""
    try:
        img_size = hash_size * highfreq_factor
        resized = image.resize((img_size, img_size), Image.Resampling.LANCZOS)
        pixels = np.asarray(resized, dtype=np.float32)
        # 2-D DCT-II via separable matrix multiplication.
        n = img_size
        k = np.arange(n)
        basis = np.cos(math.pi * (2 * k[None, :] + 1) * k[:, None] / (2 * n))
        basis = basis * np.sqrt(2.0 / n)
        basis[0, :] *= math.sqrt(0.5)
        coeffs = basis @ pixels @ basis.T
        low = coeffs[:hash_size, :hash_size].flatten()
        # Median excludes the DC term to avoid brightness dominating.
        median = float(np.median(low[1:]))
        return _bits_to_hex((low > median).tolist())
    except Exception:
        return ""


def image_hash(source: Any) -> str:
    d = dhash(source)
    p = phash(source)
    if not d and not p:
        return ""
    return f"d{d}:p{p}"


def hamming(hex_a: str, hex_b: str) -> int:
    """Hamming distance between two equal-length hex hashes (99 if invalid)."""
    if not hex_a or not hex_b:
        return 99
    try:
        a = int(hex_a, 16)
        b = int(hex_b, 16)
    except ValueError:
        return 99
    length = max(len(hex_a), len(hex_b)) * 4
    value = (a ^ b) & ((1 << length) - 1)
    return int(value.bit_count())


def parse_image_hash(value: str) -> tuple[str, str]:
    text = str(value or "")
    d = p = ""
    for part in text.split(":"):
        if part.startswith("d"):
            d = part[1:]
        elif part.startswith("p"):
            p = part[1:]
    return d, p


def _cosine(left: Iterable[float], right: Iterable[float]) -> float:
    a = list(left or [])
    b = list(right or [])
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(float(a[i]) * float(b[i]) for i in range(n))
    norm_a = math.sqrt(sum(float(a[i]) ** 2 for i in range(n)))
    norm_b = math.sqrt(sum(float(b[i]) ** 2 for i in range(n)))
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


def embedding_near_duplicate(
    left: Iterable[float], right: Iterable[float], threshold: float = 0.97,
) -> bool:
    """Near-duplicate check for callers that already have visual embeddings."""
    return _cosine(left, right) >= float(threshold)


def url_fingerprint(url: str) -> str:
    base = str(url or "").split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()
    if not base:
        return ""
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:20]


@dataclass
class DuplicateMatch:
    key: str = ""
    reason: str = ""
    distance: int = 0
    matched_kind: str = ""

    @property
    def is_duplicate(self) -> bool:
        return bool(self.key and self.reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "reason": self.reason,
            "distance": int(self.distance),
            "matched_kind": self.matched_kind,
        }


@dataclass
class PerceptualDuplicateIndex:
    """Bounded in-memory index for one video/project."""

    phash_threshold: int = 6
    dhash_threshold: int = 8
    max_entries: int = 2000
    _canonical: dict[str, str] = field(default_factory=dict, repr=False)
    _urls: dict[str, str] = field(default_factory=dict, repr=False)
    _hashes: list[tuple[str, str, str]] = field(default_factory=list, repr=False)  # (key, d, p)
    _frame_hashes: list[tuple[str, str]] = field(default_factory=list, repr=False)
    _embeddings: list[tuple[str, list[float]]] = field(default_factory=list, repr=False)
    embedding_threshold: float = 0.97
    stats: dict[str, int] = field(default_factory=lambda: {
        "checked": 0, "canonical_duplicates": 0, "url_duplicates": 0,
        "perceptual_duplicates": 0, "frame_duplicates": 0,
        "embedding_duplicates": 0,
    })

    def _remember_canonical(self, key: str) -> None:
        if len(self._canonical) >= self.max_entries:
            first = next(iter(self._canonical))
            self._canonical.pop(first, None)
        self._canonical[key] = key

    def add(
        self,
        *,
        key: str,
        urls: Iterable[str] = (),
        hash_value: str = "",
        frame_hashes: Iterable[str] = (),
        embedding: Iterable[float] | None = None,
    ) -> None:
        if key:
            self._remember_canonical(key)
        for url in urls:
            fingerprint = url_fingerprint(url)
            if fingerprint:
                self._urls[fingerprint] = key
        d, p = parse_image_hash(hash_value)
        if d or p:
            self._hashes.append((key, d, p))
        for frame in frame_hashes:
            if frame:
                self._frame_hashes.append((key, frame))
        if embedding:
            self._embeddings.append((key, [float(v) for v in embedding]))

    def check(
        self,
        *,
        key: str = "",
        urls: Iterable[str] = (),
        hash_value: str = "",
        frame_hashes: Iterable[str] = (),
        embedding: Iterable[float] | None = None,
        perceptual: bool = True,
    ) -> DuplicateMatch:
        self.stats["checked"] += 1
        if key and key in self._canonical:
            self.stats["canonical_duplicates"] += 1
            return DuplicateMatch(key=key, reason="canonical_key", matched_kind="canonical")
        for url in urls:
            fingerprint = url_fingerprint(url)
            if fingerprint and fingerprint in self._urls:
                self.stats["url_duplicates"] += 1
                return DuplicateMatch(
                    key=self._urls[fingerprint], reason="same_media_url",
                    matched_kind="url",
                )
        if perceptual:
            d, p = parse_image_hash(hash_value)
            if d or p:
                for other_key, other_d, other_p in self._hashes:
                    if other_key == key:
                        continue
                    pd = hamming(p, other_p) if p and other_p else 99
                    dd = hamming(d, other_d) if d and other_d else 99
                    if pd <= self.phash_threshold or dd <= self.dhash_threshold:
                        self.stats["perceptual_duplicates"] += 1
                        return DuplicateMatch(
                            key=other_key,
                            reason="perceptual_near_duplicate",
                            distance=min(pd, dd),
                            matched_kind="perceptual",
                        )
            for frame in frame_hashes:
                for other_key, other_frame in self._frame_hashes:
                    if other_key == key:
                        continue
                    if hamming(frame, other_frame) <= self.phash_threshold:
                        self.stats["frame_duplicates"] += 1
                        return DuplicateMatch(
                            key=other_key, reason="video_frame_near_duplicate",
                            matched_kind="frame",
                        )
            if embedding:
                for other_key, other_vec in self._embeddings:
                    if other_key == key:
                        continue
                    if _cosine(embedding, other_vec) >= self.embedding_threshold:
                        self.stats["embedding_duplicates"] += 1
                        return DuplicateMatch(
                            key=other_key, reason="embedding_near_duplicate",
                            matched_kind="embedding",
                        )
        return DuplicateMatch()

    def is_duplicate(self, **kwargs: Any) -> bool:
        return self.check(**kwargs).is_duplicate

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": len(self._canonical),
            "hashes": len(self._hashes),
            "frame_hashes": len(self._frame_hashes),
            "embeddings": len(self._embeddings),
            "thresholds": {
                "phash": self.phash_threshold,
                "dhash": self.dhash_threshold,
            },
            "stats": dict(self.stats),
        }
