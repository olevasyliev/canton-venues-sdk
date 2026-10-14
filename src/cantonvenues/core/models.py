"""Venue-agnostic domain models.

The spot models mirror the Cantex SDK response models field for field; see
SOURCES.md for the exact ``_sdk.py`` line references. Amounts are ``Decimal``
because the SDK carries all monetary values as ``Decimal``.

The market-data models below (``Market``, ``OrderBook``, ``Trade``,
``Ticker``, ``FundingRate``) exist because a derivatives venue has no pools
and no swaps: it has a symbol, a book, and a funding rate. Forcing those into
the spot vocabulary would make the field names lie.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


@dataclass(frozen=True)
class Instrument:
    """A Canton instrument (token), keyed by its admin party and instrument id."""

    admin: str
    id: str


@dataclass(frozen=True)
class Pool:
    """A single liquidity pool between two instruments."""

    contract_id: str
    token_a: Instrument
    token_b: Instrument


@dataclass(frozen=True)
class Balance:
    """A single token's balance within an account."""

    instrument: Instrument
    symbol: str
    name: str
    unlocked: Decimal
    locked: Decimal


@dataclass(frozen=True)
class Quote:
    """A price quote for swapping one instrument for another."""

    sell_amount: Decimal
    sell_instrument: Instrument
    buy_instrument: Instrument
    returned_amount: Decimal
    returned_instrument: Instrument
    trade_price: Decimal
    slippage: Decimal
    fee_percentage: Decimal
    estimated_time_seconds: Decimal
    # Venues that report these do; the rest leave them unset. Cantex charges a
    # flat network fee per swap on top of the pool fee, always in Canton Coin,
    # and it is NOT deducted from ``returned_amount``.
    network_fee: Decimal | None = None
    network_fee_instrument: Instrument | None = None
    pool_price_before: Decimal | None = None


@dataclass(frozen=True)
class SwapResult:
    """A confirmed, on-ledger swap execution."""

    input_amount: Decimal
    input_instrument: Instrument
    output_amount: Decimal
    output_instrument: Instrument
    price: Decimal
    admin_fee_amount: Decimal
    liquidity_fee_amount: Decimal
    market: str


# === market data =========================================================


class Side(Enum):
    """Which side of the book a resting order or a print sits on."""

    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Market:
    """A tradable symbol and the constraints an order against it must respect."""

    symbol: str
    base: str
    quote: str
    is_trading: bool
    tick_size: Decimal
    min_order_size: Decimal
    size_step: Decimal
    min_notional: Decimal
    max_leverage: Decimal
    funding_interval_minutes: int


@dataclass(frozen=True)
class BookLevel:
    """One price level of an order book."""

    price: Decimal
    size: Decimal


def live_levels(levels) -> tuple[BookLevel, ...]:
    """The levels a trade can fill: a price or size of 0 (a null or "" a venue sent, read as 0) is no
    level. Walking a 0-price ask divides by zero (Rocky's CBTC-USDCX, replayed 2026-10-14)."""
    return tuple(lv for lv in levels if lv.price > 0 and lv.size > 0)


@dataclass(frozen=True)
class OrderBook:
    """A depth snapshot. ``bids`` descend and ``asks`` ascend by price."""

    symbol: str
    timestamp: datetime
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]

    @property
    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def mid_price(self) -> Decimal | None:
        """Midpoint, or None when either side is empty."""
        if not (self.bids and self.asks):
            return None
        return (self.bids[0].price + self.asks[0].price) / 2

    @property
    def spread(self) -> Decimal | None:
        """Absolute spread, or None when either side is empty."""
        if not (self.bids and self.asks):
            return None
        return self.asks[0].price - self.bids[0].price


@dataclass(frozen=True)
class Trade:
    """A public print. ``side`` is the aggressor's side."""

    trade_id: str
    symbol: str
    side: Side
    price: Decimal
    size: Decimal
    timestamp: datetime
    sequence: int | None = None


@dataclass(frozen=True)
class Ticker:
    """A market's current state: prices, funding, and top of book."""

    symbol: str
    last_price: Decimal
    index_price: Decimal
    mark_price: Decimal
    open_interest: Decimal
    volume_24h: Decimal
    turnover_24h: Decimal
    funding_rate: Decimal
    next_funding_time: datetime | None
    best_bid: BookLevel | None
    best_ask: BookLevel | None


@dataclass(frozen=True)
class FundingRate:
    """One settled funding period."""

    symbol: str
    rate: Decimal
    timestamp: datetime


@dataclass(frozen=True)
class Order:
    """A resting or recently placed limit order on an order-book venue."""

    order_id: str
    symbol: str
    side: Side
    price: Decimal
    quantity: Decimal
    status: str
    created_at: datetime | None = None
    raw: dict | None = None  # the venue's own record, for fields this model does not carry
