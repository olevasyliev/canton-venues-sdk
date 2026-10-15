"""The 2026-10-15 re-audit of the 14.10 fixes: what the new code published wrong on a day CC fell 12.5%
and Tradecraft's CC/USDCx pool stood still at +6.75%. One test per finding, each failing on the code
before the fix; the tick tests run a whole collector tick on fake venues (test_accuracy_batch1._tick)."""

from __future__ import annotations

import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import collect  # noqa: E402
import model as m  # noqa: E402
import venue_pages as vp  # noqa: E402
from test_accuracy_batch1 import TEMPLE_CC_MID, _state, _tick, _Venue  # noqa: E402
from test_accuracy_batch3 import _pp  # noqa: E402
from test_accuracy_batch3 import tick as batch3_tick  # noqa: E402,F401  (the fixture, by name)

SITE = Path(__file__).resolve().parents[1] / "examples" / "canton_venues"
D = Decimal


def _book(venue, symbol, mid, size="1", step="1", quote_usd="1"):
    mid, size, step = D(mid), D(size), D(step)
    return m.BookVenue(venue, symbol, tuple((mid - step * (i + 1), size) for i in range(40)),
                       tuple((mid + step * (i + 1), size) for i in range(40)), D(quote_usd), D("0.0001"))


# --- 1. A round trip pays and is paid back in one dollar token ---------------------------------------

def test_no_round_trip_from_usdcx_into_usdc_b():
    # CBTC at the same price on Temple's USDCx book and Rocky's USDC.B book: no spread. Valued USDCx
    # at 0.9996 and USDC.B at $1 (the 14.10 code), selling into USDC.B "made" 4 bp: $54.87 on
    # $99,686 went to the channel on 2026-10-15 03:27 UTC
    temple = m.book_hop(_book("temple", "CBTC/USDCx", "83700", "5"), "CBTC", "USDCX")
    rocky = m.book_hop(_book("rocky", "CBTC-USDCB", "83700", "5"), "CBTC", "USDC.B")
    routes = m.DollarRoutes({"temple": [m.Route("temple", "CBTC", "USDCX", "CBTC", ((temple,),))],
                             "rocky": [m.Route("rocky", "CBTC", "USDC.B", "CBTC", ((rocky,),))]},
                            {"USDCX": D("0.9996"), "USDC.B": D(1)})
    assert m.usd_scan("CBTC", routes, D("0.106")) == []  # no dollar token both venues trade


def test_usdc_b_counts_as_usdcx_in_the_dollar_routes(tmp_path):
    c = collect.Collector(tmp_path)
    books = {"CBTC": {"cantex": SimpleNamespace(token="CBTC")}}
    out = c._dollar_routes(books, {}, {"CBTC": {}}, {"USDC.B": D("0.999")}, D("0.9996"))
    assert out["CBTC"].dollar == {"USDCX": D("0.9996"), "USDC.B": D("0.9996")}


def test_the_arbitrage_record_restarts_at_v4(tmp_path):
    c = collect.Collector(tmp_path)
    c.desk["arb"] = {"v": 3, "trades": [{"net_usd": 54.87, "t": 1}], "pnl_usd": 221.81, "open": []}
    c._paper({}, D("0.106"), D("0.106"), [], 1_792_050_000)
    assert c.desk["arb"]["v"] == 4 and c.desk["arb"]["trades"] == [] and c.desk["arb"]["pnl_usd"] == 0


# --- 2. Dollar sizes are converted at the market price ----------------------------------------------

def test_a_dollar_size_is_that_many_dollars_and_costs_are_against_the_market(tmp_path, monkeypatch):
    # the thin Cantex pool quotes CC at 0.118, Temple's deep book at 0.1172: the pools' blend made a
    # "$50K" buy spend 50,341 USDCx and Temple "cost" -68 bp (live 15.10: 52.2K and -432 bp)
    pair = next(p for p in _tick(tmp_path, monkeypatch)["execution"]["pairs"] if p["key"] == "USDCX")
    assert pair["market_mid"] == pytest.approx(float(TEMPLE_CC_MID), rel=2e-4)
    for row in (r for r in pair["rows"] if r["side"] == "buy"):
        assert row["amount_in"] == pytest.approx(row["size_usd"], rel=1e-3)
    sells = [r for r in pair["rows"] if r["side"] == "sell"]
    assert all(-5 < r["cost_bps"]["temple"] < 50 for r in sells)


# --- 3. Rocky's depth over two books says so ----------------------------------------------------------

def test_depth_summed_over_two_books_names_both(batch3_tick):  # noqa: F811
    rocky = next(t for t in batch3_tick["tokens"]["tokens"] if t["key"] == "CBTC")["venues"]["rocky"]
    assert rocky["books"] == 2
    assert vp.depth_pair({"symbol": "CBTC", "market": "CBTC-USDCX", "books": 2}) == "CBTC, 2 books"
    assert vp.depth_pair({"symbol": "CBTC", "market": "CBTC-USDCX", "books": 1}) == "CBTC/USDCx"


# --- 4. A token that barely traded shows no peg gap and no 24h move ------------------------------------

def test_a_quiet_token_has_no_peg_figure_and_no_24h_change(tmp_path, monkeypatch):
    out = _tick(tmp_path, monkeypatch)
    usdm1 = next(t for t in out["tokens"]["tokens"] if t["key"] == "USDM1")  # $0 traded in the fixture
    assert usdm1["change_24h"] is None
    row = next(a for a in out["premium"]["assets"] if a["key"] == "USDM1")
    assert row["status"] == "traded under $1K in 24h" and "premium" not in row
    page = (SITE / "site" / "index.html").read_text()
    assert "!pegKeys.has(t.key)" in page  # no stablecoin among the top gainers


def test_own_samples_cover_seven_days_in_the_sparkline(tmp_path, monkeypatch):
    now = int(time.time())
    api = tmp_path / "www" / "api" / "v1"
    api.mkdir(parents=True)
    ts = list(range(now - 3 * 86400, now - 300, 300))  # three days of five-minute samples
    api.joinpath("history.json").write_text(json.dumps({"t": ts, "usd": {"USDM1": [1.0] * len(ts)}, "premium": {}}))
    usdm1 = next(t for t in _tick(tmp_path, monkeypatch)["tokens"]["tokens"] if t["key"] == "USDM1")
    assert 70 <= len(usdm1["spark_7d"]) <= 74  # hourly over the three days; it was the last 14 hours


# --- 5. A pool that stood still while the market moved is marked and left out --------------------------

def _seed_still(tmp_path, key, fp, since):
    state = tmp_path / "www-state"
    state.mkdir(parents=True, exist_ok=True)
    state.joinpath("pools_still.json").write_text(json.dumps({key: [fp, since, since]}))


def test_a_still_pool_off_the_market_is_marked_and_in_no_comparison(tmp_path, monkeypatch):
    # Cantex's CC/USDCx pool at 0.12, 2.4% over Temple's book, its reserves unchanged for three hours
    # (Pool Party's CC/USDCx pool at the book's price keeps a pair to compare)
    _seed_still(tmp_path, "cantex:CC/USDCX", "40000/4800", int(time.time()) - 3 * 3600)
    pp = _Venue(pools=[_pp("CC", "USDCx", "100000", "11720")], volume={})
    out = _tick(tmp_path, monkeypatch, states=[_state("USDCx", "40000", "4800")], optional={"poolparty": pp})
    marked = out["summary"]["pools_not_trading"]
    assert [(x["venue"], x["pair"]) for x in marked] == [("cantex", "CC/USDCx")]
    assert marked[0]["gap"] == pytest.approx(0.12 / float(TEMPLE_CC_MID) - 1, rel=1e-3)
    pair = next(p for p in out["execution"]["pairs"] if p["key"] == "USDCX")
    assert "cantex" not in pair["venues"] and pair["not_trading"]["cantex"]["since"] == marked[0]["since"]
    assert out["summary"]["cc_usd"] == pytest.approx(float(TEMPLE_CC_MID), rel=2e-4)
    cc = next(a for a in out["premium"]["assets"] if a["key"] == "CC")
    assert "cantex" not in cc["premium_by_venue"]
    assert not any(r["token"] == "USDCX" and "cantex" in (r["buy_on"], r["sell_on"]) for r in out["scan"]["routes"])
    lp = next(x for x in out["lp"]["pools"] if x["venue"] == "cantex" and x["pair"] == "CC/USDCx")
    assert lp["not_trading_since"] == marked[0]["since"]  # still listed, with its reserves
    assert out["pools"]["not_trading"] == marked


def test_a_pool_still_for_less_than_two_hours_or_near_the_market_is_not_marked(tmp_path, monkeypatch):
    _seed_still(tmp_path, "cantex:CC/USDCX", "40000/4800", int(time.time()) - 3600)
    assert _tick(tmp_path, monkeypatch, states=[_state("USDCx", "40000", "4800")])["summary"]["pools_not_trading"] == []
    other = tmp_path / "b"
    other.mkdir()
    _seed_still(other, "cantex:CC/USDCX", "40000/4700", int(time.time()) - 3 * 3600)  # 0.1175: 0.26% off
    assert _tick(other, monkeypatch, states=[_state("USDCx", "40000", "4700")])["summary"]["pools_not_trading"] == []


def test_a_pool_that_moves_starts_its_clock_again(tmp_path):
    c = collect.Collector(tmp_path / "www")
    c.pool_still = {"cantex:CC/USDCX": ["40000/4800", 1000, 1000]}
    pool = SimpleNamespace(cc_reserve=D(40000), token_reserve=D(4801))
    assert c._still_since({"USDCX": {"cantex": pool}}, 1000 + 3 * 3600) == {}
    assert c.pool_still["cantex:CC/USDCX"][1] == 1000 + 3 * 3600


# --- 6. The MCP quote prices the routes the site compares ----------------------------------------------

def test_quote_routes_rebuild_from_pools_json_as_the_site_compares(batch3_tick):  # noqa: F811
    # the MCP quote went CC to CBTC through Cantex's pools only (m.route): 7.8% short of Temple's two
    # orders via USDCx on 2026-10-15. venue_hops rebuilds every venue's markets from pools.json
    hops = m.venue_hops(batch3_tick["pools"])
    pair = next(p for p in batch3_tick["execution"]["pairs"] if p["key"] == "CBTC")
    row = next(r for r in pair["rows"] if r["side"] == "sell" and r["size_usd"] == 10_000)
    for v in ("temple", "poolparty"):
        got, path = m.Route(v, "CBTC", "CC", "CBTC", tuple(m.venue_paths(hops[v], "CC", "CBTC", m.MIN_MARKET_USD))) \
            .best(True, D(str(row["amount_in"])))
        assert float(got) == pytest.approx(row["out"][v], rel=1e-6) and len(path) == 2
    assert any(o["venue"] == "poolparty" and {o["a"], o["b"]} == {"CBTC", "USDC.B"}
               for o in batch3_tick["pools"]["other_pools"])
    src = (SITE / "mcp_server.py").read_text()
    assert "m.venue_hops(raw)" in src and "m.route(" not in src


def test_no_venue_claim_rests_on_a_pool_marked_not_trading():
    # Tradecraft's card read "The largest liquidity pool, $925K in its CC/USDCx pool" on 2026-10-15
    # while that pool had stood still for hours 6.75% off the market
    from test_venue_pages import data
    d = data()
    rules = [x["rule"] for x in vp.facts(d)["tradecraft"]["leads"]]
    assert "largest_pool" in rules and "pool_tvl" in rules
    for p in d["lp"]["pools"]:
        if p["venue"] == "tradecraft" and p["pair"] == "CC/USDCx":
            p["not_trading_since"] = int(time.time()) - 3 * 3600
    rules = [x["rule"] for x in vp.facts(d)["tradecraft"]["leads"]]
    assert "largest_pool" not in rules and "pool_tvl" not in rules
