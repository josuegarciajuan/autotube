# AUTOTUBE — P05 controlled canary profile (non-secret, reversible)
#
# This file documents the ONLY approved canary profile. It is NOT loaded by
# the production service and does not change any code default.
#
# Activation must be job-scoped:
#   * P05_CANARY_JOB_IDS must be non-empty, and
#   * AUTOTUBE_JOB_ID must match one of its entries.
#
# Rollback = stop passing this file / unset these variables. Production
# defaults remain MEDIA_SELECTOR=legacy, P04_SOURCE_ROUTER=0, P05_INTEGRATED=0.

# Base selector stays legacy; P04/P05 ride on the router hook.
MEDIA_SELECTOR=legacy
MEDIA_SELECTOR_SHADOW=0

# P04 source router + P05 integrated rerank.
P04_SOURCE_ROUTER=1
P05_INTEGRATED=1

# Visual profile: preview only (FULL_VISUAL is never enabled globally).
P05_VISUAL_PROFILE=PREVIEW_VISUAL
P05_VISUAL_TOP_N=8
P05_MAX_VISUAL_CANDIDATES=12
P05_ADAPTIVE_BUDGET=1
P05_REAL_THRESHOLD=0.26
P05_AI_THRESHOLD=0.26
P05_AI_VALIDATION_REQUIRED=1

# No live AI generation during the canary.
P04_AI_ENABLED=0
P04_AI_MAX_PER_VIDEO=0

# Visual embedding cache (local only).
P02_VISUAL_CACHE_ENABLED=1

# Job scope token; the runner replaces this with a unique canary token and
# sets AUTOTUBE_JOB_ID to the same value.
P05_CANARY_JOB_IDS=canary
