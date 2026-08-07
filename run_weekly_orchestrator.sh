#!/bin/bash
# ===========================================================================
# Weekly CIO Strategy Report Scheduler (Tier 3 — NIM deepseek-v4-pro)
# ===========================================================================
# Runs src/orchestrator.py — aggregates full corpus → CIO 페르소나 거시 자산배분
# 전략 리포트 → obsidian_vault/reports/Grand_Report_YYYY-MM-DD.md 저장.
# Crontab: 0 8 * * 1 (매주 월요일 08:00 KST — 일일 리포트 07:00 이후 최신 반영)
#
# 의존: nvidia-api-proxy(pm2 nvidia-proxy, 6-key NIM rotation) 가동.
# env 오버라이드: TIER3_MODEL(기본 deepseek-ai/deepseek-v4-pro).

# Include NodeJS in PATH for NVM compatibility in cron/background jobs
export PATH="/home/mikey/.nvm/versions/node/v22.22.2/bin:$PATH"

PROJECT_DIR="/home/mikey/global_macro_time_series_knowledge_graph"
LOG_FILE="${PROJECT_DIR}/orchestrator_cron.log"

echo "=== WEEKLY ORCHESTRATOR CRON START: $(date '+%Y-%m-%d %H:%M:%S') ===" >> "${LOG_FILE}"

cd "${PROJECT_DIR}" || {
    echo "❌ Error: Could not change directory to ${PROJECT_DIR}" >> "${LOG_FILE}"
    exit 1
}

# Verify nvidia-api-proxy is alive (Tier 3 NIM backend) — start if down.
if ! curl -sf --max-time 5 http://localhost:8000/health >/dev/null 2>&1; then
    echo "⚠️ nvidia-api-proxy down — restarting via pm2..." >> "${LOG_FILE}"
    pm2 restart nvidia-proxy >/dev/null 2>&1 || \
    pm2 start /usr/bin/python3 --name nvidia-proxy -- /home/mikey/nvidia-api-proxy/proxy_server.py >> "${LOG_FILE}" 2>&1
    sleep 3
    if ! curl -sf --max-time 5 http://localhost:8000/health >/dev/null 2>&1; then
        echo "❌ nvidia-api-proxy unreachable after restart — aborting orchestrator" >> "${LOG_FILE}"
        exit 1
    fi
    echo "✓ nvidia-api-proxy recovered" >> "${LOG_FILE}"
fi

if [ ! -d ".venv" ]; then
    echo "❌ Error: Virtual environment .venv not found in ${PROJECT_DIR}" >> "${LOG_FILE}"
    exit 1
fi

source .venv/bin/activate
# 👑 python -m src.orchestrator — 루트 기준 모듈 실행(sys.path[0]=루트).
# 직접 python src/orchestrator.py 시 sys.path[0]=src/ 가 되어
# `from src import mcp_server` 가 ModuleNotFoundError 발생.
EVENT_LOG_ARGS=()
if [[ -n "${PIPELINE_EVENT_LOG:-}" ]]; then
    EVENT_LOG_ARGS=(--event-log "${PIPELINE_EVENT_LOG}")
fi
python -m src.orchestrator "${EVENT_LOG_ARGS[@]}" >> "${LOG_FILE}" 2>&1

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "✓ WEEKLY ORCHESTRATOR CRON SUCCESS: $(date '+%Y-%m-%d %H:%M:%S')" >> "${LOG_FILE}"
else
    echo "❌ WEEKLY ORCHESTRATOR CRON FAILED WITH EXIT CODE ${EXIT_CODE}: $(date '+%Y-%m-%d %H:%M:%S')" >> "${LOG_FILE}"
fi

echo "=== WEEKLY ORCHESTRATOR CRON END ===" >> "${LOG_FILE}"
echo "" >> "${LOG_FILE}"
exit "${EXIT_CODE}"
