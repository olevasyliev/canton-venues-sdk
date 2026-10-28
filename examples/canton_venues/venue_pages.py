"""Canton Venues: one static page and one share card per venue we track live.

Hash routes cannot carry Open Graph tags, so every venue gets a real path,
``/venues/<slug>/``, with its own title, description and a 1200x630 card image
drawn from the same JSON the dashboard reads. The collector calls ``build`` after
each tick; it can also run alone against a saved API directory:

    python venue_pages.py --api /var/www/canton-venues/api/v1 --out /var/www/canton-venues

Every number comes from the collected data. The card's headline is the venue's
best fact, picked by a fixed rule (``leads`` builds the candidates, ``headline``
scores them): every fact that passes the honesty rules (a ranking it tops by at
least ``LEAD_MARGIN``, markets under ``MIN_LIQUIDITY_USD`` set aside, price
claims only after known network fees) is scored by how broad it is (``SCORE``:
the whole venue beats a count of trades, which beats one pool or one quote), and
the highest wins. A venue with no such fact gets a plain card. A venue's page,
card and share text never name another venue: the runner-up is kept in code and
the log, for the margin rule only.

Each category carries its scope (``CATEGORIES``): "on Canton" only where an
outside list confirms the ranking covers the chain (an internal check; the list
is never named in public copy). Any other ranking is stated plainly and the card
and page carry one line under it: "Based on the N Canton venues with public
market data" (``scope_line``).
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import model

log = logging.getLogger("venues.pages")

SITE = "https://cantonvenues.com"
HERE = Path(__file__).resolve().parent
FONTS = HERE / "fonts"

# Every venue the collector reads live. "rows" are its ids in venues.json (Rocky's spot and
# perp books are two rows but one venue). "x" is the venue's own X account, only when the
# venue's own website links it, or the founder confirmed it; "x_source" is where it came from.
VENUES = [
    {"slug": "temple", "name": "Temple", "rows": ["temple"], "site": "https://templedigitalgroup.com",
     "x": "temple_ny", "x_source": "https://templedigitalgroup.com"},
    {"slug": "cantex", "name": "Cantex", "rows": ["cantex"], "site": "https://www.cantex.io",
     "x": "cantex_io", "x_source": "https://www.cantex.io"},
    {"slug": "rocky", "name": "Rocky", "rows": ["rocky", "rocky_perp"], "site": "https://rocky.exchange",
     "x": "Rocky_exchange", "x_source": "https://rocky.exchange"},
    # "lead": False would draw the plain card (name, pools, tokens, volume) even when the venue leads
    # a ranking; its leads are still computed and logged. Tradecraft's lead (its CC/USDCx pool) is on
    # since 2026-10-07: the founder asked for every venue's real strength on its page.
    {"slug": "tradecraft", "name": "Tradecraft", "rows": ["tradecraft"], "site": "https://tradecraft.fi",
     "x": "TradecraftFi", "x_source": "https://tradecraft.fi"},
    {"slug": "ekiden", "name": "Ekiden", "rows": ["ekiden"], "site": "https://ekiden.fi",
     "x": "ekidenfi", "x_source": "https://ekiden.fi"},
    # neither site links an X account; the founder confirmed both handles (2026-10-09)
    {"slug": "oneswap", "name": "OneSwap", "rows": ["oneswap"], "site": "https://www.oneswap.cc",
     "x": "Oneswapcc", "x_source": "founder, 2026-10-09"},
    {"slug": "pool-party", "name": "Pool Party", "rows": ["poolparty"], "site": "https://poolparty.fun",
     "x": "PoolPartyDEX", "x_source": "founder, 2026-10-09"},
    {"slug": "canborsa", "name": "Canborsa", "rows": ["canborsa"], "site": "https://canborsa.com",
     "x": "Canborsa_DEX", "x_source": "https://docs.canborsa.com/ecosystem/support-and-official-links"},
]
# Venues whose book figures (depth, spread) are read with our own account key: nobody outside can
# check them, so the card leaves them out (Temple: TEMPLE_API_KEY; its settled volume needs none).
KEYED_BOOKS = {"temple"}

# How each row's 24h volume reaches us. Only "usd" is a dollar figure the venue itself publishes;
# every other figure is converted by us, and the card says how.
#   usd    the venue reports dollars (Temple settled_volume total_volume_usd, Tradecraft /volume USD)
#   cc     the venue reports CC, converted at our CC price (Cantex /v1/public/volume volume_cc)
#   cc_leg the CC leg of each CC pool, converted at our CC price (Pool Party /volume per pool, token
#          units; its non-CC legs, e.g. EDELx, may be the other side of the same trades, unresolved)
#   quote  turnover in each book's quote token, stablecoins counted at $1 and USDCx at its price
#          (Rocky /ticker/24hr quoteVolume, Ekiden /market/tickers turnover_24h)
VOLUME_BASIS = {"temple": "usd", "tradecraft": "usd", "cantex": "cc", "poolparty": "cc_leg",
                "rocky": "quote", "rocky_perp": "quote", "ekiden": "quote", "canborsa": "quote"}

# What the page states about how each venue is read, next to the numbers it qualifies.
CAVEATS = {
    "temple": ["Taker fee 1 bp is Temple's published rate.",
               "Temple's CC/USDCx book is in every CC/USDCx comparison and in the CC price (weighted by "
               "its depth), not in the token list."],
    "rocky": ["Taker fee 0.025% is assumed: Rocky publishes no fee schedule.",
              "Books quoted in USDC.B are valued at par ($1), not at USDC.B's pool price.",
              "Its USDC.B books count as dollars at par in dollar routes while USDC.B holds within 1% of $1."],
    "oneswap": ["Volume is not published, so OneSwap has no volume figure or volume rank.",
                "Priced from reserves as a constant-product pool with a 0.30% fee."],
    "poolparty": ["Fee (0.30%) from CCTools; priced from reserves as a constant-product pool.",
                  "Pool Party publishes no issuer, so its tokens match by instrument id."],
    "ekiden": ["MainNet tickers, mark, index and funding from the public API."],
    "canborsa": ["Six of its USDCx perp markets, the ones with an outside spot price: CC, BTC, ETH, HYPE, "
                 "gold and silver. Price, bid and ask are what it quotes an order; funding is its current "
                 "hourly rate.",
                 "Its 30-day volume is its own figure over every perp market (about 87 assets against four "
                 "dollar tokens), each counted at $1. It publishes no 24h volume or open interest.",
                 "Not shown as book depth: one order is capped at $20K to $50K of position size (its docs, "
                 "\"Order size limits\"), far under the depth its orders fill against."],
    "cantex": ["Priced from live reserves with Cantex's own pool formula."],
    "tradecraft": ["Priced from live reserves with Tradecraft's own pool formula."],
}

# A market this thin is not a competitor. A pool counts when its liquidity (both sides of its
# reserves, in USD: tokens.json ``liquidity_usd`` for a pool) is at least this much; an order book
# when the dollars resting within 1% of its mid are (``depth_1pct_usd``). Below it a pool or
# book neither wins nor is counted in a best-price tally, a count of tokens or pools, or a
# per-token ranking, and a quote left with fewer than two such venues counts for nobody.
MIN_LIQUIDITY_USD = 1_000
# A perp market counts as listed for "most perp markets" once it trades this much in 24h.
MIN_PERP_TURNOVER_USD = 1_000
# A ranking is a venue's lead only when it beats the runner-up by this much (10%: $3.27M against
# $3.11M is not a lead). Two venues inside it are level for headline purposes.
LEAD_MARGIN = 0.10
# A single best price is a venue's lead only when it beats the next counted venue by this much,
# both as quoted and AFTER each venue's known per-swap network fee is taken off (``NETWORK_FEE``):
# a venue whose fee we do not know is never handed a lead by the fee we do know for another.
CLEAR_EDGE_BPS = 10
# A best price leads only at this trade size or larger: under it a per-swap network fee we cannot
# see is too large a share of the trade for any edge to mean much.
MIN_PRICE_LEAD_USD = 1_000
# Per-trade network fees we know, as (unit, low, high), from the one table in model.NETWORK_FEE: only a
# measured or documented fee; an assumed one is left out. The leader is charged the high end and the
# runner-up the low end, so a best-price lead survives the least favourable reading.
# A venue missing here never leads on price: its own fee is unknown, so no edge can be shown to
# survive it. As a runner-up it is charged nothing (the low end of an unknown range), which only
# makes the leader's edge harder to clear.
NETWORK_FEE = {v: (u, float(lo), float(hi)) for v, (u, _, lo, hi, basis) in model.NETWORK_FEE.items()
               if basis != "assumed"}

# DefiLlama's Canton DEX list: the outside source that says which venues trade on Canton at all.
# INTERNAL ONLY: it gates "on Canton" (``_on_canton``) and is never named in a headline, card,
# subline or coverage line. Its one public mention is the source credit for the daily history
# charts, in a page's method notes (``HISTORY_CREDIT``), where the data really is its.
LLAMA_DEXS = "https://api.llama.fi/overview/dexs/Canton"
# Our row id -> its DefiLlama protocol name in that list (checked 2026-10-07).
LLAMA_NAMES = {"temple": "Temple", "cantex": "Cantex", "rocky": "Rocky Exchange Spot", "poolparty": "Pool Party"}
WHERE = {"canton": "on Canton", "read": ""}
# Every category a venue can lead, with its scope. "canton" is claimed only when ``source`` confirms
# it at build time (``_on_canton``); everything else is "read": we rank only the venues we track,
# so the headline is stated plainly and ``scope_line`` says what it is based on.
CATEGORIES = {
    "spot_volume": {"scope": "canton", "source": LLAMA_DEXS},
    "kind_volume": {"scope": "canton", "source": LLAMA_DEXS},
    "perp_volume": {"scope": "read", "source": None},  # derivatives coverage unverified
    "tvl": {"scope": "read", "source": None},
    "best_count": {"scope": "read", "source": None},
    "tokens": {"scope": "read", "source": None},
    "pool_count": {"scope": "read", "source": None},
    "perp_markets": {"scope": "read", "source": None},  # derivatives coverage unverified
    "largest_pool": {"scope": "read", "source": None},
    "pool_tvl": {"scope": "read", "source": None},
    "token_depth": {"scope": "read", "source": None},
    "unique_token": {"scope": "read", "source": None},
    "pair_volume": {"scope": "read", "source": None},
    "pair_apr": {"scope": "read", "source": None},
    "best_quote": {"scope": "read", "source": None},
}
# How broad each fact is: the headline is the candidate with the highest score. A ranking of the
# whole venue confirmed chain-wide comes first, then whole-venue rankings among the venues we
# track, then a count of trades it prices best, then counts of things it lists, then one pool, one
# token or one quote. Fixed numbers, no model; a tie goes to the ``CATEGORIES`` order.
SCORE = {
    ("spot_volume", "canton"): 100, ("kind_volume", "canton"): 95,
    ("spot_volume", "read"): 90, ("kind_volume", "read"): 85, ("perp_volume", "read"): 80, ("tvl", "read"): 75,
    ("best_count", "read"): 60,
    ("tokens", "read"): 58, ("pool_count", "read"): 56, ("perp_markets", "read"): 54,
    ("largest_pool", "read"): 50, ("pool_tvl", "read"): 40, ("pair_volume", "read"): 35,
    ("unique_token", "read"): 30, ("token_depth", "read"): 25, ("pair_apr", "read"): 20,
    ("best_quote", "read"): 15,
}
# Rankings carry a "#1" on the page and in the venues table; the other facts (the only venue with a
# token, the best price on a count of trades or on one quote) are not a place in a ranking.
RANKED = {"spot_volume", "kind_volume", "perp_volume", "tvl", "tokens", "pool_count", "perp_markets",
          "largest_pool", "pool_tvl", "token_depth", "pair_volume", "pair_apr"}
# A "best price on k of n trades" fact needs at least this many trades compared, and a majority won.
MIN_COUNT_TRADES = 4
# Trading on Canton but publishing no market data we can read yet: named once, at the foot of the
# venues index, with a way to get in touch. The one place this list lives.
COMING = ["Trade.Fast", "Swap.Monster", "Kairo", "Silvana"]
# The only public credit to DefiLlama, always small print: under the method notes of a page that draws its daily charts, and under the /venues/ table.
HISTORY_CREDIT = "Daily volume history from DefiLlama's Canton DEX data."
STABLES = model.STABLES

SIZE_LABEL = {100: "$100", 1000: "$1K", 10000: "$10K", 50000: "$50K"}
KIND_SHORT = {"Spot AMM": "AMM", "Spot order book": "order book"}
LIVE = ("priced", "volume")
# what a missing figure shows as: a word, not a dash (no dashes in anything we publish)
NA = "n/a"


# === numbers ===============================================================

def _one(x: float) -> str:
    """One decimal, a trailing ".0" dropped: 60.1, 84, 2.6."""
    s = f"{x:.1f}"
    return s[:-2] if s.endswith(".0") else s


def short_money(v) -> str:
    """The one money style on cards and pages: $1.2M, $490K, $60.1K, $84K, $2.6K, $512, $2.65."""
    if v is None:
        return NA
    a = abs(v)
    if a >= 999_950_000:
        return f"${_one(v / 1e9)}B"
    if a >= 999_950:
        return f"${_one(v / 1e6)}M"
    if a >= 99_950:
        return f"${round(v / 1e3)}K"
    if a >= 999.5:
        return f"${_one(v / 1e3)}K"
    if a >= 10:
        return f"${v:.0f}"
    return f"${v:.2f}"


def money(v) -> str:
    """Same as ``short_money``: one style everywhere."""
    return short_money(v)


def price(v) -> str:
    if v is None:
        return NA
    d = 2 if abs(v) >= 1 else 4 if abs(v) >= 0.01 else 6
    return f"${v:,.{d}f}"


def stamp(t: int) -> str:
    d = datetime.fromtimestamp(t, UTC)
    return f"{d.day} {d:%b %Y, %H:%M} UTC"


def size_range(sizes: list[int]) -> str:
    """"at $50K", "at $10K and $50K", "from $1K to $50K"."""
    labels = [SIZE_LABEL.get(s, money(s)) for s in sizes]
    if len(labels) == 1:
        return f"at {labels[0]}"
    if len(labels) == 2:
        return f"at {labels[0]} and {labels[1]}"
    return f"from {labels[0]} to {labels[-1]}"


def ordinal_rank(rank: int | None, n: int, otherwise: str = "") -> str:
    """"#2 of 5" while the venue sits in the top half of a ranking of three or more; "#1" alone in a
    ranking of two ("#1 of 2" says little, "#2 of 2" nothing); else ``otherwise``."""
    if not rank:
        return otherwise
    if n < 3:
        return "#1" if rank == 1 and n == 2 else otherwise
    return f"#{rank} of {n}" if rank * 2 <= n + 1 else otherwise


def page_rank(rank: int | None, n: int) -> str:
    """A page's rank line: "#3 of 5" in a ranking of three or more, "#1" alone at the top of two,
    nothing for the second of two."""
    if not rank:
        return ""
    if n < 3:
        return "#1" if rank == 1 and n == 2 else ""
    return f"#{rank} of {n}"


def scope_line(n: int) -> str:
    """The one line under a ranking no outside list confirms chain-wide."""
    return f"Based on the {n} Canton venues with public market data."


# === facts =================================================================

def _rank(values: dict[str, float], key: str) -> tuple[int | None, int]:
    """1-based rank of ``key`` by value, descending, and how many were ranked.

    A tie ranks below everyone it ties with, so two venues level at the top both rank 2 and
    neither can claim to lead.
    """
    vals = {k: v for k, v in values.items() if v}
    if key not in vals:
        return None, len(vals)
    return 1 + sum(v >= vals[key] for k, v in vals.items() if k != key), len(vals)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9%$.]+", " ", text.lower()).replace(". ", " ").split()).strip(" .")


def method_lines(lines: list[str]) -> list[str]:
    """Every sentence once: the venue's note and our caveats overlap ("Taker fee 1 bp." and "Taker fee
    1 bp is Temple's published rate."), so a sentence or clause already said by another is dropped,
    the shorter one giving way."""
    parts = []
    for line in lines:
        for sent in re.split(r"(?<=[.;])\s+", line.strip()):
            sent = sent.strip().rstrip(";").strip()
            if sent:
                parts.append(sent[0].upper() + sent[1:] + ("" if sent.endswith(".") else "."))
    keys = [_norm(x) for x in parts]
    out = []
    for n, (text, k) in enumerate(zip(parts, keys)):
        said = any(j != n and (f" {k} " in f" {o} " and (len(o) > len(k) or j < n))
                   for j, o in enumerate(keys))
        if not said:
            out.append(text)
    return out


def pair_label(pair: dict, side: str) -> str:
    """The direction of one quote, as the dashboard's execution view means it."""
    if pair["kind"] == "usd":
        return f"$ → {pair['symbol']}" if side == "buy" else f"{pair['symbol']} → $"
    return f"CC → {pair['symbol']}" if side == "sell" else f"{pair['symbol']} → CC"


def _cc_usd(data: dict) -> float | None:
    return (data.get("summary") or {}).get("cc_usd")


def volume_note(row_id: str, cc_usd: float | None) -> str:
    """How a volume figure was reached, in a few words (the log and tests; cards no longer carry
    it: the page's method notes say it in full, ``volume_method``)."""
    basis = VOLUME_BASIS.get(row_id)
    at = f" at {price(cc_usd)}" if cc_usd else ""
    if basis == "usd":
        return "as the venue reports it"
    if basis == "cc":
        return f"CC volume{at} per CC" if at else "CC volume, converted"
    if basis == "cc_leg":
        return f"CC side{at} per CC" if at else "CC side, converted"
    if basis == "quote":
        return "quote-token turnover"
    return "converted by us"


def volume_method(row_id: str, name: str, cc_usd: float | None) -> str:
    """The same, as a sentence for the card's footer and the page."""
    basis = VOLUME_BASIS.get(row_id)
    at = f"at {price(cc_usd)} per CC" if cc_usd else "at our CC price"
    if basis == "usd":
        return f"24h volume as {name} reports it, in dollars."
    if basis == "cc":
        return f"24h volume: {name}'s CC volume, converted {at}."
    if basis == "cc_leg":
        return f"24h volume: the CC side of each CC pool, converted {at}."
    if basis == "quote":
        return f"24h volume: {name}'s turnover in each book's quote token, converted to dollars."
    return f"24h volume converted by us from {name}'s figures."


def _on_canton(rule: str, row_id: str, rows: dict, llama: list | None) -> bool:
    """True when DefiLlama's Canton DEX list confirms ``row_id`` leads the same ranking chain-wide.

    For spot volume, ours must be the largest entry in the list; for volume by kind, the largest
    among entries of our kind, and an entry we cannot place by kind that is larger blocks it.
    """
    if not llama or row_id not in LLAMA_NAMES:
        return False
    vol = {x["name"]: x.get("volume_24h") or 0 for x in llama}
    ours = vol.get(LLAMA_NAMES[row_id])
    if not ours:
        return False
    kind_of = {LLAMA_NAMES[i]: rows[i]["kind"] for i in LLAMA_NAMES if i in rows}
    for name, v in vol.items():
        if name == LLAMA_NAMES[row_id] or v < ours:
            continue
        if rule == "spot_volume" or kind_of.get(name) in (None, rows[row_id]["kind"]):
            return False
    return True


def facts(data: dict) -> dict[str, dict]:
    """Per venue slug: every figure its page and card use, with its rankings."""
    rows = {r["id"]: r for r in data["venues"]["venues"]}
    tokens = data["tokens"]["tokens"]
    pairs = data["execution"]["pairs"]
    pools = data["lp"]["pools"]
    # CC pools the collector reads but cannot quote (a token no venue names, a second pool for a
    # token): their liquidity is still 2x the CC reserve, so they count in every pool total
    unpriced = [{**p, "priced": False} for p in data["lp"].get("unpriced_pools") or []]
    # pools of two tokens other than CC, valued at our prices of their tokens (none we can value: left
    # out); before 2026-10-14 no total counted them (Pool Party ~$95K, OneSwap ~$78K that day)
    others = [{**p, "priced": True, "other": True} for p in data["lp"].get("other_pools") or []
              if p.get("tvl_usd") is not None]
    perps = data["perps"]["markets"]
    spot_total = data["venues"].get("spot_volume_24h_usd")
    cc_usd = _cc_usd(data)
    llama = ((data.get("summary") or {}).get("ecosystem") or {}).get("venues")

    spot_vol = {i: r["volume_24h_usd"] for i, r in rows.items() if r["kind"] != "Perpetuals"}
    kind_vol: dict[str, dict] = {}
    for i, r in rows.items():
        kind_vol.setdefault(r["kind"], {})[i] = r["volume_24h_usd"]
    perp_vol: dict[str, float] = {}
    perp_mkts: dict[str, list] = {}
    for p in perps:  # a venue that publishes no 24h figure (Canborsa) has none, not $0
        if p["turnover_24h_usd"] is not None:
            perp_vol[p["venue"]] = perp_vol.get(p["venue"], 0) + p["turnover_24h_usd"]
        perp_mkts.setdefault(p["venue"], []).append(p)
    tvl: dict[str, float] = {}
    pool_rows: dict[str, list] = {}
    for p in [*pools, *unpriced, *others]:
        tvl[p["venue"]] = tvl.get(p["venue"], 0) + p["tvl_usd"]
        pool_rows.setdefault(p["venue"], []).append(p)
    tok_rows: dict[str, list] = {}
    for t in tokens:
        for v, info in t["venues"].items():
            tok_rows.setdefault(v, []).append({"symbol": t["symbol"], "key": t["key"], **info})
    deepest = {v: max((x.get("depth_1pct_usd") or 0) for x in xs) for v, xs in tok_rows.items()}

    by_key = {t["key"]: t for t in tokens}
    name_of = {i: v["name"] for v in VENUES for i in v["rows"]}

    def liquidity(key: str, venue: str, market: str | None = None) -> float | None:
        """Pool liquidity, or a book's dollars within 1% of mid; None when the data has none.

        ``market`` names the book a dollar route uses. tokens.json carries the depth of each
        venue's deepest dollar book (its depth summed over all its books), so a route through another
        book is unmeasured.
        """
        info = by_key.get(key, {}).get("venues", {}).get(venue)
        if not info:
            return None
        if info.get("market"):
            return info.get("depth_1pct_usd") if market in (None, info["market"]) else None
        return info.get("liquidity_usd")

    def counted(key: str, venue: str, market: str | None = None, floor: float = MIN_LIQUIDITY_USD) -> bool:
        """Unmeasured is not thin: only a market measured under the floor is set aside."""
        liq = liquidity(key, venue, market)
        return liq is None or liq >= floor

    # best execution: per scope (CC pairs on pools, dollar quotes) and size, quotes won / quoted,
    # among the venues that are not thin
    wins: dict[tuple, dict] = {}
    quoted: dict[tuple, dict] = {}
    won_rows: dict[str, list] = {}
    quote_rows: dict[str, list] = {}
    # the trades a "best price on k of n" fact counts: a quote at a size counts only between venues
    # whose market holds at least that size (a $10K win over a $1.2K pool means nothing), and a win
    # only when it survives each venue's known network fee (``net_edge_bps``), as a price lead must
    tally: dict[tuple, dict] = {}
    for p in pairs:
        key = p.get("token") or p["key"]
        markets = {v: b["market"] for v, b in (p.get("books") or {}).items()}
        for r in p["rows"]:
            # each venue's swaps on its best route at this size (two on a route inside the venue)
            swaps = r.get("swaps") or p.get("swaps") or {}
            k = (p["kind"], r["size_usd"])
            # a route through a pool under the floor or the trade is thin too (``route_usd``: its thinnest pool)
            route = r.get("route_usd") or {}
            outs = {v: o for v, o in r["out"].items() if o is not None and counted(key, v, markets.get(v))
                    and route.get(v, MIN_LIQUIDITY_USD) >= MIN_LIQUIDITY_USD}
            floor = max(r["size_usd"], MIN_LIQUIDITY_USD)
            real = {v: o for v, o in outs.items() if counted(key, v, markets.get(v), floor)
                    and route.get(v, floor) >= floor}
            if len(real) >= 2:
                t = tally.setdefault(k, {})
                for v in real:
                    t.setdefault(v, {"won": 0, "of": 0})["of"] += 1
                # ranked after each venue's network fee as the grid ranks them (``out_net``); a win must
                # also survive the conservative fee range (``net_edge_bps``)
                ranks = ({v: r["out_net"].get(v, 0.0) for v in real} if r.get("out_net") else dict(real))
                rb = sorted(real, key=ranks.get, reverse=True)
                net = net_edge_bps(rb[0], rb[1], real[rb[0]], real[rb[1]], r["size_usd"],
                                   swaps.get(rb[0], 1), swaps.get(rb[1], 1), cc_usd)
                if ranks[rb[0]] > ranks[rb[1]] and real[rb[0]] > real[rb[1]] and net is not None and net > 0:
                    t[rb[0]]["won"] += 1
            if len(outs) < 2:
                continue
            for v in outs:
                quoted.setdefault(k, {}).setdefault(v, 0)
                quoted[k][v] += 1
            # ranked after each venue's network fee, as the dashboard ranks them (``out_net``, the
            # fee's point estimate); a row without it ranks as quoted, and a venue the fee wipes out is 0
            nets = ({v: r["out_net"].get(v, 0.0) for v in outs} if r.get("out_net") else dict(outs))
            ranked = sorted(outs, key=nets.get, reverse=True)
            best, second = ranked[0], ranked[1]
            # every counted quote, per venue: whether it was the best one (a tie is nobody's)
            for v in outs:
                quote_rows.setdefault(v, []).append(
                    {"symbol": p["symbol"], "key": key, "kind": p["kind"], "side": r["side"],
                     "size": r["size_usd"], "best": v == best and nets[best] != nets[second]})
            if nets[best] == nets[second] or nets[second] <= 0 and nets[best] <= 0:
                continue
            wins.setdefault(k, {}).setdefault(best, 0)
            wins[k][best] += 1
            won_rows.setdefault(best, []).append(
                {"pair": pair_label(p, r["side"]), "kind": p["kind"], "symbol": p["symbol"],
                 "side": r["side"], "size": r["size_usd"], "out": outs[best], "next": second,
                 "next_out": outs[second], "edge_bps": (outs[best] / outs[second] - 1) * 10_000,
                 "edge_after_fee_bps": ((nets[best] / nets[second] - 1) * 10_000 if nets[second] > 0
                                        else None),
                 "real": best in real and second in real,
                 "net_edge_bps": net_edge_bps(best, second, outs[best], outs[second], r["size_usd"],
                                              swaps.get(best, 1), swaps.get(second, 1), cc_usd)})

    # what each venue can lead, measured the same way for everyone
    tok_count = {v: len(token_split(xs)[0]) for v, xs in tok_rows.items()}
    pool_count: dict[str, int] = {}
    for p in [*pools, *unpriced, *others]:
        if p["tvl_usd"] >= MIN_LIQUIDITY_USD:
            pool_count[p["venue"]] = pool_count.get(p["venue"], 0) + 1
    live_mkts = {v: [m for m in ms if (m["turnover_24h_usd"] or 0) >= MIN_PERP_TURNOVER_USD]
                 for v, ms in perp_mkts.items()}

    # how many venues the rankings span: every venue we track that is read live this tick
    tracked_n = sum(1 for v in VENUES if (rows.get(v["rows"][0]) or {}).get("status") in LIVE)
    out = {}
    for v in VENUES:
        try:  # one venue's odd value leaves that venue out, not every page (a perp's null quote, 2026-10-14)
            ids = v["rows"]
            main = rows.get(ids[0])
            if main is None:
                continue
            f = {"venue": v, "id": ids[0], "name": v["name"], "status": main["status"], "cc_usd": cc_usd,
                 "tracked_n": tracked_n,
                 "kind": " + ".join(dict.fromkeys(rows[i]["kind"] for i in ids if i in rows)),
                 "keyed": ids[0] in KEYED_BOOKS,
                 "method": method_lines([rows[i]["note"] for i in ids if i in rows]
                                        + [c for i in ids for c in CAVEATS.get(i.replace("_perp", ""), [])])}
            i = ids[0]
            if main["kind"] != "Perpetuals":
                f["spot_volume"] = main["volume_24h_usd"]
                f["spot_rank"], f["spot_n"] = _rank(spot_vol, i)
                f["spot_share"] = (main["volume_24h_usd"] / spot_total
                                   if main["volume_24h_usd"] and spot_total else None)
                f["kind_label"] = KIND_SHORT.get(main["kind"], main["kind"].lower())
                f["kind_rank"], f["kind_n"] = _rank(kind_vol.get(main["kind"], {}), i)
                if main["volume_24h_usd"]:
                    f["method"].append(volume_method(i, v["name"], cc_usd))
            perp_id = next((x for x in ids if rows.get(x, {}).get("kind") == "Perpetuals"), None)
            if perp_id:
                f["perp_id"] = perp_id
                f["perp_volume"] = perp_vol.get(perp_id)
                # a venue's own 30-day figure where it publishes no 24h one (Canborsa)
                f["perp_volume_30d"] = rows[perp_id].get("volume_30d_usd")
                f["perp_rank"], f["perp_n"] = _rank(perp_vol, perp_id)
                f["perp_markets"] = perp_mkts.get(perp_id, [])
                counts = {k: len(x) for k, x in perp_mkts.items()}
                f["perp_mkt_rank"], f["perp_mkt_n"] = _rank(counts, perp_id)
                oi = [m["open_interest_usd"] for m in f["perp_markets"] if m["open_interest_usd"] is not None]
                f["open_interest"] = sum(oi) if oi else None
                # a perps book's dollars within 1% of mid, its deepest market trading $1K+ a day (thin
                # markets can carry a large resting quote nobody trades against)
                deep = [x for x in f["perp_markets"] if x.get("depth_1pct_usd")
                        and (x["turnover_24h_usd"] or 0) >= MIN_PERP_TURNOVER_USD]
                if deep:
                    top = max(deep, key=lambda x: x["depth_1pct_usd"])
                    f["perp_depth"] = {"symbol": top["symbol"], "usd": top["depth_1pct_usd"]}
                if f["perp_volume"]:
                    f["method"].append(f"Perps volume: 24h turnover in each market's quote token "
                                       f"({', '.join(sorted({m['quote'] or '?' for m in f['perp_markets']}))}), counted at $1.")
            f["tokens"] = sorted(tok_rows.get(i, []), key=lambda x: -(x.get("liquidity_usd") or 0))
            # each spot market's own 24h volume, when the collector has it (Temple: settled volume per
            # market; Rocky: each book's turnover in dollars), for the "Right now" chart
            if main.get("markets_24h_usd"):
                f["book_volume"] = dict(main["markets_24h_usd"])
            # the venue's own market list, when the collector has one (Temple: every market that settled
            # in the last 24h), so a card can say how many of them we price
            listed = [s.partition("/")[0] for s in main.get("markets_24h") or []]
            if listed:
                f["listed"] = list(dict.fromkeys(listed))
                f["listed_pairs"] = list(dict.fromkeys(main.get("markets_24h") or []))
                ours = [x["symbol"] for x in f["tokens"] if x["symbol"] in f["listed"]]
                if len(ours) < len(f["listed"]):
                    f["method"].append(f"{v['name']} had {len(f['listed'])} markets trading in the last 24h "
                                       f"({', '.join(f['listed'])}); we price {len(ours)} of them as tokens "
                                       f"({', '.join(ours)}).")
            if f["tokens"]:
                f["token_rank"], f["token_n"] = _rank({k: len(x) for k, x in tok_rows.items()}, i)
                top = max(f["tokens"], key=lambda x: x.get("depth_1pct_usd") or 0)
                f["deepest"] = {"symbol": top["symbol"], "usd": top.get("depth_1pct_usd"),
                                "market": top.get("market"), "books": top.get("books") or 1}
                f["deep_rank"], f["deep_n"] = _rank(deepest, i)
            if i in pool_rows:
                f["pools"] = sorted(pool_rows[i], key=lambda x: -x["tvl_usd"])
                f["pool_n"] = len(f["pools"])
                f["pool_priced"] = sum(1 for p in f["pools"] if p.get("priced", True))
                f["tvl"] = tvl[i]
                f["tvl_rank"], f["tvl_n"] = _rank(tvl, i)
                n_cc = sum(1 for p in f["pools"] if not p.get("other"))
                if f["pool_priced"] < f["pool_n"]:
                    f["method"].append(
                        f"{f['pool_n'] - f['pool_priced']} of its {n_cc} CC pools pair CC with a token we cannot "
                        "price; their liquidity is counted from the CC side (twice the CC reserve), like every pool's.")
                if n_cc < f["pool_n"]:
                    f["method"].append(
                        f"{f['pool_n'] - n_cc} of its {f['pool_n']} pools pair two tokens other than CC; their "
                        "liquidity is both sides at our token prices (twice the side we price).")
            f["exec"] = {f"{s}:{z}": {"won": wins.get((s, z), {}).get(i, 0), "of": quoted[(s, z)].get(i, 0)}
                         for (s, z) in sorted(quoted) if quoted[(s, z)].get(i)}
            f["won_rows"] = won_rows.get(i, [])
            f["quotes"] = quote_rows.get(i, [])
            f["counts"] = {k: dict(t[i], next=max((x for x in t if x != i), key=lambda x: (t[x]["won"], t[x]["of"]),
                                                   default=None))
                           for k, t in tally.items() if i in t}
            f["leads"] = _leads(f, i, perp_id, name_of, rows, llama, spot_vol, kind_vol.get(main["kind"], {}),
                                perp_vol, tvl, tok_count, pool_count, live_mkts, tokens, pools, unpriced)
            f["series"] = venue_series(v["slug"], f, data.get("history") or {})
            f["near"] = _near(f, i, perp_id, spot_vol, kind_vol.get(main["kind"], {}), perp_vol, tvl,
                              tok_count, pool_count, live_mkts, name_of)
            out[v["slug"]] = f
        except Exception:  # noqa: BLE001
            log.exception("venue %s left out of this build", v["slug"])
    return out


def _lead(values: dict[str, float], key: str, margin: float = LEAD_MARGIN):
    """(runner-up id, its value, margin over it) when ``key`` is first of two or more by at least
    ``margin`` (a share: 0.10 is 10%); else None. A tie never leads."""
    vals = {k: v for k, v in values.items() if v}
    if key not in vals or len(vals) < 2:
        return None
    rest = sorted(((v, k) for k, v in vals.items() if k != key), reverse=True)
    top = rest[0][0]
    if vals[key] > top and vals[key] >= top * (1 + margin):
        return rest[0][1], top, vals[key] / top - 1
    return None


def _near(f, i, perp_id, spot_vol, kind_vol, perp_vol, tvl, tok_count, pool_count, live_mkts, name_of):
    """Rankings this venue tops but by less than ``LEAD_MARGIN``: not leads, kept for the log."""
    out = []
    for rule, vals, key in (("spot_volume", spot_vol, i), ("kind_volume", kind_vol, i),
                            ("perp_volume", perp_vol, perp_id), ("tvl", tvl, i), ("tokens", tok_count, i),
                            ("pool_count", pool_count, i),
                            ("perp_markets", {k: len(x) for k, x in live_mkts.items()}, perp_id)):
        if key is None:
            continue
        r = _lead(vals, key, margin=0)
        if r and r[2] < LEAD_MARGIN:
            out.append({"rule": rule, "next": name_of.get(r[0], r[0]), "margin": r[2]})
    return out


def net_edge_bps(best: str, nxt: str, out_best: float, out_next: float, size: float, swaps_best: int,
                 swaps_next: int, cc_usd: float | None) -> float | None:
    """The best venue's edge over the next after each one's known network fee; the leader pays the
    high end, the runner-up the low end. None when a known fee cannot be converted to dollars."""
    def fee(v: str, swaps: int, high: bool) -> float | None:
        if v not in NETWORK_FEE:
            return 0.0
        unit, lo, hi = NETWORK_FEE[v]
        amount = hi if high else lo
        if unit == "cc":
            if not cc_usd:
                return None
            amount *= cc_usd
        return swaps * amount / size
    fb, fn = fee(best, swaps_best, True), fee(nxt, swaps_next, False)
    if fb is None or fn is None or fb >= 1:
        return None
    return (out_best * (1 - fb) / (out_next * (1 - fn)) - 1) * 10_000


def _direction(row: dict) -> str:
    """"buy CBTC with CC", "sell CBTC for dollars": one quote, in words."""
    sym = row["symbol"]
    if row["kind"] == "usd":
        return f"buy {sym} with dollars" if row["side"] == "buy" else f"sell {sym} for dollars"
    return f"buy {sym} with CC" if row["side"] == "sell" else f"sell {sym} for CC"


def received(row: dict) -> str:
    """What the trader gets back on one quote: the token on a buy, CC or dollars on a sell."""
    sym = row["symbol"]
    if row["kind"] == "usd":
        return sym if row["side"] == "buy" else "dollars"
    return sym if row["side"] == "sell" else "CC"


def quote_edge_bps(row: dict) -> float:
    """The edge a page shows for one quote: after each venue's network fee, as the dashboard shows it,
    or as quoted when the row has no fee figures."""
    return row["edge_bps"] if row.get("edge_after_fee_bps") is None else row["edge_after_fee_bps"]


def best_quote_sub(row: dict) -> str:
    """"0.87% more HANDL than the next best venue, ...": the edge is in what you receive, so it can
    never read as a higher price."""
    return (f"{quote_edge_bps(row) / 100:.2f}% more {received(row)} than the next best venue, pool fees, "
            "price impact and network fees included")


TRADE_KIND = {"cc": "CC trades", "usd": "dollar trades"}


def _leads(f, i, perp_id, name_of, rows, llama, spot_vol, kind_vol, perp_vol, tvl, tok_count, pool_count,
           live_mkts, tokens, pools, unpriced=()) -> list[dict]:
    """Every true fact this venue can lead with, best first (``SCORE``, then the ``CATEGORIES`` order).

    A ranking counts when the venue is #1 by ``LEAD_MARGIN`` or more; a price fact only after each
    venue's known network fee. Each carries ``title`` and ``sub`` for the card, ``big``/``cap`` for
    the page, ``next``/``next_value`` (the runner-up) and ``margin`` over it (code and log only),
    ``value`` (the figure the card leads with), ``scope`` with its ``source``, ``rank`` (a place in a
    ranking, shown as #1) and ``score``. Ties never lead.
    """
    out = []

    def where(rule: str, row_id: str | None = None) -> str:
        cat = CATEGORIES[rule]
        if cat["scope"] == "canton" and row_id and _on_canton(rule, row_id, rows, llama):
            return "canton"
        return "read"

    def add(rule, title, sub, r, value, fmt=short_money, scope="read", big=None, cap=None, short=None):
        """``big`` is the figure the page shows large and ``cap`` the line under it; ``next`` and
        ``margin`` stay in code and logs only, never in anything we publish."""
        nxt, nval, margin = r
        out.append({"rule": rule, "title": title, "sub": sub, "next": name_of.get(nxt, nxt),
                     "next_value": nval, "next_text": "" if nval is None else fmt(nval), "margin": margin,
                     "value": value, "scope": scope, "rank": rule in RANKED, "score": SCORE[(rule, scope)],
                     "source": CATEGORIES[rule]["source"] if scope == "canton" else None,
                     "short": short or _short(title), "big": big if big is not None else short_money(value),
                     "cap": cap if cap is not None else sub})

    share = f"{round(f['spot_share'] * 100)}% of Canton DEX spot volume" if f.get("spot_share") else ""
    if f.get("spot_volume") and (r := _lead(spot_vol, i)):
        sc = where("spot_volume", i)
        add("spot_volume", "The largest spot venue on Canton" if sc == "canton" else "The largest spot venue",
            f"{short_money(f['spot_volume'])} traded in the last 24 hours" + (f", {share}" if share else ""),
            r, f["spot_volume"], scope=sc, cap="Traded in the last 24 hours" + (f", {share}" if share else ""))
    if f.get("spot_volume") and (r := _lead(kind_vol, i)):
        sc = where("kind_volume", i)
        title = (f"The largest {f['kind_label']} on Canton by volume" if sc == "canton"
                 else f"The largest {f['kind_label']} by volume")
        add("kind_volume", title, f"{short_money(f['spot_volume'])} traded in the last 24 hours", r,
            f["spot_volume"], scope=sc, cap="Traded in the last 24 hours")
    if perp_id and (r := _lead(perp_vol, perp_id)):
        add("perp_volume", "The largest perps venue",
            f"{short_money(f['perp_volume'])} of perpetuals traded in the last 24 hours", r, f["perp_volume"],
            cap="Perpetuals traded in the last 24 hours")
    if r := _lead(tvl, i):
        add("tvl", "The most pool liquidity", f"{short_money(tvl[i])} across {pools_text(f)}", r,
            tvl[i], cap=f"In {pools_text(f)}")
    # a count of trades: at one size, the trades it and another venue with that much in its market both
    # quote, a majority of which it prices best after network fees. Only for a venue whose own network
    # fee is known (``NETWORK_FEE``), at $1K or more, the broadest such fact first (most trades won,
    # then the larger size)
    if i in NETWORK_FEE:
        counts = [(k, c) for k, c in (f.get("counts") or {}).items()
                  if k[1] >= MIN_PRICE_LEAD_USD and c["of"] >= MIN_COUNT_TRADES and c["won"] * 2 > c["of"]]
        if counts:
            (kind, size), c = max(counts, key=lambda x: (x[1]["won"], x[0][1]))
            label = SIZE_LABEL.get(size, money(size))
            add("best_count", f"Best price on {c['won']} of {c['of']} {TRADE_KIND.get(kind, 'trades')} at {label}",
                "Pool fees, price impact and network fees included", (c["next"], None, c["won"] / c["of"]),
                c["won"], fmt=lambda n: "", big=f"{c['won']} of {c['of']}",
                short=f"Best price on {TRADE_KIND.get(kind, 'trades')} at {label}",
                cap=f"{_upper_first(TRADE_KIND.get(kind, 'trades'))} at {label} where it gives the most back, "
                    "pool fees, price impact and network fees included")
    if r := _lead(tok_count, i):
        add("tokens", "The most tokens", f"{tok_count[i]} tokens with $1K or more of liquidity", r, tok_count[i],
            fmt=lambda n: f"{n} tokens", big=str(tok_count[i]), cap="Tokens with $1K or more of liquidity")
    if r := _lead(pool_count, i):
        add("pool_count", "The most pools", f"{pool_count[i]} pools with $1K or more in them",
            r, pool_count[i], fmt=lambda n: f"{n} pools", big=str(pool_count[i]),
            cap="Pools with $1K or more in them")
    if perp_id and (r := _lead({k: len(x) for k, x in live_mkts.items()}, perp_id)):
        ms = live_mkts[perp_id]
        add("perp_markets", "The most perp markets",
            f"{len(ms)} markets trading: {', '.join(m['base'] for m in ms)}", r, len(ms),
            fmt=lambda n: f"{n} markets", big=str(len(ms)),
            cap=f"Markets trading now: {', '.join(m['base'] for m in ms)}")
    # the single largest pool of any venue we track, priced or not (a pool's liquidity is twice its CC
    # reserve either way)
    biggest: dict[str, dict] = {}
    for p in [*pools, *unpriced]:
        if p["tvl_usd"] >= MIN_LIQUIDITY_USD and p["tvl_usd"] > biggest.get(p["venue"], {}).get("tvl_usd", 0):
            biggest[p["venue"]] = p
    # a pool marked not trading (reserves still while its price left the market) still counts against
    # the others, but no claim of its own venue rests on it
    if i in biggest and not biggest[i].get("not_trading_since") and \
            (r := _lead({v: p["tvl_usd"] for v, p in biggest.items()}, i)):
        p = biggest[i]
        where_in = f"its {p['pair']} pool" if p.get("priced", True) else "a CC pool"
        add("largest_pool", "The largest liquidity pool", f"{short_money(p['tvl_usd'])} in {where_in}", r,
            p["tvl_usd"], cap=f"In {where_in}")
    # one token: the deepest market within 1% of mid, among markets that are not thin. A book read
    # with our own key is not something anyone else can check, so it never leads.
    best_tok = None
    for t in tokens:
        here = t["venues"].get(i)
        depth = {v: x.get("depth_1pct_usd") for v, x in t["venues"].items()
                 if (x.get("liquidity_usd") or 0) >= MIN_LIQUIDITY_USD}
        if (f.get("keyed") and here and here.get("market")) or (here and here.get("not_trading_since")):
            continue
        if here and i in depth and (r := _lead(depth, i)) and (best_tok is None or depth[i] > best_tok[1]):
            best_tok = (t["symbol"], depth[i], "book" if here.get("market") else "pool", r, here.get("books") or 1)
    if best_tok:
        sym, d, what, r, n = best_tok
        if what == "book" and n > 1:  # several books summed: never "the deepest book"
            add("token_depth", f"The deepest {sym} order books", f"{short_money(d)} of book depth within 1% "
                f"of mid across {n} books", r, d, cap=f"Book depth within 1% of mid, {sym}, {n} books")
        elif what == "book":
            add("token_depth", f"The deepest {sym} book", f"{short_money(d)} of book depth within 1% of mid", r, d,
                cap=f"Book depth within 1% of mid, {sym}")
        else:
            add("token_depth", f"The deepest {sym} pool", f"{short_money(d)} within 1% of mid", r, d,
                cap=f"Within 1% of mid in its {sym} pool")
    # one pool pair: the largest pool for it
    live_pools = [p for p in pools if p["tvl_usd"] >= MIN_LIQUIDITY_USD]

    def best_pair(field):
        best = None
        by_pair: dict[str, dict] = {}
        for p in live_pools:
            if p.get(field) is not None:
                by_pair.setdefault(p["pair"], {})[p["venue"]] = p[field]
        still = {p["pair"] for p in live_pools if p["venue"] == i and p.get("not_trading_since")}
        for pair, vals in by_pair.items():
            if pair not in still and (r := _lead(vals, i)) and (best is None or vals[i] > best[1]):
                best = (pair, vals[i], r)
        return best

    if b := best_pair("tvl_usd"):
        pair, v, r = b
        add("pool_tvl", f"The largest {pair} pool", f"{short_money(v)} in liquidity", r, v,
            cap=f"In its {pair} pool")
    # one token no other venue we track carries at $1K or more. A market we cannot measure is not thin,
    # so it blocks the claim; a book read with our own key never leads.
    only = None
    for t in tokens:
        here = t["venues"].get(i)
        if not here or (f.get("keyed") and here.get("market")) or here.get("not_trading_since"):
            continue
        liq = token_liquidity(here) or 0
        others = [x for v, x in t["venues"].items() if v != i]
        if liq < MIN_LIQUIDITY_USD or any((token_liquidity(x) is None or token_liquidity(x) >= MIN_LIQUIDITY_USD)
                                          for x in others):
            continue
        if only is None or liq > only[1]:
            only = (t["symbol"], liq, bool(others), "book" if here.get("market") else "pool")
    if only:
        sym, liq, thin_elsewhere, what = only
        floor = " with $1K or more" if thin_elsewhere else ""
        unit = "within 1% of mid" if what == "book" else "in liquidity"
        add("unique_token", f"The only {sym} {what}{floor}", f"{short_money(liq)} {unit}",
            (None, None, None), liq, cap=f"In its {sym} {what}" if what == "pool" else f"Within 1% of mid, {sym}")
    # one pool pair: the most traded, and the best fee income to its liquidity providers
    if b := best_pair("volume_24h_usd"):
        pair, v, r = b
        add("pair_volume", f"The most traded {pair} pool", f"{short_money(v)} traded in 24 hours",
            r, v, cap=f"{pair} traded in the last 24 hours")
    if b := best_pair("fee_apr"):
        pair, v, r = b
        add("pair_apr", f"The highest {pair} LP fee APR",
            f"{v * 100:.1f}% a year from the last 24 hours of fees", r, v,
            fmt=lambda x: f"{x * 100:.1f}%", big=f"{v * 100:.1f}%",
            cap="Fee APR: the last 24 hours of LP fees over liquidity, annualised")
    # one quote: a single direction and size it still wins clearly once each venue's known network
    # fee is taken off, the largest size first, between two markets that each hold the trade's size
    clear = [w for w in f.get("won_rows", [])
             if i in NETWORK_FEE and w["size"] >= MIN_PRICE_LEAD_USD and w.get("real", True)
             and w["edge_bps"] >= CLEAR_EDGE_BPS and (w.get("net_edge_bps") or 0) >= CLEAR_EDGE_BPS]
    if clear:
        w = max(clear, key=lambda x: (x["size"], x["net_edge_bps"]))
        add("best_quote", f"Best price to {_direction(w)} at {SIZE_LABEL.get(w['size'], money(w['size']))}",
            best_quote_sub(w),
            (w["next"], w["net_edge_bps"], w["net_edge_bps"] / 10_000), quote_edge_bps(w), fmt=lambda b: "",
            big=f"+{quote_edge_bps(w) / 100:.2f}%",
            cap=(f"More {received(w)} back than the next best venue, pool fees, price impact and network "
                 "fees included"))
    order = list(CATEGORIES)
    return sorted(out, key=lambda x: (-x["score"], order.index(x["rule"])))


def _short(title: str) -> str:
    """"The largest spot venue on Canton" -> "Largest spot venue on Canton": the page's #1 line."""
    t = title[4:] if title.startswith("The ") else title
    return t[:1].upper() + t[1:]


# what headlines said before 2026-10-08; a venue held by the publish guard can still carry one
LEGACY_SCOPE = " among the Canton venues we read"


def tag(title: str) -> str:
    """The lead for the venues table, short: "Largest spot venue on Canton"."""
    return _short(title.replace(LEGACY_SCOPE, ""))


def token_liquidity(x: dict) -> float | None:
    """One token's liquidity on one venue, as the $1K rule reads it: a pool's liquidity, or a
    book's dollars within 1% of mid. None when the data has none."""
    return x.get("depth_1pct_usd") if x.get("market") else x.get("liquidity_usd")


def token_split(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """A venue's token rows split by the $1K rule on THAT venue: (counted, thin). Liquidity the
    token has elsewhere never counts here; an unmeasured market is not claimed as $1K+."""
    ok = [x for x in rows if (token_liquidity(x) or 0) >= MIN_LIQUIDITY_USD]
    return ok, [x for x in rows if (token_liquidity(x) or 0) < MIN_LIQUIDITY_USD]


def token_facts(f: dict) -> dict:
    """The one source for every token figure a card or page shows: the count, the names (non-
    stablecoins first) and the footer sentence, all from ``token_split`` on this venue."""
    ok, thin = token_split(f.get("tokens") or [])
    ok = [x for x in ok if not _stable(x)] + [x for x in ok if _stable(x)]
    names = [x["symbol"] for x in ok]
    floor = money(MIN_LIQUIDITY_USD)
    what = "within 1% of mid" if any(x.get("market") for x in f.get("tokens") or []) else "of liquidity"
    return {"n": len(names), "names": names, "rows": ok, "thin": thin, "what": what,
            "label": f"{len(names)} with {floor}+ liquidity",
            "short": f"Tokens: {len(names)} with {floor}+ on {f['name']}.",
            "note": f"Tokens: the {len(names)} with {floor} or more {what} on {f['name']} itself."}


def pools_text(f: dict) -> str:
    """"5 CC pools", or "9 pools" once pools of two other tokens count too: every pool counts, priced
    or not."""
    n = f.get("pool_n", 0)
    cc = "" if any(p.get("other") for p in f.get("pools") or []) else "CC "
    return f"{n} {cc}pool{'' if n == 1 else 's'}"


def headline(f: dict) -> dict:
    """The card's title and subtitle: the venue's best fact (``leads`` comes sorted by ``SCORE``), or
    a plain card when it has none or its ``VENUES`` row sets ``"lead": False``."""
    if f.get("leads") and f["venue"].get("lead", True):
        return f["leads"][0]
    if f.get("pools"):
        return {"rule": "pools", "title": f"{f['name']} on Canton",
                "sub": f"{short_money(f['tvl'])} in liquidity across {pools_text(f)}", "value": f["tvl"],
                "big": short_money(f["tvl"]), "cap": f"In liquidity across {pools_text(f)}"}
    if f.get("tokens"):
        n = token_facts(f)["n"]
        return {"rule": "read", "title": f"{f['name']} on Canton",
                "sub": f"{n} tokens with {money(MIN_LIQUIDITY_USD)} or more of liquidity",
                "value": None, "big": str(n), "cap": f"Tokens with {money(MIN_LIQUIDITY_USD)} or more of liquidity"}
    if f.get("perp_volume"):  # a perps-only venue between leads (its market count flips around 1K a day)
        return {"rule": "read", "title": f"{f['name']} on Canton",
                "sub": f"{short_money(f['perp_volume'])} of perpetuals traded in the last 24 hours",
                "value": None, "big": short_money(f["perp_volume"]), "cap": "Perpetuals traded in the last 24 hours"}
    if f.get("perp_volume_30d"):
        return {"rule": "read", "title": f"{f['name']} on Canton",
                "sub": f"{short_money(f['perp_volume_30d'])} of perpetuals traded in the last 30 days",
                "value": None, "big": short_money(f["perp_volume_30d"]),
                "cap": "Perpetuals traded in the last 30 days, its own figure"}
    return {"rule": "read", "title": f"{f['name']} on Canton", "sub": f["kind"], "value": None}


def head_scope(f: dict, head: dict) -> str:
    """The scope line a headline needs: a comparison no outside list confirms chain-wide is "based on
    the N Canton venues with public market data"; a confirmed one, or a plain card, needs none."""
    if head["rule"] in PLAIN or head.get("scope") == "canton":
        return ""
    return scope_line(f.get("tracked_n") or len(VENUES))


def is_ranked(head: dict) -> bool:
    """A place in a ranking (shown as #1). A head held by the publish guard from before ``rank``
    existed is a ranking unless it is a plain card or a price fact."""
    if "rank" in head:
        return bool(head["rank"])
    return head["rule"] in RANKED


def is_win(head: dict) -> bool:
    """Shown with a green #1: first in a ranking, or the best price on most of the trades compared
    at one size (a majority is required before that fact exists at all)."""
    return is_ranked(head) or head["rule"] == "best_count"


def lead_html(f: dict, head: dict) -> str:
    """The page's lead: the figure large, a green #1 and the one line it leads at, a caption under it.
    No runner-up: we never name another venue in a venue's own public text."""
    big = head.get("big")
    if head["rule"] in PLAIN:
        line = ""
    elif is_win(head):
        line = f'<span class="vwin"><b class="up">#1</b> {e(head.get("short") or _short(head["title"]))}</span>'
    else:
        line = f'<span class="vwin">{e(head.get("short") or _short(head["title"]))}</span>'
    fig = f'<span class="vbig num">{e(big)}</span>' if big else ""
    cap = f'<p class="vcap">{e(head["cap"])}.</p>' if head.get("cap") else ""
    return f'<div class="vfig">{fig}{line}</div>{cap}' if (fig or line) else cap


def names_text(names: list[str], shown: int = 4) -> str:
    """"CBTC, eXAU, eXAG", or the first ``shown`` and a count of the rest: "USDC.B, USDCx +21"."""
    rest = len(names) - shown
    return ", ".join(names[:shown]) + (f" +{rest}" if rest > 0 else "")


def fit_value(d, p: dict, font_at, width: float, size: int, floor: int):
    """A tile's value and its font: the largest size down to ``floor`` at which it fits. A list of
    names drops names (four at most, then "+N") before it would go under the floor."""
    names = p.get("names")
    tries = [names_text(names, k) for k in range(min(4, len(names)), 0, -1)] if names else [p["v"]]
    for text in tries:
        z = size
        while z > floor and d.textlength(text, font=font_at(z)) > width:
            z -= 2
        if d.textlength(text, font=font_at(z)) <= width:
            return text, font_at(z)
    return tries[-1], font_at(floor)


QUOTE_NAMES = {"USDCX": "USDCx", "USDCB": "USDC.B", "USDC.B": "USDC.B", "USDT": "USDT", "CC": "CC"}


def market_pair(symbol: str, market: str | None) -> str:
    """A book's pair the way people write it: "CBTC-USDCB" -> "CBTC/USDC.B"."""
    if not market:
        return symbol
    for sep in ("/", "-"):
        if sep in market:
            q = market.rsplit(sep, 1)[1]
            return f"{symbol}/{QUOTE_NAMES.get(q.upper(), q)}"
    return market


def depth_pair(d: dict) -> str:
    """What a book-depth figure covers: one book's pair, or the token and how many books are summed
    (Rocky's "$72M, CBTC/USDCx" was its CBTC/USDCx and CBTC/USDC.B books together, 2026-10-15)."""
    n = d.get("books") or 1
    return f"{d['symbol']}, {n} books" if n > 1 else market_pair(d["symbol"], d.get("market"))


def _stable(x: dict) -> bool:
    return (x.get("key") or x["symbol"]).upper() in STABLES


def token_tile(f: dict) -> dict:
    """The tokens there with $1K or more, by name, the largest non-stablecoin tokens first. A venue
    that publishes its own market list (Temple) gets that list instead: every market that traded on
    it in the last 24h, in its own words."""
    tf = token_facts(f)
    names = tf["names"]
    listed = f.get("listed") or []
    if listed:
        n = len(listed)
        return {"k": "Markets", "v": names_text(listed), "names": listed, "m": "markets",
                "n": f"{n} traded in the last 24h"}
    return {"k": "Tokens", "v": names_text(names), "names": names, "m": "tokens", "n": tf["label"]}


def largest_market(f: dict) -> tuple[str, float] | None:
    """The venue's largest market by 24h volume, from its own per-market figures: (pair, dollars)."""
    books = {k: v for k, v in (f.get("book_volume") or {}).items() if v}
    if not books:
        return None
    k = max(books, key=books.get)
    return book_label(k), books[k]


def perp_tile(f: dict) -> dict:
    """Perp markets by name, the ones trading first; trading means $1K or more in 24h, the same line
    the "most perp markets" headline draws."""
    ms = f["perp_markets"]
    if all(m["turnover_24h_usd"] is None for m in ms):  # no 24h figure published: trading is unknown
        names = [m["base"] for m in ms]
        return {"k": "Perp markets", "v": names_text(names), "names": names, "m": "perp_markets_read",
                "n": f"{len(ms)} we read"}
    live = [m["base"] for m in ms if (m["turnover_24h_usd"] or 0) >= MIN_PERP_TURNOVER_USD]
    if not live:
        names = [m["base"] for m in ms]
        return {"k": "Perp markets", "v": names_text(names), "names": names, "m": "perp_markets",
                "n": f"{len(ms)} listed, none trading"}
    n = f"{len(ms)} listed, all trading" if len(live) == len(ms) else f"{len(live)} trading, {len(ms)} listed"
    return {"k": "Perp markets trading", "v": names_text(live), "names": live, "n": n, "m": "perp_markets"}


def spot_share_text(f: dict) -> str:
    """"82% of Canton DEX spot volume": the venue's share of the spot volume of every Canton DEX with
    public market data (we track all of it). Empty under 1%."""
    s = f.get("spot_share")
    return f"{s * 100:.0f}% of Canton DEX spot volume" if s and s >= 0.01 else ""


def stats(f: dict) -> list[dict]:
    """Up to four card panels: header, value, small line under it, and ``m``, the kind of figure,
    which the page's method notes explain. A tile of names carries them in ``names`` so the card can
    fit as many as the width allows. No method on a tile: how a figure is reached is on the page."""
    out = []
    if f.get("spot_volume"):
        rank = ordinal_rank(f["spot_rank"], f["spot_n"])
        out.append({"k": "Spot volume, 24h", "v": short_money(f["spot_volume"]), "m": "spot_volume",
                    "n": rank or spot_share_text(f)})
    if f.get("perp_volume"):
        out.append({"k": "Perps volume, 24h", "v": short_money(f["perp_volume"]), "m": "perp_volume",
                    "n": ordinal_rank(f["perp_rank"], f["perp_n"])})
    elif f.get("perp_volume_30d"):
        out.append({"k": "Perps volume, 30 days", "v": short_money(f["perp_volume_30d"]), "m": "perp_volume_30d",
                    "n": "its own figure"})
    if f.get("pools"):
        out.append({"k": "In pools", "v": short_money(f["tvl"]), "n": pools_text(f), "m": "tvl"})
    elif f.get("keyed"):
        # a keyed book's depth cannot be checked from outside: the venue's share of Canton's spot
        # volume and its largest market take its place
        if f.get("spot_share"):
            out.append({"k": "Share of spot volume", "v": f"{f['spot_share'] * 100:.0f}%", "m": "share",
                        "n": "of Canton DEX spot volume"})
        if big := largest_market(f):
            out.append({"k": "Largest market", "v": short_money(big[1]), "m": "market", "n": f"{big[0]}, 24h"})
    elif f.get("deepest") and f["deepest"]["usd"]:
        out.append({"k": "Book depth within 1%", "v": short_money(f["deepest"]["usd"]), "m": "depth",
                    "n": depth_pair(f["deepest"])})
    if f.get("open_interest"):
        out.append({"k": "Open interest", "v": short_money(f["open_interest"]), "n": "all markets", "m": "oi"})
    if f.get("perp_markets") and len(out) < 4:
        out.append(perp_tile(f))
    if (token_facts(f)["n"] or f.get("listed")) and len(out) < 4:
        out.append(token_tile(f))
    if f.get("pools") and len(out) < 4:
        p = f["pools"][0]
        out.append({"k": "Largest pool", "v": short_money(p["tvl_usd"]), "m": "tvl",
                    "n": p["pair"] if p.get("priced", True) else "CC and an unnamed token"})
    if f.get("pools") and len(out) < 4:
        fees = sorted({p["fee"] for p in f["pools"] if p.get("fee") is not None})
        if fees:
            out.append({"k": "Pool fee", "v": f"{fees[0] * 100:.2f}%" if len(fees) == 1
                        else f"{fees[0] * 100:.2f}% to {fees[-1] * 100:.2f}%", "n": "per swap", "m": "fee"})
    return out[:4]


def tracked(f: dict) -> str:
    return f"the {f.get('tracked_n') or len(VENUES)} Canton venues with public market data"


METHOD = {
    "spot_volume": "Spot volume ranked across {tracked}.",
    "kind_volume": "Volume ranked across {tracked}.",
    "perp_volume": "Perps turnover ranked across {tracked}.",
    "tvl": "Pool liquidity ranked across {tracked}.",
    "best_count": ("Best price count: the same trade on every venue that quotes it, at one size, counted only "
                   "between markets that each hold at least that size. A trade is won when the venue gives the "
                   "most back after pool fees, price impact and network fees, its own network fee at the top "
                   "of its documented or measured range and the next venue's at the bottom of theirs. Shown "
                   "only at $1K or more, for a venue whose own network fee is known, when it wins most of them."),
    "tokens": "Tokens with $1K or more of liquidity, matched by Canton instrument, across {tracked}.",
    "pool_count": "Pools with $1K or more of liquidity, from live reserves, across {tracked}.",
    "perp_markets": "Perp markets with $1K or more traded in 24h, from each venue's public API, across {tracked}.",
    "largest_pool": "Each venue's largest pool by liquidity, from live reserves, across {tracked}.",
    "token_depth": "Dollars within 1% of mid across {tracked}; markets under $1K set aside.",
    "pool_tvl": "Pool liquidity from live reserves across {tracked}; pools under $1K set aside.",
    "unique_token": "No other venue we track has this token at $1K or more of liquidity.",
    "pair_volume": "Each pool's 24h volume across {tracked}; pools under $1K set aside.",
    "pair_apr": ("Fee APR: the last 24 hours of LP fees over liquidity, annualised, across {tracked}; pools "
                 "under $1K set aside."),
    "best_quote": ("Best price: same amount on every venue, pool fees, price impact and each venue's network "
                   "fee included. A venue leads on price only at $1K or more, only when its own per-swap network "
                   "fee is documented or measured, and only when the edge survives that fee at the top of its "
                   "range with the runner-up charged the bottom of theirs."),
    "pools": "",
    "read": "Read from the venue's API.",
}
# venues read through their web app's public routes, not a documented API (Canborsa since 2026-10-28)
WEB_APP_READ = {"canborsa"}
# the "on Canton" version of a ranking line
METHOD_CANTON = "Ranked {what} across every Canton DEX with public market data."


def metric_note(m: str, f: dict) -> str:
    """What one kind of card figure is, in a sentence, for the page's method notes."""
    if m == "spot_volume":
        return volume_method(f["id"], f["name"], f.get("cc_usd"))
    if m == "tokens":
        return token_facts(f)["note"]
    if m == "perp_volume":
        quotes = ", ".join(sorted({x["quote"] for x in f.get("perp_markets") or []})) or "the quote token"
        return f"Perps volume: 24h turnover in {quotes}, counted at $1."
    if m == "perp_volume_30d":
        return (f"Perps volume, 30 days: {f['name']}'s own figure over every perp market, each quote token "
                "counted at $1.")
    return {
        "tvl": "Pool liquidity: twice each CC pool's CC reserve, at our CC price; a pool of two other "
               "tokens at our prices of both (twice the one side we price).",
        "share": "Share: of the 24h spot volume of every Canton DEX with public market data.",
        "depth": "Book depth: dollars resting within 1% of mid on both sides of the venue's public order book.",
        "market": f"Largest market: {f['name']}'s own 24h volume per market.",
        "markets": f"Markets: every market {f['name']} reports as traded in the last 24 hours.",
        "oi": "Open interest: open contracts at mark price.",
        "perp_markets": "Perp markets trading: $1K or more in 24h.",
        "perp_markets_read": "Perp markets: the ones we read, each with an outside spot price for its basis.",
        "fee": "Pool fee: per swap, before network fees.",
    }.get(m, "")


def card_notes(f: dict, head: dict) -> str:
    """The card footer's second line: the scope of its headline when it needs one, then that the card
    is independent. How each figure is read is on the page (``card_method``), not on the card."""
    scope = head_scope(f, head)
    return f"{scope} Independent, not affiliated with {f['name']}." if scope else \
        f"Independent data, not affiliated with {f['name']}."


def card_method(f: dict, head: dict) -> list[str]:
    """The card's figures in full sentences, for the page's method notes."""
    rule = head["rule"]
    if head.get("scope") == "canton":
        lead = METHOD_CANTON.format(what={"spot_volume": "by spot volume"}.get(rule, "by volume"))
    else:
        lead = METHOD.get(rule, "").format(tracked=tracked(f))
        if rule == "read" and f.get("id") in WEB_APP_READ:
            lead = "Read from the public routes of the venue's web app; it has no documented API yet."
    out = [lead] if lead else []
    for p in stats(f):
        note = metric_note(p.get("m", ""), f)
        if note and note not in out:
            out.append(note)
    return out


# === card ==================================================================

def _font(weight: str, size: int):
    from PIL import ImageFont
    return ImageFont.truetype(str(FONTS / f"Inter-{weight}.ttf"), size)


def _wrap(draw, text: str, font, width: int, lines: int) -> list[str]:
    words, out, cur = text.split(), [], ""
    for w in words:
        nxt = f"{cur} {w}".strip()
        if draw.textlength(nxt, font=font) <= width or not cur:
            cur = nxt
        else:
            out.append(cur)
            cur = w
    out.append(cur)
    if len(out) > lines:
        out = out[:lines]
        while draw.textlength(out[-1] + "…", font=font) > width and " " in out[-1]:
            out[-1] = out[-1].rsplit(" ", 1)[0]
        out[-1] += "…"
    return out


def _fit(draw, text: str, weight: str, size: int, width: int, floor: int):
    """The largest font size, down to ``floor``, at which ``text`` fits on one line."""
    while size > floor and draw.textlength(text, font=_font(weight, size)) > width:
        size -= 2
    return _font(weight, size)


def _balanced(draw, text: str, font, width: int) -> list[str] | None:
    """Two lines of near-equal width, so a long headline never ends on one orphaned word; None
    when no split fits."""
    words = text.split()
    best = None
    for n in range(1, len(words)):
        a, b = " ".join(words[:n]), " ".join(words[n:])
        w = max(draw.textlength(a, font=font), draw.textlength(b, font=font))
        if w <= width and (best is None or w < best[0]):
            best = (w, [a, b])
    return best[1] if best else None


def note_lines(draw, text: str, font_at, width: int, size: int, two: int, floor: int):
    """The footer note on one line while it fits at ``size`` or up to 4px under, else two even
    lines at the largest size from ``two`` down to ``floor`` that fits."""
    for z in range(size, size - 5, -1):
        if draw.textlength(text, font=font_at(z)) <= width:
            return [text], font_at(z)
    for z in range(two, floor - 1, -1):
        lines = _balanced(draw, text, font_at(z), width)
        if lines:
            return lines, font_at(z)
    return _wrap(draw, text, font_at(floor), width, 2), font_at(floor)


def _hex(c: str) -> tuple[int, int, int]:
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


# The dashboard's own tokens (site/index.html :root and [data-theme="dark"]), so a card reads as a
# piece of cantonvenues.com. Light is the default: it is the site's default theme, and on X's dark
# timeline a white card stands apart where a navy one would sink into the page.
THEMES = {
    "light": {"bg": "#ffffff", "bg2": "#f8fafd", "line": "#eff2f5", "line2": "#e3e7ee", "text": "#0d1421",
              "text2": "#58667e", "text3": "#a1a7bb", "accent": "#3861fb", "shadow": True,
              "strip": "#0d1421", "strip_text": "#ffffff", "strip_2": "#a1a7bb", "strip_accent": "#6188ff"},
    "dark": {"bg": "#0d1421", "bg2": "#171924", "line": "#222531", "line2": "#323546", "text": "#ffffff",
             "text2": "#a1a7bb", "text3": "#646b80", "accent": "#6188ff", "shadow": False,
             "strip": "#171924", "strip_text": "#ffffff", "strip_2": "#a1a7bb", "strip_accent": "#6188ff"},
}
UP = "#16c784"
DOWN = "#ea3943"
# the four-square mark from the site's header svg (viewBox 26): x, y, w, h, fill
LOGO = ((1, 1, 11, 11, UP), (14, 1, 11, 7, "#3861fb"), (14, 10, 11, 15, None), (1, 14, 11, 11, DOWN))


def _mix(a, b, k: float):
    return tuple(round(x * k + y * (1 - k)) for x, y in zip(a, b))


def draw_logo(d, x: float, y: float, size: float, text_rgb, bg_rgb) -> None:
    """The site's mark at ``size`` px; its dark square is the text colour at 85%, as in the svg."""
    k = size / 26
    for rx, ry, w, h, c in LOGO:
        fill = _hex(c) if c else _mix(text_rgb, bg_rgb, 0.85)
        d.rounded_rectangle((x + rx * k, y + ry * k, x + (rx + w) * k, y + (ry + h) * k), radius=3 * k, fill=fill)


# which card ships: "venue" draws each venue's card in its own visual style (venue_style.py);
# "ours" draws every card in the dashboard's own look (``theme`` light or dark)
LOOKS = ("venue", "ours")


def render_card(f: dict, head: dict, t: int, path: Path, theme: str = "light", look: str = "venue") -> None:
    """The venue's 1200x630 share card, in its own style (``look="venue"``) or ours."""
    if look == "venue":
        import venue_style
        venue_style.render(f, head, t, path)
    else:
        render_card_ours(f, head, t, path, theme)


def render_card_ours(f: dict, head: dict, t: int, path: Path, theme: str = "light") -> None:
    """A 1200x630 PNG in the dashboard's own look, drawn at 2x and scaled down for clean edges.

    Header as on the site (mark, name, a live chip), the venue and its type, the lead as the
    headline, up to four stat tiles shaped like the dashboard's, and a dark source strip at the
    foot naming cantonvenues.com, the time and how the headline was measured.
    """
    from PIL import Image, ImageDraw, ImageFilter

    c = {k: (_hex(v) if isinstance(v, str) else v) for k, v in THEMES[theme].items()}
    S = 2
    W, H = 1200 * S, 630 * S
    M = 56 * S
    img = Image.new("RGB", (W, H), c["bg"])
    d = ImageDraw.Draw(img)

    # header: the site's mark and name, a hairline under it like the site header's border
    draw_logo(d, M, 38 * S, 40 * S, c["text"], c["bg"])
    d.text((M + 54 * S, 58 * S), "Canton Venues", font=_font("Bold", 30 * S), fill=c["text"], anchor="lm")
    live = "Live data"
    lf = _font("SemiBold", 20 * S)
    lw = d.textlength(live, font=lf)
    x1 = W - M
    x0 = x1 - lw - 50 * S
    d.rounded_rectangle((x0, 40 * S, x1, 76 * S), radius=8 * S, fill=_mix(_hex(UP), c["bg"], 0.12))
    d.ellipse((x0 + 16 * S, 53 * S, x0 + 26 * S, 63 * S), fill=_hex(UP))
    d.text((x0 + 34 * S, 58 * S), live, font=lf, fill=_mix(_hex(UP), (0, 0, 0), 0.8) if theme == "light"
           else _hex(UP), anchor="lm")
    d.line((0, 104 * S, W, 104 * S), fill=c["line2"], width=S)

    # the venue, the lead as the headline, its numbers under it
    width = W - 2 * M
    title = head["title"]
    # one line while it fits at 56px or more, else two even lines; never an orphaned last word
    tfont, lines = None, None
    for size in (64, 60, 56):
        if d.textlength(title, font=_font("Bold", size * S)) <= width:
            tfont, lines = _font("Bold", size * S), [title]
            break
    if not lines:
        for size in (58, 54, 50, 46):
            tfont = _font("Bold", size * S)
            lines = _balanced(d, title, tfont, width)
            if lines:
                break
        if not lines:
            lines = _wrap(d, title, tfont, width, 2)
    step = round(tfont.size * 1.12)
    sub_font = _fit(d, head["sub"], "Regular", 28 * S, width, 20 * S)
    block = 40 * S + 18 * S + len(lines) * step + 12 * S + 36 * S
    y = 104 * S + max(26 * S, (284 * S - block) // 2)

    nf = _font("SemiBold", 30 * S)
    d.text((M, y + 20 * S), f["name"], font=nf, fill=c["text"], anchor="lm")
    kx = M + d.textlength(f["name"], font=nf) + 16 * S
    kf = _font("SemiBold", 19 * S)
    kind = f["kind"]
    d.rounded_rectangle((kx, y + 3 * S, kx + d.textlength(kind, font=kf) + 24 * S, y + 37 * S), radius=8 * S,
                        fill=c["line"])
    d.text((kx + 12 * S, y + 20 * S), kind, font=kf, fill=c["text2"], anchor="lm")
    y += 40 * S + 18 * S
    for line in lines:
        d.text((M, y), line, font=tfont, fill=c["text"], anchor="lt")
        y += step
    d.text((M, y + 12 * S), head["sub"], font=sub_font, fill=c["text2"], anchor="lt")

    # stat tiles: the dashboard's .stat (hairline border, card fill, grey label over a bold value)
    panels = stats(f)
    if panels:
        gap, top, ph = 16 * S, 392 * S, 118 * S
        pw = (width - gap * (len(panels) - 1)) // len(panels)
        boxes = [(M + n * (pw + gap), top, M + n * (pw + gap) + pw, top + ph) for n in range(len(panels))]
        if c["shadow"]:  # the site's --shadow: 0 1px 2px and 0 4px 24px of #58667e
            for dy, blur, alpha in ((1, 2, 31), (4, 24, 20)):
                m = Image.new("L", (W, H), 0)
                md = ImageDraw.Draw(m)
                for b in boxes:
                    md.rounded_rectangle((b[0], b[1] + dy * S, b[2], b[3] + dy * S), radius=16 * S, fill=alpha)
                m = m.filter(ImageFilter.GaussianBlur(blur * S / 2))
                img = Image.composite(Image.new("RGB", (W, H), (88, 102, 126)), img, m)
            d = ImageDraw.Draw(img)
        pad = 22 * S
        for p, b in zip(panels, boxes):
            d.rounded_rectangle(b, radius=16 * S, fill=c["bg"], outline=c["line2"], width=2 * S)
            inner = pw - 2 * pad
            d.text((b[0] + pad, top + 18 * S), p["k"], font=_fit(d, p["k"], "Regular", 21 * S, inner, 15 * S),
                   fill=c["text2"], anchor="lt")
            v, vf = fit_value(d, p, lambda z: _font("Bold", z), inner, 42 * S, 24 * S)
            d.text((b[0] + pad, top + 46 * S), v, font=vf, fill=c["text"], anchor="lt")
            first = p["n"].startswith("#1 ")
            d.text((b[0] + pad, top + 96 * S), p["n"],
                   font=_fit(d, p["n"], "SemiBold" if first else "Regular", 18 * S, inner, 13 * S),
                   fill=_hex(UP) if first else c["text2"], anchor="lt")

    # the source strip: where this came from, when, and how the headline was measured
    st = 538 * S
    d.rectangle((0, st, W, H), fill=c["strip"])
    sf = _font("SemiBold", 24 * S)
    y0 = st + 30 * S
    x = M
    for part, col in (("Live Canton DEX data from ", c["strip_text"]), ("cantonvenues.com", c["strip_accent"])):
        d.text((x, y0), part, font=sf, fill=col, anchor="lm")
        x += d.textlength(part, font=sf)
    when = stamp(t)
    wf = _font("Regular", 20 * S)
    d.text((W - M, y0), when, font=wf, fill=c["strip_2"], anchor="rm")
    lines, nf = note_lines(d, card_notes(f, head), lambda z: _font("Regular", z), width, 18 * S, 16 * S, 14 * S)
    for n, line in enumerate(lines):
        d.text((M, st + (58 + 20 * n if len(lines) > 1 else 66) * S), line, font=nf, fill=c["strip_2"], anchor="lm")

    out = img.resize((1200, 630), Image.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    out.save(tmp, "PNG", optimize=True)
    os.replace(tmp, path)


# === pages =================================================================

def _site_parts() -> tuple[str, str, str, str]:
    """The dashboard's own stylesheet, logo mark, theme icons and chart code, so a venue page looks
    and draws like the site."""
    src = (HERE / "site" / "index.html").read_text()
    style = re.search(r"<style>(.*?)</style>", src, re.DOTALL).group(1)
    logo = re.search(r'<a class="logo" href="#">\s*(<svg.*?</svg>)', src, re.DOTALL).group(1)
    icons = "\n".join(re.findall(r"^const (?:SUN|MOON) = .*$", src, re.MULTILINE))
    chart = "\n".join(re.search(rf"^function {name}\(.*?^}}$", src, re.DOTALL | re.MULTILINE).group(0)
                      for name in ("money", "lineChart"))
    return style, logo, icons, chart.replace(" · ", ", ")


def _tracker() -> str:
    """The dashboard's conversion tracker (Telegram, shares, card downloads, API and MCP), so every
    generated page reports the same Umami events."""
    src = (HERE / "site" / "index.html").read_text()
    return re.search(r"^/\* -+ conversions:.*?^\}\)\(\);$", src, re.DOTALL | re.MULTILINE).group(0)


def site_nav() -> str:
    """The dashboard's own section menu, its in-page links pointed back at the dashboard, so every
    page carries the same menu and a change to it is made once, in site/index.html."""
    src = (HERE / "site" / "index.html").read_text()
    links = re.search(r'<nav class="nav" id="nav"[^>]*>\s*(.*?)\s*</nav>', src, re.DOTALL).group(1)
    return links.replace('href="#', 'href="/#')


def nav_venues(prefix: str = "/") -> str:
    """The "Venues" menu: every venue page and the index. The dashboard carries the same block,
    written out in site/index.html (a test keeps the two equal)."""
    links = "".join(f'<a href="{prefix}venues/{v["slug"]}/">{e(v["name"])}</a>' for v in VENUES)
    return (f'<div class="dd" id="ddvenues"><button class="ddb" type="button" aria-expanded="false" '
            f'aria-controls="ddmenu">Venues<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 4.5 6 7.5 9 4.5" '
            f'fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>'
            f'</svg></button><div class="ddm" id="ddmenu"><a class="all" href="{prefix}venues/">All venues</a>'
            f'{links}</div></div>')


PAGE_CSS = """
.crumbs { margin: 20px 0 6px; font-size: 13px; color: var(--text-2); font-weight: 500; }
.fine { color: var(--text-3); font-size: 12px; margin: 8px 0 0; max-width: 80ch; }
.crumbs a { font-weight: 600; }
.vhead { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin: 4px 0 10px; }
.vhead h1 { font-size: 28px; margin: 0; letter-spacing: -0.01em; }
.vhead .vchip { font-size: 12px; }
.vfig { display: flex; align-items: baseline; gap: 6px 16px; flex-wrap: wrap; }
.vbig { font-size: 34px; font-weight: 700; letter-spacing: -0.01em; line-height: 1.2; }
.vwin { font-size: 16px; font-weight: 600; color: var(--text); }
.vwin b { margin-right: 4px; }
.vcap { color: var(--text-2); margin: 2px 0 18px; font-size: 14px; }
.vtop { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(0, 1fr); gap: 16px; align-items: start; }
.vcard { display: block; width: 100%; height: auto; aspect-ratio: 1200 / 630; border-radius: 12px; background: var(--bg); }
.acts { display: grid; gap: 10px; }
.btn { display: flex; align-items: center; justify-content: center; gap: 8px; border-radius: 10px; padding: 11px 16px; font-weight: 600; font-size: 14px; border: 1px solid var(--line-2); background: var(--bg-2); color: var(--text); }
.btn:hover { text-decoration: none; border-color: var(--accent); color: var(--accent); }
.btn.x { background: var(--text); color: var(--bg); border-color: var(--text); }
.btn.x:hover { color: var(--bg); filter: brightness(1.15); }
.btn svg { width: 16px; height: 16px; }
.acts .muted { line-height: 1.5; }
.best { color: var(--up); font-weight: 700; }
table.bp td.l { white-space: nowrap; }
table.bp th, table.bp td { width: 14%; }
table.bp th.l, table.bp td.l { width: auto; }
.bpmore summary { cursor: pointer; color: var(--accent); font-weight: 600; font-size: 13px; margin-top: 12px; }
.bpmore table thead { visibility: collapse; }
.charts { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.charts .tchart .empty { margin: 0; }
.charts > .panel:last-child:nth-child(odd) { grid-column: 1 / -1; }
.hbars .hrow { grid-template-columns: minmax(0, 9.5em) minmax(0, 1fr) 4.4em; padding: 5px 4px; }
.hbars .hl { color: var(--text); font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.hbars .hrow.other .hl { color: var(--text-2); font-weight: 500; }
.hbars .hrow.other .hb i { background: var(--text-3); }
table.vt td.lead { white-space: normal; min-width: 14em; }
table.vt .depthnote { font-size: 11px; font-weight: 500; }
table.vt .tagline { display: none; color: var(--text-2); font-size: 12px; font-weight: 500; margin-top: 2px; white-space: normal; }
ul.notes { margin: 6px 0 0; padding-left: 18px; color: var(--text-2); font-size: 13px; }
ul.notes li { margin: 3px 0; }
@media (max-width: 900px) { .vtop, .charts { grid-template-columns: 1fr; } }
@media (max-width: 720px) {
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .vbig { font-size: 30px; }
  table.vt .tagline { display: block; }
  .hbars .hrow { grid-template-columns: minmax(0, 7.5em) minmax(0, 1fr) 4.4em; }
}
"""

X_ICON = ('<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M17.8 3h3.1l-6.8 7.8'
          ' 8 10.2h-6.3l-4.9-6.4L5.3 21H2.2l7.3-8.3L1.8 3h6.4l4.4 5.9L17.8 3zm-1.1 16.2h1.7L7.4 4.7H5.6l11.1'
          ' 14.5z"/></svg>')

e = html.escape

# the Venues menu, the theme switch and the burger: the same behaviour as the dashboard's
NAV_JS = """
(function () {
  var b = document.getElementById("theme"), h = document.querySelector(".head"), m = document.getElementById("burger");
  var dd = document.getElementById("ddvenues"), ddb = dd.querySelector(".ddb");
  function label() { var d = document.documentElement.getAttribute("data-theme") === "dark"; b.innerHTML = d ? SUN : MOON; b.title = d ? "Light theme" : "Dark theme"; }
  label();
  b.addEventListener("click", function () {
    var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("venues-theme", next); } catch (e) {}
    label();
    if (window.charts) window.charts();
  });
  m.addEventListener("click", function () { var o = h.classList.toggle("open"); m.setAttribute("aria-expanded", o ? "true" : "false"); });
  ddb.addEventListener("click", function (ev) { ev.stopPropagation(); var o = dd.classList.toggle("open"); ddb.setAttribute("aria-expanded", o ? "true" : "false"); });
  document.addEventListener("click", function (ev) { if (!dd.contains(ev.target)) { dd.classList.remove("open"); ddb.setAttribute("aria-expanded", "false"); } });
  document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") { dd.classList.remove("open"); ddb.setAttribute("aria-expanded", "false"); } });
})();
"""


def _shell(title: str, desc: str, canonical: str, image: str | None, body: str, t: int, script: str = "") -> str:
    style, logo, icons, _ = _site_parts()
    og_img = (f'<meta property="og:image" content="{e(image)}">\n'
              '<meta property="og:image:width" content="1200">\n<meta property="og:image:height" content="630">\n'
              f'<meta name="twitter:image" content="{e(image)}">\n') if image else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(desc)}">
<link rel="canonical" href="{e(canonical)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Canton Venues">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{e(canonical)}">
{og_img}<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{e(title)}">
<meta name="twitter:description" content="{e(desc)}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<script>
  (function () {{
    var t = null;
    try {{ t = localStorage.getItem("venues-theme"); }} catch (e) {{}}
    if (!t) t = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", t);
  }})();
</script>
<link rel="icon" type="image/png" href="/icon.png">
<link rel="apple-touch-icon" href="/icon-180.png">
<script defer src="https://cloud.umami.is/script.js" data-website-id="da1b8603-0104-4e1f-8368-dbd9adbeafcb"></script>
<style>{style}{PAGE_CSS}</style>
</head>
<body>
<header class="head">
  <div class="wrap">
    <a class="logo" href="/">{logo} Canton Venues</a>
    <nav class="nav" id="nav" aria-label="Sections">
      {site_nav()}
    </nav>
    <a class="tg" href="https://t.me/cantonvenues" target="_blank" rel="noopener" aria-label="Telegram channel"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M21.9 4.3 18.7 19.4c-.2 1-.9 1.3-1.7.8l-4.8-3.5-2.3 2.2c-.3.3-.5.5-1 .5l.3-4.9 8.9-8c.4-.3-.1-.5-.6-.2L6.5 13.2 1.8 11.7c-1-.3-1-1 .2-1.5L20.5 3c.9-.3 1.6.2 1.4 1.3z"/></svg><span>Telegram</span></a>
    <a class="tg x" href="https://x.com/cantonvenues" target="_blank" rel="noopener" aria-label="Canton Venues on X"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg></a>
    <button class="theme" id="theme" type="button" aria-label="Switch theme"></button>
    <button class="burger" id="burger" type="button" aria-label="Menu" aria-expanded="false" aria-controls="nav"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg></button>
  </div>
</header>
<main class="wrap">
{body}
  <footer class="foot">
    <div><h4>Canton Venues</h4><p>Built by <a href="https://github.com/olevasyliev">Oleksii</a> on <a href="https://github.com/olevasyliev/canton-venues-sdk">Canton Venues SDK</a>, open source. Read-only: nothing here is signed or executed.</p><p><a href="https://t.me/cantonvenues">Telegram channel</a>, <a href="https://x.com/cantonvenues">X</a>, <a href="/#contact">contact</a></p><p>Updated {e(stamp(t))}.</p></div>
    <div><h4>Method</h4><p>Pools are priced from live reserves with each venue's own formula, order books from their books. Best execution compares what each venue returns for the same amount, pool fees, price impact and each venue's network fee included. Volumes are each venue's 24h figures; where a venue reports token amounts rather than dollars we convert them, and each venue's page says how.</p></div>
    <div><h4>Data</h4><p>Every number on this page is in the open JSON API: <a href="/api/v1/venues.json">venues</a>, <a href="/api/v1/execution.json">execution</a>, <a href="/api/v1/tokens.json">tokens</a>, <a href="/api/v1/lp.json">pools</a>, <a href="/api/v1/perps.json">perps</a>, <a href="/api/v1/venue_history.json">venue history</a>.</p></div>
  </footer>
</main>
<script>
{icons}
{NAV_JS}
{_tracker()}
{script}
</script>
</body>
</html>
"""


PLAIN = ("pools", "read")  # headlines that rank nothing and open with the venue's own name


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:]  # "CC trades" stays CC, unlike str.capitalize


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def share_text(f: dict, head: dict) -> str:
    # a post that opens with an @handle reads as a reply: the name goes first
    who = f"{f['name']} (@{f['venue']['x']})" if f["venue"].get("x") else f["name"]
    if head["rule"] in PLAIN:  # the title only repeats the name: say the numbers
        return f"{who} on Canton Venues: {head['sub']}."
    return f"Where {who} leads on Canton Venues: {_lower_first(head['title'])}. {head['sub']}."


def intent_url(f: dict, head: dict) -> str:
    url = f"{SITE}/venues/{f['venue']['slug']}/"
    return f"https://x.com/intent/post?text={quote(share_text(f, head))}&url={quote(url, safe='')}"


def _table(headers: list[tuple[str, str]], rows: list[list[str]], cls: str = "mini") -> str:
    th = "".join(f'<th class="{c}">{e(h)}</th>' for h, c in headers)
    body = "".join("<tr>" + "".join(f'<td class="{headers[i][1]}">{cell}</td>' for i, cell in enumerate(r))
                   + "</tr>" for r in rows)
    return f'<div class="tablewrap"><table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


# === history ===============================================================

def _day(t: int) -> str:
    d = datetime.fromtimestamp(t, UTC)
    return f"{d.day} {d:%b %Y}"


# A history chart draws only once it is a line worth reading: a day of our own hourly readings, or
# a few days of a daily record. Until then the panel is left out (and the History heading with it,
# when no panel qualifies); it appears by itself as the readings accrue.
MIN_HOURLY_POINTS = 24
MIN_DAILY_POINTS = 3


def venue_series(slug: str, f: dict, hist: dict) -> list[dict]:
    """The page's charts, from venue_history.json: each venue's daily volume and its share of Canton's
    where an outside record goes back, then our own hourly readings, each labelled with the date it
    starts (an outside source is credited in the method notes). A series shorter than
    ``MIN_DAILY_POINTS`` / ``MIN_HOURLY_POINTS`` is left out."""
    out = []
    daily = (hist.get("daily") or {}).get(slug) or {}
    pts = daily.get("points") or []
    if len(pts) >= MIN_DAILY_POINTS:
        # the source is credited once, in the page's method notes (``HISTORY_CREDIT``), not here
        own = daily.get("source") == "tradecraft"
        out.append({"kind": "daily", "title": "Volume by day", "source": daily.get("source"),
                    "sub": (f"Each day's volume, from {f['name']}'s own record, since {_day(pts[0][0])}." if own
                            else f"Each day's volume since {_day(pts[0][0])}."),
                    "pts": [[t * 1000, v] for t, v in pts], "fmt": "money"})
        if daily.get("source") == "defillama":
            tot = {t: v for t, v in hist.get("daily_total") or []}
            share = [[t * 1000, round(v / tot[t], 5)] for t, v in pts if tot.get(t)]
            if len(share) >= MIN_DAILY_POINTS:
                out.append({"kind": "share", "title": "Share of Canton DEX volume", "source": "defillama",
                            "sub": f"Its share of each day's Canton DEX volume, since {_day(pts[0][0])}.",
                            "pts": share, "fmt": "pct"})
    h = hist.get("hourly") or {}
    ts = h.get("t") or []
    cols = (h.get("venues") or {}).get(slug) or {}
    for key, title in (("tvl", "Liquidity in pools"), ("spot_volume", "Spot volume, last 24 hours"),
                       ("perp_volume", "Perps volume, last 24 hours"), ("open_interest", "Open interest")):
        if f.get(key) is None or (key == "spot_volume" and len(pts) >= MIN_DAILY_POINTS):
            continue
        p = [[t * 1000, v] for t, v in zip(ts, cols.get(key) or []) if v is not None]
        if len(p) < MIN_HOURLY_POINTS:
            continue
        out.append({"kind": key, "title": title,
                    "sub": f"Our own reading, every hour since {stamp(p[0][0] // 1000)}.",
                    "pts": p, "fmt": "money"})
    return out


# === now ===================================================================

# The "Right now" bar charts: the current state, so a page has charts before any history accrues.
# Up to BARS_SHOWN bars, largest first; a market under BAR_FLOOR_USD (the same $1K floor as
# MIN_LIQUIDITY_USD and MIN_PERP_TURNOVER_USD) and everything past the largest BARS_SHOWN are
# summed into one "other" bar. A market at zero is left out. A chart needs MIN_BARS bars in all:
# one bar compares nothing.
BARS_SHOWN = 10
BAR_FLOOR_USD = MIN_LIQUIDITY_USD
MIN_BARS = 2


def book_label(symbol: str) -> str:
    """A venue's market name the way people write it: "CBTC-USDCX" -> "CBTC/USDCx"."""
    for sep in ("/", "-"):
        if sep in symbol:
            base, q = symbol.rsplit(sep, 1)
            return f"{base}/{QUOTE_NAMES.get(q.upper(), q)}"
    return symbol


def bar_rows(items: list[tuple[str, float | None]], what: str) -> list[dict]:
    """(label, value) pairs as bars: the largest ``BARS_SHOWN`` at or over the floor, the rest summed
    into one "other" bar named for how many it holds (``what``: "pool", "market")."""
    vals = sorted(((k, float(v)) for k, v in items if v), key=lambda x: -x[1])
    big = [x for x in vals if x[1] >= BAR_FLOOR_USD][:BARS_SHOWN]
    rest = [x for x in vals if x not in big]
    rows = [{"label": k, "value": v} for k, v in big]
    if rest:
        n = len(rest)
        rows.append({"label": f"{n} other {what}{'' if n == 1 else 's'}", "value": sum(v for _, v in rest),
                     "other": True, "names": [k for k, _ in rest]})
    return rows if len(rows) >= MIN_BARS else []


def now_charts(f: dict, t: int) -> list[dict]:
    """The current-state bar charts a venue's data supports, each titled with what it shows and
    labelled with when and how it was read. Only figures the collector already publishes."""
    when = stamp(t)
    floor = money(BAR_FLOOR_USD)
    out = []
    if f.get("pools"):
        rows = bar_rows([(p["pair"], p["tvl_usd"]) for p in f["pools"]], "pool")
        if rows:
            out.append({"kind": "pool_tvl", "title": "Liquidity by pool", "rows": rows,
                        "sub": f"Now, as of {when}. Twice each pool's CC reserve, at our CC price. "
                               f"Pools under {floor}, and past the largest {BARS_SHOWN}, are summed as other."})
    books = f.get("book_volume") or {}
    perps = f.get("perp_markets") or []
    if books:
        rows = bar_rows([(book_label(k), v) for k, v in books.items()], "market")
        how = (f"each market's settled volume in its quote token, as {f['name']} reports it"
               if VOLUME_BASIS.get(f["id"]) == "usd" else
               "each book's turnover in its quote token, converted to dollars (USDC.B at $1, "
               "other quote tokens at our price)")
        if rows:
            out.append({"kind": "book_volume", "title": ("Spot volume" if perps else "Volume") + " by market, 24h",
                        "rows": rows,
                        "sub": f"Last 24 hours, as of {when}: {how}. Markets under {floor} are summed as other."})
    if perps:
        rows = bar_rows([(f"{m['base']}/{m['quote']}", m["turnover_24h_usd"]) for m in perps], "market")
        quotes = ", ".join(sorted({m["quote"] for m in perps}))
        if rows:
            out.append({"kind": "perp_volume", "title": ("Perps volume" if books else "Volume") + " by market, 24h",
                        "rows": rows,
                        "sub": f"Last 24 hours, as of {when}: turnover in each market's quote token ({quotes}), "
                               f"counted at $1. Markets under {floor} are summed as other."})
        rows = bar_rows([(f"{m['base']}/{m['quote']}", m["open_interest_usd"]) for m in perps], "market")
        if rows:
            out.append({"kind": "open_interest", "title": "Open interest by market", "rows": rows,
                        "sub": f"Now, as of {when}: open contracts at mark price. "
                               f"Markets under {floor} are summed as other."})
    return out


def now_html(f: dict, t: int) -> str:
    """The "Right now" section: the dashboard's horizontal bars (its spread-study rows), drawn here."""
    charts = now_charts(f, t)
    if not charts:
        return ""
    panels = []
    for c in charts:
        top = max(r["value"] for r in c["rows"]) or 1
        bars = "".join(
            f'<div class="hrow{" other" if r.get("other") else ""}" tabindex="0" '
            f'title="{e(r["label"] + (": " + ", ".join(r["names"]) if r.get("other") else ""))}, {e(money(r["value"]))}">'
            f'<span class="hl">{e(r["label"])}</span><span class="hb"><i style="width:{r["value"] / top * 100:.1f}%"></i></span>'
            f'<span class="hn num">{e(money(r["value"]))}</span></div>' for r in c["rows"])
        panels.append(f'<div class="panel"><h3>{e(c["title"])}</h3><p class="sub">{e(c["sub"])}</p>'
                      f'<div class="hbars">{bars}</div></div>')
    return f"""  <section class="block">
    <h2>Right now</h2>
    <div class="charts" style="margin-top:14px">{"".join(panels)}</div>
  </section>"""


def spark_svg(vals: list[float], w: int = 136, h: int = 44) -> str:
    """The dashboard's 7-day sparkline, drawn here: green when the last point is at or above the first."""
    if len(vals) < 7:  # an empty box of the same size keeps every row the same height
        return f'<svg class="spark" width="{w}" height="{h}" aria-hidden="true"></svg>'
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1
    pts = " ".join(f"{n / (len(vals) - 1) * w:.1f},{h - 3 - (v - lo) / span * (h - 6):.1f}" for n, v in enumerate(vals))
    col = "var(--up)" if vals[-1] >= vals[0] else "var(--down)"
    return (f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}" aria-hidden="true"><polyline '
            f'points="{pts}" fill="none" stroke="{col}" stroke-width="1.5" stroke-linejoin="round"/></svg>')


def charts_html(f: dict) -> tuple[str, str]:
    """(the History section, the script that draws it with the dashboard's own line chart)."""
    series = f.get("series") or []
    if not series:
        return "", ""
    panels = "".join(f'<div class="panel"><h3>{e(s["title"])}</h3><p class="sub">{e(s["sub"])}</p>'
                     f'<div class="tchart" id="ch{n}"></div></div>' for n, s in enumerate(series))
    data = json.dumps([{"pts": s["pts"], "fmt": s["fmt"]} for s in series],
                      separators=(",", ":")).replace("</", "<\\/")
    section = f"""  <section class="block">
    <h2>History</h2>
    <div class="charts" style="margin-top:14px">{panels}</div>
  </section>"""
    _, _, _, chart = _site_parts()
    # the dashboard's chart lifts the pen over a gap, which leaves its area fill a stray triangle; a day
    # DefiLlama skips is drawn straight across here instead
    chart = chart.replace("p[0] - pts[i - 1][0] <= 3 * med", "true")
    script = f"""const $ = (id) => document.getElementById(id);
{chart}
const SERIES = {data};
const FMT = {{ money: money, pct: (v) => (v * 100).toFixed(1) + "%" }};
window.charts = function () {{ SERIES.forEach((s, n) => lineChart("ch" + n, s.pts, FMT[s.fmt], {{ h: 200, color: "var(--accent)" }})); }};
window.charts();
let rz; addEventListener("resize", () => {{ clearTimeout(rz); rz = setTimeout(window.charts, 150); }});"""
    return section, script


# === best price ============================================================

def _trade(symbol: str, kind: str, side: str) -> str:
    """One direction of a quote in words: "Buy CBTC with dollars", "Sell HANDL for CC"."""
    if kind == "usd":
        return f"Buy {symbol} with dollars" if side == "buy" else f"Sell {symbol} for dollars"
    return f"Buy {symbol} with CC" if side == "sell" else f"Sell {symbol} for CC"


BEST_ROWS_SHOWN = 10  # trades in the best-price table before the rest fold away


def best_price_html(f: dict) -> str:
    """Per token and direction, at each trade size: is this venue the best price? One table, the
    tokens it wins first; tokens it never wins are named in one line under it."""
    q = f.get("quotes") or []
    if not q:
        return ""
    sizes = sorted({x["size"] for x in q})
    groups: dict[tuple, dict] = {}
    for x in q:
        groups.setdefault((x["symbol"], x["kind"], x["key"]), {})[(x["side"], x["size"])] = x["best"]
    wins = {k: sum(g.values()) for k, g in groups.items()}
    won = sorted((k for k in groups if wins[k]), key=lambda k: (-wins[k], k[0]))
    rest = sorted({k[0] for k in groups if not wins[k]} - {k[0] for k in won})
    name = e(f["name"])
    rows = []
    for k in won:
        sym, kind, key = k
        g = groups[k]
        for side in (("buy", "sell") if kind == "usd" else ("sell", "buy")):
            if not any((side, s) in g for s in sizes):
                continue
            cells = [('<span class="best">Best</span>' if g[(side, s)] else '<span class="muted">No</span>')
                     if (side, s) in g else '<span class="muted">n/a</span>' for s in sizes]
            rows.append([f'<a class="plain" href="/#t/{quote(key)}"><b>{e(_trade(sym, kind, side))}</b></a>'] + cells)
    lead = (f'<p class="sub">The same trade on every venue we track, at four sizes. <span class="best">Best</span> '
            f"means {name} gives you the most back. Pool fees, price impact and each venue's network fee are included. "
            f"A market with under {e(money(MIN_LIQUIDITY_USD))} of liquidity is left out. "
            f'<a href="/#execution">Compare every venue</a></p>')
    hdr = [("Trade", "l")] + [(SIZE_LABEL.get(s, money(s)), "") for s in sizes]
    shown = BEST_ROWS_SHOWN
    more = (f'<details class="bpmore"><summary>Show {len(rows) - shown} more trades</summary>'
            f'{_table(hdr, rows[shown:], "mini bp")}</details>' if len(rows) > shown + 2 else "")
    table = (f'<div class="panel">{_table(hdr, rows if not more else rows[:shown], "mini bp")}{more}</div>'
             if rows else f'<p class="sub">{name} is not the best price on any trade we compare right now.</p>')
    tail = (f'<p class="sub" style="margin:12px 0 0">Not the best price at any size right now: {e(", ".join(rest))}.</p>'
            if rest else "")
    return f"""  <section class="block">
    <h2>Where {name} is the best price</h2>
    {lead}
    {table}
    {tail}
  </section>"""


def venue_page(f: dict, head: dict, t: int, card_v: int | None = None) -> str:
    """The page. ``card_v`` versions the card URL so a re-share fetches the current image."""
    v = f["venue"]
    cv = card_v or t
    url = f"{SITE}/venues/{v['slug']}/"
    image = f"{url}card.png?v={cv}"
    title = (f"{f['name']} on Canton: live liquidity and prices | Canton Venues" if head["rule"] in PLAIN
             else f"{f['name']} on Canton: {_lower_first(head['title'])} | Canton Venues")
    scope = head_scope(f, head)
    desc = (f"{head['title']}. {head['sub']}. {scope + ' ' if scope else ''}Live {f['name']} data on Canton "
            "Network: volume, liquidity and best execution, refreshed every five minutes.")
    parts = [f"""  <p class="crumbs"><a href="/">Canton Venues</a> / <a href="/venues/">Venues</a> / {e(f['name'])}</p>
  <div class="vhead"><h1>{e(f['name'])}</h1><span class="vchip">{e(f['kind'])}</span><span class="vst {'priced' if f['status'] == 'priced' else ''}">{'Live' if f['status'] in LIVE else 'Unreachable'}</span></div>
  {lead_html(f, head)}"""]

    st = []
    if f.get("spot_volume") is not None:
        st.append(("Spot volume, 24h", money(f["spot_volume"])))
        if rank := page_rank(f["spot_rank"], f["spot_n"]):
            st.append(("Rank by spot volume", rank))
    elif "spot_rank" in f:
        st.append(("Spot volume, 24h", "Not published"))
    if f.get("spot_share"):
        st.append(("Share of spot volume", f"{f['spot_share'] * 100:.1f}%"))
    if f.get("perp_volume") is not None:
        st.append(("Perps volume, 24h", money(f["perp_volume"])))
        if rank := page_rank(f["perp_rank"], f["perp_n"]):
            st.append(("Rank by perps volume", rank))
    if f.get("open_interest") is not None:
        st.append(("Open interest", money(f["open_interest"])))
    if f.get("tvl") is not None:
        st.append((f"In {pools_text(f)}", money(f["tvl"])))
    tf = token_facts(f)
    if f.get("listed"):
        if big := largest_market(f):
            st.append((f"Largest market: {big[0]}", money(big[1])))
        st.append(("Markets traded, 24h", str(len(f["listed"]))))
    elif f.get("tokens"):
        st.append((f"Tokens with {money(MIN_LIQUIDITY_USD)}+ liquidity", str(tf["n"])))
    if f.get("deepest") and not f.get("pools") and not f.get("keyed"):
        st.append((f"Book depth within 1%, {depth_pair(f['deepest'])}", money(f["deepest"]["usd"])))
    if f.get("perp_volume") is None and f.get("perp_volume_30d"):
        st.append(("Perps volume, 30 days", money(f["perp_volume_30d"])))
    if f.get("perp_markets") and all(m["turnover_24h_usd"] is None for m in f["perp_markets"]):
        # no 24h figure published: whether a market trades is unknown, not "0 of 6"
        st.append(("Perp markets we read", str(len(f["perp_markets"]))))
    elif f.get("perp_markets"):
        live = sum(1 for m in f["perp_markets"] if (m["turnover_24h_usd"] or 0) >= MIN_PERP_TURNOVER_USD)
        st.append(("Perp markets trading", f"{live} of {len(f['perp_markets'])}"))
    parts.append('  <div class="stats">' + "".join(
        f'<div class="stat"><div class="k">{e(k)}</div><div class="v num">{e(val)}</div></div>' for k, val in st)
        + "</div>")

    # the share card right under the figures, where a visitor who came from a post sees it
    handle = (f'<a class="btn" href="https://x.com/{e(v["x"])}" target="_blank" rel="noopener">{X_ICON}@{e(v["x"])}</a>'
              if v.get("x") else "")
    parts.append(f"""  <section class="block">
    <h2>Share {e(f['name'])}</h2>
    <p class="sub">The card updates with the data. Shared on X, it shows as the preview.</p>
    <div class="vtop">
    <img class="vcard" src="card.png?v={cv}" width="1200" height="630" alt="{e(f['name'])}: {e(head['title'])}. {e(head['sub'])}.">
    <div class="acts">
      <a class="btn x" href="{e(intent_url(f, head))}" target="_blank" rel="noopener">{X_ICON}Share on X</a>
      <a class="btn" href="card.png?v={cv}" download="canton-venues-{e(v['slug'])}.png">Download card</a>
      <a class="btn" href="{e(v['site'])}" target="_blank" rel="noopener">{e(v['site'].split('//')[1])} ↗</a>
      {handle}
    </div>
  </div>
  </section>""")

    if now := now_html(f, t):
        parts.append(now)
    charts, script = charts_html(f)
    if charts:
        parts.append(charts)
    if bp := best_price_html(f):
        parts.append(bp)

    if f.get("tokens"):
        ob = any(x.get("market") for x in f["tokens"])
        hdr = [("Token", "l"), ("Price here", ""), ("Liquidity", ""), ("Within 1% of mid", "")]
        if ob:
            hdr = [("Token", "l"), ("Market", "l"), ("Price here", ""), ("Within 1% of mid", ""), ("Spread", "")]

        def token_rows(xs):
            rows = []
            for x in sorted(xs, key=lambda x: -(token_liquidity(x) or 0))[:25]:
                link = f'<a href="/#t/{quote(x["key"])}"><b>{e(x["symbol"])}</b></a>'
                if ob:
                    rows.append([link, f'<span class="muted">{e(x.get("market") or "")}</span>',
                                 price(x.get("price_usd")), money(x.get("depth_1pct_usd")),
                                 NA if x.get("spread_bps") is None else f"{x['spread_bps']:.2f} bp"])
                else:
                    rows.append([link, price(x.get("price_usd")), money(x.get("liquidity_usd")),
                                 money(x.get("depth_1pct_usd"))])
            return rows

        floor = e(money(MIN_LIQUIDITY_USD))
        what = tf["what"]
        keyed_note = (f" Prices, depth and spread here are from {e(f['name'])}'s order book via our account key, "
                      "so they cannot be checked without one." if f.get("keyed") else "")
        tables = (f'<div class="panel">{_table(hdr, token_rows(tf["rows"]))}</div>' if tf["rows"]
                  else f'<p class="sub">No token has {floor} or more {what} on {e(f["name"])} right now.</p>')
        if tf["thin"]:
            tables += (f'\n    <p class="sub" style="margin:14px 0 6px">Thin, under {floor} {what} on {e(f["name"])}: '
                       "priced, but not counted in the token figures above or on the card.</p>\n"
                       f'    <div class="panel">{_table(hdr, token_rows(tf["thin"]))}</div>')
        parts.append(f"""  <section class="block">
    <h2>Tokens</h2>
    <p class="sub">The {tf['n']} token{'' if tf['n'] == 1 else 's'} with {floor} or more {what} on {e(f['name'])} itself, largest first. Click one for every venue's price.{keyed_note}</p>
    {tables}
  </section>""")

    if f.get("pools"):
        rows = [[f"<b>{e(p['pair'])}</b>" if p.get("priced", True)
                 else f'<b>{e(p["pair"])}</b><span class="muted"> (not priced)</span>',
                 money(p["tvl_usd"]), money(p.get("volume_24h_usd")),
                 NA if p.get("fee") is None else f"{p['fee'] * 100:.2f}%",
                 NA if p.get("fee_apr") is None else f"{p['fee_apr'] * 100:.1f}%"]
                for p in f["pools"] if p["tvl_usd"] >= 100][:20]
        parts.append(f"""  <section class="block">
    <h2>Pools</h2>
    <p class="sub">Pools by liquidity: a CC pool is twice its CC reserve at our CC price, so it counts even when we cannot price its other token; a pool of two other tokens is both sides at our prices (twice the side we price). Fee APR is the last 24h of LP fees over liquidity, annualised.</p>
    <div class="panel">{_table([("Pool", "l"), ("Liquidity", ""), ("Volume (24h)", ""), ("Fee", ""), ("Fee APR", "")], rows)}</div>
  </section>""")

    if f.get("perp_markets"):
        rows = [[f"<b>{e(m['base'])}</b><span class=\"muted\"> / {e(m['quote'])}</span>",
                 price(m["mark"] or m["last"]),
                 NA if m["basis"] is None else f"{m['basis'] * 100:+.2f}%",
                 NA if m["funding_rate"] is None else f"{m['funding_rate'] * 100:+.4f}%",
                 NA if m["spread_bps"] is None else f"{m['spread_bps']:.2f} bp",
                 money(m["open_interest_usd"]), money(m["turnover_24h_usd"])] for m in f["perp_markets"]]
        parts.append(f"""  <section class="block">
    <h2>Perpetuals</h2>
    <p class="sub">Basis is the perp price against the outside spot price.</p>
    <div class="panel">{_table([("Market", "l"), ("Price", ""), ("Basis", ""), ("Funding", ""), ("Spread", ""), ("Open interest", ""), ("Volume (24h)", "")], rows)}</div>
  </section>""")


    method = f["method"] + [x for x in card_method(f, head) if x not in f["method"]]
    notes = "".join(f"<li>{e(n)}</li>" for n in method)
    # the small print: the headline's scope, and the one source credit for charts that are not ours
    small = [scope] if scope else []
    if any(s.get("source") == "defillama" for s in f.get("series") or []):
        small.append(HISTORY_CREDIT)
    fine = "".join(f'<p class="fine">{e(x)}</p>' for x in small)
    parts.append(f"""  <section class="block">
    <h2>How we track {e(f['name'])}</h2>
    <ul class="notes">{notes}</ul>
    {fine}
    <p class="sub" style="margin-top:12px">Something wrong or missing? <a href="/#contact">Write to us</a>.</p>
  </section>""")
    return _shell(title, desc, url, image, "\n".join(parts), t, script)


TYPE_SHORT = {"Spot order book": "Order book", "Spot AMM": "AMM", "Perpetuals": "Perps",
              "Spot order book + Perpetuals": "Order book + perps"}


def index_page(all_facts: dict, heads: dict, t: int, card_v: dict | None = None) -> str:
    """Every venue we track in one table, the dashboard's way: name, type, its best fact, volume,
    liquidity, open interest and a 30-day volume line where a daily record exists; then the venues
    that trade on Canton but publish nothing we can read yet (``COMING``)."""
    def vol(f):  # the figure the Volume column shows: spot, or perps for a perps-only venue
        return f.get("spot_volume") if f.get("spot_volume") is not None else (f.get("perp_volume") or 0)

    order = sorted(all_facts, key=lambda s: -vol(all_facts[s]))
    rows = []
    for n, s in enumerate(order, 1):
        f, h = all_facts[s], heads[s]
        # the lead shows once at any width: in its own column on a wide screen, under the name on a
        # phone (the column is hidden there, the tagline everywhere else)
        # a plain card has nothing it leads at: its pool count, muted, rather than a blank cell
        if h["rule"] in PLAIN:
            lead = f'<span class="muted">{e(pools_text(f))}</span>' if f.get("pools") else ""
        elif h["rule"] == "best_count" and h.get("short"):
            lead = '<b class="up">#1</b> ' + e(h["short"].replace("Best price on ", "Best price on most ", 1))
        else:
            lead = ('<b class="up">#1</b> ' if is_win(h) else "") + e(tag(h["title"]))
        name = (f'<a class="plain" href="{e(s)}/"><b>{e(f["name"])}</b></a>'
                + (f'<div class="tagline">{lead}</div>' if lead else ""))
        if f.get("spot_volume") is not None:
            v24 = money(f["spot_volume"])
        elif f.get("perp_volume") is not None:  # perps-only: the Perps column already shows it
            v24 = ""
        else:
            v24 = '<span class="muted">not published</span>'
        # blank where the venue has no such thing (no perps, no open interest); n/a only where it has
        # it and we cannot show it
        perps = money(f["perp_volume"]) if f.get("perp_volume") is not None else ""
        if f.get("tvl") is not None:
            liq = money(f["tvl"])
        elif f.get("deepest") and f["deepest"].get("usd") and not f.get("keyed"):
            # an order book's resting dollars are not pool liquidity: labelled, never bare
            n = f["deepest"].get("books") or 1
            liq = (f'{money(f["deepest"]["usd"])}<div class="muted depthnote">book depth within 1%'
                   f'{f", {n} books" if n > 1 else ""}</div>')
        elif f.get("perp_depth"):
            liq = f'{money(f["perp_depth"]["usd"])}<div class="muted depthnote">perps book depth within 1%</div>'
        else:
            liq = '<span class="muted">n/a</span>'
        oi = (money(f["open_interest"]) if f.get("open_interest") else
              '<span class="muted">n/a</span>' if f.get("perp_id") else "")
        daily = next((x for x in f.get("series") or [] if x["kind"] == "daily"), None)
        spark = spark_svg([p[1] for p in daily["pts"][-30:]] if daily else [])
        rows.append(f'<tr class="click" data-href="{e(s)}/"><td class="l rank num">{n}</td><td class="l">{name}</td>'
                    f'<td class="l hide-sm"><span class="vchip">{e(TYPE_SHORT.get(f["kind"], f["kind"]))}</span></td>'
                    f'<td class="l lead hide-sm">{lead}</td><td class="num">{v24}</td>'
                    f'<td class="num hide-sm">{perps}</td><td class="num hide-sm">{liq}</td>'
                    f'<td class="num hide-sm">{oi}</td><td class="hide-sm" style="width:150px">{spark}</td></tr>')
    n = len(all_facts)
    checked = (" A lead that says on Canton covers every Canton DEX with public market data."
               if any(heads[s].get("scope") == "canton" for s in order) else "")
    # the source credit is small print under the table, never in the paragraph's body copy
    credit = (f'\n  <p class="fine">{e(HISTORY_CREDIT[:-1])}, or from the venue\'s own record.</p>'
              if any(x.get("source") == "defillama" for f in all_facts.values() for x in f.get("series") or [])
              else "")
    coming = ", ".join(COMING)
    body = f"""  <p class="crumbs"><a href="/">Canton Venues</a> / Venues</p>
  <div class="title" style="padding-top:4px"><h1>Canton venues</h1><p>Every Canton DEX with public market data, live. Sorted by 24h volume; click one for its page, its history and a card to share.</p></div>
  <div class="tablewrap"><table class="vt"><thead><tr><th class="l rank">#</th><th class="l">Venue</th><th class="l hide-sm">Type</th><th class="l hide-sm">Strongest fact</th><th>Volume (24h)</th><th class="hide-sm">Perps (24h)</th><th class="hide-sm">Liquidity</th><th class="hide-sm">Open interest</th><th class="hide-sm">Daily volume, 30 days</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>
  <p class="sub" style="margin-top:14px">Strongest fact: a ranking the venue tops by 10% or more, or the trades it prices best after network fees (most of those compared at one size); where it leads nothing, its pools. Markets under {e(money(MIN_LIQUIDITY_USD))} set aside. {e(scope_line(n))}{e(checked)} Volume is spot where the venue has a spot market. Liquidity is pool liquidity; where it says book depth, it is the dollars resting within 1% of mid on the venue's deepest public order book, or on all of one token's books where it says how many (for perps, among markets trading {e(money(MIN_PERP_TURNOVER_USD))}+ a day). A blank cell: the venue has no such market; n/a: it has one we cannot show.</p>{credit}
  <section class="block">
    <h2>Coming to Canton Venues</h2>
    <p class="sub">Trading on Canton, no public market data yet: {e(coming)}. Run one of these? <a href="/#contact">Get in touch</a>.</p>
  </section>"""
    first = order[0]
    script = """document.querySelectorAll("tr[data-href]").forEach((r) => r.addEventListener("click", (ev) => { if (!ev.target.closest("a")) location.href = r.dataset.href; }));"""
    return _shell("Canton venues: every Canton DEX with public market data | Canton Venues",
                  f"Live pages for the {n} Canton DEXs with public market data: "
                  + ", ".join(all_facts[s]["name"] for s in order)
                  + ". Volume, liquidity and best execution, refreshed every five minutes.",
                  f"{SITE}/venues/", f"{SITE}/venues/{first}/card.png?v={(card_v or {}).get(first, t)}", body, t, script)


# === build =================================================================

def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def sitemap(all_facts: dict, t: int) -> str:
    """The dashboard, the venues index, a page per live venue, the weekly card and the build page, for
    crawlers."""
    day = datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d")
    urls = ([f"{SITE}/", f"{SITE}/venues/"] + [f"{SITE}/venues/{s}/" for s in all_facts]
            + [f"{SITE}/weekly/", f"{SITE}/build/"])
    rows = "".join(f"  <url><loc>{u}</loc><lastmod>{day}</lastmod></url>\n" for u in urls)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{rows}</urlset>\n'


def build(out: Path, data: dict, t: int, cards: bool = True, look: str = "venue", guard=None,
          fresh: dict[str, int] | None = None) -> dict:
    """Write ``out/venues/<slug>/index.html`` (+ ``card.png``) and ``out/venues/index.html``.

    Pages are cheap and follow every tick; cards are drawn when ``cards`` is set (the collector
    does it every few ticks) or when a venue has none yet. A venue that is unreachable this tick
    keeps its last page and card. ``look`` picks the card's style (``LOOKS``).

    ``guard`` (publish_guard.PublishGuard) holds a venue on its last good card and page while its
    figures jump implausibly or its data is stale; ``fresh`` is each row id's last successful read
    (unix time), from the collector. Returns the headlines written this time.
    """
    from publish_guard import card_figures

    all_facts = {s: f for s, f in facts(data).items() if f["status"] in LIVE}
    heads = {s: headline(f) for s, f in all_facts.items()}
    shown = dict(heads)
    card_v = {}
    for s, f in list(all_facts.items()):
        try:  # one venue's page or card that will not build is skipped; the others are written
            d = out / "venues" / s
            card = d / "card.png"
            if guard is not None:
                figs = card_figures(f, heads[s], stats(f))
                seen = [fresh[i] for i in f["venue"]["rows"] if fresh and i in fresh]
                age = t - min(seen) if seen else None
                why = guard.check(s, figs, t, age)
                if why and (d / "index.html").exists():
                    log.warning("venue %s held on its last good card: %s", s, why)
                    del heads[s]
                    shown[s] = guard.last_head(s) or shown[s]
                    card_v[s] = int(card.stat().st_mtime) if card.exists() else t
                    # the venues table shows the held venue's last published figures, never the doubtful ones
                    kept = (guard.state["venues"].get(s) or {}).get("figures") or {}
                    held = {k: kept.get(k) for k in ("spot_volume", "perp_volume", "tvl", "open_interest")}
                    if "depth" in kept and f.get("deepest"):
                        held["deepest"] = {**f["deepest"], "usd": kept["depth"]}
                    all_facts[s] = {**f, **held, "series": f.get("series")}
                    continue
            if cards or not card.exists():
                render_card(f, heads[s], t, card, look=look)
                card_v[s] = t
            else:
                card_v[s] = int(card.stat().st_mtime)
            _write(d / "index.html", venue_page(f, heads[s], t, card_v[s]))
            if guard is not None:
                guard.accept(s, figs, heads[s], t)
        except Exception:  # noqa: BLE001
            log.exception("venue %s: page not written", s)
            all_facts.pop(s, None)
    if all_facts:
        _write(out / "venues" / "index.html", index_page(all_facts, shown, t, card_v))
        _write(out / "sitemap.xml", sitemap(all_facts, t))
    if guard is not None:
        guard.save()
    return heads


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", type=Path, required=True, help="directory holding venues.json and the rest")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--look", choices=LOOKS, default="venue",
                    help="venue: each card in its venue's own style; ours: every card in the dashboard's look")
    args = ap.parse_args()
    data = {n: json.loads((args.api / f"{n}.json").read_text())
            for n in ("venues", "tokens", "execution", "lp", "perps")}
    if (args.api / "summary.json").exists():  # CC price and DefiLlama's Canton list
        data["summary"] = json.loads((args.api / "summary.json").read_text())
    if (args.api / "venue_history.json").exists():  # the charts (venue_history.py)
        data["history"] = json.loads((args.api / "venue_history.json").read_text())
    heads = build(args.out, data, data["venues"]["t"], look=args.look)
    all_f = facts(data)
    near = {s: f["near"] for s, f in all_f.items()}
    for s, h in heads.items():
        margin = ("no lead" if h["rule"] in PLAIN else "the only venue with it" if h.get("margin") is None else
                  f"net +{h['margin'] * 10_000:.0f} bp over {h['next']} after network fees"
                  if h["rule"] == "best_quote" else f"+{h['margin'] * 100:.0f}% over {h['next']}")
        print(f"{s:12} {h['rule']:13} {h['title']} | {h['sub']} | {margin} | scope {h.get('scope', '-')}")
        for x in all_f[s]["leads"][1:]:
            m = ("only venue" if x["margin"] is None else f"net +{x['margin'] * 10_000:.0f} bp" if x["rule"] == "best_quote"
                 else f"+{x['margin'] * 100:.0f}% over {x['next']}")
            print(f"{'':12} also: {x['rule']}: {x['title']} | {x['sub']} | {m}")
        for x in near.get(s, []):
            print(f"{'':12} not a lead: {x['rule']} only +{x['margin'] * 100:.1f}% over {x['next']}")


if __name__ == "__main__":
    main()
