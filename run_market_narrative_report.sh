#!/bin/bash
# ===========================================================================
# Market Narrative Search Engine Report Scheduler (마켓 내러티브 & 핵심 병목)
# ===========================================================================
# Runs scripts/insights/run_market_narrative.py — 6대 RAG 쿼리 + SQLite 통계 +
# DeepSeek-v4-Pro(NIM) 내러티브 추론 → Markdown(reports/narrative) + Obsidian
# 동기화(Narrative_Reports) + Gmail HTML 발송.
#
# Cron / 백그라운드 전용 래퍼. systemd timer 로 매주 수/일 06:00 KST 자동 실행
# (Persistent=true — 기기가 꺼져 있던 06:00 는 부팅 직후 자동 보정 실행).
# crontab 등가 예시:
#   0 6 * * 0,3 /home/mikey/global_macro_time_series_knowledge_graph/run_market_narrative_report.sh
#
# 의존: nvidia-api-proxy(pm2 nvidia-proxy) — RAG/LLM 내러티브 추론용.
# env: GMAIL_USER / GMAIL_APP_PASSWORD (.env), NIM_BASE_URL, INSIGHT_MODEL(선택, 기본 deepseek-v4-pro).

# Include NodeJS in PATH for NVM compatibility in cron/background jobs
export PATH="/home/mikey/.nvm/versions/node/v22.22.2/bin:$PATH"

PROJECT_DIR="/home/mikey/global_macro_time_series_knowledge_graph"
LOG_FILE="${PROJECT_DIR}/narrative_report_cron.log"

echo "=== MARKET NARRATIVE REPORT CRON START: $(date '+%Y-%m-%d %H:%M:%S') ===" >> "${LOG_FILE}"

cd "${PROJECT_DIR}" || {
    echo "❌ Error: Could not change directory to ${PROJECT_DIR}" >> "${LOG_FILE}"
    exit 1
}

# .env 로드 (GMAIL_* / NIM_*)
if [ -f ".env" ]; then
    set -a
    . ./.env
    set +a
fi

# Verify nvidia-api-proxy is alive (RAG/LLM 내러티브 backend) — start if down.
if ! curl -sf --max-time 5 http://localhost:8000/health >/dev/null 2>&1; then
    echo "⚠️ nvidia-api-proxy down — restarting via pm2..." >> "${LOG_FILE}"
    pm2 restart nvidia-proxy >/dev/null 2>&1 || \
    pm2 start /usr/bin/python3 --name nvidia-proxy -- /home/mikey/nvidia-api-proxy/proxy_server.py >> "${LOG_FILE}" 2>&1
    sleep 3
    if ! curl -sf --max-time 5 http://localhost:8000/health >/dev/null 2>&1; then
        echo "❌ nvidia-api-proxy unreachable after restart — aborting narrative report" >> "${LOG_FILE}"
        exit 1
    fi
    echo "✓ nvidia-api-proxy recovered" >> "${LOG_FILE}"
fi

if [ ! -d ".venv" ]; then
    echo "❌ Error: Virtual environment .venv not found in ${PROJECT_DIR}" >> "${LOG_FILE}"
    exit 1
fi

source .venv/bin/activate
# 전체(내러티브 추론 + 저장 + Obsidian Sync + 메일 발송).
# --no-send 를 원하면 아래에 추가 (메일만 제외).
python scripts/insights/run_market_narrative.py >> "${LOG_FILE}" 2>&1

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "✓ MARKET NARRATIVE REPORT CRON SUCCESS: $(date '+%Y-%m-%d %H:%M:%S')" >> "${LOG_FILE}"
else
    echo "❌ MARKET NARRATIVE REPORT CRON FAILED WITH EXIT CODE ${EXIT_CODE}: $(date '+%Y-%m-%d %H:%M:%S')" >> "${LOG_FILE}"
fi

echo "=== MARKET NARRATIVE REPORT CRON END ===" >> "${LOG_FILE}"
echo "" >> "${LOG_FILE}"
