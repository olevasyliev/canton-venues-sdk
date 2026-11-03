"""MCP server, 3 Nov 2026: every tool marked read-only with every argument described, an icon in the
registry listing and the server info, and a log of which tools get called (never their arguments)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SITE = Path(__file__).resolve().parents[1] / "examples" / "canton_venues"
sys.path.insert(0, str(SITE))


def test_the_registry_listing_carries_the_sites_icon_and_the_servers_version() -> None:
    d = json.loads((SITE / "deploy" / "server.json").read_text())
    [icon] = d["icons"]
    assert icon["src"] == "https://cantonvenues.com/icon.png" and icon["mimeType"] == "image/png"
    assert (SITE / "site" / "icon.png").exists()  # deploy.sh installs site/*.png at the web root
    src = (SITE / "mcp_server.py").read_text()
    assert f'version="{d["version"]}"' in src  # one version for the listing and the server it lists


def test_every_tool_is_marked_read_only_in_the_source() -> None:
    src = (SITE / "mcp_server.py").read_text()
    tools = re.findall(r"@server\.tool\((.*)\)", src)
    assert len(tools) == 12 and all("annotations=READ_ONLY" in t for t in tools)


@pytest.fixture(scope="module")
def mcp_server():
    pytest.importorskip("mcp")
    import mcp_server
    return mcp_server


def test_every_tool_and_argument_is_described(mcp_server) -> None:
    import anyio
    tools = anyio.run(mcp_server.server.list_tools)
    assert len(tools) == 12
    for t in tools:
        a = t.annotations
        assert a.read_only_hint is True and a.destructive_hint is False, t.name
        props = t.input_schema.get("properties", {})
        assert all(p.get("description") for p in props.values()), (t.name, props)
    by = {t.name: t.input_schema["properties"] for t in tools}
    assert by["list_tokens"]["sort_by"]["enum"] == ["liquidity", "volume", "change_24h", "change_7d", "premium"]
    assert by["pools"]["sort_by"]["enum"] == ["tvl", "volume", "apr"]


def test_tool_calls_are_logged_without_their_arguments(mcp_server, tmp_path, monkeypatch) -> None:
    import anyio
    log = tmp_path / "mcp-calls.jsonl"
    monkeypatch.setattr(mcp_server, "CALL_LOG", log)
    req = SimpleNamespace(client=SimpleNamespace(host="203.0.113.7"))

    def ctx(method, params):
        return SimpleNamespace(method=method, params=params, request=req)

    async def answered(c):
        return {"content": [], "isError": False}

    async def failed(c):
        return {"content": [], "isError": True}

    async def run():
        await mcp_server.log_calls(ctx("initialize", {"clientInfo": {"name": "claude-ai", "version": "0.1"}}), answered)
        await mcp_server.log_calls(ctx("tools/call", {"name": "quote", "arguments": {"sell": "SECRETX"}}), answered)
        await mcp_server.log_calls(ctx("tools/call", {"name": "token", "arguments": {"symbol": "NOPE"}}), failed)
        await mcp_server.log_calls(ctx("tools/list", {}), answered)  # not a call: not logged

    anyio.run(run)
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert [(r["method"], r.get("tool"), r.get("client"), r["ok"], r["ip"]) for r in rows] == [
        ("initialize", None, "claude-ai/0.1", True, "203.0.113.7"),
        ("tools/call", "quote", None, True, "203.0.113.7"),
        ("tools/call", "token", None, False, "203.0.113.7"),
    ]
    assert "SECRETX" not in log.read_text() and "NOPE" not in log.read_text()


def test_a_broken_log_never_fails_a_call(mcp_server, tmp_path, monkeypatch) -> None:
    import anyio
    monkeypatch.setattr(mcp_server, "CALL_LOG", tmp_path / "missing-dir" / "mcp-calls.jsonl")

    async def answered(c):
        return {"content": [], "isError": False}

    c = SimpleNamespace(method="tools/call", params={"name": "quote"}, request=None)
    assert anyio.run(mcp_server.log_calls, c, answered) == {"content": [], "isError": False}
