"""The market as it stood at every tick, kept so a trade can be priced again afterwards.

One gzip member per tick in ``<dir>/YYYY-MM-DD.jsonl.gz`` (UTC day), each a JSON line starting
``{"t":``: the pricing state a quote reads (pools.json's CC price, CC pools, order books, other
pools and the pools marked not trading), every token's symbol and dollar price, and the network fee
table the tick netted with. ``around`` finds the readings either side of a moment, and ``model.venue_quotes``
under ``model.fees_from`` prices any trade on them exactly as a live quote is priced. About 6 KB a
tick compressed, under 2 MB a day. Kept off the web root, like the hourly best-execution record.
"""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

KEEP_DAYS = 400
DAY = 86400


def _name(t: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(t)) + ".jsonl.gz"


def reading(pools: dict, tokens: list[dict], fees: dict) -> dict:
    """One tick's reading: ``pools`` is pools.json as written, ``tokens`` tokens.json's list (each
    token's symbol and dollar price are kept), ``fees`` ``model.fee_table_json()`` at that tick."""
    return {"t": int(pools["t"]), "cc_usd": pools["cc_usd"], "pools": pools["pools"],
            "books": pools.get("books") or {}, "other_pools": pools.get("other_pools") or [],
            "not_trading": pools.get("not_trading") or [],
            "tokens": {x["key"]: [x["symbol"], x.get("price_usd")] for x in tokens}, "fees": fees}


def record(directory: Path, snap: dict) -> None:
    """Append one reading to its day's file (a new gzip member: a crash loses at most this one)."""
    directory.mkdir(parents=True, exist_ok=True)
    line = json.dumps(snap, separators=(",", ":")) + "\n"
    with gzip.open(directory / _name(snap["t"]), "ab") as f:
        f.write(line.encode())


def prune(directory: Path, now: int, keep_days: int = KEEP_DAYS) -> None:
    """Drop day files older than ``keep_days``."""
    cut = _name(now - keep_days * DAY)
    for p in directory.glob("*.jsonl.gz"):
        if p.name < cut:
            p.unlink()


def _lines(path: Path) -> list[bytes]:
    """A day's readings in order. A member cut short by a crash ends the day there: what precedes it
    counts (read line by line, so the error comes after the good lines)."""
    out: list[bytes] = []
    try:
        with gzip.open(path, "rb") as f:
            for line in f:
                if line.endswith(b"}\n"):
                    out.append(line[:-1])
    except FileNotFoundError:
        return []
    except (OSError, EOFError):
        pass
    return out


def _t(line: bytes) -> int | None:
    # every line starts {"t":<unix seconds>, so the time is read without parsing the whole reading
    head = line[:24]
    if not head.startswith(b'{"t":'):
        return None
    try:
        return int(head[5:head.index(b",")])
    except ValueError:
        return None


def around(directory: Path, t: int, max_gap_s: int) -> tuple[dict | None, dict | None]:
    """(the last reading at or before ``t``, the first after it), each only when within ``max_gap_s``
    of ``t``."""
    lines = []
    for day in (t - DAY, t, t + DAY):
        lines += _lines(directory / _name(day))
    before = after = None
    for line in lines:
        x = _t(line)
        if x is None or abs(x - t) > max_gap_s:
            continue
        if x <= t and (before is None or x > before[0]):
            before = (x, line)
        elif x > t and (after is None or x < after[0]):
            after = (x, line)
    return tuple(json.loads(b[1]) if b else None for b in (before, after))


def first(directory: Path) -> int | None:
    """The time of the earliest reading kept, or None when there is none."""
    days = sorted(directory.glob("*.jsonl.gz"))
    for p in days:
        for line in _lines(p):
            if (x := _t(line)) is not None:
                return x
    return None
