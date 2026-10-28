"""Canborsa: the adapter against its two public sources (REST and the socket.io stream, served locally),
a collector tick that lists its perps, and its venue page."""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import venue_pages as vp  # noqa: E402
from test_accuracy_batch1 import _tick, _Venue  # noqa: E402
from test_venue_pages import data  # noqa: E402

from cantonvenues.venues.canborsa import CanborsaMarket, CanborsaPrice, CanborsaPublicData  # noqa: E402
from cantonvenues.core.models import BookLevel, OrderBook  # noqa: E402

D = Decimal


def _market(base, quote, short, vol):
    return {"id": 1, "baseTicker": base, "quoteTicker": quote, "collateralTickers": [quote, "cc"],
            "isShort": short, "openingFee": "0.00055", "closingFee": "0.00055", "liquidationFee": "0.005",
            "volume30d": vol, "maxLeverage": "5", "minAmountUSD": "10", "maxAmountUSD": "50000",
            "settlementPeriodInHours": 1}


def _rest(markets, funding):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/perpetuals/markets"):
            return httpx.Response(200, json=markets)
        if req.url.path.endswith("/perpetuals/funding-rates"):
            return httpx.Response(200, json=funding)
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_each_market_is_kept_once_with_its_volume_counted_once():
    # the venue lists every market twice, long and short, with the same 30-day volume on both rows
    rows = [_market("cc", "usdcx", False, "6313715.6"), _market("cc", "usdcx", True, "6313715.6"),
            _market("BTC", "USDCx", False, "241596.5"), _market("btc", "usdcx", True, "241596.5"),
            {"baseTicker": "bad"}]  # a row that will not parse is left out, not the venue
    funding = {"cc/usdcx": {"rate": "0.0000239", "annualRate": "0.2095"}, "btc/usdcx": {"rate": "NaN"}}
    async with CanborsaPublicData(client=_rest(rows, funding)) as c:
        ms = await c.markets()
        assert [m.symbol for m in ms] == ["cc/usdcx", "btc/usdcx"]
        assert sum(m.volume_30d for m in ms) == D("6555312.1")
        assert ms[0].max_usd == D(50000) and ms[0].funding_hours == 1 and ms[0].collateral == ("usdcx", "cc")
        assert await c.funding_rates() == {"cc/usdcx": D("0.0000239")}  # a NaN rate is no rate


async def test_a_venue_with_no_market_listed_is_not_read():
    with pytest.raises(Exception, match="no perp market"):
        async with CanborsaPublicData(client=_rest([], {})):
            pass


@pytest.fixture
async def socket_server():
    """A socket.io server speaking what the venue's does (engine.io v4 over a websocket): open, the
    namespace join, a ping, then each subscription answered. It records what the client sent."""
    sent: list[str] = []
    pushes: list[str] = []

    async def ws_handler(request):
        if request.headers.get("Origin") != "https://app.canborsa.com":
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str('0{"sid":"a","upgrades":[],"pingInterval":25000,"pingTimeout":20000}')
        async for msg in ws:
            sent.append(msg.data)
            if msg.data == "40":
                await ws.send_str('40{"sid":"b"}')
                await ws.send_str("2")  # ping: the client must answer "3"
            elif msg.data.startswith("42") and '"execution-depth"' in msg.data:
                for p in pushes:
                    await ws.send_str(p)
        return ws

    app = web.Application()
    app.router.add_get("/socket.io/", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"ws://127.0.0.1:{port}/socket.io/?EIO=4&transport=websocket", sent, pushes
    await runner.cleanup()


async def test_stream_reads_each_price_and_depth_once_and_closes(socket_server):
    url, sent, pushes = socket_server
    rates = ["perpetual", [["cc/usdcx", 0.102595, 0.10254370250000001, 0.1026462975, "2026-10-28T10:37:40.482Z", 0.0065],
                           ["btc/usdcx", 81504.5, 81530.0, 81528.9, "2026-10-28T10:37:40.484Z", 1866],  # bid over ask
                           ["eth/usdcx", "x", 1, 2]]]  # a row that will not parse
    later = ["perpetual", [["btc/usdcx", 81504.5, 81480.0, 81528.9, "2026-10-28T10:37:41.484Z", 1866],
                           ["cc/usdcx", 0.5, 0.4, 0.6, "2026-10-28T10:37:41.482Z", 0]]]  # the first one stands
    depth = {"cc/usdcx": {"symbol": "cc/usdcx", "generatedAt": 1793183858605, "status": "ready",
                          "bids": [["0.1025", "20739"], ["0.1024", "106830"]], "asks": [["0.1026", "27046"]]},
             "btc/usdcx": {"symbol": "btc/usdcx", "status": "unavailable"},
             "eth/usdcx": {"symbol": "eth/usdcx", "status": "ready", "bids": [["2368", "10"]], "asks": []}}
    pushes += ["42" + json.dumps(["rates3", rates]), "42" + json.dumps(["execution-depth", depth]),
               "42" + json.dumps(["rates3", later]),
               "42" + json.dumps(["rates3", ["perpetual", [["eth/usdcx", 2368.25, 2367.5, 2368.9, "2026-10-28T10:37:41Z"]]]])]
    c = CanborsaPublicData(ws_url=url)
    prices, books = await c.stream(["CC/USDCX", "btc/usdcx", "eth/usdcx"], timeout=5)
    assert prices["cc/usdcx"] == CanborsaPrice("cc/usdcx", D("0.102595"), D("0.10254370250000001"),
                                               D("0.1026462975"), prices["cc/usdcx"].at)
    assert prices["cc/usdcx"].at.isoformat() == "2026-10-28T10:37:40.482000+00:00"
    assert prices["btc/usdcx"].bid == D("81480.0") and prices["eth/usdcx"].price == D("2368.25")
    # depth only where the venue has a two-sided book
    assert set(books) == {"cc/usdcx"} and books["cc/usdcx"].best_bid.price == D("0.1025")
    assert "3" in sent  # the ping was answered
    subs = [json.loads(s[2:]) for s in sent if s.startswith("42")]
    assert subs == [["sub", {"name": "rates3", "symbols": ["btc/usdcx", "cc/usdcx", "eth/usdcx"], "product": "perpetual"}],
                    ["sub", {"name": "execution-depth", "symbols": ["btc/usdcx", "cc/usdcx", "eth/usdcx"]}]]


async def test_stream_returns_what_arrived_when_the_time_runs_out(socket_server):
    url, _, pushes = socket_server
    pushes.append("42" + json.dumps(["rates3", ["perpetual", [["cc/usdcx", 0.1, 0.09, 0.11, "2026-10-28T10:00:00Z"]]]]))
    prices, books = await CanborsaPublicData(ws_url=url).stream(["cc/usdcx", "gold/usdcx"], timeout=1)
    assert set(prices) == {"cc/usdcx"} and books == {}


def _canborsa(prices):
    async def stream(symbols, *_a, **_k):
        return {s: p for s, p in prices.items() if s in symbols}, {
            "btc/usdcx": OrderBook("btc/usdcx", None, (BookLevel(D(81510), D(13)),), (BookLevel(D(81520), D(46)),))}
    mk = lambda s, b, v: CanborsaMarket(s, b, "usdcx", ("usdcx", "cc"), D("0.00055"), D("0.00055"), D(5), D(10),
                                         D(50000), 1, D(v))
    ms = [mk("cc/usdcx", "cc", "6000000"), mk("btc/usdcx", "btc", "240000"), mk("gold/usdcx", "gold", "16000"),
          mk("nvda/usdt", "nvda", "900000")]  # a stock: in the 30-day total, not in the table
    return _Venue(markets=ms, funding_rates={"cc/usdcx": D("0.0000239")}, stream=stream)


def test_a_tick_lists_its_perps_with_no_24h_figure_and_keeps_its_30_days(tmp_path, monkeypatch):
    at = lambda s, p, b, a: CanborsaPrice(s, D(p), D(b), D(a), None)
    prices = {"cc/usdcx": at("cc/usdcx", "0.1175", "0.1174", "0.1176"),
              "btc/usdcx": at("btc/usdcx", "83800", "83790", "83810")}  # gold: no price this tick
    out = _tick(tmp_path, monkeypatch, optional={"canborsa": _canborsa(prices)})
    rows = [x for x in out["perps"]["markets"] if x["venue"] == "canborsa"]
    assert [(x["symbol"], x["base"], x["quote"]) for x in rows] == [("btc/usdcx", "BTC", "USDCx"),
                                                                     ("cc/usdcx", "CC", "USDCx")]
    cc = next(x for x in rows if x["base"] == "CC")
    assert cc["last"] == 0.1175 and cc["bid"] == 0.1174 and cc["ask"] == 0.1176 and cc["funding_rate"] == 2.39e-05
    assert cc["basis"] == pytest.approx(0.1175 / out["summary"]["cc_usd"] - 1, abs=1e-6)
    btc = next(x for x in rows if x["base"] == "BTC")
    assert btc["basis"] == pytest.approx(83800 / 83700 - 1, abs=1e-6)
    # no 24h volume, open interest or depth is made up for it
    assert all(x["turnover_24h_usd"] is None and x["open_interest_usd"] is None and x["depth_1pct_usd"] is None
               for x in rows)
    row = next(v for v in out["venues"]["venues"] if v["id"] == "canborsa")
    assert row["status"] == "priced" and row["volume_24h_usd"] is None
    assert row["volume_30d_usd"] == 7_156_000  # every market, the stock's included
    # the perps total is the venues that publish a 24h figure (Ekiden's XAG here), nothing added for it
    assert out["venues"]["perp_volume_24h_usd"] == 5600


def test_a_tick_without_a_price_from_it_shows_it_down(tmp_path, monkeypatch):
    out = _tick(tmp_path, monkeypatch, optional={"canborsa": _canborsa({})})
    assert not [x for x in out["perps"]["markets"] if x["venue"] == "canborsa"]
    row = next(v for v in out["venues"]["venues"] if v["id"] == "canborsa")
    assert row["status"] == "down" and "volume_30d_usd" not in row


def _with_canborsa():
    d = data()
    d["venues"]["venues"].append({"id": "canborsa", "name": "Canborsa", "kind": "Perpetuals", "note": "Canborsa note.",
                                  "status": "priced", "volume_24h_usd": None, "volume_30d_usd": 25_639_717.8})
    for base in ("CC", "BTC", "ETH"):
        d["perps"]["markets"].append({"venue": "canborsa", "symbol": f"{base.lower()}/usdcx", "base": base,
                                      "quote": "USDCx", "last": 1.0, "mark": None, "index": None, "basis": 0.0009,
                                      "funding_rate": 2.4e-05, "open_interest_usd": None, "turnover_24h_usd": None,
                                      "spread_bps": 10.0, "depth_1pct_usd": None})
    return d


def test_its_page_leads_with_its_own_30_days_and_never_says_none_trading():
    f = vp.facts(_with_canborsa())
    cb = f["canborsa"]
    assert cb["perp_volume"] is None and cb["perp_volume_30d"] == 25_639_717.8
    head = vp.headline(cb)
    assert head["sub"] == "$25.6M of perpetuals traded in the last 30 days" and head["big"] == "$25.6M"
    tiles = {t["k"]: t for t in vp.stats(cb)}
    assert tiles["Perps volume, 30 days"]["v"] == "$25.6M" and tiles["Perps volume, 30 days"]["n"] == "its own figure"
    assert tiles["Perp markets"]["n"] == "3 we read" and "none trading" not in str(tiles)
    # no 24h figure is no $0 in anybody's ranking: the other perps venues rank as before
    assert f["rocky"]["perp_rank"] == 1 and f["rocky"]["perp_n"] == 2
    assert any("30-day volume is its own figure" in x for x in cb["method"])


def test_its_page_and_card_build(tmp_path):
    pytest.importorskip("PIL")
    vp.build(tmp_path, _with_canborsa(), 1791380618)
    page = (tmp_path / "venues" / "canborsa" / "index.html").read_text()
    assert "$25.6M" in page and "of perpetuals traded in the last 30 days" in page
    assert (tmp_path / "venues" / "canborsa" / "card.png").exists()
    assert "—" not in page
    # no 24h figure: the markets we read, never "0 of 3 trading"
    assert "Perp markets we read" in page and "Perp markets trading" not in page
    assert "Read from the public routes of the venue&#x27;s web app" in page or \
        "Read from the public routes of the venue's web app" in page
    idx = (tmp_path / "venues" / "index.html").read_text()
    assert 'href="/venues/canborsa/"' in idx and "Canborsa" not in idx.split("<h2>Coming to Canton Venues</h2>", 1)[1]
