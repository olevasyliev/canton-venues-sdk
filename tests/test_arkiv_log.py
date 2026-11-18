"""The Arkiv log (arkiv_log.py, arkiv_chain.py): our create calldata is the JS SDK 1.0.1's byte for byte, the
names the engine refuses are refused here first, the execution verdicts fit the payload limit, and a
snapshot is written once. Each test fails on a wrong encoding or a missing guard."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import arkiv_chain as ak  # noqa: E402
import arkiv_log  # noqa: E402

JS_REF = Path(__file__).with_name("arkiv_js_reference.mjs")
KEY = "0x" + "11" * 32

# (name, tag, value, indexed); a bytes value is hex
CASES = [
    dict(schema=None, lifetime_s=86400, readonly=False, salt=123,
         attributes=[("body", "bytes", b'{"hello":"world"}'.hex(), False)]),
    dict(schema="canton-venues.log", lifetime_s=30 * 86400, readonly=True, salt=2**128 - 1,
         attributes=[("kind", "str", "quotecheck", True), ("t", "u64", 1792410272, True),
                     ("version", "u64", 1, False),
                     ("src_sha256", "bytes", hashlib.sha256(b"x").hexdigest(), False),
                     ("content_type", "str", "application/json", False),
                     ("body", "bytes", json.dumps({"rows": list(range(500))}).encode().hex(), False)]),
    dict(schema="x", lifetime_s=2, readonly=False, salt=0,
         attributes=[("Title", "str", "camelCase is fine in 1.0", True)]),
]


def _py(case: dict) -> str:
    attrs = {n: ak.value(t, bytes.fromhex(v) if t == "bytes" else v, indexed)
             for n, t, v, indexed in case["attributes"]}
    return "0x" + ak.create_calldata(attrs, case["lifetime_s"], readonly=case["readonly"],
                                     salt=case["salt"], schema=case["schema"]).hex()


@pytest.mark.skipif(not os.getenv("ARKIV_JS_NODE_MODULES") or not shutil.which("node"),
                    reason="set ARKIV_JS_NODE_MODULES to a node_modules holding @arkiv-network/sdk")
def test_calldata_equals_the_js_sdk_byte_for_byte(tmp_path):
    nm = Path(os.environ["ARKIV_JS_NODE_MODULES"])
    script = nm.parent / JS_REF.name  # bare imports resolve from the script's directory upward
    script.write_text(JS_REF.read_text())
    for i, case in enumerate(CASES):
        spec = {"schema": case["schema"],
                "attributes": [[n, t, str(v) if t == "u64" else v, i] for n, t, v, i in case["attributes"]],
                "lifetimeS": case["lifetime_s"], "readonly": case["readonly"], "salt": str(case["salt"])}
        out = subprocess.run(["node", str(script)], input=json.dumps(spec), capture_output=True,
                             text=True, check=True, cwd=nm.parent)
        js = json.loads(out.stdout)
        assert js["to"].lower() == ak.ARKIV_ADDRESS.lower(), f"case {i}"
        assert _py(case) == js["data"].lower(), f"case {i}: calldata differs"


def test_calldata_equals_the_js_sdk_output_pinned_for_runs_without_node():
    # sha256 of the calldata the JS SDK 1.0.1 built for cases 0 and 1 on 18.11.2026
    for i, want in PINNED.items():
        data = bytes.fromhex(_py(CASES[i])[2:])
        assert hashlib.sha256(data).hexdigest() == want, f"case {i}"


PINNED = {0: "5708092d38b9e4d3ed8df14c4fc23aa89d346c4bdcbe7cf17e7a81b365dc34e9",
          1: "c14aef847644cfcb261e3909797e55f634ec5fb8877ec9844095606149585d62"}


@pytest.mark.parametrize("name", ["1abc", "a b", "a--b", "x" * 33, "$payload", "and", "str", "Order"])
def test_names_the_engine_refuses_are_refused_before_sending(name):
    with pytest.raises(ValueError):
        ak.create_calldata({name: "x"}, 86400)


def test_lifetime_must_be_whole_blocks_and_within_the_cap():
    for bad in (3, 0, (ak.MAX_LIFETIME_BLOCKS + 1) * ak.BLOCK_TIME_S):
        with pytest.raises(ValueError):
            ak.create_calldata({}, bad)


def test_bytes_cannot_be_indexed_and_blobs_share_one_cap():
    with pytest.raises(ValueError):
        ak.value("bytes", b"x", indexed=True)
    half = ak.MAX_ENTITY_BYTES // 2 + 1
    with pytest.raises(ValueError):
        ak.create_calldata({"a": b"x" * half, "b": b"y" * half}, 86400)


def test_indexed_attributes_carry_bit_7_and_the_schema_tag_is_an_indexed_str():
    from eth_abi import decode
    data = ak.create_calldata({"kind": ak.text("q", indexed=True), "note": "n"}, 86400, schema="p.log")
    (ops,) = decode(["(uint8,bytes)[]"], data[4:])
    (op,) = decode([ak.CREATE_TUPLE], ops[0][1])
    types = {name.rstrip(b"\0").decode(): type_id for name, type_id, _ in op[4]}
    assert types == {"arkiv.schema": 8 | 0x80, "kind": 8 | 0x80, "note": 8}


def _execution_doc(pairs=20, sizes=(100, 1000, 10000, 50000)):
    venues = ["cantex", "tradecraft", "oneswap", "poolparty", "temple"]
    rows = [{"side": side, "size_usd": s, "best_net": "temple", "edge_net_bps": 6.78335,
             "out_net": {v: 99.123456789 for v in venues}, "out": {}, "cost_bps": {}}
            for side in ("sell", "buy") for s in sizes]
    return {"t": 1792412373, "sizes_usd": list(sizes),
            "pairs": [{"symbol": f"TOK{i}", "rows": rows} for i in range(pairs)]}


def test_execution_verdicts_keep_one_row_per_pair_side_size_and_fit_the_limit():
    v = arkiv_log.execution_verdicts(_execution_doc())
    assert len(v["rows"]) == 20 * 2 * 4
    assert v["rows"][0] == ["TOK0", "sell", 100, "temple", 6.78, 99.123456789]
    assert len(json.dumps(v, separators=(",", ":"))) < ak.MAX_ENTITY_BYTES // 4


class FakeChain:
    def __init__(self):
        self.sent = []

    def balance_glm(self, address):
        return 5.0

    def estimate_create(self, sender, calldata):
        return 400_000

    def send_create(self, key, calldata):
        self.sent.append(calldata)
        n = len(self.sent)
        return ak.Created(entity_key=f"0x{n:064x}", tx_hash=f"0x{n:064x}", block=100 + n,
                          expires_at_block=1_296_100 + n, gas_used=400_000, fee_glm=0.0004)

    def get_entity(self, key):
        return {"attributes": [{"name": "body", "type": "bytes",
                                "value": "0x" + self._payload_of(int(key, 16)).hex()}]}

    def create_op(self, n):
        """(salt, expiresAt, minLifetime, creationFlags, attributes) of the n-th create sent."""
        from eth_abi import decode
        (ops,) = decode(["(uint8,bytes)[]"], self.sent[n - 1][4:])
        assert len(ops) == 1 and ops[0][0] == ak.OP_CREATE
        (op,) = decode([ak.CREATE_TUPLE], ops[0][1])
        return op

    def _payload_of(self, n):
        return next(v for name, _, v in self.create_op(n)[4] if name.rstrip(b"\0") == b"body")


def _api(tmp_path, qc_t=1792412460):
    (tmp_path / "quotecheck.json").write_text(json.dumps({"t": qc_t, "venues": {}, "rows": []}))
    (tmp_path / "execution.json").write_text(json.dumps(_execution_doc(pairs=2)))
    return str(tmp_path)


def test_a_run_writes_both_kinds_and_the_pointer_then_skips_what_it_already_wrote(tmp_path):
    api, chain = _api(tmp_path), FakeChain()
    assert arkiv_log.run(api, KEY, dry_run=False, chain=chain) == 0
    assert len(chain.sent) == 2
    for n in (1, 2):  # readonly, 30 days, our attributes
        _, expires_at, lifetime, flags, attrs = chain.create_op(n)
        assert flags == ak.FLAG_READONLY and expires_at == 0 and lifetime == 30 * 86400 // 2
        names = {name.rstrip(b"\0").decode() for name, _, _ in attrs}
        assert names == {"arkiv.schema", "kind", "t", "version", "src_sha256", "content_type", "body"}
    p = json.loads((tmp_path / "arkiv.json").read_text())
    creator = ak.Account.from_key(KEY).address
    assert p["creator"] == creator and creator in p["query"] and p["written"] == 2
    assert set(p["latest"]) == {"quotecheck", "execution"}

    assert arkiv_log.run(api, KEY, dry_run=False, chain=chain) == 0
    assert len(chain.sent) == 2, "the same snapshots were written twice"

    _api(tmp_path, qc_t=1792416060)  # the next hour's quote check
    assert arkiv_log.run(api, KEY, dry_run=False, chain=chain) == 0
    assert len(chain.sent) == 3
    p = json.loads((tmp_path / "arkiv.json").read_text())
    assert p["written"] == 3 and p["latest"]["quotecheck"]["t"] == 1792416060


def test_a_dry_run_sends_nothing_and_writes_no_pointer(tmp_path):
    api, chain = _api(tmp_path), FakeChain()
    assert arkiv_log.run(api, KEY, dry_run=True, chain=chain) == 0
    assert chain.sent == [] and not (tmp_path / "arkiv.json").exists()


def test_a_write_that_does_not_read_back_fails_the_run_and_is_not_recorded(tmp_path):
    api, chain = _api(tmp_path), FakeChain()
    chain.get_entity = lambda key: {"attributes": [{"name": "body", "type": "bytes", "value": "0x00"}]}
    assert arkiv_log.run(api, KEY, dry_run=False, chain=chain) == 1
    assert not (tmp_path / "arkiv.json").exists()


def test_a_pointer_from_the_earlier_chain_moves_under_earlier_and_the_count_restarts(tmp_path):
    api, chain = _api(tmp_path), FakeChain()
    old = {"chain": {"name": "Arkiv Tiramisu testnet", "chain_id": 7738577}, "creator": "0xabc",
           "query": "q", "written": 1434, "latest": {"quotecheck": {"t": 1792412460}}}
    (tmp_path / "arkiv.json").write_text(json.dumps(old))
    assert arkiv_log.run(api, KEY, dry_run=False, chain=chain) == 0
    assert len(chain.sent) == 2, "a snapshot written on the earlier chain must be written again here"
    p = json.loads((tmp_path / "arkiv.json").read_text())
    assert p["chain"]["chain_id"] == ak.CHAIN_ID and p["written"] == 2
    assert p["earlier"][0]["written"] == 1434 and p["earlier"][0]["chain"]["chain_id"] == 7738577
    assert arkiv_log.run(api, KEY, dry_run=False, chain=chain) == 0
    p = json.loads((tmp_path / "arkiv.json").read_text())
    assert len(p["earlier"]) == 1 and p["written"] == 2
