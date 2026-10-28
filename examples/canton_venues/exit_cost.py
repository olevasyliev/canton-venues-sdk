"""What selling a token for dollars on Canton nets right now, at any size (``api/v1/exit.json``).

For every token with a dollar price, selling it for USDCx on each venue that can (its own markets,
one swap or two, as best execution compares them), after pool fees, book fees, price impact and the
venue's network fee (``model.venue_quotes``). Every figure is a share of the sale's value at our
price for the token (tokens.json, liquidity-weighted across venues): 0.98 means 2% lost to costs.

- ``ladder``: the share each size from $1K to $1M keeps, per venue and at the best one.
- ``capacity``: the largest sale each venue takes within 1%, 2% and 5% of value; ``all_venues`` sums
  them (each venue sold on its own to that cost; a seller splitting a sale across venues gets about
  the sum, a little more since each piece is smaller). ``limited_by`` says what stopped each venue:
  the cost reached the band, or its order book as the venue's API shows it ran out first (Temple's
  API shows a limited number of levels; the venue may hold more).
- ``worst``: the lowest share the best venue kept at $10K and $50K at any tick since ``worst_since``,
  and when, from ticks that read the deep venues; the record lives in the state directory
  (``WorstRecord``).

A pool marked not trading is no market here, nor is an order book with a side that shows more of a
token than its own registry says exists (``over_supply`` in pools.json, supply.py): what it can fill
is unknown, so it is listed in ``left_out`` with its supplies instead of being counted. Read by lending,
vault and risk tools that need to know what collateral sells for on Canton, not only what it is quoted at.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path

import model as m

DOLLAR = "USDCX"
LADDER_USD = (1_000, 10_000, 50_000, 100_000, 250_000, 500_000, 1_000_000)
BANDS = (Decimal("0.01"), Decimal("0.02"), Decimal("0.05"))
WORST_AT_USD = (10_000, 50_000)
GRID = [10 * 10 ** (k / 4) for k in range(29)]  # $10 to $100M, four steps a decade
BISECT = 12  # halvings between the last grid size inside a band and the first outside: ~1/4000 of the step


def _share(quotes: tuple, amount: Decimal, price: Decimal, dollar_usd: Decimal) -> Decimal | None:
    got, _, fee = quotes
    if got <= 0:
        return None
    net = got - fee if fee is not None else got
    return max(net, Decimal(0)) * dollar_usd / (amount * price)


def _capacity(share, band: Decimal) -> tuple[float, str]:
    """(the largest size in dollars whose share stays at or above 1 - ``band``, what stopped it): the
    grid finds the last size inside the band on the falling side (a small sale loses the fixed network
    fee, so the share first rises with size), then bisection between it and the next grid size. What
    stopped it is ``band`` (the cost passed the band), ``book`` (the order book as the venue's API
    shows it ran out first: the venue may hold more than it shows) or ``grid`` (still inside at the
    largest size tried)."""
    floor = 1 - band
    inside = [x for x in GRID if (s := share(x)) is not None and s >= floor]
    if not inside:
        return 0.0, "band"
    lo = inside[-1]
    i = GRID.index(lo)
    if i == len(GRID) - 1:
        return lo, "grid"
    hi = GRID[i + 1]
    for _ in range(BISECT):
        mid = math.sqrt(lo * hi)
        s = share(mid)
        if s is not None and s >= floor:
            lo = mid
        else:
            hi = mid
    return lo, ("book" if share(hi) is None else "band")


def token_exit(raw: dict, hops: dict, k: str, symbol: str, price: Decimal, dollar_usd: Decimal) -> dict | None:
    """One token's row, or None when no venue sells it for dollars in one or two swaps. ``hops`` is
    ``model.venue_hops(raw)``, built once a tick."""
    venues = sorted(m.venue_quotes(raw, k, DOLLAR, Decimal(1_000) / price, dollar_usd, hops))
    if not venues:
        return None
    ladder = []
    for usd in LADDER_USD:
        amount = Decimal(usd) / price
        quotes = m.venue_quotes(raw, k, DOLLAR, amount, dollar_usd, hops)
        per = {v: _share(q, amount, price, dollar_usd) for v, q in quotes.items()}
        filled = {v: s for v, s in per.items() if s is not None}
        best = max(filled, key=filled.get) if filled else None
        ladder.append({"usd": usd, "best_venue": best, "best": _r(filled.get(best)),
                       "by_venue": {v: _r(s) for v, s in per.items()}})
    capacity = {f"{int(b * 100)}pct": {"by_venue": {}, "limited_by": {}} for b in BANDS}
    for v in venues:
        seen: dict[float, Decimal | None] = {}

        def share(usd: float, v=v, seen=seen) -> Decimal | None:
            if usd not in seen:
                amount = Decimal(str(usd)) / price
                q = m.venue_quotes(raw, k, DOLLAR, amount, dollar_usd, {v: hops[v]}).get(v)
                seen[usd] = None if q is None else _share(q, amount, price, dollar_usd)
            return seen[usd]
        for b in BANDS:
            usd, why = _capacity(share, b)
            capacity[f"{int(b * 100)}pct"]["by_venue"][v] = round(usd, 2)
            capacity[f"{int(b * 100)}pct"]["limited_by"][v] = why
    for c in capacity.values():
        c["all_venues"] = round(sum(c["by_venue"].values()), 2)
    return {"key": k, "symbol": symbol, "price_usd": float(price), "venues": venues, "ladder": ladder,
            "capacity_usd": capacity}


def _r(x: Decimal | None) -> float | None:
    return None if x is None else round(float(x), 6)


def backed(raw: dict) -> tuple[dict, list[dict]]:
    """pools.json without the order books that show more than exists on a side, and those books."""
    kept, left_out = {}, []
    for base, by_market in (raw.get("books") or {}).items():
        for name, d in by_market.items():
            if d.get("over_supply"):
                left_out.append({"token": base, "venue": d["venue"], "market": name,
                                 "sides": d["over_supply"], "supply": d.get("supply") or {}})
            else:
                kept.setdefault(base, {})[name] = d
    return {**raw, "books": kept}, left_out


def build(raw: dict, tokens: list[dict], now: int) -> dict:
    """exit.json for this tick (``raw`` is pools.json as written, ``tokens`` tokens.json's list)."""
    prices = {t["key"]: t for t in tokens if t.get("price_usd")}
    dollar_usd = Decimal(str((prices.get(DOLLAR) or {}).get("price_usd") or 1))
    raw, left_out = backed(raw)
    rows, hops = [], m.venue_hops(raw)
    cc = {"key": m.CC, "symbol": "CC", "price_usd": raw["cc_usd"]}
    for t in [cc] + [t for t in tokens if t["key"] != DOLLAR]:
        if not t.get("price_usd"):
            continue
        row = token_exit(raw, hops, t["key"], t["symbol"], Decimal(str(t["price_usd"])), dollar_usd)
        if row:
            rows.append(row)
    rows.sort(key=lambda x: -x["capacity_usd"]["2pct"]["all_venues"])
    return {"t": now, "sell_for": "USDCx", "ladder_usd": list(LADDER_USD),
            "bands": [f"{int(b * 100)}pct" for b in BANDS], "tokens": rows,
            "not_trading": raw.get("not_trading") or [], "left_out": left_out}


class WorstRecord:
    """The lowest share the best venue kept at each size in ``WORST_AT_USD``, per token, since the
    record began: one small JSON file in the state directory, updated from ticks that read the venues
    holding most of the depth (the collector decides which: a venue that was not read would make the
    market look thinner than it was)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def update(self, exit_doc: dict) -> None:
        now = exit_doc["t"]
        self.data.setdefault("since", now)
        worst = self.data.setdefault("tokens", {})
        for row in exit_doc["tokens"]:
            for step in row["ladder"]:
                if step["usd"] not in WORST_AT_USD or step["best"] is None:
                    continue
                key = str(step["usd"])
                have = worst.setdefault(row["key"], {}).get(key)
                if have is None or step["best"] < have["best"]:
                    worst[row["key"]][key] = {"best": step["best"], "venue": step["best_venue"], "t": now}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, separators=(",", ":")))
        tmp.replace(self.path)

    def attach(self, exit_doc: dict) -> dict:
        """exit.json with each token's worst so far and the record's start."""
        worst = self.data.get("tokens") or {}
        for row in exit_doc["tokens"]:
            row["worst"] = worst.get(row["key"]) or {}
        exit_doc["worst_since"] = self.data.get("since")
        return exit_doc
