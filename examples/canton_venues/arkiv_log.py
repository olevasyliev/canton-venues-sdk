"""The Arkiv log: every hour, the quote check and the best-execution verdicts go to Arkiv (Tiramisu
testnet) as readonly entities, a record nobody, us included, can rewrite afterwards. The engine
refuses any change to a readonly entity's contents (a patch reverts ReadOnlyEntity, 19.10.2026); the
owner can still delete one or extend its life, and either shows on chain.

Two entities per run (canton-venues-arkiv.timer, at :25 after the quote check's :20):

- ``kind = 'quotecheck'``: ``quotecheck.json`` exactly as served (~32 KB).
- ``kind = 'execution'``: the verdict rows of ``execution.json`` (117 KB served, near the 128 KB
  payload limit): pair, side, size, best venue after network fees, its edge over the next venue in
  bp, what it pays out.

Attributes on both: ``project = 'canton-venues'``, ``kind``, ``t`` (u64, the snapshot's own unix
time), ``schema`` (u64), ``src_sha256`` (bytes32, sha256 of the file as served). A snapshot whose t
was already written is not written again (the quote check skipped an hour). Anyone can write
``project = 'canton-venues'``, so a reader filters on our creator address too; ``arkiv.json`` in the
API directory gives that address, the query and the latest entity of each kind.

The writer's key is ARKIV_PRIVATE_KEY (the server's .env, chmod 600): a testnet-only key that pays
gas in testnet GLM (about 0.0017 GLM a run, 19.10).

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

PROJECT = "canton-venues"
SCHEMA = 1
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
            "project": PROJECT, "kind": kind, "t": ak.u64(int(doc["t"])), "schema": ak.u64(SCHEMA),
            "src_sha256": ak.b32(hashlib.sha256(raw).digest())}})
    return out


def reader_query(creator: str) -> str:
    return f"project = str('{PROJECT}') AND $creator = addr({creator})"


def pointer(previous: dict, creator: str, written: dict[str, dict]) -> dict:
    latest = {**previous.get("latest", {}), **written}
    return {
        "t": int(time.time()),
        "about": "Each hour the quote check and the best-execution verdicts are written to Arkiv "
                 "(Tiramisu testnet) as readonly entities: their contents cannot be changed after "
                 "the fact. Query with the creator address below; latest gives the newest entity "
                 "of each kind.",
        "chain": {"name": "Arkiv Tiramisu testnet", "chain_id": ak.CHAIN_ID, "rpc": ak.RPC_URL,
                  "explorer": ak.EXPLORER, "data_explorer": "https://data.arkiv.network"},
        "creator": creator,
        "query": reader_query(creator),
        "lifetime_days": LIFETIME_DAYS,
        "written": previous.get("written", 0) + len(written),
        "latest": latest,
    }


def run(api: str, key: str, dry_run: bool, chain: ak.Tiramisu | None = None) -> int:
    chain = chain or ak.Tiramisu()
    creator = Account.from_key(key).address
    out_path = None if api.startswith("http") else Path(api) / "arkiv.json"
    previous = json.loads(out_path.read_text()) if out_path and out_path.exists() else {}
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
        calldata = ak.create_calldata(e["payload"], "application/json", e["attributes"],
                                      LIFETIME_DAYS * 86400, readonly=True)
        if dry_run:
            gas = chain.estimate_create(creator, calldata)
            log.info("%s t=%s payload %d B, gas %d (dry run: not sent)", e["kind"], e["t"],
                     len(e["payload"]), gas)
            continue
        try:
            c = chain.send_create(key, calldata)
            back = chain.get_entity(c.entity_key)
            if back is None or bytes.fromhex(back["payload"][2:]) != e["payload"]:
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
