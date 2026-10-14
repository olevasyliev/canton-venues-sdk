"""Batch 3 of the 2026-10-14 accuracy audit: the whole market, compared the right way. One test per
finding, each failing on the code before the fix; the tick tests run a whole collector tick on fake
venues (test_accuracy_batch1._tick) with Rocky's books and Pool Party's pools added."""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import collect  # noqa: E402
import model as m  # noqa: E402
import venue_pages as vp  # noqa: E402
from test_accuracy_batch1 import INST, _book, _state, _tick, _Venue  # noqa: E402
from test_venue_pages import data  # noqa: E402

from cantonvenues.core.models import Instrument  # noqa: E402
from cantonvenues.venues.reserves import ReservePool  # noqa: E402

D = Decimal
ROCKY_BOOKS = {"CBTC-USDCX": ("CBTC", "USDCx", "83690"), "CBTC-USDCB": ("CBTC", "USDC.B", "83710"),
               "CETH-USDCB": ("cETH", "USDC.B", "2441")}


def _pp(sym_a, sym_b, ra, rb):
    ida = "Amulet" if sym_a == "CC" else INST[sym_a].id
    return ReservePool("poolparty", f"pp-{sym_a}-{sym_b}", sym_a, sym_b, Instrument("", ida),
                       Instrument("", INST[sym_b].id), D(ra), D(rb), D("0.003"), "test")


@pytest.fixture
def tick(tmp_path, monkeypatch):
    rocky = _Venue(markets=[SimpleNamespace(symbol=s, base=b, quote=q) for s, (b, q, _) in ROCKY_BOOKS.items()],
                   tickers=[SimpleNamespace(symbol=s, turnover_24h=D(1)) for s in ROCKY_BOOKS],
                   order_book=lambda s, *_: _book(s, ROCKY_BOOKS[s][2], "0.5" if ROCKY_BOOKS[s][0] == "CBTC" else "20",
                                                  "1" if ROCKY_BOOKS[s][0] == "CBTC" else "0.05"))
    poolparty = _Venue(pools=[_pp("CC", "USDC.B", "400000", "46880"),        # $1.00 per USDC.B
                              _pp("CBTC", "USDC.B", "0.5", "41850"),         # $83.7K per CBTC, ~$84K deep
                              _pp("CBTC", "USDCx", "0.001", "10")],          # $94 pool at $10K per CBTC
                       volume={})
    return _tick(tmp_path, monkeypatch, optional={"rocky": rocky, "poolparty": poolparty},
                 states=[_state("cETH", "1000000", "48")])  # $2,441 per cETH


# --- 1. Pools without CC count in liquidity and pool counts ---------------------------------------

def test_pools_without_cc_are_listed_and_valued(tick):
    others = {(o["venue"], o["pair"]): o["tvl_usd"] for o in tick["lp"]["other_pools"]}
    assert others[("poolparty", "CBTC/USDC.B")] == pytest.approx(0.5 * 83_700 + 41_850, rel=0.01)
    assert others[("poolparty", "CBTC/USDCx")] < 100


def test_venue_pages_count_pools_without_cc():
    d = data()
    d["lp"]["other_pools"] = [{"venue": "poolparty", "pair": "CBTC/USDC.B", "tvl_usd": 56_000.0,
                               "reason": "pairs two tokens, not CC"}]
    f = vp.facts(d)["pool-party"]
    assert f["tvl"] == pytest.approx(12_000 + 56_000) and f["pool_n"] == 2
    assert vp.pools_text(f) == "2 pools"
    assert any("pair two tokens other than CC" in x for x in f["method"])


# --- 2. Rocky: every dollar book counts; USDC.B at par with a depeg guard -----------------------

def test_rocky_counts_both_of_its_cbtc_books(tick):
    cbtc = next(t for t in tick["tokens"]["tokens"] if t["key"] == "CBTC")
    each = [float(m.book_depth_usd(b.bids, b.asks, b.mid_price, D(1)))
            for b in (_book(s, ROCKY_BOOKS[s][2], "0.5", "1") for s in ("CBTC-USDCX", "CBTC-USDCB"))]
    assert cbtc["venues"]["rocky"]["depth_1pct_usd"] == pytest.approx(sum(each), rel=1e-6)


def test_ceth_gets_a_dollar_view_on_rockys_usdc_b_book(tick):
    pair = next(p for p in tick["execution"]["pairs"] if p["key"] == "CETH:USD")
    assert "rocky" in pair["venues"] and pair["books"]["rocky"]["market"] == "CETH-USDCB"
    assert all(r["swaps"]["rocky"] == 1 for r in pair["rows"] if "rocky" in r["swaps"])


def test_usdc_b_off_its_peg_is_no_dollar(tmp_path):
    c = collect.Collector(tmp_path)
    asyncio.run(c.http.aclose())
    b = _book("CETH-USDCB", "2441", "20", "0.05")
    bv = m.BookVenue("rocky", "CETH-USDCB", tuple((x.price, x.size) for x in b.bids),
                     tuple((x.price, x.size) for x in b.asks), D(1), D("0.00025"))
    hops = {"rocky": [m.book_hop(bv, "CETH", "USDC.B")]}
    books = {"CETH": {"cantex": m.VenuePool("cantex", "cETH", D(1), D(1), D(0), D(0), lambda s, a: a)}}
    near = c._dollar_routes(books, hops, {"CETH": {}}, {"USDC.B": D("1.003")}, D(1))
    off = c._dollar_routes(books, hops, {"CETH": {}}, {"USDC.B": D("1.02")}, D(1))
    assert near["CETH"].venues == ["rocky"] and off["CETH"].venues == []


# --- 3. The best route inside each venue, up to two swaps, the same rule for all -----------------

def test_temple_and_pool_party_reach_cbtc_from_cc_in_two_swaps(tick):
    pair = next(p for p in tick["execution"]["pairs"] if p["key"] == "CBTC")
    assert {"temple", "poolparty", "cantex"} <= set(pair["venues"])
    row = next(r for r in pair["rows"] if r["side"] == "sell" and r["size_usd"] == 10_000)
    assert row["via"]["temple"] == "USDCX" and row["via"]["poolparty"] == "USDC.B"
    cc_usd = D(str(tick["summary"]["cc_usd"]))
    assert row["fee_usd"]["temple"] == pytest.approx(float(2 * m.network_fee_usd("temple", cc_usd)))
    assert pair["books"]["temple"]["market"] == "CBTC/USDCx"


def test_a_thin_pool_is_no_route_and_makes_no_spread(tick):
    pair = next(p for p in tick["execution"]["pairs"] if p["key"] == "CBTC:USD")
    buy = next(r for r in pair["rows"] if r["side"] == "buy" and r["size_usd"] == 100)
    assert buy["swaps"]["poolparty"] == 1 and buy["cost_bps"]["poolparty"] > 0  # the USDC.B pool, not the $94 one
    assert all(r["net_usd"] < 25 for r in tick["scan"]["routes"])


def test_two_swap_paths_need_markets_of_a_thousand_dollars():
    swap = lambda a_to_b, x: x  # noqa: E731
    thin_cc = m.Hop("v", "CC", "X", D(1), swap, usd=D(500))
    deep = m.Hop("v", "X", "Y", D(1), swap, usd=D(50_000))
    thin = m.Hop("v", "Z", "Y", D(1), swap, usd=D(300))
    assert m.venue_paths([thin_cc], "CC", "X", D(1000)) == [(thin_cc,)]  # a CC pool asked directly stays
    assert m.venue_paths([thin_cc, deep], "CC", "Y", D(1000)) == []
    assert m.venue_paths([thin], "Z", "Y", D(1000)) == []


def test_a_lead_pays_the_network_fee_of_each_of_its_swaps():
    def counts(swaps_best):
        d = data()
        pair = d["execution"]["pairs"][0]  # USDCx: Cantex against Tradecraft
        pair["rows"] = [{"side": "sell", "size_usd": 1_000, "out": {"cantex": ratio, "tradecraft": 1.0},
                         "best": "cantex", "edge_bps": 0.0, "swaps": {"cantex": swaps_best, "tradecraft": 1}}]
        return vp.facts(d)["cantex"]["counts"][("cc", 1_000)]["won"]

    # an edge that covers one Cantex network fee but not two
    one = vp.net_edge_bps("cantex", "tradecraft", 1.0, 1.0, 1_000, 1, 1, 0.12)
    two = vp.net_edge_bps("cantex", "tradecraft", 1.0, 1.0, 1_000, 2, 1, 0.12)
    ratio = 1 + (-(one + two) / 2) / 10_000
    assert counts(1) == 1 and counts(2) == 0
