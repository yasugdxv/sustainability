import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import competitor_source_discovery as discovery  # noqa: E402


def test_guess_url_type_targets():
    assert discovery.guess_url_type("https://example.com/sustainability/our-targets") == "TARGETS"


def test_guess_url_type_progress():
    assert discovery.guess_url_type("https://example.com/sustainability/progress-2026") == "PROGRESS"


def test_guess_url_type_reports_by_extension():
    assert discovery.guess_url_type("https://example.com/files/annual-report-2026.pdf") == "REPORTS"


def test_guess_url_type_news():
    assert discovery.guess_url_type("https://example.com/newsroom/press-release-1") == "NEWS"


def test_guess_url_type_disclosure():
    assert discovery.guess_url_type("https://example.com/esg-rating-cdp-score") == "DISCLOSURE"


def test_guess_url_type_uses_title_too():
    assert discovery.guess_url_type("https://example.com/page-x", title="Our 2030 Targets") == "TARGETS"


def test_guess_url_type_defaults_to_other():
    assert discovery.guess_url_type("https://example.com/contact-us") == "OTHER"


def test_is_excluded_filters_boilerplate_pages():
    assert discovery._is_excluded("https://example.com/cookie-policy")
    assert discovery._is_excluded("https://example.com/age-gate/1")
    assert not discovery._is_excluded("https://example.com/sustainability/targets")
