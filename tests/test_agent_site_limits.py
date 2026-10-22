"""TinyFish Agentの上限を「対象サイト数 × 1サイトあたりの記事数」にした変更のテスト（2026-10-21追加）

背景: 従来の「1プロセス10件(バックログ30件)」は先着順で、記事が多い1サイト(Council of the EU: 10件)が
上限を使い切り、後ろのサイト(FT・McKinsey・Reuters・IMF・丸紅など)にAgentが回らなかった。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402


@pytest.fixture(autouse=True)
def _limits(monkeypatch):
    monkeypatch.setattr(ac, "_tinyfish_agent_call_count", 0)
    monkeypatch.setattr(ac, "_tinyfish_agent_site_calls", {})
    monkeypatch.setattr(ac, "TINYFISH_AGENT_MAX_SITES", 2)
    monkeypatch.setattr(ac, "TINYFISH_AGENT_MAX_PER_SITE", 3)
    monkeypatch.setattr(ac, "TINYFISH_AGENT_RUN_CAP", 100)


def test_defaults_are_50_sites_and_10_articles_per_site():
    """環境変数なしの既定値は、別プロセスで確認する（モジュールの再読み込みで、他のテストに影響しないように）"""
    import os
    import subprocess
    env = {k: v for k, v in os.environ.items() if not k.startswith("TINYFISH_AGENT_MAX")}
    code = ("import article_crawler as a;"
            "print(a.TINYFISH_AGENT_MAX_SITES, a.TINYFISH_AGENT_MAX_PER_SITE,"
            " a.TINYFISH_AGENT_RUN_CAP, a.BACKLOG_AGENT_CAP)")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).parent.parent), env=env,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["50", "10", "500", "500"]


def test_one_site_cannot_use_more_than_its_per_site_limit():
    for _ in range(3):
        assert ac._tinyfish_agent_budget_available("A")
        ac._record_tinyfish_agent_call("A")
    assert not ac._tinyfish_agent_budget_available("A")          # Aは3件で打ち止め
    assert ac._tinyfish_agent_budget_available("B")              # 他のサイトには、まだ回る


def test_a_big_site_no_longer_starves_the_following_sites():
    """従来: 先頭のサイトが10件を使い切ると、後ろのサイトは0件だった"""
    for _ in range(3):
        ac._record_tinyfish_agent_call("big-site")
    assert ac._tinyfish_agent_budget_available("next-1")
    ac._record_tinyfish_agent_call("next-1")
    assert not ac._tinyfish_agent_budget_available("big-site")


def test_new_sites_are_refused_after_the_site_limit_but_known_sites_continue():
    ac._record_tinyfish_agent_call("A")
    ac._record_tinyfish_agent_call("B")                          # 2サイト目で、サイト数の上限
    assert not ac._tinyfish_agent_budget_available("C")          # 3サイト目は不可
    assert ac._tinyfish_agent_budget_available("A")              # すでに使っているサイトは、記事数の範囲で続けられる


def test_overall_cap_still_applies(monkeypatch):
    monkeypatch.setattr(ac, "TINYFISH_AGENT_RUN_CAP", 2)         # --agent-capでの手動指定
    ac._record_tinyfish_agent_call("A")
    ac._record_tinyfish_agent_call("B")
    assert not ac._tinyfish_agent_budget_available("A")
    assert not ac._tinyfish_agent_budget_available()


def test_without_target_id_only_the_overall_cap_is_checked():
    for _ in range(10):
        ac._record_tinyfish_agent_call()
    assert ac._tinyfish_agent_budget_available()


def test_backlog_run_resets_the_site_counters():
    ac._record_tinyfish_agent_call("A")
    import inspect
    assert "_tinyfish_agent_site_calls.clear()" in inspect.getsource(ac._run_pass2_backlog)
