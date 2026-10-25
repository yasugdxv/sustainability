#!/bin/bash
set -e
cd /home/site/wwwroot
export PYTHONPATH="/home/site/wwwroot/.python_packages/lib/site-packages:/home/site/wwwroot/antenv/lib/python3.11/site-packages"
# 追い込み実行: daily-crawl-analyze が途中で止まった日だけ、続きから再開する(完了済みの日は何もしない)
/opt/python/3/bin/python run_daily.py --catchup --skip-competitor-crawl --skip-selector
