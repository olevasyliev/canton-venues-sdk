"""Batch 2 of the 2026-10-15 re-audit: one bad value never stops the site, one failing book never
drops a venue, and the watchdog sees what it missed. One test per finding, each failing on the code
before the fix (the replays are canton/docs/audit-2026-10-15/auditB-guards-watchdog.md)."""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import collect  # noqa: E402
import crosscheck as cx  # noqa: E402
import model as m  # noqa: E402
import watchdog as wd  # noqa: E402
from test_accuracy_batch1 import TEMPLE_CC_MID, _book, _tick, _Venue  # noqa: E402
from test_accuracy_batch3 import ROCKY_BOOKS  # noqa: E402

from cantonvenues import Instrument, TempleAdapter  # noqa: E402

D = Decimal


# --- 1. A "NaN" from a venue is refused at its row, and never stops the tick -------------------------

def test_a_non_finite_number_is_refused_where_it_is_read():
    from cantonvenues.core.models import book_levels, to_decimal
    for bad in ("NaN", "nan", "Infinity", "-inf", float("nan"), "abc"):
        with pytest.raises(InvalidOperation):
            to_decimal(bad)
    assert to_decimal("", D(0)) == 0 and to_decimal("1.5") == D("1.5")
    side = book_levels([["0.1170", "10"], ["NaN", "5"], ["0.1169", "Infinity"], ["0.1168", "3"]])
    assert [lv.price for lv in side] == [D("0.1170"), D("0.1168")]  # the bad levels, not the book


def test_a_nan_in_temples_settled_volume_does_not_stop_the_tick(tmp_path, monkeypatch):
    temple = _Venue(tickers=[SimpleNamespace(symbol="CC/USDCx")],
                    order_book=lambda s, *_: _book("CC/USDCx", TEMPLE_CC_MID, "50000", "0.00001"),
                    settled_volume={"markets": [{"symbol": "CC/USDCx", "quote_volume": "NaN"}],
                                    "total_volume_usd": "4000000"})
    out = _tick(tmp_path, monkeypatch, optional={"temple": temple})
    assert out["summary"]["status"] == "ok"  # it failed every tick at the JSON write (13 of 17 fields)


# --- 2. One Temple book failing leaves the others, its CC book above all -----------------------------

def test_one_failing_temple_book_keeps_its_cc_book(tmp_path, monkeypatch):
    def book(s, *_):
        if s == "CBTC/USDCx":
            raise RuntimeError("HTTP 502")
        return _book("CC/USDCx", TEMPLE_CC_MID, "50000", "0.00001")
    temple = _Venue(tickers=[SimpleNamespace(symbol=s) for s in ("CC/USDCx", "CBTC/USDCx")], order_book=book,
                    settled_volume={"markets": [], "total_volume_usd": "4000000"})
    s = _tick(tmp_path, monkeypatch, optional={"temple": temple})["summary"]
    assert s["temple_cc_book"] is True and "CBTC/USDCx" in s["temple_error"]
    assert s["cc_usd"] == pytest.approx(float(TEMPLE_CC_MID), rel=2e-4)  # not the pools' 0.118


def test_a_crossed_cc_book_is_no_market(tmp_path, monkeypatch):
    crossed = SimpleNamespace(**vars(_book("CC/USDCx", TEMPLE_CC_MID, "50000", "0.00001")))
    crossed.bids, crossed.asks = crossed.asks, crossed.bids  # bids above asks
    from cantonvenues.core.models import OrderBook
    crossed = OrderBook("CC/USDCx", datetime.now(UTC), tuple(sorted(crossed.bids, key=lambda lv: -lv.price)),
                        tuple(sorted(crossed.asks, key=lambda lv: lv.price)))
    temple = _Venue(tickers=[SimpleNamespace(symbol="CC/USDCx")], order_book=lambda *_: crossed,
                    settled_volume={"markets": [], "total_volume_usd": "4000000"})
    s = _tick(tmp_path, monkeypatch, optional={"temple": temple})["summary"]
    assert s["temple_cc_book"] is False and "crossed" in s["temple_error"]


def test_temple_connects_on_its_key_when_the_keyless_route_fails():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path.endswith("settled_volume"):
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"tickers": []})
    a = TempleAdapter(api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(a.connect())  # it failed, so the keyed books went unread with the volume
    assert seen[-1] == "/api/v1/market/ticker"
    keyless = TempleAdapter(api_key="", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(Exception):
        asyncio.run(keyless.connect())


# --- 3. One Rocky book failing leaves the others ------------------------------------------------------

def test_one_failing_rocky_book_keeps_rocky(tmp_path, monkeypatch):
    def book(s, *_):
        if s == "CBTC-USDCB":
            raise RuntimeError("HTTP 404")
        base, _, px = ROCKY_BOOKS[s]
        return _book(s, px, "0.5" if base == "CBTC" else "20", "1" if base == "CBTC" else "0.05")
    rocky = _Venue(markets=[SimpleNamespace(symbol=s, base=b, quote=q) for s, (b, q, _) in ROCKY_BOOKS.items()],
                   tickers=[SimpleNamespace(symbol=s, turnover_24h=D(1)) for s in ROCKY_BOOKS], order_book=book)
    out = _tick(tmp_path, monkeypatch, optional={"rocky": rocky})
    cbtc = next(t for t in out["tokens"]["tokens"] if t["key"] == "CBTC")
    assert cbtc["venues"]["rocky"]["market"] == "CBTC-USDCX"  # Rocky was dropped whole before


# --- 4. State that outlives a restart ------------------------------------------------------------------

def test_cc_instrument_and_source_times_outlive_a_restart(tmp_path):
    www = tmp_path / "www"
    (www / "api" / "v1").mkdir(parents=True)
    (www / "api" / "v1" / "summary.json").write_text(json.dumps({"sources": {"gecko": 1000, "llama": 5}}))
    c = collect.Collector(www)
    asyncio.run(c.http.aclose())
    assert c.sources == {"gecko": 1000}  # a source dead since a deploy now ages and is told
    inst = Instrument("DSO::1220", "Amulet")
    c._cc_instrument([SimpleNamespace(symbol_a="CC", symbol_b="USDCx", token_a=inst, token_b=None)])
    again = collect.Collector(www)
    asyncio.run(again.http.aclose())
    assert again._cc_instrument([]) == inst  # Cantex down at a restart failed every tick before


# --- 5. Volumes: one pool that does not answer, and the age limits everywhere -------------------------

def test_a_pool_without_a_volume_keeps_its_last_one_for_an_hour(tmp_path):
    c = collect.Collector(tmp_path / "www")
    asyncio.run(c.http.aclose())
    vols, missing = c._volume_seen("tc", {"a": 100.0, "b": 50.0}, ["a", "b"])
    assert vols == {"a": 100.0, "b": 50.0} and missing == []
    vols, missing = c._volume_seen("tc", {"a": 120.0}, ["a", "b"])  # b did not answer: its last one
    assert vols == {"a": 120.0, "b": 50.0} and missing == []
    c.vol_seen["tc"]["b"] = (50.0, time.time() - 3700)
    vols, missing = c._volume_seen("tc", {"a": 120.0}, ["a", "b"])
    assert vols == {"a": 120.0} and missing == ["b"]  # the total then ages out, it is not cut silently


def test_a_tiny_own_cc_pool_does_not_value_its_venues_tokens(tmp_path, monkeypatch):
    # Cantex's CC/USDCx pool holds $47 at 0.141 (20% over the book): it priced every Cantex token
    from test_accuracy_batch1 import _state
    out = _tick(tmp_path, monkeypatch, states=[_state("USDCx", "40000", "4720")])  # the deeper pool
    assert "cantex" in out["premium"]["assets"][0]["premium_by_venue"]
    small = tmp_path / "small"
    small.mkdir()
    out = _tick(small, monkeypatch, cantex={"pool_states": [_state("USDCx", "200", "28.2"), _state("USDM1", "500000", "59000")]})
    usdm1 = next(t for t in out["tokens"]["tokens"] if t["key"] == "USDM1")
    assert usdm1["venues"]["cantex"]["price_usd"] == pytest.approx(float(TEMPLE_CC_MID) / 0.118, rel=1e-3)


# --- 6. The watchdog ------------------------------------------------------------------------------------

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def _summary(**kw):
    return {"t": NOW.timestamp() - 60, "status": "ok", "cc_usd": 0.106, "tokens": 34, "down_since": {}, **kw}


def test_watchdog_tells_a_missing_cc_book_flaky_ticks_and_an_empty_snapshot():
    s = _summary(sources={"temple_cc_book": NOW.timestamp() - 40 * 60})
    assert "source:temple_cc_book" in wd.troubles(s, NOW.timestamp(), False)
    s = _summary(failed_ticks_1h=3, last_error={"t": NOW.timestamp() - 300, "error": "boom"})
    assert "boom" in wd.troubles(s, NOW.timestamp(), False)["flaky"][1]
    assert "empty" in wd.troubles(_summary(cc_usd=None), NOW.timestamp(), False)
    assert "unreadable" in wd.troubles(_summary(down_since={"cantex": "x"}), NOW.timestamp(), False)


def test_watchdog_says_nothing_when_one_of_two_troubles_clears():
    two = _summary(down_since={"cantex": NOW.timestamp() - 45 * 60},
                   sources={"gecko": NOW.timestamp() - 45 * 60})
    text, state = wd.check(two, {}, NOW)
    assert text and set(state["venues"]) == {"cantex", "source:gecko"}
    later = NOW + timedelta(minutes=10)
    one = {**two, "t": later.timestamp() - 60, "sources": {"gecko": later.timestamp() - 60}}
    text, state = wd.check(one, state, later)
    assert text is None and state["venues"] == ["cantex"]  # it re-announced Cantex as new before


# --- 7. Small ones ------------------------------------------------------------------------------------

def test_crosscheck_asks_again_after_a_rate_limit(monkeypatch):
    calls = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": 1}'

    def urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
        return Resp()
    monkeypatch.setattr(cx.urllib.request, "urlopen", urlopen)
    assert cx.http_get("https://api.coingecko.com/x", sleep=lambda s: None) == {"ok": 1} and len(calls) == 2


def test_change_from_two_base_points_takes_the_nearer_not_the_higher():
    now = 10 * 86_400_000
    series = [(now - 86_400_000 - 7_200_000, 100.0), (now - 86_400_000 - 3_600_000, 1.0), (now, 1.0)]
    assert m.change(series, now, 86_400_000) == pytest.approx(0.0)  # it read -99% off the wick
