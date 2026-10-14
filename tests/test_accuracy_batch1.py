"""Batch 1 of the 2026-10-14 accuracy audit: published numbers that were wrong. One test per finding,
each failing on the code before the fix; the first runs a whole collector tick on fake venues, so the
fixes are checked where the collector wires them, not only in the pure functions."""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import alerts as al  # noqa: E402
import channel  # noqa: E402
import collect  # noqa: E402
import model as m  # noqa: E402
import venue_history  # noqa: E402
import venue_pages  # noqa: E402

from cantonvenues.core.models import BookLevel, Instrument, OrderBook, Ticker  # noqa: E402
from cantonvenues.venues.cantex_public import Candle, PoolState  # noqa: E402

SITE = Path(__file__).resolve().parents[1] / "examples" / "canton_venues"
D = Decimal
CC_I = Instrument("DSO::1220", "Amulet")
INST = {s: Instrument(f"{s.lower()}::1", s) for s in ("USDCx", "CBTC", "eXAG", "USDM1", "TRKXRWA", "cETH", "USDC.B")}
TEMPLE_CC_MID = D("0.1172")


def _state(sym, cc, tok):
    return PoolState(f"cid-{sym}", f"pool-{sym}", f"CC-{sym.upper()}", CC_I, INST[sym], "CC", sym,
                     D(cc), D(tok), D("0.003"), D(tok) / D(cc), D(cc) * 2)


def _book(symbol, mid, size, tick):
    mid, size, tick = D(mid), D(size), D(tick)
    return OrderBook(symbol, datetime.now(UTC),
                     tuple(BookLevel(mid - tick * (i + 1), size) for i in range(60)),
                     tuple(BookLevel(mid + tick * (i + 1), size) for i in range(60)))


class _Venue:
    """An adapter double: connects, closes, and answers the reads the collector makes."""

    def __init__(self, **reads):
        self.reads, self.has_key = reads, True

    async def connect(self):
        if not self.reads:
            raise ConnectionError("not in this test")

    async def close(self):
        pass

    def __getattr__(self, name):
        if name not in self.reads:
            raise AttributeError(name)

        async def call(*_a, **_k):
            v = self.reads[name]
            got = v(*_a) if callable(v) else v
            return await got if inspect.isawaitable(got) else got
        return call


def _tick(tmp_path, monkeypatch, optional=None, states=(), cantex=None, gecko=None):
    """One collector tick: a thin Cantex CC/USDCx pool at 0.118 and Temple's deep CC/USDCx book at
    0.1172; Temple's settled volume; a dead TRKXRWA candle series; USDM1 untraded; Ekiden's XAG index.
    ``optional`` replaces optional venues (Rocky, Pool Party, ...), ``states`` adds Cantex pools,
    ``cantex`` replaces Cantex reads and ``gecko`` CoinGecko rows."""
    now = int(time.time())
    monkeypatch.setattr(m, "metals_open", lambda t: True, raising=False)  # the spot market trades
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    c = collect.Collector(tmp_path / "www")
    asyncio.run(c.http.aclose())
    hour = (now // 3600) * 3600 * 1000
    cc_bars = [Candle(hour - k * 3_600_000, D("0.118"), D("0.118"), D("0.118"), D("0.118"), D(1)) for k in range(220)]
    # a pool that stopped trading a week ago, its last bar a wick: the old code read it as the 24h change
    dead = [Candle(hour - (170 + k) * 3_600_000, D(1), D(1), D(1), D("0.01") if k == 0 else D("0.001"), D(1))
            for k in range(30)]
    states = [_state("USDCx", "30000", "3540"), _state("CBTC", "1000000", "1.4"),
              _state("eXAG", "200000", "418"), _state("USDM1", "500000", "59000"),
              _state("TRKXRWA", "300000", "330"), *states]
    c.cantex = _Venue(
        pool_states=states,
        volume={"fees_cc": "10", "lp_fees_cc": "9", "volume_cc": "1000000", "swap_count": "100"},
        tickers=[{"base_currency": "CBTC", "target_currency": "CC", "target_volume": "500000", "base_volume": "1"},
                 {"base_currency": "USDM1", "target_currency": "CC", "target_volume": "0", "base_volume": "0"}],
        markets=[{"symbol": "CC-USDCX", "source": "cantex"}, {"symbol": "TRKXRWA-CC", "source": "cantex"}],
        candles={"CC-USDCX": cc_bars, "TRKXRWA-CC": dead},
        tokens=[{"instrument_admin": i.admin, "instrument_id": i.id, "instrument_symbol": s} for s, i in INST.items()])
    c.cantex.reads.update(cantex or {})
    c.tradecraft = _Venue(pool_states=[], pools=[])
    books = {"CC/USDCx": _book("CC/USDCx", TEMPLE_CC_MID, "50000", "0.00001"),
             "CBTC/USDCx": _book("CBTC/USDCx", "83700", "0.5", "1")}
    temple = _Venue(tickers=[SimpleNamespace(symbol=s) for s in books], order_book=lambda s, *_: books[s],
                    settled_volume={"markets": [{"symbol": "CC/USDCx", "quote_volume": "4000000"},
                                                {"symbol": "CBTC/USDCx", "quote_volume": "1000000"}],
                                    "total_volume_usd": "5000000"})
    xag = Ticker("XAG-USDCx", D("56.11"), D("56.10"), D("56.12"), D(10), D(100), D(5600), D(0), None,
                 BookLevel(D("56.11"), D(1)), BookLevel(D("56.13"), D(1)))
    ekiden = _Venue(tickers=[xag], order_book=lambda *_: _book("XAG-USDCx", "56.12", "10", "0.01"))
    c.optional = {"rocky": _Venue(), "rocky_perp": _Venue(), "oneswap": _Venue(), "poolparty": _Venue(),
                  "temple": temple, "ekiden": ekiden, **(optional or {})}
    fresh = {"usd": 1.0, "last_updated_at": now}
    gecko = {"xreserve-bridged-usdc-canton": fresh, "canton-network": {"usd": 0.1173, "last_updated_at": now},
             "bitcoin": {"usd": 83700, "last_updated_at": now},
             "kinesis-silver": {"usd": 57.2, "last_updated_at": now},  # KAG: what the old code compared with
             **(gecko or {})}

    async def fake_json(url, params=None):
        if url == collect.GECKO:
            return gecko
        raise ConnectionError(url)  # DefiLlama and logos: not part of this test
    c._json = fake_json
    asyncio.run(c.tick())
    api = tmp_path / "www" / "api" / "v1"
    return {p.stem: json.loads(p.read_text()) for p in api.glob("*.json")}


@pytest.fixture
def tick(tmp_path, monkeypatch):
    return _tick(tmp_path, monkeypatch)


# --- 1. CC price, depth-weighted across every CC/USDCx market incl. Temple's book ---------------

def test_cc_price_follows_the_deep_book_not_the_thin_pool(tick):
    cc = tick["summary"]["cc_usd"]
    assert abs(cc / float(TEMPLE_CC_MID) - 1) < 2e-4  # the pool alone said 0.118, 68 bp higher
    row = next(a for a in tick["premium"]["assets"] if a["key"] == "CC")
    assert row["canton_usd"] == pytest.approx(cc) and row["premium"] < 0  # below global 0.1173, not above
    assert tick["pools"]["cc_usd"] == pytest.approx(cc)


def test_usdcx_is_not_read_off_its_peg_by_the_pools_gap_to_the_book(tick):
    # the pool quotes CC 0.68% above the book; valued at the book's CC price, the pool alone read
    # USDCx 0.68% under $1 (live 14.10 16:15: +0.27%), a false peg reading on a $1.4M token
    usdcx = next(t for t in tick["tokens"]["tokens"] if t["key"] == "USDCX")
    assert usdcx["price_usd"] == pytest.approx(1.0, abs=2e-4)
    row = next(a for a in tick["premium"]["assets"] if a["key"] == "USDCX")
    assert abs(row["premium"]) < 2e-4


def test_a_pool_token_is_valued_through_its_own_venues_usdcx_pool(tick):
    # USDM1 sits at 0.118 per CC in Cantex's pool, where CC is 0.118 USDCx: $1.00 on that venue. At the
    # market CC price (the book's 0.1172) it read 0.68% under its peg, the pools' gap to the book
    usdm1 = next(t for t in tick["tokens"]["tokens"] if t["key"] == "USDM1")
    assert usdm1["price_usd"] == pytest.approx(1.0, abs=2e-4)
    assert abs(next(a for a in tick["premium"]["assets"] if a["key"] == "USDM1")["premium"]) < 2e-4
    cc = next(a for a in tick["premium"]["assets"] if a["key"] == "CC")
    assert set(cc["premium_by_venue"]) == {"cantex", "temple"}  # the gap shows on CC, venue by venue
    assert cc["premium_by_venue"]["cantex"] > cc["premium_by_venue"]["temple"]


def test_samples_between_the_two_steps_are_dropped_but_cc(tmp_path):
    c = collect.Collector(tmp_path)
    asyncio.run(c.http.aclose())
    c.history = {"t": [100, 200, 300], "usd": {"CC": [0.11, 0.12, 0.12], "USDM1": [1.0, 1.007, 1.007]},
                 "premium": {"CC": [0.0, 0.001, 0.001], "USDM1": [0.0, 0.007, 0.007]},
                 "steps": [{**s, "t": 200} for s in collect.STEPS if s["id"] != "pool-tokens-in-venue"]}
    c._mark_steps(400)
    assert c.history["usd"]["USDM1"] == [1.0, None, None] and c.history["premium"]["USDM1"] == [0.0, None, None]
    assert c.history["usd"]["CC"] == [0.11, 0.12, 0.12]
    c._mark_steps(500)  # once only
    assert [s["id"] for s in c.history["steps"]].count("pool-tokens-in-venue") == 1


def test_the_step_is_marked_once_in_the_history(tick, tmp_path):
    steps = {s["id"]: s for s in tick["history"]["steps"]}
    assert steps["cc-price-depth-weighted"]["keys"] == ["CC"] and steps["cc-price-depth-weighted"]["t"]
    c = collect.Collector(tmp_path / "www")
    asyncio.run(c.http.aclose())
    c._mark_steps(int(time.time()) + 300)
    assert len(c.history["steps"]) == len(collect.STEPS)


def test_book_depth_weighs_in_the_cc_price():
    pool = m.VenuePool("cantex", "USDCx", D(30000), D(3540), D("0.003"), D("0.9"),
                       m.FORMULAS["cantex"](D(30000), D(3540), D("0.003")), "cantex", D("0.003"))
    b = _book("CC/USDCx", TEMPLE_CC_MID, "50000", "0.00001")
    book = m.CCBook("temple", "USDCx", m.BookVenue("temple", "CC/USDCx", tuple((x.price, x.size) for x in b.bids),
                                                   tuple((x.price, x.size) for x in b.asks), D(1), D("0.0001")))
    assert book.depth_usd() > 600_000 > m.amm_depth_usd(pool, D("0.118")) * 1000
    assert float(m.cc_price_usd({"cantex": pool}, D(1))) == pytest.approx(float(pool.mid))
    assert abs(m.cc_price_usd({"cantex": pool, "temple": book}, D(1)) / TEMPLE_CC_MID - 1) < D("0.0002")


# --- 2. Temple's CC/USDCx volume belongs to USDCx --------------------------------------------------

def test_temple_cc_volume_is_credited_to_usdcx(tick):
    usdcx = next(t for t in tick["tokens"]["tokens"] if t["key"] == "USDCX")
    assert usdcx["volume_24h_usd"] >= 4_000_000


# --- 3. Dollar routes: each venue's best route on its own markets, every swap paying its fee -------
# (batch 1 took a pool route's CC/USDCx leg on whichever venue was best, Temple's book included; batch 3
# keeps every route inside its venue, the rule F set for all venues)

def test_dollar_route_stays_inside_the_venue_and_pays_each_swap(tick):
    pair = next(p for p in tick["execution"]["pairs"] if p["key"] == "CBTC:USD")
    row = next(r for r in pair["rows"] if r["side"] == "buy" and r["size_usd"] == 10_000)
    assert row["swaps"] == {"cantex": 2, "temple": 1} and row["via"] == {"cantex": "CC"}
    cc_usd = D(str(tick["summary"]["cc_usd"]))
    assert row["fee_usd"]["cantex"] == pytest.approx(float(2 * m.network_fee_usd("cantex", cc_usd)))


# --- 4. Metals against spot (Ekiden's index, market hours only); no stock-token references --------

def test_silver_is_measured_against_spot_not_kag(tick):
    row = next(a for a in tick["premium"]["assets"] if a["key"] == "EXAG")
    assert row["reference_usd"] == pytest.approx(56.10) and row["reference"] == "silver spot"
    perp = next(p for p in tick["perps"]["markets"] if p["symbol"] == "XAG-USDCx")
    assert perp["spot_usd"] == pytest.approx(56.10)


def test_spot_metals_trade_sunday_evening_to_friday_close_new_york_time():
    at = lambda *a: int(datetime(*a, tzinfo=UTC).timestamp())  # noqa: E731  (October: New York is UTC-4)
    assert m.metals_open(at(2026, 10, 14, 15, 0))          # Wednesday afternoon
    assert not m.metals_open(at(2026, 10, 14, 21, 30))     # the daily hour from 17:00
    assert m.metals_open(at(2026, 10, 16, 20, 30))         # Friday 16:30
    assert not m.metals_open(at(2026, 10, 16, 21, 30))     # Friday after the close
    assert not m.metals_open(at(2026, 10, 17, 12, 0))      # Saturday
    assert not m.metals_open(at(2026, 10, 18, 21, 30))     # Sunday before 18:00
    assert m.metals_open(at(2026, 10, 18, 22, 30))         # Sunday after 18:00


def test_an_index_that_stands_still_in_market_hours_is_not_spot(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "metals_open", lambda t: True)
    c = collect.Collector(tmp_path)
    asyncio.run(c.http.aclose())
    assert c._metal("XAG", D("56.1"), 1000) == D("56.1")
    assert c._metal("XAG", D("56.1"), 1000 + collect.METAL_STILL_MAX_S) == D("56.1")
    assert c._metal("XAG", D("56.1"), 1001 + collect.METAL_STILL_MAX_S) is None
    assert c._metal("XAG", D("56.2"), 1002 + collect.METAL_STILL_MAX_S) == D("56.2")
    monkeypatch.setattr(m, "metals_open", lambda t: False)
    assert c._metal("XAG", D("56.3"), 5000) is None


def _premium_rows(tmp_path, books, prices, gecko, now):
    """``now`` is the history's last sample; the premium is read at the current time."""
    c = collect.Collector(tmp_path)
    asyncio.run(c.http.aclose())
    c.history = {"t": [now], "usd": {}, "premium": {}}
    return {a["key"]: a for a in c._premium(books, D("0.12"), gecko, {}, prices)}  # at time.time()


def test_stock_tokens_get_no_premium_against_tokenized_shares(tmp_path):
    now = int(time.time())
    pool = m.VenuePool("cantex", "SPYe", D(100000), D("15.5"), D("0.003"), D("0.9"), lambda s, a: a, "cp", D("0.003"))
    rows = _premium_rows(tmp_path, {"SPYE": {"cantex": pool}}, {"SPYE": D("774")},
                         {"sp500-xstock": {"usd": 776.4, "last_updated_at": now}}, now)
    assert rows["SPYE"]["status"] == "no spot price connected" and "premium" not in rows["SPYE"]


def test_an_old_coingecko_price_is_no_reference(tmp_path):
    now = int(time.time())
    pool = m.VenuePool("cantex", "CBTC", D(1000000), D("1.4"), D("0.003"), D("0.9"), lambda s, a: a, "cp", D("0.003"))
    gecko = {"bitcoin": {"usd": 83700, "last_updated_at": now - 3600},
             "canton-network": {"usd": 0.12, "last_updated_at": now}}
    rows = _premium_rows(tmp_path, {"CBTC": {"cantex": pool}}, {"CBTC": D("83700")}, gecko, now)
    assert rows["CBTC"]["status"] == "no reference price" and rows["CC"]["status"] == "ok"


# --- 5. A 24h/7d change never from a series that stopped ---------------------------------------

def test_a_dead_candle_series_gives_no_change(tick):
    t = next(x for x in tick["tokens"]["tokens"] if x["key"] == "TRKXRWA")
    assert t["history_source"] == "own samples" and t["change_24h"] is None  # +900% on the old code


def test_change_needs_a_recent_last_point():
    hour, now = 3_600_000, 400 * 3_600_000
    week_old = [(now - (170 + k) * hour, 1.0) for k in range(60)][::-1]
    assert m.change(week_old, now, 24 * hour) is None
    stale_base = [(now - 40 * hour, 1.0), (now, 2.0)]  # nearest point 16 h before the window starts
    assert m.change(stale_base, now, 24 * hour) is None


# --- 6. A token that did not trade raises no alert and leaves the pegs post ----------------------

def test_untraded_token_is_marked_and_skipped(tick):
    usdm1 = next(t for t in tick["tokens"]["tokens"] if t["key"] == "USDM1")
    assert usdm1["traded_24h"] is False and usdm1["liquidity_usd"] >= 100_000
    peg = [{"key": "USDM1", "kind": "peg", "status": "ok", "premium": -0.03, "symbol": "USDM1", "canton_usd": 0.97}]
    assert al.evaluate(tick["tokens"]["tokens"], peg, [], {}) == []


def test_pegs_post_skips_an_untraded_stablecoin():
    now = int(datetime(2026, 10, 16, 9, 5, tzinfo=UTC).timestamp())
    ts = [now - 300 * i for i in range(7 * 288)][::-1]
    hist = {"t": ts, "premium": {"USDM1": [0.02] * len(ts), "USDCX": [0.0] * len(ts)}}
    tokens = [{"key": "USDM1", "symbol": "USDM1", "liquidity_usd": 102_000, "traded_24h": False},
              {"key": "USDCX", "symbol": "USDCx", "liquidity_usd": 1_400_000, "traded_24h": True}]
    post = channel.pegs_post(hist, tokens, {"USDM1": "USDM1", "USDCX": "USDCx"}, now)
    assert "USDCx" in post.html and "USDM1" not in post.html


def test_pegs_post_reads_a_premium_from_its_method_step_on():
    now = int(datetime(2026, 10, 16, 9, 5, tzinfo=UTC).timestamp())
    ts = [now - 300 * i for i in range(7 * 288)][::-1]
    step = now - 2 * 86400
    col = [-0.02 if t < step else 0.001 for t in ts]  # against KAG before the step, spot after
    hist = {"t": ts, "premium": {"EXAG": col}, "steps": [{"id": "metals-against-spot", "keys": ["EXAG"],
                                                           "premium": True, "t": step}]}
    tokens = [{"key": "EXAG", "symbol": "eXAG", "liquidity_usd": 300_000, "traded_24h": True}]
    post = channel.pegs_post(hist, tokens, {"EXAG": "eXAG"}, now)
    assert "eXAG +0.10% vs silver spot (since 14 Oct)" in post.html


# --- 7. The home headline is our own venue total; outside figures carry their day ---------------

def test_spot_volume_change_is_our_own_readings_a_day_apart():
    t0 = 1_791_900_000
    state = {"hourly": {"t": [t0, t0 + 86400], "venues": {
        "temple": {"spot_volume": [30e6, 33e6]}, "cantex": {"spot_volume": [1e6, 1.1e6]}}}}
    assert venue_history.total_change(state, t0 + 86400 + 60) == pytest.approx(34.1 / 31 - 1)
    state["hourly"]["venues"]["cantex"]["spot_volume"][0] = None  # not the same venues: no change
    assert venue_history.total_change(state, t0 + 86400 + 60) is None


def test_the_dashboard_reads_no_defillama_volume():
    html = (SITE / "site" / "index.html").read_text()
    assert "dex_volume_24h" not in html and "dex_change_1d" not in html and "e.venues" not in html
    assert "spot_volume_24h_usd" in html and "tvl_day" in html


def test_market_overview_reads_our_own_volume():
    src = (SITE / "mcp_server.py").read_text()
    assert 'get("dex_volume_24h")' not in src and '"spot_volume_24h_usd": v.get("spot_volume_24h_usd")' in src


# --- 8. Stale copy -------------------------------------------------------------------------------

def test_stale_copy_is_gone(tick):
    assert "round_trip_cost_cc" not in tick["scan"]
    assert not any("feeds the CC premium board" in c for c in venue_pages.CAVEATS["temple"])
    assert "network fees are not deducted" not in (SITE / "site" / "index.html").read_text()
    assert "3 CC pool to pool" not in (SITE / "mcp_server.py").read_text()


def test_execution_post_names_the_assumed_network_fees(tmp_path):
    now = int(datetime(2026, 10, 21, 9, 5, tzinfo=UTC).timestamp())
    fees = m.fee_table_json()
    with (tmp_path / "2026-10-21.jsonl").open("w") as f:
        for h in range(30):
            f.write(json.dumps({"t": now - 3600 * h, "network_fee": fees,
                                "venues": {"USDCX": ["cantex", "poolparty", "temple", "tradecraft"]},
                                "rows": [["USDCX", "sell", 1000, "temple", 9.0, "temple", 10.0]]}) + "\n")
    post = channel.execution_post(channel.read_exec(tmp_path, now))
    assert "Pool Party and Temple publish no network fee, so theirs is assumed at 1.5 CC" in post.html
    assert "Rocky" not in post.html


# --- 9. The paper router restarts once Temple's book routes ----------------------------------------

def test_paper_router_restarts_at_v3(tmp_path):
    c = collect.Collector(tmp_path)
    asyncio.run(c.http.aclose())
    c.desk["router"] = {"v": 2, "fills": [{"venue": "cantex"}] * 400, "n": 400, "extra_usd": 9.0, "by_venue": {}}
    c._paper({}, D("0.12"), [], 1_791_990_000)
    assert c.desk["router"]["v"] == 3 and c.desk["router"]["fills"] == []


# --- 10. Cantex's measured network fee keeps its measured range ------------------------------------

def test_cantex_fee_range_is_the_measured_one():
    cc = D("0.12")
    lo, pt, hi = (m.network_fee_usd("cantex", cc, e) for e in ("low", "point", "high"))
    assert lo == D("0.67") * cc and pt == D("0.86") * cc and hi == D("1.20") * cc
