"""Kora joins the site as a priced spot order book (2026-11-23): its USDCx books are compared like
Temple's, its CC/USDCx book joins every CC/USDCx comparison and the CC price, each book takes the taker
fee Kora states for that market, and its own 24h volume is read but counted nowhere while
``collect.KORA_VOLUME_PUBLISHED`` is off.

Kora's CBTC-USDCx book on 2026-11-23 20:50 UTC showed $129K of bids and $859 of asks: a buy past the
asks must read as "cannot fill" on Kora, never as its best price."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import collect  # noqa: E402
from cantonvenues.core.models import BookLevel, Market, OrderBook  # noqa: E402
from test_accuracy_batch1 import TEMPLE_CC_MID, _book, _tick, _Venue  # noqa: E402

D = Decimal


def _market(symbol: str, status: str = "active") -> Market:
    base, _, quote = symbol.partition("-")
    return Market(symbol, base, quote, status == "active", D("0.01"), D("0.0001"), D("0.0001"), D(10), D(1), 0)


def _thin_asks(symbol: str, mid: str) -> OrderBook:
    """Deep bids, four asks worth about $860 in all (Kora's CBTC-USDCx on 2026-11-23)."""
    full = _book(symbol, mid, "0.5", "1")
    asks = tuple(BookLevel(D(mid) + i + 1, D("0.0026")) for i in range(4))
    return OrderBook(symbol, full.timestamp, full.bids, asks)


VOLUMES = {"CBTC-USDCx": {"usd": D("1636661"), "stale": False, "as_of": None},
           "CC-USDCx": {"usd": D("32474"), "stale": False, "as_of": None},
           "CBTC-eXAU": {"usd": D("1956540"), "stale": False, "as_of": None}}


def _kora(read: list | None = None, fees: dict | None = None) -> _Venue:
    books = {"CBTC-USDCx": _thin_asks("CBTC-USDCx", "83700"),
             "CC-USDCx": _book("CC-USDCx", TEMPLE_CC_MID, "20000", "0.00001"),
             "CBTC-eXAU": _book("CBTC-eXAU", "23.1", "1", "0.001")}

    def book(symbol, *_):
        if read is not None:
            read.append(symbol)
        return books[symbol]
    return _Venue(markets=[_market(s) for s in books],
                  taker_fees=fees if fees is not None else {s: D("0.00025") for s in books},
                  order_book=book, volumes_usd=VOLUMES)


def _pair(out: dict, key: str) -> dict:
    return next(p for p in out["execution"]["pairs"] if p["key"] == key)


def _row(pair: dict, side: str, size: int) -> dict:
    return next(r for r in pair["rows"] if r["side"] == side and r["size_usd"] == size)


def test_kora_books_are_compared_and_a_thin_side_cannot_fill(tmp_path, monkeypatch):
    read: list = []
    out = _tick(tmp_path, monkeypatch, optional={"kora": _kora(read)})
    assert "CBTC-eXAU" not in read  # a book quoted in eXAU is not read: no non-dollar book is compared yet
    cbtc = _pair(out, "CBTC:USD")
    assert "kora" in cbtc["venues"] and cbtc["books"]["kora"]["market"] == "CBTC-USDCx"
    buy = _row(cbtc, "buy", 10_000)
    assert buy["out"]["kora"] is None and buy["best"] != "kora"  # $10K walks past $860 of asks
    small = _row(cbtc, "buy", 100)
    assert small["out"]["kora"] is not None  # $100 fills inside the asks
    sell = _row(cbtc, "sell", 10_000)
    assert sell["out"]["kora"] is not None  # the bids are deep
    tok = next(t for t in out["tokens"]["tokens"] if t["key"] == "CBTC")
    assert tok["venues"]["kora"]["market"] == "CBTC-USDCx" and tok["venues"]["kora"]["depth_1pct_usd"] > 0
    assert "CBTC-USDCx" in out["pools"]["books"]["CBTC"]


def test_kora_takes_the_fee_its_market_states(tmp_path, monkeypatch):
    fees = {"CBTC-USDCx": D("0.0004"), "CC-USDCx": D("0.00025")}
    out = _tick(tmp_path, monkeypatch, optional={"kora": _kora(fees=fees)})
    assert _pair(out, "CBTC:USD")["books"]["kora"]["fee"] == pytest.approx(0.0004)
    assert out["pools"]["books"]["CBTC"]["CBTC-USDCx"]["fee"] == pytest.approx(0.0004)


def test_kora_cc_book_joins_cc_usdcx_and_the_cc_price(tmp_path, monkeypatch):
    out = _tick(tmp_path, monkeypatch, optional={"kora": _kora()})
    cc = _pair(out, "USDCX")
    assert {"temple", "kora"} <= set(cc["venues"]) and cc["books"]["kora"]["market"] == "CC-USDCx"
    assert set(out["pools"]["books"]["CC"]) == {"CC/USDCx", "CC-USDCx"}
    assert "kora" in next(a for a in out["premium"]["assets"] if a["key"] == "CC")["premium_by_venue"]
    assert out["execution"]["network_fee"]["kora"]["basis"] == "assumed"


def test_kora_volume_is_counted_nowhere_while_the_switch_is_off(tmp_path, monkeypatch):
    base = _tick(tmp_path / "a", monkeypatch)
    out = _tick(tmp_path / "b", monkeypatch, optional={"kora": _kora()})
    row = next(v for v in out["venues"]["venues"] if v["id"] == "kora")
    assert row["status"] == "priced" and row["volume_24h_usd"] is None and "markets_24h_usd" not in row
    # the same but for cents: Kora's CC book moves the CC price Cantex's CC volume is converted at
    assert out["venues"]["spot_volume_24h_usd"] == pytest.approx(base["venues"]["spot_volume_24h_usd"], rel=1e-4)
    vol = {t["key"]: t["volume_24h_usd"] for t in out["tokens"]["tokens"]}
    assert vol["CBTC"] == pytest.approx({t["key"]: t["volume_24h_usd"] for t in base["tokens"]["tokens"]}["CBTC"],
                                        rel=1e-4)
    assert out["summary"]["cc_24h"]["venues"]["kora"] is None  # a CC market whose volume is not counted
    assert out["summary"]["cc_24h"]["volume_usd"] == pytest.approx(base["summary"]["cc_24h"]["volume_usd"], rel=1e-4)


def test_kora_volume_is_its_own_dollar_figure_once_switched_on(tmp_path, monkeypatch):
    monkeypatch.setattr(collect, "KORA_VOLUME_PUBLISHED", True)
    out = _tick(tmp_path, monkeypatch, optional={"kora": _kora()})
    row = next(v for v in out["venues"]["venues"] if v["id"] == "kora")
    assert row["volume_24h_usd"] == pytest.approx(1636661 + 32474 + 1956540)
    assert row["markets_24h_usd"]["CBTC-USDCx"] == pytest.approx(1636661)
    assert out["summary"]["cc_24h"]["venues"]["kora"] == pytest.approx(32474)


def test_kora_unreachable_leaves_the_rest_published(tmp_path, monkeypatch):
    out = _tick(tmp_path, monkeypatch, optional={"kora": _Venue()})  # connect fails
    assert out["summary"]["status"] == "ok"
    assert next(v for v in out["venues"]["venues"] if v["id"] == "kora")["status"] == "down"
    assert "kora" not in _pair(out, "USDCX")["venues"]
