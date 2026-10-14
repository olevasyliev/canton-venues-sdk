"""The watchdog: one message when the live snapshot goes stale, a reminder every six hours, one when back."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import watchdog as wd

NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


def summary(age_min: float, **extra):
    return {"t": NOW.timestamp() - age_min * 60, "status": "ok"} | extra


def test_a_fresh_snapshot_sends_nothing():
    assert wd.check(summary(4), {}, NOW) == (None, {})


def test_a_stale_snapshot_alerts_once_with_the_collector_error():
    s = summary(45, status="error", error="conversion from NoneType to Decimal is not supported",
                error_t=NOW.timestamp() - 300)
    text, state = wd.check(s, {}, NOW)
    assert "is not refreshing" in text
    assert "45 min ago" in text and "14 Oct 09:15 UTC" in text
    assert "Collector error at 14 Oct 09:55 UTC: conversion from NoneType" in text
    assert state == {"alerted": NOW.timestamp(), "last_t": s["t"]}
    # ten minutes later, still stale: already reported
    later = NOW + timedelta(minutes=10)
    assert wd.check(s, state, later) == (None, state)


def test_still_stale_after_six_hours_reminds_once():
    s = summary(400)
    state = {"alerted": NOW.timestamp() - wd.REMIND_S - 1, "last_t": s["t"]}
    text, new = wd.check(s, state, NOW)
    assert "still not refreshing" in text and "6.7 h ago" in text
    assert new["alerted"] == NOW.timestamp() and new["last_t"] == s["t"]


def test_fresh_again_after_an_alert_says_how_long_and_clears_the_state():
    last = NOW.timestamp() - 22 * 3600
    text, state = wd.check(summary(1), {"alerted": last + 1800, "last_t": last}, NOW)
    assert "is fresh again" in text and "no fresh snapshot for 22.0 h" in text
    assert state == {}


def test_a_site_that_does_not_answer_counts_as_stale():
    text, state = wd.check(None, {}, NOW, fetch_error="HTTPError: 502")
    assert "is not refreshing" in text and "did not answer: HTTPError: 502" in text
    assert state["alerted"] == NOW.timestamp() and state["last_t"] is None


def test_run_keeps_state_between_runs_and_retries_an_undelivered_message(tmp_path):
    path, sent = tmp_path / "watchdog.json", []
    stale = lambda: (summary(45), None)  # noqa: E731

    # Telegram fails: nothing kept, so the next run tries again
    assert wd.run(path, NOW, get=stale, deliver=lambda t: False) is None
    assert not path.exists()

    def deliver(text):
        sent.append(text)
        return True

    wd.run(path, NOW, get=stale, deliver=deliver)
    wd.run(path, NOW + timedelta(minutes=10), get=stale, deliver=deliver)
    assert len(sent) == 1 and json.loads(path.read_text())["alerted"] == NOW.timestamp()

    back = NOW + timedelta(minutes=20)
    wd.run(path, back, get=lambda: ({"t": back.timestamp() - 60}, None), deliver=deliver)
    assert len(sent) == 2 and "fresh again" in sent[1]
    assert json.loads(path.read_text()) == {}
