"""Temple venue adapter — read-only market data (Canton spot order books).

Temple runs off-chain central limit order books with settlement on Canton and
is the largest venue on the network by volume. Two surfaces:

- **Settled volume, no key.** ``GET /api/exchange/settled_volume`` returns USD
  volume and trade counts per market for any RFC 3339 window. Verified live on
  MainNet on 2026-10-03.
- **Market data, key required.** Ticker, order book and recent trades under
  ``/api/v1/market/*`` need an ``X-API-Key`` header, issued per account in the
  Temple app (Settings > API Keys). Routes and parameters come from Temple's SDK
  ``@temple-digital-group/temple-canton-js`` 2.1.10; the payloads were verified
  live on MainNet on 2026-10-04 and **differ from the SDK's TypeScript types**:
  each response is wrapped (``{"tickers": [...]}``, ``{"orderbook": {...}}``,
  ``{"trades": [...]}``), numbers arrive as JSON numbers, the ticker carries no
  bid/ask, and trades are stamped ``created_at``. Without a key these methods
  raise ``VenueAuthError`` instead of calling out.
- **Fees** (help.templedigitalgroup.com, "Fees & Rebates", read 2026-10-04):
  taker 1 bp, maker 0.5 bp, prepaid in USDCx; deposits and withdrawals cost
  5-12 CC through the partner wallets.

Venue-shape notes:

- The SDK rewrites ``CC`` to ``Amulet`` in request symbols; the live API accepts
  both and answers with ``CC/USDCx`` either way. ``wire_symbol`` keeps the SDK's
  rewrite for requests.
- There are no perpetuals and no funding: ``funding_history`` returns nothing.

Trading (``OrderTradingAdapter``), off unless the adapter is built with
``trading=True``:

- Routes from the SDK: ``POST /api/trading/orders`` (limit, optional
  ``post_only``, ``expires_at``), ``POST /api/trading/orders/{id}/cancel``,
  ``POST /api/trading/orders/cancel-all``, ``GET /api/trading/orders/active``,
  ``GET /api/trading/balances``, ``GET /api/trading/delegation``.
- The read routes were verified live on 2026-10-04; empty lists come back as
  ``null``, which is read as empty. **The order, cancel and cancel-all
  responses are not yet verified** (no order has been placed through this
  adapter); they are parsed defensively and the venue's record is kept in
  ``Order.raw``.
- Before any order fills, the account needs a linked wallet with a trading
  delegation, funds deposited, and a prepaid fee balance in USDCx;
  ``trading_status`` reports all three.
- Mainnet REST is ``https://api.templedigitalgroup.com``; testnet is
  ``https://api-testnet.templedigitalgroup.com`` (SDK config).
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from ..core.models import Balance, FundingRate, Instrument, Market, Order, OrderBook, Side, Ticker, Trade, book_levels, to_decimal
from ..core.venue import OrderTradingAdapter, TradingDisabledError, VenueAuthError, VenueError, VenueRequestError

MAINNET_BASE_URL = "https://api.templedigitalgroup.com"
TESTNET_BASE_URL = "https://api-testnet.templedigitalgroup.com"
API_KEY_ENV = "TEMPLE_API_KEY"
TAKER_FEE = Decimal("0.0001")  # 1 bp, Temple help center, "Fees & Rebates"


def _dec(value: object) -> Decimal:
    return to_decimal(value, Decimal(0))


def _ts(value: object) -> datetime:
    """Timestamps are RFC 3339 strings in the SDK types; epoch milliseconds are accepted too."""
    if isinstance(value, int | float) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def wire_symbol(symbol: str) -> str:
    """``CC/USDCx`` -> ``Amulet/USDCx``, as Temple's market routes expect."""
    return re.sub(r"\bCC\b", "Amulet", symbol)


def _rfc3339(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class TempleAdapter(OrderTradingAdapter):
    """Temple: settled volume without a key; books, tickers and (opt-in) trading with one."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = MAINNET_BASE_URL,
        client: httpx.AsyncClient | None = None,
        trading: bool = False,
    ) -> None:
        self._key = api_key if api_key is not None else os.getenv(API_KEY_ENV)
        self._base = base_url.rstrip("/")
        self._client = client
        self._trading = trading

    @property
    def has_key(self) -> bool:
        return bool(self._key)

    # === plumbing ========================================================

    async def _get(self, path: str, params: dict | None = None, *, keyed: bool = True) -> object:
        if self._client is None:
            raise VenueRequestError("adapter is not connected; call connect() first")
        if keyed and not self._key:
            raise VenueAuthError(f"GET {path}: Temple market data needs an API key ({API_KEY_ENV})")
        headers = {"X-API-Key": self._key} if keyed else None
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = await self._client.get(f"{self._base}{path}", params=clean or None, headers=headers)
        except httpx.HTTPError as exc:
            raise VenueRequestError(f"GET {path}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise VenueAuthError(f"GET {path}: HTTP {resp.status_code}: {resp.text.strip()[:200]}")
        if resp.status_code >= 400:
            raise VenueRequestError(f"GET {path}: HTTP {resp.status_code}: {resp.text.strip()[:200]}")
        return resp.json()

    async def _post(self, path: str, body: dict | None = None) -> object:
        if not self._trading:
            raise TradingDisabledError(f"POST {path}: build TempleAdapter(trading=True) to trade")
        if self._client is None:
            raise VenueRequestError("adapter is not connected; call connect() first")
        if not self._key:
            raise VenueAuthError(f"POST {path}: Temple trading needs an API key ({API_KEY_ENV})")
        try:
            resp = await self._client.post(f"{self._base}{path}", json=body or {}, headers={"X-API-Key": self._key})
        except httpx.HTTPError as exc:
            raise VenueRequestError(f"POST {path}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise VenueAuthError(f"POST {path}: HTTP {resp.status_code}: {resp.text.strip()[:200]}")
        if resp.status_code >= 400:
            raise VenueRequestError(f"POST {path}: HTTP {resp.status_code}: {resp.text.strip()[:300]}")
        return resp.json() if resp.content.strip() else {}

    # === settled volume (no key) =========================================

    async def settled_volume(self, hours: int = 24, end: datetime | None = None) -> dict:
        """USD volume and trade count per market over the last ``hours``."""
        end = end or datetime.now(UTC)
        raw = await self._get("/api/exchange/settled_volume",
                              {"start_time": _rfc3339(end - timedelta(hours=hours)), "end_time": _rfc3339(end)},
                              keyed=False)
        if not isinstance(raw, dict) or not isinstance(raw.get("markets"), list):
            raise VenueRequestError(f"unexpected settled_volume response: {raw!r}")
        return raw

    # === MarketDataAdapter ===============================================

    async def connect(self) -> None:
        """Open the client and check Temple answers: its keyless settled volume, or, with a key, its
        keyed ticker when the keyless route fails (its books then still read; 2026-10-15 replay)."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        try:
            await self.settled_volume(1)
        except VenueError:
            if not self._key:
                raise
            await self._get("/api/v1/market/ticker")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def markets(self) -> list[Market]:
        """Every listed market with a key (from the ticker); without one, those that settled in 24 h."""
        if self._key:
            raw = await self._get("/api/v1/market/ticker")
            symbols = [t["symbol"] for t in self._list(raw, "tickers")]
        else:
            symbols = [mk["symbol"] for mk in (await self.settled_volume(24))["markets"]]
        out = []
        for symbol in symbols:
            mk = {"symbol": symbol}
            base, _, quote = mk["symbol"].partition("/")
            out.append(Market(symbol=mk["symbol"], base=base, quote=quote, is_trading=True,
                              tick_size=Decimal(0), min_order_size=Decimal(0), size_step=Decimal(0),
                              min_notional=Decimal(0), max_leverage=Decimal(1), funding_interval_minutes=0))
        return out

    @staticmethod
    def _list(raw: object, field: str) -> list[dict]:
        if not isinstance(raw, dict) or not isinstance(raw.get(field), list):
            raise VenueRequestError(f"unexpected response, no {field!r} list: {raw!r}"[:300])
        return raw[field]

    async def tickers(self, symbol: str | None = None) -> list[Ticker]:
        """24h stats per market. Temple's ticker has no bid/ask; read ``order_book`` for those.
        ``turnover_24h`` is Temple's own ``quote_volume_24h_usd``."""
        raw = await self._get("/api/v1/market/ticker", {"symbol": wire_symbol(symbol) if symbol else None})
        return [
            Ticker(
                symbol=t["symbol"], last_price=_dec(t.get("last_price")), index_price=Decimal(0),
                mark_price=Decimal(0), open_interest=Decimal(0), volume_24h=_dec(t.get("volume_24h")),
                turnover_24h=_dec(t.get("quote_volume_24h_usd")), funding_rate=Decimal(0),
                next_funding_time=None, best_bid=None, best_ask=None,
            )
            for t in self._list(raw, "tickers")
        ]

    async def order_book(self, symbol: str, depth: int = 50) -> OrderBook:
        raw = await self._get("/api/v1/market/orderbook", {"symbol": wire_symbol(symbol), "levels": depth})
        book = raw.get("orderbook") if isinstance(raw, dict) else None
        if not isinstance(book, dict) or "bids" not in book:
            raise VenueRequestError(f"unexpected orderbook response: {raw!r}")
        return OrderBook(
            symbol=book.get("symbol", symbol),
            timestamp=_ts(book["timestamp"]) if book.get("timestamp") else datetime.now(UTC),
            bids=book_levels((lv.get("price"), lv.get("quantity")) for lv in book["bids"] if isinstance(lv, dict)),
            asks=book_levels((lv.get("price"), lv.get("quantity")) for lv in book["asks"] if isinstance(lv, dict)),
        )

    async def recent_trades(self, symbol: str, limit: int = 50) -> list[Trade]:
        raw = await self._get("/api/v1/market/trades", {"symbol": wire_symbol(symbol), "limit": min(limit, 500)})
        trades = [
            Trade(trade_id=str(t["trade_id"]), symbol=t.get("symbol", symbol),
                  side=Side.SELL if str(t.get("side", "")).lower() == "sell" else Side.BUY,
                  price=_dec(t["price"]), size=_dec(t["quantity"]), timestamp=_ts(t["created_at"]))
            for t in self._list(raw, "trades")
        ]
        return sorted(trades, key=lambda t: t.timestamp, reverse=True)

    async def funding_history(self, symbol: str, limit: int = 50) -> list[FundingRate]:
        return []

    # === trading =========================================================

    async def trading_status(self) -> dict:
        """What stands between this account and a fill: linked wallet, delegation, fee balance."""
        deleg = await self._get("/api/trading/delegation")
        bal = await self._get("/api/trading/balances")
        fees = (bal or {}).get("fee_balances") or []
        fee_available = sum((_dec(f.get("available")) for f in fees), Decimal(0))
        linked = (deleg or {}).get("linked_parties") or []
        delegations = (deleg or {}).get("delegations") or []
        return {
            "linked_parties": len(linked),
            "delegations": len(delegations),
            "fee_available_usdcx": fee_available,
            "ready": bool(linked) and bool(delegations) and fee_available > 0,
        }

    async def balances(self) -> list[Balance]:
        raw = await self._get("/api/trading/balances")
        return [
            Balance(instrument=Instrument(admin="", id=b["asset"]), symbol=b["asset"], name=b["asset"],
                    unlocked=_dec(b.get("unlocked")), locked=_dec(b.get("locked")) + _dec(b.get("in_flight")))
            for b in ((raw or {}).get("balances") or [])
        ]

    async def open_orders(self, symbol: str | None = None) -> list[Order]:
        raw = await self._get("/api/trading/orders/active", {"symbol": wire_symbol(symbol) if symbol else None})
        return [self._order(o) for o in ((raw or {}).get("orders") or [])]

    @staticmethod
    def _order(o: dict, fallback: dict | None = None) -> Order:
        """One order record; ``fallback`` fills what an unverified response may leave out."""
        f = fallback or {}
        side = str(o.get("side", f.get("side", ""))).lower()
        stamp = o.get("created_at")
        return Order(
            order_id=str(o.get("order_id") or o.get("id") or o.get("request_id") or ""),
            symbol=o.get("symbol", f.get("symbol", "")),
            side=Side.SELL if side == "sell" else Side.BUY,
            price=_dec(o.get("price", f.get("price"))),
            quantity=_dec(o.get("quantity", f.get("quantity"))),
            status=str(o.get("status", "submitted")),
            created_at=_ts(stamp) if stamp else None,
            raw=o,
        )

    async def place_limit_order(
        self,
        symbol: str,
        side: Side,
        quantity: Decimal,
        price: Decimal,
        *,
        post_only: bool = False,
        expires_at: datetime | None = None,
    ) -> Order:
        if quantity <= 0 or price <= 0:
            raise VenueRequestError("quantity and price must be positive")
        body: dict = {"symbol": wire_symbol(symbol), "side": side.value, "quantity": float(quantity),
                      "price": float(price), "order_type": "limit"}
        if post_only:
            body["order_subtype"] = "post_only"
        if expires_at:
            body["expires_at"] = _rfc3339(expires_at)
        raw = await self._post("/api/trading/orders", body)
        record = raw.get("order", raw) if isinstance(raw, dict) else {}
        return self._order(record if isinstance(record, dict) else {}, fallback=body | {"symbol": symbol})

    async def cancel_order(self, order_id: str) -> bool:
        if not order_id:
            raise VenueRequestError("order_id is required")
        raw = await self._post(f"/api/trading/orders/{order_id}/cancel")
        return bool(isinstance(raw, dict) and raw.get("success", True))

    async def cancel_all(self, symbol: str | None = None) -> int:
        raw = await self._post("/api/trading/orders/cancel-all", {"symbol": wire_symbol(symbol)} if symbol else None)
        if not isinstance(raw, dict):
            return 0
        for k in ("cancelled_count", "canceled_count", "count", "cancelled", "canceled"):
            v = raw.get(k)
            if isinstance(v, int):
                return v
            if isinstance(v, list):
                return len(v)
        return 0
