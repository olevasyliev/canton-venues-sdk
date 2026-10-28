"""The build page and the app registration it posts to contact.py."""

from __future__ import annotations

import html
import json
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import build_page as bp  # noqa: E402
import contact  # noqa: E402

T = 1793174522


def test_contact_form_without_kind_is_read_as_before():
    entry, why = contact.read_form({"name": "A", "contact": "a@b.c", "message": "hello"})
    assert entry == {"name": "A", "contact": "a@b.c", "message": "hello"} and why == ""
    assert contact.read_form({"contact": "a@b.c", "message": "x"}) == (None, "contact and message are required")


def test_registration_needs_the_app_and_a_contact_and_reads_the_listing_flag():
    entry, _ = contact.read_form({"kind": "register", "app": "Some Wallet", "contact": "@team", "uses": "MCP, SDK",
                                  "listed": True, "line": "We route swaps by it", "url": "https://x.example"})
    assert entry["kind"] == "register" and entry["listed"] is True and entry["line"] == "We route swaps by it"
    assert entry["message"] == "" and entry["uses"] == "MCP, SDK"
    assert contact.read_form({"kind": "register", "app": "W", "contact": "@t"})[0] is None
    assert contact.read_form({"kind": "register", "app": "Wallet", "contact": " "})[0] is None
    # only a real yes is a yes: a stray string never asks for a listing
    assert contact.read_form({"kind": "register", "app": "Wallet", "contact": "@t", "listed": "false"})[0]["listed"] is False
    long = contact.read_form({"kind": "register", "app": "Wallet", "contact": "@t", "line": "x" * 999})[0]
    assert len(long["line"]) == contact.LIMITS["line"]


def test_registration_message_is_escaped_for_telegram():
    entry, _ = contact.read_form({"kind": "register", "app": "<b>Evil</b>", "contact": "a&b", "listed": True,
                                  "line": "1 < 2"})
    text = contact.notify_text(entry)
    assert text.startswith("<b>cantonvenues.com: app registered</b>\n")
    assert "&lt;b&gt;Evil&lt;/b&gt;" in text and "a&amp;b" in text and "yes, line: 1 &lt; 2" in text
    assert "Listing: no" in contact.notify_text(contact.read_form({"kind": "register", "app": "Ab", "contact": "c"})[0])


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    handler = type("H", (contact.Handler,), {"log_path": tmp_path / "contact.jsonl"})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, handler.log_path
    srv.shutdown()


def _post(srv, body: dict) -> int:
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as err:
        return err.code


def test_registration_and_message_both_land_in_the_record(server):
    srv, log = server
    assert _post(srv, {"kind": "register", "app": "Some Wallet", "contact": "@team", "listed": True}) == 200
    assert _post(srv, {"name": "A", "contact": "a@b.c", "message": "hello"}) == 200
    assert _post(srv, {"kind": "register", "app": "", "contact": "@team"}) == 422
    assert _post(srv, {"kind": "register", "app": "Bot", "contact": "@bot", "website": "spam"}) == 200  # honeypot
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert [r.get("kind") for r in rows] == ["register", None]
    assert rows[0]["app"] == "Some Wallet" and rows[0]["listed"] is True and isinstance(rows[0]["t"], int)
    assert rows[1]["message"] == "hello"


def test_page_posts_a_registration_and_shows_no_listing_while_none_is_listed(monkeypatch):
    monkeypatch.setattr(bp, "APPS", [])
    page = bp.page(T)
    assert 'id="regform"' in page and 'kind: "register"' in page and 'fetch("/api/contact"' in page
    assert "Built on Canton Venues</h2>" not in page
    assert f"at least {bp.NOTICE_DAYS} days ahead" in page
    assert '<link rel="canonical" href="https://cantonvenues.com/build/">' in page
    # the page carries the dashboard's menu, its own link in it
    assert '<a href="/build/">Build</a>' in page
    assert "—" not in page
    # every call it shows is on the live API
    for s in bp.STARTS:
        assert s["url"].startswith("https://cantonvenues.com/api/v1/")
        assert html.escape(s["url"]) in page


def test_listed_apps_appear_in_their_own_words_escaped(monkeypatch):
    monkeypatch.setattr(bp, "APPS", [{"name": "Some <Wallet>", "url": "https://x.example",
                                      "line": "We pick the venue for every swap", "since": "2026-11-02"}])
    page = bp.page(T)
    built = page.split("<h2>Built on Canton Venues</h2>", 1)[1].split("</section>", 1)[0]
    assert "Some &lt;Wallet&gt;" in built and "We pick the venue for every swap" in built and "Since 2026-11-02" in built


def test_mcp_tools_on_the_build_page_match_the_api_index():
    index = json.loads((Path(bp.__file__).parent / "site" / "api-index.json").read_text())
    listed = re.search(r"Tools: ([a-z_, ]+)\.", bp.page(T)).group(1).split(", ")
    assert listed == index["mcp"]["tools"]
    assert index["build"]["url"] == "https://cantonvenues.com/build/"
