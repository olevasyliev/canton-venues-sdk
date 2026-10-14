"""Temple's CC/USDCx order book joins every CC/USDCx comparison, never the CC price or the pool tables."""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import collect
import model as m

CC_USD = Decimal("0.12")


def pool(venue="tradecraft", cc="4000000", tok="480000", fee=Decimal("0.003")):
    cc, tok = Decimal(cc), Decimal(tok)
    return m.VenuePool(venue, "USDCx", cc, tok, fee, Decimal("0.9"), m.FORMULAS["cantex"](cc, tok, fee), "cantex", fee)


def book(levels=60, size="50000", tick="0.00001"):
    """A deep book around 0.12 USDCx per CC: ``levels`` levels each side of ``size`` CC."""
    tick, size = Decimal(tick), Decimal(size)
    bids = tuple((Decimal("0.11999") - i * tick, size) for i in range(levels))
    asks = tuple((Decimal("0.12001") + i * tick, size) for i in range(levels))
    return m.BookVenue("temple", "CC/USDCx", bids, asks, Decimal(1), m.BOOK_TAKER_FEE["temple"])


def test_the_book_weighs_nothing_in_the_cc_price():
    p, b = pool(), m.CCBook("temple", "USDCx", book())
    assert m.blended_mid([p, b]) == m.blended_mid([p]) == p.mid
    assert b.mid == Decimal("0.12")


def test_a_deep_book_beats_a_pool_at_size_and_is_compared_after_its_fee():
    pools = {"tradecraft": pool(), "temple": m.CCBook("temple", "USDCx", book())}
    rows = m.ladder(pools, CC_USD, m.blended_mid(list(pools.values())))
    at_50k = {(r["side"], r["size_usd"]): r for r in rows}
    for side in ("sell", "buy"):
        r = at_50k[(side, 50_000)]
        assert r["best_net"] == "temple" and r["out"]["temple"] > r["out"]["tradecraft"]
        assert r["fee_usd"]["temple"] == float(m.network_fee_usd("temple", CC_USD))


def test_a_book_that_runs_out_shows_unfilled_and_never_wins():
    thin = m.CCBook("temple", "USDCx", book(levels=2, size="1000"))  # ~$480 a side
    rows = m.ladder({"tradecraft": pool(), "temple": thin}, CC_USD, pool().mid)
    big = [r for r in rows if r["size_usd"] >= 1_000]
    assert all(r["out"]["temple"] is None and r["best"] == "tradecraft" and "temple" not in r["out_net"]
               for r in big)


def test_scanner_and_paper_router_take_the_book():
    pools = {"tradecraft": pool(), "temple": m.CCBook("temple", "USDCx", book())}
    found = m.scan({"USDCX": pools}, CC_USD)
    assert {(x["buy_on"], x["sell_on"]) for x in found} == {("tradecraft", "temple"), ("temple", "tradecraft")}
    fill = m.route_order(pools, "sell", m.ROUTER_SIZE_USD, CC_USD, pool().mid)
    assert fill["venue"] in pools and fill["got"] > 0


def test_execution_names_the_book_on_the_cc_usdcx_pair(tmp_path):
    c = collect.Collector(tmp_path)
    asyncio.run(c.http.aclose())
    pairs = c._execution({"USDCX": {"tradecraft": pool(), "temple": m.CCBook("temple", "USDCx", book())}}, CC_USD)
    assert pairs[0]["venues"] == ["temple", "tradecraft"] and pairs[0]["symbol"] == "USDCx"
    assert pairs[0]["books"] == {"temple": {"market": "CC/USDCx", "fee": 0.0001}}
    plain = c._execution({"USDCX": {"tradecraft": pool(), "cantex": pool("cantex")}}, CC_USD)
    assert "books" not in plain[0]
