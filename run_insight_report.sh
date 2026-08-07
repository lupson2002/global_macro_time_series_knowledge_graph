#!/bin/bash
# ===========================================================================
# Weekly Insight Report Scheduler (정기 인사이트 리포트 + 메일 발송)
# ===========================================================================
# Runs scripts/insight_report.py — 크로스 매트릭스 + 지식그래프 + RAG 인사이트 취합
# → 마크다운 + plotly 대시보드 → Gmail SMTP 메일 발송.
# Crontab: 0 5 * * 5 (매주 금요일 05:00 KST — 주간 마감 후 새벽 정기 발송)
#
# 의존: nvidia-api-proxy(pm2 nvidia-proxy) — RAG/LLM 인사이트 산출용.
# env: GMAIL_USER / GMAIL_APP_PASSWORD (.env), NIM_BASE_URL.

# Include NodeJS in PATH for NVM compatibility in cron/background jobs
export PATH="/home/mikey/.nvm/versions/node/v22.22.2/bin:$PATH"

PROJECT_DIR="/home/mikey/global_macro_time_series_knowledge_graph"
LOG_FILE="${PROJECT_DIR}/insight_report_cron.log"

echo "=== WEEKLY INSIGHT REPORT CRON START: $(date '+%Y-%m-%d %H:%M:%S') ===" >> "${LOG_FILE}"

cd "${PROJECT_DIR}" || {
    echo "❌ Error: Could not change directory to ${PROJECT_DIR}" >> "${LOG_FILE}"
    exit 1
}

# Verify nvidia-api-proxy is alive (RAG/LLM 인사이트 backend) — start if down.
if ! curl -sf --max-time 5 http://localhost:8000/health >/dev/null 2>&1; then
    echo "⚠️ nvidia-api-proxy down — restarting via pm2..." >> "${LOG_FILE}"
    pm2 restart nvidia-proxy >/dev/null 2>&1 || \
    pm2 start /usr/bin/python3 --name nvidia-proxy -- /home/mikey/nvidia-api-proxy/proxy_server.py >> "${LOG_FILE}" 2>&1
    sleep 3
    if ! curl -sf --max-time 5 http://localhost:8000/health >/dev/null 2>&1; then
        echo "❌ nvidia-api-proxy unreachable after restart — aborting insight report" >> "${LOG_FILE}"
        exit 1
    fi
    echo "✓ nvidia-api-proxy recovered" >> "${LOG_FILE}"
fi

if [ ! -d ".venv" ]; then
    echo "❌ Error: Virtual environment .venv not found in ${PROJECT_DIR}" >> "${LOG_FILE}"
    exit 1
fi

source .venv/bin/activate
# scripts/insight_report.py — 전체(크로스매트릭스+지식그래프+RAG 인사이트) + 메일 발송.
EVENT_LOG_ARGS=()
if [[ -n "${PIPELINE_EVENT_LOG:-}" ]]; then
    EVENT_LOG_ARGS=(--event-log "${PIPELINE_EVENT_LOG}")
fi
python scripts/insight_report.py "${EVENT_LOG_ARGS[@]}" >> "${LOG_FILE}" 2>&1

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "✓ WEEKLY INSIGHT REPORT CRON SUCCESS: $(date '+%Y-%m-%d %H:%M:%S')" >> "${LOG_FILE}"
else
    echo "❌ WEEKLY INSIGHT REPORT CRON FAILED WITH EXIT CODE ${EXIT_CODE}: $(date '+%Y-%m-%d %H:%M:%S')" >> "${LOG_FILE}"
fi

echo "=== WEEKLY INSIGHT REPORT CRON END ===" >> "${LOG_FILE}"
echo "" >> "${LOG_FILE}"
exit "${EXIT_CODE}"
