"""The Telegram channel's morning post: a different kind each weekday, built from our own records.

    Mon  the weekly card (weekly.py) with its total and venue shares
    Tue  one venue's share card and headline, venues in turn
    Wed  where a CC trade got the best price over the week, net of each venue's network fee
    Thu  the next venue's card
    Fri  stablecoins and world-price premiums over the week
    Sat, Sun  nothing

Each builder returns a ``Post`` or None when its data is not there (a held weekly card, a week of
history not yet recorded); a day with nothing to say stays quiet rather than falling back to a
template. No DefiLlama figure appears in any post.
"""

from __future__ import annotations

import html
import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from model import REFERENCES, STABLES, idle
from venue_pages import VENUES

SITE = "https://cantonvenues.com"
SEND_HOUR_UTC = 9
# weekday() -> kind; Saturday and Sunday are left out
SCHEDULE = {0: "weekly", 1: "venue", 2: "execution", 3: "venue", 4: "pegs"}
# F 2026-10-15: until our quotes are checked every day against the venues' own quote APIs, the channel
# says only what a venue's own API confirms (volume, liquidity, depth, counts). No best-price, spread,
# peg or premium post or alert: the Wednesday execution and Friday pegs posts are skipped, a venue card
# whose headline is a price claim is passed over, and loud alerts stay on the site. False when that
# check runs.
PRICE_CLAIMS_PAUSED = True
PRICE_KINDS = {"execution", "pegs"}
CHECKABLE_LEADS = {"spot_volume", "kind_volume", "perp_volume", "tvl", "tokens", "pool_count", "perp_markets",
                   "largest_pool", "pool_tvl", "token_depth", "unique_token", "pair_volume", "pools", "read"}
MIN_LIQUIDITY_USD = 100_000   # a stablecoin or token below this is not news for the channel
PEG_BAND = 0.005
HELD_SAMPLES = 6              # five-minute readings: a peg gap counts once it holds 30 minutes
WEEK_S = 7 * 86400
MIN_EXEC_HOURS = 24           # less history than this and the execution post waits
EXEC_PAIR = "USDCX"           # CC against USDCx, the pair every CC venue quotes
EXEC_SIZES = (1_000, 10_000, 50_000)
NAMES = {"cantex": "Cantex", "tradecraft": "Tradecraft", "rocky": "Rocky", "temple": "Temple",
         "oneswap": "OneSwap", "poolparty": "Pool Party", "ekiden": "Ekiden"}


@dataclass(frozen=True)
class Post:
    kind: str
    html: str                 # Telegram HTML; a photo's caption when ``photo`` is set
    photo: str | None = None  # an image URL Telegram fetches itself


def due(state: dict, now: datetime) -> str | None:
    """Today's kind, once, on the first tick at or after the send hour; None otherwise."""
    if now.hour < SEND_HOUR_UTC or state.get("date") == now.date().isoformat():
        return None
    kind = SCHEDULE.get(now.weekday())
    return None if PRICE_CLAIMS_PAUSED and kind in PRICE_KINDS else kind


def signed(f: float, d: int = 1) -> str:
    return f"{'+' if f > 0 else ''}{f * 100:.{d}f}%".replace("-", "−")


def money(v: float) -> str:
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def _day(t: int) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%-d %b %H:%M UTC")


def _span(seconds: float) -> str:
    days = round(seconds / 86400)
    return "last 7 days" if seconds >= 6.5 * 86400 else "last 24 hours" if days <= 1 else f"last {days} days"


def held_worst(pts: list[tuple[int, float]], n: int) -> tuple[int, float]:
    """The furthest from zero a reading stayed for ``n`` samples in a row (its start, its smallest
    size in that run): one odd five-minute reading is not a depeg."""
    best = (pts[0][0], 0.0)
    for i in range(len(pts) - n + 1):
        run = [v for _, v in pts[i:i + n]]
        if all(v > 0 for v in run) or all(v < 0 for v in run):
            v = min(run, key=abs)
            if abs(v) > abs(best[1]):
                best = (pts[i][0], v)
    return best


# === Monday: the weekly card ===============================================

def weekly_post(w: dict | None, now: datetime) -> Post | None:
    """The card for the seven days that closed at midnight; None when it is not that week's (held)."""
    if not w or w.get("slug") != (now.date() - timedelta(days=1)).isoformat():
        return None
    lines = [f"📅 <b>{w['label']}</b>, {w['range']}", f"<b>{money(w['total_usd'])}</b>"
             + (f" ({signed(w['change'])} on the week before)" if w.get("change") is not None else "")]
    shown = w.get("shares_shown") or []
    parts = [f"{v['name']} {s}" for v, s in zip(w["venues"], shown)][:4]
    if parts:
        lines.append(", ".join(parts))
    lines += ["", f'🔗 <a href="{SITE}/weekly/{w["slug"]}/">The week, day by day</a>']
    return Post("weekly", "\n".join(lines), f"{SITE}/weekly/{w['slug']}/card.png?v={w['t']}")


# === Tuesday, Thursday: one venue ==========================================

def venue_post(published: dict, turn: int, card_v: dict[str, int]) -> tuple[Post | None, int]:
    """The next venue, in the site's order, that has a published headline and card, and the turn after it."""
    order = [v["slug"] for v in VENUES]
    heads = (published or {}).get("venues") or {}
    for i in range(len(order)):
        slug = order[(turn + i) % len(order)]
        head = (heads.get(slug) or {}).get("head")
        if not head or slug not in card_v or (PRICE_CLAIMS_PAUSED and head.get("rule") not in CHECKABLE_LEADS):
            continue
        name = next(v["name"] for v in VENUES if v["slug"] == slug)
        html = "\n".join([f"🏛 <b>{name}</b>: {head['title']}", head["sub"], "",
                          f'🔗 <a href="{SITE}/venues/{slug}/">{name} on Canton Venues</a>'])
        return (Post("venue", html, f"{SITE}/venues/{slug}/card.png?v={card_v[slug]}"),
                (turn + i + 1) % len(order))
    return None, turn


# === Wednesday: best execution over the week ===============================

def read_exec(directory: Path, now: int) -> list[dict]:
    """The hourly samples (exec_history.py) of the last seven days."""
    out = []
    for k in range(8):
        path = directory / (datetime.fromtimestamp(now - k * 86400, UTC).strftime("%Y-%m-%d") + ".jsonl")
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            s = json.loads(line)
            if s["t"] >= now - WEEK_S:
                out.append(s)
    return sorted(out, key=lambda s: s["t"])


def execution_post(samples: list[dict]) -> Post | None:
    """For a CC order at each size: which venue returned the most, net of network fees, how often."""
    if len(samples) < MIN_EXEC_HOURS:
        return None
    wins: dict[int, Counter] = defaultdict(Counter)
    edges: dict[int, list[float]] = defaultdict(list)
    for s in samples:
        for pair, _side, size, _best, _edge, best_net, edge_net in s["rows"]:
            if pair == EXEC_PAIR and size in EXEC_SIZES and best_net:
                wins[size][best_net] += 1
                edges[size].append(edge_net)
    if not wins:
        return None
    span = _span(samples[-1]["t"] - samples[0]["t"])
    # the venues compared on the pair whose network fee is assumed, named in the closing line
    fees = samples[-1].get("network_fee") or {}
    compared = {v for s in samples for v in (s.get("venues") or {}).get(EXEC_PAIR, [])}
    assumed = sorted(NAMES.get(v, v) for v, f in fees.items()
                     if f.get("basis") == "assumed" and (v in compared or not compared))
    lines = [f"🏁 <b>Where a CC trade got the best price</b>, {span}", ""]
    for size in EXEC_SIZES:
        c = wins.get(size)
        if not c:
            continue
        n = sum(c.values())
        ranked = c.most_common()
        top, k = ranked[0]
        line = f"<b>{money(size)}</b>: {NAMES.get(top, top)} {k * 100 // n}% of the time"
        if len(ranked) > 1:
            line += ", " + ", ".join(f"{NAMES.get(v, v)} {x * 100 // n}%" for v, x in ranked[1:3])
        line += f". The next venue returned a median {statistics.median(edges[size]) / 100:.2f}% less"
        lines.append(line)
    note = ("Buys and sells of CC for USDCx, checked every hour. Pool fees, price impact and each venue's "
            "network fee included")
    if assumed:
        n = (", ".join(assumed[:-1]) + " and " + assumed[-1]) if len(assumed) > 1 else assumed[0]
        each = next(f for f in fees.values() if f.get("basis") == "assumed")
        note += f"; {n} publish no network fee, so theirs is assumed at {each['point']:g} {each['unit'].upper()}"
    lines += ["", note + ".", f'🔗 <a href="{SITE}/#execution">Live best execution</a>']
    return Post("execution", "\n".join(lines))


# === Friday: pegs and premiums over the week ===============================

def pegs_post(history: dict, tokens: list[dict], symbols: dict[str, str], now: int) -> Post | None:
    """Stablecoins: share of the week within 0.5% of $1 and the worst reading. Reference assets: the
    median premium to their outside price. Only tokens with ``MIN_LIQUIDITY_USD`` in pools today that
    traded $1K or more in the last 24h (a pool that barely trades only retells CC's moves). A premium is read
    only from its last method step on (history "steps"), and the line says since when."""
    ts = history.get("t") or []
    lo = next((i for i, t in enumerate(ts) if t >= now - WEEK_S), None)
    if lo is None or not ts or ts[-1] - ts[lo] < 3 * 86400:
        return None
    liq = {t["key"]: t.get("liquidity_usd") or 0 for t in tokens}
    quiet = idle(tokens)
    since: dict[str, int] = {}
    for s in history.get("steps") or []:
        if s.get("premium"):
            for k in s["keys"]:
                since[k] = max(since.get(k, 0), s["t"])
    span = _span(ts[-1] - ts[lo])
    stable, ref = [], []
    for key, col in (history.get("premium") or {}).items():
        pts = [(t, v) for t, v in zip(ts[lo:], col[lo:]) if v is not None and t >= since.get(key, 0)]
        if len(pts) < 100 or key in quiet:
            continue
        name = symbols.get(key, key)
        if key in STABLES and liq.get(key, 0) >= MIN_LIQUIDITY_USD:
            inside = sum(abs(v) <= PEG_BAND for _, v in pts) / len(pts)
            worst_t, worst = held_worst(pts, HELD_SAMPLES)
            stable.append((inside, name, worst, worst_t))
        elif key in REFERENCES and (key == "CC" or liq.get(key, 0) >= MIN_LIQUIDITY_USD):
            label = REFERENCES[key][1]
            if since.get(key, 0) > ts[lo] + 86400:  # a shorter run than the post's span
                label += f" (since {datetime.fromtimestamp(since[key], UTC).strftime('%-d %b')})"
            ref.append((name, label, statistics.median(v for _, v in pts)))
    if not stable and not ref:
        return None
    lines = [f"💵 <b>Stablecoins on Canton</b>, {span}"]
    for inside, name, worst, worst_t in sorted(stable, key=lambda x: (-x[0], x[1])):
        line = f"{html.escape(name)}: within 0.5% of $1 {inside * 100:.0f}% of the time"  # Telegram HTML
        if abs(worst) > PEG_BAND:
            line += f", furthest {signed(worst, 2)} held 30 min ({_day(worst_t)})"
        lines.append(line)
    if ref:
        lines += ["", "🌍 <b>Against outside prices</b>, median"]
        lines += [f"{html.escape(name)} {signed(p, 2)} vs {html.escape(label)}"
                  for name, label, p in sorted(ref, key=lambda x: -abs(x[2]))]
    lines += ["", "Read every five minutes from Canton pools and order books.",
              f'🔗 <a href="{SITE}/#premium">Live premiums</a>']
    return Post("pegs", "\n".join(lines))


# === preview ===============================================================

def preview(api: Path, out: Path, state: Path, now: int) -> list[Post]:
    """Every kind built from today's data, whatever the weekday; the execution post with whatever
    history there is. For a look at the formats, never for the channel."""
    def load(p: Path, default):
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return default

    tokens = load(api / "tokens.json", {}).get("tokens", [])
    w = load(api / "weekly.json", None)
    card_v = {p.parent.name: int(p.stat().st_mtime) for p in (out / "venues").glob("*/card.png")}
    published = load(out / "venues" / "published.json", {})
    posts = []
    if w:
        posts.append(weekly_post(w, datetime.fromisoformat(w["slug"]).replace(tzinfo=UTC) + timedelta(days=1)))
    turn = 0
    for _ in range(2):
        post, turn = venue_post(published, turn, card_v)
        posts.append(post)
    samples = read_exec(state / "exec", now)
    posts.append(execution_post(samples) if len(samples) >= MIN_EXEC_HOURS else
                 execution_post(samples * MIN_EXEC_HOURS) if samples else None)
    posts.append(pegs_post(load(api / "history.json", {}), tokens, {t["key"]: t["symbol"] for t in tokens}, now))
    return [p for p in posts if p]


def main() -> None:
    """Send ``preview`` to the founder's chat (CONTACT_CHAT_ID, the contact form's), not the channel.

        python channel.py --api /var/www/canton-venues/api/v1 --out /var/www/canton-venues
    """
    import argparse
    import os
    import time

    import httpx

    ap = argparse.ArgumentParser(description="Send one of each channel post to the founder's chat.")
    ap.add_argument("--api", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--state", type=Path, help="default: <out>-state")
    args = ap.parse_args()
    state = args.state or args.out.with_name(args.out.name + "-state")
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("CONTACT_CHAT_ID")
    if not (token and chat):
        raise SystemExit("TELEGRAM_BOT_TOKEN and CONTACT_CHAT_ID are needed")
    for post in preview(args.api, args.out, state, int(time.time())):
        body = f"[preview: {post.kind}]\n\n{post.html}"
        # the URL carries the bot token: never print it or the response
        if post.photo:
            resp = httpx.post(f"https://api.telegram.org/bot{token}/sendPhoto", data={
                "chat_id": chat, "photo": post.photo, "caption": body, "parse_mode": "HTML"}, timeout=30)
        else:
            resp = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", data={
                "chat_id": chat, "text": body, "parse_mode": "HTML", "disable_web_page_preview": "true"}, timeout=30)
        print(post.kind, resp.status_code)


if __name__ == "__main__":
    main()
