"""Tradecraft venue adapter — read-only spot AMM on Canton mainnet.

Tradecraft (Obsidian Systems) runs the deepest Canton AMM we have found with a
public API: 23 pools across 13 tokens on mainnet, no credentials needed to
read or price. Shapes below were verified against ``https://api.tradecraft.fi/v1``
on 2026-08-11; see SOURCES.md for the route map.

Venue-shape notes (vs the other spot venues):

- Settlement is by Daml choices on the venue's own package (``AMMRules``),
  which needs a validator node and a party of our own, so this implements
  ``PoolDataAdapter`` rather than ``VenueAdapter``. Everything up to the
  order — discovery, state, pricing, LP sizing — is here.
- Pools are identified by an AMM id (``TC CC/USDCx LP``), not a contract id.
- Amounts arrive as JSON numbers, not decimal strings, so responses are parsed
  with ``parse_float=Decimal``: that keeps the venue's own digits instead of
  passing them through a float a second time.

Venue quirks this adapter absorbs, each measured live:

- **Path order is honoured by the quote routes and ignored by everything
  else.** ``/quoteForFixedInput/USDCx/CC`` really does sell USDCx, but
  ``/inspect``, ``/ratio``, ``/tokenA``, ``/tokenB`` and ``/quoteLPDeposit``
  answer in the pool's own canonical order whichever way you ask, without an
  error. Sizing an LP deposit from the reversed path therefore inverts the
  pair: ``/quoteLPDeposit/USDCx/CC?instrument1Amount=1000`` means 1000 CC.
  This adapter never trusts request order — it orients every response by the
  ``token_a_id``/``token_b_id`` the venue itself returns.
- **The realized fee is half the pool's total fee on *each* leg.** The API
  publishes an LP fee and an operator fee (0.2% + 0.1% on most pools); the
  engine charges ``(lp + operator) / 2`` on the way in and again on the way
  out. Verified across all 23 pools: the local pricing below reproduces every
  venue quote to the last digit of a float64.
- **The same two fee constants ship in two units.** ``/pools`` reports
  ``lp_fee_percent: 0.2`` (a percent) while ``/feeAmount`` reports
  ``fee_amount: 0.002`` (a fraction) with a schema that calls it a percentage.
  ``fees()`` returns fractions, always.
- **The two history routes take different windows.** ``yield_history`` accepts
  hour/day/week/month/year, ``volume_history`` only hour/day/week, and the
  published spec declares hour/day/week for both. Rejected client-side.
- Zero and negative amounts are quoted rather than refused: the venue answers
  ``200 {"user_gets": 0}``. Guarded here before the request goes out.
- An unknown path returns ``text/plain`` while every documented error is the
  JSON envelope, so error text is read defensively.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import quote as urlquote

import httpx

from ..core.models import Instrument, Pool, Quote
from ..core.venue import PoolDataAdapter, VenueRequestError

log = logging.getLogger("cantonvenues")

MAINNET_BASE_URL = "https://api.tradecraft.fi/v1"
DEVNET_BASE_URL = "https://tradecraft.validator.dev.canton.obsidian.systems/amm-http-api"
BASE_URL_ENV = "TRADECRAFT_BASE_URL"

YIELD_WINDOWS = ("hour", "day", "week", "month", "year")
VOLUME_WINDOWS = ("hour", "day", "week")

_HUNDRED = Decimal(100)
_TWO = Decimal(2)


def _dec(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def swap_output(
    reserve_in: Decimal,
    reserve_out: Decimal,
    giving_amount: Decimal,
    total_fee: Decimal,
) -> Decimal:
    """Price a swap the way the venue's engine prices it.

    Constant product with ``total_fee / 2`` charged on the input leg and again
    on the output leg. ``total_fee`` is a fraction (0.003 for the usual
    0.2% + 0.1% pool), so the realized fee is ``1 - (1 - total_fee / 2) ** 2``
    — a shade under the published total, which is what the measurements show.
    """
    leg = Decimal(1) - total_fee / _TWO
    net_in = giving_amount * leg
    return leg * reserve_out * net_in / (reserve_in + net_in)


def swap_input(
    reserve_in: Decimal,
    reserve_out: Decimal,
    getting_amount: Decimal,
    total_fee: Decimal,
) -> Decimal:
    """Inverse of :func:`swap_output`: input required for a fixed output."""
    leg = Decimal(1) - total_fee / _TWO
    gross_out = getting_amount / leg
    if gross_out >= reserve_out:
        raise VenueRequestError(
            f"output {getting_amount} exceeds the pool's reserve of {reserve_out}"
        )
    return reserve_in * gross_out / ((reserve_out - gross_out) * leg)


@dataclass(frozen=True)
class PoolState:
    """One AMM pool, oriented the way the venue itself orients it.

    ``token_a``/``token_b`` are the venue's canonical order for the pool, not
    the order they were asked for.
    """

    amm_id: str
    token_a: str
    token_b: str
    reserve_a: Decimal
    reserve_b: Decimal
    lp_supply: Decimal
    lp_token_name: str
    lp_fee: Decimal
    operator_fee: Decimal
    yield_24h: Decimal | None = None
    unclaimed_operator_fees: Decimal | None = None
    updated_at: datetime | None = None

    @property
    def total_fee(self) -> Decimal:
        """Total fee as a fraction; the engine charges half of it per leg."""
        return self.lp_fee + self.operator_fee

    @property
    def realized_fee(self) -> Decimal:
        """What a swap actually pays, both legs included."""
        leg = Decimal(1) - self.total_fee / _TWO
        return Decimal(1) - leg * leg

    def reserves_for(self, sell: str, buy: str) -> tuple[Decimal, Decimal]:
        """(reserve in, reserve out) for selling ``sell`` into this pool."""
        if {sell, buy} != {self.token_a, self.token_b}:
            raise VenueRequestError(f"pool {self.amm_id} does not trade {sell}/{buy}")
        if sell == self.token_a:
            return self.reserve_a, self.reserve_b
        return self.reserve_b, self.reserve_a

    def price(self, base: str, quote: str) -> Decimal:
        """Spot price of one ``base`` in ``quote``, fees excluded.

        Prefer this to the venue's ``/ratio`` route, which answers in the
        pool's canonical order however it is asked and whose published
        description ("the price of token B expressed in token A") is the
        inverse of the formula it ships with (tokenB / tokenA).
        """
        reserve_base, reserve_quote = self.reserves_for(base, quote)
        return reserve_quote / reserve_base


@dataclass(frozen=True)
class LiquidityQuote:
    """An LP deposit or withdrawal, labelled by symbol rather than by position.

    The venue returns ``instrument_1``/``instrument_2``, which follow the
    pool's canonical order and not the order of the request.
    """

    amount_a: Decimal
    token_a: str
    amount_b: Decimal
    token_b: str
    lp_tokens: Decimal | None = None


class TradecraftAdapter(PoolDataAdapter):
    """Read-only pools, state and pricing from the public Tradecraft API."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or os.getenv(BASE_URL_ENV) or MAINNET_BASE_URL).rstrip("/")
        self._client = client
        self._states: dict[str, PoolState] = {}
        self._instruments: dict[str, Instrument] = {}
        self._symbols: dict[Instrument, str] = {}

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
        try:
            # parse_float=Decimal: amounts cross the wire as JSON numbers, and
            # two pools are already large enough that a float64 cannot step at
            # the ledger's 1e-10. This keeps the digits the venue sent.
            return json.loads(resp.text, parse_float=Decimal)
        except ValueError as exc:
            raise VenueRequestError(f"GET {path}: non-JSON response: {resp.text[:200]}") from exc

    @staticmethod
    def _error_text(resp: httpx.Response) -> str:
        """Errors are JSON, except unknown paths, which are plain text."""
        try:
            body = resp.json()
        except ValueError:
            return f"HTTP {resp.status_code}: {resp.text.strip()[:200]}"
        if isinstance(body, dict):
            return f"HTTP {resp.status_code}: {body.get('error') or body}"
        return f"HTTP {resp.status_code}: {body}"

    @staticmethod
    def _pair_path(route: str, token_a: str, token_b: str) -> str:
        return f"{route}/{urlquote(token_a, safe='')}/{urlquote(token_b, safe='')}"

    # === PoolDataAdapter =================================================

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        health = await self._get("/health")
        if not isinstance(health, dict) or health.get("status") != "ok":
            raise VenueRequestError(f"venue is not healthy: {health!r}")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def pool_states(self) -> list[PoolState]:
        """Every pool with its reserves and fee constants, from ``/pools``."""
        raw = await self._get("/pools")
        if not isinstance(raw, dict) or not isinstance(raw.get("pools"), list):
            raise VenueRequestError(f"unexpected /pools response: {raw!r}")
        states = []
        for row in raw["pools"]:
            try:  # one malformed row is skipped, not the whole venue (a null holding, replayed 2026-10-14)
                states.append(self._state_from_pools_row(row))
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                log.warning("tradecraft: pool row skipped: %r", exc)
        self._states = {s.amm_id: s for s in states}
        return states

    @staticmethod
    def _state_from_pools_row(row: dict) -> PoolState:
        return PoolState(
            amm_id=row["lp_token_name"],
            token_a=row["token1"],
            token_b=row["token2"],
            reserve_a=_dec(row["token1_holdings"]),
            reserve_b=_dec(row["token2_holdings"]),
            lp_supply=_dec(row["total_lp_tokens"]),
            lp_token_name=row["lp_token_name"],
            # published as percents on this route and as fractions on
            # /feeAmount; fractions everywhere here.
            lp_fee=_dec(row["lp_fee_percent"]) / _HUNDRED,
            operator_fee=_dec(row["operator_fee_percent"]) / _HUNDRED,
            yield_24h=_dec(row["yield24h"]) if row.get("yield24h") is not None else None,
        )

    async def pools(self) -> list[Pool]:
        """Pools as core ``Pool`` objects, with real Canton instrument ids.

        The instrument behind a symbol is resolved once per pool and cached:
        it is what lets the same CC be recognised across this venue and
        Cantex.
        """
        states = await self.pool_states()
        pools = []
        for state in states:
            # sequential on purpose: 13 tokens across 23 pools, so the cache
            # absorbs most of the work, and this is somebody else's mainnet API
            try:  # a new pool whose tokens will not resolve is left out, not every pool
                inst_a, inst_b = await self._instruments_for(state.token_a, state.token_b)
            except VenueRequestError as exc:
                log.warning("tradecraft: pool %s left out: %s", state.amm_id, exc)
                continue
            pools.append(Pool(contract_id=state.amm_id, token_a=inst_a, token_b=inst_b))
        return pools

    async def _instruments_for(self, token_a: str, token_b: str) -> tuple[Instrument, Instrument]:
        cached_a, cached_b = self._instruments.get(token_a), self._instruments.get(token_b)
        if cached_a is not None and cached_b is not None:
            return cached_a, cached_b
        raw_a, raw_b = await asyncio.gather(
            self._get(self._pair_path("/tokenA", token_a, token_b)),
            self._get(self._pair_path("/tokenB", token_a, token_b)),
        )
        instruments = []
        for raw in (raw_a, raw_b):
            if not isinstance(raw, dict) or "instrument_id" not in raw:
                raise VenueRequestError(f"unexpected token response: {raw!r}")
            ident = raw["instrument_id"]
            instruments.append(Instrument(admin=ident["admin"], id=ident["id"]))
        # keyed by the venue's canonical order, which these routes answer in
        # whichever way the path is written
        self._instruments[token_a], self._instruments[token_b] = instruments
        self._symbols[instruments[0]] = token_a
        self._symbols[instruments[1]] = token_b
        return instruments[0], instruments[1]

    async def _symbol_for(self, instrument: Instrument) -> str:
        """The symbol this venue's routes key on, for a Canton instrument.

        Not a formality: Canton Coin is ``CC`` on every route here and
        ``Amulet`` as an instrument id, the same mismatch Cantex has, and it
        is the only token where the two differ.
        """
        if instrument in self._symbols:
            return self._symbols[instrument]
        await self.pools()
        if instrument in self._symbols:
            return self._symbols[instrument]
        raise VenueRequestError(f"no symbol on this venue for instrument {instrument.id!r}")

    async def inspect(self, token_a: str, token_b: str) -> PoolState:
        """Live state for one pool, oriented by the ids the venue returns."""
        raw = await self._get(self._pair_path("/inspect", token_a, token_b))
        if not isinstance(raw, dict) or "token_a_id" not in raw:
            raise VenueRequestError(f"unexpected /inspect response: {raw!r}")
        fees = await self._fee_constants(raw["token_a_id"], raw["token_b_id"])
        updated = raw.get("updated_at")
        return PoolState(
            amm_id=f"TC {raw['token_a_id']}/{raw['token_b_id']} LP",
            token_a=raw["token_a_id"],
            token_b=raw["token_b_id"],
            reserve_a=_dec(raw["token_a_holdings"]),
            reserve_b=_dec(raw["token_b_holdings"]),
            lp_supply=_dec(raw["total_lp_token_supply"]),
            lp_token_name=f"TC {raw['token_a_id']}/{raw['token_b_id']} LP",
            lp_fee=fees[0],
            operator_fee=fees[1],
            unclaimed_operator_fees=(
                _dec(raw["unclaimed_operator_fees"])
                if raw.get("unclaimed_operator_fees") is not None
                else None
            ),
            updated_at=datetime.fromisoformat(updated.replace("Z", "+00:00")) if updated else None,
        )

    async def _fee_constants(self, token_a: str, token_b: str) -> tuple[Decimal, Decimal]:
        for state in self._states.values():
            if {state.token_a, state.token_b} == {token_a, token_b}:
                return state.lp_fee, state.operator_fee
        await self.pool_states()
        for state in self._states.values():
            if {state.token_a, state.token_b} == {token_a, token_b}:
                return state.lp_fee, state.operator_fee
        raise VenueRequestError(f"no pool for {token_a}/{token_b}")

    async def fees(self, token_a: str, token_b: str) -> tuple[Decimal, Decimal]:
        """(LP fee, operator fee) as fractions.

        ``/feeAmount`` already returns fractions while ``/pools`` returns
        percents for the same two constants; this route is the fraction one,
        and its schema is the one that calls them percentages.
        """
        raw = await self._get(self._pair_path("/feeAmount", token_a, token_b))
        if not isinstance(raw, dict) or "fee_amount" not in raw:
            raise VenueRequestError(f"unexpected /feeAmount response: {raw!r}")
        return _dec(raw["fee_amount"]), _dec(raw["operator_fee_amount"])

    async def _state_for(self, sell: str, buy: str) -> PoolState:
        for state in self._states.values():
            if {state.token_a, state.token_b} == {sell, buy}:
                return state
        await self.pool_states()
        for state in self._states.values():
            if {state.token_a, state.token_b} == {sell, buy}:
                return state
        raise VenueRequestError(f"no pool for {sell}/{buy}")

    async def quote(
        self,
        sell_amount: Decimal,
        sell_instrument: Instrument,
        buy_instrument: Instrument,
    ) -> Quote:
        """Price a swap of ``sell_amount`` through the venue's own route.

        Instruments are translated to the symbols the routes take — which is
        not the instrument id for Canton Coin — and ``sell_instrument`` is the
        leg being given away. The quote routes are the one place on this venue
        where path order means what it says.
        """
        return await self.quote_symbols(
            sell_amount,
            await self._symbol_for(sell_instrument),
            await self._symbol_for(buy_instrument),
            sell_instrument=sell_instrument,
            buy_instrument=buy_instrument,
        )

    async def quote_symbols(
        self,
        sell_amount: Decimal,
        sell_symbol: str,
        buy_symbol: str,
        *,
        sell_instrument: Instrument | None = None,
        buy_instrument: Instrument | None = None,
    ) -> Quote:
        """``quote()`` by symbol, for callers that have not resolved instruments."""
        if sell_amount <= 0:
            raise VenueRequestError(
                f"sell_amount must be positive, got {sell_amount}; the venue answers "
                "zero and negative amounts with 200 and a quote of zero"
            )
        state = await self._state_for(sell_symbol, buy_symbol)
        raw = await self._get(
            self._pair_path("/quoteForFixedInput", sell_symbol, buy_symbol),
            {"givingAmount": f"{sell_amount:f}"},
        )
        if not isinstance(raw, dict) or "user_gets" not in raw:
            raise VenueRequestError(f"unexpected /quoteForFixedInput response: {raw!r}")
        returned = _dec(raw["user_gets"])
        return self._as_quote(
            state,
            sell_amount,
            returned,
            sell_symbol,
            buy_symbol,
            sell_instrument,
            buy_instrument,
        )

    async def quote_for_output(
        self,
        buy_amount: Decimal,
        sell_symbol: str,
        buy_symbol: str,
    ) -> Quote:
        """Price the input needed for a fixed output."""
        if buy_amount <= 0:
            raise VenueRequestError(f"buy_amount must be positive, got {buy_amount}")
        state = await self._state_for(sell_symbol, buy_symbol)
        raw = await self._get(
            self._pair_path("/quoteForFixedOutput", sell_symbol, buy_symbol),
            {"gettingAmount": f"{buy_amount:f}"},
        )
        if not isinstance(raw, dict) or "user_gives" not in raw:
            raise VenueRequestError(f"unexpected /quoteForFixedOutput response: {raw!r}")
        return self._as_quote(state, _dec(raw["user_gives"]), buy_amount, sell_symbol, buy_symbol)

    def _as_quote(
        self,
        state: PoolState,
        sell_amount: Decimal,
        returned: Decimal,
        sell_symbol: str,
        buy_symbol: str,
        sell_instrument: Instrument | None = None,
        buy_instrument: Instrument | None = None,
    ) -> Quote:
        sell_inst = sell_instrument or self._instruments.get(sell_symbol) or Instrument(
            admin="", id=sell_symbol
        )
        buy_inst = buy_instrument or self._instruments.get(buy_symbol) or Instrument(
            admin="", id=buy_symbol
        )
        trade_price = returned / sell_amount
        spot_price = state.price(sell_symbol, buy_symbol)
        slippage = Decimal(0) if spot_price == 0 else Decimal(1) - trade_price / spot_price
        return Quote(
            sell_amount=sell_amount,
            sell_instrument=sell_inst,
            buy_instrument=buy_inst,
            returned_amount=returned,
            returned_instrument=buy_inst,
            trade_price=trade_price,
            slippage=slippage,
            fee_percentage=state.realized_fee,
            estimated_time_seconds=Decimal(0),
        )

    def quote_locally(
        self,
        state: PoolState,
        sell_amount: Decimal,
        sell_symbol: str,
        buy_symbol: str,
    ) -> Decimal:
        """Price a swap from pool state alone, without asking the venue.

        Reproduces the venue's own quotes exactly on all 23 mainnet pools
        (2026-08-11), which is what makes the fee model above a measurement
        rather than a guess.
        """
        reserve_in, reserve_out = state.reserves_for(sell_symbol, buy_symbol)
        return swap_output(reserve_in, reserve_out, sell_amount, state.total_fee)

    # === liquidity =======================================================

    async def liquidity_deposit_quote(
        self,
        token_a: str,
        token_b: str,
        amount_1: Decimal,
        amount_2: Decimal,
    ) -> LiquidityQuote:
        """Ratio-aligned deposit amounts and the LP tokens they mint.

        ``instrument_1``/``instrument_2`` in the response follow the pool's
        canonical order, not the request's, so the result is relabelled here
        by symbol before it reaches the caller.
        """
        state = await self._state_for(token_a, token_b)
        raw = await self._get(
            self._pair_path("/quoteLPDeposit", token_a, token_b),
            {"instrument1Amount": f"{amount_1:f}", "instrument2Amount": f"{amount_2:f}"},
        )
        if not isinstance(raw, dict) or "lp_tokens_to_mint" not in raw:
            raise VenueRequestError(f"unexpected /quoteLPDeposit response: {raw!r}")
        return LiquidityQuote(
            amount_a=_dec(raw["instrument_1_to_deposit"]),
            token_a=state.token_a,
            amount_b=_dec(raw["instrument_2_to_deposit"]),
            token_b=state.token_b,
            lp_tokens=_dec(raw["lp_tokens_to_mint"]),
        )

    async def liquidity_withdrawal_quote(
        self,
        token_a: str,
        token_b: str,
        lp_token_amount: Decimal,
    ) -> LiquidityQuote:
        """What burning ``lp_token_amount`` returns, labelled by symbol."""
        state = await self._state_for(token_a, token_b)
        raw = await self._get(
            self._pair_path("/quoteLPWithdrawal", token_a, token_b),
            {"lpTokenAmount": f"{lp_token_amount:f}"},
        )
        if not isinstance(raw, dict) or "amount_instrument_1" not in raw:
            raise VenueRequestError(f"unexpected /quoteLPWithdrawal response: {raw!r}")
        return LiquidityQuote(
            amount_a=_dec(raw["amount_instrument_1"]),
            token_a=state.token_a,
            amount_b=_dec(raw["amount_instrument_2"]),
            token_b=state.token_b,
        )

    # === history =========================================================

    async def pool_yield(self, token_a: str, token_b: str) -> dict[str, Decimal]:
        """APY by lookback window, keyed as the venue keys it (1h, 1d, 7d...)."""
        raw = await self._get(self._pair_path("/yield", token_a, token_b))
        if not isinstance(raw, dict) or "yield" not in raw:
            raise VenueRequestError(f"unexpected /yield response: {raw!r}")
        return {k: _dec(v) for k, v in raw["yield"].items()}

    async def pool_volume_usd(self, token_a: str, token_b: str) -> dict[str, Decimal]:
        """Traded volume in USD by lookback window."""
        raw = await self._get(self._pair_path("/volume", token_a, token_b))
        if not isinstance(raw, dict) or "volume_usd" not in raw:
            raise VenueRequestError(f"unexpected /volume response: {raw!r}")
        return {k: _dec(v) for k, v in raw["volume_usd"].items()}

    async def yield_history(
        self, token_a: str, token_b: str, window: str
    ) -> list[tuple[datetime, Decimal]]:
        """APY over time, oldest first."""
        rows = await self._history("/yield_history", token_a, token_b, window, YIELD_WINDOWS)
        return [(self._ts(r["timestamp"]), _dec(r["apy"])) for r in rows]

    async def volume_history(
        self, token_a: str, token_b: str, window: str
    ) -> list[tuple[datetime, Decimal]]:
        """USD volume over time, oldest first."""
        rows = await self._history("/volume_history", token_a, token_b, window, VOLUME_WINDOWS)
        return [(self._ts(r["timestamp"]), _dec(r["volume_usd"])) for r in rows]

    async def _history(
        self,
        route: str,
        token_a: str,
        token_b: str,
        window: str,
        allowed: tuple[str, ...],
    ) -> list[dict]:
        if window not in allowed:
            raise VenueRequestError(
                f"{route} accepts {allowed}, got {window!r}; the two history routes "
                "take different windows and the published spec understates both"
            )
        raw = await self._get(self._pair_path(route, token_a, token_b), {"window": window})
        if not isinstance(raw, dict) or not isinstance(raw.get("history"), list):
            raise VenueRequestError(f"unexpected {route} response: {raw!r}")
        return raw["history"]

    @staticmethod
    def _ts(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
