"""The ChatGPT plugin package (24.11): the listing stays inside the directory's limits, names the server this
repo deploys, carries the review cases it asks for, and advertises no paid plan (plugin guidelines 1.3, 5.1)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from PIL import Image

PKG = Path(__file__).resolve().parents[1] / "examples" / "canton_venues" / "chatgpt-plugin"


def manifest() -> dict:
    return json.loads((PKG / "plugin.json").read_text())


def test_listing_fits_the_directory_limits():
    d = manifest()
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", d["name"]) and len(d["name"]) <= 64
    i = d["extensions"]["com.openai"]["interface"]
    assert len(i["displayName"]) <= 30 and len(i["shortDescription"]) <= 30 and len(i["longDescription"]) <= 4000
    assert "MCP" not in i["displayName"] and "Plugin" not in i["displayName"]
    assert len(i["defaultPrompt"]) <= 3 and all(len(p) <= 128 for p in i["defaultPrompt"])
    assert len(i["capabilities"]) <= 20 and all(len(c) <= 120 for c in i["capabilities"])
    assert len(d["keywords"]) <= 25
    for key in ("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL"):
        assert i[key].startswith("https://cantonvenues.com"), key
    # the deploy installs this file (deploy.sh); the review rejects a package without a recording
    assert d["extensions"]["com.openai"]["review"]["demo_recording_url"] == "https://cantonvenues.com/demo/chatgpt.mp4"
    assert (PKG.parent / "site" / "demo" / "chatgpt.mp4").stat().st_size > 100_000


def test_icons_exist_and_are_square():
    i = manifest()["extensions"]["com.openai"]["interface"]
    for key in ("logo", "composerIcon"):
        w, h = Image.open(PKG / i[key]).size
        assert w == h >= 48, key


def test_review_has_five_positive_and_three_negative_cases_on_real_tools():
    src = (PKG.parent / "mcp_server.py").read_text()
    tools = set(re.findall(r"@server\.tool\(.*\)\s*\n(?:async )?def (\w+)", src))
    cases = manifest()["extensions"]["com.openai"]["review"]["test_cases"]
    assert len(cases["positive"]) == 5 and len(cases["negative"]) == 3
    for c in cases["positive"]:
        assert c["prompt"] and c["expected_behavior"]
        assert {t.strip() for t in c["tools_triggered"].split(",")} <= tools, c["tools_triggered"]


def test_one_server_and_it_is_the_one_we_deploy():
    servers = json.loads((PKG / "mcp.json").read_text())["mcpServers"]
    assert list(servers.values()) == [{"type": "streamable-http", "url": "https://cantonvenues.com/mcp"}]


def test_no_paid_plan_or_em_dash_in_the_package():
    text = (PKG / "plugin.json").read_text()
    for word in ("pricing", "Pro ", "$199", "$3,999", "subscription", "free trial", "—"):
        assert word not in text, word
