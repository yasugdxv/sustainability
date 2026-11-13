#!/bin/bash
set -e
cd /home/site/wwwroot
export PYTHONPATH="/home/site/wwwroot/.python_packages/lib/site-packages:/home/site/wwwroot/antenv/lib/python3.11/site-packages"
# 週次メール用: 発信日(月曜JST)の前日(日曜JST)に、記事選定 → ドラフト生成までを行う（送信はしない）。
# 承認・送信は、月曜に人が画面「週次メール承認」から行う。
# 選定が失敗したら（set -e）、古い選定結果でドラフトを作らないよう、ここで止める。
# 直近8日分の記事を、テーマ別の上位10件まで選定する（日次ジョブと同じ設定）
/opt/python/3/bin/python sustainability_article_selector.py --top-n-per-theme 10
# 直近7日に実行した選定結果から、ドラフトを1通分作る（review_requiredで保存。送信はしない）
/opt/python/3/bin/python weekly_email_report.py build --since-days 7
