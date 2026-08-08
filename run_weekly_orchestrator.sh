#!/bin/bash
# ===========================================================================
# Unified Weekly Investment Intelligence Report Scheduler
#  (decision-first: deterministic cross-asset signals + RAG 발언 + LLM 합성)
# ===========================================================================
# Runs scripts/unified_weekly_report.py — 결정론적 신호(weekly_signals) +
# 실제 발언 검색(collect_rag_context) → Executive Brief형 주간 통합 리포트 →
# reports/weekly/weekly_investment_intelligence_YYYY-MM-DD.md 와
# obsidian_vault/Weekly_Reports/Weekly_Investment_Intelligence_YYYY-MM-DD.md 저장.
# Crontab: 0 8 * * 1 (매주 월요일 08:00 KST — 일일 리포트 07:00 이후 최신 반영).
#
# 기존 src/orchestrator.py(Grand CIO)는 통합 리포트로 대체됐고 파일은 보존됨.
# 의존: nvidia-api-proxy(pm2 nvidia-proxy, 6-key NIM rotation) 가동.
# env 오버라이드: INSIGHT_MODEL(기본 deepseek-ai/deepseek-v4-flash).

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
# 📈 python scripts/unified_weekly_report.py — 결정론적 신호 + RAG + LLM 주간 통합 리포트.
# 스크립트가 PROJECT_ROOT를 sys.path에 추가하므로 루트에서 직접 실행 가능.
EVENT_LOG_ARGS=()
if [[ -n "${PIPELINE_EVENT_LOG:-}" ]]; then
    EVENT_LOG_ARGS=(--event-log "${PIPELINE_EVENT_LOG}")
fi
python scripts/unified_weekly_report.py "${EVENT_LOG_ARGS[@]}" >> "${LOG_FILE}" 2>&1

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "✓ WEEKLY ORCHESTRATOR CRON SUCCESS: $(date '+%Y-%m-%d %H:%M:%S')" >> "${LOG_FILE}"
else
    echo "❌ WEEKLY ORCHESTRATOR CRON FAILED WITH EXIT CODE ${EXIT_CODE}: $(date '+%Y-%m-%d %H:%M:%S')" >> "${LOG_FILE}"
fi

echo "=== WEEKLY ORCHESTRATOR CRON END ===" >> "${LOG_FILE}"
echo "" >> "${LOG_FILE}"
exit "${EXIT_CODE}"
