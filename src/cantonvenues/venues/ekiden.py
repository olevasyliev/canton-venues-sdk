"""Ekiden venue adapter — read-only market data (Canton perpetuals).

Ekiden runs an institutional order-book perpetuals venue. It moved off Aptos
onto Canton; its gateway (`ekiden-gateway`, Rust/Axum) serves an unauthenticated
public market-data surface, which is what this adapter covers. Trading needs an
Ed25519-signed session and is not implemented here.

Endpoint and payload shapes were verified against the live testnet gateway on
2026-07-27 and against MainNet (the default) on 2026-10-03; see SOURCES.md.

Venue-shape notes (vs the spot venues):

- There are no pools and nothing to swap, so this implements
  ``MarketDataAdapter`` rather than ``VenueAdapter``.
- Prices and sizes arrive as decimal strings and are parsed exactly. Timestamps
  are epoch milliseconds and are surfaced as timezone-aware UTC datetimes.
- Trade rows use single-letter keys (``i``/``s``/``S``/``v``/``p``/``T``), and
  book rows are bare ``[price, size]`` pairs.

Venue quirks this adapter absorbs:

- ``depth`` is an undeclared enum: only 10, 50 and 200 are accepted. Anything
  else fails server-side, and that failure arrives as **plain text**, not the
  JSON error envelope every other route uses.
- The published OpenAPI document does not parse (a trailing comma after the
  staging server entry), so client generation from it fails; these shapes were
  taken from live responses instead.
- ``/api/v1/info`` still reports the Canton validator under a field named
  ``aptos_network``, left over from the migration.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from ..core.models import (
    book_levels,
    to_decimal,
    BookLevel,
    FundingRate,
    Market,
    OrderBook,
    Side,
    Ticker,
    Trade,
)
from ..core.venue import MarketDataAdapter, VenueRequestError

# MainNet opened 2026-09-08; Ekiden's docs list it as "Production". Its routes and payloads
# match the testnet gateway's (checked 2026-10-03).
log = logging.getLogger("cantonvenues")

MAINNET_BASE_URL = "https://api.ekiden.fi"
TESTNET_BASE_URL = "https://api.cnt.ekiden.fi"
STAGING_BASE_URL = "https://api.canton.ekiden.fi"
BASE_URL_ENV = "EKIDEN_BASE_URL"

VALID_DEPTHS = (10, 50, 200)


def _dec(value: object) -> Decimal:
    return to_decimal(value)


def _ts(millis: object) -> datetime:
    return datetime.fromtimestamp(int(millis) / 1000, tz=UTC)


class EkidenAdapter(MarketDataAdapter):
    """Read-only market data from the Ekiden gateway."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or os.getenv(BASE_URL_ENV) or MAINNET_BASE_URL).rstrip("/")
        self._client = client
        self._info: dict | None = None

    # === plumbing ========================================================

    async def _get(self, path: str, params: dict | None = None) -> object:
        if self._client is None:
            raise VenueRequestError("adapter is not connected; call connect() first")
        try:
            resp = await self._client.get(f"{self._base_url}{path}", params=params)
        except httpx.HTTPError as exc:
            raise VenueRequestError(f"GET {path}: {exc}") from exc
        if resp.status_code >= 400:
            raise VenueRequestError(f"GET {path}: {self._error_text(resp)}")
        return resp.json()

    @staticmethod
    def _error_text(resp: httpx.Response) -> str:
        """Errors are usually JSON, but query-string rejections are plain text."""
        try:
            body = resp.json()
        except ValueError:
            return f"HTTP {resp.status_code}: {resp.text.strip()[:200]}"
        if isinstance(body, dict):
            return f"HTTP {resp.status_code}: {body.get('error') or body}"
        return f"HTTP {resp.status_code}: {body}"

    @staticmethod
    def _rows(raw: object, path: str) -> list[dict]:
        """Unwrap the `{"list": [...]}` envelope the market routes return."""
        if not isinstance(raw, dict) or not isinstance(raw.get("list"), list):
            raise VenueRequestError(f"unexpected {path} response: {raw!r}")
        return raw["list"]

    # === MarketDataAdapter ===============================================

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        info = await self._get("/api/v1/info")
        if not isinstance(info, dict):
            raise VenueRequestError(f"unexpected /api/v1/info response: {info!r}")
        phase = info.get("runtime_manifest_phase")
        if phase != "ready":
            raise VenueRequestError(f"gateway is not ready: runtime_manifest_phase={phase!r}")
        self._info = info

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def info(self) -> dict | None:
        """The gateway's `/api/v1/info` payload, populated by connect()."""
        return self._info

    async def markets(self) -> list[Market]:
        raw = await self._get("/api/v1/market/instruments-info")
        out = []
        for m in self._rows(raw, "/api/v1/market/instruments-info"):
            lot = m["lot_size_filter"]
            out.append(
                Market(
                    symbol=m["symbol"],
                    base=m["base_coin"],
                    quote=m["quote_coin"],
                    is_trading=m["status"] == "Trading",
                    tick_size=_dec(m["price_filter"]["tick_size"]),
                    min_order_size=_dec(lot["min_order_qty"]),
                    size_step=_dec(lot["qty_step"]),
                    min_notional=_dec(lot["min_notional_value"]),
                    max_leverage=_dec(m["leverage_filter"]["max_leverage"]),
                    funding_interval_minutes=int(m["funding_interval"]),
                )
            )
        return out

    async def tickers(self, symbol: str | None = None) -> list[Ticker]:
        params = {"symbol": symbol} if symbol else None
        raw = await self._get("/api/v1/market/tickers", params)
        out = []
        for t in self._rows(raw, "/api/v1/market/tickers"):
            try:  # one malformed row is skipped, not the whole venue (a null mark_price, 2026-10-14)
                out.append(self._ticker(t))
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                log.warning("ekiden: ticker row skipped: %r", exc)
        return out

    def _ticker(self, t: dict) -> Ticker:
        return Ticker(
            symbol=t["symbol"],
            last_price=_dec(t["last_price"]),
            index_price=_dec(t["index_price"]),
            mark_price=_dec(t["mark_price"]),
            open_interest=_dec(t["open_interest"]),
            volume_24h=_dec(t["volume_24h"]),
            turnover_24h=_dec(t["turnover_24h"]),
            funding_rate=_dec(t["funding_rate"]),
            next_funding_time=_ts(t["next_funding_time"]) if t.get("next_funding_time") else None,
            best_bid=self._top(t.get("best_bid_price"), t.get("best_bid_size")),
            best_ask=self._top(t.get("best_ask_price"), t.get("best_ask_size")),
        )

    @staticmethod
    def _top(price: object, size: object) -> BookLevel | None:
        """Top of book, or None when that side is empty.

        An empty side is reported as ``"0"``/``"0"`` rather than null, in a
        field a client reads as a price. Passing that through would hand
        callers a best ask of zero. Observed live on 2026-07-27, when the CC
        book had no offers left after a run of buys.
        """
        if price is None or size is None:
            return None
        price_dec, size_dec = _dec(price), _dec(size)
        if price_dec <= 0 or size_dec <= 0:
            return None
        return BookLevel(price=price_dec, size=size_dec)

    async def order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        if depth not in VALID_DEPTHS:
            raise VenueRequestError(
                f"depth must be one of {VALID_DEPTHS}, got {depth}; the venue accepts "
                "no other value and rejects it with a plain-text error"
            )
        raw = await self._get("/api/v1/market/orderbook", {"symbol": symbol, "depth": depth})
        if not isinstance(raw, dict) or not isinstance(raw.get("result"), dict):
            raise VenueRequestError(f"unexpected /api/v1/market/orderbook response: {raw!r}")
        book = raw["result"]
        return OrderBook(
            symbol=book["s"],
            timestamp=_ts(book["ts"]),
            bids=book_levels(book.get("b", [])),
            asks=book_levels(book.get("a", [])),
        )

    async def recent_trades(self, symbol: str, limit: int = 50) -> list[Trade]:
        raw = await self._get("/api/v1/market/recent-trade", {"symbol": symbol, "limit": limit})
        return [
            Trade(
                trade_id=t["i"],
                symbol=t["s"],
                side=Side.SELL if t["S"] == "Sell" else Side.BUY,
                price=_dec(t["p"]),
                size=_dec(t["v"]),
                timestamp=_ts(t["T"]),
                sequence=t.get("seq"),
            )
            for t in self._rows(raw, "/api/v1/market/recent-trade")
        ]

    async def funding_history(self, symbol: str, limit: int = 50) -> list[FundingRate]:
        raw = await self._get("/api/v1/market/funding/history", {"symbol": symbol, "limit": limit})
        return [
            FundingRate(
                symbol=f["symbol"],
                rate=_dec(f["funding_rate"]),
                timestamp=_ts(f["funding_time"]),
            )
            for f in self._rows(raw, "/api/v1/market/funding/history")
        ]
