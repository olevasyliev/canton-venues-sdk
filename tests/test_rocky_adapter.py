"""Offline tests for the Rocky market-data adapter.

HTTP is mocked with ``httpx.MockTransport``. Fixtures are trimmed excerpts of
live MainNet responses from `https://api.rocky.exchange` captured on
2026-10-03, including the ticker's always-zero bid/ask and the perp routes'
empty bodies.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from cantonvenues import RockyAdapter, Side, VenueRequestError

SPOT_INFO = {
    "timezone": "UTC",
    "symbols": [
        {
            "symbol": "CBTC-USDCX",
            "baseAsset": "CBTC",
            "quoteAsset": "USDCX",
            "status": "TRADING",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                {"filterType": "LOT_SIZE", "minQty": "0.0001", "stepSize": "0.0001"},
                {"filterType": "NOTIONAL", "minNotional": "5"},
            ],
        }
    ],
}
PERP_INFO = {
    "symbols": [
        {"symbol": "CCUSDT", "pair": "CC-PERP", "status": "TRADING", "baseAsset": "CC",
         "quoteAsset": "USDT", "pricePrecision": 2, "quantityPrecision": 3}
    ]
}
TICKERS = [
    {"askPrice": "0", "askQty": "0", "bidPrice": "0", "bidQty": "0", "count": 1546,
     "lastPrice": "84544.400000000000000000", "quoteVolume": "824168.139778000000000000",
     "symbol": "CBTC-USDCX", "volume": "9.746600000000000000"}
]
DEPTH = {
    "bids": [["84547.1", "15.1226"], ["84547", "0.0252"]],
    "asks": [["84547.2", "1.5753"], ["84547.3", "0.0458"]],
    "lastUpdateId": 1791020689453,
}
TRADES = [
    {"id": "01a10127-0fba-76d1-be69-ce6ad6f4580a", "isBuyerMaker": False,
     "price": "84544.300000000000000000", "qty": "0.002000000000000000", "time": 1791020700000},
    {"id": "01a10127-871d-7de1-bef3-7dfecb85abe9", "isBuyerMaker": True,
     "price": "84547.100000000000000000", "qty": "0.000300000000000000", "time": 1791020730141},
]


def _adapter(routes: dict, market_type: str = "spot", seen: list | None = None) -> RockyAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url)
        body = routes.get(request.url.path)
        if body is None:
            return httpx.Response(404, text="not found")
        if body == "":
            return httpx.Response(200, content=b"")
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RockyAdapter(market_type, client=client)


async def test_spot_markets_map_filters() -> None:
    async with _adapter({"/api/v3/exchangeInfo": SPOT_INFO}) as rocky:
        (mk,) = await rocky.markets()
    assert (mk.symbol, mk.base, mk.quote, mk.is_trading) == ("CBTC-USDCX", "CBTC", "USDCX", True)
    assert mk.tick_size == Decimal("0.1") and mk.min_notional == Decimal("5")


async def test_perps_use_the_fapi_prefix() -> None:
    async with _adapter({"/fapi/v1/exchangeInfo": PERP_INFO}, "perp") as rocky:
        (mk,) = await rocky.markets()
    assert mk.symbol == "CCUSDT" and mk.quote == "USDT"


async def test_ticker_zero_bid_ask_is_not_passed_through() -> None:
    routes = {"/api/v3/exchangeInfo": SPOT_INFO, "/api/v3/ticker/24hr": TICKERS}
    async with _adapter(routes) as rocky:
        (t,) = await rocky.tickers()
    assert t.last_price == Decimal("84544.4") and t.turnover_24h == Decimal("824168.139778")
    assert t.best_bid is None and t.best_ask is None


async def test_order_book_gives_top_of_book() -> None:
    seen: list = []
    routes = {"/api/v3/exchangeInfo": SPOT_INFO, "/api/v3/depth": DEPTH}
    async with _adapter(routes, seen=seen) as rocky:
        book = await rocky.order_book("CBTC-USDCX", 5)
    assert book.best_bid.price == Decimal("84547.1") and book.best_ask.size == Decimal("1.5753")
    assert book.mid_price == Decimal("84547.15")
    assert seen[-1].params["symbol"] == "CBTC-USDCX" and seen[-1].params["limit"] == "5"


async def test_trades_newest_first_with_aggressor_side() -> None:
    routes = {"/api/v3/exchangeInfo": SPOT_INFO, "/api/v3/trades": TRADES}
    async with _adapter(routes) as rocky:
        newest, older = await rocky.recent_trades("CBTC-USDCX")
    assert newest.side is Side.SELL and older.side is Side.BUY
    assert newest.timestamp > older.timestamp


async def test_volume_candles_oldest_first_in_quote_units() -> None:
    # live CBTC-USDCX 1h klines, 2026-10-28 (newest first here to show the sort); the third will not parse
    klines = [
        [1793167200000, "80873.91", "80980", "80842.4", "80849.91", "0.0415", 1793170799999,
         "3357.306023000000000000", 46, "0", "0", "0"],
        [1793163600000, "80966.8", "80966.8", "80873.91", "80873.91", "0.0352", 1793167199999,
         "2847.875733000000000000", 48, "0", "0", "0"],
        [1793160000000, "81079.2"],
    ]
    seen: list = []
    routes = {"/api/v3/exchangeInfo": SPOT_INFO, "/api/v3/klines": klines}
    async with _adapter(routes, seen=seen) as rocky:
        older, newer = await rocky.volume_candles("CBTC-USDCX", "1h", 3)
    assert (older["t"], newer["t"]) == (1793163600, 1793167200)
    assert newer["quote_volume"] == Decimal("3357.306023") and newer["trades"] == 46
    assert seen[-1].params["interval"] == "1h" and seen[-1].params["limit"] == "3"


async def test_empty_perp_funding_body_is_an_empty_list() -> None:
    routes = {"/fapi/v1/exchangeInfo": PERP_INFO, "/fapi/v1/fundingRate": ""}
    async with _adapter(routes, "perp") as rocky:
        assert await rocky.funding_history("CCUSDT") == []


async def test_connect_rejects_an_unexpected_payload() -> None:
    with pytest.raises(VenueRequestError):
        async with _adapter({"/api/v3/exchangeInfo": {"oops": 1}}):
            pass


def test_unknown_market_type_is_refused() -> None:
    with pytest.raises(ValueError):
        RockyAdapter("options")
