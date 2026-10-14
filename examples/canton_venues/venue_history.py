"""Per-venue history for the venue pages, kept small in ``api/v1/venue_history.json``.

Two kinds of series, each labelled on the page with where it comes from and since when:

- ``daily``: one day's volume per point, from an outside record that already goes back. DefiLlama's
  Canton DEX overview carries a per-protocol daily breakdown (``totalDataChartBreakdown``, read
  2026-10-07) for Temple, Cantex, Rocky spot and Pool Party; Tradecraft's own ``/volume_history``
  route gives each pool's hourly volume for the last week, summed here into complete UTC days and
  kept as they accumulate. ``daily_total`` is the DefiLlama Canton total for each day, for shares.
- ``hourly``: our own reading of each venue's figures (spot volume, pool liquidity, perps volume,
  open interest), one sample an hour, from the day this file started. Nobody else publishes these.

Capped: ``KEEP_DAILY_DAYS`` of daily points and ``KEEP_HOURLY_S`` of hourly ones.
"""

from __future__ import annotations

from datetime import datetime

KEEP_DAILY_DAYS = 365
KEEP_HOURLY_S = 90 * 86400
HOURLY_STEP_S = 3300  # a tick every five minutes: one sample per hour, give or take a tick
FIGURES = ("spot_volume", "tvl", "perp_volume", "open_interest")
# DefiLlama protocol name -> our venue slug (venue_pages.LLAMA_NAMES, by row id)
LLAMA_SLUG = {"Temple": "temple", "Cantex": "cantex", "Rocky Exchange Spot": "rocky", "Pool Party": "pool-party"}
SOURCES = {"defillama": "DefiLlama's Canton DEX list", "tradecraft": "Tradecraft's own volume history"}


def llama_daily(dex: dict, now: int) -> tuple[dict[str, list], list]:
    """(per-venue daily volume, Canton daily total) from DefiLlama's dex overview response."""
    cut = now - KEEP_DAILY_DAYS * 86400
    per: dict[str, list] = {}
    total = []
    for t, by in dex.get("totalDataChartBreakdown") or []:
        if t < cut or not isinstance(by, dict):
            continue
        s = 0.0
        for name, v in by.items():
            s += v or 0
            slug = LLAMA_SLUG.get(name)
            if slug:
                per.setdefault(slug, []).append([int(t), round(float(v or 0), 2)])
        total.append([int(t), round(s, 2)])
    return per, total


def _ts(x) -> int:
    if isinstance(x, datetime):
        return int(x.timestamp())
    if isinstance(x, str):
        return int(datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp())
    return int(x)


def hourly_to_days(histories: list[list]) -> list[list]:
    """Sum hourly volume series (one per pool) into complete UTC days: a day is kept only when all 24
    of its hours are present, so the first and the running day never show as a dip."""
    hours: dict[int, float] = {}
    for h in histories:
        for ts, v in h:
            t = _ts(ts)
            hours[t] = hours.get(t, 0.0) + float(v or 0)
    days: dict[int, list] = {}
    for t, v in hours.items():
        d = days.setdefault(t - t % 86400, [0.0, set()])
        d[0] += v
        d[1].add(t % 86400 // 3600)
    return [[d, round(v, 2)] for d, (v, hrs) in sorted(days.items()) if len(hrs) == 24]


def merge_daily(old: list, new: list, now: int) -> list:
    """Old points kept, new ones win on the same day, capped to ``KEEP_DAILY_DAYS``."""
    by = {int(t): v for t, v in old or []}
    by.update({int(t): v for t, v in new})
    cut = now - KEEP_DAILY_DAYS * 86400
    return [[t, by[t]] for t in sorted(by) if t >= cut]


def set_daily(state: dict, slug: str, source: str, points: list, now: int) -> None:
    """Merge a fresh read of a venue's daily record. ``t`` is when it was read: the weekly card
    (weekly.py) counts a day only from a read made after that day ended, since an outside record
    shows the running day as a partial figure until it closes."""
    prev = (state.setdefault("daily", {}).get(slug) or {})
    keep = prev.get("points") if prev.get("source") == source else []
    state["daily"][slug] = {"source": source, "t": int(now), "points": merge_daily(keep, points, now)}


def total_change(state: dict, now: int, figure: str = "spot_volume") -> float | None:
    """The change in our own Canton total of ``figure`` over a day: the latest hourly sample against
    the one a day before it. None without a sample in the last two hours, without one within an hour
    of a day earlier, or when the two samples do not cover the same venues."""
    h = state.get("hourly") or {}
    ts = h.get("t") or []
    if not ts or now - ts[-1] > 7200:
        return None
    j = min(range(len(ts)), key=lambda k: abs(ts[k] - (ts[-1] - 86400)))
    if abs(ts[j] - (ts[-1] - 86400)) > 3600:
        return None
    cols = [c[figure] for c in (h.get("venues") or {}).values() if figure in c]
    then, latest = [c[j] for c in cols], [c[-1] for c in cols]
    if [x is None for x in then] != [x is None for x in latest]:
        return None
    base = sum(x for x in then if x is not None)
    return sum(x for x in latest if x is not None) / base - 1 if base > 0 else None


def record_hourly(state: dict, now: int, facts: dict[str, dict]) -> bool:
    """One sample of every venue's figures, at most once an hour. True when one was taken."""
    h = state.setdefault("hourly", {"t": [], "venues": {}})
    if h["t"] and now - h["t"][-1] < HOURLY_STEP_S:
        return False
    h["t"].append(int(now))
    n = len(h["t"])
    for slug, f in facts.items():
        cols = h["venues"].setdefault(slug, {})
        for k in FIGURES:
            if f.get(k) is None and k not in cols:
                continue
            cols.setdefault(k, [None] * (n - 1)).append(None if f.get(k) is None else round(float(f[k]), 2))
    for cols in h["venues"].values():  # a venue missing this hour gets a gap, not a shifted series
        for col in cols.values():
            col.extend([None] * (n - len(col)))
    cut = next((i for i, t in enumerate(h["t"]) if t >= now - KEEP_HOURLY_S), 0)
    if cut:
        h["t"] = h["t"][cut:]
        for cols in h["venues"].values():
            for k in cols:
                cols[k] = cols[k][cut:]
    state["t"] = int(now)
    return True
