#!/bin/bash
set -e
cd /home/site/wwwroot
export PYTHONPATH="/home/site/wwwroot/.python_packages/lib/site-packages:/home/site/wwwroot/antenv/lib/python3.11/site-packages"
/opt/python/3/bin/python run_daily.py --skip-crawl --skip-reference-list --skip-api-articles --skip-filter-coverage-check --skip-analyze --skip-selector
