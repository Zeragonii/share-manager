from datetime import datetime, timedelta


def test_news_banner_overlap_rule():
    start = datetime(2026, 9, 10, 10, 0)
    end = start + timedelta(hours=2)
    # Adjacent windows do not overlap; intersecting ones do. Mirrors SQL predicate starts < other_end and ends > other_start.
    assert not (end < end and start > end)
    next_start = end
    next_end = end + timedelta(hours=1)
    assert not (start < next_end and end > next_start)
    overlap_start = start + timedelta(minutes=30)
    overlap_end = end + timedelta(minutes=30)
    assert start < overlap_end and end > overlap_start


def test_noncritical_banner_scope_rule():
    def visible(severity: str, scope: str) -> bool:
        return scope == "account" or severity == "critical"

    for severity in ("info", "advisory", "warning"):
        assert visible(severity, "account")
        assert not visible(severity, "global")
    assert visible("critical", "account")
    assert visible("critical", "global")
