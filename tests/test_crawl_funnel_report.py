"""crawl_funnel_report.summarize() のテスト（DB非依存）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import article_crawler as ac  # noqa: E402
import crawl_funnel_report as rep  # noqa: E402


def _log(target, result, notes=None):
    return {"crawl_target_id": target, "run_result": result, "notes": notes}


def test_summarize_counts_pass1_pass2_and_unreached():
    logs = [
        # Pass1で記事3件失敗 → TinyFish Fetchで2件救済、Zyteで1件投入して失敗 → 最終失敗1件
        _log("a", "一部成功", ac._compose_notes("", {
            "p1_article_failed": 3, "tiers": {"tinyfish_fetch": [3, 2], "zyte": [1, 0]}, "final_failed": 1})),
        # Pass1で記事2件失敗したが、Pass2の記録が無い（チャンクの時間切れ等で未到達）
        _log("b", "更新なし", ac._compose_notes("", {"p1_article_failed": 2, "p1_pending": [{"u": "https://x/1"}, {"u": "https://x/2"}]})),
        # 一覧取得に失敗して暫定行のまま（listing_retryなし = 未到達）
        _log("c", "更新なし", ac._compose_notes("", {"p1_listing_failed": 1})),
        # 一覧取得リトライに成功
        _log("d", "成功", ac._compose_notes("", {"p1_listing_failed": 1, "listing_retry": "ok"})),
        # Zyte方式の対象を最初からPass2へ直送（失敗ではない）。Pass2未到達のまま
        _log("f", "更新なし", ac._compose_notes("", {"p1_routed_to_pass2": "Zyte"})),
        # 記録なし（funnel導入前や、Pass1で失敗が無かった行）
        _log("e", "更新なし"),
    ]
    s = rep.summarize(logs)
    assert s["targets"] == 6 and s["with_funnel"] == 5
    assert s["p1_routed_targets"] == 1
    assert s["p1_article_failed"] == 5
    assert s["p1_listing_failed_targets"] == 2
    assert s["tiers"]["tinyfish_fetch"][:2] == [3, 2] and s["tiers"]["zyte"][:2] == [1, 0]
    assert s["tiers"]["tinyfish_agent"][:2] == [0, 0]
    assert s["final_failed"] == 1
    assert s["pass2_not_reached"] == 3  # b(再試行待ちが残っている)、c、f
    assert s["pending_outstanding"] == 2


def test_render_does_not_fail_on_empty_logs():
    text = rep.render(rep.summarize([]), "2026-10-01")
    assert "実行ログ 0件" in text


def test_summarize_and_render_show_success_rate_and_average_seconds_per_tier():
    """所要秒数付き([試行, 成功, 秒])の記録から、成功率と平均秒数を出せる（順番の見直し用）。
    秒数の無い旧形式の記録(2要素)と混在しても落ちない"""
    logs = [
        _log("a", "成功", ac._compose_notes("", {"tiers": {"tinyfish_fetch": [4, 3, 40.0], "zyte": [1, 1, 30.0]}})),
        _log("b", "成功", ac._compose_notes("", {"tiers": {"tinyfish_fetch": [6, 3, 60.0]}})),
        _log("c", "成功", ac._compose_notes("", {"tiers": {"tinyfish_fetch": [2, 2]}})),   # 旧形式
    ]
    s = rep.summarize(logs)
    assert s["tiers"]["tinyfish_fetch"] == [12, 8, 100.0]
    text = rep.render(s, "2026-10-19")
    assert "成功率67%" in text and "平均8秒/件" in text


def test_tier_profile_requires_enough_samples_and_recommends_order_only_when_measured():
    few = rep.summarize([_log("a", "成功", ac._compose_notes("", {"tiers": {"tinyfish_fetch": [5, 4, 50.0]}}))])
    prof = rep.tier_profile(few)
    assert prof["tinyfish_fetch"]["p"] is None and prof["tinyfish_fetch"]["n"] == 5     # 30件未満は判断しない
    assert "既定の順を維持" in rep.render(few, "x")

    enough = rep.summarize([_log("a", "成功", ac._compose_notes("", {"tiers": {
        "tinyfish_fetch": [100, 76, 4000.0], "zyte": [40, 30, 400.0]}}))])
    prof = rep.tier_profile(enough)
    assert prof["tinyfish_fetch"]["p"] == 0.76 and prof["tinyfish_fetch"]["t"] == 40.0
    assert prof["zyte"]["p"] == 0.75 and prof["zyte"]["t"] == 10.0
    text = rep.render(enough, "x")
    assert "推奨 = zyte → tinyfish_fetch → tinyfish_agent" in text   # Zyteの方が速い記録 → 先に推奨
