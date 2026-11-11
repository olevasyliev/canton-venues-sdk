"""Offline tests for the daily X post sent to the founder's chat (xpost.py)."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import channel  # noqa: E402
import collect  # noqa: E402
import xpost  # noqa: E402

MON = datetime(2026, 11, 9, 9, 5, tzinfo=UTC)
WED = datetime(2026, 11, 11, 9, 5, tzinfo=UTC)
CHECKED = {"tradecraft", "cantex", "temple", "rocky"}
PUB = {"venues": {
    "temple": {"head": {"rule": "spot_volume", "title": "The largest spot venue on Canton",
                        "sub": "$19.3M traded in the last 24 hours, 92% of Canton DEX spot volume"}},
    "tradecraft": {"head": {"rule": "best_count", "title": "Best price on 19 of 32 CC trades at $1K",
                            "sub": "Pool fees, price impact and network fees included"}},
    "ekiden": {"head": {"rule": "read", "title": "Ekiden on Canton",
                        "sub": "$122K of perpetuals traded in the last 24 hours"}}}}
CARDS = {"temple": 5, "tradecraft": 6, "ekiden": 7}
WEEK = {"slug": "2026-11-08", "t": 7, "range": "2 to 8 Nov 2026", "total_usd": 142_820_580, "change": -0.05344,
        "shares_shown": ["90.0%", "8.2%", "1.1%", "0.7%"],
        "venues": [{"slug": "temple", "name": "Temple"}, {"slug": "cantex", "name": "Cantex"},
                   {"slug": "rocky", "name": "Rocky"}, {"slug": "tradecraft", "name": "Tradecraft"}],
        "left_out": [{"slug": "pool-party", "name": "Pool Party", "why": "no daily figure for 2 Nov"}]}


def _samples(now: int, n: int = 30) -> list[dict]:
    return [{"t": now - 3600 * h, "rows": [
        ["USDCX", "sell", 1000, "cantex", 9.0, "tradecraft" if h % 3 else "temple", 10.0],
        ["USDCX", "buy", 10000, "temple", 30.0, "temple", 20.0]],
        "network_fee": {"rocky": {"basis": "assumed", "point": 1.5, "unit": "cc"}},
        "venues": {"USDCX": ["temple", "tradecraft", "rocky"]}} for h in range(n)]


def test_x_counts_a_link_as_23_and_a_wide_character_as_two():
    assert xpost.x_len("abc") == 3
    assert xpost.x_len("see cantonvenues.com/weekly/2026-11-08/") == 4 + 23
    assert xpost.x_len("−1.2%") == 6  # the minus sign is outside X's single-weight ranges


def test_weekly_post_tags_each_venue_and_says_who_is_left_out():
    post = xpost.weekly(WEEK, MON)
    assert post.text.startswith("Spot volume on Canton venues, 2 to 8 Nov: $142.8M, down 5.3% on the same venues")
    assert "@temple_ny 90.0%, @cantex_io 8.2%, @Rocky_exchange 1.1%, @TradecraftFi 0.7%." in post.text
    assert "Not counted: Pool Party" in post.text
    assert post.photo == "https://cantonvenues.com/weekly/2026-11-08/card.png?v=7"
    assert post.reply.endswith("cantonvenues.com/weekly/2026-11-08/")
    assert "cantonvenues.com" not in post.text  # the link goes in the reply
    assert xpost.weekly({**WEEK, "slug": "2026-11-07"}, MON) is None  # a held card


def test_venue_post_reads_the_card_headline_as_a_sentence():
    post, turn = xpost.venue(PUB, 0, CARDS, CHECKED)
    assert post.text == ("Temple (@temple_ny): the largest spot venue on Canton. "
                         "$19.3M traded in the last 24 hours, 92% of Canton DEX spot volume.")
    assert post.photo.endswith("/venues/temple/card.png?v=5")
    post, turn = xpost.venue(PUB, turn, CARDS, CHECKED)
    assert post.text == ("Tradecraft (@TradecraftFi): best price on 19 of 32 CC trades at $1K right now. "
                         "Pool fees, price impact and network fees included.")
    post, _ = xpost.venue(PUB, turn, CARDS, CHECKED)
    assert post.text == "Ekiden (@ekidenfi) on Canton: $122K of perpetuals traded in the last 24 hours."
    # a price headline without a passing quote check is skipped, as in the channel
    post, _ = xpost.venue(PUB, 1, CARDS, set())
    assert post.text.startswith("Ekiden")


def test_execution_post_tags_the_winners_and_puts_the_method_in_the_reply(monkeypatch):
    monkeypatch.setattr(channel, "EXEC_SINCE", 0)
    post = xpost.execution(_samples(int(WED.timestamp())), CHECKED)
    assert "$1K: @TradecraftFi 66%, @temple_ny 33%" in post.text
    assert "$10K: @temple_ny 100%" in post.text and post.photo is None
    assert post.reply.startswith("The runner-up returned a median 0.10% less at $1K, 0.20% less at $10K.")
    assert "Rocky publishes no network fee, so its fee is assumed at 1.5 CC" in post.reply
    assert xpost.execution([], CHECKED) is None


def test_pegs_post_lists_stablecoins_and_keeps_within_the_limit(monkeypatch):
    now = int(WED.timestamp())
    ts = [now - 300 * i for i in range(7 * 288)][::-1]
    prem = {f"S{i}": [0.001] * len(ts) for i in range(30)}
    prem["S0"] = [-0.012] * len(ts)
    prem["CC"] = [-0.004] * len(ts)
    tokens = [{"key": k, "symbol": f"STABLE{k}", "liquidity_usd": 1_000_000} for k in prem if k != "CC"]
    monkeypatch.setattr(channel, "STABLES", set(prem) - {"CC"})
    post = xpost.pegs({"t": ts, "premium": prem}, tokens, {t["key"]: t["symbol"] for t in tokens} | {"CC": "CC"}, now)
    assert post.text.startswith("Stablecoin prices in Canton pools and order books, last 7 days: the share")
    assert "\n\nSTABLES0 0%, furthest −1.20% for 30 min\nSTABLES1 100%" in post.text  # the slipped one first
    assert xpost.x_len(post.text) <= 280 and xpost.x_len(post.reply) <= 280  # 30 coins trimmed to fit
    assert "CC −0.40% vs global CC markets" in post.reply


def test_build_falls_back_to_a_venue_card_and_moves_the_turn():
    state = {}
    src = {"now": WED, "published": PUB, "card_v": CARDS, "samples": []}
    post = xpost.build("execution", src, state, set())  # no passing quote check: no price post
    assert post.kind == "venue" and state["venue_turn"] == 1  # temple (first in the site's order) used
    post = xpost.build("weekly", {**src, "now": MON, "weekly": {**WEEK, "slug": "2026-11-01"}}, state, CHECKED)
    assert post.kind == "venue" and post.text.startswith("Tradecraft")
    assert xpost.build("weekly", {**src, "now": MON, "weekly": WEEK}, {}, CHECKED).kind == "weekly"
    assert xpost.build("venue", {"now": WED}, {}, CHECKED) is None  # nothing to post at all


def test_due_once_a_day_every_day_and_messages_say_what_to_do():
    assert xpost.due({}, WED) and not xpost.due({}, WED.replace(hour=8))
    assert not xpost.due({"date": "2026-11-11"}, WED)
    assert set(xpost.SCHEDULE) == set(range(7))
    post, _ = xpost.venue(PUB, 0, CARDS, CHECKED)
    msgs = xpost.messages(post, WED)
    assert [p for _, p in msgs] == [post.photo, None, None]
    assert msgs[0][0].startswith("X post for Wed 11 Nov. Save this card")
    assert msgs[1][0] == post.text and msgs[2][0] == post.reply


def test_the_collector_sends_the_draft_to_the_founder_never_to_the_channel(tmp_path, monkeypatch):
    out = tmp_path / "www"
    (out / "venues" / "temple").mkdir(parents=True)
    (out / "venues" / "temple" / "card.png").write_bytes(b"png")
    (out / "venues" / "published.json").write_text(json.dumps(PUB))
    c = collect.Collector(out)
    asyncio.run(c.http.aclose())
    sent = []

    async def fake(body, photo=None, chat=None, html=True):
        sent.append((body, photo, chat, html))
        return True
    monkeypatch.setattr(c, "_telegram", fake)

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return WED
    monkeypatch.setattr(collect, "datetime", Fixed)
    monkeypatch.delenv("CONTACT_CHAT_ID", raising=False)
    asyncio.run(c._x_morning([]))
    assert sent == [] and not (c.state / "x.json").exists()  # no founder chat: nothing, never the channel
    monkeypatch.setenv("CONTACT_CHAT_ID", "42")
    asyncio.run(c._x_morning([]))
    assert [(chat, html) for _, _, chat, html in sent] == [("42", False)] * 3
    assert sent[1][0].startswith("Temple (@temple_ny)")
    asyncio.run(c._x_morning([]))  # once a day
    assert len(sent) == 3
    assert json.loads((c.state / "x.json").read_text())["posts"][-1]["sent"] == "venue"
