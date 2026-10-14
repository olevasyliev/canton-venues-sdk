"""Cantex public market data — no account, no key.

Cantex serves a public REST API (``/v1/public``) and a public websocket
(``/v1/ws/public``) next to the authenticated SDK surface. Everything here
reads those, so a market-data consumer never has to hold an operator key.
Route shapes were verified against ``https://api.cantex.io`` on 2026-10-02;
see SOURCES.md.

Venue-shape notes:

- Pools are constant product with the fee taken on the input.
  ``swap_output`` below reproduces the authenticated ``/v2/pools/quote`` to
  the 10th decimal (measured on CC/USDCx and CBTC/CC at 100 and 10,000 CC,
  both directions), so pricing is done locally from ``/pools/state``.
- That quote also charges a flat network fee in CC on top (0.82–1.24 CC per
  swap across the measurements, varying with ledger traffic). It is not in
  ``/pools/state`` and is NOT included in ``quote()`` here.
- ``/connect/quote`` is public but prices the Connect transfer-with-memo
  path, which carries its own fee (0.25% and 2 CC network fee on 2026-10-02),
  so it is not a stand-in for the API trader's price.
- Amounts are decimal strings everywhere except the CoinGecko-standard
  routes, which send JSON numbers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal

import httpx

from ..core.models import Instrument, Pool, Quote
from ..core.venue import PoolDataAdapter, VenueRequestError

MAINNET_HOST = "https://api.cantex.io"
TESTNET_HOST = "https://api.testnet.cantex.io"
HOST_ENV = "CANTEX_PUBLIC_HOST"
CANDLE_PERIODS = (60, 300, 600, 3600)
CC_ID = "Amulet"

log = logging.getLogger(__name__)


def swap_output(reserve_in: Decimal, reserve_out: Decimal, amount: Decimal, fee: Decimal) -> Decimal:
    """Constant product, fee charged on the input — Cantex's own pricing."""
    net_in = amount * (Decimal(1) - fee)
    return reserve_out * net_in / (reserve_in + net_in)


def _inst(raw: dict) -> Instrument:
    return Instrument(admin=raw["admin"], id=raw["id"])


@dataclass(frozen=True)
class PoolState:
    """One Cantex pool as ``/pools/state`` reports it."""

    contract_id: str
    pool_id: str
    symbol: str  # e.g. "CC-USDCX": token_a-token_b, the market symbol
    token_a: Instrument
    token_b: Instrument
    symbol_a: str
    symbol_b: str
    reserve_a: Decimal
    reserve_b: Decimal
    fee: Decimal  # fraction, charged on the input
    price: Decimal  # token_b per token_a
    tvl_cc: Decimal

    def reserves_for(self, sell: Instrument, buy: Instrument) -> tuple[Decimal, Decimal]:
        if (sell, buy) == (self.token_a, self.token_b):
            return self.reserve_a, self.reserve_b
        if (sell, buy) == (self.token_b, self.token_a):
            return self.reserve_b, self.reserve_a
        raise VenueRequestError(f"pool {self.symbol} does not trade {sell.id}/{buy.id}")

    def output(self, sell: Instrument, buy: Instrument, amount: Decimal) -> Decimal:
        reserve_in, reserve_out = self.reserves_for(sell, buy)
        return swap_output(reserve_in, reserve_out, amount, self.fee)


@dataclass(frozen=True)
class Candle:
    start_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class CantexPublicData(PoolDataAdapter):
    """Read-only Cantex market data from the public API."""

    def __init__(self, *, host: str | None = None, client: httpx.AsyncClient | None = None) -> None:
        self._host = (host or os.getenv(HOST_ENV) or MAINNET_HOST).rstrip("/")
        self._client = client
        self._states: list[PoolState] | None = None

    async def _get(self, path: str, params: dict | None = None) -> dict:
        if self._client is None:
            raise VenueRequestError("adapter is not connected; call connect() first")
        try:
            resp = await self._client.get(f"{self._host}/v1/public{path}", params=params)
        except httpx.HTTPError as exc:
            raise VenueRequestError(f"GET {path}: {exc}") from exc
        if resp.status_code >= 400:
            raise VenueRequestError(f"GET {path}: HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise VenueRequestError(f"GET {path}: non-JSON response") from exc

    # === PoolDataAdapter ===================================================

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def pool_states(self) -> list[PoolState]:
        """Every pool with reserves, fee and TVL, from ``/pools/state``.

        A pool listed before it is funded comes with zero reserves and ``price: null``
        (CBNB-CC, 2026-10-13); its price and TVL read as 0. A pool missing anything else
        is left out with a warning, so one malformed pool never fails the whole read.
        """
        raw = await self._get("/pools/state")
        states = []
        for p in raw["data"]["pools"]:
            try:
                states.append(PoolState(
                    contract_id=p["contract_id"],
                    pool_id=p["pool_id"],
                    symbol=p["symbol"],
                    token_a=_inst(p["token_a_instrument"]),
                    token_b=_inst(p["token_b_instrument"]),
                    symbol_a=p["token_a_instrument"]["symbol"],
                    symbol_b=p["token_b_instrument"]["symbol"],
                    reserve_a=Decimal(p["reserve_a"]),
                    reserve_b=Decimal(p["reserve_b"]),
                    fee=Decimal(p["fee_rate"]),
                    price=Decimal(p["price"] or 0),
                    tvl_cc=Decimal(p["tvl_cc"] or 0),
                ))
            except (KeyError, TypeError, ArithmeticError) as exc:
                log.warning("cantex: pool %s left out: %r", p.get("symbol"), exc)
        self._states = states
        return states

    async def pools(self) -> list[Pool]:
        return [Pool(s.contract_id, s.token_a, s.token_b) for s in await self.pool_states()]

    async def quote(self, sell_amount: Decimal, sell_instrument: Instrument,
                    buy_instrument: Instrument) -> Quote:
        """Price a swap locally from current reserves (deepest matching pool).

        Excludes the flat CC network fee the venue adds to an executed swap.
        """
        if sell_amount <= 0:
            raise VenueRequestError("sell amount must be positive")
        states = self._states if self._states is not None else await self.pool_states()
        matching = [s for s in states if {s.token_a, s.token_b} == {sell_instrument, buy_instrument}]
        if not matching:
            raise VenueRequestError(f"no pool for {sell_instrument.id}/{buy_instrument.id}")
        pool = max(matching, key=lambda s: s.tvl_cc)
        out = pool.output(sell_instrument, buy_instrument, sell_amount)
        reserve_in, reserve_out = pool.reserves_for(sell_instrument, buy_instrument)
        mid = reserve_out / reserve_in
        price = out / sell_amount
        return Quote(
            sell_amount=sell_amount,
            sell_instrument=sell_instrument,
            buy_instrument=buy_instrument,
            returned_amount=out,
            returned_instrument=buy_instrument,
            trade_price=price,
            slippage=Decimal(1) - price / mid,
            fee_percentage=pool.fee,
            estimated_time_seconds=Decimal(0),
            pool_price_before=mid,
        )

    # === exchange statistics ===============================================

    async def tokens(self) -> list[dict]:
        """Every token Cantex knows, with its CoinGecko id where it has one."""
        raw = await self._get("/tokens/info")
        return [t["info"] for t in raw["tokens"]]

    async def volume(self) -> dict:
        """Swap volume, fees and swap count over the trailing 24 hours (in CC)."""
        return (await self._get("/volume"))["data"]

    async def stats(self) -> dict:
        """Daily CC volume series, TVL and active-trader counts."""
        return (await self._get("/stats"))["stats"]

    async def tickers(self) -> list[dict]:
        """CoinGecko-standard tickers: last price and 24h volume per pair."""
        raw = await self._get("/coingecko/tickers")
        return raw if isinstance(raw, list) else raw.get("tickers", [])

    async def markets(self) -> list[dict]:
        """Market symbols with their source (``cantex`` or ``external``)."""
        return (await self._get("/markets/info"))["data"]["markets"]

    async def candles(self, symbols: list[str], period: int = 3600,
                      timeout: float = 30.0) -> dict[str, list[Candle]]:
        """Candle history for several markets, from one websocket snapshot each.

        The public stream answers a subscription with a snapshot of recent
        bars (about three weeks of hourly bars on 2026-10-02), then streams
        updates; this collects the snapshots and closes.
        """
        if period not in CANDLE_PERIODS:
            raise VenueRequestError(f"period must be one of {CANDLE_PERIODS}")
        if self._client is None:
            raise VenueRequestError("adapter is not connected; call connect() first")
        import aiohttp  # the SDK's own transport; httpx has no websocket client

        wanted = {f"market.{s}.candles.{period}": s for s in symbols}
        found: dict[str, list[Candle]] = {}
        url = self._host.replace("https://", "wss://").replace("http://", "ws://") + "/v1/ws/public"

        async def run() -> None:
            async with aiohttp.ClientSession() as session, session.ws_connect(url) as ws:
                await ws.send_json({"op": "subscribe", "channels": list(wanted)})
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = json.loads(msg.data)
                    if data.get("op") == "ping":
                        await ws.send_json({"op": "pong"})
                        continue
                    channel = data.get("channel")
                    if channel in wanted and channel not in found and "bars" in data.get("data", {}):
                        found[channel] = [
                            Candle(int(b["start_ts"]), Decimal(b["open"]), Decimal(b["high"]),
                                   Decimal(b["low"]), Decimal(b["close"]), Decimal(b["volume"]))
                            for b in data["data"]["bars"]
                        ]
                        if len(found) == len(wanted):
                            return

        try:
            await asyncio.wait_for(run(), timeout)
        except TimeoutError:
            pass  # return whatever arrived; a quiet market may send nothing
        return {wanted[c]: bars for c, bars in found.items()}
