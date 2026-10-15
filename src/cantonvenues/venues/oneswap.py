"""OneSwap public market data — pools and reserves, no key.

OneSwap (Sats Terminal) runs constant-product pools on Canton with atomic DvP
settlement. ``GET /api/rt/pools`` and ``/api/rt/tokens`` are public; quotes
(``POST /api/rt/pool/{id}/quote``) and swaps need an ``sk_live_`` SDK key and
are not used here. Shapes were verified against ``https://api.oneswap.cc/swapv2``
on 2026-10-03; see SOURCES.md.

Venue-shape notes:

- Each pool lists ``assetX``/``assetY`` with the instrument admin and id, so
  pools match other venues' tokens exactly, by issuer.
- ``feeBps`` is the pool fee (30 on every pool on 2026-10-03; the docs call it
  "the pool's 0.30% swap fee"). On top, a per-swap network fee is carved from
  the input, "typically around $1.5–2" per the docs. It is not in the pool
  data and is NOT included in ``ReservePool.output``.
- Reserves come twice: as JSON numbers at the top level and as decimal strings
  under ``accounting``. The strings are used, so nothing is lost to floats.
- The docs do not spell out the curve; constant product with the fee on the
  input is assumed, which is what "price derived from the pool's own reserve
  ratio" and the quote's ``newReserveX``/``newReserveY`` fields suggest.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import httpx

from ..core.models import Instrument, to_decimal
from ..core.venue import VenueRequestError
from .reserves import ReservePool

log = logging.getLogger("cantonvenues")

BASE_URL = "https://api.oneswap.cc/swapv2"
DOCS_FEE = "pool feeBps; docs.oneswap.cc"


class OneSwapPublicData:
    """Read-only OneSwap pools."""

    def __init__(self, *, base_url: str = BASE_URL, client: httpx.AsyncClient | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._client = client

    async def __aenter__(self) -> OneSwapPublicData:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _get(self, path: str) -> object:
        if self._client is None:
            raise VenueRequestError("not connected; call connect() first")
        try:
            resp = await self._client.get(f"{self._base}{path}")
        except httpx.HTTPError as exc:
            raise VenueRequestError(f"GET {path}: {exc}") from exc
        if resp.status_code >= 400:
            raise VenueRequestError(f"GET {path}: HTTP {resp.status_code}: {resp.text.strip()[:200]}")
        return resp.json()

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        await self.pools()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def pools(self) -> list[ReservePool]:
        raw = await self._get("/api/rt/pools")
        if not isinstance(raw, list):
            raise VenueRequestError(f"unexpected /api/rt/pools response: {raw!r}")
        out = []
        for p in raw:
            if not p.get("swapsEnabled", True) or not p.get("visible", True):
                continue
            try:  # one malformed row is skipped, not the whole venue (a null reserve or fee, 2026-10-14)
                pool = self._pool(p)
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                log.warning("oneswap: pool row skipped: %r", exc)
                continue
            # a pool listed before it is funded, or drained on one side, has no price
            if pool.reserve_a > 0 and pool.reserve_b > 0:
                out.append(pool)
        return out

    @staticmethod
    def _pool(p: dict) -> ReservePool:
        acct = p.get("accounting") or {}
        x, y = p["assetX"], p["assetY"]
        return ReservePool(
            venue="oneswap",
            pool_id=p["id"],
            symbol_a=x["symbol"],
            symbol_b=y["symbol"],
            token_a=Instrument(admin=x["admin"], id=x["id"]),
            token_b=Instrument(admin=y["admin"], id=y["id"]),
            reserve_a=to_decimal(acct.get("reserveX", p["reserveX"])),
            reserve_b=to_decimal(acct.get("reserveY", p["reserveY"])),
            fee=Decimal(p["feeBps"]) / 10_000,
            fee_source=DOCS_FEE,
        )
