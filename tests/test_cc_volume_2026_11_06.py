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


# --- Cantex's swap count moved to its own venue page; the MCP overview carries CC's volume -----------

def test_cantex_swaps_show_on_cantexs_page_and_no_other():
    import venue_pages as vp
    from test_venue_pages import T, data

    d = data()
    d["summary"]["cantex_24h"] = {"volume_usd": 1_830_000, "swaps": 126_580}
    f = vp.facts(d)
    assert f["cantex"]["swaps_24h"] == 126_580
    page = vp.venue_page(f["cantex"], vp.headline(f["cantex"]), T)
    assert "Swaps, 24h" in page and "126,580" in page
    assert "own count over every pool, from its public API" in page  # the method note
    assert all("swaps_24h" not in x for s, x in f.items() if s != "cantex")
    assert "Swaps, 24h" not in vp.venue_page(f["tradecraft"], vp.headline(f["tradecraft"]), T)


def test_unknown_cantex_swaps_show_nothing_not_zero():
    import venue_pages as vp
    from test_venue_pages import T, data

    d = data()
    d["summary"]["cantex_24h"] = {"volume_usd": None, "swaps": None}
    f = vp.facts(d)["cantex"]
    assert "swaps_24h" not in f and "Swaps, 24h" not in vp.venue_page(f, vp.headline(f), T)


def test_market_overview_gives_cc_volume_by_venue_not_cantex_swaps(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    import json

    import mcp_server

    api = tmp_path / "api"
    api.mkdir()
    files = {
        "summary": {"t": 1, "cc_usd": 0.0977, "cantex_24h": {"swaps": 126_580},
                    "cc_24h": {"volume_usd": 4_825_832.79,
                               "venues": {"temple": 2_738_830.42, "cantex": 1_830_442.81, "oneswap": None}}},
        "venues": {"venues": [{"id": "temple", "name": "Temple", "kind": "Spot order book", "volume_24h_usd": 14e6},
                              {"id": "cantex", "name": "Cantex", "kind": "Spot AMM", "volume_24h_usd": 1.83e6},
                              {"id": "oneswap", "name": "OneSwap", "kind": "Spot AMM", "volume_24h_usd": None}]},
        "tokens": {"tokens": []}, "premium": {"assets": []}, "alerts": {"feed": []},
    }
    for n, x in files.items():
        (api / f"{n}.json").write_text(json.dumps(x))
    monkeypatch.setattr(mcp_server, "API_DIR", str(api))
    mcp_server._cache.clear()
    o = mcp_server.market_overview()
    assert "cantex_swaps_24h" not in o
    assert o["cc_volume_24h_usd"] == 4_825_832.79
    assert o["cc_volume_by_venue_24h_usd"] == {"Temple": 2_738_830.42, "Cantex": 1_830_442.81, "OneSwap": None}
