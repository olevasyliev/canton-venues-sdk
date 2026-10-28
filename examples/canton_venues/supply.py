"""How much of a token exists, from its own registry, to tell an order book that shows more than that.

Every CIP-56 token admin publishes its instruments' metadata, ``totalSupply`` among it. On 2026-10-28
Rocky's CBTC-USDCX book offered 237 BTC and bid $18.5M in USDCx while CBTC's registry said 40.8 CBTC
existed and USDCx's said $7.0M: 146 ticks in a row never fell under 218 BTC a side, and its largest
trade in seven days was 2.9 BTC. A side like that says nothing about what can be sold, so it is left
out of depth and exit cost; the book's price still counts (``collect.py``, ``exit_cost.py``).
"""

from __future__ import annotations

from decimal import Decimal

# Digital Asset's Utilities registry answers for the admins it hosts (CBTC, USDCx, USDM1, eXAU, eXAG
# on 2026-10-28); one it does not host (CC's DSO) gives no supply, and its books are not judged
REGISTRY = ("https://api.utilities.digitalasset.com/api/token-standard/v0/registrars/{admin}"
            "/registry/metadata/v1/instruments")
SIDES = ("bids", "asks")


def parse(raw: dict, instrument_id: str) -> float | None:
    """One instrument's total supply from a registry's instrument list; None when it is not listed,
    gives none, or gives zero (a supply of nothing would judge every book)."""
    for inst in (raw or {}).get("instruments") or []:
        if isinstance(inst, dict) and inst.get("id") == instrument_id:
            try:
                total = float(inst.get("totalSupply"))
            except (TypeError, ValueError):
                return None
            return total if total > 0 else None
    return None


async def read(get_json, tokens: list[tuple[str, str, str]]) -> dict[str, float]:
    """{key: total supply} for each (key, admin, instrument id), one request per admin; an admin whose
    registry does not answer leaves its tokens without a supply."""
    by_admin: dict[str, list[tuple[str, str]]] = {}
    for key, admin, inst in tokens:
        by_admin.setdefault(admin, []).append((key, inst))
    out: dict[str, float] = {}
    for admin, wanted in by_admin.items():
        try:
            raw = await get_json(REGISTRY.format(admin=admin))
        except Exception:  # noqa: BLE001  (a registry we cannot read judges nothing)
            continue
        for key, inst in wanted:
            if (total := parse(raw, inst)) is not None:
                out[key] = total
    return out


def over_supply(bids, asks, base_supply: float | None, quote_supply: float | None) -> list[str]:
    """The sides of a book that show more than exists: asks offering more base than its supply, bids
    offering more quote (price times size) than its supply. A side whose token has no known supply is
    not judged. Levels are (price, size) pairs or objects with ``price`` and ``size``."""
    def pair(lv):
        return (lv.price, lv.size) if hasattr(lv, "price") else (Decimal(str(lv[0])), Decimal(str(lv[1])))
    out = []
    if quote_supply is not None and float(sum((p * q for p, q in map(pair, bids)), Decimal(0))) > quote_supply:
        out.append("bids")
    if base_supply is not None and float(sum((q for _, q in map(pair, asks)), Decimal(0))) > base_supply:
        out.append("asks")
    return out
