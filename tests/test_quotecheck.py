"""The quote check (quotecheck.py) and each Cantex pair's own network fee (founder 15.10: no price claim
without a check against the venues' own quote engines). Each test fails on the code before them."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import collect  # noqa: E402
import model as m  # noqa: E402
import quotecheck as qc  # noqa: E402

D = Decimal
CC_USD = D("0.106")


def _run(read, quote, formula="tradecraft"):
    return asyncio.run(qc._check("tradecraft", read, quote, formula, CC_USD))


def test_our_math_and_the_venues_quote_agree_and_every_trade_is_recorded():
    pools = {"USDCx": (D(4_000_000), D(430_000), D("0.003"))}

    async def read():
        return pools

    async def quote(tok, sell_cc, amount):  # the venue's engine: the same constant product
        return m.FORMULAS["tradecraft"](*pools[tok])(sell_cc, amount), None
    rows = _run(read, quote)
    assert len(rows) == len(qc.SIZES_USD) * 2 and all(r["ok"] and r["gap_bp"] == 0 for r in rows)
    rec = qc.summarise(rows, {"cantex": qc.NO_KEYS}, {}, 1)
    assert rec["venues"]["tradecraft"] == {"status": "ok", "checked": 6, "agree": 6, "max_gap_bp": 0.0,
                                           "median_gap_bp": 0.0}
    assert qc.message(rec) is None  # no keys for Cantex is by setup, not news


def test_a_gap_is_asked_again_and_a_gap_that_holds_is_told():
    state = {"n": 0}
    pools = {"USDCx": (D(4_000_000), D(430_000), D("0.003"))}

    async def read():
        state["n"] += 1
        return pools

    async def quote(tok, sell_cc, amount):  # the venue pays 5 bp less than our math says
        return m.FORMULAS["tradecraft"](*pools[tok])(sell_cc, amount) * D("0.9995"), None
    rows = _run(read, quote)
    assert state["n"] == 1 + len(rows)  # every failing trade read the pool again before failing
    assert not any(r["ok"] for r in rows) and rows[0]["gap_bp"] == pytest.approx(5.0, abs=0.01)
    text = qc.message(qc.summarise(rows, {}, {}, 1))
    assert "tradecraft: 6 of 6 trades off the venue's own quote, largest 5.0 bp" in text


def test_a_pool_that_moved_between_the_reads_is_not_a_gap():
    first = {"USDCx": (D(4_000_000), D(430_000), D("0.003"))}
    moved = {"USDCx": (D(4_010_000), D(429_000), D("0.003"))}
    seen = {"n": 0}

    async def read():
        seen["n"] += 1
        return first if seen["n"] == 1 else moved

    async def quote(tok, sell_cc, amount):  # the venue already prices the moved pool
        return m.FORMULAS["tradecraft"](*moved[tok])(sell_cc, amount), None
    rows = _run(read, quote)
    assert all(r["ok"] for r in rows)


def test_cantex_pair_fees_reach_every_net_figure(tmp_path):
    c = collect.Collector(tmp_path / "www")
    asyncio.run(c.http.aclose())
    (tmp_path / "www-state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "www-state" / "quote_check.json").write_text(json.dumps(
        {"t": int(time.time()), "cantex_fee_cc": {"USDCx": "3.4877", "CBTC": "1.3625"}}))
    c._pair_fees(int(time.time()))
    usdcx = m.Hop("cantex", m.CC, "USDCX", D("0.106"), lambda a, x: x)
    cbtc = m.Hop("cantex", m.CC, "CBTC", D("0.0000013"), lambda a, x: x)
    assert m.swap_fee_usd("cantex", CC_USD, usdcx) == D("3.4877") * CC_USD
    assert m.path_fee_usd("cantex", (usdcx, cbtc), CC_USD) == (D("3.4877") + D("1.3625")) * CC_USD
    # a pair without a measured fee, and every other venue, keep the table's point
    other = m.Hop("cantex", m.CC, "HANDL", D(1), lambda a, x: x)
    assert m.swap_fee_usd("cantex", CC_USD, other) == m.NETWORK_FEE["cantex"][1] * CC_USD
    assert m.fee_table_json()["cantex"]["pairs_cc"] == {"CC/CBTC": 1.3625, "CC/USDCX": 3.4877}
    # a stale record is dropped: every pair is back on the point
    (tmp_path / "www-state" / "quote_check.json").write_text(json.dumps(
        {"t": int(time.time()) - 13 * 3600, "cantex_fee_cc": {"USDCx": "3.4877"}}))
    c._pair_fees(int(time.time()))
    assert m.PAIR_FEE_CC == {}


def test_a_ladder_row_nets_each_venues_pair_fee():
    m.PAIR_FEE_CC.clear()
    m.PAIR_FEE_CC[("cantex", frozenset((m.CC, "USDCX")))] = D("3.4877")
    try:
        pool = m.VenuePool("cantex", "USDCx", D(1_000_000), D(106_000), D("0.003"), D(0),
                           m.FORMULAS["cantex"](D(1_000_000), D(106_000), D("0.003")), "cantex", D("0.003"))
        tc = m.VenuePool("tradecraft", "USDCx", D(1_000_000), D(106_000), D("0.003"), D(0),
                         m.FORMULAS["tradecraft"](D(1_000_000), D(106_000), D("0.003")), "tradecraft", D("0.003"))
        row = m.ladder({"cantex": pool, "tradecraft": tc}, CC_USD, D("0.106"))[0]  # sell $100 of CC
        assert row["fee_usd"]["cantex"] == pytest.approx(float(D("3.4877") * CC_USD))
        assert row["out_net"]["cantex"] == pytest.approx(row["out"]["cantex"] - float(D("3.4877") * CC_USD))
    finally:
        m.PAIR_FEE_CC.clear()
