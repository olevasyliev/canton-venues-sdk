"""Quote check: what our pool math says an AMM returns, against what the venue's own quote engine says,
at the same minute. Every hour (canton-venues-quotecheck.timer): Cantex's fees move 25% within an hour.

- Tradecraft quotes without a key (``/quoteForFixedInput``).
- Cantex quotes through its SDK with our account's keys (CANTEX_OPERATOR_KEY, CANTEX_TRADING_KEY from
  the server's .env; quote calls only, nothing is traded). Its quote also names the network fee of each
  pair, the same at any size and both ways: those fees go to ``<state>/quote_check.json``, which the
  collector reads (``model.PAIR_FEE_CC``). Without the keys Cantex is reported unchecked.
- OneSwap's quote needs an SDK key we do not have and Pool Party has no quote: unchecked, and said so.
  Temple and Rocky are order books: the book we walk is the venue's own data.

Each CC pool holding $20K or more is asked at $100, $1K and $10K, both ways, and priced by our formula
(``model.FORMULAS``) on reserves read the same minute. A gap over ``GAP_BP`` is asked again once (a
pool can trade between the two reads); one that holds is a failure. Written: ``api/v1/quotecheck.json``
(public: when, what was checked, every gap) and the fees for the collector. ONE Telegram message to the
owner (the pair contact.py and crosscheck.py use) only when a gap holds or a venue does not answer.

    python quotecheck.py --api /var/www/canton-venues/api/v1 --state /var/www/canton-venues-state
    python quotecheck.py ... --dry-run      # print, write nothing, send nothing
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import statistics
import time
from decimal import Decimal
from pathlib import Path

import model as m
from crosscheck import send

from cantonvenues import CantexPublicData, TradecraftAdapter

log = logging.getLogger("quotecheck")

SIZES_USD = (100, 1_000, 10_000)
MIN_POOL_USD = 20_000   # a pool this deep or more is checked (twice its CC side)
GAP_BP = Decimal(1)     # ours and the venue's agree to the last digit when the math is the same
MAX_POOLS = 12          # per venue, the deepest first
NO_KEYS = "no Cantex keys on this machine"
UNCHECKED = {"oneswap": "its quote needs an SDK key we do not have",
             "poolparty": "it publishes no quote",
             "temple": "an order book: the book we walk is its own",
             "rocky": "an order book: the book we walk is its own"}


def _gap_bp(ours: Decimal, theirs: Decimal) -> float | None:
    return float((ours / theirs - 1) * 10_000) if theirs > 0 else None


async def _tradecraft(cc_usd: Decimal) -> list[dict]:
    a = TradecraftAdapter()
    await a.connect()
    try:
        async def read():
            out = {}
            for st in await a.pool_states():
                if m.CC not in (st.token_a, st.token_b):
                    continue
                tok = st.token_b if st.token_a == m.CC else st.token_a
                cc_res, tok_res = st.reserves_for(m.CC, tok)
                if 2 * cc_res * cc_usd >= MIN_POOL_USD and tok_res > 0:
                    out[tok] = (cc_res, tok_res, st.total_fee)
            return out

        async def quote(tok, sell_cc, amount):
            q = await a.quote_symbols(amount, m.CC if sell_cc else tok, tok if sell_cc else m.CC)
            return q.returned_amount, None
        return await _check("tradecraft", read, quote, "tradecraft", cc_usd)
    finally:
        await a.close()


async def _cantex(cc_usd: Decimal) -> tuple[list[dict], dict[str, Decimal]]:
    from cantonvenues.venues.cantex import CantexAdapter
    pub = CantexPublicData()
    await pub.connect()
    a = CantexAdapter()
    await a.connect()
    fees: dict[str, Decimal] = {}
    inst: dict[str, tuple] = {}
    try:
        async def read():
            states = await pub.pool_states()
            cc = next(s.token_a if s.symbol_a == m.CC else s.token_b for s in states if m.CC in (s.symbol_a, s.symbol_b))
            out = {}
            for s in states:  # the deepest pool per token, as the collector quotes it
                if cc not in (s.token_a, s.token_b):
                    continue
                cc_is_a = s.token_a == cc
                tok_inst, tok = (s.token_b, s.symbol_b) if cc_is_a else (s.token_a, s.symbol_a)
                cc_res, tok_res = (s.reserve_a, s.reserve_b) if cc_is_a else (s.reserve_b, s.reserve_a)
                if tok_res <= 0 or (tok in out and out[tok][0] >= cc_res):
                    continue
                inst[tok] = (cc, tok_inst)
                out[tok] = (cc_res, tok_res, s.fee)
            # the network fee of every CC pair, deep or not (a pair without one takes the venue's point)
            for tok, (cc_res, _, _) in out.items():
                if tok not in fees:
                    try:
                        q = await a.quote(Decimal(100) / cc_usd, *inst[tok])
                        if q.network_fee is not None and q.network_fee_instrument.id == "Amulet":
                            fees[tok] = q.network_fee
                    except Exception as exc:  # noqa: BLE001
                        log.warning("cantex fee %s: %r", tok, exc)
            return {t: v for t, v in out.items() if 2 * v[0] * cc_usd >= MIN_POOL_USD}

        async def quote(tok, sell_cc, amount):
            cc, ti = inst[tok]
            q = await a.quote(amount, cc if sell_cc else ti, ti if sell_cc else cc)
            return q.returned_amount, q.network_fee
        rows = await _check("cantex", read, quote, "cantex", cc_usd)
        return rows, fees
    finally:
        await a.close()
        await pub.close()


async def _check(venue: str, read, quote, formula: str, cc_usd: Decimal) -> list[dict]:
    """Every deep pool at every size both ways; a gap is asked again once on reserves read again."""
    pools = await read()
    rows = []
    for tok in sorted(pools, key=lambda t: -pools[t][0])[:MAX_POOLS]:
        for size in SIZES_USD:
            for sell_cc in (True, False):
                row = None
                for attempt in range(2):
                    cc_res, tok_res, fee = pools[tok]
                    amount = Decimal(size) / cc_usd * (Decimal(1) if sell_cc else tok_res / cc_res)
                    ours = m.FORMULAS[formula](cc_res, tok_res, fee)(sell_cc, amount)
                    theirs, net_fee = await quote(tok, sell_cc, amount)
                    gap = _gap_bp(ours, theirs)
                    row = {"venue": venue, "trade": f"CC->{tok}" if sell_cc else f"{tok}->CC", "size_usd": size,
                           "amount_in": float(amount), "ours": float(ours), "venue_quote": float(theirs),
                           "gap_bp": None if gap is None else round(gap, 4),
                           **({"network_fee_cc": float(net_fee)} if net_fee is not None else {})}
                    if gap is not None and abs(gap) <= GAP_BP:
                        break
                    if attempt == 0:  # the pool may have traded between our read and the quote
                        pools = await read()
                        if tok not in pools:
                            break
                row["ok"] = row["gap_bp"] is not None and abs(row["gap_bp"]) <= float(GAP_BP)
                rows.append(row)
    return rows


def summarise(rows: list[dict], failed: dict[str, str], fees: dict[str, Decimal], now: int) -> dict:
    venues = {}
    for v in ("tradecraft", "cantex"):
        vr = [r for r in rows if r["venue"] == v]
        if v in failed:
            venues[v] = {"status": "not checked", "why": failed[v]}
        elif vr:
            gaps = [abs(r["gap_bp"]) for r in vr if r["gap_bp"] is not None]
            venues[v] = {"status": "ok" if all(r["ok"] for r in vr) else "gap", "checked": len(vr),
                         "agree": sum(r["ok"] for r in vr), "max_gap_bp": round(max(gaps), 4) if gaps else None,
                         "median_gap_bp": round(statistics.median(gaps), 4) if gaps else None}
    if fees:
        venues.setdefault("cantex", {})["network_fee_cc"] = {f"CC/{t}": float(f) for t, f in sorted(fees.items())}
    for v, why in UNCHECKED.items():
        venues[v] = {"status": "not checked", "why": why}
    return {"t": now, "gap_bp_limit": float(GAP_BP), "sizes_usd": list(SIZES_USD), "min_pool_usd": MIN_POOL_USD,
            "venues": venues, "rows": rows}


def message(record: dict) -> str | None:
    lines = []
    for v, s in record["venues"].items():
        if v in UNCHECKED:
            continue
        if s.get("status") == "gap":
            bad = [r for r in record["rows"] if r["venue"] == v and not r["ok"]]
            lines.append(f"{v}: {len(bad)} of {s['checked']} trades off the venue's own quote, largest "
                         f"{s['max_gap_bp']:.1f} bp (" + html.escape(", ".join(f"{r['trade']} ${r['size_usd']:,}"
                                                                             for r in bad[:4])) + ")")
        elif s.get("status") == "not checked" and s.get("why") != NO_KEYS:  # no keys: by setup, not news
            lines.append(f"{v}: not checked ({html.escape(s.get('why', ''))})")
    if not lines:
        return None
    return "<b>cantonvenues.com quote check</b>\n" + "\n".join(lines)


async def run(api: Path, state: Path, dry: bool) -> dict:
    cc_usd = Decimal(str(json.loads((api / "summary.json").read_text())["cc_usd"]))
    rows, failed, fees = [], {}, {}
    try:
        rows += await _tradecraft(cc_usd)
    except Exception as exc:  # noqa: BLE001
        failed["tradecraft"] = f"{type(exc).__name__}: {exc}"[:200]
    if os.getenv("CANTEX_OPERATOR_KEY"):
        try:
            got, fees = await _cantex(cc_usd)
            rows += got
        except Exception as exc:  # noqa: BLE001
            failed["cantex"] = f"{type(exc).__name__}: {exc}"[:200]
    else:
        failed["cantex"] = NO_KEYS
    now = int(time.time())
    record = summarise(rows, failed, fees, now)
    for v, s in record["venues"].items():
        log.info("%-10s %s", v, json.dumps({k: x for k, x in s.items() if k != "network_fee_cc"}))
    if fees:
        log.info("cantex network fee per pair (CC): %s", {t: float(f) for t, f in sorted(fees.items())})
    if dry:
        return record
    from collect import write_json
    write_json(api / "quotecheck.json", record)
    if fees:
        write_json(state / "quote_check.json", {"t": now, "cantex_fee_cc": {t: str(f) for t, f in fees.items()}})
    if (text := message(record)) is not None:
        send(text)
    return record


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", type=Path, required=True, help="the site's api/v1 directory")
    ap.add_argument("--state", type=Path, required=True, help="records off the web root (the collector's)")
    ap.add_argument("--dry-run", action="store_true", help="print, write nothing, send nothing")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run(args.api, args.state, args.dry_run))


if __name__ == "__main__":
    main()
