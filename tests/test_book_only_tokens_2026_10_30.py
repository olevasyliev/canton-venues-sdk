"""A token Temple trades on its book alone, with no CC pool on any venue, is a token on the site; and a
Temple market that trades while we read no book of it is told to the owner.

BETH/USDCx was listed on Temple on 19 Oct 2026 and traded $18.1M in 23-29 Oct (12% of Temple's volume,
its own record) while the collector read only the books of tokens that also had a CC pool: BETH was in
Temple's volume and nowhere else, and nobody was told. Found by hand on 30 Oct for an X post."""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import watchdog as wd  # noqa: E402
from test_accuracy_batch1 import TEMPLE_CC_MID, _book, _tick, _Venue  # noqa: E402

D = Decimal
BETH_MID = "2230"


def _temple(markets: dict[str, str], extra_tickers=()):
    books = {"CC/USDCx": _book("CC/USDCx", TEMPLE_CC_MID, "50000", "0.00001"),
             "CBTC/USDCx": _book("CBTC/USDCx", "83700", "0.5", "1"),
             "BETH/USDCx": _book("BETH/USDCx", BETH_MID, "2", "0.5")}

    def book(s, *_):
        if s not in books:
            raise RuntimeError("Symbol not supported")  # what Temple answers for its USDA markets
        return books[s]
    return _Venue(tickers=[SimpleNamespace(symbol=s) for s in (*books, *extra_tickers)], order_book=book,
                  settled_volume={"markets": [{"symbol": s, "quote_volume": v} for s, v in markets.items()],
                                  "total_volume_usd": str(sum(float(v) for s, v in markets.items()
                                                              if s.endswith("/USDCx")))})


VOLUME = {"CC/USDCx": "4000000", "CBTC/USDCx": "1000000", "BETH/USDCx": "2900000"}


def test_a_token_on_temples_book_alone_is_priced_listed_and_has_an_exit(tmp_path, monkeypatch):
    out = _tick(tmp_path, monkeypatch, optional={"temple": _temple(VOLUME)})
    assert out["summary"]["status"] == "ok"
    tokens = {t["key"]: t for t in out["tokens"]["tokens"]}
    beth = tokens["BETH"]  # it was in no file before
    assert beth["symbol"] == "BETH" and set(beth["venues"]) == {"temple"}
    assert beth["price_usd"] == pytest.approx(float(BETH_MID), rel=1e-3)  # the book's mid, in dollars
    assert beth["venues"]["temple"]["market"] == "BETH/USDCx"
    assert beth["liquidity_usd"] == beth["venues"]["temple"]["depth_1pct_usd"] > 0  # what rests within 1%
    assert beth["volume_24h_usd"] == pytest.approx(2_900_000) and beth["traded_24h"] is True
    assert out["summary"]["tokens"] == len(out["tokens"]["tokens"])  # counted with the pooled ones
    # its book is in pools.json, so exit cost (and the MCP quote, which reads the same file) can use it
    assert "BETH/USDCx" in out["pools"]["books"]["BETH"]
    row = next(x for x in out["exit"]["tokens"] if x["key"] == "BETH")
    assert row["capacity_usd"]["2pct"]["all_venues"] > 0
    # one venue: nothing to compare, so no best-execution pair
    assert not any(p.get("token") == "BETH" or p["key"] == "BETH" for p in out["execution"]["pairs"])
    # its reference is ETH; with no ETH price in this read it says so instead of a figure
    row = next(a for a in out["premium"]["assets"] if a["key"] == "BETH")
    assert row["reference"] == "ETH" and row["status"] == "no reference price" and "premium" not in row
    # nothing unread: every market that traded had its book read
    assert "temple_unread" not in out["summary"]


def test_a_book_only_token_is_measured_against_its_outside_price(tmp_path, monkeypatch):
    # BETH trades at ETH's price (30 Oct: $2,233 against $2,234); it is measured against ETH, as cETH is
    eth = {"ethereum": {"usd": 2240.0, "last_updated_at": int(time.time())}}
    out = _tick(tmp_path, monkeypatch, optional={"temple": _temple(VOLUME)}, gecko=eth)
    row = next(a for a in out["premium"]["assets"] if a["key"] == "BETH")
    assert row["symbol"] == "BETH" and row["reference"] == "ETH" and row["reference_id"] == "ethereum"
    assert row["status"] == "ok" and row["reference_usd"] == 2240.0
    assert row["premium"] == pytest.approx(float(BETH_MID) / 2240 - 1, abs=2e-4)  # about -0.45%
    assert set(row["premium_by_venue"]) == {"temple"}
    assert row["premium_by_venue"]["temple"] == pytest.approx(row["premium"], abs=1e-9)
    # the pooled tokens keep their own names in the same list
    assert next(a for a in out["premium"]["assets"] if a["key"] == "CBTC")["symbol"] == "CBTC"


def test_pooled_tokens_are_as_before_with_a_book_only_token_beside_them(tmp_path, monkeypatch):
    before = _tick(tmp_path / "a", monkeypatch)
    after = _tick(tmp_path / "b", monkeypatch, optional={"temple": _temple(VOLUME)})
    old = {t["key"]: t for t in before["tokens"]["tokens"]}
    new = {t["key"]: t for t in after["tokens"]["tokens"]}
    assert set(new) - set(old) == {"BETH"}
    for k in old:
        assert new[k]["price_usd"] == pytest.approx(old[k]["price_usd"], rel=1e-6), k
        assert set(new[k]["venues"]) == set(old[k]["venues"]), k


def test_a_temple_market_that_trades_unread_is_in_the_summary_and_told(tmp_path, monkeypatch):
    # CBTC/eXAU (quoted in gold) and CC/USDA are listed; one trades, the other does not
    volume = {**VOLUME, "CBTC/eXAU": "7.25", "CC/USDA": "0"}
    out = _tick(tmp_path, monkeypatch, optional={"temple": _temple(volume, ("CBTC/eXAU", "CC/USDA"))})
    s = out["summary"]
    assert s["temple_unread"] == {"CBTC/eXAU": 7.25}  # in its quote token, not dollars; CC/USDA traded 0
    now = time.time()
    down = wd.troubles({**s, "t": now}, now, False)
    line = down["unread:CBTC/eXAU"][1]
    assert "CBTC/eXAU" in line and "7 eXAU" in line and "do not read" in line
    msg, _ = wd.check({**s, "t": now}, {}, datetime.fromtimestamp(now, UTC))
    assert msg and "short of data" in msg and "CBTC/eXAU" in msg


def test_no_unread_market_is_told_while_temples_books_are_not_read_at_all(tmp_path, monkeypatch):
    # without its key Temple shows volume only; the watchdog's own Temple trouble covers that, so every
    # market is not listed as unread on top
    temple = _temple(VOLUME)
    temple.has_key = False
    s = _tick(tmp_path, monkeypatch, optional={"temple": temple})["summary"]
    assert s["temple_priced"] is False and "temple_unread" not in s


# --- a restart does not make a venue that was out "back" -------------------------------------------------

def test_a_venue_out_before_a_restart_stays_out_from_its_own_time(tmp_path):
    # Pool Party was unread from 28 Oct 11:22 UTC; each deploy on 30 Oct restarted the count, its entry left
    # summary.json for 10 minutes and the watchdog told the owner "whole again" while it was still out
    import asyncio
    import json

    import collect
    www = tmp_path / "www"
    (www / "api" / "v1").mkdir(parents=True)
    since = int(time.time()) - 49 * 3600
    (www / "api" / "v1" / "summary.json").write_text(json.dumps({"optional_down": {"poolparty": since}}))
    c = collect.Collector(www)
    asyncio.run(c.http.aclose())
    now = c.started + 60  # the first tick after the restart, not 10 minutes later
    assert c._optional_down(now)["poolparty"] == since
    c.fresh["poolparty"] = now  # read again after the restart: back, as before
    assert "poolparty" not in c._optional_down(now + 60)
    # the watchdog sees the same venue out since the same time: no "whole again", nothing new to tell
    state = {"alerted": now - 600, "kind": "venues", "venues": ["venue:poolparty"], "since": since}
    summary = {"t": now, "status": "ok", "cc_usd": 0.1, "tokens": 1,
               "optional_down": c._optional_down(now)}
    msg, _ = wd.check(summary, state, datetime.fromtimestamp(now, UTC))
    assert msg is None
