"""A minimal client for Arkiv's Tiramisu testnet (arkiv.network): create an entity, query entities.

The official Python SDK (``arkiv-sdk`` 1.0.0b3.dev0, released 07.05.2026) still speaks the retired
OP-Stack RPC: on Tiramisu its queries fail with ``unknown field `includeData`, expected one of
`atBlock`, `select`, `limit`, `cursor``` (19.10.2026). This follows the JS SDK 0.8.1 wire format
(``src/utils/arkivTransactions.ts``, ``src/entity/operations.ts``, ``src/attr/codec.ts``):

- A write is one call to ``execute((uint8 operation, bytes operationData)[])`` on the system address
  ``0x44…44``. A create's ``operationData`` is the ABI tuple ``(uint128 salt, uint64 expiresAt,
  uint64 minLifetime, uint8 creationFlags, (bytes32 name, uint8 typeId, bytes value)[] attributes)``.
- The payload and content type travel as the system cells ``$payload`` and ``$contentType`` inside
  the attribute list, which is sorted by name.
- A lifetime is a block count (2 s blocks) in ``minLifetime``, with ``expiresAt`` 0.
- Reads are ``arkiv_query(query, {select, limit, cursor})``; a string literal is ``str('…')``.

``tests/test_arkiv_log.py`` checks the calldata against the JS SDK's own output byte for byte.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

RPC_URL = "https://rpc.tiramisu.db-chain.testnet.arkiv.network"
CHAIN_ID = 7738577
EXPLORER = "https://tiramisu.explorer.arkiv.network"
ARKIV_ADDRESS = "0x4400000000000000000000000000000000000044"
BLOCK_TIME_S = 2
MAX_PAYLOAD_BYTES = 128 * 1024
MAX_ATTRIBUTES = 32

OP_CREATE = 1
FLAG_READONLY = 0b01
TYPE_IDS = {"bool": 1, "i32": 2, "u64": 3, "u256": 4, "dec": 5, "bytes32": 6, "bytes": 7,
            "str": 8, "addr": 9, "key": 10}

EXECUTE_SELECTOR = keccak(text="execute((uint8,bytes)[])")[:4]
CREATE_TUPLE = "(uint128,uint64,uint64,uint8,(bytes32,uint8,bytes)[])"
ENTITY_CREATED = "0x" + keccak(text="EntityCreated(bytes32,address,uint64,uint8)").hex()

# The engine takes lowercase names only: "camelCase" reverts Ident32InvalidByte(5, 'C') in
# eth_estimateGas (19.10.2026), though the JS SDK's own check (/^[A-Za-z][A-Za-z0-9._-]*$/) passes it.
NAME_RE = re.compile(r"^[a-z][a-z0-9._-]*$")


def u64(value: int) -> tuple[str, int]:
    return ("u64", value)


def b32(value: bytes) -> tuple[str, bytes]:
    return ("bytes32", value)


def _value_bytes(tag: str, value: Any) -> bytes:
    if tag == "str":
        return value.encode()
    if tag == "bytes":
        return bytes(value)
    if tag == "bool":
        return (1 if value else 0).to_bytes(32, "big")
    if tag in ("u64", "u256"):
        return int(value).to_bytes(32, "big")
    if tag == "i32":
        return (int(value) % 2**256).to_bytes(32, "big")
    if tag in ("bytes32", "key"):
        raw = bytes.fromhex(value[2:]) if isinstance(value, str) else bytes(value)
        if len(raw) != 32:
            raise ValueError(f"a {tag} value must be 32 bytes, got {len(raw)}")
        return raw
    if tag == "addr":
        return bytes(12) + bytes.fromhex(value[2:].lower())
    raise ValueError(f"unsupported attribute type {tag!r}")


def _name32(name: str) -> bytes:
    raw = name.encode()
    if name.startswith("$"):
        return raw.ljust(32, b"\0")
    if not NAME_RE.match(name) or "--" in name:
        raise ValueError(f"attribute name {name!r}: lowercase letters, digits, '.', '_', '-' only, "
                         "a letter first")
    if len(raw) > 32:
        raise ValueError(f"attribute name {name!r} is over 32 bytes")
    return raw.ljust(32, b"\0")


def create_calldata(payload: bytes, content_type: str, attributes: dict[str, Any], lifetime_s: int,
                    readonly: bool = False, salt: int | None = None) -> bytes:
    """The ``execute`` calldata for one create. A bare ``str`` attribute is a str; anything else is
    a ``(tag, value)`` pair, e.g. ``u64(1792410272)``."""
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload is {len(payload)} bytes, the limit is {MAX_PAYLOAD_BYTES}")
    if lifetime_s <= 0 or lifetime_s % BLOCK_TIME_S:
        raise ValueError(f"a lifetime must be a positive multiple of {BLOCK_TIME_S}s, got {lifetime_s}")
    cells = [(name, *(("str", v) if isinstance(v, str) else v)) for name, v in attributes.items()]
    for name, _, _ in cells:
        if name.startswith("$"):
            raise ValueError(f"{name!r}: names starting with $ are system attributes")
    cells += [("$payload", "bytes", payload), ("$contentType", "str", content_type)]
    if len(cells) > MAX_ATTRIBUTES:
        raise ValueError(f"{len(cells)} attributes, the limit is {MAX_ATTRIBUTES}")
    encoded = sorted((_name32(n), TYPE_IDS[t], _value_bytes(t, v)) for n, t, v in cells)
    salt = secrets.randbits(128) if salt is None else salt
    flags = FLAG_READONLY if readonly else 0
    op_data = abi_encode([CREATE_TUPLE], [(salt, 0, lifetime_s // BLOCK_TIME_S, flags, encoded)])
    return EXECUTE_SELECTOR + abi_encode(["(uint8,bytes)[]"], [[(OP_CREATE, op_data)]])


@dataclass
class Created:
    entity_key: str
    tx_hash: str
    block: int
    expires_at_block: int
    gas_used: int
    fee_glm: float


class Tiramisu:
    def __init__(self, rpc_url: str = RPC_URL, timeout_s: float = 30):
        self.rpc_url = rpc_url
        self.http = httpx.Client(timeout=timeout_s)

    def rpc(self, method: str, *params: Any) -> Any:
        r = self.http.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                               "params": list(params)})
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"{method}: {body['error']}")
        return body["result"]

    def balance_glm(self, address: str) -> float:
        return int(self.rpc("eth_getBalance", address, "latest"), 16) / 1e18

    def estimate_create(self, sender: str, calldata: bytes) -> int:
        return int(self.rpc("eth_estimateGas", {"from": sender, "to": ARKIV_ADDRESS,
                                                "data": "0x" + calldata.hex()}), 16)

    def send_create(self, private_key: str, calldata: bytes, wait_s: float = 60) -> Created:
        account = Account.from_key(private_key)
        gas = self.estimate_create(account.address, calldata)
        tx = {
            "to": to_checksum_address(ARKIV_ADDRESS),
            "data": calldata,
            "value": 0,
            "nonce": int(self.rpc("eth_getTransactionCount", account.address, "pending"), 16),
            "gas": gas * 12 // 10,
            "gasPrice": int(self.rpc("eth_gasPrice"), 16),
            "chainId": CHAIN_ID,
        }
        signed = account.sign_transaction(tx)
        tx_hash = self.rpc("eth_sendRawTransaction", "0x" + signed.raw_transaction.hex())
        receipt = self._wait_receipt(tx_hash, wait_s)
        if int(receipt["status"], 16) != 1:
            raise RuntimeError(f"transaction {tx_hash} reverted")
        created = [log for log in receipt["logs"] if log["address"].lower() == ARKIV_ADDRESS.lower()
                   and log["topics"][0] == ENTITY_CREATED]
        if len(created) != 1:
            raise RuntimeError(f"transaction {tx_hash} succeeded (do not retry) but emitted "
                               f"{len(created)} EntityCreated events for one create")
        gas_used = int(receipt["gasUsed"], 16)
        price = int(receipt.get("effectiveGasPrice", hex(tx["gasPrice"])), 16)
        return Created(entity_key=created[0]["topics"][1], tx_hash=tx_hash,
                       block=int(receipt["blockNumber"], 16),
                       expires_at_block=int(created[0]["data"][2:66], 16),
                       gas_used=gas_used, fee_glm=gas_used * price / 1e18)

    def _wait_receipt(self, tx_hash: str, wait_s: float) -> dict:
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            receipt = self.rpc("eth_getTransactionReceipt", tx_hash)
            if receipt:
                return receipt
            time.sleep(1)
        raise TimeoutError(f"no receipt for {tx_hash} after {wait_s}s (it may still land: check "
                           "before resending)")

    def query(self, query: str, limit: int = 50, payload: bool = False,
              cursor: str | None = None) -> dict:
        select = {"key": True, "owner": True, "creator": True, "createdAt": True, "expiresAt": True,
                  "creationFlags": True, "contentType": True, "attributes": True, "payload": payload}
        options: dict[str, Any] = {"select": select, "limit": hex(limit)}
        if cursor:
            options["cursor"] = cursor
        return self.rpc("arkiv_query", query, options)

    def get_entity(self, entity_key: str) -> dict | None:
        rows = self.query(f"$key = key({entity_key})", limit=1, payload=True)["data"]
        return rows[0] if rows else None
