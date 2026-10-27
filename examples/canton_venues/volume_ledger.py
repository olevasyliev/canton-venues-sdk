"""Every venue's daily spot volume for the last month, with each day's records side by side
(``api/v1/volume.json``, written by the weekly job twice a day).

A day's volume on a venue can be read up to three ways, and the ledger keeps every one it has:

- ``venue_day``: the venue's own record of that exact UTC day. Temple's settled volume (with its
  trade count and each market's), Cantex's volume in CC at the mean of our CC price readings that day
  (with its swap count), Tradecraft's hourly volume history summed into the day.
- ``venue_24h``: our reading, just after midnight UTC, of the venue's own rolling 24-hour counter
  (the only figure Rocky spot and Pool Party publish).
- ``outside``: an outside aggregator's daily record of the closed day, read after the day ended; a
  day equal to the one before to the cent is a stuck record and is left out.

``usd`` is the first of those, in that order (the weekly card counts the same figure). ``spread``
is how far the records the day has disagree (largest over smallest, less one) and ``check`` says
``agree`` (within ``AGREE``), ``differ``, or ``single`` (one record only). A venue's own day record
and the aggregator's can share a source (Temple's agree to the dollar), so ``agree`` means the
records match, not that they were made independently; our midnight reading of the venue's live
counter is the one made by us. Trades and the average trade are the venue's own counts.

For anyone who has to state how much traded where: an issuer's or an oracle's adoption figure, a
reward programme's baseline, a reviewer checking either.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
        if rec.get("trades"):
            row["trades"] = rec["trades"]
            if figs.get("venue_day"):
                row["avg_trade_usd"] = round(figs["venue_day"] / rec["trades"], 2)
        if rec.get("cc") is not None:
            row["volume_cc"] = round(float(rec["cc"]), 4)
        if rec.get("markets"):
            row["markets"] = {sym: {"usd": round(usd, 2), "trades": n} for sym, (usd, n) in sorted(rec["markets"].items())}
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
                       "day, never estimated. Perpetuals are not counted; OneSwap publishes no volume.")}
