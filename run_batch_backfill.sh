#!/bin/bash
# ===========================================================================
# Ver 3.1 — Unlimited Batch Backfill Scheduler
# ===========================================================================
# nvidia-api-proxy (localhost:8000, 6-key NIM rotation) has no RPD ceiling.
# Runs a historical backfill across the channel pool (configs/channels.json).
# Tier 1 (6 channels) is the only _enabled tier as of 2026-06-09; Tier 2/3/4 are
# retained in the config but skipped unless include_disabled is forced.
# Chunks to safe 100-video batches, throttled for YouTube's per-IP rate-limit.
#
# Usage:
#   ./run_batch_backfill.sh                          # default: 60d, Tier 1 only, 100/chunk
#   ./run_batch_backfill.sh 30d                      # 30-day window
#   ./run_batch_backfill.sh 60d all                  # 60d, all _enabled tiers (Tier 1 = 6 channels)
#   ./run_batch_backfill.sh 30d tier_1_highest_density
#   ./run_batch_backfill.sh 60d all 250              # custom chunk size
#   ./run_batch_backfill.sh 60d all 100 "UCxxx,UCyyy"  # custom channel list (overrides tiers)
#
# Why chunks?  A single run is bounded so cron can interleave it with the
# 2-hour RSS poller and so we never starve Ollama/YouTube under sustained load.
# Drain the full backlog by chaining runs: cron entry runs every 4-6 hours.

set -euo pipefail

# Include NodeJS in PATH for NVM compatibility
export PATH="/home/mikey/.nvm/versions/node/v22.22.2/bin:$PATH"

PROJECT_DIR="/home/mikey/global_macro_time_series_knowledge_graph"
LOG_FILE="${PROJECT_DIR}/pipeline_cron.log"
WINDOW="${1:-60d}"
TIERS_ARG="${2:-tier_1_highest_density}"   # Default to Tier 1 for safety
CHUNK_SIZE="${3:-100}"
CUSTOM_CHANNELS="${4:-}"

# Map window string → hours
case "$WINDOW" in
    7d)  HOURS=168 ;;
    14d) HOURS=336 ;;
    30d) HOURS=720 ;;
    60d) HOURS=1440 ;;
    90d) HOURS=2160 ;;
    *)
        # Raw hours if user passed a number (e.g. "12" for 12h)
        HOURS="$WINDOW"
        ;;
esac

echo "" >> "${LOG_FILE}"
echo "=== BATCH BACKFILL START: $(date '+%Y-%m-%d %H:%M:%S') (window=${HOURS}h / ${WINDOW}, tiers=${TIERS_ARG}, chunk=${CHUNK_SIZE}) ===" >> "${LOG_FILE}"

cd "${PROJECT_DIR}" || {
    echo "❌ Could not cd to ${PROJECT_DIR}" >> "${LOG_FILE}"
    exit 1
}

if [ ! -d ".venv" ]; then {
    echo "❌ .venv not found" >> "${LOG_FILE}"
    exit 1
}; fi

source .venv/bin/activate

# Build target args
TARGET_ARGS="--fetch_latest --max_age_hours ${HOURS} --max_videos ${CHUNK_SIZE}"

if [ -n "${CUSTOM_CHANNELS}" ]; then
    # Custom channel list overrides tier filter
    TARGET_ARGS="${TARGET_ARGS} --channel_id ${CUSTOM_CHANNELS}"
elif [ "${TIERS_ARG}" = "all" ]; then
    TARGET_ARGS="${TARGET_ARGS} --tiers all"
else
    TARGET_ARGS="${TARGET_ARGS} --tiers ${TIERS_ARG}"
fi

# 3-second inter-video delay = ~20 videos/min; well within YT rate-limit.
# --llm_delay 0 because nvidia-api-proxy rotates 6 NIM keys (rate-limit-free).
# --overwrite so backfill can refresh records that may have been updated.
python main.py \
    ${TARGET_ARGS} \
    --ingest_delay 3 \
    --llm_delay 0 \
    --overwrite >> "${LOG_FILE}" 2>&1

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "✓ BATCH BACKFILL SUCCESS: $(date '+%Y-%m-%d %H:%M:%S')" >> "${LOG_FILE}"
else
    echo "❌ BATCH BACKFILL FAILED WITH EXIT ${EXIT_CODE}: $(date '+%Y-%m-%d %H:%M:%S')" >> "${LOG_FILE}"
fi

echo "=== BATCH BACKFILL END ===" >> "${LOG_FILE}"
echo "" >> "${LOG_FILE}"
