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
    assert _told(rec, rec) == [None, None]  # no keys for Cantex is by setup, not news


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
    rec = qc.summarise(rows, {}, {}, 1)
    text = _told(rec, rec)[1]
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


# --- the smaller findings of the 15.10 re-audit (report A) -------------------------------------------

def test_a_thin_two_swap_route_is_not_counted_at_a_size_above_its_thinnest_pool():
    # OneSwap's CC to HECTO to tf-usdt counted at $50K through a $49.5K pool (report A, finding 5)
    import venue_pages as vp
    from test_venue_pages import data
    d = data()
    slug = {v["rows"][0]: v["slug"] for v in vp.VENUES if v.get("rows")}

    def of(f, v, size):
        return (f.get(slug.get(v, v), {}).get("counts") or {}).get(("cc", size), {}).get("of", 0)
    pair = next(p for p in d["execution"]["pairs"] if p.get("kind") == "cc"
                and any(r["size_usd"] == 50_000 and len([o for o in r["out"].values() if o]) >= 2 for r in p["rows"]))
    row = next(r for r in pair["rows"] if r["size_usd"] == 50_000 and r["side"] == "sell")
    before = vp.facts(d)
    v = next(x for x in row["out"] if of(before, x, 50_000) > 0)
    row["route_usd"] = {v: 49_500.0}
    assert of(vp.facts(d), v, 50_000) == of(before, v, 50_000) - 1


def test_the_channels_feed_lists_only_what_was_sent():
    import alerts as al
    state = {"channel": {"active": []}}
    loud = [al.Alert(f"route:T{i}:cantex", "route", f"t{i}", f"t{i}", True) for i in range(4)]
    sent = al.channel(state["channel"], loud, 1_792_050_000)
    assert len(sent) == al.CHANNEL_PER_DAY and len(state["channel"]["feed"]) == al.CHANNEL_PER_DAY


def test_the_desk_books_one_spread_per_pool_per_tick(tmp_path):
    c = collect.Collector(tmp_path / "www")
    asyncio.run(c.http.aclose())
    trip = lambda tok, net, sell: {"token": tok, "buy_on": "tradecraft", "sell_on": sell, "net_usd": net,  # noqa: E731
                                   "clears": True, "size_usd": 1000}
    scan = [trip("USDCX", 432.0, "temple"), trip("USDCX", 131.0, "cantex"), trip("CBTC", 30.0, "cantex")]
    c._paper({}, D("0.106"), D("0.106"), scan, 1_792_050_000)
    booked = [(t["token"], t["sell_on"]) for t in c.desk["arb"]["trades"]]
    # the second USDCx exit sells into Tradecraft's same CC/USDCx pool: not booked again
    assert booked == [("USDCX", "temple"), ("CBTC", "cantex")]


def test_an_order_book_with_an_empty_side_is_no_market():
    b = m.BookVenue("rocky", "CBTC-USDCX", (), ((D(80000), D(1)),), D(1), D("0.00025"))
    with pytest.raises(ValueError):
        m.book_hop(b, "CBTC", "USDCX")
    raw = {"cc_usd": "0.1", "pools": {}, "books": {"CBTC": {"CBTC-USDCX": m.book_to_json(b, "USDCX")}}}
    assert not any(m.venue_hops(raw).values())  # skipped, not a crash in the MCP quote


# --- OneSwap: checked against its own quotes, its fee by direction ----------------------------------

def test_oneswap_is_priced_on_the_input_less_the_fee_its_quote_names():
    pools = {"USDCx": (D(1_000_000), D(106_000), D("0.003"))}

    async def read():
        return pools

    async def quote(tok, sell_cc, amount):  # OneSwap: fee carved from the input, in input units
        fee_in = D("11.56") if sell_cc else D("1.478")
        return m.FORMULAS["cp"](*pools[tok])(sell_cc, amount - fee_in), fee_in
    rows = asyncio.run(qc._check("oneswap", read, quote, "cp", CC_USD, fee_from_input=True))
    assert all(r["ok"] and r["gap_bp"] == 0 for r in rows)
    fees = qc.oneswap_fees(rows)
    assert fees["CC>USDCX"] == D("11.56")
    assert fees["USDCX>CC"] == pytest.approx(D("1.478") * D(1_000_000) / D(106_000), rel=D("1e-6"))  # in CC


def test_a_fee_by_direction_is_charged_by_what_is_sold():
    m.PAIR_FEE_CC.clear()
    m.PAIR_FEE_CC[("oneswap", m.CC, "USDCX")] = D("11.56")
    m.PAIR_FEE_CC[("oneswap", "USDCX", m.CC)] = D("13.84")
    try:
        hop = m.Hop("oneswap", m.CC, "USDCX", D("0.106"), lambda a, x: x)
        assert m.swap_fee_usd("oneswap", CC_USD, hop, m.CC) == D("11.56") * CC_USD
        assert m.swap_fee_usd("oneswap", CC_USD, hop, "USDCX") == D("13.84") * CC_USD
        assert m.swap_fee_usd("oneswap", CC_USD, hop) == D("13.84") * CC_USD  # no direction: the higher
        assert m.path_fee_usd("oneswap", (hop,), CC_USD, "USDCX") == D("13.84") * CC_USD
        assert m.fee_table_json()["oneswap"]["pairs_cc"] == {"CC>USDCX": 11.56, "USDCX>CC": 13.84}
    finally:
        m.PAIR_FEE_CC.clear()


def test_the_three_false_peg_alerts_of_14_10_leave_the_feed(tmp_path, monkeypatch):
    # founder 15.10: USDM1, FRXUSD.B and USDXLR "off their peg" at 16:10-16:26 were our method's, not the market's
    c = collect.Collector(tmp_path / "www")
    asyncio.run(c.http.aclose())
    keep = {"t": 1791990000, "kind": "route", "key": "route:CBTC:cantex", "text": "x"}
    feed = [{"t": t, "kind": "peg", "key": k, "text": "x"} for t, k in sorted(collect.WITHDRAWN_ALERTS)] + [keep]
    c.api.mkdir(parents=True, exist_ok=True)
    (c.api / "alerts.json").write_text(json.dumps({"active": [], "feed": feed, "channel": {"active": [], "feed": []}}))
    monkeypatch.setattr(collect.al, "evaluate", lambda *a, **k: [])
    asyncio.run(c._alerts([], [], [], 1_792_070_000))
    assert json.loads((c.api / "alerts.json").read_text())["feed"] == [keep]


# --- the owner is told once per incident (founder 16.10: no message for one failed check) -----------

def _record(**venues):
    """One check's record: ``venue="ok"``, ``"gap"`` (1.7 bp on 2 of 72), ``"big"`` (12 bp) or ``"down"``."""
    rows, failed = [], {}
    for v, how in venues.items():
        if how == "down":
            failed[v] = "VenueRequestError: API error 429"
            continue
        gap = {"ok": 0.0, "gap": 1.7, "big": 12.0}[how]
        rows += [{"venue": v, "trade": "CC->CBTC", "size_usd": 100, "gap_bp": gap if i < 2 else 0.0,
                  "ok": gap <= 1 or i >= 2} for i in range(72)]
    return qc.summarise(rows, failed, {}, 1)


def _told(*checks):
    """The messages a run of checks sends, in order (None where nothing is sent)."""
    prev, out = {}, []
    for rec in checks:
        text, prev = qc.alerts(rec, prev)
        out.append(text)
    return out


def test_one_failed_check_is_not_told_and_two_in_a_row_are_told_once():
    ok, gap, down = _record(cantex="ok"), _record(cantex="gap"), _record(cantex="down")
    # 16.10 06:20 and 08:20 UTC: one gap, then a pass: nothing
    assert _told(ok, gap, ok) == [None, None, None]
    # a gap, then a 429: told on the second, once, then once when it agrees again
    sent = _told(gap, down, down, ok, ok)
    assert sent[0] is None and sent[2] is None and sent[4] is None
    assert "cantex: not checked (VenueRequestError: API error 429); 2 checks in a row" in sent[1]
    assert "cantex: agrees again, 72 of 72 trades" in sent[3]


def test_a_large_gap_on_one_check_is_not_told_and_unchecked_by_setup_never_counts():
    # 16.10 08:49 UTC: cETH->CC $100 31.6 bp off, to the digit minutes later (the pool had traded)
    big = _record(cantex="big")
    assert _told(big, _record(cantex="ok")) == [None, None]
    assert "largest 12.0 bp" in _told(big, big)[1]
    no_keys = qc.summarise([], {"cantex": qc.NO_KEYS, "oneswap": qc.NO_ONESWAP_KEY}, {}, 1)
    assert _told(no_keys, no_keys, no_keys) == [None, None, None]


def test_a_venue_that_does_not_answer_is_asked_again(monkeypatch):
    monkeypatch.setattr(qc, "RETRY_AFTER_S", 0)
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("API error 429")
        return ["rows"]
    assert asyncio.run(qc._twice("cantex", flaky)) == ["rows"] and len(calls) == 2

    async def down():
        raise RuntimeError("HTTP 503")
    with pytest.raises(RuntimeError):
        asyncio.run(qc._twice("tradecraft", down))


def test_the_run_keeps_the_count_between_checks_and_sends_only_on_the_second(tmp_path, monkeypatch):
    api, state = tmp_path / "api", tmp_path / "state"
    api.mkdir()
    state.mkdir()
    (api / "summary.json").write_text(json.dumps({"cc_usd": "0.106"}))
    monkeypatch.setattr(qc, "RETRY_AFTER_S", 0)
    monkeypatch.delenv("CANTEX_OPERATOR_KEY", raising=False)
    monkeypatch.delenv("ONESWAP_SDK_KEY", raising=False)
    sent = []
    monkeypatch.setattr(qc, "send", sent.append)

    async def down(cc_usd):
        raise RuntimeError("GET /health: HTTP 503: no available server")
    monkeypatch.setattr(qc, "_tradecraft", down)
    asyncio.run(qc.run(api, state, dry=False))
    assert sent == [] and json.loads((state / qc.ALERTS).read_text())["streak"]["tradecraft"] == 1
    asyncio.run(qc.run(api, state, dry=False))
    assert len(sent) == 1 and "tradecraft: not checked" in sent[0] and "2 checks in a row" in sent[0]
