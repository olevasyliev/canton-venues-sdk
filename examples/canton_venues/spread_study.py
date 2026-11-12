"""Daily spread study: how long cross-venue spreads live on Canton, and who closes them.

Once a day (a systemd timer at a random hour, so the runs cover the clock) this snapshots every
pool and book the collector prices (Temple's books too, when TEMPLE_API_KEY is set), every ``--interval`` seconds for ``--duration`` seconds, and
tracks each round trip that clears ``MIN_ROUTE_USD`` after network costs: when it opened, how long
it stood, and whether the pools traded while it did. A pool the site marks not trading (the
collector's ``<out>/api/v1/pools.json``, ``not_trading``) is left out of every route, as on the site.
One summary per run is appended to ``<out>/api/v1/spreads.json`` (the last 60 runs). Read-only,
public sources, no keys.

    python spread_study.py --out /var/www/canton-venues --duration 3600 --interval 20
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import statistics
import time
from decimal import Decimal
from pathlib import Path

import model as m
from collect import USDCX, Collector, load_json, write_json

log = logging.getLogger("study")
KEEP_RUNS = 60
VERSION = 4
# v3 runs from Pool Party's return on 2026-11-10 07:02 UTC (every pool of it standing still since,
# CC/EDELx 62% off the market) booked a $400-1,270 EDELx trip into Pool Party that stood the whole
# hour: $2.4-3.0K captured an hour against a $202 median over the 34 runs to 6 Nov. Those runs are
# dropped when the first v4 run is written.
V3_WRONG_FROM = 1794294143
BUCKETS = (("under 40 s", 0, 40), ("40-80 s", 40, 80), ("80-160 s", 80, 160),
           ("160 s-10 min", 160, 600), ("over 10 min", 600, 10**9))


def summarize(samples: list[dict], step: int) -> dict:
    """Episodes from samples of ``{"t", "routes": [(key, net_usd, size_usd, fp)]}``."""
    open_: dict = {}
    done: list = []
    best = []
    for s in samples:
        seen = set()
        best.append(max((r[1] for r in s["routes"]), default=0.0))
        for key, net, size, fp in s["routes"]:
            if net < float(m.MIN_ROUTE_USD):
                continue
            seen.add(key)
            e = open_.get(key)
            if e is None:
                open_[key] = {"key": key, "t0": s["t"], "t1": s["t"], "fps": {fp}, "net": net, "size": size, "n": 1}
            else:
                e["t1"], e["n"] = s["t"], e["n"] + 1
                e["fps"].add(fp)
                e["net"] = max(e["net"], net)
        for key in [k for k in open_ if k not in seen]:
            done.append(open_.pop(key))
    done += open_.values()
    for e in done:
        e["life_s"] = e["t1"] - e["t0"] + step
    lives = [e["life_s"] for e in done]
    return {
        "start": samples[0]["t"], "end": samples[-1]["t"], "interval_s": step, "samples": len(samples),
        "episodes": len(done),
        "median_life_s": statistics.median(lives) if lives else None,
        "gone_fast": sum(1 for e in done if e["n"] == 1),
        "pools_changed": sum(1 for e in done if len(e["fps"]) > 1),
        "typical_best_net": round(statistics.median(best), 2) if best else None,
        "share_any_clearing": round(sum(b >= float(m.MIN_ROUTE_USD) for b in best) / len(best), 2) if best else None,
        "capture_usd": round(sum(e["net"] for e in done), 2),
        "median_net": round(statistics.median(e["net"] for e in done), 2) if done else None,
        "median_size": round(statistics.median(e["size"] for e in done)) if done else None,
        "stable_episodes": sum(1 for e in done if e["key"].split(":")[0] in m.STABLES),
        "hist": [{"label": lab, "n": sum(1 for x in lives if lo <= x < hi)} for lab, lo, hi in BUCKETS],
        "top": [{"token": e["key"].split(":")[0], "buy": e["key"].split(":")[1].split(">")[0],
                 "sell": e["key"].split(">")[1], "life_s": e["life_s"], "net": round(e["net"], 2),
                 "size": round(e["size"])} for e in sorted(done, key=lambda e: -e["net"])[:5]],
        "network_fee": m.fee_table_json(),
    }


async def setup(c: Collector) -> None:
    """Connect and read what every sample needs once. Tradecraft is optional, as in the collector: a
    "degraded" health answer used to stop the whole run before its first sample (replayed 2026-10-14)."""
    await c.cantex.connect()
    try:
        await c.tradecraft.connect()
    except Exception as exc:  # noqa: BLE001
        log.warning("tradecraft: %s; sampling without it until it answers", exc)
    tc = await _or_empty(c.tradecraft.pool_states(), "tradecraft pools")
    await c._refresh_slow(tc, await _or_empty(c.cantex.markets(), "cantex markets"))


async def _or_empty(call, what: str) -> list:
    """A read the study can do without: its result, or nothing when it fails."""
    try:
        return await call
    except Exception as exc:  # noqa: BLE001
        log.warning("%s: %s", what, exc)
        return []


def not_trading(c: Collector) -> set[tuple[str, str]]:
    """(venue, pair) of every pool the site marks not trading on its latest tick: its reserves stood
    still for two hours while its price sat 1% or more off the market. Empty when the collector has
    written no pools.json, so the study still runs on its own."""
    doc = load_json(c.api / "pools.json", {})
    return {(x["venue"], x["pair"]) for x in doc.get("not_trading") or []}


def mark_not_trading(c: Collector, books: dict, listed: set[tuple[str, str]]) -> list[str]:
    """The collector's own marks for the listed pools (``c.still`` for CC pools, ``still`` on its other
    pools), so its prices and routes leave them out as the site's do. Returns them as "venue pair"."""
    c.still = {}
    for k, pools in books.items():
        for v, p in pools.items():
            if (v, f"CC/{p.token}") in listed:
                c.still[(v, k)] = {"pair": f"CC/{p.token}"}
    for o in c.other_pools:
        if (o["venue"], f"{o['a']}/{o['b']}") in listed:
            o["still"] = {"pair": f"{o['a']}/{o['b']}"}
    return sorted([f"{v} {s['pair']}" for (v, _), s in c.still.items()]
                  + [f"{o['venue']} {o['still']['pair']}" for o in c.other_pools if o.get("still")])


async def sample(c: Collector) -> dict:
    cx = await c.cantex.pool_states()
    tc = await _or_empty(c.tradecraft.pool_states(), "tradecraft pools")
    tcp = await _or_empty(c.tradecraft.pools(), "tradecraft instruments") if tc else []
    books = c._books(cx, tc, tcp, Decimal("0.9"))
    await c._reserve_venues(books, cx)
    left_out = mark_not_trading(c, books, not_trading(c))
    _, t_books, _, cc_book = await c._temple_spot(books, Decimal(1))
    # Temple's CC/USDCx book prices CC, as on the site; the CC pairs below stay pool to pool, since the
    # study fingerprints pool reserves, and the dollar routes are each venue's best inside it
    cc_markets = {v: p for v, p in books[USDCX].items() if (v, USDCX) not in c.still} or dict(books[USDCX])
    if cc_book is not None and cc_book.bids and cc_book.asks:
        cc_markets["temple"] = m.CCBook("temple", next(iter(books[USDCX].values())).token, cc_book)
    cc_usd = m.cc_price_usd(cc_markets, Decimal(1))
    c.cc_at, c.cc_usd_last = {v: p.mid for v, p in cc_markets.items()}, cc_usd
    ob, ob_books, _ = await c._rocky_spot(books, cc_usd, Decimal(1))
    for k, v in t_books.items():
        for b in v.values():
            ob_books.setdefault(k, {})[b.symbol] = (b, USDCX)
    prices = c._prices(books, cc_usd, ob)
    hops = c._hops(books, cc_markets, ob_books, prices)
    routes = []
    trading = {k: {v: p for v, p in pools.items() if (v, k) not in c.still} for k, pools in books.items()}
    for r in m.scan({k: v for k, v in trading.items() if len(v) > 1}, cc_usd):
        pools = books[r["token"]]
        routes.append((f'{r["token"]}:{r["buy_on"]}>{r["sell_on"]}', r["net_usd"], r["size_usd"],
                       m.fingerprint(pools[r["buy_on"]], pools[r["sell_on"]])))
    for k, dr in c._dollar_routes(books, hops, ob_books, prices, Decimal(1)).items():
        for r in m.usd_scan(k, dr, cc_usd):
            routes.append((f'{r["token"]}:{r["buy_on"]}>{r["sell_on"]}', r["net_usd"], r["size_usd"], r["fp"]))
    return {"t": int(time.time()), "routes": routes, "left_out": left_out}


def append_run(path: Path, run: dict) -> dict:
    """``run`` added to the study file at ``path``, the last ``KEEP_RUNS`` kept, the wrong v3 runs out."""
    data = load_json(path, {"runs": []})
    runs = [x for x in data["runs"] if not (x.get("v") == 3 and x["start"] >= V3_WRONG_FROM)]
    data["runs"] = (runs + [run])[-KEEP_RUNS:]
    data["t"] = run["end"]
    write_json(path, data)
    return data


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--duration", type=int, default=3600)
    ap.add_argument("--interval", type=int, default=20)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    c = Collector(args.out)
    samples, errors = [], 0
    try:
        await setup(c)
        started = time.monotonic()
        while time.monotonic() - started < args.duration:
            t0 = time.monotonic()
            try:
                samples.append(await sample(c))
            except Exception as exc:  # noqa: BLE001
                errors += 1
                log.warning("sample failed: %s", exc)
            await asyncio.sleep(max(1.0, args.interval - (time.monotonic() - t0)))
    finally:
        await c.stop()
    if len(samples) < 10:
        log.error("only %d samples, not publishing", len(samples))
        return
    # v2: dollar stables valued at par and dollar routes on USDCx books only (2026-10-04). The v1 run
    # of that morning valued USDC.B at its pool premium and booked trips that did not exist. v3
    # (2026-10-14): each venue's dollar route is its best inside the venue, Rocky's USDC.B books in. v4
    # (2026-11-12): pools the site marks not trading are left out (``left_out``: each one, at any sample).
    left_out = sorted({p for s in samples for p in s.get("left_out", [])})
    run = summarize(samples, args.interval) | {"errors": errors, "left_out": left_out, "v": VERSION}
    append_run(args.out / "api" / "v1" / "spreads.json", run)
    log.info("study: %d samples, %d episodes, median life %s s, %d pools left out", len(samples),
             run["episodes"], run["median_life_s"], len(left_out))


if __name__ == "__main__":
    asyncio.run(main())
