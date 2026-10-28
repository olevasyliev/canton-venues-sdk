"""Offline tests for the Canton Venues per-venue pages and share cards."""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import html

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import venue_pages as vp

T = 1791380618  # 2026-10-07 13:43 UTC


def _venue(i, name, kind, vol, status="priced"):
    return {"id": i, "name": name, "kind": kind, "note": f"{name} note.", "status": status, "volume_24h_usd": vol}


def _row(side, size, out, best):
    return {"side": side, "size_usd": size, "out": out, "best": best, "edge_bps": 10.0}


def data():
    venues = [
        _venue("temple", "Temple", "Spot order book", 34_000_000),
        _venue("cantex", "Cantex", "Spot AMM", 4_000_000),
        _venue("tradecraft", "Tradecraft", "Spot AMM", 200_000),
        _venue("rocky", "Rocky", "Spot order book", 2_800_000),
        _venue("rocky_perp", "Rocky perps", "Perpetuals", 4_600_000),
        _venue("ekiden", "Ekiden", "Perpetuals", 280_000),
        _venue("poolparty", "Pool Party", "Spot AMM", 2_600),
        _venue("oneswap", "OneSwap", "Spot AMM", None),
        _venue("silvana", "Silvana", "Private order book", None, status="closed"),
    ]
    small, big = {"cantex": 1.01, "tradecraft": 1.0}, {"cantex": 1.0, "tradecraft": 1.1}
    pairs = [
        {"key": "USDCX", "kind": "cc", "symbol": "USDCx", "venues": ["cantex", "tradecraft"], "rows": [
            _row("sell", 100, dict(small), "cantex"), _row("buy", 100, dict(small), "cantex"),
            _row("sell", 50_000, dict(big), "tradecraft"), _row("buy", 50_000, dict(big), "tradecraft")]},
        {"key": "CBTC:USD", "kind": "usd", "token": "CBTC", "symbol": "CBTC",
         "venues": ["cantex", "temple", "rocky"], "rows": [
             _row("buy", 10_000, {"cantex": 1, "temple": 2, "rocky": 1.5}, "temple"),
             _row("sell", 10_000, {"cantex": 1, "temple": 2, "rocky": 1.5}, "temple"),
             _row("buy", 50_000, {"cantex": 1, "temple": 2, "rocky": None}, "temple"),
             _row("sell", 50_000, {"cantex": 1, "temple": 2, "rocky": None}, "temple")]},
    ]
    tokens = [
        {"symbol": "USDCx", "key": "USDCX", "venues": {
            "cantex": {"price_usd": 1.0, "liquidity_usd": 400_000, "depth_1pct_usd": 2_000},
            "tradecraft": {"price_usd": 1.0, "liquidity_usd": 1_200_000, "depth_1pct_usd": 3_000},
            "oneswap": {"price_usd": 0.99, "liquidity_usd": 36_000, "depth_1pct_usd": 180},
            "poolparty": {"price_usd": 1.0, "liquidity_usd": 12_000, "depth_1pct_usd": 60}}},
        {"symbol": "CBTC", "key": "CBTC", "venues": {
            "temple": {"price_usd": 83_000, "liquidity_usd": 490_000, "depth_1pct_usd": 490_000,
                       "market": "CBTC/USDCx", "spread_bps": 0.1},
            "rocky": {"price_usd": 83_000, "liquidity_usd": 36_000_000, "depth_1pct_usd": 36_000_000,
                      "market": "CBTC-USDCX", "spread_bps": 0.0}}},
    ]
    pool = lambda v, tvl: {"venue": v, "pair": "CC/USDCx", "tvl_usd": tvl, "volume_24h_usd": 1000.0,
                           "fee": 0.003, "fee_apr": 0.01}
    lp = [pool("tradecraft", 1_200_000), pool("cantex", 400_000), pool("oneswap", 36_000),
          pool("poolparty", 12_000)]
    perp = lambda v, base, vol, oi: {"venue": v, "symbol": base, "base": base, "quote": "USDCx",
                                     "last": 1.0, "mark": None, "index": None, "basis": -0.001,
                                     "funding_rate": None, "open_interest_usd": oi, "turnover_24h_usd": vol,
                                     "spread_bps": 1.0}
    perps = [perp("rocky_perp", "BTC", 4_000_000, None), perp("rocky_perp", "ETH", 600_000, None),
             perp("ekiden", "BTC", 200_000, 40_000), perp("ekiden", "ETH", 60_000, 10_000),
             perp("ekiden", "CC", 20_000, 2_000)]
    # DefiLlama's Canton DEX list as summary.json carries it: it confirms the "on Canton" spot rankings
    llama = [{"name": "Temple", "volume_24h": 34_500_000}, {"name": "Cantex", "volume_24h": 4_500_000},
             {"name": "Rocky Exchange Spot", "volume_24h": 2_600_000}, {"name": "Pool Party", "volume_24h": 2_900}]
    return {"venues": {"t": T, "venues": venues, "spot_volume_24h_usd": 41_002_600},
            "tokens": {"t": T, "tokens": tokens}, "execution": {"t": T, "pairs": pairs},
            "lp": {"t": T, "pools": lp}, "perps": {"t": T, "markets": perps},
            "summary": {"cc_usd": 0.12, "ecosystem": {"venues": llama}}}


def test_every_live_venue_gets_a_page_and_closed_ones_do_not():
    f = vp.facts(data())
    assert set(f) == {"temple", "cantex", "tradecraft", "rocky", "ekiden", "oneswap", "pool-party"}
    # Rocky's spot and perp rows are one venue with both kinds
    assert f["rocky"]["kind"] == "Spot order book + Perpetuals"
    assert f["rocky"]["perp_volume"] == 4_600_000


def test_headline_is_the_venues_best_fact():
    f = vp.facts(data())
    h = {s: vp.headline(x) for s, x in f.items()}
    assert h["temple"]["rule"] == "spot_volume" and "$34M" in h["temple"]["sub"]
    assert h["temple"]["title"] == "The largest spot venue on Canton" and h["temple"]["scope"] == "canton"
    assert h["temple"]["next"] == "Cantex"  # runner-up by spot volume
    assert h["cantex"]["rule"] == "kind_volume" and h["cantex"]["title"] == "The largest AMM on Canton by volume"
    assert h["rocky"]["rule"] == "perp_volume"
    # derivatives coverage is not confirmed by any outside list: stated plainly, the scope in one line under it
    assert h["rocky"]["title"] == "The largest perps venue" and h["rocky"]["scope"] == "read"
    assert vp.head_scope(f["rocky"], h["rocky"]) == "Based on the 7 Canton venues with public market data."
    assert vp.head_scope(f["temple"], h["temple"]) == ""  # confirmed chain-wide: no scope line
    # Tradecraft wins both $50K CC-pair quotes, but its first lead is its pool liquidity, now on its card;
    # the runner-up is kept in code for the margin rule
    tc = f["tradecraft"]["leads"][0]
    assert tc["rule"] == "tvl" and tc["next"] == "Cantex"
    assert h["tradecraft"]["rule"] == "tvl"
    assert h["ekiden"]["rule"] == "perp_markets" and "3 markets trading: BTC, ETH, CC" in h["ekiden"]["sub"]
    # leads nothing: a plain count, never a superlative, and no #1 on its page
    assert h["oneswap"]["rule"] == "pools" and "most" not in h["oneswap"]["title"].lower()
    assert "#1" not in vp.lead_html(f["oneswap"], h["oneswap"])
    assert '<b class="up">#1</b> Largest spot venue on Canton' in vp.lead_html(f["temple"], h["temple"])


def test_no_headline_or_card_tile_ranks_venues_on_price_across_sizes():
    d = data()
    for v in d["venues"]["venues"]:  # nobody leads a volume, pool or count ranking any more
        v["volume_24h_usd"] = None
    d["lp"]["pools"], d["perps"]["markets"], d["tokens"]["tokens"] = [], [], []
    for f in vp.facts(d).values():
        h = vp.headline(f)
        assert "best execution" not in h["title"].lower() and " from $" not in h["title"]
        assert not any(p["k"].startswith("Best price") for p in vp.stats(f))
    # a single quote won clearly is still a fact, named by direction and size, once the leader's own
    # network fee is known (Tradecraft's: $0.10-1.40, its fee page and its swap size)
    lead = vp.facts(d)["tradecraft"]["leads"][0]
    assert lead["rule"] == "best_quote" and lead["title"] == "Best price to buy USDCx with CC at $50K"


def test_a_thin_pool_is_not_a_competitor():
    d = data()
    rows = d["execution"]["pairs"][0]["rows"]
    for r in rows:  # Pool Party joins the CC/USDCx quotes and wins the $100 ones
        r["out"]["poolparty"] = 2.0
    rows[0]["best"] = rows[1]["best"] = "poolparty"
    usdcx = d["tokens"]["tokens"][0]["venues"]
    usdcx["poolparty"]["liquidity_usd"] = vp.MIN_LIQUIDITY_USD + 1
    assert vp.facts(d)["pool-party"]["exec"]["cc:100"] == {"won": 2, "of": 2}
    usdcx["poolparty"]["liquidity_usd"] = vp.MIN_LIQUIDITY_USD - 1
    f = vp.facts(d)
    assert "cc:100" not in f["pool-party"]["exec"]  # neither wins nor is counted
    assert f["cantex"]["exec"]["cc:100"] == {"won": 2, "of": 2}  # the win goes to the best real pool
    # a quote whose only alternative is thin counts for nobody
    usdcx["tradecraft"]["liquidity_usd"] = 500
    f = vp.facts(d)
    assert "cc:100" not in f["cantex"]["exec"] and "cc:100" not in f["tradecraft"]["exec"]


def test_a_tie_never_leads():
    d = data()
    lp = d["lp"]["pools"]
    lp[1]["tvl_usd"] = lp[0]["tvl_usd"]  # Cantex and Tradecraft level on pool liquidity
    assert all(x["rule"] != "tvl" for x in vp.facts(d)["tradecraft"]["leads"])


def test_method_lines_never_repeat():
    lines = vp.method_lines(["Order books from Temple's API (account key); settled volume per market. Taker fee 1 bp.",
                             "Taker fee 1 bp is Temple's published rate."])
    assert lines == ["Order books from Temple's API (account key).", "Settled volume per market.",
                     "Taker fee 1 bp is Temple's published rate."]
    for f in vp.facts(data()).values():
        assert len(f["method"]) == len(set(f["method"]))
    assert vp.method_lines(["MainNet tickers.", "MainNet tickers."]) == ["MainNet tickers."]


def test_card_ranks_only_the_top_half_and_never_of_two():
    panels = {p["k"]: p for p in vp.stats(vp.facts(data())["ekiden"])}
    # #2 of 2 is not shown, and no method on the tile (it is on the page)
    assert panels["Perps volume, 24h"]["n"] == ""
    # #1 of 2 shows as #1 alone
    assert {p["k"]: p for p in vp.stats(vp.facts(data())["rocky"])}["Perps volume, 24h"]["n"] == "#1"
    panels = {p["k"]: p for p in vp.stats(vp.facts(data())["temple"])}
    assert panels["Spot volume, 24h"]["n"] == "#1 of 5"
    assert vp.ordinal_rank(3, 5) == "#3 of 5" and vp.ordinal_rank(4, 5) == "" and vp.ordinal_rank(2, 2) == ""
    assert vp.page_rank(5, 5) == "#5 of 5" and vp.page_rank(1, 2) == "#1" and vp.page_rank(2, 2) == ""


class _Meta(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta, self.links, self.title = {}, [], None
        self._t = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            self.meta[a.get("property") or a.get("name")] = a.get("content")
        elif tag == "a":
            self.links.append(a.get("href"))
        self._t = tag == "title"

    def handle_data(self, data):
        if self._t and self.title is None:
            self.title = data


def test_page_carries_its_own_social_card_and_share_link(tmp_path):
    pytest.importorskip("PIL")
    vp.build(tmp_path, data(), T)
    page = (tmp_path / "venues" / "temple" / "index.html").read_text()
    p = _Meta()
    p.feed(page)
    assert p.title.startswith("Temple on Canton")
    assert p.meta["twitter:card"] == "summary_large_image"
    assert p.meta["og:url"] == "https://cantonvenues.com/venues/temple/"
    img = urlparse(p.meta["og:image"])
    assert img.path == "/venues/temple/card.png" and p.meta["twitter:image"] == p.meta["og:image"]
    assert (p.meta["og:image:width"], p.meta["og:image:height"]) == ("1200", "630")
    intent = next(h for h in p.links if h.startswith("https://x.com/intent/post"))
    q = parse_qs(urlparse(intent).query)
    assert q["text"][0].startswith("Where Temple (@temple_ny) leads on Canton Venues: the largest spot venue")
    assert "Largest spot venue on Canton" in page and "Next:" not in page and q["url"] == ["https://cantonvenues.com/venues/temple/"]
    # a plain card (no lead) tags the venue too, and the page links its X account
    one = (tmp_path / "venues" / "oneswap" / "index.html").read_text()
    oq = parse_qs(urlparse(next(h for h in _links(one) if "intent/post" in h)).query)
    assert oq["text"][0].startswith("OneSwap (@Oneswapcc) on Canton Venues")
    assert "https://x.com/Oneswapcc" in _links(one)
    assert 'href="temple/"' in (tmp_path / "venues" / "index.html").read_text()


def _links(page):
    p = _Meta()
    p.feed(page)
    return p.links


def test_cards_are_1200_by_630_and_kept_between_redraws(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    vp.build(tmp_path, data(), T)
    card = tmp_path / "venues" / "rocky" / "card.png"
    with Image.open(card) as im:
        assert im.size == (1200, 630) and im.format == "PNG"
    before = card.stat().st_mtime_ns
    vp.build(tmp_path, data(), T + 300, cards=False)
    assert card.stat().st_mtime_ns == before
    assert f"card.png?v={int(card.stat().st_mtime)}" in (card.parent / "index.html").read_text()


def test_an_unreachable_venue_keeps_its_last_page(tmp_path):
    pytest.importorskip("PIL")
    vp.build(tmp_path, data(), T)
    d = data()
    next(v for v in d["venues"]["venues"] if v["id"] == "ekiden")["status"] = "down"
    heads = vp.build(tmp_path, d, T + 300, cards=False)
    assert "ekiden" not in heads and (tmp_path / "venues" / "ekiden" / "index.html").exists()


def test_card_colours_are_the_dashboards_own():
    css = (Path(vp.HERE) / "site" / "index.html").read_text()
    for theme, block in (("light", css.split(':root[data-theme="dark"]')[0]),
                         ("dark", css.split(':root[data-theme="dark"]')[1].split("}")[0])):
        for key, var in (("bg", "--bg"), ("bg2", "--bg-2"), ("line", "--line"), ("line2", "--line-2"),
                         ("text", "--text"), ("text2", "--text-2"), ("accent", "--accent")):
            assert f"{var}: {vp.THEMES[theme][key]};" in block, (theme, var)
    assert f"--up: {vp.UP};" in css and f"--down: {vp.DOWN};" in css


def test_a_long_headline_splits_into_two_even_lines():
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    Image = pytest.importorskip("PIL.Image")
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    font = vp._font("Bold", 58)
    title = "Best price to sell HANDL for CC at $10K"
    lines = vp._balanced(d, title, font, 1088)
    assert lines and " ".join(lines) == title and len(lines) == 2
    widths = [d.textlength(x, font=font) for x in lines]
    assert max(widths) <= 1088 and min(widths) > 0.6 * max(widths)  # no orphaned last word
    assert vp._balanced(d, "word " * 80, font, 1088) is None


# === venue-style cards and outward text ======================================

import venue_style as vs  # noqa: E402

BANNED = ("·", "—", "–")  # middle dot, em dash, en dash: none in anything we publish


class _Text(HTMLParser):
    """A page's visible text plus its title and meta contents; styles and scripts left out."""

    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], False

    def handle_starttag(self, tag, attrs):
        self._skip = tag in ("style", "script")
        a = dict(attrs)
        if tag == "meta" and a.get("content"):
            self.parts.append(a["content"])
        if tag == "img" and a.get("alt"):
            self.parts.append(a["alt"])

    def handle_endtag(self, tag):
        self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def test_no_middle_dot_or_dash_in_cards_pages_or_share_text(tmp_path):
    pytest.importorskip("PIL")
    d = data()
    next(v for v in d["venues"]["venues"] if v["id"] == "temple")["markets_24h"] = [
        "CBTC/USDCx", "CC/USDCx", "eXAU/USDCx"]
    vp.build(tmp_path, d, T)
    for s, f in vp.facts(d).items():
        head = vp.headline(f)
        card = [head["title"], head["sub"], f"{f['name']} / {f['kind']}", vs.strip_line(), vs.footer_note(f, head),
                vp.stamp(T)] + [x for p in vp.stats(f) for x in (p["k"], p["v"], p["n"])]
        page = _Text()
        page.feed((tmp_path / "venues" / s / "index.html").read_text())
        for text in card + page.parts + [vp.share_text(f, head)]:
            assert not any(b in text for b in BANNED), (s, text)
    index = _Text()
    index.feed((tmp_path / "venues" / "index.html").read_text())
    assert not any(b in x for x in index.parts for b in BANNED)
    assert vs.strip_line() == "Live Canton DEX data from cantonvenues.com"


def test_token_tile_names_the_tokens_and_a_listing_venue_its_own_markets():
    d = data()
    f = vp.facts(d)
    tile = vp.token_tile(f["cantex"])
    assert tile["k"] == "Tokens" and tile["names"] == ["USDCx"] and "USDCx" in tile["v"]
    # the venue publishes its own market list: the tile is that list, in plain words, nothing "excluded"
    temple = next(v for v in d["venues"]["venues"] if v["id"] == "temple")
    temple["markets_24h"] = ["CBTC/USDCx", "CC/USDCx", "eXAU/USDCx"]
    temple["markets_24h_usd"] = {"CBTC/USDCx": 17_100_000.0, "CC/USDCx": 2_400_000.0, "eXAU/USDCx": 8_800_000.0}
    f = vp.facts(d)
    tiles = {p["k"]: p for p in vp.stats(f["temple"])}
    assert tiles["Markets"]["names"] == ["CBTC", "CC", "eXAU"] and tiles["Markets"]["n"] == "3 traded in the last 24h"
    # in place of the keyed book's depth: its share and its largest market, computed
    assert tiles["Largest market"]["v"] == "$17.1M" and tiles["Largest market"]["n"] == "CBTC/USDCx, 24h"
    assert tiles["Share of spot volume"]["n"] == "of Canton DEX spot volume"
    assert not any("excluded" in p["n"] or "we price" in p["k"].lower() for p in vp.stats(f["temple"]))
    assert any("we price 1 of them as tokens (CBTC)" in m for m in f["temple"]["method"])
    # many names: four at most, then a count of the rest
    assert vp.names_text(["A", "B", "C", "D", "E", "F"]) == "A, B, C, D +2"
    assert vp.names_text(["A", "B", "C"]) == "A, B, C"


def test_a_long_name_list_drops_names_before_it_shrinks_past_the_floor():
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    Image = pytest.importorskip("PIL.Image")
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    p = {"k": "Tokens priced", "names": ["USDC.B", "USDCx", "EDELx", "eXAU", "CBTC", "HECTO"]}
    text, font = vp.fit_value(d, p, lambda z: vp._font("Bold", z), 440, 84, 44)
    assert d.textlength(text, font=font) <= 440 and font.size >= 44
    assert text.startswith("USDC.B") and text.endswith(f"+{6 - len(text.split(', '))}")


def test_perp_tile_agrees_with_the_headline():
    d = data()
    d["perps"]["markets"].append(dict(d["perps"]["markets"][-1], base="XAU", symbol="XAU", turnover_24h_usd=1.0))
    f = vp.facts(d)["ekiden"]
    head = vp.headline(f)
    tile = vp.perp_tile(f)
    assert head["sub"].startswith("3 markets trading")
    assert tile["names"] == ["BTC", "ETH", "CC"] and tile["n"] == "3 trading, 4 listed"
    assert vp.perp_tile(vp.facts(data())["rocky"])["n"] == "2 listed, all trading"


def test_style_table_covers_every_venue_with_open_fonts(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    assert set(vs.STYLES) == {v["slug"] for v in vp.VENUES}
    keys = {"source", "bg", "ink", "muted", "accent", "rule", "tile", "tile_edge", "tile_label", "tile_ink",
            "tile_note", "up", "head", "label", "num", "body", "head_case", "label_case", "track", "radius",
            "border"}
    licences = {p.name.split("-")[0] for p in vp.FONTS.glob("*OFL.txt")} | {"Inter"}  # Inter's is OFL.txt
    for slug, s in vs.STYLES.items():
        assert keys <= set(s), (slug, keys - set(s))
        assert s["source"].startswith("https://"), slug
        for face in {s["head"], s["label"], s["num"], s["body"], s.get("names", s["num"])}:
            assert (vp.FONTS / f"{face}.ttf").exists(), face
            assert face.split("-")[0] in licences, face
    # every venue renders in its own style, 1200x630; our two footer lines sit on the card's own
    # background, not on a white strip
    f = vp.facts(data())
    for slug, x in f.items():
        out = tmp_path / f"{slug}.png"
        vs.render(x, vp.headline(x), T, out)
        with Image.open(out) as im:
            assert im.size == (1200, 630)
            s = vs.STYLES[slug]
            px, want = im.convert("RGB").getpixel((1190, 620)), vp._hex(s.get("bg2", s["bg"]))
            assert max(abs(x - y) for x, y in zip(px, want)) <= 3, slug  # a gradient lands within a step


def test_our_own_look_stays_available(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    vp.build(tmp_path, data(), T, look="ours")
    with Image.open(tmp_path / "venues" / "temple" / "card.png") as im:
        assert im.convert("RGB").getpixel((5, 300)) == vp._hex(vp.THEMES["light"]["bg"])


# === the verifier's fixes (2026-10-07) ======================================

def test_an_unpriced_cc_pool_still_counts_from_its_cc_side():
    d = data()
    # Pool Party's fifth pool pairs CC with a token no venue names: 2x its CC reserve, never dropped
    d["lp"]["unpriced_pools"] = [{"venue": "poolparty", "pair": "CC/481871d4", "tvl_usd": 54_000.0,
                                  "reason": "token not named by any venue we price"}]
    f = vp.facts(d)["pool-party"]
    assert f["tvl"] == 66_000 and f["pool_n"] == 2 and f["pool_priced"] == 1
    assert vp.headline({**f, "leads": []})["sub"] == "$66K in liquidity across 2 CC pools"
    assert vp.pools_text(f) == "2 CC pools"  # no "1 priced": every pool counts from its CC side
    tiles = {p["k"]: p for p in vp.stats(f)}
    assert tiles["In pools"]["v"] == "$66K" and tiles["In pools"]["n"] == "2 CC pools"
    assert tiles["Largest pool"]["n"] == "CC and an unnamed token"  # the unnamed pool is the largest
    assert any("counted from the CC side" in m for m in f["method"])
    # the same rule for every AMM: an unpriced pool on Cantex counts in its total and its pool count
    d["lp"]["unpriced_pools"].append({"venue": "cantex", "pair": "CC/USDCx", "tvl_usd": 5_000.0,
                                      "reason": "a second pool for the same token"})
    assert vp.facts(d)["cantex"]["tvl"] == 405_000


def test_volume_is_labelled_as_the_venue_reports_it_only_when_it_reports_dollars():
    f = vp.facts(data())
    assert vp.volume_note("temple", 0.12) == vp.volume_note("tradecraft", 0.12) == "as the venue reports it"
    assert vp.volume_note("poolparty", 0.1177) == "CC side at $0.1177 per CC"
    assert vp.volume_note("cantex", 0.1177) == "CC volume at $0.1177 per CC"
    assert vp.volume_note("rocky", 0.12) == "quote-token turnover"
    pp = {p["k"]: p for p in vp.stats(f["pool-party"])}
    # the tile carries no method ("CC side at $0.12 per CC"): bottom of the ranking, under 1% share
    assert pp["Spot volume, 24h"]["n"] == ""
    cx = {p["k"]: p for p in vp.stats(f["cantex"])}
    assert cx["Spot volume, 24h"]["n"] == "#2 of 5"
    head = vp.headline(f["pool-party"])
    assert vp.card_notes(f["pool-party"], head) == "Independent data, not affiliated with Pool Party."
    method = vp.card_method(f["pool-party"], head)
    assert "24h volume: the CC side of each CC pool, converted at $0.1200 per CC." in method
    assert not any("as Pool Party reports it" in x for x in method)


def test_a_lead_needs_a_clear_margin_over_the_runner_up():
    d = data()
    lp = d["lp"]["pools"]
    lp[1]["tvl_usd"] = lp[0]["tvl_usd"] / 1.05  # Tradecraft only 5% ahead of Cantex on pool liquidity
    f = vp.facts(d)["tradecraft"]
    assert all(x["rule"] != "tvl" for x in f["leads"])
    assert {"rule": "tvl", "next": "Cantex"} == {k: f["near"][0][k] for k in ("rule", "next")}
    # it falls to the next category the rule finds: its USDCx pool, 50% deeper than Cantex's
    assert f["leads"][0]["rule"] == "token_depth" and f["leads"][0]["margin"] == pytest.approx(0.5)
    lp[1]["tvl_usd"] = lp[0]["tvl_usd"] / (1 + vp.LEAD_MARGIN)  # exactly at the margin: a lead
    assert vp.facts(d)["tradecraft"]["leads"][0]["rule"] == "tvl"
    assert vp._lead({"a": 109, "b": 100}, "a") is None and vp._lead({"a": 111, "b": 100}, "a")[0] == "b"


def test_a_best_price_lead_must_survive_known_network_fees():
    d = data()
    d["execution"]["pairs"].append({"key": "HANDL", "kind": "cc", "symbol": "HANDL", "venues": ["cantex", "oneswap"],
                                    "rows": [_row("sell", 1000, {"oneswap": 1.0071, "cantex": 1.0}, "oneswap")]})
    d["tokens"]["tokens"].append({"symbol": "HANDL", "key": "HANDL", "venues": {
        "cantex": {"liquidity_usd": 50_000}, "oneswap": {"liquidity_usd": 100_000}}})
    lead = next(x for x in vp.facts(d)["oneswap"]["leads"] if x["rule"] == "best_quote")
    assert lead["title"] == "Best price to buy HANDL with CC at $1K"
    assert lead["sub"].endswith("pool fees, price impact and network fees included")
    # 71 bp quoted; OneSwap charged its $2 network fee (20 bp at $1K), Cantex 0.86 CC at $0.12 (1 bp)
    assert lead["margin"] * 10_000 == pytest.approx(71 - 20 + 1, abs=1)
    # 25 bp quoted is gone once OneSwap's fee is paid: not a lead
    d["execution"]["pairs"][-1]["rows"][0]["out"]["oneswap"] = 1.0025
    assert not any(x["rule"] == "best_quote" for x in vp.facts(d)["oneswap"]["leads"])
    # a venue whose fee we do not know never wins on the fee we know for the other: 5 bp quoted is not 10
    d["execution"]["pairs"][-1]["rows"][0]["out"] = {"poolparty": 1.0005, "cantex": 1.0}
    d["tokens"]["tokens"][-1]["venues"]["poolparty"] = {"liquidity_usd": 50_000}
    assert not any(x["rule"] == "best_quote" for x in vp.facts(d)["pool-party"]["leads"])


def test_on_canton_only_where_an_outside_list_confirms_it():
    assert vp.CATEGORIES["spot_volume"]["source"] == vp.LLAMA_DEXS
    assert all(c["scope"] == "read" for r, c in vp.CATEGORIES.items() if r not in ("spot_volume", "kind_volume"))
    d = data()
    d["summary"]["ecosystem"]["venues"] = []  # no outside list: nothing is claimed for the whole chain
    h = {s: vp.headline(x) for s, x in vp.facts(d).items()}
    assert h["temple"]["title"] == "The largest spot venue" and h["temple"]["scope"] == "read"
    assert h["cantex"]["title"] == "The largest AMM by volume"
    assert vp.head_scope(vp.facts(d)["temple"], h["temple"]).startswith("Based on the 7 Canton venues")
    # DefiLlama lists a bigger spot venue than ours: Temple's lead stays scoped to what we read
    d["summary"]["ecosystem"]["venues"] = [{"name": "Temple", "volume_24h": 1}, {"name": "Elsewhere", "volume_24h": 9}]
    assert vp.headline(vp.facts(d)["temple"])["scope"] == "read"
    # a larger entry we cannot place by kind also blocks "the largest AMM on Canton"
    d["summary"]["ecosystem"]["venues"] = [{"name": "Cantex", "volume_24h": 4}, {"name": "Elsewhere", "volume_24h": 9}]
    assert vp.headline(vp.facts(d)["cantex"])["scope"] == "read"
    for x in vp.facts(data()).values():
        for lead in x["leads"]:
            assert ("on Canton" in lead["title"]) == (lead["scope"] == "canton"), lead["title"]


def test_a_keyed_book_never_appears_on_the_card():
    d = data()
    f = vp.facts(d)["temple"]
    tiles = {p["k"]: p for p in vp.stats(f)}
    assert "Book depth within 1%" not in tiles and tiles["Share of spot volume"]["v"] == "83%"
    assert not any(x["rule"] == "token_depth" for x in f["leads"])
    # Rocky's book is public: its depth tile stays, labelled as book depth, and names the pair
    rocky = {p["k"]: p for p in vp.stats(vp.facts(d)["rocky"])}
    assert rocky["Book depth within 1%"]["n"] == "CBTC/USDCx"


def test_one_money_style_and_tokens_lead_with_non_stablecoins():
    assert [vp.short_money(x) for x in (60_100, 2_636, 84_000, 1_200_000, 490_000, 512, 2.65, 999_960)] == [
        "$60.1K", "$2.6K", "$84K", "$1.2M", "$490K", "$512", "$2.65", "$1M"]
    assert vp.money(34_115_904) == vp.short_money(34_115_904) == "$34.1M"
    f = {"name": "X", "tokens": [{"symbol": s, "key": k, "liquidity_usd": 5_000} for s, k in (
        ("USDC.B", "USDC.B"), ("USDCx", "USDCX"), ("EDELx", "EDELX"), ("eXAU", "EXAU"))]}
    assert vp.token_tile(f)["names"] == ["EDELx", "eXAU", "USDC.B", "USDCx"]


def test_card_footer_is_one_short_line_and_the_method_moves_to_the_page(tmp_path):
    pytest.importorskip("PIL")
    vp.build(tmp_path, data(), T)
    for slug, f in vp.facts(data()).items():
        head = vp.headline(f)
        scope = vp.head_scope(f, head)
        assert vp.card_notes(f, head) == (f"{scope} Independent, not affiliated with {f['name']}." if scope
                                          else f"Independent data, not affiliated with {f['name']}.")
        page = (tmp_path / "venues" / slug / "index.html").read_text()
        for p in vp.stats(f):
            note = vp.metric_note(p["m"], f)
            assert note in vp.card_method(f, head) and html.escape(note) in page, (slug, p["m"])


# === second verifier pass (2026-10-07) ======================================

def test_token_count_names_and_footer_apply_the_1k_rule_on_this_venue():
    d = data()
    # HANDL has $500K on Cantex but $300 on Tradecraft: it counts for Cantex only
    d["tokens"]["tokens"].append({"symbol": "HANDL", "key": "HANDL", "venues": {
        "cantex": {"price_usd": 0.1, "liquidity_usd": 500_000, "depth_1pct_usd": 2_500},
        "tradecraft": {"price_usd": 0.1, "liquidity_usd": 300, "depth_1pct_usd": 2}}})
    f = vp.facts(d)
    tc, cx = vp.token_facts(f["tradecraft"]), vp.token_facts(f["cantex"])
    assert (tc["n"], tc["names"], [x["symbol"] for x in tc["thin"]]) == (1, ["USDCx"], ["HANDL"])
    assert (cx["n"], cx["names"]) == (2, ["HANDL", "USDCx"])
    for slug, t in (("tradecraft", tc), ("cantex", cx)):
        tile = vp.token_tile(f[slug])
        assert tile["names"] == t["names"] and tile["n"] == f"{t['n']} with $1K+ liquidity"
        head = vp.headline(f[slug])
        assert (f"Tokens: the {t['n']} with $1K or more of liquidity on {f[slug]['name']} itself."
                in vp.card_method(f[slug], head))
    # an order book is held to its depth within 1% of mid, not its total liquidity
    rows = [{"symbol": "A", "key": "A", "market": "A/USDCx", "liquidity_usd": 9_000, "depth_1pct_usd": 400},
            {"symbol": "B", "key": "B", "market": "B/USDCx", "liquidity_usd": 9_000, "depth_1pct_usd": 4_000},
            {"symbol": "C", "key": "C", "liquidity_usd": None}]
    ok, thin = vp.token_split(rows)
    assert [x["symbol"] for x in ok] == ["B"] and [x["symbol"] for x in thin] == ["A", "C"]


def test_page_lists_thin_tokens_apart_and_counts_only_the_rest(tmp_path):
    pytest.importorskip("PIL")
    d = data()
    d["tokens"]["tokens"].append({"symbol": "HANDL", "key": "HANDL", "venues": {
        "tradecraft": {"price_usd": 0.1, "liquidity_usd": 300, "depth_1pct_usd": 2},
        "cantex": {"price_usd": 0.1, "liquidity_usd": 500_000, "depth_1pct_usd": 2_500}}})
    vp.build(tmp_path, d, T)
    page = (tmp_path / "venues" / "tradecraft" / "index.html").read_text()
    head, _, thin = page.partition("Thin, under $1K of liquidity on Tradecraft")
    assert thin and "HANDL" in thin and "HANDL" not in head.split("<h2>Tokens</h2>")[1]
    assert "The 1 token with $1K or more of liquidity on Tradecraft itself" in page
    assert "Thin, under" not in (tmp_path / "venues" / "cantex" / "index.html").read_text()


def test_best_price_sub_names_what_you_receive():
    row = {"symbol": "HANDL", "kind": "cc", "side": "sell", "edge_bps": 87.0}
    assert vp.best_quote_sub(row) == ("0.87% more HANDL than the next best venue, pool fees, price "
                                      "impact and network fees included")
    # with fee figures on the row, the edge shown is the one after each venue's network fee
    assert vp.best_quote_sub({**row, "edge_after_fee_bps": 42.0}).startswith("0.42% more HANDL ")
    assert vp.best_quote_sub({**row, "side": "buy"}).startswith("0.87% more CC than the next best venue")
    assert vp.best_quote_sub({**row, "kind": "usd", "side": "sell"}).startswith("0.87% more dollars ")
    d = data()
    d["execution"]["pairs"].append({"key": "HANDL", "kind": "cc", "symbol": "HANDL", "venues": ["cantex", "oneswap"],
                                    "rows": [_row("sell", 1000, {"oneswap": 1.0087, "cantex": 1.0}, "oneswap")]})
    d["tokens"]["tokens"].append({"symbol": "HANDL", "key": "HANDL", "venues": {
        "cantex": {"liquidity_usd": 50_000}, "oneswap": {"liquidity_usd": 100_000}}})
    lead = next(x for x in vp.facts(d)["oneswap"]["leads"] if x["rule"] == "best_quote")
    assert lead["sub"].startswith("0.87% more HANDL than the next best venue")


def test_a_venue_can_still_be_set_to_a_plain_card():
    assert all(v.get("lead", True) for v in vp.VENUES)  # every venue shows its lead today
    f = vp.facts(data())["tradecraft"]
    h = vp.headline({**f, "venue": {**f["venue"], "lead": False}})
    assert h["rule"] == "pools" and h["title"] == "Tradecraft on Canton"
    assert "#1" not in vp.lead_html(f, h) and "most" not in vp.share_text(f, h).lower()
    tiles = [p["k"] for p in vp.stats(f)]
    assert not any("1% of mid" in k for k in tiles) and "Largest pool" in tiles


# === third pass: price leads need a known fee and $1K (2026-10-07) ===========

def _edel(d, size, out):
    d["execution"]["pairs"].append({"key": "EDELX", "kind": "cc", "symbol": "EDELx",
                                    "venues": list(out), "rows": [_row("buy", size, out, max(out, key=out.get))]})
    d["tokens"]["tokens"].append({"symbol": "EDELx", "key": "EDELX", "venues": {
        v: {"liquidity_usd": 80_000} for v in out}})
    return vp.facts(d)


def test_a_venue_whose_own_network_fee_is_unknown_never_leads_on_price():
    assert "poolparty" not in vp.NETWORK_FEE
    # Pool Party 2% ahead of Tradecraft at $10K: a clear quote, but Pool Party's fee is unknown
    f = _edel(data(), 10_000, {"poolparty": 1.02, "tradecraft": 1.0})
    assert not any(x["rule"] == "best_quote" for x in f["pool-party"]["leads"])
    assert vp.headline(f["pool-party"])["rule"] == "pools"
    assert vp.headline(f["pool-party"])["title"] == "Pool Party on Canton"
    # OneSwap's documented $1.5-2 counts: charged $2 (2 bp at $10K), the runner-up charged nothing
    f = _edel(data(), 10_000, {"oneswap": 1.02, "tradecraft": 1.0})
    lead = next(x for x in f["oneswap"]["leads"] if x["rule"] == "best_quote")
    assert lead["margin"] * 10_000 == pytest.approx(200 - 2, abs=1)
    # an edge the top of the leader's fee range wipes out is not a lead: 25 bp at $1K vs $2 (20 bp)
    # leaves 5 bp, under the 10 bp bar
    f = _edel(data(), 1_000, {"oneswap": 1.0025, "tradecraft": 1.0})
    assert not any(x["rule"] == "best_quote" for x in f["oneswap"]["leads"])


def test_no_price_lead_under_1k():
    assert vp.MIN_PRICE_LEAD_USD == 1_000
    # OneSwap 5% ahead at $100: even after its $2 fee (2%) that clears the bar, but $100 is too small
    f = _edel(data(), 100, {"oneswap": 1.05, "tradecraft": 1.0})
    assert not any(x["rule"] == "best_quote" for x in f["oneswap"]["leads"])
    f = _edel(data(), 1_000, {"oneswap": 1.05, "tradecraft": 1.0})
    assert any(x["rule"] == "best_quote" for x in f["oneswap"]["leads"])


def test_every_card_footer_fits_two_readable_lines():
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    Image = pytest.importorskip("PIL.Image")
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    S = 2
    for slug, f in vp.facts(data()).items():
        note = vp.card_notes(f, vp.headline(f))
        lines, font = vp.note_lines(d, note, lambda z: vp._font("Regular", z), (1200 - 112) * S, 19 * S, 17 * S, 14 * S)
        assert len(lines) <= 2 and font.size >= 14 * S and not any("…" in x for x in lines), (slug, note)


# === second design pass (2026-10-07) ==========================================

import re  # noqa: E402

import venue_history as vh  # noqa: E402


def _main_text(page: str) -> str:
    """The venue's own content: between the header (whose menu lists every venue) and the footer."""
    body = page.split("<main", 1)[1].split('<footer class="foot">', 1)[0]
    p = _Text()
    p.feed(body)
    return " ".join(p.parts)


def test_no_competitor_is_named_in_a_venues_public_text(tmp_path):
    pytest.importorskip("PIL")
    d = data()
    vp.build(tmp_path, d, T)
    names = {v["slug"]: v["name"] for v in vp.VENUES}
    for slug, f in vp.facts(d).items():
        head = vp.headline(f)
        page = (tmp_path / "venues" / slug / "index.html").read_text()
        meta = _Meta()
        meta.feed(page)
        texts = [_main_text(page), meta.title, *meta.meta.values(), vp.share_text(f, head), head["title"],
                 head["sub"], vs.footer_note(f, head), vp.lead_html(f, head)]
        texts += [x for p in vp.stats(f) for x in (p["k"], p["v"], p["n"])]
        others = [n for s, n in names.items() if s != slug]
        for text in filter(None, texts):
            assert not any(re.search(rf"\b{re.escape(n)}\b", text) for n in others), (slug, text[:200])
        # the runner-up is still known to the code, for the margin rule and the log
        for lead in f["leads"]:
            if lead["rule"] != "unique_token":
                assert lead["next"] in names.values() and lead["margin"] is not None


def _pool(v, pair, tvl, vol=0.0, apr=None):
    return {"venue": v, "pair": pair, "tvl_usd": tvl, "volume_24h_usd": vol, "fee": 0.003, "fee_apr": apr}


def test_wider_categories_give_each_venue_a_true_lead():
    d = data()
    d["lp"]["pools"] += [_pool("poolparty", "CC/EDELx", 90_000, vol=50_000, apr=0.4),
                         _pool("cantex", "CC/EDELx", 300_000, vol=40_000, apr=0.1),
                         _pool("oneswap", "CC/HECTO", 60_000)]
    d["tokens"]["tokens"] += [
        {"symbol": "HECTO", "key": "HECTO", "venues": {"oneswap": {"liquidity_usd": 60_000}}},
        {"symbol": "SBC", "key": "SBC", "venues": {"tradecraft": {"liquidity_usd": 134_000},
                                                   "cantex": {"liquidity_usd": 4}}},
        {"symbol": "MOD", "key": "MOD", "venues": {"cantex": {"liquidity_usd": 20_000},
                                                   "poolparty": {"liquidity_usd": None}}}]
    f = vp.facts(d)

    def rules(slug):
        return {x["rule"]: x for x in f[slug]["leads"]}

    # the most traded CC/EDELx pool (+25%) and the best fee APR on it: Pool Party now leads something
    pp = rules("pool-party")
    assert pp["pair_volume"]["title"] == "The most traded CC/EDELx pool"
    assert pp["pair_volume"]["margin"] == pytest.approx(0.25)
    assert pp["pair_apr"]["big"] == "40.0%" and pp["pair_apr"]["next"] == "Cantex"
    assert vp.headline(f["pool-party"])["rule"] == "pair_volume"
    # the only venue with a token: no other venue lists HECTO at all
    assert rules("oneswap")["unique_token"]["title"] == "The only HECTO pool"
    # another venue lists SBC, but under $1K: the claim says so
    tc = rules("tradecraft")["unique_token"]
    assert tc["title"] == "The only SBC pool with $1K or more"
    assert tc["next"] is None and tc["margin"] is None
    # an unmeasured market elsewhere is not thin: no "only MOD" claim
    assert "unique_token" not in rules("cantex") or "MOD" not in rules("cantex")["unique_token"]["title"]
    # pools under $1K never count, either way
    d["lp"]["pools"][-3]["tvl_usd"] = 900
    assert "pair_volume" not in {x["rule"] for x in vp.facts(d)["pool-party"]["leads"]}


def test_a_pair_pool_outranks_a_thin_depth_lead():
    """Tradecraft's $1.2M CC/USDCx pool leads its card, not the dollars within 1% of its mid."""
    d = data()
    d["lp"]["pools"].append(_pool("cantex", "CC/EDELx", 750_000))  # pool totals level: no "most liquidity" lead
    leads = [x["rule"] for x in vp.facts(d)["tradecraft"]["leads"]]
    assert leads.index("pool_tvl") < leads.index("token_depth")
    assert list(vp.CATEGORIES).index("pool_tvl") < list(vp.CATEGORIES).index("token_depth")


def test_the_venues_menu_is_the_same_on_the_dashboard_and_every_page(tmp_path):
    pytest.importorskip("PIL")
    dash = (Path(vp.HERE) / "site" / "index.html").read_text()
    assert vp.nav_venues("/") in dash
    vp.build(tmp_path, data(), T)
    for slug in ("temple", "oneswap"):
        page = (tmp_path / "venues" / slug / "index.html").read_text()
        assert vp.nav_venues("/") in page
    links = re.findall(r'href="(/venues/[^"]*)"', vp.nav_venues("/"))
    assert links == ["/venues/"] + [f"/venues/{v['slug']}/" for v in vp.VENUES]
    # the dashboard's venue table: plain links in the text colour, no per-venue colour
    assert 'class="plain" href="venues/${VP[x.id]}/"' in dash and '"color:" + VC[x.id]' not in dash


def test_venues_index_is_a_table_without_thumbnails(tmp_path):
    pytest.importorskip("PIL")
    vp.build(tmp_path, data(), T)
    idx = (tmp_path / "venues" / "index.html").read_text()
    assert '<table class="vt">' in idx and "card.png" not in idx.split("<main", 1)[1]
    rows = re.findall(r'<tr class="click" data-href="([^"]+)"', idx)
    assert rows[0] == "temple/" and len(rows) == 7  # by 24h volume
    assert '<b class="up">#1</b> Largest spot venue on Canton' in idx


def test_best_price_table_reads_per_token_and_size(tmp_path):
    pytest.importorskip("PIL")
    vp.build(tmp_path, data(), T)
    page = (tmp_path / "venues" / "temple" / "index.html").read_text()
    sec = page.split("Where Temple is the best price", 1)[1].split("</section>", 1)[0]
    assert "Buy CBTC with dollars" in sec and "Sell CBTC for dollars" in sec
    assert sec.count('<span class="best">Best</span>') == 4 + 1  # four sizes won, plus the legend
    text = _Text()
    text.feed(sec)
    assert not re.search(r"\d of \d", " ".join(text.parts))  # no "2 of 6" tallies


def test_venue_history_series():
    dex = {"totalDataChartBreakdown": [[T - 86400, {"Temple": 30.0, "Cantex": 10.0}],
                                       [T, {"Temple": 20.0, "Cantex": 20.0, "Other": 10.0}]]}
    per, total = vh.llama_daily(dex, T)
    assert per["temple"] == [[T - 86400, 30.0], [T, 20.0]] and total[-1] == [T, 50.0]
    day = 1791331200  # 2026-10-07 00:00 UTC
    hours = [(day - 3600 * 3 + 3600 * k, 1.0) for k in range(20)]  # 21:00 the day before to 16:00
    assert vh.hourly_to_days([hours]) == []  # neither day is complete
    full = [(day + 3600 * k, 2.0) for k in range(24)]
    assert vh.hourly_to_days([full, full]) == [[day, 96.0]]
    st = {}
    assert vh.record_hourly(st, T, {"cantex": {"tvl": 5.0, "spot_volume": 1.0}})
    assert not vh.record_hourly(st, T + 600, {"cantex": {"tvl": 6.0}})  # within the hour
    assert vh.record_hourly(st, T + 3600, {"cantex": {"tvl": 6.0}, "ekiden": {"open_interest": 2.0}})
    c = st["hourly"]["venues"]
    assert c["cantex"]["tvl"] == [5.0, 6.0] and c["cantex"]["spot_volume"] == [1.0, None]
    assert c["ekiden"]["open_interest"] == [None, 2.0]
    # the page labels a series with its source and start
    three = [[T - 2 * 86400, 25.0], *per["temple"]]
    tot3 = [[T - 2 * 86400, 50.0], *total]
    f = vp.facts({**data(), "history": {"daily": {"temple": {"source": "defillama", "points": three}},
                                        "daily_total": tot3, "hourly": st["hourly"]}})
    titles = [(s["title"], s["sub"]) for s in f["temple"]["series"]]
    # the chart's own line never names the outside source; the page credits it once in its method notes
    assert titles[0][0] == "Volume by day" and "DefiLlama" not in titles[0][1] and "since 5 Oct 2026" in titles[0][1]
    assert f["temple"]["series"][0]["source"] == "defillama"
    assert f["temple"]["series"][1]["pts"][-1][1] == pytest.approx(0.4)


def _hist(n_hourly: int, n_daily: int) -> dict:
    """A venue history with ``n_hourly`` hourly readings of every figure and ``n_daily`` days of volume."""
    ts = [T - 3600 * (n_hourly - 1 - k) for k in range(n_hourly)]
    cols = {"tvl": [1000.0 + k for k in range(n_hourly)], "perp_volume": [5.0] * n_hourly,
            "open_interest": [7.0] * n_hourly, "spot_volume": [3.0] * n_hourly}
    days = [[T - 86400 * (n_daily - 1 - k), 10.0 + k] for k in range(n_daily)]
    return {"hourly": {"t": ts, "venues": {s: dict(cols) for s in ("tradecraft", "rocky", "ekiden", "oneswap",
                                                                    "pool-party")}},
            "daily": {"temple": {"source": "defillama", "points": days},
                      "tradecraft": {"source": "tradecraft", "points": days}},
            "daily_total": [[t, 100.0] for t, _ in days]}


def test_a_history_panel_draws_only_once_it_is_a_line(tmp_path):
    """No "recording since" placeholders: a series under its minimum is left out, and the History
    heading with it; the same pages grow their panels by themselves as readings accrue."""
    pytest.importorskip("PIL")
    assert vp.MIN_HOURLY_POINTS == 24 and vp.MIN_DAILY_POINTS == 3
    early = vp.facts({**data(), "history": _hist(vp.MIN_HOURLY_POINTS - 1, vp.MIN_DAILY_POINTS - 1)})
    assert all(not f["series"] for f in early.values())
    out = tmp_path / "early"
    vp.build(out, {**data(), "history": _hist(vp.MIN_HOURLY_POINTS - 1, vp.MIN_DAILY_POINTS - 1)}, T)
    for slug in ("temple", "tradecraft", "rocky", "ekiden", "oneswap", "pool-party"):
        page = (out / "venues" / slug / "index.html").read_text()
        assert "<h2>History</h2>" not in page and "Recording since" not in page and 'class="tchart"' not in page

    later = vp.facts({**data(), "history": _hist(vp.MIN_HOURLY_POINTS, vp.MIN_DAILY_POINTS)})
    kinds = {s: [x["kind"] for x in f["series"]] for s, f in later.items()}
    assert kinds["tradecraft"] == ["daily", "tvl"]  # its own daily record stands in for hourly volume
    assert kinds["rocky"] == ["spot_volume", "perp_volume"]  # no open interest published: no panel for it
    assert kinds["ekiden"] == ["perp_volume", "open_interest"]
    assert kinds["oneswap"] == ["tvl"] and kinds["pool-party"] == ["tvl", "spot_volume"]
    assert kinds["temple"] == ["daily", "share"]
    assert all(len(x["pts"]) >= vp.MIN_HOURLY_POINTS for x in later["ekiden"]["series"])
    out = tmp_path / "later"
    vp.build(out, {**data(), "history": _hist(vp.MIN_HOURLY_POINTS, vp.MIN_DAILY_POINTS)}, T)
    page = (out / "venues" / "ekiden" / "index.html").read_text()
    assert "<h2>History</h2>" in page and page.count('class="tchart"') == 2
    assert "Our own reading, every hour since 6 Oct 2026" in page


def _bars(page: str, title: str) -> list[tuple[str, str]]:
    """(label, value) of each bar in the "Right now" panel titled ``title``."""
    panel = page.split(f"<h3>{title}</h3>", 1)[1].split('</div></div>', 1)[0]
    return [(html.unescape(a), html.unescape(b)) for a, b in
            re.findall(r'<span class="hl">([^<]*)</span>.*?<span class="hn num">([^<]*)</span>', panel)]


def test_right_now_charts_need_no_history(tmp_path):
    """Current-state bars on every venue page from the data of this tick alone: pools by liquidity,
    perps and order-book markets by 24h volume, open interest by market. Thin ones summed as other."""
    pytest.importorskip("PIL")
    d = data()
    d["lp"]["pools"] += [_pool("tradecraft", f"CC/T{k:02d}", 100_000 - k * 1000) for k in range(12)]
    d["lp"]["pools"] += [_pool("tradecraft", "CC/DUST", 22), _pool("tradecraft", "CC/ZERO", 0)]
    d["lp"]["pools"] += [_pool("oneswap", "CC/HECTO", 60_000)]
    next(v for v in d["venues"]["venues"] if v["id"] == "temple")["markets_24h_usd"] = {
        "CBTC/USDCx": 30_000_000.0, "CC/USDCx": 3_500_000.0, "eXAU/USDCx": 400.0}
    next(v for v in d["venues"]["venues"] if v["id"] == "rocky")["markets_24h_usd"] = {
        "CBTC-USDCX": 2_000_000.0, "CETH-USDCB": 800_000.0}
    vp.build(tmp_path, d, T)  # no history at all
    page = lambda s: (tmp_path / "venues" / s / "index.html").read_text()

    tc = page("tradecraft")
    assert "<h2>Right now</h2>" in tc and "<h2>History</h2>" not in tc
    bars = _bars(tc, "Liquidity by pool")
    assert len(bars) == vp.BARS_SHOWN + 1 and bars[0] == ("CC/USDCx", "$1.2M")
    # past the top ten, the three smallest $1K+ pools and the $22 one are summed; the empty pool is not there
    assert bars[-1] == ("4 other pools", vp.money(91_000 + 90_000 + 89_000 + 22))
    assert "CC/ZERO" not in tc.split("<h2>Right now</h2>", 1)[1].split("</section>", 1)[0]
    assert "Now, as of 7 Oct 2026, 13:43 UTC" in tc

    assert _bars(page("oneswap"), "Liquidity by pool") == [("CC/HECTO", "$60K"), ("CC/USDCx", "$36K")]
    # a single pool compares nothing: no chart, and no empty section either
    assert "<h2>Right now</h2>" not in page("cantex")

    assert _bars(page("temple"), "Volume by market, 24h") == [("CBTC/USDCx", "$30M"), ("CC/USDCx", "$3.5M"),
                                                               ("1 other market", "$400")]
    assert "settled volume in its quote token, as Temple reports it" in page("temple")
    rk = page("rocky")
    assert _bars(rk, "Spot volume by market, 24h") == [("CBTC/USDCx", "$2M"), ("CETH/USDC.B", "$800K")]
    assert _bars(rk, "Perps volume by market, 24h") == [("BTC/USDCx", "$4M"), ("ETH/USDCx", "$600K")]
    assert "Open interest by market" not in rk  # Rocky publishes none

    ek = page("ekiden")
    assert _bars(ek, "Volume by market, 24h") == [("BTC/USDCx", "$200K"), ("ETH/USDCx", "$60K"),
                                                  ("CC/USDCx", "$20K")]
    assert _bars(ek, "Open interest by market") == [("BTC/USDCx", "$40K"), ("ETH/USDCx", "$10K"),
                                                    ("CC/USDCx", "$2K")]
    assert "counted at $1" in ek and "open contracts at mark price" in ek

    # bars are scaled to the largest, which fills the track
    assert 'style="width:100.0%"' in ek
    # every panel is labelled with when its figures are from
    for s in ("tradecraft", "oneswap", "temple", "rocky", "ekiden"):
        sec = page(s).split("<h2>Right now</h2>", 1)[1].split("</section>", 1)[0]
        assert sec.count('<p class="sub">') == sec.count("as of 7 Oct 2026, 13:43 UTC") > 0


def test_bar_rows_group_thin_and_need_two_bars():
    assert vp.bar_rows([("a", 5_000), ("b", None), ("c", 0)], "pool") == []
    rows = vp.bar_rows([("a", 5_000), ("b", 999)], "pool")
    assert rows == [{"label": "a", "value": 5_000.0},
                    {"label": "1 other pool", "value": 999.0, "other": True, "names": ["b"]}]
    assert vp.book_label("CBTC-USDCX") == "CBTC/USDCx" and vp.book_label("eXAU/USDCx") == "eXAU/USDCx"


# === shareability pass: best fact, no outside list named, plain words (2026-10-08) ===

def _cc_pair(sym, rows, liq):
    """A CC pair quoted on several venues, each with ``liq`` dollars of pool liquidity."""
    return ({"key": sym.upper(), "kind": "cc", "symbol": sym, "venues": list(liq), "rows": rows},
            {"symbol": sym, "key": sym.upper(), "venues": {v: {"liquidity_usd": x} for v, x in liq.items()}})


def _with_trades(d, wins_for, n_tokens=3, size=10_000, liq=None, edge=1.01):
    """``n_tokens`` CC pairs on OneSwap and Tradecraft at ``size``, both directions, each won by
    ``wins_for`` by ``edge``; every pool holds ``liq`` (default 20x the size)."""
    liq = liq or size * 20
    for k in range(n_tokens):
        out = {"oneswap": 1.0, "tradecraft": 1.0}
        out[wins_for] = edge
        rows = [_row(side, size, dict(out), wins_for) for side in ("sell", "buy")]
        pair, tok = _cc_pair(f"TK{k}", rows, {"oneswap": liq, "tradecraft": liq})
        d["execution"]["pairs"].append(pair)
        d["tokens"]["tokens"].append(tok)
    return d


def test_a_count_of_trades_won_beats_one_quote_and_needs_a_known_fee():
    # OneSwap prices 6 of 6 CC trades best at $10K, after its $2 network fee: broader than any one quote
    f = vp.facts(_with_trades(data(), "oneswap"))["oneswap"]
    rules = [x["rule"] for x in f["leads"]]
    assert rules.index("best_count") < rules.index("best_quote")
    head = vp.headline(f)
    assert head["rule"] == "best_count" and head["title"] == "Best price on 6 of 6 CC trades at $10K"
    assert head["sub"] == "Pool fees, price impact and network fees included" and not vp.is_ranked(head)
    # not a place in a ranking, but a majority of the trades won: shown as a win (founder, 2026-10-09)
    assert '<b class="up">#1</b> Best price on CC trades at $10K' in vp.lead_html(f, head) and head["big"] == "6 of 6"
    # the same trades won by Pool Party: its own network fee is unknown, so no price fact at all
    f = vp.facts(_with_trades(data(), "poolparty"))["pool-party"]
    assert not any(x["rule"] in ("best_count", "best_quote") for x in f["leads"])
    assert f["counts"][("cc", 10_000)]["won"] >= 6  # counted, and ready once the fee is known
    # Tradecraft's fee is known ($0.10-1.40)
    rules = [x["rule"] for x in vp.facts(_with_trades(data(), "tradecraft"))["tradecraft"]["leads"]]
    # a whole-venue ranking (its pool liquidity) still outranks a count of trades
    assert rules[0] == "tvl" and "best_count" in rules


def test_a_count_needs_a_majority_real_markets_and_1k():
    # a minority of trades won is not a fact worth a headline
    d = _with_trades(data(), "oneswap", n_tokens=2)
    _with_trades(d, "tradecraft", n_tokens=3)
    for p in d["execution"]["pairs"][-3:]:  # rename the second batch so tokens do not collide
        p["symbol"] = p["key"] = "Z" + p["symbol"]
    for t in d["tokens"]["tokens"][-3:]:
        t["symbol"] = t["key"] = "Z" + t["symbol"]
    c = vp.facts(d)["oneswap"]["counts"][("cc", 10_000)]
    assert (c["won"], c["of"]) == (4, 10)
    assert not any(x["rule"] == "best_count" for x in vp.facts(d)["oneswap"]["leads"])
    # a $10K trade between pools smaller than $10K is not counted at all
    f = vp.facts(_with_trades(data(), "oneswap", liq=5_000))["oneswap"]
    assert ("cc", 10_000) not in f["counts"] and not any(x["rule"] == "best_count" for x in f["leads"])
    # nor under $1K
    f = vp.facts(_with_trades(data(), "oneswap", size=100))["oneswap"]
    assert not any(x["rule"] == "best_count" for x in f["leads"])
    # and a win the leader's own network fee wipes out is not a win: $2 is 20 bp at $1K
    f = vp.facts(_with_trades(data(), "oneswap", size=1_000, edge=1.001))["oneswap"]
    assert f["counts"][("cc", 1_000)]["won"] == 0


def test_the_broadest_fact_leads():
    d = data()
    d["lp"]["pools"] += [_pool("poolparty", "CC/EDELx", 90_000, vol=50_000, apr=0.4),
                         _pool("cantex", "CC/EDELx", 300_000, vol=40_000, apr=0.1)]
    f = vp.facts(d)
    scores = [x["score"] for x in f["pool-party"]["leads"]]
    assert scores == sorted(scores, reverse=True)
    # one pool's volume beats that pool's fee APR
    assert [x["rule"] for x in f["pool-party"]["leads"]][:2] == ["pair_volume", "pair_apr"]
    # the single largest pool of any venue beats "the largest CC/USDCx pool"
    tc = [x["rule"] for x in f["tradecraft"]["leads"]]
    assert tc.index("largest_pool") < tc.index("pool_tvl")
    lp = next(x for x in f["tradecraft"]["leads"] if x["rule"] == "largest_pool")
    assert lp["title"] == "The largest liquidity pool" and lp["sub"] == "$1.2M in its CC/USDCx pool"
    assert all((rule, scope) in vp.SCORE for rule in vp.CATEGORIES for scope in ("read",))


def test_no_outside_list_is_named_in_public_copy_but_its_charts_are_credited(tmp_path):
    pytest.importorskip("PIL")
    d = {**data(), "history": _hist(vp.MIN_HOURLY_POINTS, vp.MIN_DAILY_POINTS)}
    vp.build(tmp_path, d, T)
    for slug, f in vp.facts(d).items():
        head = vp.headline(f)
        card = [head["title"], head["sub"], vs.footer_note(f, head), vp.share_text(f, head)]
        card += [x for p in vp.stats(f) for x in (p["k"], p["v"], p["n"])]
        assert not any("llama" in x.lower() for x in card), slug
        page = (tmp_path / "venues" / slug / "index.html").read_text()
        meta = _Meta()
        meta.feed(page)
        assert not any("llama" in (x or "").lower() for x in [meta.title, *meta.meta.values()])
        # the only mention: one credit line in the method block at the foot, under charts that use it
        body = _main_text(page)
        head_part, _, foot = body.partition(f"How we track {f['name']}")
        assert "llama" not in head_part.lower() and "we read" not in body
        assert foot.count("DefiLlama") == (1 if slug == "temple" else 0), slug
        assert ("DefiLlama" in foot) == (vp.HISTORY_CREDIT in foot)
        # and it is small print, never body copy
        if "DefiLlama" in foot:
            assert f'<p class="fine">{vp.e(vp.HISTORY_CREDIT)}</p>' in page
    idx = (tmp_path / "venues" / "index.html").read_text()
    head_part, _, foot = _main_text(idx).partition("Strongest fact:")
    assert "llama" not in head_part.lower() and foot.count("DefiLlama") == 1
    # the index's "Strongest fact" paragraph under the table never names the source; the credit is small print
    body = idx.split("<main", 1)[1]
    lead_p = re.search(r'<p class="sub"[^>]*>Strongest fact:.*?</p>', body, re.DOTALL).group(0)
    assert "DefiLlama" not in lead_p
    fine = re.findall(r'<p class="fine">(.*?)</p>', body, re.DOTALL)
    assert sum("DefiLlama" in x for x in fine) == 1
    assert ".fine {" in idx and "font-size: 12px" in idx


def test_a_scope_line_sits_under_an_unconfirmed_ranking_on_card_and_page(tmp_path):
    pytest.importorskip("PIL")
    vp.build(tmp_path, data(), T)
    f = vp.facts(data())
    line = "Based on the 7 Canton venues with public market data."
    rocky = vp.headline(f["rocky"])
    assert "among" not in rocky["title"] and vs.footer_note(f["rocky"], rocky).startswith(line)
    page = (tmp_path / "venues" / "rocky" / "index.html").read_text()
    assert line in _main_text(page).partition("How we track Rocky")[2]
    assert line in _Meta_of(page)["og:description"]
    # confirmed chain-wide, or no ranking at all: no line
    for slug in ("temple", "oneswap"):
        page = (tmp_path / "venues" / slug / "index.html").read_text()
        assert line not in page or slug == "oneswap" and vp.headline(f[slug])["rule"] not in vp.PLAIN
    assert vs.footer_note(f["temple"], vp.headline(f["temple"])) == "Independent data, not affiliated with Temple."
    # the count follows the venues read live
    d = data()
    next(v for v in d["venues"]["venues"] if v["id"] == "ekiden")["status"] = "down"
    assert vp.facts(d)["rocky"]["tracked_n"] == 6


def _Meta_of(page):
    p = _Meta()
    p.feed(page)
    return p.meta


def test_venues_index_top_line_coming_block_and_one_lead_per_row(tmp_path):
    pytest.importorskip("PIL")
    vp.build(tmp_path, data(), T)
    idx = (tmp_path / "venues" / "index.html").read_text()
    text = _main_text(idx)
    assert "Every Canton DEX with public market data, live." in text
    coming = idx.split("<h2>Coming to Canton Venues</h2>", 1)[1].split("</section>", 1)[0]
    assert "Trading on Canton, no public market data yet: " + ", ".join(vp.COMING) + "." in coming
    # Canborsa left this list on 2026-10-28, read through its web app's public routes
    assert vp.COMING == ["Trade.Fast", "Swap.Monster", "Kairo", "Silvana"]
    assert 'href="/#contact">Get in touch</a>' in coming
    # each row carries its lead twice in the markup, but only one is ever shown: the column on a wide
    # screen (hide-sm hides it under 720px), the tagline under the name on a phone (hidden above)
    row = re.search(r'<tr class="click" data-href="rocky/">(.*?)</tr>', idx).group(1)
    assert row.count("Largest perps venue") == 2
    assert '<div class="tagline">' in row and 'class="l lead hide-sm"' in row
    assert "table.vt .tagline { display: none;" in idx
    narrow = [b.split("@media", 1)[0] for b in idx.split("@media (max-width: 720px) {")[1:]]
    assert any(".hide-sm { display: none; }" in b for b in narrow)
    assert any("table.vt .tagline { display: block; }" in b for b in narrow)
    # an order book's depth is labelled, never shown bare in the liquidity column
    assert "book depth within 1%" in row
    # a fact that is not a ranking carries no #1
    assert "#1</b> Best price" not in idx


def test_publish_guard_does_not_watch_a_count_of_trades():
    import publish_guard as pg
    figs = pg.card_figures({}, {"rule": "best_count", "value": 6}, [])
    assert not any(k.startswith("headline:") for k in figs)


def test_every_generated_page_reports_the_dashboards_conversion_events(tmp_path):
    pytest.importorskip("PIL")
    vp.build(tmp_path, data(), T)
    tracker = vp._tracker()
    assert 'track(k, { place: place(a)' in tracker and 'track("copy"' in tracker
    pages = list(tmp_path.rglob("index.html"))
    assert len(pages) > 2 and all(tracker in p.read_text() for p in pages)


def test_a_perps_venue_without_a_lead_still_says_what_it_trades():
    f = {"name": "Ekiden", "kind": "Perpetuals", "venue": {}, "leads": [], "perp_volume": 104_457.8}
    head = vp.headline(f)
    assert head["title"] == "Ekiden on Canton"
    assert head["sub"] == "$104K of perpetuals traded in the last 24 hours" and head["big"] == "$104K"


def test_index_shows_perps_book_depth_and_leaves_what_a_venue_lacks_blank():
    d = data()
    ms = d["perps"]["markets"]
    ms[2]["depth_1pct_usd"], ms[3]["depth_1pct_usd"] = 300_000, 90_000
    # a thin market's large resting quote is not the venue's depth
    ms.append(dict(ms[-1], base="XAU", symbol="XAU", turnover_24h_usd=1.0, depth_1pct_usd=900_000))
    f = vp.facts(d)
    assert f["ekiden"]["perp_depth"] == {"symbol": "BTC", "usd": 300_000}
    idx = vp.index_page(f, {s: vp.headline(x) for s, x in f.items()}, T)
    row = lambda name: re.search(rf'<b>{name}</b>.*?</tr>', idx, re.DOTALL).group(0)
    assert vp.money(300_000) in row("Ekiden") and "perps book depth within 1%" in row("Ekiden")
    # Cantex has no perps: blank, not n/a; Rocky has perps but no open interest we can show: n/a
    assert "n/a" not in row("Cantex") and "n/a" in row("Rocky")


def test_best_price_table_ranks_after_network_fees():
    # OneSwap returns more as quoted, but its network fee leaves Cantex ahead: the page marks Cantex,
    # as the dashboard does
    d = data()
    row = _row("sell", 1000, {"oneswap": 1.0071, "cantex": 1.0}, "oneswap")
    row["out_net"] = {"oneswap": 0.9871, "cantex": 0.9990}
    d["execution"]["pairs"].append({"key": "HANDL", "kind": "cc", "symbol": "HANDL", "venues": ["cantex", "oneswap"],
                                    "rows": [row]})
    d["tokens"]["tokens"].append({"symbol": "HANDL", "key": "HANDL", "venues": {
        "cantex": {"liquidity_usd": 50_000}, "oneswap": {"liquidity_usd": 100_000}}})
    f = vp.facts(d)
    best = lambda v: [q["best"] for q in f[v]["quotes"] if q["symbol"] == "HANDL"]  # noqa: E731
    assert best("cantex") == [True] and best("oneswap") == [False]
    assert not any(x["rule"] == "best_quote" for x in f["oneswap"]["leads"])
    assert "network fees are not" not in vp.best_price_html(f["cantex"])


def test_sitemap_lists_the_dashboard_and_every_live_venue_page(tmp_path):
    vp.build(tmp_path, data(), T)
    xml = (tmp_path / "sitemap.xml").read_text()
    locs = re.findall(r"<loc>(.+?)</loc>", xml)
    pages = {p.parent.name for p in (tmp_path / "venues").glob("*/index.html")}
    assert locs[:2] == [f"{vp.SITE}/", f"{vp.SITE}/venues/"]
    assert locs[-2:] == [f"{vp.SITE}/weekly/", f"{vp.SITE}/build/"]
    assert {u.rsplit("/", 2)[1] for u in locs[2:-2]} == pages


def test_agent_guide_and_api_index_list_the_same_endpoints():
    import json

    site = Path(vp.__file__).parent / "site"
    guide = re.findall(r"^- \[(\w+)\.json\]\((\S+)\)", (site / "llms.txt").read_text(), re.M)
    index = json.loads((site / "api-index.json").read_text())
    assert [(e["name"], e["url"]) for e in index["endpoints"]] == guide
    assert all(u == f"{index['base']}{n}.json" for n, u in guide)
    assert "—" not in (site / "llms.txt").read_text()
