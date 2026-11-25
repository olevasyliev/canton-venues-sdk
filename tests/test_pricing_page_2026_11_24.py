"""The pricing page (24.11): three tiers on top of the open API, a request form that reaches contact.py,
and the menu, sitemap and API notice that point to it."""

from __future__ import annotations

import json
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "examples" / "canton_venues"
sys.path.insert(0, str(HERE))

import collect  # noqa: E402
import contact  # noqa: E402
import pricing_page as pp  # noqa: E402
import venue_pages as vp  # noqa: E402

T = 1795530000


def test_the_page_shows_three_tiers_in_order_with_their_prices():
    html = pp.page(T)
    names = re.findall(r'<div class="tier" id="plan-(\w+)">\s*<h2>([^<]+)</h2>', html)
    assert names == [("open", "Open"), ("pro", "Pro"), ("partner", "Data partner")]
    prices = re.findall(r'<div class="tprice">(.*?)</div>', html)
    assert prices == ["<b>$0</b>", "<b>$199</b><span>a month</span>", "<span>from</span><b>$3,999</b><span>a month</span>"]
    assert '<link rel="canonical" href="https://cantonvenues.com/pricing/">' in html


def test_each_paid_tier_opens_the_form_on_its_own_plan_and_the_form_offers_only_known_plans():
    html = pp.page(T)
    assert re.findall(r'data-plan="(\w+)"', html) == ["pro", "partner"]
    assert re.findall(r'name="plan" value="(\w+)"', html) == list(pp.PLANS)
    assert set(pp.PLANS) == set(contact.PLAN_NAMES)
    assert '"kind": "pricing"' not in html and 'kind: "pricing"' in html  # the JS sends it
    assert 'id="request"' in html and 'id="reqform"' in html


def test_venues_cannot_buy_a_place_and_the_open_api_stays_free():
    html = pp.page(T)
    assert "Venues cannot buy a place" in html and "The open API is free and stays free" in html
    # the hourly best-execution record is promised as an open dataset (#778): never sold here
    assert "best-execution" not in html.lower() and "hourly" not in html.lower()


def test_the_footer_no_longer_claims_the_prices_are_in_the_api():
    html = pp.page(T)
    assert "Every number on this page is in the open JSON API" not in html
    assert "Every market number on this site is in the open JSON API" in html


def test_pricing_sits_in_the_menu_after_build_on_every_page():
    nav = vp.site_nav()
    assert '<a href="/build/">Build</a><a href="/pricing/">Pricing</a>' in nav
    assert '<a href="/pricing/">Pricing</a>' in pp.page(T)
    index = (HERE / "site" / "index.html").read_text()
    assert '<a href="/build/">Build</a><a href="/pricing/">Pricing</a>' in index


def test_sitemap_lists_the_pricing_page():
    assert "<loc>https://cantonvenues.com/pricing/</loc>" in vp.sitemap({}, T)


def test_the_bot_notice_points_to_pro_and_keeps_the_free_registration():
    assert "https://cantonvenues.com/pricing/" in collect.BOT_NOTICE
    assert "https://cantonvenues.com/build/#register" in collect.BOT_NOTICE
    assert "open and key-free" in collect.BOT_NOTICE
    index = json.loads((HERE / "site" / "api-index.json").read_text())
    assert "https://cantonvenues.com/pricing/" in index["bots"]


def test_a_pricing_request_needs_a_known_plan_a_name_and_a_contact():
    entry, why = contact.read_form({"kind": "pricing", "plan": "pro", "app": "Arb Desk", "contact": "@desk",
                                    "message": "we poll every two minutes"})
    assert why == "" and entry == {"kind": "pricing", "plan": "pro", "app": "Arb Desk", "contact": "@desk",
                                   "message": "we poll every two minutes"}
    assert contact.read_form({"kind": "pricing", "plan": "gold", "app": "Arb", "contact": "@d"}) == (None, "unknown plan")
    assert contact.read_form({"kind": "pricing", "plan": "pro", "app": "A", "contact": "@d"})[0] is None
    assert contact.read_form({"kind": "pricing", "plan": "partner", "app": "Data Co", "contact": " "})[0] is None
    assert len(contact.read_form({"kind": "pricing", "plan": "pro", "app": "Arb", "contact": "@d",
                                  "message": "x" * 9999})[0]["message"]) == contact.LIMITS["message"]


def test_a_pricing_request_reads_well_in_telegram_and_is_escaped():
    entry, _ = contact.read_form({"kind": "pricing", "plan": "partner", "app": "<b>Data</b> & Co", "contact": "a<b"})
    text = contact.notify_text(entry)
    assert text.startswith("<b>cantonvenues.com: pricing request</b>\nPlan: Data partner, from $3,999 a month\n")
    assert "From: &lt;b&gt;Data&lt;/b&gt; &amp; Co" in text and "Reply to: a&lt;b" in text
    assert text.endswith("(nothing on what they need)")


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


def test_a_pricing_request_lands_in_the_record_and_a_bad_one_does_not(server):
    srv, log = server
    assert _post(srv, {"kind": "pricing", "plan": "pro", "app": "Arb Desk", "contact": "@desk"}) == 200
    assert _post(srv, {"kind": "pricing", "plan": "vip", "app": "Arb Desk", "contact": "@desk"}) == 422
    assert _post(srv, {"kind": "pricing", "plan": "pro", "app": "Bot", "contact": "@b", "website": "x"}) == 200  # honeypot
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert [(r["kind"], r["plan"]) for r in rows] == [("pricing", "pro")]


def test_pro_is_pitched_to_research_with_the_archive_first():
    # F 25.11: "for bots" was weak (real bots read venues directly); Pro's uncopyable part is the archive
    pro = next(t for t in pp.TIERS if t["id"] == "pro")
    assert pro["desc"] == "For research and backtests on Canton market history."
    assert pro["items"][0].startswith("Archive since 27")
    html = pp.page(T)
    assert "For research and backtests" in html and "For bots" not in html
