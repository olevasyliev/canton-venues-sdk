"""Offline tests for the Canton Venues channel's morning posts."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import channel  # noqa: E402

MON = datetime(2026, 10, 12, 9, 5, tzinfo=UTC)


def test_due_follows_the_weekday_once_a_day(monkeypatch):
    monkeypatch.setattr(channel, "PRICE_CLAIMS_PAUSED", False)  # the schedule itself
    assert channel.due({}, MON) == "weekly"
    assert channel.due({}, MON.replace(hour=8)) is None
    assert channel.due({"date": "2026-10-12"}, MON) is None
    assert [channel.due({}, MON.replace(day=d)) for d in range(13, 19)] == \
        ["venue", "execution", "venue", "pegs", None, None]


def test_weekly_post_only_for_the_week_that_just_closed():
    w = {"slug": "2026-10-11", "t": 7, "label": "Canton DEX spot volume", "range": "5 to 11 Oct 2026",
         "total_usd": 262_646_343, "change": 0.051, "shares_shown": ["79.3%", "12.3%"],
         "venues": [{"name": "Temple"}, {"name": "Cantex"}]}
    post = channel.weekly_post(w, MON)
    assert post.photo == "https://cantonvenues.com/weekly/2026-10-11/card.png?v=7"
    assert "$262.6M" in post.html and "+5.1%" in post.html and "Temple 79.3%, Cantex 12.3%" in post.html
    assert channel.weekly_post({**w, "slug": "2026-10-10"}, MON) is None  # a held card is not posted


def test_venue_post_takes_turns_and_skips_venues_without_a_card(monkeypatch):
    monkeypatch.setattr(channel, "PRICE_CLAIMS_PAUSED", False)  # the schedule itself
    pub = {"venues": {"temple": {"head": {"title": "The largest spot venue", "sub": "$29.7M"}},
                      "rocky": {"head": {"title": "The largest perps venue", "sub": "$7.1M"}}}}
    post, turn = channel.venue_post(pub, 0, {"temple": 5, "rocky": 6})
    assert post.photo.endswith("/venues/temple/card.png?v=5") and "<b>Temple</b>" in post.html
    post, turn = channel.venue_post(pub, turn, {"temple": 5, "rocky": 6})  # cantex has no head: skipped
    assert "<b>Rocky</b>" in post.html
    post, _ = channel.venue_post(pub, turn, {"temple": 5, "rocky": 6})
    assert "<b>Temple</b>" in post.html  # round again
    assert channel.venue_post({}, 0, {}) == (None, 0)


def test_execution_post_counts_wins_net_of_fees(tmp_path):
    now = int(MON.timestamp())
    with (tmp_path / "2026-10-12.jsonl").open("w") as f:
        for h in range(30):
            best = "tradecraft" if h % 3 else "cantex"
            f.write(json.dumps({"t": now - 3600 * h, "rows": [
                ["USDCX", "sell", 1000, "cantex", 9.0, best, 10.0],
                ["USDCX", "buy", 10000, "tradecraft", 30.0, "tradecraft", 20.0],
                ["CBTC:USD", "buy", 1000, "rocky", 1.0, "rocky", 1.0]]}) + "\n")
    post = channel.execution_post(channel.read_exec(tmp_path, now))
    assert "<b>$1K</b>: Tradecraft 66% of the time, Cantex 33%" in post.html
    assert "<b>$10K</b>: Tradecraft 100% of the time. The next venue returned a median 0.20% less" in post.html
    assert "$50K" not in post.html and "Rocky" not in post.html
    assert channel.execution_post([]) is None


def test_pegs_post_skips_thin_pools_and_names_the_worst_reading():
    now = int(MON.timestamp())
    ts = [now - 300 * i for i in range(7 * 288)][::-1]
    usdcx = [0.001] * len(ts)
    usdcx[100] = -0.03            # one odd reading: not the furthest held
    usdcx[200:206] = [-0.012, -0.013, -0.015, -0.012, -0.014, -0.02]
    hist = {"t": ts, "premium": {"USDCX": usdcx, "USX": [0.03] * len(ts), "CC": [-0.004] * len(ts)}}
    tokens = [{"key": "USDCX", "symbol": "USDCx", "liquidity_usd": 1_600_000},
              {"key": "USX", "symbol": "USX", "liquidity_usd": 6_000}]
    post = channel.pegs_post(hist, tokens, {"USDCX": "USDCx", "CC": "CC"}, now)
    assert "USDCx: within 0.5% of $1 100% of the time, furthest −1.20% held 30 min" in post.html
    assert "USX" not in post.html and "CC −0.40% vs global CC markets" in post.html
    assert channel.pegs_post({"t": ts[-10:], "premium": {}}, tokens, {}, now) is None
