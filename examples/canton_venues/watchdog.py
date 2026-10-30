"""Watchdog: tell the owner when cantonvenues.com stops refreshing, and again when it is back.

Every ten minutes (canton-venues-watchdog.timer) this reads the live ``summary.json`` through the
public URL, so a dead collector, a stopped nginx and an expired certificate all show the same way:
no fresh snapshot. The collector writes a snapshot every five minutes and stamps it with ``t``; a
tick that fails keeps the old snapshot and records ``error`` and ``error_t`` in the same file.

- snapshot older than ``STALE_S`` (30 min), or the URL does not answer: ONE Telegram message to the
  owner's chat, with the collector's last error when there is one;
- still stale: a reminder every ``REMIND_S`` (6 h), not on every run;
- fresh again: one message saying how long the site was stale.

A core venue (Cantex, Tradecraft) that stops answering no longer stops the collector: it is left
out and ``summary.json`` names it in ``down_since``. Left out for over ``STALE_S`` is told the same
way (one message, reminders, one message when it is back); a new trouble is told at once. Since
2026-10-14 the same goes for: any other priced venue out (``optional_down``), Temple's keyed books
not read (``temple_priced``), an outside source past its age limit (``sources``: CoinGecko 30 min,
candles and Tradecraft or Temple volume 1 h, DefiLlama 6 h), and a tick that fails on two runs in a
row (``status: error`` with a fresh ``t``: summary.json is the tick's last write). Since 2026-10-15:
Temple's CC/USDCx book unread for 30 min, three failed ticks in an hour, a fresh snapshot without a
CC price or tokens; a trouble that clears while others stay sends nothing, and "whole again" waits for
a second clear run. Since 2026-10-30: a Temple market that traded in 24h with no book of it read
(``temple_unread``), so a new listing is told the day it trades, not found by hand 11 days later.

The bot and chat are the pair contact.py and crosscheck.py use (TELEGRAM_BOT_TOKEN, CONTACT_CHAT_ID
from the server's .env). What was last sent lives in ``--state`` (off the web root), so a restart
never repeats an alert. Written after the 13–14 Oct outage: a new empty Cantex pool failed every
tick for 22 hours and nothing told us.

    python watchdog.py --state /var/www/canton-venues-state/watchdog.json
    python watchdog.py --state ... --dry-run      # print, send nothing, keep the state
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from crosscheck import send

log = logging.getLogger("watchdog")

URL = "https://cantonvenues.com/api/v1/summary.json"
STALE_S = 30 * 60      # six missed five-minute ticks
REMIND_S = 6 * 3600    # while still stale, one reminder per six hours
# an outside source read longer ago than this is a trouble (summary.json ``sources``: last good read)
SOURCE_MAX_AGE_S = {"gecko": 30 * 60, "candles": 3600, "tc_volume": 3600, "temple_volume": 3600,
                    "llama": 6 * 3600, "temple_cc_book": 30 * 60}
SOURCE_NAME = {"gecko": "CoinGecko prices", "candles": "Cantex candles", "tc_volume": "Tradecraft volume",
               "temple_volume": "Temple settled volume", "llama": "DefiLlama TVL",
               "temple_cc_book": "Temple's CC/USDCx book (CC is priced by the pools alone without it)"}
# this many failed ticks in an hour (of about 12) is a trouble: the watchdog sees every other tick at
# most, so a tick failing every third time never failed "on two runs in a row" (2026-10-15 replay)
FLAKY_TICKS_1H = 3
# trouble keys that are not a venue (a core venue's key is its bare id)
NOT_VENUE = ("temple_book", "error", "flaky", "empty", "unreadable")
NAMES = {"poolparty": "Pool Party", "oneswap": "OneSwap", "rocky_perp": "Rocky perps"}


def _name(v: str) -> str:
    return NAMES.get(v, v.capitalize())


def _when(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%d %b %H:%M UTC").lstrip("0")


def _ago(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds < 7200 else f"{seconds / 3600:.1f} h"


def fetch(url: str = URL, now: datetime | None = None) -> tuple[dict | None, str | None]:
    """The live summary, or None and the reason it could not be read."""
    now = now or datetime.now(UTC)
    try:
        # ?t= skips any cache between us and the file, as the site's own polling does
        with urllib.request.urlopen(f"{url}?t={int(now.timestamp())}", timeout=20) as r:
            return json.loads(r.read()), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {getattr(exc, 'code', '') or exc}"[:200]


def troubles(summary: dict, now_s: float, error_seen: bool) -> dict[str, tuple[float, str]]:
    """What is wrong on a fresh snapshot, held past its limit: key -> (since, line). A core venue's key
    is its id; others are prefixed (``venue:``, ``source:``) or ``temple_book``, ``error``, ``flaky``,
    ``empty``, ``unreadable``. A summary of the wrong shape is a trouble, never a crash."""
    try:
        return _troubles(summary, now_s, error_seen)
    except Exception as exc:  # noqa: BLE001
        return {"unreadable": (now_s, f"The snapshot could not be read: {html.escape(type(exc).__name__)}.")}


def _troubles(summary: dict, now_s: float, error_seen: bool) -> dict[str, tuple[float, str]]:
    out: dict[str, tuple[float, str]] = {}
    for v, since in sorted((summary.get("down_since") or {}).items()):
        if now_s - since > STALE_S:
            out[v] = (since, f"{_name(v)} has not answered since {_when(since)}, {_ago(now_s - since)} ago. "
                             "The site runs without its prices.")
    for v, since in sorted((summary.get("optional_down") or {}).items()):
        if now_s - since > STALE_S:
            out[f"venue:{v}"] = (since, f"{_name(v)} has not been read since {_when(since)}, "
                                        f"{_ago(now_s - since)} ago.")
    sources = summary.get("sources") or {}
    book_t = sources.get("temple_book")
    if summary.get("temple_priced") is False and (book_t is None or now_s - book_t > STALE_S):
        why = summary.get("temple_error") or "its API or our key"
        out["temple_book"] = (book_t or now_s, f"Temple's order books are not read ({html.escape(str(why)[:200])}): "
                                               "Temple shows volume only"
                                               + (f", since {_when(book_t)}." if book_t else "."))
    # a Temple market that trades while we read no book of it (BETH/USDCx, $30M over 11 days from 19 Oct
    # 2026, seen by hand): it is in Temple's volume only, in no price, depth, comparison or exit
    for sym, v in sorted((summary.get("temple_unread") or {}).items()):
        quote = html.escape(sym.partition("/")[2] or "its quote token")
        out[f"unread:{sym}"] = (now_s, f"Temple's {html.escape(sym)} traded {v:,.0f} {quote} in 24h, a market "
                                       "we do not read: in no price, depth or exit.")
    for src, limit in SOURCE_MAX_AGE_S.items():
        if sources.get(src) is not None and now_s - sources[src] > limit:
            out[f"source:{src}"] = (sources[src], f"{SOURCE_NAME[src]} last read {_when(sources[src])}, "
                                                  f"{_ago(now_s - sources[src])} ago.")
    if summary.get("status") == "error" and error_seen:
        since = summary.get("error_t") or now_s
        out["error"] = (since, f"A tick failed on two runs in a row: {html.escape(str(summary.get('error'))[:300])}")
    elif (n := summary.get("failed_ticks_1h") or 0) >= FLAKY_TICKS_1H:
        last = summary.get("last_error") or {}
        out["flaky"] = (last.get("t") or now_s, f"{n} ticks failed in the last hour"
                        + (f", the last: {html.escape(str(last.get('error'))[:300])}" if last.get("error") else "."))
    # a fresh "ok" snapshot that carries nothing is not a working site
    if summary.get("status") == "ok" and (("cc_usd" in summary and not summary["cc_usd"])
                                           or ("tokens" in summary and not summary["tokens"])):
        out["empty"] = (now_s, "The snapshot is fresh but has no CC price or no tokens.")
    return out


def check(summary: dict | None, state: dict, now: datetime,
          fetch_error: str | None = None) -> tuple[str | None, dict]:
    """The message to send (None for none) and the state to keep."""
    now_s = now.timestamp()
    t = (summary or {}).get("t")
    fresh = t is not None and now_s - t <= STALE_S
    # a tick that failed on this run and the last one is a trouble even on a fresh snapshot
    erring = fresh and (summary or {}).get("status") == "error"
    down = troubles(summary or {}, now_s, bool(state.get("error_seen"))) if fresh else {}
    seen = {"error_seen": now_s} if erring else {}
    if fresh and not down:
        if not state.get("alerted"):
            return None, seen
        if state.get("kind") == "venues" and not state.get("clear_seen"):
            # clear once is not back: one more clear run first (a venue read once between two
            # 30-minute gaps paged "missing", "whole again", "missing" within 20 min)
            return None, {**state, "clear_seen": now_s, **seen}
        if state.get("kind") == "venues":
            keys = state.get("venues", [])
            if all(":" not in k and k not in NOT_VENUE for k in keys):
                names = ", ".join(_name(v) for v in keys)
                return (f"<b>cantonvenues.com has every venue again</b>\n{names} answering again, "
                        f"out for {_ago(now_s - state['since'])}."), seen
            return (f"<b>cantonvenues.com is whole again</b>\nEverything told since {_when(state['since'])} "
                    f"is fine again ({_ago(now_s - state['since'])})."), seen
        last = state.get("last_t")
        gap = f"no fresh snapshot for {_ago(t - last)} (since {_when(last)})" if last else \
            "the site had not answered"
        return f"<b>cantonvenues.com is fresh again</b>\nSnapshot {_when(t)}; {gap}.", {}

    kind = "venues" if fresh else "stale"
    alerted = state.get("alerted")
    # A reminder only for troubles already told; a new one (the site stops, another venue goes) is told
    # now. One that clears while others stay is not a message (it re-announced the rest as new).
    prev = state.get("venues", []) if state.get("kind", "stale") == kind else None
    same = prev is not None and set(down) <= set(prev)
    if alerted and same and now_s - alerted < REMIND_S:
        return None, ({k: v for k, v in state.items() if k != "clear_seen"} | {"venues": list(down)}
                      if fresh else state)
    still = "still " if alerted and same else ""
    if fresh:
        venue = any(":" not in k and k not in NOT_VENUE or k.startswith("venue:") for k in down)
        head = ("is {still}missing a venue" if venue else "is {still}failing a tick" if "error" in down
                else "is {still}short of data").format(still=still)
        lines = [f"<b>cantonvenues.com {head}</b>"] + [line for _, line in down.values()]
        return "\n".join(lines), {"alerted": now_s, "kind": "venues", "venues": list(down),
                                  "since": min(since for since, _ in down.values()), **seen}
    last = t if t is not None else state.get("last_t")
    lines = [f"<b>cantonvenues.com {'still not' if still else 'is not'} refreshing</b>"]
    if t is None:
        lines.append(f"The site did not answer: {html.escape(fetch_error or 'no snapshot time')}.")
    if last:
        lines.append(f"Last snapshot {_when(last)}, {_ago(now_s - last)} ago.")
    if summary and summary.get("status") == "error" and summary.get("error"):
        at = f" at {_when(summary['error_t'])}" if summary.get("error_t") else ""
        lines.append(f"Collector error{at}: {html.escape(str(summary['error'])[:300])}")
    return "\n".join(lines), {"alerted": now_s, "kind": "stale", "last_t": last}


def run(state_path: Path, now: datetime | None = None,
        get: Callable[[], tuple[dict | None, str | None]] = fetch,
        deliver: Callable[[str], bool] = send) -> str | None:
    """One check; sends at most one message and keeps the state for the next run."""
    now = now or datetime.now(UTC)
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    summary, error = get()
    text, new_state = check(summary, state, now, error)
    if text is None:
        log.info("nothing to send: %s", "fresh" if not new_state else "stale, already reported")
    elif deliver(text):
        log.info("sent: %s", text.splitlines()[0])
    else:
        # not delivered: keep the old state, so the next run tries again
        log.warning("not sent, will retry next run: %s", text.splitlines()[0])
        return None
    if new_state != state:
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(new_state))
        tmp.replace(state_path)
    return text


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--state", type=Path, required=True, help="what was last sent, kept between runs")
    ap.add_argument("--url", default=URL, help="the live summary.json")
    ap.add_argument("--dry-run", action="store_true", help="print the message, send nothing")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.dry_run:
        try:
            state = json.loads(args.state.read_text())
        except (OSError, ValueError):
            state = {}
        summary, error = fetch(args.url)
        text, _ = check(summary, state, datetime.now(UTC), error)
        print(text if text is not None else f"nothing to send; snapshot {_when((summary or {}).get('t', 0))}")
        return
    run(args.state, get=lambda: fetch(args.url))


if __name__ == "__main__":
    main()
