"""A minimal Python client for Arkiv 1.0 (arkiv.network): create an entity, read it back, query.

Arkiv 1.0 shipped on 30.10.2026 with a JS SDK only. The Python SDK on PyPI (``arkiv-sdk``
1.0.0b3.dev0, released 07.05.2026) predates 1.0, and Arkiv's docs list no Python client. This follows
the 1.0 wire format as Arkiv documents it (docs.arkiv.network, "Write transactions" and "Read
methods") and as the JS SDK 1.0.1 builds it (``src/entity/operations.ts``, ``src/schema/codec.ts``,
``src/schema/attributes.ts``):

- A write is one call to ``execute((uint8 operation, bytes operationData)[])`` on ``0x44…44``. A
  create's ``operationData`` is the ABI tuple ``(uint128 salt, uint64 expiresAt, uint64 minLifetime,
  uint8 creationFlags, (bytes32 name, uint8 typeId, bytes value)[] attributes)``, the attributes
  sorted by name.
- An entity is its attributes and nothing else: data travels as a ``bytes`` attribute. Bit 7 of
  ``typeId`` marks an attribute indexed, and a query can filter only on indexed attributes. A named
  schema adds the indexed ``str`` tag ``arkiv.schema``.
- A lifetime is a block count (2 s blocks) in ``minLifetime``, with ``expiresAt`` 0.
- Reads are ``arkiv_query(query, {select, limit, offset})`` and ``arkiv_getEntity(key)``.
- Rabdi's RPC takes an optional access key as the last path segment; without one, requests go to
  the public RPC, which is rate limited.

``tests/test_arkiv_log.py`` checks the calldata against the JS SDK 1.0.1's own output byte for byte.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

RPC_URL = "https://rpc.rabdi.db-chain.testnet.arkiv.network"
CHAIN_ID = 7794095
CHAIN_NAME = "Arkiv Rabdi testnet"
EXPLORER = "https://rabdi.explorer.arkiv.network"
ARKIV_ADDRESS = "0x4400000000000000000000000000000000000044"
BLOCK_TIME_S = 2
MAX_LIFETIME_BLOCKS = 7_884_000  # about 182.5 days
MAX_ENTITY_BYTES = 128 * 1024  # all bytes attributes of one create together
MAX_STR_BYTES = 128
MAX_ATTRIBUTES = 32

OP_CREATE = 1
FLAG_READONLY = 0b01
FLAG_PERMISSIONLESS_EXTENSION = 0b10
INDEXED = 0x80
TYPE_IDS = {"bool": 1, "i32": 2, "u64": 3, "u256": 4, "dec": 5, "bytes": 7, "str": 8, "addr": 9,
            "key": 10}
SCHEMA_ATTRIBUTE = "arkiv.schema"

EXECUTE_SELECTOR = keccak(text="execute((uint8,bytes)[])")[:4]
CREATE_TUPLE = "(uint128,uint64,uint64,uint8,(bytes32,uint8,bytes)[])"
ENTITY_CREATED = "0x" + keccak(text="EntityCreated(bytes32,address,uint64,uint8)").hex()

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")
RESERVED_NAMES = {"and", "or", "not", "true", "false", "between", "startswith", "exists", "typeof",
                  "order", "by", "asc", "desc", *TYPE_IDS}


@dataclass(frozen=True)
class Value:
    """A typed attribute value; ``indexed`` makes it filterable in queries."""
    tag: str
    value: Any
    indexed: bool = False


def value(tag: str, v: Any, indexed: bool = False) -> Value:
    if tag not in TYPE_IDS:
        raise ValueError(f"unknown attribute type {tag!r}")
    if tag == "bytes" and indexed:
        raise ValueError("a bytes attribute cannot be indexed")
    return Value(tag, v, indexed)


def u64(v: int, indexed: bool = False) -> Value:
    return value("u64", v, indexed)


def text(v: str, indexed: bool = False) -> Value:
    return value("str", v, indexed)


def blob(v: bytes) -> Value:
    return value("bytes", v)


def _value_bytes(tag: str, v: Any) -> bytes:
    if tag == "str":
        raw = v.encode()
        if len(raw) > MAX_STR_BYTES:
            raise ValueError(f"a str value is at most {MAX_STR_BYTES} bytes, got {len(raw)}")
        return raw
    if tag == "bytes":
        return bytes(v)
    if tag == "bool":
        return (1 if v else 0).to_bytes(32, "big")
    if tag == "u64":
        if not 0 <= int(v) < 2**64:
            raise ValueError(f"{v} does not fit a u64")
        return int(v).to_bytes(32, "big")
    if tag == "u256":
        return int(v).to_bytes(32, "big")
    if tag == "i32":
        if not -2**31 <= int(v) < 2**31:
            raise ValueError(f"{v} does not fit an i32")
        return (int(v) % 2**256).to_bytes(32, "big")
    if tag == "dec":  # a signed integer of the value times 10^18
        from decimal import Decimal
        units = int(Decimal(str(v)) * 10**18)
        return (units % 2**256).to_bytes(32, "big")
    if tag == "key":
        raw = bytes.fromhex(v[2:]) if isinstance(v, str) else bytes(v)
        if len(raw) != 32:
            raise ValueError(f"a key is 32 bytes, got {len(raw)}")
        return raw
    if tag == "addr":
        return bytes(12) + bytes.fromhex(v[2:].lower())
    raise ValueError(f"unsupported attribute type {tag!r}")


def _name32(name: str) -> bytes:
    raw = name.encode()
    if name.startswith("$"):
        raise ValueError(f"{name!r}: names starting with $ are the engine's own fields")
    if not NAME_RE.match(name) or "--" in name or name.lower() in RESERVED_NAMES:
        raise ValueError(f"attribute name {name!r}: a letter, then letters, digits, '.', '_', '-'; "
                         "no '--', not a query-language word")
    if len(raw) > 32:
        raise ValueError(f"attribute name {name!r} is over 32 bytes")
    return raw.ljust(32, b"\0")


def _cell(name: str, v: Any) -> tuple[bytes, int, bytes]:
    if isinstance(v, str):
        v = text(v)
    elif isinstance(v, (bytes, bytearray)):
        v = blob(bytes(v))
    elif not isinstance(v, Value):
        raise ValueError(f"{name!r}: give a str, bytes or a typed value such as u64(…)")
    return _name32(name), TYPE_IDS[v.tag] | (INDEXED if v.indexed else 0), _value_bytes(v.tag, v.value)


def create_calldata(attributes: dict[str, Any], lifetime_s: int, readonly: bool = False,
                    salt: int | None = None, schema: str | None = None) -> bytes:
    """The ``execute`` calldata for one create. A bare ``str`` is a str attribute, bare ``bytes`` a
    bytes attribute, anything else a typed value such as ``u64(1792410272, indexed=True)``.
    ``schema`` names the entity's kind: it is written as the indexed tag ``arkiv.schema``."""
    if lifetime_s <= 0 or lifetime_s % BLOCK_TIME_S:
        raise ValueError(f"a lifetime must be a positive multiple of {BLOCK_TIME_S}s, got {lifetime_s}")
    if lifetime_s // BLOCK_TIME_S > MAX_LIFETIME_BLOCKS:
        raise ValueError(f"a lifetime is at most {MAX_LIFETIME_BLOCKS} blocks")
    cells = [_cell(n, v) for n, v in attributes.items()]
    if schema is not None:
        cells.append((SCHEMA_ATTRIBUTE.encode().ljust(32, b"\0"), TYPE_IDS["str"] | INDEXED,
                      _value_bytes("str", schema)))
    if len(cells) > MAX_ATTRIBUTES:
        raise ValueError(f"{len(cells)} attributes, the limit is {MAX_ATTRIBUTES}")
    blob_bytes = sum(len(v) for _, t, v in cells if t == TYPE_IDS["bytes"])
    if blob_bytes > MAX_ENTITY_BYTES:
        raise ValueError(f"bytes attributes hold {blob_bytes} bytes, the limit is {MAX_ENTITY_BYTES}")
    cells.sort()
    salt = secrets.randbits(128) if salt is None else salt
    flags = FLAG_READONLY if readonly else 0
    op_data = abi_encode([CREATE_TUPLE], [(salt, 0, lifetime_s // BLOCK_TIME_S, flags, cells)])
    return EXECUTE_SELECTOR + abi_encode(["(uint8,bytes)[]"], [[(OP_CREATE, op_data)]])


def attribute(entity: dict, name: str) -> Any:
    """One attribute's value from an entity as ``arkiv_getEntity`` or ``arkiv_query`` returns it."""
    for a in entity.get("attributes") or []:
        if a["name"] == name:
            return a["value"]
    return None


@dataclass
class Created:
    entity_key: str
    tx_hash: str
    block: int
    expires_at_block: int
    gas_used: int
    fee_glm: float


class Arkiv:
    def __init__(self, rpc_url: str = RPC_URL, access_key: str | None = None,
                 chain_id: int = CHAIN_ID, timeout_s: float = 30):
        self.rpc_url = f"{rpc_url.rstrip('/')}/{quote(access_key, safe='')}" if access_key else rpc_url
        self.chain_id = chain_id
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
            "chainId": self.chain_id,
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

    def query(self, query: str, limit: int = 50, offset: int = 0, attributes: bool = True,
              count: bool = False) -> dict:
        """One page of ``arkiv_query``. ``limit`` is 1 to 100; page with ``offset`` (add ``ORDER BY
        $key ASC`` for a stable order)."""
        select = {"key": True, "owner": True, "creator": True, "createdAt": True,
                  "expiresAt": True, "creationFlags": True, "attributes": attributes,
                  "count": count}
        options: dict[str, Any] = {"select": select, "limit": hex(limit)}
        if offset:
            options["offset"] = hex(offset)
        return self.rpc("arkiv_query", query, options)

    def get_entity(self, entity_key: str) -> dict | None:
        return self.rpc("arkiv_getEntity", entity_key)
