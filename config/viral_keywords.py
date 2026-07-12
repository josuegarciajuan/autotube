"""Viral Mirror: English niche keywords per channel for YouTube search.

Each channel maps its Spanish niche to English search queries used to discover
viral videos in English-speaking markets. The queries are designed to find
high-performing content that can be mirrored (translated + adapted) for the
Spanish-speaking audience.

Usage:
    from config.viral_keywords import NICHE_KEYWORDS_ENG

    keywords = NICHE_KEYWORDS_ENG.get("canal5", NICHE_KEYWORDS_ENG["default"])
"""

# ── Niche Keywords (English) ──────────────────────────────────────────────

NICHE_KEYWORDS_ENG: dict[str, list[str]] = {
    # Canal 5: Anomalías Médicas → Medical anomalies / rare diseases
    "canal5": [
        # Direct medical anomalies
        "medical anomalies",
        "rare medical cases",
        "mysterious diseases",
        "unexplained medical conditions",
        "medical mysteries",
        "rare diseases explained",
        "medical phenomena science can't explain",
        "bizarre medical conditions",

        # Viral hook formats (what works on YouTube)
        "most shocking medical cases",
        "doctors couldn't explain this",
        "rarest diseases in the world",
        "medical cases that changed science",
        "patients who baffled doctors",
        "unexplained recoveries medical",
        "medical miracles true stories",
        "strangest syndromes",

        # Broad niche
        "rare genetic disorders",
        "weird medical conditions",
        "undiagnosed diseases documentary",
        "medical documentary rare cases",
    ],

    # Canal 2: Sincronías → Mysteries, coincidences, miracles
    "canal2": [
        "unexplained mysteries",
        "incredible coincidences",
        "synchronicity explained",
        "miracles caught on camera",
        "mind blowing coincidences",
        "strange synchronicities",
        "real life miracles",
        "unexplained phenomena",
        "incredible true stories",
        "mysteries science can't explain",
        "paranormal stories real",
        "strange but true stories",
    ],

    # Canal 3: Civilizaciones Olvidadas → Lost civilizations, ancient mysteries
    "canal3": [
        "lost civilizations",
        "ancient mysteries",
        "forgotten civilizations",
        "ancient technology",
        "archaeological discoveries",
        "ancient ruins unexplained",
        "lost cities found",
        "ancient aliens documentary",
        "prehistoric civilizations",
        "mysterious archaeological sites",
        "ancient artifacts unexplained",
        "hidden history documentary",
    ],

    # Canal 4: Expediciones sin Retorno → Survival, expeditions, disappearances
    "canal4": [
        "survival stories",
        "expeditions gone wrong",
        "unexplained disappearances",
        "survival documentary",
        "lost in the wilderness",
        "expedition mysteries",
        "true survival stories",
        "missing explorers",
        "wilderness survival documentary",
        "deadliest expeditions",
        "survival against all odds",
        "mysterious disappearances documentary",
    ],

    # Default: broad viral-friendly queries (used when channel has no specific mapping)
    "default": [
        "viral documentary",
        "unexplained phenomena",
        "incredible true stories",
        "amazing discoveries",
        "mysteries solved",
        "unbelievable true stories",
        "top 10 unexplained",
    ],
}


# ── Viral scoring thresholds ─────────────────────────────────────────────

# Minimum views for a video to be considered a viral candidate
VIRAL_MIN_VIEWS: int = 500_000

# Maximum days since publication (videos older than this are discarded)
VIRAL_MAX_AGE_DAYS: int = 30

# Max queries per search session (per channel, per day)
VIRAL_MAX_QUERIES: int = 8

# Results per query (yt-dlp returns up to this many)
VIRAL_RESULTS_PER_QUERY: int = 15

# Max candidates to store per channel (drops oldest/lowest-score if exceeded)
VIRAL_MAX_CANDIDATES: int = 20
