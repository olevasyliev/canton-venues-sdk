"""Kora venue adapter: read-only market data for its Canton spot order books.

Kora (kora.trade) runs central limit order books (plus an RFQ desk) with settlement on Canton. Its
market data is public and needs no key, under ``https://spot.api.prod.kora.so/public/v1``: the
markets with their fees and limits, 24h tickers, a depth snapshot per market, recent trades and
candles. Trading is signed and not implemented here; RFQ quotes need a login and are not read.

Endpoint and payload shapes were verified against the live MainNet API on 2026-11-23; see
SOURCES.md.

Venue quirks this adapter absorbs:

- Every market states its own taker and maker fee in basis points (``takerFeeBps`` 2.5 on all
  seven markets on 2026-11-23), so the fee is read per market (``taker_fees``), never assumed.
- The 24h ticker carries a best bid and ask without their sizes, so top of book is taken from a
  depth snapshot, never from the ticker.
- ``/exchange/depth/{market}`` returns the whole book up to ``depth`` levels; 200 answered with
  every level on 2026-11-23 and 1000 was refused with an error body.
- The ticker carries Kora's own dollar figure for each market's 24h volume (``volumeUsd``, with
  ``volumeUsdStale``), read apart (``volumes_usd``) so a caller decides whether to count it.
- ``/exchange/stats`` answered 501 on 2026-11-23 and is not used.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from ..core.models import FundingRate, Market, OrderBook, Side, Ticker, Trade, book_levels, to_decimal
from ..core.venue import MarketDataAdapter, VenueRequestError

log = logging.getLogger("cantonvenues")

BASE_URL = "https://spot.api.prod.kora.so"
_PREFIX = "/public/v1"
# the most levels the depth route accepts (1000 is refused); every book read so far was shorter
MAX_DEPTH = 200


def _dec(value: object) -> Decimal:
    return to_decimal(value, Decimal(0))


def _maybe(value: object) -> Decimal | None:
    """A number, or None when it is missing, not a number, NaN or infinite."""
    try:
        return to_decimal(value)
    except (ArithmeticError, TypeError, ValueError):
        return None


def _when(stamp: object) -> datetime:
    """An ISO-8601 time as Kora writes it (nanoseconds and a trailing Z), in UTC."""
    text = str(stamp).replace("Z", "+00:00")
    head, dot, rest = text.partition(".")
    if dot:  # Python reads at most six fractional digits
        digits = "".join(ch for ch in rest if ch.isdigit())
        text = f"{head}.{digits[:6]}{rest[len(digits):]}"
    t = datetime.fromisoformat(text)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


class KoraAdapter(MarketDataAdapter):
    """Read-only market data from Kora's spot order books."""

    def __init__(self, *, base_url: str = BASE_URL, client: httpx.AsyncClient | None = None) -> None:
        self._root = base_url.rstrip("/") + _PREFIX
        self._client = client

    # === plumbing ========================================================

    async def _get(self, path: str, params: dict | None = None) -> object:
        if self._client is None:
            raise VenueRequestError("adapter is not connected; call connect() first")
        try:
            resp = await self._client.get(f"{self._root}{path}", params=params)
        except httpx.HTTPError as exc:
            raise VenueRequestError(f"GET {path}: {exc}") from exc
        if resp.status_code >= 400:
            raise VenueRequestError(f"GET {path}: HTTP {resp.status_code}: {resp.text.strip()[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise VenueRequestError(f"GET {path}: not JSON: {resp.text.strip()[:200]}") from exc

    async def _markets_raw(self) -> list[dict]:
        raw = await self._get("/markets")
        if not isinstance(raw, dict) or not isinstance(raw.get("markets"), list):
            raise VenueRequestError(f"unexpected /markets response: {raw!r}"[:300])
        return raw["markets"]

    # === MarketDataAdapter ===============================================

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        await self._markets_raw()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def markets(self) -> list[Market]:
        out = []
        for s in await self._markets_raw():
            try:  # one market that will not parse is left out, not every market
                out.append(Market(
                    symbol=s["symbol"],
                    base=s["base"],
                    quote=s["quote"],
                    is_trading=s.get("status") == "active",
                    tick_size=_dec(s.get("tickSize")),
                    min_order_size=_dec(s.get("minOrderBase")),
                    size_step=_dec(s.get("stepSize")),
                    min_notional=_dec(s.get("minNotionalQuote")),
                    max_leverage=Decimal(1),
                    funding_interval_minutes=0,
                ))
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                log.warning("kora: market row skipped: %r", exc)
        return out

    async def taker_fees(self) -> dict[str, Decimal]:
        """Each market's taker fee as a fraction (``takerFeeBps`` 2.5 is 0.00025), as Kora states it;
        a market without a readable fee is left out, never given one."""
        out = {}
        for s in await self._markets_raw():
            bps = _maybe(s.get("takerFeeBps"))
            if s.get("symbol") and bps is not None and bps >= 0:
                out[s["symbol"]] = bps / 10_000
        return out

    async def _tickers_raw(self) -> list[dict]:
        raw = await self._get("/exchange/tickers")
        if not isinstance(raw, dict) or not isinstance(raw.get("tickers"), list):
            raise VenueRequestError(f"unexpected /exchange/tickers response: {raw!r}"[:300])
        return raw["tickers"]

    async def tickers(self, symbol: str | None = None) -> list[Ticker]:
        out = []
        for t in await self._tickers_raw():
            if symbol is not None and t.get("market") != symbol:
                continue
            try:  # one row that will not parse is left out, not every ticker
                out.append(Ticker(
                    symbol=t["market"],
                    last_price=_dec(t.get("last")),
                    index_price=Decimal(0),
                    mark_price=Decimal(0),
                    open_interest=Decimal(0),
                    volume_24h=_dec(t.get("volume")),
                    turnover_24h=_dec(t.get("quoteVolume")),
                    funding_rate=Decimal(0),
                    next_funding_time=None,
                    # the ticker's bid and ask come without sizes: read the book instead
                    best_bid=None,
                    best_ask=None,
                ))
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                log.warning("kora: ticker row skipped: %r", exc)
        return out

    async def volumes_usd(self) -> dict[str, dict]:
        """Kora's own dollar figure for each market's 24h volume: {symbol: {"usd", "stale", "as_of"}}.
        ``usd`` is None when the field is missing or not a number."""
        out = {}
        for t in await self._tickers_raw():
            if not t.get("market"):
                continue
            out[t["market"]] = {"usd": _maybe(t.get("volumeUsd")),
                                "stale": bool(t.get("volumeUsdStale")),
                                "as_of": t.get("volumeUsdAsOf")}
        return out

    async def order_book(self, symbol: str, depth: int = MAX_DEPTH) -> OrderBook:
        raw = await self._get(f"/exchange/depth/{symbol}", {"depth": min(depth, MAX_DEPTH)})
        if not isinstance(raw, dict) or "bids" not in raw or "asks" not in raw:
            raise VenueRequestError(f"unexpected /exchange/depth response: {raw!r}"[:300])
        try:
            stamp = _when(raw["asOf"]) if raw.get("asOf") else datetime.now(UTC)
        except ValueError:
            stamp = datetime.now(UTC)
        return OrderBook(symbol=symbol, timestamp=stamp,
                         bids=book_levels(raw.get("bids") or []), asks=book_levels(raw.get("asks") or []))

    async def recent_trades(self, symbol: str, limit: int = 50) -> list[Trade]:
        raw = await self._get(f"/exchange/trades/{symbol}", {"limit": limit})
        if not isinstance(raw, dict) or not isinstance(raw.get("trades"), list):
            raise VenueRequestError(f"unexpected /exchange/trades response: {raw!r}"[:300])
        trades = []
        for t in raw["trades"]:
            try:  # one print that will not parse is left out, not every print
                trades.append(Trade(
                    trade_id=str(t["tradeId"]),
                    symbol=symbol,
                    # the taker's side, as Kora labels it (not checked against a trade of our own)
                    side=Side.BUY if str(t.get("side")).upper() == "BUY" else Side.SELL,
                    price=_dec(t["price"]),
                    size=_dec(t["quantity"]),
                    timestamp=_when(t["at"]),
                ))
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                log.warning("kora: trade skipped: %r", exc)
        return sorted(trades, key=lambda t: t.timestamp, reverse=True)

    async def volume_candles(self, symbol: str, interval: str = "1h",
                             start: datetime | None = None) -> list[dict]:
        """Oldest first: each candle's open time (unix seconds), base volume and close price."""
        params = {"interval": interval}
        if start is not None:
            params["startTime"] = start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw = await self._get(f"/exchange/candlesticks/{symbol}", params)
        if not isinstance(raw, dict) or not isinstance(raw.get("candles"), list):
            raise VenueRequestError(f"unexpected /exchange/candlesticks response: {raw!r}"[:300])
        out = []
        for c in raw["candles"]:
            try:  # one candle that will not parse is left out, not the whole series
                out.append({"t": int(_when(c["openTime"]).timestamp()), "volume": _dec(c.get("v")),
                            "close": _dec(c.get("c")), "closed": bool(c.get("closed"))})
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                log.warning("kora: candle skipped: %r", exc)
        return sorted(out, key=lambda c: c["t"])

    async def funding_history(self, symbol: str, limit: int = 50) -> list[FundingRate]:
        return []  # spot only
