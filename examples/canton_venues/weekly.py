"""Canton trading this week: one share card and page for the last seven finished UTC days.

    python weekly.py --api /var/www/canton-venues/api/v1 --out /var/www/canton-venues

Writes ``<out>/weekly/index.html`` (the latest week), ``<out>/weekly/<last day>/`` (a permalink per
period, so a post keeps its preview after the next week replaces the latest one), each with a
1200x630 ``card.png``, and ``<out>/api/v1/weekly.json`` with every number on them. A timer runs it
after midnight UTC (deploy/canton-venues-weekly.timer); no model is involved anywhere.

Each venue's volume for each UTC day comes from the first of these that has it:

1. The venue's own record of that exact day, read here and kept in ``weekly/days.json``:
   Temple ``/api/exchange/settled_volume`` (USD, one call per day: it refuses windows over 24 h),
   Cantex ``/v1/public/volume`` (CC, converted at the mean of our own CC price readings that day,
   ``history.json``, when they cover at least ``MIN_PRICE_HOURS`` of its hours), and Tradecraft's
   hourly ``/volume_history``, summed into complete UTC days by the collector (venue_history.py).
2. Our own reading: Rocky spot and Pool Party publish only a rolling 24-hour volume; the
   collector's first hourly reading after midnight UTC (within ``volume_ledger.READING_WINDOW_S``) stands for
   the day that just closed. Recorded from 7 Oct 2026.
3. DefiLlama's Canton DEX record, per protocol per UTC day (``totalDataChartBreakdown``), only
   when neither of the above exists and only from a read made after the day closed (its running
   day is partial). Its point at time T covers [T, T + 1 day): checked 2026-10-07, its Temple point
   for 6 Oct ($36,118,149) is Temple's own figure for that day ($36,117,825). Credited on the page's
   notes, never on the card.

- The big number: the sum over the period for the venues with a figure on all seven days. A venue
  missing a day is left out and named on the page, never estimated. A zero counts as missing.
- Week on week: the same venues over the seven days before, only when each has all seven.
- Share by venue: each counted venue's seven-day sum over the total.
- Scope: "Canton DEX spot volume" only when every protocol on DefiLlama's Canton DEX list, on every
  day of the period, is a venue we count (its per-protocol figures add up to its Canton total, so
  none is missing); otherwise "Spot volume, Canton venues we read".

Left out because the data does not exist for a week: best execution (we keep a snapshot, not a
week of quotes), new markets listed (no market list from a week ago), perps (not spot) and OneSwap
(publishes no volume).

Before anything is overwritten the period's total goes through the publish guard
(publish_guard.py, state in ``weekly/published.json``): an implausible jump or stale data keeps the
last good card and page up.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import volume_ledger
from crosscheck import CANTEX, TEMPLE, http_get
from publish_guard import PublishGuard
from venue_pages import (SITE, THEMES, UP, VENUES, X_ICON, _fit, _font, _hex, _mix, _shell, _write,
                         draw_logo, e, money, stamp)

log = logging.getLogger("venues.weekly")

DAY = 86400
DAYS = 7
SLUG = "weekly"  # the guard's key
# DefiLlama's Canton total and its per-protocol figures we keep must agree to this, per day, for the
# card to say "Canton": rounding to cents in venue_history leaves a few cents of difference.
TOTAL_TOLERANCE_USD = 1.0
EARLIER_SHOWN = 12
# a venue's own day record: read once the day has been closed this long, and read again on later
# runs until a read lands this long after the close (late settlement), then kept as final
SETTLE_S = 600
FINAL_AFTER_S = 6 * 3600
BACKFILL_DAYS = 2 * DAYS  # this period and the one before, for week on week
MIN_PRICE_HOURS = 22      # our CC price readings must cover this many of a day's hours to convert it
# our venue slug -> its name in DefiLlama's Canton DEX list (venue_pages.LLAMA_NAMES, by slug)
LLAMA_SLUG = {"temple": "Temple", "cantex": "Cantex", "rocky": "Rocky Exchange Spot", "pool-party": "Pool Party"}
# where a day's figure came from, as the page says it
SOURCE = {"temple": "Temple's own settled volume", "cantex": "Cantex's own volume in CC, at our CC price that day",
          "tradecraft": "Tradecraft's own hourly volume history", "reading": "our reading of its 24-hour volume "
          "just after midnight UTC", "defillama": "an outside daily record of the closed day (small print)"}
OWN_DAY = {"temple", "cantex"}  # venues whose own API answers for an exact past day


# === the venues' own day records =============================================

def _iso(t: int) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_twice(get, url: str, params: dict):
    try:
        return get(url, params)
    except Exception:  # noqa: BLE001 - Temple answers a 502 now and then; one retry
        return get(url, params)


def own_day(get, slug: str, t: int) -> dict:
    """One venue's own figure for the UTC day starting at ``t``: the window DefiLlama's adapters ask
    for too, day start + 1 s to day end. With its trade count (None when the venue gives none: the
    key says it was asked) and Temple's per market, for the volume ledger (volume_ledger.py)."""
    params = {"start_time": _iso(t + 1), "end_time": _iso(t + DAY)}
    if slug == "temple":
        raw = _get_twice(get, TEMPLE, params)
        n = raw.get("trade_count")
        out = {"usd": float(raw["total_volume_usd"]), "trades": None if n is None else int(n)}
        markets = {x["symbol"]: [float(x["quote_volume"]), int(x.get("trade_count") or 0)]
                   for x in raw.get("markets") or [] if x.get("symbol") and x.get("quote_volume") is not None}
        if markets:
            out["markets"] = markets
        return out
    raw = _get_twice(get, CANTEX, params)["data"]
    n = raw.get("swap_count")
    return {"cc": float(raw["volume_cc"]), "trades": None if n is None else int(float(n))}


def cc_price_day(history: dict, t: int) -> float | None:
    """The mean of our own CC price readings in the UTC day from ``t``, when they cover enough of it."""
    ts, col = history.get("t") or [], (history.get("usd") or {}).get("CC") or []
    vals, hours = [], set()
    for x, p in zip(ts, col):
        if t <= x < t + DAY and p:
            vals.append(float(p))
            hours.add((x - t) // 3600)
    return sum(vals) / len(vals) if len(hours) >= MIN_PRICE_HOURS else None


def fetch_days(cache: dict, history: dict, now: int, get=http_get) -> dict:
    """Read every venue-own day record the period and the one before still lack, and the volume
    ledger's days (``volume_ledger.DAYS``) read before trade counts were kept. Each venue and day
    fails alone: a day that cannot be read is simply not there (and the card leaves it out). A
    re-read keeps a converted dollar figure: Cantex's needs our CC price readings of that day, which
    history.json keeps for a week only."""
    end = now - now % DAY
    for t in range(end - max(BACKFILL_DAYS, volume_ledger.DAYS) * DAY, end, DAY):
        if now < t + DAY + SETTLE_S:
            continue
        for slug in sorted(OWN_DAY):
            have = cache.setdefault(slug, {}).get(str(t))
            if have and have["t"] >= t + DAY + FINAL_AFTER_S and "trades" in have:
                continue
            if have is None and t < end - BACKFILL_DAYS * DAY:
                continue  # the ledger fills in counts for days the card read, it does not reach further back
            try:
                final = have is not None and have["t"] >= t + DAY + FINAL_AFTER_S  # re-read for its count only
                kept = {k: have[k] for k in ("usd", "cc_usd") if final and k in have and slug == "cantex"}
                cache[slug][str(t)] = {**own_day(get, slug, t), **kept, "t": now}
            except Exception as exc:  # noqa: BLE001
                log.warning("weekly: %s %s: %s", slug, _iso(t)[:10], type(exc).__name__)
    for t, rec in (cache.get("cantex") or {}).items():
        if rec.get("usd") is None and rec.get("cc") is not None:
            price = cc_price_day(history, int(t))
            if price:
                rec.update(usd=rec["cc"] * price, cc_usd=price)
    return cache


# === numbers ===============================================================

def period(now: int) -> tuple[int, int]:
    """(first day, end): the seven finished UTC days before the day ``now`` falls in."""
    end = now - now % DAY
    return end - DAYS * DAY, end


def _date(t: int, year: bool = True) -> str:
    d = datetime.fromtimestamp(t, UTC)
    return f"{d.day} {d:%b %Y}" if year else f"{d.day} {d:%b}"


def date_range(start: int, end: int) -> str:
    """"30 Sep to 6 Oct 2026", "1 to 7 Oct 2026": the first and the last day counted."""
    a, b = datetime.fromtimestamp(start, UTC), datetime.fromtimestamp(end - DAY, UTC)
    if a.year != b.year:
        return f"{_date(start)} to {_date(end - DAY)}"
    if a.month != b.month:
        return f"{_date(start, False)} to {_date(end - DAY)}"
    return f"{a.day} to {_date(end - DAY)}"


def _days_text(ts: list[int]) -> str:
    return ", ".join(_date(t, False) for t in ts)


def final_after(source: str) -> int:
    """How long after a day closes a read of this daily record holds that day's final figure.
    DefiLlama's point for the running day is a rolling 24-hour figure, not the day so far (its 7 Oct
    point at 21:00 UTC, $34.86M, was its 24 h total; Temple's own 00:00 to 21:00 was $31.31M), so
    just after midnight its point for the day that closed may still be a pre-midnight reading: it
    counts only from a read ``FINAL_AFTER_S`` after the close. Tradecraft's own hourly history is
    final once read after the close (its last hour may be partial before)."""
    return FINAL_AFTER_S if source == "defillama" else 0


def day_figures(hist: dict, days_cache: dict, slug: str) -> tuple[dict[int, tuple[float, str]], set[int]]:
    """(every day we have a final figure for, with where it came from, best source first; the days
    whose only figure is not final yet)."""
    out: dict[int, tuple[float, str]] = {}
    early: set[int] = set()
    series = (hist.get("daily") or {}).get(slug) or {}
    read = series.get("t") or 0
    source = series.get("source") or "defillama"
    points = {int(t): v for t, v in series.get("points") or []}
    for t, v in points.items():
        # an outside record that stopped reading a venue repeats its last day: DefiLlama gave Rocky
        # $690,821 for both 18 and 19 Oct 2026, while Rocky's API answered nobody, so a day equal to
        # the one before to the cent is no figure (the venue is then left out, never filled in)
        if source == "defillama" and v and round(v, 2) == round(points.get(t - DAY) or 0, 2):
            continue
        if v and v > 0:
            if read >= int(t) + DAY + final_after(source):
                out[int(t)] = (float(v), source)
            else:
                early.add(int(t))
    for t, v in volume_ledger.readings(hist, slug).items():
        out[t] = (v, "reading")
    for t, rec in (days_cache.get(slug) or {}).items():
        if rec.get("usd") and rec["usd"] > 0:
            out[int(t)] = (float(rec["usd"]), slug)
    return out, early - set(out)


def week(hist: dict, now: int, days_cache: dict | None = None) -> dict | None:
    """The period's figures, or None when no venue has a figure for every day."""
    start, end = period(now)
    days = [start + n * DAY for n in range(DAYS)]
    before = [t - DAYS * DAY for t in days]
    known = set((hist.get("daily") or {})) | set(days_cache or {})
    venues, left_out, pending = [], [], []
    for v in VENUES:
        if v["slug"] not in known:
            continue
        figs, early = day_figures(hist, days_cache or {}, v["slug"])
        missing = [t for t in days if t not in figs]
        if missing and set(missing) <= early:  # only not final yet: the card waits for it
            pending.append({"slug": v["slug"], "name": v["name"], "days": missing})
            continue
        if missing:
            left_out.append({"slug": v["slug"], "name": v["name"], "why": f"no daily figure for {_days_text(missing)}"})
            continue
        prev = [figs.get(t) for t in before]
        venues.append({"slug": v["slug"], "name": v["name"],
                       "daily": [figs[t][0] for t in days], "src": [figs[t][1] for t in days],
                       "total": sum(figs[t][0] for t in days),
                       "prev": sum(p[0] for p in prev) if all(prev) else None})
    if not venues:
        return None
    total = sum(v["total"] for v in venues)
    by_day = [sum(v["daily"][n] for v in venues) for n in range(DAYS)]
    for v in venues:
        v["share"] = v["total"] / total
    venues.sort(key=lambda v: -v["total"])
    prev_total = sum(v["prev"] for v in venues) if all(v["prev"] is not None for v in venues) else None
    return {"start": start, "end": end, "slug": datetime.fromtimestamp(end - DAY, UTC).strftime("%Y-%m-%d"),
            "range": date_range(start, end), "days": days, "by_day": by_day, "total": total,
            "prev_total": prev_total, "change": total / prev_total - 1 if prev_total else None,
            "venues": venues, "left_out": left_out, "pending": pending,
            "scope": "canton" if on_canton(hist, days, {v["slug"] for v in venues}) else "read",
            "sources": sorted({s for v in venues for s in v["src"]})}


def on_canton(hist: dict, days: list[int], counted: set[str]) -> bool:
    """True when, on every day, DefiLlama's Canton DEX total is made of protocols we know (its
    per-protocol figures we keep add up to it) and every one of them with volume is a venue we count."""
    total = {int(t): float(x) for t, x in hist.get("daily_total") or []}
    per = {slug: {int(t): float(x) for t, x in ((hist.get("daily") or {}).get(slug) or {}).get("points") or []}
           for slug in LLAMA_SLUG if ((hist.get("daily") or {}).get(slug) or {}).get("source") == "defillama"}
    for t in days:
        if t not in total:
            return False
        listed = {s: p[t] for s, p in per.items() if p.get(t)}
        if abs(sum(listed.values()) - total[t]) > TOTAL_TOLERANCE_USD or not set(listed) <= counted:
            return False
    return True


def label(w: dict) -> str:
    return "Canton DEX spot volume" if w["scope"] == "canton" else "Spot volume, Canton venues we read"


def pct(x: float) -> str:
    return "<0.1%" if x < 0.0005 else f"{x * 100:.1f}%"


def shares(w: dict) -> list[str]:
    """Each venue's share in tenths of a percent, rounded by largest remainder so the shown shares
    add up to 100.0%; a share that rounds to nothing shows as "<0.1%"."""
    raw = [v["share"] * 1000 for v in w["venues"]]
    units = [int(x) for x in raw]
    for i in sorted(range(len(raw)), key=lambda i: -(raw[i] - units[i]))[:1000 - sum(units)]:
        units[i] += 1
    return ["<0.1%" if u == 0 else f"{u / 10:.1f}%" for u in units]


def usd(v: float) -> str:
    """Money with one decimal kept in millions, so a column of days reads alike: $28.0M, $45.7M."""
    return f"${v / 1e6:.1f}M" if abs(v) >= 999_950 else money(v)


def change_text(w: dict) -> str | None:
    return None if w["change"] is None else f"{w['change'] * 100:+.1f}% on the 7 days before"


# === card ==================================================================

def render_card(w: dict, path: Path) -> None:
    """1200x630, drawn at 2x and scaled down: the dashboard's dark theme, one number dominant, the
    seven days as bars, each counted venue's share, and our name as the source: which record each
    day came from is on the page, not on the card."""
    from PIL import Image, ImageDraw

    c = {k: (_hex(v) if isinstance(v, str) else v) for k, v in THEMES["dark"].items()}
    S = 2
    W, H, M = 1200 * S, 630 * S, 56 * S
    img = Image.new("RGB", (W, H), c["bg"])
    d = ImageDraw.Draw(img)

    draw_logo(d, M, 40 * S, 36 * S, c["text"], c["bg"])
    d.text((M + 50 * S, 58 * S), "Canton Venues", font=_font("Bold", 27 * S), fill=c["text"], anchor="lm")
    d.text((W - M, 58 * S), "Canton trading this week", font=_font("SemiBold", 24 * S), fill=c["text2"],
           anchor="rm")

    # left: what is counted, the number, the days it covers, the change
    left = 560 * S
    lab = label(w) + ", 7 days"
    d.text((M, 140 * S), lab, font=_fit(d, lab, "Regular", 28 * S, left, 20 * S), fill=c["text2"], anchor="lt")
    big = money(w["total"])
    bf = _fit(d, big, "Bold", 150 * S, left, 96 * S)
    d.text((M - 4 * S, 186 * S), big, font=bf, fill=c["text"], anchor="lt")
    y = 186 * S + bf.size + 24 * S
    d.text((M, y), w["range"] + ", UTC", font=_font("SemiBold", 28 * S), fill=c["text"], anchor="lt")
    if ch := change_text(w):
        col = _hex(UP) if w["change"] >= 0 else _hex("#ea3943")
        d.text((M, y + 44 * S), ch, font=_font("SemiBold", 26 * S), fill=col, anchor="lt")

    # right: one bar a day, the largest day labelled
    x0, x1, top, base = 664 * S, W - M, 172 * S, 418 * S
    d.text((x0, 140 * S), "By day", font=_font("Regular", 24 * S), fill=c["text2"], anchor="lt")
    gap = 14 * S
    bw = (x1 - x0 - gap * (DAYS - 1)) / DAYS
    hi = max(w["by_day"]) or 1
    peak = w["by_day"].index(hi)
    bar = _mix(_hex(THEMES["dark"]["accent"]), c["bg"], 0.52)  # a tone of the accent, not the accent itself
    for n, v in enumerate(w["by_day"]):
        bx = x0 + n * (bw + gap)
        h = max(2 * S, (base - top - 34 * S) * v / hi)
        d.rounded_rectangle((bx, base - h, bx + bw, base), radius=5 * S, fill=bar, corners=(True, True, False, False))
        day = datetime.fromtimestamp(w["days"][n], UTC)
        d.text((bx + bw / 2, base + 22 * S), str(day.day), font=_font("SemiBold", 22 * S), fill=c["text2"],
               anchor="mm")
        d.text((bx + bw / 2, base + 46 * S), f"{day:%a}", font=_font("Regular", 17 * S), fill=c["text3"],
               anchor="mm")
        if n == peak:
            d.text((bx + bw / 2, base - h - 14 * S), usd(v), font=_font("SemiBold", 20 * S), fill=c["text"],
                   anchor="ms")
    d.line((x0, base, x1, base), fill=c["line2"], width=S)

    # each counted venue's share of the week, largest first, on one line
    parts = [(v["name"], sh) for v, sh in zip(w["venues"], shares(w))]
    head = "By venue"
    for z in range(27, 17, -1):
        nf, pf, sep = _font("SemiBold", z * S), _font("Regular", z * S), 34 * S
        width = (d.textlength(head, font=pf) + sep * len(parts)
                 + sum(d.textlength(a + " ", font=nf) + d.textlength(b, font=pf) for a, b in parts))
        if width <= W - 2 * M:
            break
    x, ys = M, 512 * S
    d.text((x, ys), head, font=pf, fill=c["text2"], anchor="lm")
    x += d.textlength(head, font=pf) + sep
    for a, b in parts:
        d.text((x, ys), a + " ", font=nf, fill=c["text"], anchor="lm")
        x += d.textlength(a + " ", font=nf)
        d.text((x, ys), b, font=pf, fill=c["text2"], anchor="lm")
        x += d.textlength(b, font=pf) + sep

    # the source strip
    st = 560 * S
    d.rectangle((0, st, W, H), fill=c["strip"])
    sf = _font("SemiBold", 22 * S)
    x, yf = M, st + 35 * S
    for part, col in (("Data: ", c["strip_text"]), ("cantonvenues.com", c["strip_accent"])):
        d.text((x, yf), part, font=sf, fill=col, anchor="lm")
        x += d.textlength(part, font=sf)

    out = img.resize((1200, 630), Image.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    out.save(tmp, "PNG", optimize=True)
    os.replace(tmp, path)


# === page ==================================================================

# the shared footer's method line is about the live pages (24h figures); this page has its own
SHELL_METHOD = "Volumes are each venue's 24h figures; where a venue reports token amounts rather than dollars we convert them, and each venue's page says how."
WEEKLY_METHOD = ("This week's volume is each venue's spot volume for each closed UTC day, summed over the "
                 "seven days; where a venue reports token amounts we convert them at that day's price, and "
                 "How this is counted above says which record each day comes from.")

PAGE_CSS = """<style>
.wk .vbig { font-size: 44px; }
.wk .charts { margin-top: 14px; }
.wk .hbars .hrow { grid-template-columns: minmax(0, 7em) minmax(0, 1fr) 5.2em; }
.wk ol.earlier { margin: 8px 0 0; padding-left: 18px; color: var(--text-2); }
.wk ol.earlier li { margin: 3px 0; }
.wk .fine { color: var(--text-3); font-size: 12px; margin: 10px 0 0; max-width: 80ch; }
@media (max-width: 720px) { .wk .vbig { font-size: 36px; } }
</style>"""


def share_text(w: dict) -> str:
    ch = f", {change_text(w)}" if w["change"] is not None else ""
    return f"Canton trading this week: {label(w)} {money(w['total'])}, {w['range']}{ch}."


def intent_url(w: dict) -> str:
    url = f"{SITE}/weekly/{w['slug']}/"
    return f"https://x.com/intent/post?text={quote(share_text(w))}&url={quote(url, safe='')}"


def _bars(rows: list[tuple[str, float, str]]) -> str:
    top = max(v for _, v, _ in rows) or 1
    return '<div class="hbars">' + "".join(
        f'<div class="hrow" tabindex="0" title="{e(k)}, {e(shown)}"><span class="hl">{e(k)}</span>'
        f'<span class="hb"><i style="width:{v / top * 100:.1f}%"></i></span><span class="hn num">{e(shown)}</span></div>'
        for k, v, shown in rows) + "</div>"


def venue_note(v: dict, days: list[int]) -> str:
    """Where one venue's seven figures came from: "Temple: Temple's own settled volume, each UTC day."
    or, mixed, each source with the days it covers."""
    by: dict[str, list[int]] = {}
    for t, s in zip(days, v["src"]):
        by.setdefault(s, []).append(t)
    if len(by) == 1:
        return f"{v['name']}: {SOURCE.get(next(iter(by)), next(iter(by)))}, each UTC day."
    return f"{v['name']}: " + "; ".join(f"{SOURCE.get(s, s)} for {_days_text(ts)}" for s, ts in by.items()) + "."


def notes(w: dict) -> list[str]:
    """How the week is counted, venue by venue. Names no outside aggregator: that is ``small_print``."""
    out = [venue_note(v, w["days"]) for v in w["venues"]]
    if "reading" in w["sources"]:
        out.append("Rocky and Pool Party publish only a rolling 24-hour volume: our first reading after "
                   "midnight UTC stands for the day that just closed.")
    if w["scope"] == "canton":
        out.append("Canton DEX spot volume: every DEX on the public list of Canton DEX volume, on every day "
                   "of the week, is counted here.")
    else:
        out.append("Not every DEX on the public list of Canton DEX volume has a full week here, so this is the "
                   "volume among the Canton venues we read, not all of Canton.")
    for x in w["left_out"]:
        out.append(f"Not in the total: {x['name']}, {x['why']}. A missing day is never estimated or "
                   "filled with a zero.")
    if w["change"] is None:
        out.append("No week-on-week change: not every venue counted has all seven days before this period.")
    out.append("Not in the total: perps, and OneSwap, which publishes no volume.")
    return out


def small_print(w: dict) -> str:
    """The one line that names the outside daily history: where it fills a day, and the list the
    "Canton DEX" scope is checked against."""
    if "defillama" in w["sources"]:
        return ("The outside daily record that fills a closed day we have no venue record or reading of is "
                "DefiLlama's daily history, also the public list of Canton DEX volume the scope is checked "
                "against.")
    return "The public list of Canton DEX volume the scope is checked against is DefiLlama's."


def page(w: dict, t: int, permalink: bool, card_v: int, earlier: list[str]) -> str:
    url = f"{SITE}/weekly/{w['slug']}/" if permalink else f"{SITE}/weekly/"
    card = f"{SITE}/weekly/{w['slug']}/card.png?v={card_v}"
    title = f"Canton trading, {w['range']} | Canton Venues"
    desc = (f"{label(w)}, {w['range']}: {money(w['total'])} over seven days"
            + (f", {change_text(w)}" if w["change"] is not None else "")
            + ". Daily spot volume by venue, from cantonvenues.com.")
    stats = [(f"{label(w)}, 7 days", money(w["total"]))]
    if w["change"] is not None:
        stats.append(("Week on week", f"{w['change'] * 100:+.1f}%"))
    stats.append(("Venues counted", str(len(w["venues"]))))
    days = [(f"{datetime.fromtimestamp(t0, UTC):%a} {_date(t0, False)}", v, usd(v))
            for t0, v in zip(w["days"], w["by_day"])]
    by_venue = [(v["name"], v["total"], sh) for v, sh in zip(w["venues"], shares(w))]
    alt = f"{label(w)}, {w['range']}: {money(w['total'])}."
    crumbs = (f'<a href="/">Canton Venues</a> / <a href="/weekly/">This week</a> / {e(w["slug"])}' if permalink
              else '<a href="/">Canton Venues</a> / This week')
    prior = "".join(f'<li><a href="/weekly/{e(s)}/">Seven days to {e(_date(int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())))}</a></li>'
                    for s in earlier[:EARLIER_SHOWN])
    body = f"""{PAGE_CSS}
  <div class="wk">
  <p class="crumbs">{crumbs}</p>
  <div class="vhead"><h1>Canton trading this week</h1></div>
  <p class="vcap">{e(w['range'])}: the seven finished UTC days to {e(_date(w['end']))} 00:00 UTC.</p>
  <div class="stats">{"".join(f'<div class="stat"><div class="k">{e(k)}</div><div class="v num">{e(v)}</div></div>' for k, v in stats)}</div>
  <section class="block">
    <h2>Share this week</h2>
    <p class="sub">Shared on X, the card shows as the preview. This link keeps this week's card.</p>
    <div class="vtop">
    <img class="vcard" src="/weekly/{e(w['slug'])}/card.png?v={card_v}" width="1200" height="630" alt="{e(alt)}">
    <div class="acts">
      <a class="btn x" href="{e(intent_url(w))}" target="_blank" rel="noopener">{X_ICON}Share on X</a>
      <a class="btn" href="/weekly/{e(w['slug'])}/card.png?v={card_v}" download="canton-trading-{e(w['slug'])}.png">Download card</a>
      <p class="muted">Updated {e(stamp(t))}.</p>
    </div>
    </div>
  </section>
  <section class="block">
    <h2>By day and by venue</h2>
    <div class="charts">
      <div class="panel"><h3>Spot volume by day</h3><p class="sub">Every venue counted, each UTC day.</p>{_bars(days)}</div>
      <div class="panel"><h3>Share of the week</h3><p class="sub">Each venue's seven days over the total.</p>{_bars(by_venue)}</div>
    </div>
  </section>
  <section class="block">
    <h2>How this is counted</h2>
    <ul class="notes">{"".join(f"<li>{e(n)}</li>" for n in notes(w))}</ul>
    <p class="fine">{e(small_print(w))}</p>
    <p class="sub" style="margin-top:12px">Every figure is in <a href="/api/v1/weekly.json">weekly.json</a>. Something wrong? <a href="/#contact">Write to us</a>.</p>
  </section>
  {f'<section class="block"><h2>Earlier weeks</h2><ol class="earlier">{prior}</ol></section>' if prior else ""}
  </div>"""
    html = _shell(title, desc, url, card, body, t)
    assert SHELL_METHOD in html, "the shared footer's method text changed: update SHELL_METHOD"
    return html.replace(SHELL_METHOD, WEEKLY_METHOD)


# === build =================================================================

def _api(w: dict, t: int) -> dict:
    return {"t": t, "start": w["start"], "end": w["end"], "slug": w["slug"], "range": w["range"],
            "scope": w["scope"], "label": label(w), "total_usd": round(w["total"], 2),
            "prev_total_usd": None if w["prev_total"] is None else round(w["prev_total"], 2),
            "change": None if w["change"] is None else round(w["change"], 5),
            "by_day": [[t0, round(v, 2)] for t0, v in zip(w["days"], w["by_day"])],
            "venues": [{"slug": v["slug"], "name": v["name"], "total_usd": round(v["total"], 2),
                        "share": round(v["share"], 5), "daily": [round(x, 2) for x in v["daily"]],
                        "daily_source": v["src"]}
                       for v in w["venues"]],
            "left_out": w["left_out"], "notes": notes(w), "small_print": small_print(w),
            "shares_shown": shares(w)}


def build(out: Path, hist: dict, now: int, guard: PublishGuard | None = None,
          days_cache: dict | None = None) -> dict | None:
    """Write the latest page, the period's permalink, both cards' image and weekly.json. Returns the
    week written, or None when nothing was (no full week, or the guard held the last good one).
    ``days_cache`` is weekly/days.json, the venues' own day records (``fetch_days``)."""
    w = week(hist, now, days_cache)
    root = out / "weekly"
    if w is None:
        log.warning("weekly: no venue has a full week of daily figures read since the week ended")
        return None
    if w["pending"]:  # never publish a week with a day that is not final yet; a later run will
        log.warning("weekly held: not final yet: %s", "; ".join(
            f"{x['name']} {_days_text(x['days'])}" for x in w["pending"]))
        return None
    if guard is not None:
        # the collector's last read of a daily record (every slow refresh, ~15 min): a stopped
        # collector holds the card (STALE_AFTER_S). Not hist["t"]: that moves only once an hour
        reads = [x.get("t") for x in (hist.get("daily") or {}).values() if x.get("t")]
        age = now - max(reads) if reads else None
        why = guard.check(SLUG, {"total": w["total"]}, now, age)
        if why and (root / "index.html").exists():
            log.warning("weekly held on its last good card: %s", why)
            guard.save()
            return None
    d = root / w["slug"]
    render_card(w, d / "card.png")
    earlier = sorted((p.name for p in root.iterdir() if p.is_dir() and p.name != w["slug"]
                      and (p / "index.html").exists()), reverse=True)
    _write(d / "index.html", page(w, now, True, now, earlier))
    _write(root / "index.html", page(w, now, False, now, earlier))
    _write(out / "api" / "v1" / "weekly.json", json.dumps(_api(w, now), separators=(",", ":")))
    if guard is not None:
        guard.accept(SLUG, {"total": w["total"]}, {"rule": "weekly", "title": label(w), "sub": w["range"]}, now)
        guard.save()
    log.info("weekly %s: %s %s, %d venues, scope %s, change %s, left out %s", w["slug"], label(w),
             money(w["total"]), len(w["venues"]), w["scope"], change_text(w),
             ", ".join(x["name"] for x in w["left_out"]) or "none")
    return w


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", type=Path, required=True, help="directory holding venue_history.json, history.json")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--now", type=int, help="unix time to build for (default: now)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    now = args.now or int(time.time())
    hist = json.loads((args.api / "venue_history.json").read_text())
    try:  # our CC price readings, to convert Cantex's CC volume
        prices = json.loads((args.api / "history.json").read_text())
    except (OSError, ValueError):
        prices = {}
    cache_path = args.out / "weekly" / "days.json"
    try:
        cache = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cache = {}
    fetch_days(cache, prices, now)
    _write(cache_path, json.dumps(cache, separators=(",", ":")))
    try:  # every venue's daily volume for the last month, each day's records side by side
        _write(args.out / "api" / "v1" / "volume.json",
               json.dumps(volume_ledger.build(hist, cache, now, VENUES), separators=(",", ":")))
    except Exception:  # the weekly card does not wait on the ledger
        log.exception("volume ledger")
    build(args.out, hist, now, PublishGuard(args.out / "weekly" / "published.json"), cache)


if __name__ == "__main__":
    main()
