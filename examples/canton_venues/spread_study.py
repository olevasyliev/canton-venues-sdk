"""Daily spread study: how long cross-venue spreads live on Canton, and who closes them.

Once a day (a systemd timer at a random hour, so the runs cover the clock) this snapshots every
pool and book the collector prices (Temple's books too, when TEMPLE_API_KEY is set), every ``--interval`` seconds for ``--duration`` seconds, and
tracks each round trip that clears ``MIN_ROUTE_USD`` after network costs: when it opened, how long
it stood, and whether the pools traded while it did. One summary per run is appended to
``<out>/api/v1/spreads.json`` (the last 60 runs). Read-only, public sources, no keys.

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


async def sample(c: Collector) -> dict:
    cx = await c.cantex.pool_states()
    tc = await c.tradecraft.pool_states()
    tcp = await c.tradecraft.pools()
    books = c._books(cx, tc, tcp, Decimal("0.9"))
    await c._reserve_venues(books, cx)
    _, t_books, _, cc_book = await c._temple_spot(books, Decimal(1))
    # Temple's CC/USDCx book prices CC, as on the site; the CC pairs below stay pool to pool, since the
    # study fingerprints pool reserves, and the dollar routes are each venue's best inside it
    cc_markets = dict(books[USDCX])
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
    for r in m.scan({k: v for k, v in books.items() if len(v) > 1}, cc_usd):
        pools = books[r["token"]]
        routes.append((f'{r["token"]}:{r["buy_on"]}>{r["sell_on"]}', r["net_usd"], r["size_usd"],
                       m.fingerprint(pools[r["buy_on"]], pools[r["sell_on"]])))
    for k, dr in c._dollar_routes(books, hops, ob_books, prices, Decimal(1)).items():
        for r in m.usd_scan(k, dr, cc_usd):
            routes.append((f'{r["token"]}:{r["buy_on"]}>{r["sell_on"]}', r["net_usd"], r["size_usd"], r["fp"]))
    return {"t": int(time.time()), "routes": routes}


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--duration", type=int, default=3600)
    ap.add_argument("--interval", type=int, default=20)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    c = Collector(args.out)
    await c.start()
    samples, errors = [], 0
    try:
        tc = await c.tradecraft.pool_states()
        await c._refresh_slow(tc, await c.cantex.markets())
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
    # (2026-10-14): each venue's dollar route is its best inside the venue, Rocky's USDC.B books in.
    run = summarize(samples, args.interval) | {"errors": errors, "v": 3}
    path = args.out / "api" / "v1" / "spreads.json"
    data = load_json(path, {"runs": []})
    data["runs"] = (data["runs"] + [run])[-KEEP_RUNS:]
    data["t"] = run["end"]
    write_json(path, data)
    log.info("study: %d samples, %d episodes, median life %s s", len(samples), run["episodes"], run["median_life_s"])


if __name__ == "__main__":
    asyncio.run(main())
