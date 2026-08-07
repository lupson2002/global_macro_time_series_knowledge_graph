#!/bin/bash
# ===========================================================================
# 6-Hour Mini-Batch Pipeline Scheduler Script
# ===========================================================================
# Auto-activates python virtualenv and runs main.py with --fetch_latest mode.
# Append execution logs to pipeline_cron.log.
# Crontab: 0 */6 * * * (every 6 hours)

# Include NodeJS in PATH for NVM compatibility in cron/background jobs
export PATH="/home/mikey/.nvm/versions/node/v22.22.2/bin:$PATH"

PROJECT_DIR="/home/mikey/global_macro_time_series_knowledge_graph"
LOG_FILE="${PROJECT_DIR}/pipeline_cron.log"

echo "=== PIPELINE CRON START: $(date '+%Y-%m-%d %H:%M:%S') ===" >> "${LOG_FILE}"

# Change to project root directory
cd "${PROJECT_DIR}" || {
    echo "❌ Error: Could not change directory to ${PROJECT_DIR}" >> "${LOG_FILE}"
    exit 1
}

# Verify nvidia-api-proxy is alive (Tier 1 LLM backend) — start if down.
if ! curl -sf --max-time 5 http://localhost:8000/health >/dev/null 2>&1; then
    echo "⚠️ nvidia-api-proxy down — restarting via pm2..." >> "${LOG_FILE}"
    pm2 restart nvidia-proxy >/dev/null 2>&1 || \
    pm2 start /usr/bin/python3 --name nvidia-proxy -- /home/mikey/nvidia-api-proxy/proxy_server.py >> "${LOG_FILE}" 2>&1
    sleep 3
    if ! curl -sf --max-time 5 http://localhost:8000/health >/dev/null 2>&1; then
        echo "❌ nvidia-api-proxy unreachable after restart — aborting pipeline" >> "${LOG_FILE}"
        exit 1
    fi
    echo "✓ nvidia-api-proxy recovered" >> "${LOG_FILE}"
fi

# Check virtual environment
if [ ! -d ".venv" ]; then
    echo "❌ Error: Virtual environment .venv not found in ${PROJECT_DIR}" >> "${LOG_FILE}"
    exit 1
fi

# Activate virtualenv and run orchestrator in RSS mode
source .venv/bin/activate
EVENT_LOG_ARGS=()
if [[ -n "${PIPELINE_EVENT_LOG:-}" ]]; then
    EVENT_LOG_ARGS=(--event-log "${PIPELINE_EVENT_LOG}")
fi
python main.py --fetch_latest --max_age_hours 36 "${EVENT_LOG_ARGS[@]}" >> "${LOG_FILE}" 2>&1

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "✓ PIPELINE CRON SUCCESS: $(date '+%Y-%m-%d %H:%M:%S')" >> "${LOG_FILE}"
else
    echo "❌ PIPELINE CRON FAILED WITH EXIT CODE ${EXIT_CODE}: $(date '+%Y-%m-%d %H:%M:%S')" >> "${LOG_FILE}"
fi

echo "=== PIPELINE CRON END ===" >> "${LOG_FILE}"
echo "" >> "${LOG_FILE}"
