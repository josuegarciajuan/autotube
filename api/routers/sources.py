"""Source health monitoring endpoints."""
from fastapi import APIRouter
from scrapers.base import get_source_health

router = APIRouter()


@router.get("/health")
def sources_health():
    """Return health status of all scrape sources.

    Each entry includes failure count, last error, and whether the
    source has been degraded (10+ consecutive failures).
    """
    health = get_source_health()
    return {
        "sources": health,
        "degraded": [k for k, v in health.items() if v.get("degraded")],
        "total_tracked": len(health),
    }
