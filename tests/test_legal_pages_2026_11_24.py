"""Privacy and terms pages (24.11), which the Claude and ChatGPT directories ask for, the footer links to
them, and an MCP answer that no longer carries the API files' line for bots."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "examples" / "canton_venues"
sys.path.insert(0, str(HERE))

import legal_pages as lp  # noqa: E402
import venue_pages as vp  # noqa: E402

T = 1795530000


def test_privacy_says_what_is_recorded_why_who_sees_it_and_for_how_long():
    html = lp.page("privacy", T)
    for heading in ("What is recorded", "Who sees it", "How long", "Your choices"):
        assert f"<h2>{heading}</h2>" in html
    # the MCP log keeps no arguments (mcp_server._record_call), and the page says so
    assert "The arguments of the call and the question you asked your assistant are not recorded" in html
    assert "Telegram's Bot API" in html and "within 12 months" in html
    assert '<link rel="canonical" href="https://cantonvenues.com/privacy/">' in html


def test_mcp_call_log_keeps_no_arguments_as_the_privacy_page_promises():
    src = (HERE / "mcp_server.py").read_text()
    record = src.split("def _record_call", 1)[1].split("\ndef ", 1)[0]
    assert '"arguments"' not in record and "params.get(\"arguments\")" not in record


def test_terms_say_not_advice_free_use_and_no_paid_placement():
    html = lp.page("terms", T)
    assert "not investment advice" in html and "free to use for any purpose" in html
    assert "no venue can pay for its place in the rankings" in html
    assert '<link rel="canonical" href="https://cantonvenues.com/terms/">' in html


def test_every_page_links_privacy_and_terms_in_the_footer():
    for html in (lp.page("privacy", T), (HERE / "site" / "index.html").read_text()):
        assert 'href="/privacy/"' in html and 'href="/terms/"' in html
    shell = vp._shell("x", "y", "https://cantonvenues.com/x/", None, "", T)
    assert 'href="/privacy/">privacy</a>' in shell and 'href="/terms/">terms</a>' in shell


def test_home_page_title_and_description_name_canton_dex_analytics():
    index = (HERE / "site" / "index.html").read_text()
    assert "<title>Canton Venues: Canton DEX analytics, prices and best execution</title>" in index
    assert 'content="Canton DEX analytics: ' in index and "MCP server for AI agents" in index


def test_the_venues_tool_drops_the_line_for_bots(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    import mcp_server
    api = tmp_path / "api" / "v1"
    api.mkdir(parents=True)
    (api / "venues.json").write_text(json.dumps({"t": 1, "venues": [{"name": "Temple"}], "notice": "Running a bot?"}))
    monkeypatch.setattr(mcp_server, "API_DIR", str(api))
    mcp_server._cache.clear()
    fn = getattr(mcp_server.venues, "fn", mcp_server.venues)
    out = fn()
    assert out == {"t": 1, "venues": [{"name": "Temple"}]}
