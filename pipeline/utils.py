"""Shared utility functions for the pipeline module."""

import re


def slugify_filename(text: str, max_len: int = 100) -> str:
    """Sanitize a title into a filesystem-safe slug for YouTube SEO naming.

    Rules:
    - Lowercase
    - Keep only [a-z0-9_-]
    - Collapse consecutive underscores
    - Trim to max_len chars (preserves last complete word-like segment)

    Args:
        text: Raw title text (e.g. "El Misterio del Triángulo de las Bermudas").
        max_len: Maximum length of the resulting slug (default 100).

    Returns:
        Slug like "el_misterio_del_triangulo_de_las_bermudas".
    """
    slug = text.lower().strip()
    # Normalize accented chars → ASCII equivalents
    slug = slug.replace("á", "a").replace("é", "e").replace("í", "i")
    slug = slug.replace("ó", "o").replace("ú", "u").replace("ü", "u")
    slug = slug.replace("ñ", "n")
    slug = slug.replace("Á", "a").replace("É", "e").replace("Í", "i")
    slug = slug.replace("Ó", "o").replace("Ú", "u").replace("Ü", "u")
    slug = slug.replace("Ñ", "n")
    # Replace other non-ASCII with nothing
    slug = re.sub(r'[^\x00-\x7F]+', '', slug)
    # Keep only alphanumeric, space, hyphen, underscore
    slug = re.sub(r'[^a-z0-9\s_-]', '', slug)
    # Treat hyphens as word separators (same as spaces)
    slug = re.sub(r'[\s-]+', '_', slug)
    # Collapse consecutive underscores
    slug = re.sub(r'_+', '_', slug)
    # Strip leading/trailing separators
    slug = slug.strip('_-')
    # Trim to max_len
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip('_-')
    return slug or "video"
