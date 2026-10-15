"""F 2026-10-15: a price claim in the channel needs our quotes checked against the venues' own (the
hourly quote check) and names only venues whose prices are checked; without a passing check the channel
posts only what a venue's own API confirms."""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import channel  # noqa: E402

NOW = time.time()


def _check(t=NOW, cantex="ok"):
    return {"t": t, "venues": {"tradecraft": {"status": "ok"}, "cantex": {"status": cantex},
                               "oneswap": {"status": "not checked"}, "poolparty": {"status": "not checked"}}}


def test_checked_venues_need_a_fresh_passing_check():
    assert channel.checked_venues(_check(), NOW) == {"tradecraft", "cantex", "temple", "rocky"}
    assert channel.checked_venues(_check(cantex="gap"), NOW) == set()  # a gap anywhere: no price claim
    assert channel.checked_venues(_check(t=NOW - 4 * 3600), NOW) == set()  # stale: none
    assert channel.checked_venues(None, NOW) == set()


def test_no_price_post_without_a_passing_check():
    fri = datetime(2026, 10, 16, 9, 5, tzinfo=UTC)   # pegs
    wed = datetime(2026, 10, 21, 9, 5, tzinfo=UTC)   # execution
    mon = datetime(2026, 10, 19, 9, 5, tzinfo=UTC)   # weekly card: volumes
    assert channel.due({}, fri, set()) is None and channel.due({}, wed, set()) is None
    assert channel.due({}, mon, set()) == "weekly"
    assert channel.due({}, fri, {"tradecraft", "cantex", "temple", "rocky"}) == "pegs"


def test_a_price_headline_card_only_for_a_checked_venue():
    heads = {"venues": {
        "oneswap": {"head": {"rule": "best_quote", "title": "Best price to buy HANDL", "sub": "x"}},
        "cantex": {"head": {"rule": "best_quote", "title": "Best price to sell CBTC", "sub": "y"}}}}
    checked = {"tradecraft", "cantex", "temple", "rocky"}
    post, _ = channel.venue_post(heads, 0, {"oneswap": 1, "cantex": 1}, checked)
    assert "Cantex" in post.html  # OneSwap's price headline is passed over: nothing checks its quotes
    assert channel.venue_post(heads, 0, {"oneswap": 1, "cantex": 1}, set()) == (None, 0)


def test_execution_post_counts_only_samples_after_the_fixes_and_checked_winners():
    old = {"t": channel.EXEC_SINCE - 3600, "rows": [["USDCX", "sell", 1000, "tradecraft", 5, "tradecraft", 5]]}
    new = [{"t": channel.EXEC_SINCE + 3600 * k, "rows": [["USDCX", "sell", 1000, "temple", 5, "temple", 5],
                                                         ["USDCX", "sell", 10_000, "oneswap", 5, "oneswap", 5]]}
           for k in range(channel.MIN_EXEC_HOURS)]
    assert channel.execution_post([old] * 30) is None  # nothing from before 15.10 13:00 UTC counts
    post = channel.execution_post([old] + new, {"temple", "cantex", "tradecraft", "rocky"})
    assert "Temple 100% of the time" in post.html and "OneSwap" not in post.html


def test_no_loud_alert_names_an_unchecked_venue():
    src = (Path(channel.__file__).parent / "collect.py").read_text()
    assert '{r["buy_on"], r["sell_on"]} <= checked' in src and "if checked else []" in src
