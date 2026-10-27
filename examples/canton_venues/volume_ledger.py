"""Every venue's daily spot volume for the last month, with each day's records side by side
(``api/v1/volume.json``, written by the weekly job twice a day).

A day's volume on a venue can be read up to three ways, and the ledger keeps every one it has:

- ``venue_day``: the venue's own record of that exact UTC day. Temple's settled volume (and each
  market's), Cantex's volume in CC at the mean of our CC price readings that day, Tradecraft's
  hourly volume history summed into the day.
- ``venue_24h``: our reading, just after midnight UTC, of the venue's own rolling 24-hour counter
  (the only figure Rocky spot and Pool Party publish).
- ``outside``: an outside aggregator's daily record of the closed day, read after the day ended; a
  day equal to the one before to the cent is a stuck record and is left out.

``usd`` is the first of those, in that order (the weekly card counts the same figure). ``spread``
is how far the records the day has disagree (largest over smallest, less one) and ``check`` says
``agree`` (within ``AGREE``), ``differ``, or ``single`` (one record only). A venue's own day record
and the aggregator's can share a source (Temple's agree to the dollar), so ``agree`` means the
records match, not that they were made independently; our midnight reading of the venue's live
counter is the one made by us.

The venues' own trade counts (Temple's and Cantex's, Temple's per market too) are kept off the web
root (``COUNTS_FILE`` in the state directory, written by the weekly job) and are not published: the
number of trades and the average trade size are given on request, from ``averages`` (run this file
with ``--averages``).

For anyone who has to state how much traded where: an issuer's or an oracle's adoption figure, a
reward programme's baseline, a reviewer checking either.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

DAY = 86400
DAYS = 30
AGREE = 0.05
# a venue's own day record (weekly/days.json), by our slug
OWN_DAY = {"temple", "cantex"}
SOURCES = {
    "venue_day": "the venue's own record of that UTC day",
    "venue_24h": "our reading of the venue's own rolling 24-hour volume just after midnight UTC",
    "outside": "an outside aggregator's daily record of the closed day",
}
# where a venue's own day record comes from, as the ledger says it
OWN_SOURCE = {"temple": "Temple's settled volume for the day",
              "cantex": "Cantex's volume in CC for the day, at the mean of our CC price readings that day",
              "tradecraft": "Tradecraft's hourly volume history, summed into the day"}
OUTSIDE_FINAL_S = 6 * 3600  # the aggregator's point for a closed day is final this long after (weekly.final_after)
READING_WINDOW_S = 3600     # our first reading within this of 00:00 UTC stands for the day that closed
COUNTS_FILE = "volume_counts.json"  # in the state directory, beside the web root
ON_REQUEST = ("Trade counts and the average trade size per venue and day (Temple also per market) are "
              "recorded and given on request: https://cantonvenues.com/#contact")


def _day(t: int) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d")


def readings(hist: dict, slug: str) -> dict[int, float]:
    """Our first hourly reading of the venue's rolling 24 h spot volume after each midnight UTC, by
    the day it closes (the weekly card's figure for Rocky spot and Pool Party)."""
    h = hist.get("hourly") or {}
    col = ((h.get("venues") or {}).get(slug) or {}).get("spot_volume") or []
    out: dict[int, float] = {}
    for x, v in zip(h.get("t") or [], col, strict=False):
        day = x - x % DAY - DAY
        if x % DAY < READING_WINDOW_S and v and v > 0 and day not in out:
            out[day] = float(v)
    return out


def _daily(hist: dict, slug: str) -> tuple[str | None, dict[int, float]]:
    """(its source, each closed day's final point) from venue_history's daily series, leaving out a
    stuck aggregator day (equal to the day before to the cent)."""
    series = (hist.get("daily") or {}).get(slug) or {}
    source, read = series.get("source"), series.get("t") or 0
    points = {int(t): v for t, v in series.get("points") or []}
    out = {}
    for t, v in points.items():
        if not v or v <= 0:
            continue
        stuck = round(v, 2) == round(points.get(t - DAY) or 0, 2)
        if source == "defillama" and (stuck or read < t + DAY + OUTSIDE_FINAL_S):
            continue
        out[t] = float(v)
    return source, out


def venue_days(hist: dict, cache: dict, slug: str, days: list[int]) -> list[dict]:
    source, daily = _daily(hist, slug)
    reading = readings(hist, slug)
    own = cache.get(slug) or {}
    rows = []
    for t in days:
        figs: dict[str, float] = {}
        rec = own.get(str(t)) or {}
        if slug in OWN_DAY and rec.get("usd"):
            figs["venue_day"] = float(rec["usd"])
        elif source == "tradecraft" and t in daily:
            figs["venue_day"] = daily[t]
        if t in reading:
            figs["venue_24h"] = reading[t]
        if source == "defillama" and t in daily:
            figs["outside"] = daily[t]
        row: dict = {"day": _day(t), "t": t}
        if figs:
            best = next(k for k in SOURCES if k in figs)
            spread = max(figs.values()) / min(figs.values()) - 1 if len(figs) > 1 else None
            row.update(usd=round(figs[best], 2), source=best,
                       figures={k: round(v, 2) for k, v in figs.items()},
                       spread=None if spread is None else round(spread, 5),
                       check="single" if spread is None else ("agree" if spread <= AGREE else "differ"))
        else:
            row.update(usd=None, check="none")
        if rec.get("cc") is not None:
            row["volume_cc"] = round(float(rec["cc"]), 4)
        if rec.get("markets"):
            row["markets_usd"] = {sym: round(usd, 2) for sym, usd in sorted(rec["markets"].items())}
        rows.append(row)
    return rows


def build(hist: dict, cache: dict, now: int, venues: list[dict]) -> dict:
    """volume.json: ``hist`` is venue_history.json, ``cache`` weekly/days.json (the venues' own day
    records), ``venues`` the venue pages' list (those with any spot record are kept)."""
    end = now - now % DAY
    days = [end - (DAYS - n) * DAY for n in range(DAYS)]
    known = set(hist.get("daily") or {}) | set(cache) | set((hist.get("hourly") or {}).get("venues") or {})
    out_venues = []
    for v in venues:
        if v["slug"] not in known:
            continue
        rows = venue_days(hist, cache, v["slug"], days)
        if not any(r["usd"] for r in rows):
            continue
        out_venues.append({"slug": v["slug"], "name": v["name"], "own_record": OWN_SOURCE.get(v["slug"]),
                           "days": rows})
    by_day = []
    for i, t in enumerate(days):
        have = [(v["name"], v["days"][i]["usd"]) for v in out_venues if v["days"][i]["usd"]]
        by_day.append({"day": _day(t), "t": t, "usd": round(sum(x for _, x in have), 2),
                       "venues": [n for n, _ in have],
                       "missing": [v["name"] for v in out_venues if not v["days"][i]["usd"]]})
    return {"t": now, "days": DAYS, "agree_within": AGREE, "sources": SOURCES, "venues": out_venues,
            "by_day": by_day,
            "method": ("Spot volume per venue per closed UTC day. usd is the venue's own day record where it "
                       "publishes one, else our midnight reading of its 24-hour counter, else an outside "
                       "daily record; figures lists every record the day has and check says whether they "
                       f"agree within {int(AGREE * 100)}%. A venue with no record for a day is missing that "
                       "day, never estimated. Perpetuals are not counted; OneSwap publishes no volume."),
            "on_request": ON_REQUEST}


def averages(cache: dict, counts: dict, now: int, days: int = DAYS) -> list[dict]:
    """For a request: each venue's trades and average trade in dollars per closed day (Temple per
    market too), from the venues' own day records (``cache``) and their counts (``counts``)."""
    end = now - now % DAY
    out = []
    for slug in sorted(counts):
        for t in range(end - days * DAY, end, DAY):
            n, rec = (counts[slug].get(str(t)) or {}), (cache.get(slug) or {}).get(str(t)) or {}
            if not n.get("trades") or not rec.get("usd"):
                continue
            row = {"venue": slug, "day": _day(t), "usd": round(rec["usd"], 2), "trades": n["trades"],
                   "avg_trade_usd": round(rec["usd"] / n["trades"], 2)}
            mt, mu = n.get("market_trades") or {}, rec.get("markets") or {}
            if mt:
                row["markets"] = {s: {"usd": round(mu[s], 2), "trades": k, "avg_trade_usd": round(mu[s] / k, 2)}
                                  for s, k in sorted(mt.items()) if k and mu.get(s)}
            out.append(row)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Trade counts and average trade size per venue and day, on request")
    ap.add_argument("--averages", action="store_true", required=True)
    ap.add_argument("--out", type=Path, required=True, help="the web root (its weekly/days.json)")
    ap.add_argument("--state", type=Path, help="private records (default: <out>-state)")
    ap.add_argument("--days", type=int, default=DAYS)
    args = ap.parse_args()
    state = args.state or args.out.with_name(args.out.name + "-state")
    cache = json.loads((args.out / "weekly" / "days.json").read_text())
    counts = json.loads((state / COUNTS_FILE).read_text())
    now = int(datetime.now(UTC).timestamp())
    print(json.dumps(averages(cache, counts, now, args.days), indent=1))


if __name__ == "__main__":
    main()
