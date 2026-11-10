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
    assert state == {"alerted": NOW.timestamp(), "kind": "stale", "last_t": s["t"]}
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


def test_a_venue_left_out_for_over_30_min_is_told_once_then_when_it_is_back():
    s = summary(3, down_since={"cantex": NOW.timestamp() - 10 * 60})
    assert wd.check(s, {}, NOW) == (None, {})  # ten minutes out: a blip, nothing sent

    s["down_since"]["cantex"] = NOW.timestamp() - 45 * 60
    text, state = wd.check(s, {}, NOW)
    assert "is missing a venue" in text and "Cantex has not answered since 14 Oct 09:15 UTC, 45 min ago" in text
    assert state["kind"] == "venues" and state["venues"] == ["cantex"]
    assert wd.check(s, state, NOW + timedelta(minutes=10)) == (None, state)

    later = NOW + timedelta(minutes=30)
    # clear once is not back yet (2026-10-15: a flapping venue paged three times in 20 min)
    quiet, state = wd.check({"t": later.timestamp() - 120, "down_since": {}}, state, later)
    assert quiet is None and state["clear_seen"] == later.timestamp()
    later += timedelta(minutes=10)
    back, _ = wd.check({"t": later.timestamp() - 120, "down_since": {}}, state, later)
    assert "has every venue again" in back and "Cantex answering again, out for 85 min" in back


def test_a_new_trouble_is_told_at_once_not_held_for_the_reminder():
    venues = {"alerted": NOW.timestamp() - 600, "kind": "venues", "venues": ["cantex"],
              "since": NOW.timestamp() - 3600}
    text, state = wd.check(summary(45), venues, NOW)  # the whole site stopped
    assert "is not refreshing" in text and state["kind"] == "stale"
    both = summary(2, down_since={"cantex": NOW.timestamp() - 3600, "tradecraft": NOW.timestamp() - 2400})
    text, state = wd.check(both, venues, NOW)  # a second venue went
    assert "Tradecraft has not answered" in text and state["venues"] == ["cantex", "tradecraft"]


# --- the owner is told what he can act on (founder 10.11: "spams me with information I do not know
# what to do with") ---------------------------------------------------------------------------------

def test_a_venue_the_site_runs_without_is_told_after_a_day_once_and_never_reminded():
    """Pool Party, 30 Oct to 10 Nov 2026: told at 30 minutes and then every six hours for eleven days."""
    def s(hours):
        return summary(3, optional_down={"poolparty": NOW.timestamp() - hours * 3600})
    assert wd.check(s(1), {}, NOW) == (None, {})
    assert wd.check(s(23), {}, NOW) == (None, {})
    text, state = wd.check(s(25), {}, NOW)
    assert "is missing a venue" in text and "Pool Party has not been read since 13 Oct 09:00 UTC, 25.0 h ago" in text
    assert "Nothing for you to do" in text and "No reminders" in text
    for days in (1, 3, 11):
        later = NOW + timedelta(days=days)
        again = {"t": later.timestamp() - 120, "status": "ok", "optional_down": {"poolparty": NOW.timestamp() - 25 * 3600}}
        assert wd.check(again, state, later)[0] is None
    later = NOW + timedelta(days=11)
    clear = {"t": later.timestamp() - 120, "status": "ok", "optional_down": {}}
    quiet, state = wd.check(clear, state, later)
    assert quiet is None
    back, _ = wd.check(clear | {"t": later.timestamp() + 480}, state, later + timedelta(minutes=10))
    assert "is whole again" in back


def test_an_old_outside_source_waits_a_day_and_a_new_temple_market_is_told_at_once_without_reminders():
    old = summary(3, sources={"llama": NOW.timestamp() - 8 * 3600})
    assert wd.check(old, {}, NOW) == (None, {})
    old["sources"]["llama"] = NOW.timestamp() - 30 * 3600
    assert "DefiLlama TVL last read" in wd.check(old, {}, NOW)[0]
    new = summary(3, temple_unread={"BETH/USDCx": 2_700_000.0})
    text, state = wd.check(new, {}, NOW)
    assert "Temple's BETH/USDCx traded 2,700,000 USDCx in 24h" in text and "Nothing for you to do" in text
    later = NOW + timedelta(days=2)
    assert wd.check(new | {"t": later.timestamp() - 120}, state, later)[0] is None


def test_what_visitors_see_broken_is_told_at_once_reminded_and_says_what_to_do():
    s = summary(3, down_since={"cantex": NOW.timestamp() - 45 * 60},
                optional_down={"poolparty": NOW.timestamp() - 45 * 60})
    text, state = wd.check(s, {}, NOW)
    # the core venue is told; the other one is under a day old and stays out of the message
    assert "Cantex has not answered" in text and "Pool Party" not in text
    assert text.endswith("What to do: open a Canton session so it gets looked at.")
    later = NOW + timedelta(seconds=wd.REMIND_S + 60)
    again, _ = wd.check(s | {"t": later.timestamp() - 120}, state, later)
    assert "is still missing a venue" in again
    stale, _ = wd.check(summary(45), {}, NOW)
    assert "is not refreshing" in stale and stale.endswith("so it gets looked at.")
