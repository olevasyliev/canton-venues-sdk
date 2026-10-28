"""2026-10-28: an order book side that shows more of a token than its registry says exists is no depth
and no exit (Rocky's CBTC book: ~469 BTC offered, 40.8 CBTC in existence); Rocky's volume read from its
own candles once its 24h ticker fell to $740; Pool Party's volume counted differently, said so."""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import collect  # noqa: E402
import exit_cost  # noqa: E402
import supply  # noqa: E402
import volume_ledger  # noqa: E402
import weekly as wk  # noqa: E402

DAY = 86400


# --- how much exists, from the token's own registry ---------------------------------------------------

def test_parse_reads_the_listed_instrument_only_and_never_a_zero_supply():
    raw = {"instruments": [{"id": "CBTC", "totalSupply": "40.8028323957"},
                           {"id": "USDM0", "totalSupply": "0.0"}, {"id": "BAD", "totalSupply": "n/a"}]}
    assert supply.parse(raw, "CBTC") == pytest.approx(40.8028323957)
    assert supply.parse(raw, "USDM0") is None  # a supply of nothing would judge every book
    assert supply.parse(raw, "BAD") is None and supply.parse(raw, "eXAU") is None and supply.parse({}, "x") is None


def test_read_asks_each_admin_once_and_a_registry_that_fails_judges_nothing():
    asked = []

    async def get_json(url):
        asked.append(url)
        if "ember" in url:
            raise OSError("down")
        return {"instruments": [{"id": "CBTC", "totalSupply": "40.8"}, {"id": "USDCx", "totalSupply": "7001941.87"}]}

    got = asyncio.run(supply.read(get_json, [("CBTC", "cbtc::1", "CBTC"), ("USDCX", "cbtc::1", "USDCx"),
                                             ("EXAU", "ember::1", "eXAU")]))
    assert got == {"CBTC": 40.8, "USDCX": 7001941.87}
    assert len(asked) == 2 and supply.REGISTRY.format(admin="cbtc::1") in asked


def test_over_supply_judges_each_side_by_the_token_it_delivers():
    # Rocky's CBTC-USDCX on 2026-10-28: 228 BTC bid ($18.5M of USDCx) and 237 BTC offered
    bids = [(D("81462.4"), D("128")), (D("81000"), D("100"))]
    asks = [(D("81462.5"), D("137")), (D("81900"), D("100"))]
    assert supply.over_supply(bids, asks, 40.8, 7_001_941.87) == ["bids", "asks"]
    # Temple's CBTC book the same tick: 7.5 BTC bid, 8.3 offered, inside both supplies
    assert supply.over_supply([(D("81000"), D("7.5"))], [(D("81100"), D("8.3"))], 40.8, 7e6) == []
    # a token with no known supply (CC: its registry is not hosted there) is not judged
    assert supply.over_supply(bids, asks, None, None) == []
    # level objects (the adapters' book levels) and JSON string pairs read alike
    lv = SimpleNamespace(price=D("81000"), size=D("50"))
    assert supply.over_supply([lv], [["81100", "50"]], 40.8, 1e9) == ["asks"]


def test_the_collector_leaves_a_side_that_shows_more_than_exists_out_of_depth():
    c = collect.Collector.__new__(collect.Collector)
    c.supply, c.over = {"t": 1, "supply": {"CBTC": 40.8, "USDCX": 7e6}}, {}
    lv = lambda p, q: SimpleNamespace(price=D(p), size=D(q))  # noqa: E731
    rocky = SimpleNamespace(bids=[lv("81000", "228")], asks=[lv("81010", "237")])
    temple = SimpleNamespace(bids=[lv("81000", "7")], asks=[lv("81010", "8")])
    mid = D("81005")
    assert c._depth_backed("rocky", "CBTC-USDCX", rocky, "CBTC", "USDCX", mid, D(1)) == 0
    assert c.over[("rocky", "CBTC-USDCX")] == {"sides": ["bids", "asks"], "supply": {"CBTC": 40.8, "USDCX": 7e6}}
    assert c._depth_backed("temple", "CBTC/USDCx", temple, "CBTC", "USDCX", mid, D(1)) == 81000 * 7 + 81010 * 8
    assert ("temple", "CBTC/USDCx") not in c.over
    # pools.json carries the sides and the supplies that said so
    bv = collect.m.BookVenue("rocky", "CBTC-USDCX", ((D("81000"), D("228")),), ((D("81010"), D("237")),), D(1), D(0))
    d = c._book_json(bv, "USDCX")
    assert d["over_supply"] == ["bids", "asks"] and d["supply"]["CBTC"] == 40.8


def _book(venue: str, sym: str, bid_q: str, over=None) -> dict:
    d = {"venue": venue, "symbol": sym, "quote": "USDCX", "quote_usd": 1.0, "fee": 0.0,
         "bids": [["0.1", bid_q]], "asks": [["0.1001", bid_q]]}
    return d | ({"over_supply": over, "supply": {"CC": 1e6}} if over else {})


def test_exit_leaves_out_a_book_that_shows_more_than_exists_and_says_so():
    raw = {"t": 1, "cc_usd": 0.1, "pools": {}, "other_pools": [], "not_trading": [],
           "books": {"CC": {"CC/USDCx": _book("temple", "CC/USDCx", "1000000"),
                            "CC-USDCX": _book("rocky", "CC-USDCX", "50000000", ["bids", "asks"])}}}
    kept, left = exit_cost.backed(raw)
    assert list(kept["books"]["CC"]) == ["CC/USDCx"] and raw["books"]["CC"].keys() == {"CC/USDCx", "CC-USDCX"}
    assert left == [{"token": "CC", "venue": "rocky", "market": "CC-USDCX", "sides": ["bids", "asks"],
                     "supply": {"CC": 1e6}}]
    doc = exit_cost.build(raw, [{"key": "USDCX", "symbol": "USDCx", "price_usd": 1.0}], 1)
    cc = next(r for r in doc["tokens"] if r["key"] == "CC")
    assert "rocky" not in cc["capacity_usd"]["2pct"]["by_venue"] and doc["left_out"] == left


def test_the_mcp_quote_flags_a_route_through_such_a_book():
    src = (Path(collect.__file__).parent / "mcp_server.py").read_text()
    assert '"book_shows_more_than_exists"' in src and 'd.get("over_supply")' in src


# --- Rocky's volume from its own candles --------------------------------------------------------------

def _rocky_get(t: int, fail: str | None = None):
    calls = []

    def get(url, params):
        calls.append((url, dict(params)))
        if url.endswith("/exchangeInfo"):
            return {"symbols": [{"symbol": "CBTC-USDCX"}, {"symbol": "CBTC-USDM1"}, {"symbol": "EXAU-CBTC"}]}
        sym = params["symbol"]
        if sym == fail:
            raise OSError("timeout")
        # [open, o, h, l, c, base volume, close, quote volume, trades]
        rows = {"CBTC-USDCX": [t * 1000, 0, 0, 0, 0, "9", 0, "724411", 1240],
                "CBTC-USDM1": [t * 1000, 0, 0, 0, 0, "12", 0, "996276", 73],
                "EXAU-CBTC": [t * 1000, 0, 0, 0, 0, "1", 0, "0.05", 2],
                "CBTC-USDCB": [t * 1000, 0, 0, 0, 0, "0", 0, "0", 0]}
        return [rows[sym]] if sym in rows else []
    return get, calls


def test_rocky_day_sums_every_book_it_lists_or_listed_in_dollars():
    t = wk.ROCKY_DAY_FROM + 7 * DAY  # 27 Oct 2026
    get, calls = _rocky_get(t)
    rec = wk.rocky_day(get, t)
    vwap = 724411 / 9
    assert rec["usd"] == pytest.approx(724411 + 996276 + 0.05 * vwap)
    assert rec["trades"] == 1240 + 73 + 2 + 0 and rec["markets"]["CBTC-USDM1"] == 996276
    asked = {p["symbol"] for u, p in calls if u.endswith("/klines")}
    assert asked == {"CBTC-USDCX", "CBTC-USDM1", "EXAU-CBTC", *wk.ROCKY_PAST}
    k = next(p for u, p in calls if u.endswith("/klines"))
    assert (k["interval"], k["startTime"], k["endTime"]) == ("1d", t * 1000, (t + DAY) * 1000 - 1)


def test_rocky_day_fails_whole_when_a_book_does_not_answer_and_before_20_october():
    t = wk.ROCKY_DAY_FROM + DAY
    get, _ = _rocky_get(t, fail="CBTC-USDM1")
    with pytest.raises(OSError):
        wk.rocky_day(get, t)
    with pytest.raises(ValueError):
        wk.rocky_day(_rocky_get(t)[0], wk.ROCKY_DAY_FROM - DAY)


def test_fetch_days_reads_rocky_from_20_october_and_keeps_its_counts_off_the_public_cache():
    now = wk.ROCKY_DAY_FROM + 8 * DAY + 7 * 3600  # 28 Oct 07:00 UTC
    end = now - now % DAY
    rocky_calls = []

    def get(url, params):
        if url.startswith(wk.ROCKY):
            rocky_calls.append(params.get("startTime"))
            return _rocky_get(params["startTime"] // 1000 if "startTime" in params else 0)[0](url, params)
        if url == wk.TEMPLE:
            return {"total_volume_usd": 1e6, "markets": []}
        return {"data": {"volume_cc": "1000"}}

    counts: dict = {}
    cache = wk.fetch_days({}, {}, now, get, counts)
    days = sorted(int(t) for t in cache["rocky"])
    assert days == list(range(wk.ROCKY_DAY_FROM, end, DAY))  # 20-27 Oct, nothing before
    assert all("trades" not in r for r in cache["rocky"].values())
    assert counts["rocky"][str(days[-1])]["trades"] == 1240 + 73 + 2


def test_the_ledger_takes_rockys_day_record_and_notes_how_pool_party_counts():
    t = wk.ROCKY_DAY_FROM
    now = t + 2 * DAY
    cache = {"rocky": {str(t): {"usd": 3_073_603.0, "markets": {"CBTC-USDCX": 1_359_258.0}, "t": now}}}
    hist = {"daily": {"pool-party": {"source": "defillama", "t": now, "points": [[t, 2347.0]]}},
            "hourly": {"t": [t + DAY + 600], "venues": {"rocky": {"spot_volume": [3_181_305.0]},
                                                       "pool-party": {"spot_volume": [542.0]}}}}
    venues = [{"slug": "rocky", "name": "Rocky"}, {"slug": "pool-party", "name": "Pool Party"}]
    doc = volume_ledger.build(hist, cache, now, venues)
    rocky = next(v for v in doc["venues"] if v["slug"] == "rocky")
    day = next(d for d in rocky["days"] if d["t"] == t)
    assert day["source"] == "venue_day" and day["usd"] == 3_073_603.0 and day["markets_usd"]
    assert "20 Oct 2026" in rocky["own_record"]
    pp = next(v for v in doc["venues"] if v["slug"] == "pool-party")
    assert "both sides of every swap" in pp["note"] and "note" not in rocky
    assert "rocky" in wk.SOURCE  # the weekly page names every source it shows
