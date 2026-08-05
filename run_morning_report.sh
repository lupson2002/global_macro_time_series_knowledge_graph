#!/bin/bash
# ===========================================================================
# Morning Macro Report Agent Cron Scheduler Script
# ===========================================================================
# Auto-activates python virtualenv and runs report_generator.py.
# Append execution logs to morning_report_cron.log.

# Include NodeJS in PATH for NVM compatibility in cron/background jobs
export PATH="/home/mikey/.nvm/versions/node/v22.22.2/bin:$PATH"

PROJECT_DIR="/home/mikey/global_macro_time_series_knowledge_graph"
LOG_FILE="${PROJECT_DIR}/morning_report_cron.log"

echo "=== MORNING REPORT CRON START: $(date '+%Y-%m-%d %H:%M:%S') ===" >> "${LOG_FILE}"

# Change to project root directory
cd "${PROJECT_DIR}" || {
    echo "❌ Error: Could not change directory to ${PROJECT_DIR}" >> "${LOG_FILE}"
    exit 1
}

# Check virtual environment
if [ ! -d ".venv" ]; then
    echo "❌ Error: Virtual environment .venv not found in ${PROJECT_DIR}" >> "${LOG_FILE}"
    exit 1
fi

# Activate virtualenv and run report generator
source .venv/bin/activate
python src/report_generator.py "$@" >> "${LOG_FILE}" 2>&1

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "✓ MORNING REPORT CRON SUCCESS: $(date '+%Y-%m-%d %H:%M:%S')" >> "${LOG_FILE}"
else
    echo "❌ MORNING REPORT CRON FAILED WITH EXIT CODE ${EXIT_CODE}: $(date '+%Y-%m-%d %H:%M:%S')" >> "${LOG_FILE}"
fi

echo "=== MORNING REPORT CRON END ===" >> "${LOG_FILE}"
echo "" >> "${LOG_FILE}"
