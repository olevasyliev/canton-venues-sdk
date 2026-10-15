"""Batch 2 of the 2026-10-14 accuracy audit: one bad value must never stop the site, and nothing may
fail silently. One test per finding (the audit's offline replays, made small), each failing on the
code before the fix."""

from __future__ import annotations

import asyncio
import math
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import alerts as al  # noqa: E402
import collect  # noqa: E402
import crosscheck as cx  # noqa: E402
import spread_study  # noqa: E402
import venue_pages as vp  # noqa: E402
import watchdog as wd  # noqa: E402
from test_accuracy_batch1 import _book, _tick, _Venue  # noqa: E402
from test_crosscheck import NOW as CX_NOW  # noqa: E402
from test_crosscheck import fake_get, published  # noqa: E402
from test_venue_pages import data  # noqa: E402

from cantonvenues import (  # noqa: E402
    CantexPublicData,
    EkidenAdapter,
    OneSwapPublicData,
    PoolPartyPublicData,
    RockyAdapter,
    TradecraftAdapter,
)

D = Decimal


def _client(routes: dict[str, object]) -> httpx.AsyncClient:
    """An HTTP client that answers each path ending with a key of ``routes`` with its JSON."""
    def handler(req: httpx.Request) -> httpx.Response:
        body = next((v for k, v in routes.items() if req.url.path.endswith(k)), None)
        if body is None:
            return httpx.Response(404, text="no route")
        return httpx.Response(200, json=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


# --- 1. Single fields that stopped the whole tick ------------------------------------------------

def test_cantex_volume_fields_null_or_missing(tmp_path, monkeypatch):
    c = CantexPublicData(client=_client({"/volume": {"data": None}}))
    assert run(c.volume()) == {}
    t = _tick(tmp_path, monkeypatch, cantex={"volume": {"fees_cc": "10", "lp_fees_cc": None, "volume_cc": None,
                                                        "swap_count": None}})
    assert t["summary"]["status"] == "ok" and t["summary"]["cantex_24h"]["volume_usd"] is None


def test_cantex_ticker_volume_null(tmp_path, monkeypatch):
    t = _tick(tmp_path, monkeypatch, cantex={"tickers": [
        {"base_currency": "CBTC", "target_currency": "CC", "target_volume": None, "base_volume": None}]})
    assert next(x for x in t["tokens"]["tokens"] if x["key"] == "CBTC")["volume_24h_usd"] is not None


def test_cantex_market_without_symbol(tmp_path, monkeypatch):
    t = _tick(tmp_path, monkeypatch, cantex={"markets": [{"source": "cantex"}, {"symbol": "CC-USDCX", "source": "cantex"}]})
    assert t["summary"]["status"] == "ok"


def test_cantex_incomplete_token_entries_are_left_out():
    ok = {"instrument_admin": "a", "instrument_id": "X", "instrument_symbol": "X"}
    c = CantexPublicData(client=_client({"/tokens/info": {"tokens": [
        {"info": ok}, {"info": None}, {"info": {**ok, "instrument_symbol": None}},
        {"info": {"instrument_id": "Y", "instrument_symbol": "Y"}}]}}))
    assert run(c.tokens()) == [ok]


def test_oneswap_empty_pool_and_malformed_row_are_skipped():
    asset = lambda s: {"symbol": s, "admin": "a", "id": s}  # noqa: E731
    row = lambda i, rx, ry, fee="30": {"id": i, "assetX": asset("Amulet"), "assetY": asset(i),  # noqa: E731
                                       "reserveX": rx, "reserveY": ry, "feeBps": fee}
    c = OneSwapPublicData(client=_client({"/api/rt/pools": [row("A", "100", "5"), row("B", "0", "5"),
                                                            row("C", "100", "5", fee=None)]}))
    assert [p.pool_id for p in run(c.pools())] == ["A"]


def test_book_levels_at_price_zero_are_dropped():
    c = RockyAdapter(client=_client({"/depth": {"bids": [["99", "1"], ["0", "5"]], "asks": [["", "1"], ["101", "2"]]}}))
    b = run(c.order_book("CBTC-USDCX", 20))
    assert [lv.price for lv in b.bids] == [D(99)] and [lv.price for lv in b.asks] == [D(101)]


def test_rocky_book_without_a_ticker_row(tmp_path, monkeypatch):
    rocky = _Venue(markets=[SimpleNamespace(symbol="CBTC-USDCX", base="CBTC", quote="USDCx")], tickers=[],
                   order_book=lambda s, *_: _book(s, "83690", "0.5", "1"))
    t = _tick(tmp_path, monkeypatch, optional={"rocky": rocky})
    assert "rocky" in next(x for x in t["tokens"]["tokens"] if x["key"] == "CBTC")["venues"]


@pytest.mark.parametrize("usd", [None, 0])
def test_coingecko_usdcx_null_or_zero(tmp_path, monkeypatch, usd):
    t = _tick(tmp_path, monkeypatch, gecko={"xreserve-bridged-usdc-canton": {"usd": usd, "last_updated_at": int(time.time())}})
    assert t["summary"]["status"] == "ok" and t["summary"]["cc_usd"] > 0


def test_one_venues_odd_value_leaves_only_that_venue_out():
    d = data()
    d["perps"]["markets"][0]["quote"] = None  # Rocky perps: a market whose quoteAsset is null
    f = vp.facts(d)
    assert {"cantex", "tradecraft", "temple", "ekiden"} <= set(f)


def test_spread_study_runs_without_tradecraft(tmp_path):
    c = collect.Collector(tmp_path)
    run(c.http.aclose())
    c.cantex = _Venue(markets=[], pool_states=[], candles={}, tokens=[])
    c.tradecraft = SimpleNamespace(connect=_raises, pool_states=_raises)
    c.optional = {k: _Venue() for k in c.optional}
    run(spread_study.setup(c))  # a "degraded" Tradecraft used to stop the run before its first sample


async def _raises(*_a, **_k):
    raise RuntimeError("venue is not healthy: degraded")


# --- 2. One malformed row is skipped, not the whole venue ----------------------------------------

def test_tradecraft_skips_a_malformed_pool_row():
    row = {"lp_token_name": "A", "token1": "CC", "token2": "X", "token1_holdings": "1", "token2_holdings": "2",
           "total_lp_tokens": "1", "lp_fee_percent": "0.2", "operator_fee_percent": "0.1"}
    c = TradecraftAdapter(client=_client({"/pools": {"pools": [row, {**row, "lp_token_name": "B",
                                                                     "token1_holdings": None}]}}))
    assert [s.amm_id for s in run(c.pool_states())] == ["A"]


def test_pool_party_skips_a_malformed_volume():
    c = PoolPartyPublicData(client=_client({"/volume": {"perPool": {"A": {"volume": {"Amulet": "5"}},
                                                                    "B": {"volume": {"Amulet": None}}}}}))
    assert list(run(c.volume())) == ["A"]


def test_ekiden_skips_a_malformed_ticker():
    t = {"symbol": "BTC-USDCx", "last_price": "1", "index_price": "1", "mark_price": "1", "open_interest": "1",
         "volume_24h": "1", "turnover_24h": "1", "funding_rate": "0"}
    c = EkidenAdapter(client=_client({"/api/v1/market/tickers": {"list": [t, {**t, "symbol": "X", "mark_price": None}]}}))
    assert [x.symbol for x in run(c.tickers())] == ["BTC-USDCx"]


# --- 3. tick_no advances when a tick fails ---------------------------------------------------------

def test_a_failed_tick_still_advances(tmp_path, monkeypatch):
    c = collect.Collector(tmp_path)
    run(c.http.aclose())
    c.cantex, c.tradecraft = _Venue(), _Venue()  # neither answers: left out, as in production
    c.optional = {k: _Venue() for k in c.optional}

    async def boom(*_a, **_k):
        raise RuntimeError("one bad value")
    monkeypatch.setattr(c, "_gecko", boom)
    with pytest.raises(RuntimeError):
        run(c.tick())
    assert c.tick_no == 1  # a failed slow tick used to repeat the slow refresh on every tick


# --- 4. Every cache has an age limit; unknown is None ----------------------------------------------

def test_old_volumes_are_unknown_not_frozen(tmp_path):
    c = collect.Collector(tmp_path)
    run(c.http.aclose())
    now = int(time.time())
    c.slow.update(tc_volume={"a": 100.0}, temple_total=29e6, temple_volume={"CC/USDCx": 4e6})
    c.sources.update(tc_volume=now - 2 * 3600, temple_volume=now - 2 * 3600)
    rows = {v["id"]: v for v in c._venues(1e6, None, [], now)["venues"]}
    assert rows["tradecraft"]["volume_24h_usd"] is None
    assert rows["temple"]["status"] == "down" and rows["temple"]["volume_24h_usd"] is None


def test_coingeckos_last_answer_stands_in_for_minutes_only(tmp_path, monkeypatch):
    c = collect.Collector(tmp_path)
    run(c.http.aclose())

    async def fail(*_a, **_k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(c, "_json", fail)
    c.slow["gecko_last"] = {"bitcoin": {"usd": 1}}
    c.sources["gecko"] = int(time.time()) - 60
    assert run(c._gecko()) == {"bitcoin": {"usd": 1}}
    c.sources["gecko"] = int(time.time()) - 3600
    assert run(c._gecko()) == {}


def test_a_zero_reference_is_no_reference(tmp_path):
    assert collect._gecko_usd({"bitcoin": {"usd": 0, "last_updated_at": 100}}, "bitcoin", 100) is None


# --- 5. Temple can read down, and a key that fails is seen ----------------------------------------

def test_a_failing_temple_key_is_reported(tmp_path, monkeypatch):
    temple = _Venue(tickers=_raises, settled_volume={"markets": [], "total_volume_usd": "0"})
    t = _tick(tmp_path, monkeypatch, optional={"temple": temple})
    s = t["summary"]
    assert s["temple_priced"] is False and "not healthy" in s["temple_error"]


# --- 6. Watchdog and cross-check ------------------------------------------------------------------

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def _summary(**kw):
    return {"t": NOW.timestamp() - 120, "status": "ok", "down_since": {}, **kw}


def test_watchdog_tells_a_venue_temple_and_a_stale_source():
    old = NOW.timestamp() - 3600
    s = _summary(optional_down={"rocky": old}, temple_priced=False, sources={"temple_book": old, "gecko": old})
    text, state = wd.check(s, {}, NOW)
    assert "Rocky has not been read since" in text and "Temple's order books are not read" in text
    assert "CoinGecko prices last read" in text and state["kind"] == "venues"


def test_watchdog_tells_a_tick_that_fails_twice():
    s = _summary(status="error", error="boom", error_t=NOW.timestamp() - 60)
    text, state = wd.check(s, {}, NOW)
    assert text is None and state.get("error_seen")
    text, _ = wd.check(s, state, NOW + timedelta(minutes=10))
    assert "failed on two runs in a row: boom" in text


def test_summary_is_the_ticks_last_write(tmp_path, monkeypatch):
    _tick(tmp_path, monkeypatch)
    api = tmp_path / "www" / "api" / "v1"
    times = {p.name: p.stat().st_mtime_ns for p in api.glob("*.json")}
    assert times["summary.json"] == max(times.values())


def test_crosscheck_tells_one_held_card_and_silent_sources():
    one = published()
    next(iter(one["venues"].values()))["t"] -= 3 * 3600
    assert "card not republished for 3.2 h" in cx.run(one, fake_get(), CX_NOW)[0]
    assert "sources did not answer" in cx.run(published(), fake_get(fail=("ekiden",)), CX_NOW)[0]


# --- 7. Half-read venues, NaN, unescaped symbols ----------------------------------------------------

def test_rocky_perps_rows_only_after_the_whole_read(tmp_path, monkeypatch):
    mk = [SimpleNamespace(symbol=s, base=b, quote="USDT") for s, b in (("BTCUSDT", "BTC"), ("CCUSDT", "CC"))]
    tk = [SimpleNamespace(symbol=x.symbol, last_price=D(1), turnover_24h=D(1)) for x in mk]

    def book(s, *_):
        if s == "CCUSDT":
            raise RuntimeError("bad book")
        return _book(s, "83000", "1", "1")
    t = _tick(tmp_path, monkeypatch, optional={"rocky_perp": _Venue(markets=mk, tickers=tk, order_book=book)})
    assert not any(p["venue"] == "rocky_perp" for p in t["perps"]["markets"])


def test_json_with_nan_is_written_as_null_not_a_failed_tick(tmp_path, caplog):
    # 2026-10-15: failing the write made one "NaN" in any venue's field stop every tick; the file stays
    # valid JSON (no NaN token a page cannot parse) and the warning says what happened
    collect.write_json(tmp_path / "x.json", {"a": math.nan, "b": [1.5, math.inf], "c": "ok"})
    import json
    assert json.loads((tmp_path / "x.json").read_text()) == {"a": None, "b": [1.5, None], "c": "ok"}
    assert "2 non-finite" in caplog.text


def test_alert_symbols_are_escaped_for_telegram():
    tokens = [{"key": "X", "symbol": "A<B&", "liquidity_usd": 1e6, "price_usd": 2.0}]
    out = al.evaluate(tokens, [], [], {"X": 1.0})
    assert "A&lt;B&amp;" in out[0].html and "A<B" not in out[0].html

