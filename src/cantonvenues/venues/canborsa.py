"""Canborsa public market data — perpetuals, read-only, no key.

Canborsa is an account-based exchange on Canton: one balance, deposits from six
networks, perpetuals on about 87 assets and swaps. It publishes no documented
API yet (its roadmap lists "API access for professional traders" for Q4 2026).
Its web app reads two sources without a session, and this adapter reads them
the same way:

- REST under ``https://app.canborsa.com/api/v1``: ``/perpetuals/markets``
  (fees, leverage and 30-day volume per market) and
  ``/perpetuals/funding-rates`` (each market's current rate per settlement
  period, one hour on every market).
- socket.io at ``wss://app.canborsa.com/socket.io``: ``rates3`` (each perp
  market's price, bid and ask, about once a second) and ``execution-depth``
  (the depth a perp order fills against, 20 levels a side, valid 60 s).

Shapes were checked live on 2026-10-28; see SOURCES.md.

Venue quirks this adapter absorbs:

- Every market is listed twice, once with ``isShort`` true, both rows carrying
  the same ``volume30d``: one row per symbol is kept and its volume counted once.
- Symbols are lower case ``base/quote`` (``cc/usdcx``); gold and silver are
  ``gold`` and ``silver``.
- The depth is the perp's, not a spot book's: CC/USDCx and CC/USDC return the
  same levels, and a swap-only token (``xaut/usdcx``) is "unavailable".
- Prices arrive as JSON floats (0.10254370250000001) and are read through
  ``str`` into Decimal.
- No 24h volume, open interest or trade list is public.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from ..core.models import OrderBook, book_levels, to_decimal
from ..core.venue import VenueRequestError

log = logging.getLogger("cantonvenues")

BASE_URL = "https://app.canborsa.com/api/v1"
WS_URL = "wss://app.canborsa.com/socket.io/?EIO=4&transport=websocket"
# the socket answers only a browser's origin (a connect without it was refused, 2026-10-28)
ORIGIN = "https://app.canborsa.com"


@dataclass(frozen=True)
class CanborsaMarket:
    """One perp market as Canborsa lists it."""

    symbol: str  # "cc/usdcx"
    base: str  # "cc", "gold"
    quote: str  # "usdcx"
    collateral: tuple[str, ...]
    opening_fee: Decimal  # a fraction of the position: 0.00055
    closing_fee: Decimal
    max_leverage: Decimal
    min_usd: Decimal
    max_usd: Decimal
    funding_hours: int
    volume_30d: Decimal  # the venue's own figure, in the quote token


@dataclass(frozen=True)
class CanborsaPrice:
    """A perp market's price as the venue quotes it, with the bid and ask an order gets."""

    symbol: str
    price: Decimal
    bid: Decimal
    ask: Decimal
    at: datetime


def _ms(x: object) -> datetime:
    return datetime.fromtimestamp(int(x) / 1000, tz=UTC)


class CanborsaPublicData:
    """Read-only Canborsa perpetuals: markets, funding, prices and execution depth."""

    def __init__(self, *, base_url: str = BASE_URL, ws_url: str = WS_URL,
                 client: httpx.AsyncClient | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._ws = ws_url
        self._client = client

    async def __aenter__(self) -> CanborsaPublicData:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _get(self, path: str):
        if self._client is None:
            raise VenueRequestError("not connected; call connect() first")
        try:
            resp = await self._client.get(f"{self._base}{path}")
        except httpx.HTTPError as exc:
            raise VenueRequestError(f"GET {path}: {exc}") from exc
        if resp.status_code >= 400:
            raise VenueRequestError(f"GET {path}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise VenueRequestError(f"GET {path}: not JSON") from exc

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "canton-venues/1"})
        await self.markets()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def markets(self) -> list[CanborsaMarket]:
        """Every perp market, one row per symbol (the venue lists each twice, long and short)."""
        raw = await self._get("/perpetuals/markets")
        if not isinstance(raw, list):
            raise VenueRequestError(f"unexpected /perpetuals/markets response: {str(raw)[:200]}")
        out: dict[str, CanborsaMarket] = {}
        for row in raw:
            try:  # one row that will not parse is left out, not the venue
                base, quote = str(row["baseTicker"]).lower(), str(row["quoteTicker"]).lower()
                sym = f"{base}/{quote}"
                if sym in out:
                    continue
                out[sym] = CanborsaMarket(
                    sym, base, quote, tuple(str(c).lower() for c in row.get("collateralTickers") or ()),
                    to_decimal(row["openingFee"]), to_decimal(row["closingFee"]), to_decimal(row["maxLeverage"]),
                    to_decimal(row["minAmountUSD"]), to_decimal(row["maxAmountUSD"]),
                    int(row["settlementPeriodInHours"]), to_decimal(row["volume30d"]))
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                log.warning("canborsa: market row skipped: %r", exc)
        if not out:
            raise VenueRequestError("no perp market listed")
        return list(out.values())

    async def funding_rates(self) -> dict[str, Decimal]:
        """Each market's current funding rate per settlement period (one hour), a fraction."""
        raw = await self._get("/perpetuals/funding-rates")
        if not isinstance(raw, dict):
            raise VenueRequestError(f"unexpected /perpetuals/funding-rates response: {str(raw)[:200]}")
        out = {}
        for sym, row in raw.items():
            try:
                out[str(sym).lower()] = to_decimal(row["rate"])
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
        return out

    async def stream(self, symbols: list[str], timeout: float = 15.0
                     ) -> tuple[dict[str, CanborsaPrice], dict[str, OrderBook]]:
        """Each symbol's price and execution depth, from one socket session: it subscribes, keeps the
        first of each that arrives, and closes once every symbol has both (a market without depth
        answers "unavailable" and gets none) or ``timeout`` runs out, returning what arrived."""
        import aiohttp  # httpx has no websocket client; the Cantex adapter uses the same

        wanted = {s.lower() for s in symbols}
        prices: dict[str, CanborsaPrice] = {}
        books: dict[str, OrderBook] = {}
        answered: set[str] = set()  # a depth reply, a book or "unavailable"

        def read_prices(rows) -> None:
            for row in rows:
                try:
                    sym = str(row[0]).lower()
                    price, bid, ask = (to_decimal(str(x)) for x in row[1:4])
                except (IndexError, TypeError, ValueError, ArithmeticError):
                    continue
                if sym in wanted and sym not in prices and 0 < bid <= price <= ask:
                    at = datetime.fromisoformat(str(row[4]).replace("Z", "+00:00")) if len(row) > 4 else None
                    prices[sym] = CanborsaPrice(sym, price, bid, ask, at or datetime.now(UTC))

        def read_depth(by_symbol: dict) -> None:
            for sym, d in by_symbol.items():
                sym = str(sym).lower()
                if sym not in wanted or sym in answered or not isinstance(d, dict):
                    continue
                answered.add(sym)
                if d.get("status") != "ready":
                    continue
                book = OrderBook(sym, _ms(d.get("generatedAt") or 0), book_levels(d.get("bids") or ()),
                                 book_levels(d.get("asks") or ()))
                if book.bids and book.asks and book.best_bid.price < book.best_ask.price:
                    books[sym] = book

        async def run() -> None:
            async with aiohttp.ClientSession() as session, \
                    session.ws_connect(self._ws, headers={"Origin": ORIGIN}) as ws:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = msg.data
                    if data.startswith("0{"):  # engine.io open: join the default namespace
                        await ws.send_str("40")
                    elif data.startswith("40"):
                        subs = [{"name": "rates3", "symbols": sorted(wanted), "product": "perpetual"},
                                {"name": "execution-depth", "symbols": sorted(wanted)}]
                        for sub in subs:
                            await ws.send_str("42" + json.dumps(["sub", sub]))
                    elif data == "2":  # ping
                        await ws.send_str("3")
                    elif data.startswith("44"):
                        raise VenueRequestError(f"socket refused: {data[:200]}")
                    elif data.startswith("42"):
                        try:
                            event, *args = json.loads(data[2:])
                        except (ValueError, TypeError):
                            continue
                        if event == "rates3" and args and isinstance(args[0], list) and len(args[0]) > 1 \
                                and args[0][0] == "perpetual":
                            read_prices(args[0][1])
                        elif event == "execution-depth" and args and isinstance(args[0], dict):
                            read_depth(args[0])
                    if wanted <= prices.keys() and wanted <= answered:
                        return

        try:
            await asyncio.wait_for(run(), timeout)
        except TimeoutError:
            pass  # what arrived is returned; the caller decides whether that is a read
        except aiohttp.ClientError as exc:
            raise VenueRequestError(f"socket: {exc}") from exc
        return prices, books
