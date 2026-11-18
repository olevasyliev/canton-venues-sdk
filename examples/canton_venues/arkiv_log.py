"""The Arkiv log: every hour, the quote check and the best-execution verdicts go to Arkiv (Rabdi
testnet, Arkiv 1.0) as readonly entities, a record nobody, us included, can rewrite afterwards. The
engine refuses any change to a readonly entity's attributes; the owner can still delete one or extend
its life, and either shows on chain. From 19.10 to 18.11.2026 the log ran on Tiramisu, the testnet
Rabdi replaced; arkiv.json keeps that run under ``earlier``.

Two entities per run (canton-venues-arkiv.timer, at :25 after the quote check's :20):

- ``kind = 'quotecheck'``: ``quotecheck.json`` exactly as served (~32 KB).
- ``kind = 'execution'``: the verdict rows of ``execution.json`` (117 KB served, near the 128 KB
  payload limit): pair, side, size, best venue after network fees, its edge over the next venue in
  bp, what it pays out.

Both carry the schema tag ``arkiv.schema = 'canton-venues.log'`` and: ``kind`` (str, indexed), ``t``
(u64, indexed: the snapshot's own unix time), ``version`` (u64), ``src_sha256`` (bytes, sha256 of the
file as served), ``content_type`` (str) and ``body`` (bytes, the JSON). A snapshot whose t was already
written is not written again (the quote check skipped an hour). Anyone can write the same tag, so a
reader filters on our creator address too; ``arkiv.json`` in the API directory gives that address,
the query and the latest entity of each kind.

The writer's key is ARKIV_PRIVATE_KEY (the server's .env, chmod 600): a testnet-only key that pays
gas in testnet GLM. ARKIV_ACCESS_KEY (optional, same .env) is an Arkiv Hub access key for Rabdi's
RPC; without it the run uses the public, rate-limited RPC.

    python arkiv_log.py --api /var/www/canton-venues/api/v1
    python arkiv_log.py --api https://cantonvenues.com/api/v1 --dry-run   # build, estimate, send nothing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx
from eth_account import Account

import arkiv_chain as ak

SCHEMA_TAG = "canton-venues.log"
VERSION = 1
LIFETIME_DAYS = 30
KINDS = ("quotecheck", "execution")
LOW_BALANCE_GLM = 0.1

log = logging.getLogger("arkiv_log")


def read_api(api: str, name: str) -> bytes:
    if api.startswith("http"):
        r = httpx.get(f"{api.rstrip('/')}/{name}", timeout=30)
        r.raise_for_status()
        return r.content
    return (Path(api) / name).read_bytes()


def execution_verdicts(doc: dict) -> dict:
    rows = []
    for pair in doc["pairs"]:
        for row in pair["rows"]:
            best = row.get("best_net")
            if not best:
                continue
            edge = row.get("edge_net_bps")
            rows.append([pair["symbol"], row["side"], row["size_usd"], best,
                         None if edge is None else round(edge, 2), row["out_net"][best]])
    return {"t": doc["t"], "sizes_usd": doc["sizes_usd"],
            "columns": ["pair", "side", "size_usd", "best_net", "edge_net_bps", "out_net"],
            "rows": rows}


def entities(api: str) -> list[dict]:
    out = []
    for kind in KINDS:
        raw = read_api(api, f"{kind}.json")
        doc = json.loads(raw)
        payload = raw if kind == "quotecheck" else json.dumps(
            execution_verdicts(doc), separators=(",", ":")).encode()
        out.append({"kind": kind, "t": int(doc["t"]), "payload": payload, "attributes": {
            "kind": ak.text(kind, indexed=True), "t": ak.u64(int(doc["t"]), indexed=True),
            "version": ak.u64(VERSION), "src_sha256": ak.blob(hashlib.sha256(raw).digest()),
            "content_type": "application/json", "body": ak.blob(payload)}})
    return out


def reader_query(creator: str) -> str:
    return f"arkiv.schema = str('{SCHEMA_TAG}') AND $creator = addr({creator})"


def on_this_chain(previous: dict) -> dict:
    """The pointer as it stands for the current chain. A pointer written for another chain (the
    Tiramisu run) moves whole under ``earlier``, and the count on this chain starts from zero."""
    if previous.get("chain", {}).get("chain_id") in (None, ak.CHAIN_ID):
        return previous
    old = {k: previous[k] for k in ("chain", "creator", "query", "lifetime_days", "written", "latest")
           if k in previous}
    old["until"] = int(time.time())
    return {"earlier": previous.get("earlier", []) + [old]}


def pointer(previous: dict, creator: str, written: dict[str, dict]) -> dict:
    latest = {**previous.get("latest", {}), **written}
    out = {
        "t": int(time.time()),
        "about": "Each hour the quote check and the best-execution verdicts are written to Arkiv "
                 "(Rabdi testnet, Arkiv 1.0) as readonly entities: their attributes cannot be "
                 "changed after the fact. Query with the creator address below; latest gives the "
                 "newest entity of each kind. The JSON is the entity's body attribute.",
        "chain": {"name": ak.CHAIN_NAME, "chain_id": ak.CHAIN_ID, "rpc": ak.RPC_URL,
                  "explorer": ak.EXPLORER},
        "creator": creator,
        "query": reader_query(creator),
        "lifetime_days": LIFETIME_DAYS,
        "written": previous.get("written", 0) + len(written),
        "latest": latest,
    }
    if previous.get("earlier"):
        out["earlier"] = previous["earlier"]
    return out


def run(api: str, key: str, dry_run: bool, chain: ak.Arkiv | None = None) -> int:
    chain = chain or ak.Arkiv(access_key=os.getenv("ARKIV_ACCESS_KEY") or None)
    creator = Account.from_key(key).address
    out_path = None if api.startswith("http") else Path(api) / "arkiv.json"
    previous = on_this_chain(json.loads(out_path.read_text())) if out_path and out_path.exists() else {}
    balance = chain.balance_glm(creator)
    log.info("writer %s, balance %.6f testnet GLM", creator, balance)
    if balance < LOW_BALANCE_GLM:
        log.warning("balance under %s testnet GLM: claim more at hub.arkiv.network/faucet", LOW_BALANCE_GLM)

    written, failed = {}, 0
    for e in entities(api):
        last_t = previous.get("latest", {}).get(e["kind"], {}).get("t")
        if last_t == e["t"]:
            log.info("%s t=%s already written, skipped", e["kind"], e["t"])
            continue
        calldata = ak.create_calldata(e["attributes"], LIFETIME_DAYS * 86400, readonly=True,
                                      schema=SCHEMA_TAG)
        if dry_run:
            gas = chain.estimate_create(creator, calldata)
            log.info("%s t=%s payload %d B, gas %d (dry run: not sent)", e["kind"], e["t"],
                     len(e["payload"]), gas)
            continue
        try:
            c = chain.send_create(key, calldata)
            back = chain.get_entity(c.entity_key)
            body = ak.attribute(back or {}, "body")
            if body is None or bytes.fromhex(body[2:]) != e["payload"]:
                raise RuntimeError(f"{c.entity_key} does not read back as written")
        except Exception:
            log.exception("%s t=%s not written", e["kind"], e["t"])
            failed += 1
            continue
        written[e["kind"]] = {"t": e["t"], "entity_key": c.entity_key, "tx": c.tx_hash,
                              "block": c.block, "expires_at_block": c.expires_at_block}
        log.info("%s t=%s -> %s, block %d, %.6f GLM, read back identical", e["kind"], e["t"],
                 c.entity_key, c.block, c.fee_glm)

    if out_path and written:
        tmp = out_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(pointer(previous, creator, written), indent=1))
        tmp.replace(out_path)
    return 1 if failed else 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True, help="the API directory, or its https URL (read only)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    key = os.getenv("ARKIV_PRIVATE_KEY")
    if not key:
        log.error("ARKIV_PRIVATE_KEY is not set")
        return 1
    return run(args.api, key, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
