"""Dates and shared labels read naturally in each language, and Spanish reads as it always did."""

from datetime import UTC, datetime, timedelta

import pytest

from session_bridge import ui

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)


def ago(**delta):
    return (NOW - timedelta(**delta)).isoformat()


CASES = [
    # (moment, Spanish long, Spanish compact, English long, English compact)
    (ago(seconds=20), "ahora", "ahora", "now", "now"),
    (ago(minutes=5), "hace 5 min", "5 min", "5 min ago", "5 min"),
    (ago(hours=2), "hace 2 h", "2 h", "2 h ago", "2 h"),
    (ago(days=1), "ayer", "ayer", "yesterday", "yesterday"),
    (ago(days=3), "hace 3 días", "3 d", "3 days ago", "3 d"),
    ("2026-09-02T10:00:00+00:00", "2 sep", "2 sep", "Sep 2", "Sep 2"),
    ("2025-12-24T10:00:00+00:00", "24 dic 2025", "24 dic 2025", "Dec 24, 2025", "Dec 24, 2025"),
]


@pytest.mark.parametrize("moment,es_long,es_short,en_long,en_short", CASES)
def test_relative_times_in_spanish_and_english(moment, es_long, es_short, en_long, en_short, lang):
    assert ui.relative_time(moment, now=NOW) == es_long
    assert ui.compact_time(moment, now=NOW) == es_short
    lang("en")
    assert ui.relative_time(moment, now=NOW) == en_long
    assert ui.compact_time(moment, now=NOW) == en_short


def test_a_missing_or_unreadable_time_reads_as_nothing():
    assert ui.relative_time(None) == "" and ui.relative_time("not a date") == ""
