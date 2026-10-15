"""Canton Venues MCP server: Canton DEX market data for AI agents.

Read-only. Every tool answers from the JSON the collector publishes, so the
numbers match https://cantonvenues.com exactly. Quotes for any amount are
priced from live pool reserves with each venue's own formula.

Two ways to run it:

    # remote, as hosted at https://cantonvenues.com/mcp
    python mcp_server.py --http --port 8097

    # local stdio, reading the public API over HTTPS
    python mcp_server.py

Data source: ``VENUES_API_DIR`` (the collector's output directory) when set,
otherwise ``VENUES_API_URL`` (default https://cantonvenues.com/api/v1).
"""

from __future__ import annotations

import argparse
import json
import os
import time
from decimal import Decimal
from pathlib import Path

import httpx
import model as m
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

API_DIR = os.getenv("VENUES_API_DIR")
API_URL = os.getenv("VENUES_API_URL", "https://cantonvenues.com/api/v1").rstrip("/")
CACHE_S = 30
SITE = "https://cantonvenues.com"

_cache: dict[str, tuple[float, object]] = {}


def load(name: str):
    """One API file, cached for half a minute (the collector writes every five)."""
    hit = _cache.get(name)
    if hit and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    if API_DIR:
        data = json.loads((Path(API_DIR) / f"{name}.json").read_text())
    else:
        data = httpx.get(f"{API_URL}/{name}.json", timeout=20).json()
    _cache[name] = (time.monotonic(), data)
    return data


def _r(x, n=6):
    return None if x is None else round(float(x), n)


def _books() -> tuple[dict, float]:
    raw = load("pools")
    books = {k: {v: m.pool_from_json(d) for v, d in vs.items()} for k, vs in raw["pools"].items()}
    return books, raw["cc_usd"]


def _token_row(key: str) -> dict | None:
    return next((t for t in load("tokens")["tokens"] if t["key"] == m.key(key)), None)


def _premium_row(key: str) -> dict | None:
    return next((a for a in load("premium")["assets"] if a["key"] == m.key(key) and a["status"] == "ok"), None)


server = MCPServer(
    "canton-venues",
    title="Canton Venues",
    description="Live market data for Canton Network DEXes (Temple, Cantex, Tradecraft, Rocky, OneSwap, Pool Party, Ekiden): prices, "
                "premium to global markets, best execution, spreads, perps, alerts.",
    instructions=(
        "Market data for Canton Network DEXes, refreshed every five minutes. Prices are in USD. "
        "Tokens are named by symbol (CC is Canton Coin). Use quote() for any trade size: every venue "
        "is priced on its own markets (pools from live reserves, the Temple and Rocky order books), in one "
        "swap or two, as the site's best execution compares them; each venue's output comes before and "
        "after its per-trade network fee (network_fees). Nothing here executes a trade."
    ),
    website_url=SITE,
    version="1.0.0",
)


@server.tool(title="Canton DEX market overview")
def market_overview() -> dict:
    """Canton DEX market right now: CC price, spot volume per venue we read, Canton's TVL for the day
    named, top movers, the largest premiums to global prices, and the latest alerts."""
    s = load("summary")
    v = load("venues")
    eco = s.get("ecosystem") or {}
    toks = [t for t in load("tokens")["tokens"] if (t.get("liquidity_usd") or 0) >= 20_000
            and t["key"] not in m.STABLES and t.get("change_24h") is not None]
    toks.sort(key=lambda t: t["change_24h"], reverse=True)
    prem = [a for a in load("premium")["assets"] if a["status"] == "ok"]
    prem.sort(key=lambda a: -abs(a["premium"]))
    feed = load("alerts").get("feed", [])[-5:]
    return {
        "as_of": s["t"],
        "cc_usd": s["cc_usd"],
        "cc_change_24h": s.get("cc_change_24h"),
        "cc_global_usd": s.get("cc_global_usd"),
        # our own reading of every venue's last 24 hours, the same figures as venues.json
        "spot_volume_24h_usd": v.get("spot_volume_24h_usd"),
        "spot_volume_change_24h": v.get("spot_volume_change_24h"),
        "spot_volume_by_venue_24h_usd": {x["name"]: x["volume_24h_usd"] for x in v.get("venues", [])
                                         if x["kind"] != "Perpetuals" and x.get("volume_24h_usd")},
        "canton_defi_tvl_usd": eco.get("tvl"),
        "canton_defi_tvl_day": eco.get("tvl_day"),
        "cantex_swaps_24h": (s.get("cantex_24h") or {}).get("swaps"),
        "top_gainers_24h": [{"symbol": t["symbol"], "change": t["change_24h"]} for t in toks[:5]],
        "top_losers_24h": [{"symbol": t["symbol"], "change": t["change_24h"]} for t in toks[::-1][:5]],
        "largest_premiums": [{"symbol": a["symbol"], "vs": a["reference"], "premium": a["premium"]} for a in prem[:5]],
        "latest_alerts": [a["text"] for a in feed],
        "source": SITE,
    }


@server.tool(title="List Canton DEX tokens")
def list_tokens(sort_by: str = "liquidity", limit: int = 30) -> dict:
    """Every token on Canton DEXes with price, 24h/7d change, liquidity and volume.
    sort_by: liquidity, volume, change_24h, change_7d or premium."""
    keys = {"liquidity": "liquidity_usd", "volume": "volume_24h_usd", "change_24h": "change_24h",
            "change_7d": "change_7d", "premium": "premium"}
    if sort_by not in keys:
        raise ToolError(f"sort_by must be one of {sorted(keys)}")
    prem = {a["key"]: a["premium"] for a in load("premium")["assets"] if a["status"] == "ok"}
    rows = [{"symbol": t["symbol"], "price_usd": t["price_usd"], "change_24h": t["change_24h"],
             "change_7d": t["change_7d"], "liquidity_usd": t["liquidity_usd"],
             "volume_24h_usd": t["volume_24h_usd"], "venues": sorted(t["venues"]),
             "premium": prem.get(t["key"])} for t in load("tokens")["tokens"]]
    k = keys[sort_by]
    rows.sort(key=lambda r: (r[k] is None, -(abs(r[k]) if k == "premium" and r[k] is not None else (r[k] or 0))))
    return {"as_of": load("tokens")["t"], "tokens": rows[:max(1, min(limit, 100))]}


@server.tool(title="Token detail")
def token(symbol: str) -> dict:
    """One token: price, changes, liquidity and volume, price on each venue, premium to its outside
    reference, its pools with fee APR, and hourly USD prices for the last 7 days."""
    t = _token_row(symbol)
    if t is None:
        raise ToolError(f"unknown token {symbol!r}; call list_tokens() for symbols")
    p = _premium_row(symbol)
    pools = [x for x in load("lp")["pools"] if m.key(x["pair"]) == m.key(f"CC/{t['symbol']}")]
    return {
        "symbol": t["symbol"], "price_usd": t["price_usd"], "change_24h": t["change_24h"],
        "change_7d": t["change_7d"], "liquidity_usd": t["liquidity_usd"], "volume_24h_usd": t["volume_24h_usd"],
        "by_venue": t["venues"],
        "premium": None if p is None else {"vs": p["reference"], "premium": p["premium"],
                                           "by_venue": p.get("premium_by_venue"),
                                           "outside_price_usd": p.get("reference_usd")},
        "pools": pools,
        "price_history_7d": t.get("spark_7d"),
        "history_step": "1h",  # own five-minute samples are kept hourly too, so both cover 7 days
        "page": f"{SITE}/#t/{t['key']}",
    }


@server.tool(title="Quote a trade on Canton DEXes")
def quote(sell: str, buy: str, amount: float | None = None, amount_usd: float | None = None) -> dict:
    """Best execution for selling `amount` of token `sell` (or `amount_usd` worth) for token `buy`, on
    every venue that can make the trade on its own markets in one or two swaps: an AMM pool (Cantex,
    Tradecraft, OneSwap, Pool Party), an order book (Temple, Rocky), or two of them in a row on the same
    venue (Temple: CC to USDCx, then USDCx to CBTC; Pool Party: CC to USDC.B, then USDC.B to CBTC). The
    same comparison as the site's best execution, at any size. Pool fees, book fees and price impact are
    included; `after_network_fee` also takes off the venue's per-trade network fee on each swap (the fee
    table, with which fees are measured, documented or assumed, is in `network_fees`). A pool whose
    reserves have stood still while its price left the market is no market here (`not_trading`)."""
    raw = load("pools")
    cc_usd = Decimal(str(raw["cc_usd"]))
    if (amount is None) == (amount_usd is None):
        raise ToolError("give exactly one of amount or amount_usd")
    s, b = m.key(sell), m.key(buy)
    if s == b:
        raise ToolError("sell and buy are the same token")

    def usd(k: str, name: str) -> Decimal | None:
        px = cc_usd if k == m.CC else (_token_row(name) or {}).get("price_usd")
        return Decimal(str(px)) if px else None

    if amount is None:
        price = usd(s, sell)
        if not price:
            raise ToolError(f"no USD price for {sell!r}; call list_tokens() for symbols")
        amount = float(Decimal(str(amount_usd)) / price)
    names = {t["key"]: t["symbol"] for t in load("tokens")["tokens"]} | {m.CC: "CC"}
    out_usd = usd(b, buy)
    by_venue = {}
    for v, hops in sorted(m.venue_hops(raw).items()):
        paths = m.venue_paths(hops, s, b, m.MIN_MARKET_USD)
        if not paths:
            continue
        fee = m.network_fee_usd(v, cc_usd) / out_usd if out_usd else Decimal(0)
        got, path = m.Route(v, names.get(b, b), s, b, tuple(paths)).best(True, Decimal(str(amount)), fee)
        if got <= 0:
            by_venue[v] = {"amount_out": None, "note": "too thin for this size"}
            continue
        via = m.via(path)
        by_venue[v] = {"amount_out": _r(got, 10), "swaps": len(path),
                       **({"via": names.get(via, via)} if via else {}),
                       "markets": [h.market or f"{names.get(h.a, h.a)}/{names.get(h.b, h.b)} pool" for h in path],
                       "after_network_fee": _r(got - len(path) * fee, 10) if out_usd else None}
    if not by_venue:
        raise ToolError(f"no venue trades {sell!r} for {buy!r} in one or two swaps; call list_tokens() for symbols")
    filled = {v: q for v, q in by_venue.items() if q["amount_out"]}
    key = (lambda v: filled[v]["after_network_fee"]) if out_usd else (lambda v: filled[v]["amount_out"])
    best = max(filled, key=key) if filled else None
    return {"sell": names.get(s, sell), "buy": names.get(b, buy), "amount_in": _r(amount, 10),
            "best_venue": best, "amount_out": filled[best]["amount_out"] if best else None,
            "by_venue": by_venue, "as_of": raw["t"],
            "not_trading": raw.get("not_trading") or [],
            "network_fees": {"included": "after_network_fee, per swap", "per_trade": m.fee_table_json()}}


@server.tool(title="Canton premium to global prices")
def premiums(kind: str = "all") -> dict:
    """How far Canton DEX prices sit from outside markets: CBTC vs BTC, cETH vs ETH, eXAU and eXAG vs
    gold and silver spot (while the spot market trades), CC vs global CC, and every stablecoin vs $1.
    kind: all, reference or peg."""
    rows = [a for a in load("premium")["assets"] if a["status"] == "ok" and (kind == "all" or a["kind"] == kind)]
    rows.sort(key=lambda a: -abs(a["premium"]))
    return {"as_of": load("premium")["t"], "assets": [
        {"symbol": a["symbol"], "kind": a["kind"], "vs": a["reference"], "premium": a["premium"],
         "canton_usd": a["canton_usd"], "outside_usd": a["reference_usd"], "by_venue": a.get("premium_by_venue")}
        for a in rows]}


@server.tool(title="Cross-venue spreads")
def spreads(limit: int = 10) -> dict:
    """Buy-on-one-venue, sell-on-the-other round trips at their best size, after each venue's network
    fee: `cost_cc` per route, `swap_cost_cc` per venue (Cantex measured, OneSwap and Tradecraft
    documented, the rest assumed at 1.5 CC). A trip through an order book in dollars (via=usd) also
    pays the CC/USDCx leg of each pool side. `clears` is true when one nets at least $0.50."""
    s = load("scan")
    return {"as_of": s["t"], "swap_cost_cc": s.get("swap_cost_cc"), "routes": s["routes"][:max(1, min(limit, 50))]}


@server.tool(title="Recent alerts")
def alerts(limit: int = 20) -> dict:
    """Alerts fired on Canton DEXes: stablecoin peg breaks, premiums over 1%, hourly moves over 5%,
    and spreads that clear their costs. Newest last."""
    feed = load("alerts").get("feed", [])
    return {"alerts": feed[-max(1, min(limit, 300)):], "channel": "https://t.me/cantonvenues"}


@server.tool(title="Liquidity pools")
def pools(sort_by: str = "tvl", limit: int = 20) -> dict:
    """CC pools on every AMM (Cantex, Tradecraft, OneSwap, Pool Party) with liquidity, 24h volume where
    the venue publishes it, fee and LP fee APR from the last 24 hours of volume; a pool whose reserves
    have stood still while its price left the market carries `not_trading_since`. sort_by: tvl, volume
    or apr."""
    k = {"tvl": "tvl_usd", "volume": "volume_24h_usd", "apr": "fee_apr"}.get(sort_by)
    if k is None:
        raise ToolError("sort_by must be tvl, volume or apr")
    rows = sorted(load("lp")["pools"], key=lambda r: -(r.get(k) or 0))
    return {"as_of": load("lp")["t"], "pools": rows[:max(1, min(limit, 60))]}


@server.tool(title="Canton venues and coverage")
def venues() -> dict:
    """Every Canton trading venue we know of: type, 24h volume, and whether we price it live,
    see its volume only, or cannot read it (key needed, testnet, no public API). Also the share of
    Canton spot DEX volume that these prices cover."""
    return load("venues")


@server.tool(title="Perpetual futures")
def perps(base: str | None = None) -> dict:
    """Perp markets on Rocky and Ekiden: price, basis to the outside spot price, funding, spread,
    open interest and 24h volume. base filters by asset, e.g. BTC, ETH or CC."""
    d = load("perps")
    rows = [x for x in d["markets"] if base is None or x["base"].upper() == base.upper()]
    return {"as_of": d["t"], "markets": rows}


def main() -> None:
    ap = argparse.ArgumentParser(description="Canton Venues MCP server")
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8097)
    args = ap.parse_args()
    if not args.http:
        server.run()
        return
    server.run(
        "streamable-http", host=args.host, port=args.port, stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["cantonvenues.com", "www.cantonvenues.com", "127.0.0.1:*", "localhost:*"],
            allowed_origins=["https://cantonvenues.com", "https://www.cantonvenues.com",
                             "https://claude.ai", "http://localhost:*", "http://127.0.0.1:*"],
        ),
    )


if __name__ == "__main__":
    main()
