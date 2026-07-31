#!/bin/bash
# regen_recent_batch.sh — Regenera thumbnails de los últimos 3 días con rate-limiting
#
# Procesa los videos de canal2, canal4, canal5 publicados en los últimos 3 días.
# Espera 60s entre cada video para no saturar Pollo AI.

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

# Videos a regenerar (14 videos de los últimos 3 días, con youtube_id y published)
VIDEO_IDS=(
    # canal2 (4 videos)
    967  964  956  930
    # canal4 (5 videos)
    1023 1022 955  948  946
    # canal5 (5 videos)
    1382 966  957  943  932
)

TOTAL=${#VIDEO_IDS[@]}
SUCCESS=0
FAILED_GEN=0
FAILED_UPLOAD=0

echo "╔═════════════════════════════════════════════════════╗"
echo "║  Batch Thumbnail Regeneration + YouTube Upload       ║"
echo "║  Videos: $TOTAL | Delay: 60s between videos            ║"
echo "╚═════════════════════════════════════════════════════╝"
echo ""

START_TIME=$(date +%s)

for i in "${!VIDEO_IDS[@]}"; do
    VID="${VIDEO_IDS[$i]}"
    CURRENT=$((i + 1))

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "[$CURRENT/$TOTAL] Processing video #$VID (started at $(date '+%H:%M:%S'))"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    if python3 scripts/regenerate_and_upload_thumbnails.py "$VID" 2>&1; then
        SUCCESS=$((SUCCESS + 1))
        echo "  ✅ [$CURRENT/$TOTAL] Video #$VID — DONE"
    else
        exit_code=$?
        if [ $exit_code -eq 1 ]; then
            FAILED_GEN=$((FAILED_GEN + 1))
            echo "  ❌ [$CURRENT/$TOTAL] Video #$VID — GENERATION FAILED"
        elif [ $exit_code -eq 2 ]; then
            FAILED_UPLOAD=$((FAILED_UPLOAD + 1))
            echo "  ⚠️  [$CURRENT/$TOTAL] Video #$VID — UPLOAD FAILED (thumbnail regenerated)"
        fi
    fi

    # Rate-limiting: wait 60s between videos UNLESS it's the last one
    if [ "$CURRENT" -lt "$TOTAL" ]; then
        REMAINING=$((TOTAL - CURRENT))
        echo ""
        echo "  ⏳ Rate-limiting: waiting 60s before next video ($REMAINING remaining)..."
        sleep 60
    fi
done

END_TIME=$(date +%s)
DURATION=$(( (END_TIME - START_TIME) / 60 ))

echo ""
echo "╔═════════════════════════════════════════════════════╗"
echo "║  BATCH COMPLETE                                      ║"
echo "║  Duration: ${DURATION} min                              ║"
echo "║  ✅ Success:          $SUCCESS/$TOTAL"
echo "║  ❌ Generation failed: $FAILED_GEN/$TOTAL"
echo "║  ⚠️  Upload failed:     $FAILED_UPLOAD/$TOTAL"
echo "╚═════════════════════════════════════════════════════╝"

# Return non-zero if any failures
if [ "$FAILED_GEN" -gt 0 ] || [ "$FAILED_UPLOAD" -gt 0 ]; then
    exit 1
fi
exit 0
