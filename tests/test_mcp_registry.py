"""The MCP registry listing: server.json and the domain proof the registry fetches from cantonvenues.com."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "examples" / "canton_venues"


def test_server_json_names_our_domain_and_the_live_endpoint() -> None:
    d = json.loads((ROOT / "deploy" / "server.json").read_text())
    assert d["name"].startswith("com.cantonvenues/")  # the namespace the domain proof grants
    assert len(d["description"]) <= 100  # the registry's limit
    assert d["remotes"] == [{"type": "streamable-http", "url": "https://cantonvenues.com/mcp"}]
    assert "packages" not in d


def test_the_domain_proof_is_a_public_key_only_and_is_deployed() -> None:
    proof = (ROOT / "site" / ".well-known" / "mcp-registry-auth").read_text().strip()
    assert re.fullmatch(r"v=MCPv1; k=ed25519; p=[A-Za-z0-9+/]{43}=", proof)
    assert ".well-known/mcp-registry-auth /var/www/canton-venues/.well-known/" in (ROOT / "deploy" / "deploy.sh").read_text()
