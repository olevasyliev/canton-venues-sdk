"""2026-10-27: a trade checked against every venue at its time (market archive + trade_check), the cost
of selling a token for dollars at any size (exit_cost), and the daily volume ledger (volume_ledger)."""

from __future__ import annotations

import gzip
import json
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import exit_cost  # noqa: E402
import market_archive  # noqa: E402
import model as m  # noqa: E402
import trade_check  # noqa: E402
import volume_ledger  # noqa: E402
import weekly  # noqa: E402

from cantonvenues import constant_product_output  # noqa: E402

CC_USD = 0.1
T0 = 1_793_100_000  # 2026-10-27, a five-minute tick


def _pool(venue: str, cc: str, usd: str, fee: str = "0.003") -> dict:
    return {"venue": venue, "token": "USDCx", "cc_reserve": cc, "token_reserve": usd, "formula": "cp",
            "fee_param": fee, "fee": fee, "lp_share": "0"}


def _raw(t: int = T0, cc_a: str = "1000000", usd_a: str = "100000", cc_b: str = "200000",
         usd_b: str = "20000", bids=None) -> dict:
    """Two AMM venues with a CC/USDCx pool each (a deep one and a thin one) and, optionally, Temple's
    CC/USDCx book."""
    raw = {"t": t, "cc_usd": CC_USD,
           "pools": {"USDCX": {"tradecraft": _pool("tradecraft", cc_a, usd_a),
                               "oneswap": _pool("oneswap", cc_b, usd_b)}},
           "books": {}, "other_pools": [], "not_trading": []}
    if bids is not None:
        raw["books"] = {"CC": {"CC/USDCx": {"venue": "temple", "symbol": "CC/USDCx", "quote": "USDCX",
                                            "quote_usd": 1.0, "fee": 0.0,
                                            "bids": [[str(p), str(q)] for p, q in bids],
                                            "asks": [["0.1001", "1000000"]]}}}
    return raw


TOKENS = [{"key": "USDCX", "symbol": "USDCx", "price_usd": 1.0}]
FEES = {"tradecraft": {"unit": "usd", "point": 0.10, "low": 0.10, "high": 1.40, "basis": "documented"},
        "oneswap": {"unit": "usd", "point": 1.30, "low": 1.0, "high": 2.0, "basis": "measured"},
        "temple": {"unit": "cc", "point": 1.5, "low": 1.5, "high": 1.5, "basis": "assumed"}}


def _cp(rin: str, rout: str, amount: D, fee: str = "0.003") -> D:
    return constant_product_output(D(rin), D(rout), amount, D(fee))


# --- the shared quote and the fee table it nets -------------------------------------------------------

def test_venue_quotes_prices_each_venue_and_nets_its_network_fee_from_the_table():
    raw = _raw()
    with m.fees_from(FEES):
        q = m.venue_quotes(raw, "CC", "USDCX", D(10_000), D(1))
    got, path, fee = q["tradecraft"]
    assert got == _cp("1000000", "100000", D(10_000)) and len(path) == 1 and fee == D("0.10")
    assert q["oneswap"][2] == D("1.30")


def test_fees_from_applies_a_pair_fee_and_restores_the_live_tables():
    before_venue, before_pair = dict(m.NETWORK_FEE), dict(m.PAIR_FEE_CC)
    table = {**FEES, "oneswap": {**FEES["oneswap"], "pairs_cc": {"CC>USDCX": 20.0}}}
    with m.fees_from(table):
        q = m.venue_quotes(_raw(), "CC", "USDCX", D(1_000), D(1))
        assert q["oneswap"][2] == D(20) * D(str(CC_USD))  # 20 CC at $0.10, in USDCx at $1
        assert m.NETWORK_FEE["tradecraft"][1] == D("0.1")
    assert m.NETWORK_FEE == before_venue and m.PAIR_FEE_CC == before_pair


# --- the market archive --------------------------------------------------------------------------------

def _archive(tmp_path: Path, raws: list[dict]) -> Path:
    d = tmp_path / "market"
    for raw in raws:
        market_archive.record(d, market_archive.reading(raw, TOKENS, FEES))
    return d


def test_archive_finds_the_readings_either_side_and_survives_a_cut_member(tmp_path):
    d = _archive(tmp_path, [_raw(T0), _raw(T0 + 300), _raw(T0 + 600)])
    before, after = market_archive.around(d, T0 + 400, 600)
    assert before["t"] == T0 + 300 and after["t"] == T0 + 600
    assert before["tokens"]["USDCX"] == ["USDCx", 1.0] and before["fees"] == FEES
    assert market_archive.around(d, T0 + 5000, 600) == (None, None)
    assert market_archive.first(d) == T0
    path = next(d.glob("*.jsonl.gz"))
    path.write_bytes(path.read_bytes() + gzip.compress(b'{"t":1793100900,"cc_usd"')[:-6])  # a crash mid-write
    assert market_archive.around(d, T0 + 400, 600)[0]["t"] == T0 + 300


def test_archive_prunes_old_days(tmp_path):
    d = _archive(tmp_path, [_raw(T0 - 500 * 86400), _raw(T0)])
    market_archive.prune(d, T0)
    assert [p.name for p in d.glob("*.jsonl.gz")] == ["2026-10-27.jsonl.gz"]


# --- the trade check -----------------------------------------------------------------------------------

def test_a_trade_is_measured_against_the_best_venue_after_its_network_fee(tmp_path):
    d = _archive(tmp_path, [_raw(T0)])
    best = _cp("1000000", "100000", D(10_000)) - D("0.10")  # tradecraft, after its $0.10 fee
    mine = _cp("200000", "20000", D(10_000)) - D("1.30")     # what oneswap gave
    r = trade_check.check(d, "CC", "USDCx", 10_000, str(mine), T0 + 120, venue="OneSwap")
    assert r["best_venue"] == "tradecraft"
    assert r["best_after_network_fee"] == pytest.approx(float(best), rel=1e-9)
    assert r["shortfall_bps"] == pytest.approx(float((best / mine - 1) * 10_000), abs=0.1)
    assert r["venues_that_returned_more"] == ["tradecraft"]
    assert r["your_venue"]["fill_vs_your_venue_bps"] == 0.0  # the fill was exactly oneswap's market
    assert r["your_venue"]["venue_choice_bps"] == pytest.approx(r["shortfall_bps"], abs=0.1)
    assert r["readings"][0]["minutes_from_trade"] == -2.0 and "tradecraft returned" in r["verdict"]


def test_the_shortfall_is_taken_at_the_reading_least_favourable_to_it(tmp_path):
    # the deep pool is richer at the later reading: the claim rests on the earlier, smaller gap
    d = _archive(tmp_path, [_raw(T0), _raw(T0 + 300, usd_a="103000")])
    fill = D("950")
    r = trade_check.check(d, "CC", "USDCx", 10_000, str(fill), "2026-10-27T11:22:00Z")
    low = _cp("1000000", "100000", D(10_000)) - D("0.10")
    assert r["best_after_network_fee"] == pytest.approx(float(low), rel=1e-9)
    assert r["shortfall_bps_range"][0] == r["shortfall_bps"] < r["shortfall_bps_range"][1]
    assert len(r["readings"]) == 2


def test_a_good_fill_says_so_and_a_trade_outside_the_record_says_where_it_starts(tmp_path):
    d = _archive(tmp_path, [_raw(T0)])
    r = trade_check.check(d, "CC", "USDCx", 10_000, 2_000, T0)
    assert r["shortfall_bps"] == 0.0 and r["venues_that_returned_more"] == [] and "at least as much" in r["verdict"]
    with pytest.raises(trade_check.CheckError, match="the record starts 2026-10-27"):
        trade_check.check(d, "CC", "USDCx", 10_000, 900, T0 - 3600)
    with pytest.raises(trade_check.CheckError, match="positive"):
        trade_check.check(d, "CC", "USDCx", 0, 900, T0)
    with pytest.raises(trade_check.CheckError, match="ISO"):
        trade_check.check(d, "CC", "USDCx", 10, 1, "yesterday")


def test_a_pool_marked_not_trading_at_that_tick_is_no_market_for_the_check(tmp_path):
    raw = _raw(T0)
    raw["not_trading"] = [{"venue": "tradecraft", "pair": "CC/USDCx", "since": T0 - 7200, "gap": 0.02}]
    d = _archive(tmp_path, [raw])
    r = trade_check.check(d, "CC", "USDCx", 10_000, 900, T0)
    assert "tradecraft" not in r["readings"][0]["by_venue"] and r["best_venue"] == "oneswap"


# --- exit cost -----------------------------------------------------------------------------------------

def _amm_share(cc: str, usd: str, sale_usd: float, fee_usd: str) -> D:
    amount = D(str(sale_usd)) / D(str(CC_USD))
    return (_cp(cc, usd, amount) - D(fee_usd)) / D(str(sale_usd))


def test_exit_ladder_and_capacity_match_the_pool_math():
    raw = _raw()
    with m.fees_from(FEES):
        doc = exit_cost.build(raw, TOKENS, T0)
    cc = next(x for x in doc["tokens"] if x["key"] == "CC")
    step = next(x for x in cc["ladder"] if x["usd"] == 10_000)
    assert step["best_venue"] == "tradecraft"
    assert step["best"] == pytest.approx(float(_amm_share("1000000", "100000", 10_000, "0.10")), abs=1e-6)
    cap = cc["capacity_usd"]["2pct"]
    tc = cap["by_venue"]["tradecraft"]
    # the largest sale inside 2%: just under it keeps 98% or more, just over it less
    assert _amm_share("1000000", "100000", tc * 0.999, "0.10") >= D("0.98") > _amm_share("1000000", "100000", tc * 1.001, "0.10")
    assert cap["limited_by"]["tradecraft"] == "band"
    assert cap["all_venues"] == pytest.approx(tc + cap["by_venue"]["oneswap"], abs=0.01)


def test_a_book_that_runs_out_before_the_band_says_so():
    raw = _raw(bids=[(D("0.1"), D("100000")), (D("0.0999"), D("100000"))])  # 200K CC bid, all within 0.1%
    with m.fees_from(FEES):
        doc = exit_cost.build(raw, TOKENS, T0)
    cap = next(x for x in doc["tokens"] if x["key"] == "CC")["capacity_usd"]["5pct"]
    # sizes are the sale's value at our price ($0.10): the book takes 200K CC, $20K of value
    assert cap["limited_by"]["temple"] == "book" and 19_900 < cap["by_venue"]["temple"] < 20_000


def test_the_worst_record_only_goes_down(tmp_path):
    rec = exit_cost.WorstRecord(tmp_path / "exit_worst.json")
    with m.fees_from(FEES):
        deep = exit_cost.build(_raw(), TOKENS, T0)
        thin = exit_cost.build(_raw(T0 + 300, usd_a="90000", cc_a="900000"), TOKENS, T0 + 300)
    rec.update(deep)
    rec.update(thin)
    rec.update(deep)  # a better tick later does not lift the worst
    worst = exit_cost.WorstRecord(tmp_path / "exit_worst.json").attach(deep)
    w = next(x for x in worst["tokens"] if x["key"] == "CC")["worst"]["50000"]
    thin_best = next(s for s in next(x for x in thin["tokens"] if x["key"] == "CC")["ladder"] if s["usd"] == 50_000)["best"]
    assert w["best"] == thin_best and w["t"] == T0 + 300 and worst["worst_since"] == T0


# --- the volume ledger --------------------------------------------------------------------------------

DAY = 86400
NOW = 1_793_100_000
D1 = NOW - NOW % DAY - DAY  # yesterday
D0 = D1 - DAY


def test_ledger_keeps_every_record_and_says_whether_they_agree():
    hist = {"daily": {"temple": {"source": "defillama", "t": NOW, "points": [[D0, 100.0], [D1, 200.5]]},
                      "rocky": {"source": "defillama", "t": NOW, "points": [[D0, 50.0], [D1, 50.0]]}},
            "hourly": {"t": [D1 + 300, D1 + DAY + 300],
                       "venues": {"temple": {"spot_volume": [101.0, 203.0]}, "rocky": {"spot_volume": [80.0, 90.0]}}}}
    cache = {"temple": {str(D1): {"usd": 200.0, "t": NOW, "markets": {"CC/USDCx": 150.0, "CBTC/USDCx": 50.0}}}}
    venues = [{"slug": "temple", "name": "Temple"}, {"slug": "rocky", "name": "Rocky"}, {"slug": "oneswap", "name": "OneSwap"}]
    doc = volume_ledger.build(hist, cache, NOW, venues)
    by = {v["slug"]: {r["t"]: r for r in v["days"]} for v in doc["venues"]}
    t1 = by["temple"][D1]
    assert t1["usd"] == 200.0 and t1["source"] == "venue_day" and t1["check"] == "agree"
    assert t1["figures"] == {"venue_day": 200.0, "venue_24h": 203.0, "outside": 200.5}
    assert t1["markets_usd"] == {"CBTC/USDCx": 50.0, "CC/USDCx": 150.0}
    assert "trade" not in json.dumps(doc["venues"]) and "on request" in doc["on_request"]
    r1 = by["rocky"][D1]  # the outside record repeats the day before to the cent: stuck, left out
    assert r1["figures"] == {"venue_24h": 90.0} and r1["check"] == "single" and r1["usd"] == 90.0
    r0 = by["rocky"][D0]
    assert r0["check"] == "differ" and r0["spread"] == pytest.approx(0.6) and r0["source"] == "venue_24h"
    assert "oneswap" not in by
    assert doc["by_day"][-1] == {"day": "2026-10-26", "t": D1, "usd": 290.0, "venues": ["Temple", "Rocky"], "missing": []}


def test_trade_counts_go_to_the_private_record_only_and_a_final_day_keeps_its_dollar_figure():
    end = NOW - NOW % DAY
    old = end - 20 * DAY
    cache = {"cantex": {str(old): {"cc": 1000.0, "usd": 100.0, "cc_usd": 0.1, "t": old + DAY + 7 * 3600}},
             "temple": {str(old): {"usd": 5.0, "t": old + DAY + 7 * 3600}}}
    counts: dict = {}
    calls = []

    def get(url, params):
        calls.append(url)
        if url == weekly.TEMPLE:
            return {"total_volume_usd": 5.0, "trade_count": 3, "markets": [{"symbol": "CC/USDCx", "quote_volume": 5.0, "trade_count": 3}]}
        return {"data": {"volume_cc": "1000", "swap_count": "7.0000000000"}}
    weekly.fetch_days(cache, {}, NOW, get, counts=counts)
    c = cache["cantex"][str(old)]
    assert c["usd"] == 100.0 and c["cc_usd"] == 0.1  # re-read for its count, the converted figure stays
    assert cache["temple"][str(old)]["markets"] == {"CC/USDCx": 5.0}
    # days.json sits under the web root: no count in it; the counts are in the private record
    assert "trade" not in json.dumps(cache)
    assert counts["cantex"][str(old)] == {"trades": 7}
    assert counts["temple"][str(old)] == {"trades": 3, "market_trades": {"CC/USDCx": 3}}
    # a day older than the card's two weeks that was never read is not reached for
    assert str(end - 25 * DAY) not in cache["cantex"]
    # Rocky's days fail here (this fake answers it as Cantex) and are asked again: not counted below
    n = len([u for u in calls if not u.startswith(weekly.ROCKY)])
    weekly.fetch_days(cache, {}, NOW, get, counts=counts)  # every day read is final and counted
    assert len([u for u in calls if not u.startswith(weekly.ROCKY)]) == n
    # on request: the average trade per venue and day, Temple per market
    rows = {(r["venue"], r["day"]): r for r in volume_ledger.averages(cache, counts, NOW)}
    t = rows[("temple", volume_ledger._day(old))]
    assert t["avg_trade_usd"] == pytest.approx(5 / 3, abs=0.01) and t["markets"]["CC/USDCx"]["trades"] == 3
    assert rows[("cantex", volume_ledger._day(old))]["avg_trade_usd"] == pytest.approx(100 / 7, abs=0.01)


def test_exit_and_ledger_files_round_trip_as_json():
    with m.fees_from(FEES):
        doc = exit_cost.build(_raw(), TOKENS, T0)
    assert json.loads(json.dumps(doc))["sell_for"] == "USDCx"
