# -*- coding: utf-8 -*-
"""
クロール対象の「HTTPステータスは200で成功扱いだが、実際には長期間まったく記事を
検出できていない」状態を検知するヘルスチェックスクリプト。

背景（2026-09-14）: env.go.jp（trafilaturaが公開日を抽出できずlookback判定で
全候補が誤って除外）、fao.org（一覧ページがJS描画でHTML取得のみでは記事リンクが
0件）、kirinholdings.com（target_urlがリダイレクトされ別ページになっていた）の
3クロール対象が、それぞれ全く別の理由で何週間も0件のまま放置されていた。
crawl_logs.run_result='更新なし'・error_message=Noneのため、通常のエラー監視では
一切検知されなかった。

既存のBot対策自動降格（article_crawler.downgrade_bot_blocked_targets）は
「Bot対策検知」という単一の明確な原因のみを対象にした自動修復だが、今回のような
原因はケースバイケースで自動修復できないため、本スクリプトは検知・一覧化（アラート）
のみを行う。run_daily.pyのステップに組み込み、異常検知時はexit(1)にすることで、
既存の失敗通知メール（notify_failures）にそのまま乗せる。

判定ロジック: 直近lookback_days日以内にmin_runs回以上実行されているのに
検出記事数(items_detected)の合計が0、かつ、より長い期間(long_window_days、
既定90日)を通しても一度も検出実績が無いクロール対象を「要確認」として報告する
（長期間内に一度でも成功していれば、単に一時的に静かなだけの可能性があるため対象外）。

使い方:
    python crawl_health_check.py [--lookback-days 14] [--min-runs 3] [--long-window-days 90]
"""
import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import sustainability_expert_common as common
from article_crawler import SupabaseClient, load_config

# 既に手動対応済み（＝自動クロール対象外と分かっている）ものはアラート対象から除外
INACTIVE_METHODS = {"手動"}


def main():
    parser = argparse.ArgumentParser(description="クロール対象のヘルスチェック（長期間0件放置の検知）")
    parser.add_argument("--lookback-days", type=int, default=14, help="直近この日数で判定する")
    parser.add_argument("--min-runs", type=int, default=3,
                         help="直近lookback_days日以内でこの回数以上実行されている対象のみ判定する"
                              "（追加直後で実行回数が少ない対象を誤検知しないため）")
    parser.add_argument("--long-window-days", type=int, default=90,
                         help="この期間内に一度でも検出実績があれば「一時的に静かなだけ」として対象外にする")
    args = parser.parse_args()

    config = load_config()
    client = SupabaseClient(config)

    targets = client.select("crawl_targets", {
        "select": "crawl_target_id,target_name,domain,target_url,crawl_method,endpoint_type,created_at",
    })
    targets = [t for t in targets if t.get("crawl_method") not in INACTIVE_METHODS]
    targets_by_id = {t["crawl_target_id"]: t for t in targets}
    target_ids = list(targets_by_id.keys())
    print(f"判定対象クロール対象: {len(targets)}件（手動対応済みを除く）")

    now = datetime.now(timezone.utc)
    long_cutoff = (now - timedelta(days=args.long_window_days)).isoformat()
    recent_cutoff = (now - timedelta(days=args.lookback_days)).isoformat()

    logs = common._select_in_chunks(client, "crawl_logs", {
        "select": "crawl_target_id,started_at,items_detected",
        "started_at": f"gte.{long_cutoff}",
    }, "crawl_target_id", target_ids)

    logs_by_target = defaultdict(list)
    for log in logs:
        logs_by_target[log["crawl_target_id"]].append(log)

    alerts = []
    for tid, target in targets_by_id.items():
        all_logs = logs_by_target.get(tid, [])
        recent_logs = [lg for lg in all_logs if lg["started_at"] >= recent_cutoff]
        if len(recent_logs) < args.min_runs:
            continue  # 直近あまり実行されていない(追加直後等)は判定対象外
        recent_total = sum(lg.get("items_detected") or 0 for lg in recent_logs)
        if recent_total > 0:
            continue
        long_total = sum(lg.get("items_detected") or 0 for lg in all_logs)
        if long_total > 0:
            continue  # long_window_days以内に一度でも成功していれば対象外
        alerts.append({
            "target_name": target.get("target_name"), "domain": target.get("domain"),
            "target_url": target.get("target_url"), "crawl_method": target.get("crawl_method"),
            "endpoint_type": target.get("endpoint_type"),
            "runs_in_window": len(recent_logs), "created_at": target.get("created_at"),
        })

    if not alerts:
        print(f"異常なし（直近{args.lookback_days}日間で{args.min_runs}回以上実行された対象のうち、"
              f"過去{args.long_window_days}日間ずっと0件のものは無し）")
        return 0

    print(f"\n[要確認] 直近{args.lookback_days}日間で{args.min_runs}回以上実行されているのに、"
          f"過去{args.long_window_days}日間一度も記事を検出できていないクロール対象: {len(alerts)}件\n")
    for a in sorted(alerts, key=lambda x: x["target_name"] or ""):
        print(f"  ・{a['target_name']}（{a['domain']}）: 直近{a['runs_in_window']}回実行/0件検出 "
              f"URL={a['target_url']} 方式={a['crawl_method']}/{a['endpoint_type']} 登録日={a['created_at']}")
    print("\n考えられる原因の例（過去の実例）: JS描画必須のためHTML取得のみでは記事リンクが"
          "取得できない／target_urlがリダイレクトされ別ページになっている／公開日が抽出できず"
          "lookback判定で除外されている 等。原因はケースバイケースのため個別に確認が必要です。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
