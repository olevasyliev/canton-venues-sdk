"""The home page's CC card shows CC's own 24h volume on every DEX we read (summary ``cc_24h``), not
Cantex's platform figures: until 6 Nov 2026 it showed "Cantex volume 24h" and "Cantex swaps 24h", 11% of
the spot volume in the headline above it, under CC's price."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_accuracy_batch1 import TEMPLE_CC_MID, _book, _tick, _Venue  # noqa: E402

D = Decimal


def test_cc_volume_counts_every_cc_market_and_no_other(tmp_path, monkeypatch):
    # Rocky: a CC/USDCx book and a CBTC/USDCx one; only the first is CC's
    books = {"CC-USDCX": ("CC", "USDCx", "0.117", "20000"), "CBTC-USDCX": ("CBTC", "USDCx", "83700", "2000000")}
    rocky = _Venue(markets=[SimpleNamespace(symbol=s, base=b, quote=q) for s, (b, q, _, _) in books.items()],
                   tickers=[SimpleNamespace(symbol=s, turnover_24h=D(v), last_price=D(p))
                            for s, (_, _, p, v) in books.items()],
                   order_book=lambda s, *_: _book(s, books[s][2], "0.5", "1"))
    out = _tick(tmp_path, monkeypatch, optional={"rocky": rocky})
    s = out["summary"]
    cc = s["cc_usd"]
    v = s["cc_24h"]["venues"]
    # Cantex: the CC side of its CC pools' tickers (500K + 100K + 0 CC), at our CC price
    assert v["cantex"] == pytest.approx(600_000 * cc, rel=1e-6)
    # Temple: its CC/USDCx market (4M USDCx), not its CBTC/USDCx one
    assert v["temple"] == pytest.approx(4_000_000)
    # Rocky: its CC/USDCx book's turnover, not its CBTC book
    assert v["rocky"] == pytest.approx(20_000)
    assert s["cc_24h"]["volume_usd"] == pytest.approx(600_000 * cc + 4_000_000 + 20_000, rel=1e-6)
    assert list(v) == ["temple", "cantex", "rocky"]  # largest first
    # Cantex's own platform figures stay in the API, apart from CC's
    assert s["cantex_24h"]["swaps"] == 100


def test_a_venue_with_cc_markets_and_no_volume_is_unknown_not_zero(tmp_path, monkeypatch):
    # Temple's CC book is read and its settled volume is not: CC trades there, its volume is unknown
    books = {"CC/USDCx": _book("CC/USDCx", TEMPLE_CC_MID, "50000", "0.00001")}
    temple = _Venue(tickers=[SimpleNamespace(symbol=s) for s in books], order_book=lambda s, *_: books[s])
    s = _tick(tmp_path, monkeypatch, optional={"temple": temple})["summary"]
    v = s["cc_24h"]["venues"]
    assert "temple" in v and v["temple"] is None
    assert s["cc_24h"]["volume_usd"] == pytest.approx(v["cantex"])  # the total is what is known
