"""The X post of the day for @cantonvenues, sent to the founder's chat for him to post by hand.

X's API has no free tier (every post is paid for, one with a link 13 times more: docs.x.com pricing, read
11 Nov 2026), so the post is written here and the founder posts it: the card when there is one, the
text, then the link as the first reply. One a day, every day:

    Mon  the weekly card: the week's spot volume and each venue's share
    Wed  where a CC trade got the best price over the week, after fees
    Fri  stablecoins against $1 over the week
    Tue, Thu, Sat, Sun  one venue's card, venues in turn

The numbers and the honesty rules are the channel's (channel.py): a price claim only on a passing
quote check and naming only checked venues. A day whose post cannot be built (a held weekly card, no
passing check, too little history) gets the next venue's card instead; with no card either, nothing.
Text is plain, under X's 280-character limit as X counts it, venues tagged by their own accounts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import channel
from channel import SITE, money, signed

SEND_HOUR_UTC = channel.SEND_HOUR_UTC
SCHEDULE = {0: "weekly", 1: "venue", 2: "execution", 3: "venue", 4: "pegs", 5: "venue", 6: "venue"}
LIMIT = 280
# each venue's own account on X, by site slug and by the key the hourly samples use
HANDLES = {"temple": "@temple_ny", "cantex": "@cantex_io", "rocky": "@Rocky_exchange", "tradecraft": "@TradecraftFi",
           "ekiden": "@ekidenfi", "oneswap": "@Oneswapcc", "pool-party": "@PoolPartyDEX", "poolparty": "@PoolPartyDEX",
           "canborsa": "@Canborsa_DEX"}
SHORT_SITE = SITE.removeprefix("https://")
# X counts a link as 23 characters whatever its length; these are the links a post here can carry
LINK = re.compile(r"(?:https?://)?(?:[\w-]+\.)+(?:com|io|fi|cc|xyz|org|net)(?:/\S*)?", re.I)
# X's weighted count (twitter-text v3): these code points weigh 1, every other one 2
LIGHT = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))
LOWER_FIRST = {"The", "Best"}   # a venue headline read after "Temple (@temple_ny): "
RIGHT_NOW = {"best_count", "best_quote"}  # headlines about this hour's prices


@dataclass(frozen=True)
class XPost:
    kind: str
    text: str                 # the post, plain text
    reply: str                # the first reply: where the numbers live
    photo: str | None = None  # the card's URL, for Telegram to fetch


def x_len(text: str) -> int:
    """The length X holds against its 280 limit."""
    n = 23 * len(LINK.findall(text))
    for ch in LINK.sub("", text):
        n += 1 if any(lo <= ord(ch) <= hi for lo, hi in LIGHT) else 2
    return n


def _fits(*texts: str) -> bool:
    return all(x_len(t) <= LIMIT for t in texts)


def _join(head: list[str], items: list[str], tail: list[str]) -> str:
    """Head, the list, tail; list lines dropped from the end until it fits (at least one kept)."""
    items = list(items)
    while True:
        text = "\n".join(head + items + tail).strip()
        if x_len(text) <= LIMIT or len(items) <= 1:
            return text
        items.pop()


def tag(key: str, name: str | None = None) -> str:
    return HANDLES.get(key) or name or channel.NAMES.get(key, key)


def weekly(w: dict | None, now: datetime) -> XPost | None:
    """The seven days that closed at midnight on Sunday (Monday's post); None for a held card."""
    if not w or w.get("slug") != (now.date() - timedelta(days=1)).isoformat():
        return None
    when = w["range"].removesuffix(f" {now.year}")
    line = f"Spot volume on Canton venues, {when}: {money(w['total_usd'])}"
    if w.get("change") is not None:
        line += f", {'up' if w['change'] >= 0 else 'down'} {abs(w['change']) * 100:.1f}% on the same venues the week before"
    shares = [f"{tag(v.get('slug', ''), v['name'])} {s}" for v, s in zip(w["venues"], w.get("shares_shown") or [])]
    out = [x["name"] for x in w.get("left_out") or []]
    tail = [f"\nNot counted: {', '.join(out)}, without a full week of daily figures."] if out else []
    text = _join([line + ".", ""], [", ".join(shares[:4]) + "."] if shares else [], tail)
    reply = f"Every venue by day, and how each figure is read: {SHORT_SITE}/weekly/{w['slug']}/"
    photo = f"{SITE}/weekly/{w['slug']}/card.png?v={w['t']}"
    return XPost("weekly", text, reply, photo) if _fits(text, reply) else None


def venue(published: dict, turn: int, card_v: dict[str, int],
          checked: set[str] | None = None) -> tuple[XPost | None, int]:
    """The next venue's card (``channel.pick_venue``), and the turn after it."""
    v, turn = channel.pick_venue(published, turn, card_v, checked)
    if v is None:
        return None, turn
    name, slug, head = v["name"], v["slug"], v["head"]
    who = f"{name} ({HANDLES[slug]})" if slug in HANDLES else name
    sub = head["sub"].rstrip(".")
    if head["title"] == f"{name} on Canton":
        text = f"{who} on Canton: {sub}."
    else:
        first, _, rest = head["title"].partition(" ")
        title = f"{first.lower()} {rest}" if first in LOWER_FIRST else head["title"]
        if head.get("rule") in RIGHT_NOW:
            title += " right now"
        text = f"{who}: {title}. {sub}."
    reply = f"{name} on Canton Venues, live: {SHORT_SITE}/venues/{slug}/"
    return (XPost("venue", text, reply, v["photo"]) if _fits(text, reply) else None), turn


def execution(samples: list[dict], checked: set[str] | None = None) -> XPost | None:
    """Wednesday's post: the week's best execution (``channel.execution_stats``)."""
    st = channel.execution_stats(samples, checked)
    if st is None:
        return None
    head = [f"Where a CC trade for USDCx got the best price on Canton, {st['span']}, after fees. "
            "Share of hours by size:", ""]
    rows = [f"{money(size)}: " + ", ".join(f"{tag(v)} {pct}%" for v, pct in ranked[:3] if pct)
            for size, ranked, _ in st["rows"]]
    text = _join(head, rows, [])
    edges = ", ".join(f"{edge:.2f}% less at {money(size)}" for size, _, edge in st["rows"])
    note = channel.assumed_fee_note(st).removeprefix("; ")
    reply = (f"The runner-up returned a median {edges}. Buys and sells checked every hour, pool fees, price impact"
             f" and network fees included{'. ' + note[0].upper() + note[1:] if note else ''}.\n\n"
             f"Live: {SHORT_SITE}/#execution")
    if not _fits(reply):
        reply = f"The runner-up returned a median {edges}.\n\nLive: {SHORT_SITE}/#execution"
    return XPost("execution", text, reply) if _fits(text, reply) else None


def pegs(history: dict, tokens: list[dict], symbols: dict[str, str], now: int) -> XPost | None:
    """Friday's post: stablecoins against $1 over the week (``channel.pegs_stats``)."""
    st = channel.pegs_stats(history, tokens, symbols, now)
    if st is None or not st["stable"]:
        return None
    rows = []  # the furthest from $1 first: what a trim to 280 drops is the coins that held
    for inside, name, worst, _ in sorted(st["stable"], key=lambda x: (x[0], -abs(x[2]), x[1])):
        rows.append(f"{name} {inside * 100:.0f}%" + (f", furthest {signed(worst, 2)} for 30 min"
                                                      if abs(worst) > channel.PEG_BAND else ""))
    # the price in Canton's pools and books, not the coin's own peg: the line says where it was read
    text = _join([f"Stablecoin prices in Canton pools and order books, {st['span']}: "
                  "the share of time within 0.5% of $1.", ""], rows, [])
    refs = [f"{name} {signed(p, 2)} vs {label}" for name, label, p in st["ref"]]
    tail = f"Read every five minutes: {SHORT_SITE}/#premium"
    reply = tail
    for k in range(len(refs), 0, -1):  # as many outside-price lines as fit, furthest first
        if _fits(r := f"Against outside prices, median: {'; '.join(refs[:k])}.\n\n{tail}"):
            reply = r
            break
    return XPost("pegs", text, reply) if _fits(text, reply) else None


def due(state: dict, now: datetime) -> bool:
    """Once a day, on the first tick at or after the send hour."""
    return now.hour >= SEND_HOUR_UTC and state.get("date") != now.date().isoformat()


def build(kind: str, src: dict, state: dict, checked: set[str]) -> XPost | None:
    """Today's post: ``kind`` when it can be built, else the next venue's card. ``src`` holds the inputs
    (weekly, published, card_v, samples, history, tokens, now); ``state["venue_turn"]`` moves on a venue post."""
    post = None
    if kind in channel.PRICE_KINDS and not checked:
        kind = "venue"
    if kind == "weekly":
        post = weekly(src.get("weekly"), src["now"])
    elif kind == "execution":
        post = execution(src.get("samples") or [], checked)
    elif kind == "pegs":
        tokens = src.get("tokens") or []
        post = pegs(src.get("history") or {}, tokens, {x["key"]: x["symbol"] for x in tokens},
                    int(src["now"].timestamp()))
    if post is None:
        post, state["venue_turn"] = venue(src.get("published") or {}, state.get("venue_turn", 0),
                                          src.get("card_v") or {}, checked)
    return post


def messages(post: XPost, now: datetime) -> list[tuple[str, str | None]]:
    """What the founder's chat gets, in order, as (text, photo): what to do, the post, the reply. The
    post and the reply are messages of their own so each copies whole."""
    day = now.strftime("%a %-d %b")
    if post.photo:
        how = (f"X post for {day}. Save this card and post it with the next message as the text, "
               "then add the message after it as the first reply.")
    else:
        how = (f"X post for {day}, text only: post the next message, "
               "then add the message after it as the first reply.")
    return [(how, post.photo), (post.text, None), (post.reply, None)]
