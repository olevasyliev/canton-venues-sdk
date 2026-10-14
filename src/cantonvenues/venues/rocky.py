"""Rocky Exchange venue adapter — read-only market data (Canton spot and perps).

Rocky runs off-chain order books for spot and perpetuals with settlement on
Canton. Its public REST surface is Binance-compatible and needs no key:
``/api/v3/*`` for spot and ``/fapi/v1/*`` for perpetuals. Trading is signed and
not implemented here.

Endpoint and payload shapes were verified against the live MainNet API on
2026-10-03; see SOURCES.md.

Venue quirks this adapter absorbs:

- The 24h ticker reports ``bidPrice``/``askPrice`` as ``"0"`` on every symbol
  even while the book is full (observed on all four spot symbols, 2026-10-03),
  so top of book is taken from a depth snapshot, never from the ticker.
- Numbers arrive as decimal strings with 18 fractional digits; they are parsed
  exactly.
- Spot symbols are dashed (``CBTC-USDCX``), perp symbols are not
  (``BTCUSDT``). Symbols are passed through exactly as the venue lists them.
- ``/fapi/v1/premiumIndex`` and ``/fapi/v1/fundingRate`` answered with empty
  bodies on 2026-10-03, so mark, index and funding are left at zero and
  ``funding_history`` returns what the venue returns, which may be nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx

from ..core.models import BookLevel, FundingRate, Market, OrderBook, Side, Ticker, Trade, live_levels
from ..core.venue import MarketDataAdapter, VenueRequestError

BASE_URL = "https://api.rocky.exchange"
SPOT, PERP = "spot", "perp"
_PREFIX = {SPOT: "/api/v3", PERP: "/fapi/v1"}


def _dec(value: object) -> Decimal:
    return Decimal(str(value)) if value not in (None, "") else Decimal(0)


def _ts(millis: object) -> datetime:
    return datetime.fromtimestamp(int(millis) / 1000, tz=UTC)


class RockyAdapter(MarketDataAdapter):
    """Read-only market data from Rocky, for its spot or its perp books."""

    def __init__(
        self,
        market_type: str = SPOT,
        *,
        base_url: str = BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if market_type not in _PREFIX:
            raise ValueError(f"market_type must be {SPOT!r} or {PERP!r}, got {market_type!r}")
        self.market_type = market_type
        self._root = base_url.rstrip("/") + _PREFIX[market_type]
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
        if not resp.content.strip():
            return None  # some perp routes answer 200 with an empty body
        return resp.json()

    # === MarketDataAdapter ===============================================

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        info = await self._get("/exchangeInfo")
        if not isinstance(info, dict) or not isinstance(info.get("symbols"), list):
            raise VenueRequestError(f"unexpected /exchangeInfo response: {info!r}")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def markets(self) -> list[Market]:
        info = await self._get("/exchangeInfo")
        out = []
        for s in info["symbols"]:  # type: ignore[index]
            filters = {f["filterType"]: f for f in s.get("filters", [])}
            lot = filters.get("LOT_SIZE", {})
            out.append(
                Market(
                    symbol=s["symbol"],
                    base=s["baseAsset"],
                    quote=s["quoteAsset"],
                    is_trading=s["status"] == "TRADING",
                    tick_size=_dec(filters.get("PRICE_FILTER", {}).get("tickSize")),
                    min_order_size=_dec(lot.get("minQty")),
                    size_step=_dec(lot.get("stepSize")),
                    min_notional=_dec(filters.get("NOTIONAL", {}).get("minNotional")),
                    max_leverage=Decimal(1) if self.market_type == SPOT else Decimal(0),
                    funding_interval_minutes=0,
                )
            )
        return out

    async def tickers(self, symbol: str | None = None) -> list[Ticker]:
        raw = await self._get("/ticker/24hr", {"symbol": symbol} if symbol else None)
        rows = [raw] if isinstance(raw, dict) else (raw or [])
        return [
            Ticker(
                symbol=t["symbol"],
                last_price=_dec(t.get("lastPrice")),
                index_price=Decimal(0),
                mark_price=Decimal(0),
                open_interest=Decimal(0),
                volume_24h=_dec(t.get("volume")),
                turnover_24h=_dec(t.get("quoteVolume")),
                funding_rate=Decimal(0),
                next_funding_time=None,
                # the ticker's bid/ask are always "0": read the book instead
                best_bid=None,
                best_ask=None,
            )
            for t in rows
        ]

    async def order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        raw = await self._get("/depth", {"symbol": symbol, "limit": depth})
        if not isinstance(raw, dict):
            raise VenueRequestError(f"unexpected /depth response: {raw!r}")
        stamp = raw.get("T") or raw.get("E") or raw.get("lastUpdateId")
        return OrderBook(
            symbol=symbol,
            timestamp=_ts(stamp) if stamp else datetime.now(UTC),
            bids=live_levels(BookLevel(price=_dec(p), size=_dec(q)) for p, q in raw.get("bids", [])),
            asks=live_levels(BookLevel(price=_dec(p), size=_dec(q)) for p, q in raw.get("asks", [])),
        )

    async def recent_trades(self, symbol: str, limit: int = 50) -> list[Trade]:
        raw = await self._get("/trades", {"symbol": symbol, "limit": limit}) or []
        trades = [
            Trade(
                trade_id=str(t["id"]),
                symbol=symbol,
                # the maker was the buyer, so the aggressor sold
                side=Side.SELL if t.get("isBuyerMaker") else Side.BUY,
                price=_dec(t["price"]),
                size=_dec(t["qty"]),
                timestamp=_ts(t["time"]),
            )
            for t in raw  # type: ignore[union-attr]
        ]
        return sorted(trades, key=lambda t: t.timestamp, reverse=True)

    async def funding_history(self, symbol: str, limit: int = 50) -> list[FundingRate]:
        if self.market_type == SPOT:
            return []
        raw = await self._get("/fundingRate", {"symbol": symbol, "limit": limit}) or []
        rates = [
            FundingRate(symbol=f["symbol"], rate=_dec(f["fundingRate"]), timestamp=_ts(f["fundingTime"]))
            for f in raw  # type: ignore[union-attr]
        ]
        return sorted(rates, key=lambda f: f.timestamp, reverse=True)
