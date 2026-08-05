#!/usr/bin/env bash
# run_auto_blog.sh — 블로그 자동 발행 cron 래퍼 (xvfb-run 가상 디스플레이)
# crontab: 30 8 * * * /home/mikey/global_macro_time_series_knowledge_graph/run_auto_blog.sh
set -uo pipefail
cd /home/mikey/global_macro_time_series_knowledge_graph
mkdir -p logs blog_drafts
# xvfb-run -a: 자동 가상 디스플레이 할당 → cron 무인 headed 발행(봇탐지 회피 + 클립보드 동작)
exec xvfb-run -a .venv/bin/python scripts/auto_blog.py >> logs/auto_blog.log 2>&1