"""Offline tests for the Kora market-data adapter.

HTTP is mocked with ``httpx.MockTransport``. Fixtures are trimmed excerpts of live MainNet responses
from `https://spot.api.prod.kora.so/public/v1` captured on 2026-11-23, including a one-sided thin
book (CBTC-USDCx: four asks, about $859 in all) and nanosecond timestamps.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from cantonvenues import KoraAdapter, Side, VenueRequestError

MARKETS = {"markets": [
    {"base": "CBTC", "defaultOrderTtlSeconds": 2592000, "defaultSlippagePercent": "2", "makerFeeBps": 1,
     "maxOrderQtyLimit": "10.0", "maxOrderQtyMarket": "1.0", "maxOrderTtlSeconds": 2592000,
     "minNotionalQuote": "10.0", "minOrderBase": "0.0001", "mtbLimitPercent": "5", "quote": "USDCx",
     "status": "active", "stepSize": "0.0001", "symbol": "CBTC-USDCx", "takerDelayMs": 400,
     "takerFeeBps": 2.5, "tickSize": "1.0"},
    {"base": "CC", "makerFeeBps": 1, "maxOrderQtyMarket": "500000.0", "minNotionalQuote": "10.0",
     "minOrderBase": "100.0", "quote": "USDCx", "status": "active", "stepSize": "1.0", "symbol": "CC-USDCx",
     "takerFeeBps": 2.5, "tickSize": "0.0001"},
    {"base": "CBTC", "makerFeeBps": 1, "minNotionalQuote": "0.002", "minOrderBase": "0.0002", "quote": "eXAU",
     "status": "halted", "stepSize": "0.0001", "symbol": "CBTC-eXAU", "takerFeeBps": "NaN", "tickSize": "0.001"},
]}
TICKERS = {"asOf": "2026-11-23T20:50:45.771962206Z", "tickers": [
    {"asOf": "2026-11-23T20:50:45.771962206Z", "ask": "61820", "bid": "61086", "high": "64530", "last": "61099",
     "low": "60372", "market": "CBTC-USDCx", "priceChange": "364", "priceChangePercent": "0.60",
     "quoteVolume": "1636897.8405", "volume": "26.6039", "volumeUsd": "1636661.1614412421",
     "volumeUsdAsOf": "2026-11-23T20:50:31Z", "volumeUsdStale": False},
    {"asOf": "2026-11-23T20:50:45.771962206Z", "ask": "0.0775", "bid": "0.0774", "last": "0.0775",
     "market": "CC-USDCx", "quoteVolume": "32479.0443", "volume": "419760", "volumeUsd": None,
     "volumeUsdStale": True},
]}
DEPTH = {"asOf": "2026-11-23T20:50:58.15375665Z",
         "asks": [["61152", "0.0004"], ["61153", "0.0003"], ["61177", "0.0072"], ["62695", "0.006"]],
         "bestAskPrice": "61152", "bestAskQty": "0.0004", "bestBidPrice": "61139", "bestBidQty": "0.0093",
         "bids": [["61139", "0.0093"], ["61135", "0.0032"], ["61129", "0.0081"]],
         "lastUpdateId": 1}
TRADES = {"market": "CBTC-USDCx", "trades": [
    {"at": "2026-11-23T20:49:57.88381Z", "mktEpoch": 1795090349856, "price": "61207", "quantity": "0.0006",
     "settlementState": "settled", "side": "SELL", "tradeId": "201443"},
    {"at": "2026-11-23T20:50:07.501744Z", "mktEpoch": 1795090349856, "price": "61099", "quantity": "0.0007",
     "settlementState": "settled", "side": "BUY", "tradeId": "201444"},
]}
CANDLES = {"candles": [
    {"c": "3936.62", "closed": True, "h": "3941.24", "l": "3930.59", "o": "3939.08",
     "openTime": "2026-11-03T02:00:00Z", "v": "31.179"},
    {"c": "3939.08", "closed": True, "h": "3949.2", "l": "3935.24", "o": "3947.63",
     "openTime": "2026-11-03T01:00:00Z", "v": "43.792"},
]}


def _adapter(routes: dict, seen: list | None = None) -> KoraAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url)
        body = routes.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"code": 404, "error": "not found"})
        return httpx.Response(200, json=body)

    return KoraAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_markets_map_status_and_limits() -> None:
    async with _adapter({"/public/v1/markets": MARKETS}) as kora:
        mks = {mk.symbol: mk for mk in await kora.markets()}
    cbtc = mks["CBTC-USDCx"]
    assert (cbtc.base, cbtc.quote, cbtc.is_trading) == ("CBTC", "USDCx", True)
    assert cbtc.tick_size == Decimal("1.0") and cbtc.min_notional == Decimal("10.0")
    assert cbtc.min_order_size == Decimal("0.0001") and cbtc.size_step == Decimal("0.0001")
    assert mks["CBTC-eXAU"].is_trading is False  # "halted" is not "active"


async def test_taker_fee_read_per_market_and_bad_one_left_out() -> None:
    async with _adapter({"/public/v1/markets": MARKETS}) as kora:
        fees = await kora.taker_fees()
    assert fees == {"CBTC-USDCx": Decimal("0.00025"), "CC-USDCx": Decimal("0.00025")}


async def test_tickers_take_no_top_of_book_and_filter_by_symbol() -> None:
    async with _adapter({"/public/v1/markets": MARKETS, "/public/v1/exchange/tickers": TICKERS}) as kora:
        all_ = await kora.tickers()
        (one,) = await kora.tickers("CC-USDCx")
    assert [t.symbol for t in all_] == ["CBTC-USDCx", "CC-USDCx"]
    assert all_[0].turnover_24h == Decimal("1636897.8405") and all_[0].volume_24h == Decimal("26.6039")
    assert all_[0].best_bid is None and all_[0].best_ask is None
    assert one.last_price == Decimal("0.0775")


async def test_volumes_usd_keep_kora_figure_and_staleness() -> None:
    async with _adapter({"/public/v1/markets": MARKETS, "/public/v1/exchange/tickers": TICKERS}) as kora:
        vol = await kora.volumes_usd()
    assert vol["CBTC-USDCx"]["usd"] == Decimal("1636661.1614412421") and vol["CBTC-USDCx"]["stale"] is False
    assert vol["CC-USDCx"] == {"usd": None, "stale": True, "as_of": None}


async def test_order_book_parses_levels_and_nanosecond_time() -> None:
    seen: list = []
    async with _adapter({"/public/v1/markets": MARKETS, "/public/v1/exchange/depth/CBTC-USDCx": DEPTH},
                        seen) as kora:
        book = await kora.order_book("CBTC-USDCx", 5000)
    assert seen[-1].params["depth"] == "200"  # capped: the route refuses 1000
    assert book.best_bid.price == Decimal("61139") and book.best_ask.price == Decimal("61152")
    assert len(book.asks) == 4 and len(book.bids) == 3
    assert book.timestamp == datetime(2026, 11, 23, 20, 50, 58, 153756, tzinfo=UTC)


async def test_recent_trades_newest_first_with_taker_side() -> None:
    async with _adapter({"/public/v1/markets": MARKETS, "/public/v1/exchange/trades/CBTC-USDCx": TRADES}) as kora:
        trades = await kora.recent_trades("CBTC-USDCx", 100)
    assert [t.trade_id for t in trades] == ["201444", "201443"]
    assert trades[0].side is Side.BUY and trades[1].side is Side.SELL
    assert trades[0].size == Decimal("0.0007") and trades[0].price == Decimal("61099")


async def test_volume_candles_oldest_first() -> None:
    async with _adapter({"/public/v1/markets": MARKETS,
                         "/public/v1/exchange/candlesticks/eXAU-USDCx": CANDLES}) as kora:
        candles = await kora.volume_candles("eXAU-USDCx")
    assert [c["volume"] for c in candles] == [Decimal("43.792"), Decimal("31.179")]
    assert candles[0]["t"] == int(datetime(2026, 11, 3, 1, tzinfo=UTC).timestamp())


async def test_funding_history_is_empty_for_spot() -> None:
    async with _adapter({"/public/v1/markets": MARKETS}) as kora:
        assert await kora.funding_history("CBTC-USDCx") == []


async def test_errors_become_venue_request_errors() -> None:
    async with _adapter({"/public/v1/markets": MARKETS}) as kora:
        with pytest.raises(VenueRequestError, match="HTTP 404"):
            await kora.order_book("NOPE-USDCx")
    with pytest.raises(VenueRequestError, match="unexpected /markets"):
        async with _adapter({"/public/v1/markets": {"code": 501}}):
            pass
