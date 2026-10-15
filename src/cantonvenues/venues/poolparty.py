"""Pool Party public market data — reserves and volume, no key.

Pool Party is the Send Foundation's AMM on Canton, reached through the Send
Canton Wallet. Its public API has two routes, ``/tvl`` (per-pool reserves) and
``/volume`` (per-pool volume in token units); there is no quote, trades or
token-metadata route. Shapes were verified against
``https://api-mainnet.cantonwallet.com/canton/pool-party/public/v1`` on
2026-10-03; see SOURCES.md.

Venue-shape notes:

- Pools are named ``"<idA>-<idB>"`` by instrument id (``Amulet`` is CC; one
  token appears as a bare UUID). Ids can contain dashes, so pools are split
  using the reserve keys the route returns for each pool, not the name.
- No issuer (admin party) is published, so ``token_a``/``token_b`` carry
  ``admin=""``. Matching to other venues falls back to the instrument id.
- Pool Party publishes no fee or curve documentation. The 0.30% fee comes from
  CCTools' normalised pool feed (``feeRate: 0.003`` on every Send pool), a third
  party; constant product with the fee on the input is assumed.
- Empty pools (both reserves ``"0"``) are listed and are skipped here.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import httpx

from ..core.models import Instrument, to_decimal
from ..core.venue import VenueRequestError
from .reserves import ReservePool

log = logging.getLogger("cantonvenues")

BASE_URL = "https://api-mainnet.cantonwallet.com/canton/pool-party/public/v1"
ASSUMED_FEE = Decimal("0.003")
FEE_SOURCE = "CCTools feeRate (third party); Pool Party publishes none"


class PoolPartyPublicData:
    """Read-only Pool Party reserves and volume."""

    def __init__(self, *, base_url: str = BASE_URL, client: httpx.AsyncClient | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._client = client

    async def __aenter__(self) -> PoolPartyPublicData:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        if self._client is None:
            raise VenueRequestError("not connected; call connect() first")
        try:
            resp = await self._client.get(f"{self._base}{path}", params=params)
        except httpx.HTTPError as exc:
            raise VenueRequestError(f"GET {path}: {exc}") from exc
        if resp.status_code >= 400:
            raise VenueRequestError(f"GET {path}: HTTP {resp.status_code}: {resp.text.strip()[:200]}")
        body = resp.json()
        if not isinstance(body, dict):
            raise VenueRequestError(f"unexpected {path} response: {body!r}")
        return body

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        await self.pools()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def pools(self) -> list[ReservePool]:
        raw = await self._get("/tvl")
        out = []
        for name, reserves in (raw.get("pools") or {}).items():
            if not isinstance(reserves, dict) or len(reserves) != 2:
                continue
            (a, ra), (b, rb) = sorted(reserves.items(), key=lambda kv: name.find(kv[0]))
            try:  # one malformed row is skipped, not the whole venue
                ra, rb = to_decimal(ra), to_decimal(rb)
            except ArithmeticError as exc:
                log.warning("poolparty: pool %s skipped: %r", name, exc)
                continue
            if ra <= 0 or rb <= 0:
                continue
            out.append(ReservePool(
                venue="poolparty", pool_id=name, symbol_a=a, symbol_b=b,
                token_a=Instrument(admin="", id=a), token_b=Instrument(admin="", id=b),
                reserve_a=ra, reserve_b=rb, fee=ASSUMED_FEE, fee_source=FEE_SOURCE,
            ))
        return out

    async def volume(self, period: str = "24h") -> dict[str, dict[str, Decimal]]:
        """Per pool, volume in each token's own units over ``period`` (24h or 7d)."""
        raw = await self._get("/volume", {"period": period})
        out: dict[str, dict[str, Decimal]] = {}
        for name, p in (raw.get("perPool") or {}).items():
            try:  # a pool whose volume will not parse is left out, not every pool (a null, 2026-10-14)
                out[name] = {tok: to_decimal(v) for tok, v in ((p or {}).get("volume") or {}).items()}
            except (ArithmeticError, AttributeError) as exc:
                log.warning("poolparty: volume of %s skipped: %r", name, exc)
        return out
