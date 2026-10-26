"""Rocky's CBTC/USDM1 book (listed 23 Oct 2026) counts in Rocky's spot volume at $1 a USDM1 while the
book's own price holds the peg; Rocky's volume missed it until 26 Oct ($1.4M of that week)."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_accuracy_batch1 import _book, _tick, _Venue  # noqa: E402

D = Decimal


def _rocky_volume(tmp_path, monkeypatch, usdm1_last: str) -> float:
    books = {"CBTC-USDCX": ("CBTC", "USDCx", "83700", "2000000"), "CBTC-USDM1": ("CBTC", "USDM1", usdm1_last, "800000")}
    rocky = _Venue(markets=[SimpleNamespace(symbol=s, base=b, quote=q) for s, (b, q, _, _) in books.items()],
                   tickers=[SimpleNamespace(symbol=s, turnover_24h=D(v), last_price=D(p))
                            for s, (_, _, p, v) in books.items()],
                   order_book=lambda s, *_: _book(s, books[s][2], "0.5", "1"))
    out = _tick(tmp_path, monkeypatch, optional={"rocky": rocky})
    return next(v for v in out["venues"]["venues"] if v["id"] == "rocky")["volume_24h_usd"]


def test_the_usdm1_book_counts_at_a_dollar_while_it_holds_the_peg(tmp_path, monkeypatch):
    assert _rocky_volume(tmp_path, monkeypatch, "83650") == 2_800_000  # USDM1 at $1.0006 by the book


def test_the_usdm1_book_off_its_peg_is_left_out(tmp_path, monkeypatch):
    assert _rocky_volume(tmp_path, monkeypatch, "85000") == 2_000_000  # USDM1 at $0.985 by the book
