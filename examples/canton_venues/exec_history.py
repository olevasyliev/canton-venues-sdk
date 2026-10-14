"""Hourly record of best execution on Canton: for every pair, side and size, which venue returned the
most and by how much, before and after each venue's network fee.

One JSON line per sample in ``<dir>/YYYY-MM-DD.jsonl`` (UTC day), at most one sample an hour. It is
kept off the web root: the live snapshot is public (api/v1/execution.json), the series is ours. Each
line carries the fee table it was netted with, so a later fee correction can be told apart from a
change in the market.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

STEP_S = 3600


def _last_t(path: Path) -> int | None:
    if not path.exists():
        return None
    with path.open("rb") as f:
        lines = f.read().splitlines()
    return json.loads(lines[-1])["t"] if lines else None


def sample(now: int, pairs: list[dict], fees: dict) -> dict:
    """One line: ``rows`` as [pair, side, size, best, edge_bps, best_net, edge_net_bps], and the
    venues each pair compared (from 2026-10-14)."""
    rows = []
    for p in pairs:
        for r in p["rows"]:
            rows.append([p["key"], r["side"], r["size_usd"], r["best"], round(r["edge_bps"], 2),
                         r.get("best_net"), round(r.get("edge_net_bps") or 0.0, 2)])
    return {"t": int(now), "network_fee": fees, "rows": rows,
            "venues": {p["key"]: sorted(p.get("venues") or []) for p in pairs}}


def record(directory: Path, now: int, pairs: list[dict], fees: dict) -> bool:
    """Append a sample when the last one is an hour old or more. True when one was written."""
    path = directory / (time.strftime("%Y-%m-%d", time.gmtime(now)) + ".jsonl")
    last = _last_t(path)
    if last is None:  # a new day: the last sample may sit in yesterday's file
        last = _last_t(directory / (time.strftime("%Y-%m-%d", time.gmtime(now - 86400)) + ".jsonl"))
    if last is not None and now - last < STEP_S:
        return False
    directory.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(sample(now, pairs, fees), separators=(",", ":")) + "\n")
    return True
