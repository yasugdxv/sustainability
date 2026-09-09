#!/bin/bash
set -e
cd /home/site/wwwroot
export PYTHONPATH="/home/site/wwwroot/.python_packages/lib/site-packages:/antenv/lib/python3.11/site-packages"
/antenv/bin/python run_daily.py --skip-competitor-crawl --skip-selector
