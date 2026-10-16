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
        _log("b", "更新なし", ac._compose_notes("", {"p1_article_failed": 2})),
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
    assert s["tiers"]["tinyfish_fetch"] == [3, 2] and s["tiers"]["zyte"] == [1, 0]
    assert s["tiers"]["tinyfish_agent"] == [0, 0]
    assert s["final_failed"] == 1
    assert s["pass2_not_reached"] == 3  # b、c、f


def test_render_does_not_fail_on_empty_logs():
    text = rep.render(rep.summarize([]), "2026-10-01")
    assert "実行ログ 0件" in text
