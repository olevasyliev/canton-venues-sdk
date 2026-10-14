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
way (one message, reminders, one message when it is back); a new trouble is told at once.

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


def check(summary: dict | None, state: dict, now: datetime,
          fetch_error: str | None = None) -> tuple[str | None, dict]:
    """The message to send (None for none) and the state to keep."""
    now_s = now.timestamp()
    t = (summary or {}).get("t")
    fresh = t is not None and now_s - t <= STALE_S
    # a core venue the collector has left out for over STALE_S (summary.json ``down_since``)
    down = {v: since for v, since in sorted(((summary or {}).get("down_since") or {}).items())
            if now_s - since > STALE_S} if fresh else {}
    if fresh and not down:
        if not state.get("alerted"):
            return None, {}
        if state.get("kind") == "venues":
            names = ", ".join(v.capitalize() for v in state.get("venues", []))
            return (f"<b>cantonvenues.com has every venue again</b>\n{names} answering again, "
                    f"out for {_ago(now_s - state['since'])}."), {}
        last = state.get("last_t")
        gap = f"no fresh snapshot for {_ago(t - last)} (since {_when(last)})" if last else \
            "the site had not answered"
        return f"<b>cantonvenues.com is fresh again</b>\nSnapshot {_when(t)}; {gap}.", {}

    kind = "venues" if fresh else "stale"
    alerted = state.get("alerted")
    # a reminder only for the same trouble; a new one (the site stops, another venue goes) is told now
    same = state.get("kind", "stale") == kind and state.get("venues", []) == list(down)
    if alerted and same and now_s - alerted < REMIND_S:
        return None, state
    still = "still " if alerted and same else ""
    if fresh:
        lines = [f"<b>cantonvenues.com is {still}missing a venue</b>"]
        for v, since in down.items():
            lines.append(f"{v.capitalize()} has not answered since {_when(since)}, {_ago(now_s - since)} ago. "
                         "The site runs without its prices.")
        return "\n".join(lines), {"alerted": now_s, "kind": "venues", "venues": list(down),
                                  "since": min(down.values())}
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
