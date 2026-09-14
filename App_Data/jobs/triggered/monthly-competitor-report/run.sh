#!/bin/bash
set -e
cd /home/site/wwwroot
export PYTHONPATH="/home/site/wwwroot/.python_packages/lib/site-packages:/home/site/wwwroot/antenv/lib/python3.11/site-packages"
# 前月分の下書きを生成する（送信はしない。PMOレビュー→承認後の送信は
# 引き続きsustainability_expert_dashboard.pyからの手動操作）
REPORT_MONTH=$(date -u -d "1 month ago" +%Y-%m)
/opt/python/3/bin/python monthly_competitor_report.py build --report-month "$REPORT_MONTH"
