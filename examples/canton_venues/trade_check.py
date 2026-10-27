"""Was a trade well executed? A trade made on Canton, against what every venue returned for it then.

Give what was sold and bought, the amounts, the time and (optionally) the venue. The trade is priced
again on the market archive's readings either side of that time (``market_archive.around``: the
pools' reserves, the order books and the network fee table of those ticks), by the code that prices
a live quote (``model.venue_quotes``). The answer says which venue returned the most for the same
amount, after its network fee, and how far the trade's own output fell short of it, at the reading
least favourable to that claim: a pool that moved between our reading and the trade moves the two
readings apart, and the shortfall is the smaller of the two.

Nothing is executed and nothing is stored about the trade. Served by the MCP server as the
``check_trade`` tool and at ``/api/v1/check`` (mcp_server.py).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import market_archive
import model as m

MAX_GAP_S = 600  # a reading within ten minutes either side of the trade
METHOD = ("Priced again from the pool reserves and order books Canton Venues read at the ticks either "
          "side of the trade (every five minutes), by the same code as the live quote: each venue's "
          "best way on its own markets in one swap or two, after pool fees, book fees and price "
          "impact; after_network_fee also takes off each swap's network fee as that tick's fee "
          "table had it (measured, documented or assumed: network_fees). A pool marked not trading "
          "at that tick is no market. The shortfall is measured at the reading least favourable to "
          "it. If your venue took its network fee from what you received, compare with "
          "after_network_fee; if you paid it separately, compare with amount_out.")


class CheckError(ValueError):
    """A trade that cannot be checked: the reason is the message, for the caller as is."""


def parse_time(x: str | float) -> int:
    """Unix seconds, or an ISO 8601 time (``2026-10-27T18:05:00Z``; no zone means UTC)."""
    if isinstance(x, int | float):
        return int(x)
    s = str(x).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        raise CheckError(f"time {x!r}: give unix seconds or an ISO time such as 2026-10-27T18:05:00Z") from None
    return int((d if d.tzinfo else d.replace(tzinfo=UTC)).timestamp())


def _iso(t: int) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _f(x: Decimal | None, n: int = 10) -> float | None:
    return None if x is None else round(float(x), n)


def _bps(a: Decimal, b: Decimal) -> float:
    """How much more ``a`` is than ``b``, in basis points of ``b``."""
    return round(float((a / b - 1) * 10_000), 1)


def _price(r: dict, k: str) -> Decimal | None:
    if k == m.CC:
        return Decimal(str(r["cc_usd"]))
    px = (r.get("tokens", {}).get(k) or [None, None])[1]
    return Decimal(str(px)) if px else None


def _priced(r: dict, s: str, b: str, amount: Decimal) -> dict | None:
    """One reading's answer: every venue's output for ``amount`` and the best after network fees, or
    None when no venue's markets could fill it."""
    with m.fees_from(r.get("fees")):
        quotes = m.venue_quotes(r, s, b, amount, _price(r, b))
    rows, net = {}, {}
    for v, (got, path, fee) in quotes.items():
        if got <= 0:
            rows[v] = {"amount_out": None, "note": "too thin for this size"}
            continue
        net[v] = got - fee if fee is not None else got
        rows[v] = {"amount_out": _f(got), "after_network_fee": _f(net[v]) if fee is not None else None,
                   "swaps": len(path), "network_fee_basis": (r.get("fees", {}).get(v) or {}).get("basis")}
    if not net:
        return None
    best = max(net, key=net.get)
    return {"t": r["t"], "time": _iso(r["t"]), "best_venue": best, "best_net": net[best], "net": net,
            "by_venue": rows, "not_trading": r.get("not_trading") or []}


def check(directory: Path, sell: str, buy: str, amount_in: float | str, amount_out: float | str,
          when: str | float, venue: str | None = None) -> dict:
    """The trade against every venue at the readings around ``when``. Raises ``CheckError`` with a
    reason a caller can show as is."""
    s, b = m.key(sell), m.key(buy)
    if s == b:
        raise CheckError("sell and buy are the same token")
    try:
        a_in, a_out = Decimal(str(amount_in)), Decimal(str(amount_out))
    except ArithmeticError:
        raise CheckError("amount_in and amount_out must be numbers") from None
    if not (a_in > 0 and a_out > 0):
        raise CheckError("amount_in and amount_out must be positive")
    t = parse_time(when)
    v = venue.strip().lower().replace(" ", "") if venue else None
    reads = [r for r in market_archive.around(directory, t, MAX_GAP_S) if r]
    if not reads:
        start = market_archive.first(directory)
        since = f"; the record starts {_iso(start)}" if start else "; the record is empty"
        raise CheckError(f"no market reading within ten minutes of {_iso(t)}{since}")
    priced = [p for p in (_priced(r, s, b, a_in) for r in reads) if p]
    if not priced:
        raise CheckError(f"no venue we read could sell {a_in} {sell} for {buy} in one or two swaps "
                         f"at {_iso(t)}")
    symbols = {k: x[0] for k, x in (reads[0].get("tokens") or {}).items()} | {m.CC: "CC"}
    low = min(priced, key=lambda p: p["best_net"])  # least favourable to "you could have got more"
    high = max(priced, key=lambda p: p["best_net"])
    gap = _bps(low["best_net"], a_out)
    better = sorted(x for x, n in low["net"].items() if n > a_out)
    out = {
        "trade": {"sell": symbols.get(s, sell), "buy": symbols.get(b, buy), "amount_in": _f(a_in),
                  "amount_out": _f(a_out), "time": _iso(t), "venue": v},
        "best_venue": low["best_venue"],
        "best_after_network_fee": _f(low["best_net"]),
        "shortfall_bps": gap if gap > 0 else 0.0,
        "shortfall_bps_range": sorted({max(gap, 0.0), max(_bps(high["best_net"], a_out), 0.0)}),
        "venues_that_returned_more": better,
        "readings": [{"time": p["time"], "minutes_from_trade": round((p["t"] - t) / 60, 1),
                      "best_venue": p["best_venue"], "best_after_network_fee": _f(p["best_net"]),
                      "by_venue": p["by_venue"], "not_trading": p["not_trading"]} for p in priced],
    }
    if v is not None:
        mine = [p["net"].get(v) for p in priced]
        if all(x is None for x in mine):
            out["your_venue"] = {"note": f"{v} could not fill this trade at these readings, or is not a venue we read"}
        else:
            got = min(x for x in mine if x is not None)
            out["your_venue"] = {
                "after_network_fee": _f(got),
                # what choosing the best venue would have added, and what your fill lost against your
                # own venue's market (price moved, slippage, a fee we do not see), both in bps
                "venue_choice_bps": max(_bps(low["best_net"], got), 0.0),
                "fill_vs_your_venue_bps": _bps(got, a_out),
            }
    if gap > 0:
        out["verdict"] = (f"{len(better)} venue{'s' if len(better) != 1 else ''} returned more than you received. "
                          f"{low['best_venue']} returned {gap / 100:.2f}% more after its network fee "
                          f"(at the reading least favourable to that).")
    else:
        out["verdict"] = "You received at least as much as the best venue we read returned after its network fee."
    out["method"] = METHOD
    out["network_fees"] = reads[0].get("fees")
    return out
