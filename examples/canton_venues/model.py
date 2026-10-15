"""Pure computations behind Canton Venues: no I/O, everything testable offline.

Every venue pool is reduced to one shape, ``VenuePool``: a CC-paired AMM that
can price a swap in either direction. Prices, premiums, the execution ladder,
the spread scan and the paper desk are all derived from those.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

CC = "CC"
SIZES_USD = (100, 1_000, 10_000, 50_000)
ROUTER_SIZE_USD = 1_000
MIN_ROUTE_USD = Decimal("0.50")

# Assets with an independent market outside Canton, and what they track. A CoinGecko id, or
# "spot:XAU"/"spot:XAG" for a metal's spot price, which is Ekiden's index for its XAU/XAG perps
# (collect.py): on 2026-10-14 it sat inside Swissquote's interbank bid/ask while PAXG ran +0.25% and
# KAG +1.9% over spot, so a "premium" against them was the token's own premium (eXAG -1.8% shown,
# +0.03% against spot). Stables are measured against the 1.00 peg instead.
REFERENCES = {
    "CC": ("canton-network", "global CC markets"),
    "CETH": ("ethereum", "ETH"),
    "CBTC": ("bitcoin", "BTC"),
    "EXAU": ("spot:XAU", "gold spot"),
    "EXAG": ("spot:XAG", "silver spot"),
}
# Tokens that track an outside asset we have no spot price for: the only keyless prices are tokenized
# shares that carry their own premium (SPYx +0.59% and QQQx +0.34% over SPY and QQQ in market hours,
# 2026-10-14), so no premium is shown for these until a spot source is connected.
NO_SPOT = {"SPYE": "SPY", "QQQE": "QQQ"}
STABLES = {"USDCX", "USDC.B", "FRXUSD.B", "USX", "TF-USDT", "USDXLR", "USDM1"}
# A token that traded less than this in 24h raises no alert and leaves the pegs post: its pool barely
# moves, so its dollar price only follows CC's (USDM1, $130 traded in a $102K pool, read +1.03% "off its
# peg" on 2026-10-14 16:47 as Tradecraft's CC/USDCx pool moved)
MIN_TRADED_USD = 1_000


def idle(tokens: list[dict]) -> set[str]:
    """Tokens that did not trade ``MIN_TRADED_USD`` in 24h on venues that publish volume."""
    return {t["key"] for t in tokens if t.get("traded_24h") is False
            or (t.get("traded_24h") and t.get("volume_24h_usd") is not None and t["volume_24h_usd"] < MIN_TRADED_USD)}
# A reference this far off is a unit mismatch (an ounce against a gram, say),
# not a premium; it is reported as unmatched rather than shown as a number.
MAX_PLAUSIBLE_PREMIUM = Decimal("0.25")
# An outside price older than this is not a reference (CoinGecko's last_updated_at)
REFERENCE_MAX_AGE_S = 900
_NEW_YORK = ZoneInfo("America/New_York")


def metals_open(t: int) -> bool:
    """Whether spot gold and silver trade at unix time ``t``: Sunday 18:00 to Friday 17:00 New York
    time, less the daily hour from 17:00 (Pyth's XAU/XAG schedule, read 2026-10-14). Holidays are not
    modelled. Outside it the spot price stands at the last close while Canton keeps trading."""
    d = datetime.fromtimestamp(t, UTC).astimezone(_NEW_YORK)
    if d.weekday() == 5:
        return False
    if d.weekday() == 6:
        return d.hour >= 18
    if d.weekday() == 4:
        return d.hour < 17
    return d.hour != 17


def key(symbol: str) -> str:
    return symbol.upper()


@dataclass(frozen=True)
class VenuePool:
    """A CC/token AMM pool on one venue."""

    venue: str
    token: str  # display symbol of the non-CC side
    cc_reserve: Decimal
    token_reserve: Decimal
    fee: Decimal  # what a swap pays, as a fraction (for display and APR)
    lp_share: Decimal  # part of the fee that goes to liquidity providers
    _out: Callable[[bool, Decimal], Decimal] = field(repr=False, compare=False)
    # how ``_out`` prices, so a pool can be saved and rebuilt (see pool_to_json)
    formula: str = ""        # "cantex": fee on input; "tradecraft": half the fee on each leg
    fee_param: Decimal = Decimal(0)

    @property
    def mid(self) -> Decimal:
        """Token per CC, before fees."""
        return self.token_reserve / self.cc_reserve

    def out(self, sell_cc: bool, amount: Decimal) -> Decimal:
        return self._out(sell_cc, amount)


def blended_mid(pools: list[VenuePool]) -> Decimal:
    """Token per CC across venues, weighted by each pool's CC depth."""
    depth = sum(p.cc_reserve for p in pools)
    return sum(p.mid * p.cc_reserve for p in pools) / depth


def cc_usd(stable_pools: list[VenuePool], usdcx_usd: Decimal) -> Decimal:
    """The pools' CC/USDCx mid, blended by CC reserve, times USDCx's own dollar price."""
    return blended_mid(stable_pools) * usdcx_usd


def cc_price_usd(markets: dict, usdcx_usd: Decimal) -> Decimal:
    """CC in dollars across every CC/USDCx market, pools and order books (``CCBook``) alike, each
    weighted by the dollars it can trade within 1% of its own mid: the rule every token's price uses.

    Until 2026-10-14 CC was the pools' blend by CC reserve, which left out Temple's book: ~$2M within
    1% against ~$7K in all four pools, so the pools' thin quote set the price (0.17-0.51% above
    Temple's mid) and the sign of the CC premium.
    """
    quotes = []
    for p in markets.values():
        px = p.mid * usdcx_usd
        quotes.append((px, p.depth_usd() if isinstance(p, CCBook) else amm_depth_usd(p, px)))
    pools = [p for p in markets.values() if not isinstance(p, CCBook)]
    return weighted_usd(quotes) or cc_usd(pools, usdcx_usd)


def token_usd(pools: list[VenuePool], cc_in_usd: Decimal) -> Decimal:
    return cc_in_usd / blended_mid(pools)


# === order-book venues =====================================================

DEPTH_BAND = Decimal("0.01")  # depth is what trades within 1% of a venue's own mid


def book_depth_usd(bids, asks, mid: Decimal, quote_usd: Decimal, band: Decimal = DEPTH_BAND) -> Decimal:
    """Dollars resting within ``band`` of mid on both sides of an order book."""
    lo, hi = mid * (1 - band), mid * (1 + band)
    total = sum((lv.price * lv.size for lv in bids if lv.price >= lo), Decimal(0))
    total += sum((lv.price * lv.size for lv in asks if lv.price <= hi), Decimal(0))
    return total * quote_usd


def amm_depth_usd(pool: VenuePool, cc_in_usd: Decimal, band: Decimal = DEPTH_BAND) -> Decimal:
    """The same measure for a constant-product pool, so AMMs and books weigh alike.

    Moving an x*y=k price by ``band`` takes about R*(sqrt(1+band)-1) of a side worth R, each way.
    """
    side_usd = pool.cc_reserve * cc_in_usd
    return 2 * side_usd * (Decimal(str(math.sqrt(1 + float(band)))) - 1)


def weighted_usd(quotes: list[tuple[Decimal, Decimal]]) -> Decimal | None:
    """One price across venues from (price, depth) pairs, weighted by depth within the band."""
    depth = sum(d for _, d in quotes)
    if depth <= 0:
        return None
    return sum(p * d for p, d in quotes) / depth


def premium(canton_usd: Decimal, reference_usd: Decimal) -> Decimal | None:
    """Canton price over the outside price, as a fraction. None if implausible."""
    if reference_usd <= 0:
        return None
    p = canton_usd / reference_usd - 1
    return p if abs(p) <= MAX_PLAUSIBLE_PREMIUM else None


def edge_bps(a: Decimal, b: Decimal) -> float:
    return float((a / b - 1) * 10_000) if b > 0 else 0.0


# === execution ladder ======================================================


def runner_up_bps(outs: dict) -> float:
    """How much the best venue beats the next best, in bps. Against the worst it would only
    measure how thin the smallest pool is."""
    top = sorted(outs.values(), reverse=True)[:2]
    return edge_bps(top[0], top[1]) if len(top) == 2 else 0.0


def net_of_fees(outs: dict, fee_usd: dict, out_price_usd: Decimal) -> dict:
    """What each venue returns after its network fee: ``fee_usd`` in dollars, taken off in the output
    token at ``out_price_usd`` per unit. ``out_net``, ``best_net`` and ``edge_net_bps`` for a row."""
    net = {v: o - fee_usd[v] / out_price_usd for v, o in outs.items() if o is not None and o > 0}
    net = {v: n for v, n in net.items() if n > 0}
    return {"fee_usd": {v: float(f) for v, f in fee_usd.items()},
            "out_net": {v: float(n) for v, n in net.items()},
            "best_net": max(net, key=net.get) if net else None,
            "edge_net_bps": runner_up_bps(net) if net else 0.0}


def ladder(pools: dict[str, VenuePool], cc_in_usd: Decimal, mid: Decimal) -> list[dict]:
    """What each venue returns at each size, both directions.

    ``mid`` (token per CC, at market prices) converts dollar sizes into token amounts, so every venue is
    asked about exactly the same trade, and is what ``cost_bps`` measures against. A venue given as a ``Route`` takes its best
    path after its network fee per swap; ``swaps`` and ``via`` say which.
    """
    rows = []
    for side in ("sell", "buy"):
        for size in SIZES_USD:
            cc_amount = Decimal(size) / cc_in_usd
            amount = cc_amount if side == "sell" else cc_amount * mid
            # what one unit received is worth: the token on a sell (``mid`` per CC), CC on a buy
            out_usd = cc_in_usd / mid if side == "sell" else cc_in_usd
            picks = {v: _pick(p, side == "sell", amount,
                              lambda path, v=v: path_fee_usd(v, path, cc_in_usd) / out_usd)
                     for v, p in pools.items()}
            outs = {v: o for v, (o, _) in picks.items()}
            filled = {v: o for v, o in outs.items() if o > 0}  # a book that runs out returns 0
            best = max(filled, key=filled.get)
            swaps = {v: len(picks[v][1]) for v in filled}
            fair = amount * mid if side == "sell" else amount / mid  # at the market mid, before fees
            rows.append({
                "side": side,  # sell = sell CC for the token, buy = buy CC with it
                "size_usd": size,
                "amount_in": float(amount),
                "out": {v: (float(o) if o > 0 else None) for v, o in outs.items()},
                "cost_bps": {v: float((1 - o / fair) * 10_000) for v, o in filled.items()},
                "best": best,
                "edge_bps": runner_up_bps(filled),
                "swaps": swaps,
                "via": {v: via(picks[v][1]) for v in filled if swaps[v] == 2},
                **net_of_fees(filled, {v: path_fee_usd(v, picks[v][1], cc_in_usd) for v in filled}, out_usd),
            })
    return rows


def crossover_usd(rows: list[dict], side: str, a: str, b: str) -> list[float]:
    """Dollar sizes where the better of venues a and b flips (log-interpolated)."""
    key = lambda r: r.get("out_net") or r["out"]  # noqa: E731  (after network fees when known)
    pts = [r for r in rows if r["side"] == side and a in key(r) and b in key(r)]
    out = []
    for r0, r1 in zip(pts, pts[1:]):
        e0 = edge_bps(Decimal(str(key(r0)[a])), Decimal(str(key(r0)[b])))
        e1 = edge_bps(Decimal(str(key(r1)[a])), Decimal(str(key(r1)[b])))
        if e0 == 0 or e1 == 0 or (e0 > 0) == (e1 > 0):
            continue
        l0, l1 = math.log(r0["size_usd"]), math.log(r1["size_usd"])
        out.append(math.exp(l0 + (l1 - l0) * e0 / (e0 - e1)))
    return out


# === spread scan ===========================================================


def round_trip(buy_on: VenuePool, sell_on: VenuePool, cc_in: Decimal) -> Decimal:
    """CC back after buying the token on one venue and selling it on the other."""
    return sell_on.out(False, buy_on.out(True, cc_in))


def best_round_trip(buy_on: VenuePool, sell_on: VenuePool, cc_in_usd: Decimal,
                    lo_usd: float = 10, hi_usd: float = 100_000, steps: int = 60) -> tuple[Decimal, Decimal]:
    """The input (CC) that maximises the round trip's gross gain, and that gain.

    The gain is concave in the input for two constant-product pools, so a log
    grid followed by a golden-section refine finds the maximum.
    """
    def gain(x: Decimal) -> Decimal:
        return round_trip(buy_on, sell_on, x) - x

    grid = [Decimal(lo_usd * (hi_usd / lo_usd) ** (i / (steps - 1))) / cc_in_usd for i in range(steps)]
    gains = [gain(x) for x in grid]
    i = max(range(steps), key=lambda k: gains[k])
    a, b = grid[max(i - 1, 0)], grid[min(i + 1, steps - 1)]
    phi = Decimal((math.sqrt(5) - 1) / 2)
    for _ in range(40):
        c, d = b - phi * (b - a), a + phi * (b - a)
        if gain(c) > gain(d):
            b = d
        else:
            a = c
    x = (a + b) / 2
    return x, gain(x)


def scan(books: dict[str, dict[str, VenuePool]], cc_in_usd: Decimal) -> list[dict]:
    """Every token on two or more venues, both directions, at its best size."""
    found = []
    for token, pools in books.items():
        venues = sorted(pools)
        for buy in venues:
            for sell in venues:
                if buy == sell:
                    continue
                x, g = best_round_trip(pools[buy], pools[sell], cc_in_usd)
                cost_cc = (swap_cost_cc(buy, cc_in_usd, pool_hop(pools[buy]))
                           + swap_cost_cc(sell, cc_in_usd, pool_hop(pools[sell])))
                net_cc = g - cost_cc
                found.append({
                    "token": token,
                    "buy_on": buy,
                    "sell_on": sell,
                    "size_cc": float(x),
                    "size_usd": float(x * cc_in_usd),
                    "gross_cc": float(g),
                    "net_cc": float(net_cc),
                    "net_usd": float(net_cc * cc_in_usd),
                    "cost_cc": float(cost_cc),
                    "clears": net_cc * cc_in_usd >= MIN_ROUTE_USD,
                })
    return sorted(found, key=lambda r: -r["net_usd"])


# === order books against pools, in dollars ================================

# The network fee assumed for a venue that publishes none (``NETWORK_FEE`` basis "assumed").
SWAP_COST_CC = Decimal("1.5")
# The network fee a trader pays per trade on each venue, on top of its trading or pool fee: the one
# table every figure reads (best execution, the scanner, the paper desk, venue headlines). Each entry
# is (unit, point, low, high, basis): "usd" or "cc"; ``point`` is the best estimate every net figure
# uses, ``low``/``high`` the range a best-price claim must survive; basis "measured", "documented" or
# "assumed". Only a measured or documented fee can carry a best-price claim; an assumed one fills the
# net figures and is shown as an assumption. Replace an entry when it is measured from our own trades.
NETWORK_FEE: dict[str, tuple[str, Decimal, Decimal, Decimal, str]] = {
    # Cantex's own quotes on all 24 CC pairs, 2026-10-15, three reads from 11:20 to 12:44 UTC: 0.88 CC
    # (HANDL, HECTO, MOD, TRKX*) to 3.49 CC (CC/USDCx), the same at any size and both ways, moving 25%
    # within an hour (CC/USDCx 3.42, 3.49, 3.13; 0.86 flat, measured 03.10, is out of date). Each pair's
    # own fee comes from quotecheck.py every hour (``PAIR_FEE_CC``); this is the stand-in when that
    # record is stale, with headroom around what was seen. A best-price claim must survive the range.
    "cantex": ("cc", Decimal("1.36"), Decimal("0.85"), Decimal("3.60"), "measured"),
    # docs.oneswap.cc: "typically around $1.5-2 at recent network prices"
    "oneswap": ("usd", Decimal("1.75"), Decimal("1.5"), Decimal("2.0"), "documented"),
    # docs.tradecraft.fi/fees-and-pricing "Gas: $0.10" (illustrative); high end: its DAR guide sizes an
    # immediate swap at ~23 kB, $1.38 at MainNet's 60 USD/MB with no free burst left. Checked 2026-10-08.
    "tradecraft": ("usd", Decimal("0.10"), Decimal("0.10"), Decimal("1.40"), "documented"),
    # Temple and Rocky trade from a deposited balance and settle on the trader's behalf (Rocky in
    # 5-second batches); neither states a per-trade network cost (checked 2026-10-08). Pool Party shows
    # a per-swap "Network fee" in its app, only to a signed-in wallet. All three: the default assumption.
    "temple": ("cc", SWAP_COST_CC, SWAP_COST_CC, SWAP_COST_CC, "assumed"),
    "rocky": ("cc", SWAP_COST_CC, SWAP_COST_CC, SWAP_COST_CC, "assumed"),
    "poolparty": ("cc", SWAP_COST_CC, SWAP_COST_CC, SWAP_COST_CC, "assumed"),
}


_ASSUMED = ("cc", SWAP_COST_CC, SWAP_COST_CC, SWAP_COST_CC, "assumed")


def network_fee_usd(venue: str, cc_in_usd: Decimal, end: str = "point") -> Decimal:
    """One trade's network fee on ``venue`` in dollars: its best estimate, or the low or high end."""
    unit, point, lo, hi, _ = NETWORK_FEE.get(venue, _ASSUMED)
    amount = {"low": lo, "high": hi}.get(end, point)
    return amount * cc_in_usd if unit == "cc" else amount


def fee_known(venue: str) -> bool:
    return venue in NETWORK_FEE and NETWORK_FEE[venue][4] != "assumed"


def fee_table_json() -> dict:
    out = {v: {"unit": u, "point": float(pt), "low": float(lo), "high": float(hi), "basis": b}
           for v, (u, pt, lo, hi, b) in NETWORK_FEE.items()}
    for (v, pair), fee in sorted(PAIR_FEE_CC.items(), key=lambda x: (x[0][0], sorted(x[0][1]))):
        a, b = sorted(pair, key=lambda k: (k != CC, k))  # CC first
        out.setdefault(v, {}).setdefault("pairs_cc", {})[f"{a}/{b}"] = float(fee)
    return out


def swap_cost_cc(venue: str, cc_in_usd: Decimal, hop=None) -> Decimal:
    """Network cost of one swap on ``venue``, in CC (its best estimate; ``hop``'s pair's own where the
    venue's quotes give one, ``PAIR_FEE_CC``)."""
    return swap_fee_usd(venue, cc_in_usd, hop) / cc_in_usd


# The network fee of one swap on one pair, in CC, as the venue's own quotes give it (quotecheck.py):
# Cantex names it per pair, the same at any size and both ways (2026-10-15: CC/USDCx 3.42 CC, CC/USDC.B
# 1.32, CC/CBTC 1.28, CC/HECTO 1.12; the flat 0.86 measured 03.10 understated CC/USDCx 4x). The
# collector fills it from the check's record while that is fresh; a pair without one takes the venue's
# point in ``NETWORK_FEE``. Keyed (venue, frozenset of the two token keys).
PAIR_FEE_CC: dict[tuple[str, frozenset], Decimal] = {}


def swap_fee_usd(venue: str, cc_in_usd: Decimal, hop=None) -> Decimal:
    """One swap's network fee on ``venue`` in dollars: its pair's measured fee, else the venue's point."""
    if hop is not None and (fee := PAIR_FEE_CC.get((venue, frozenset((hop.a, hop.b))))) is not None:
        return fee * cc_in_usd
    return network_fee_usd(venue, cc_in_usd)


def path_fee_usd(venue: str, path: tuple, cc_in_usd: Decimal) -> Decimal:
    """A path's network fees in dollars, each swap its pair's (a hop of None: the venue's point)."""
    return sum((swap_fee_usd(venue, cc_in_usd, h) for h in path), Decimal(0))
# Rocky publishes no fee schedule; its homepage example charges 0.025% per order and says the app
# is the source of truth. An assumption until the app or their docs say otherwise.
BOOK_TAKER_FEE = {"rocky": Decimal("0.00025"),
                  # Temple: 1 bp taker, 0.5 bp maker (help center, "Fees & Rebates", read 2026-10-04)
                  "temple": Decimal("0.0001")}


@dataclass(frozen=True)
class BookVenue:
    """One order book: base priced in a dollar token worth ``quote_usd``."""

    venue: str
    symbol: str
    bids: tuple  # (price, size) descending
    asks: tuple  # (price, size) ascending
    quote_usd: Decimal
    fee: Decimal

    def sell_base(self, qty: Decimal) -> Decimal | None:
        """Quote received for ``qty`` base, walking the bids; None if the book runs out."""
        got, left = Decimal(0), qty
        for price, size in self.bids:
            take = min(left, size)
            got += take * price
            left -= take
            if left <= 0:
                return got * (1 - self.fee)
        return None

    def buy_base(self, quote: Decimal) -> Decimal | None:
        """Base received for ``quote`` spent, walking the asks; None if the book runs out."""
        got, left = Decimal(0), quote
        for price, size in self.asks:
            take = min(left, size * price)
            got += take / price
            left -= take
            if left <= 0:
                return got * (1 - self.fee)
        return None

    def fingerprint(self) -> str:
        top = (self.bids[:1] + self.asks[:1])
        return f"{self.venue}:" + ":".join(f"{p}x{q}" for p, q in top)


@dataclass(frozen=True)
class CCBook:
    """An order book for CC itself (Temple's CC/USDCx), asked like a pool so it joins every CC
    comparison: best execution, the scanner and the paper router.

    It has no reserves: ``cc_reserve`` is 0, so it weighs nothing in ``blended_mid`` (the mid a ladder
    converts sizes with) and never enters the pool tables. It does weigh in the CC price, by its
    depth (``cc_price_usd``). ``out`` walks the book after its taker fee; a book that runs out
    returns 0, which every comparison reads as "cannot fill".
    """

    venue: str
    token: str  # display symbol of the quote side, as a pool's non-CC side
    book: BookVenue
    cc_reserve: Decimal = Decimal(0)

    @property
    def mid(self) -> Decimal:
        """Quote token per CC: halfway between the best bid and the best ask."""
        return (self.book.bids[0][0] + self.book.asks[0][0]) / 2

    def depth_usd(self, band: Decimal = DEPTH_BAND) -> Decimal:
        """Dollars resting within ``band`` of mid on both sides, as ``book_depth_usd``."""
        lo, hi = self.mid * (1 - band), self.mid * (1 + band)
        total = sum((p * q for p, q in self.book.bids if p >= lo), Decimal(0))
        total += sum((p * q for p, q in self.book.asks if p <= hi), Decimal(0))
        return total * self.book.quote_usd

    def out(self, sell_cc: bool, amount: Decimal) -> Decimal:
        got = self.book.sell_base(amount) if sell_cc else self.book.buy_base(amount)
        return got if got is not None else Decimal(0)


def book_to_json(b: BookVenue, quote_key: str) -> dict:
    return {"venue": b.venue, "symbol": b.symbol, "quote": quote_key, "quote_usd": float(b.quote_usd),
            "fee": float(b.fee), "bids": [[str(p), str(q)] for p, q in b.bids],
            "asks": [[str(p), str(q)] for p, q in b.asks]}


def book_from_json(d: dict) -> BookVenue:
    return BookVenue(d["venue"], d["symbol"], tuple((Decimal(p), Decimal(q)) for p, q in d["bids"]),
                     tuple((Decimal(p), Decimal(q)) for p, q in d["asks"]),
                     Decimal(str(d["quote_usd"])), Decimal(str(d["fee"])))


# === the best route inside each venue ======================================
# Every venue is asked the same thing: its best way, on its own markets, from one token to another in
# one or two swaps (F 2026-10-14: the same rule for every venue). A pool venue may go through a second
# pool of its own (Pool Party: CC to USDC.B to CBTC), an order-book venue places two orders (Temple: CC
# to USDCx to CBTC), and each swap pays the venue's network fee. Before 2026-10-14 a CC trade was one
# CC pool per venue, and a dollar trade took its CC/USDCx leg on whichever venue was best.


@dataclass(frozen=True)
class Hop:
    """One market on one venue between two tokens (keys): a pool, with CC or without, or an order
    book. ``swap(a_to_b, amount)`` is what comes out, 0 when the market cannot fill it."""

    venue: str
    a: str
    b: str
    mid: Decimal  # b per a, before fees
    swap: Callable[[bool, Decimal], Decimal] = field(repr=False, compare=False)
    market: str = ""  # a book's market name ("CBTC/USDCx"); "" for a pool
    fee: Decimal = Decimal(0)  # a book's taker fee, for the page
    usd: Decimal | None = None  # a pool's liquidity in dollars; None for a book or when unknown

    def to(self, frm: str) -> str:
        return self.b if frm == self.a else self.a

    def out(self, frm: str, amount: Decimal) -> Decimal:
        return self.swap(frm == self.a, amount) if amount > 0 else Decimal(0)

    def rate(self, frm: str) -> Decimal:
        return self.mid if frm == self.a else 1 / self.mid

    def fingerprint(self) -> str:
        return f"{self.venue}:{self.a}/{self.b}:{self.mid:.12g}"


def pool_hop(p: VenuePool) -> Hop:
    """A CC pool (or Temple's CC book, ``CCBook``) as a hop from CC to its token."""
    return Hop(p.venue, CC, key(p.token), p.mid, p.out,
               *((p.book.symbol, p.book.fee) if isinstance(p, CCBook) else ()))


def book_hop(b: BookVenue, base: str, quote: str) -> Hop:
    """An order book as a hop from its base to its quote token (keys)."""
    def swap(sell_base: bool, amount: Decimal) -> Decimal:
        got = b.sell_base(amount) if sell_base else b.buy_base(amount)
        return got if got is not None else Decimal(0)
    return Hop(b.venue, base, quote, (b.bids[0][0] + b.asks[0][0]) / 2, swap, b.symbol, b.fee)


# a pool of two other tokens holding less than this is no market for a route (the site's $1K floor)
MIN_MARKET_USD = Decimal(1_000)


def venue_hops(raw: dict) -> dict[str, list[Hop]]:
    """Every venue's markets from pools.json, as the collector's routes see them: its CC pools (each
    with its liquidity, twice its CC side), its pools of two other tokens and its order books (Temple's
    CC/USDCx book under CC). A pool listed in ``not_trading`` is no market."""
    cc_usd = Decimal(str(raw["cc_usd"]))
    still = {(x["venue"], x["pair"]) for x in raw.get("not_trading") or []}
    hops: dict[str, list[Hop]] = {}
    for vs in raw["pools"].values():
        for v, d in vs.items():
            p = pool_from_json(d)
            if (v, f"CC/{p.token}") not in still:
                hops.setdefault(v, []).append(replace(pool_hop(p), usd=2 * p.cc_reserve * cc_usd))
    for o in raw.get("other_pools") or []:
        ra, rb = Decimal(o["ra"]), Decimal(o["rb"])
        usd = Decimal(str(o["usd"])) if o.get("usd") is not None else None
        hops.setdefault(o["venue"], []).append(
            Hop(o["venue"], o["a"], o["b"], rb / ra, FORMULAS[o["formula"]](ra, rb, Decimal(o["fee"])), usd=usd))
    for base, by_market in (raw.get("books") or {}).items():
        for d in by_market.values():
            hops.setdefault(d["venue"], []).append(book_hop(book_from_json(d), base, d["quote"]))
    return hops


def walk(path: tuple, frm: str, amount: Decimal) -> Decimal:
    """What ``amount`` of ``frm`` becomes after every hop of ``path`` in turn."""
    for h in path:
        amount = h.out(frm, amount)
        frm = h.to(frm)
    return amount


def venue_paths(hops: list[Hop], frm: str, to: str, floor: Decimal = Decimal(0)) -> list[tuple[Hop, ...]]:
    """Every way from ``frm`` to ``to`` on one venue's markets, in one swap or two (one-swap first).
    A pool under ``floor`` dollars is no market here, except a CC pool asked directly (a venue's own
    CC pool for the token was compared before routes existed, and the pages set thin ones aside)."""
    deep = [h for h in hops if h.usd is None or h.usd >= floor]
    one = [(h,) for h in hops if {h.a, h.b} == {frm, to} and (h in deep or CC in (h.a, h.b))]
    two = [(h1, h2) for h1 in deep if frm in (h1.a, h1.b) and h1.to(frm) not in (frm, to)
           for h2 in deep if h2 is not h1 and {h2.a, h2.b} == {h1.to(frm), to}]
    return one + two


@dataclass(frozen=True)
class Route:
    """One venue's ways between two tokens, asked like a pool: ``out(forward, amount)`` is its best
    path's output before network fees, ``paths`` every way of one or two swaps. ``cc_reserve`` is its
    direct CC pool's (0 without one), so it weighs in ``blended_mid`` as that pool would."""

    venue: str
    token: str  # display symbol of ``to``
    frm: str
    to: str
    paths: tuple
    cc_reserve: Decimal = Decimal(0)

    @property
    def mid(self) -> Decimal:
        """``to`` per ``frm`` along its shortest path, before fees."""
        r, f = Decimal(1), self.frm
        for h in self.paths[0]:
            r *= h.rate(f)
            f = h.to(f)
        return r

    def quotes(self, forward: bool, amount: Decimal) -> list[tuple[Decimal, tuple]]:
        """(output, path as walked) for every path; forward is ``frm`` to ``to``."""
        start = self.frm if forward else self.to
        return [(walk(p if forward else p[::-1], start, amount), p if forward else p[::-1]) for p in self.paths]

    def best(self, forward: bool, amount: Decimal, fee_per_swap=Decimal(0)) -> tuple[Decimal, tuple]:
        """The path that keeps the most after its network fees (in output units): ``fee_per_swap`` a
        swap, or a function of the path (each swap its pair's fee); (0, ()) when none can fill."""
        filled = [(o, p) for o, p in self.quotes(forward, amount) if o > 0]
        if not filled:
            return Decimal(0), ()
        cost = _cost(fee_per_swap)
        return max(filled, key=lambda q: q[0] - cost(q[1]))

    def out(self, forward: bool, amount: Decimal) -> Decimal:
        return self.best(forward, amount)[0]

    def book(self) -> Hop | None:
        """The order book its shortest path trades ``to`` on (None for a pool): what its liquidity is."""
        return next((h for h in self.paths[0] if self.to in (h.a, h.b) and h.market), None)


def _cost(fee) -> Callable[[tuple], Decimal]:
    """A path's network cost: ``fee`` a swap, or ``fee`` itself when it is a function of the path."""
    return fee if callable(fee) else (lambda path: len(path) * fee)


def _pick(p, forward: bool, amount: Decimal, fee_per_swap) -> tuple[Decimal, tuple]:
    """A venue's output and path for one trade: a ``Route`` picks its best path after its network fees;
    a plain pool or book is one swap (its own hop, so its pair's fee applies)."""
    if isinstance(p, Route):
        return p.best(forward, amount, fee_per_swap)
    return p.out(forward, amount), ((pool_hop(p),) if isinstance(p, VenuePool | CCBook) else (None,))


class DollarRoutes:
    """Buy or sell a token for dollars on each venue, every venue by its best route on its own markets
    (``Route``s from each dollar token it trades): one order on a book, a pool of the token against a
    dollar token, or dollars to CC to the token through two of its own pools. ``dollar`` is what each
    dollar token counts as (USDCx at its price, USDC.B as USDCx while it holds its peg)."""

    def __init__(self, routes: dict[str, list[Route]], dollar: dict[str, Decimal]) -> None:
        self.routes = {v: rs for v, rs in routes.items() if rs}
        self.dollar = dollar

    @property
    def venues(self) -> list[str]:
        return sorted(self.routes)

    @property
    def books(self) -> dict[str, Hop]:
        """Venues with a one-order book route, and that book."""
        out = {}
        for v, rs in self.routes.items():
            hop = next((p[0] for r in rs for p in r.paths if len(p) == 1 and p[0].market), None)
            if hop is not None:
                out[v] = hop
        return out

    def min_swaps(self, venue: str) -> int:
        """The fewest swaps any of its routes takes."""
        return min(len(p) for r in self.routes[venue] for p in r.paths)

    def buy(self, venue: str, usd: Decimal, fee_per_swap: Decimal = Decimal(0),
            dollar: str | None = None) -> tuple[Decimal, tuple]:
        """(token received for ``usd`` dollars, path) on its best route after ``fee_per_swap`` tokens;
        ``dollar`` keeps to routes paid in that dollar token."""
        best, cost = (Decimal(0), ()), _cost(fee_per_swap)
        for r in self.routes.get(venue, []):
            if dollar is not None and r.frm != dollar:
                continue
            got = r.best(True, usd / self.dollar[r.frm], cost)
            if got[0] - cost(got[1]) > best[0] - cost(best[1]):
                best = got
        return best

    def sell(self, venue: str, qty: Decimal, fee_per_swap: Decimal = Decimal(0),
             dollar: str | None = None) -> tuple[Decimal, tuple]:
        """(dollars received for ``qty`` of the token, path) after ``fee_per_swap`` dollars a swap;
        ``dollar`` keeps to routes that pay out in that dollar token."""
        best, cost = (Decimal(0), ()), _cost(fee_per_swap)
        for r in self.routes.get(venue, []):
            if dollar is not None and r.frm != dollar:
                continue
            o, p = r.best(False, qty, lambda path, d=self.dollar[r.frm]: cost(path) / d)
            o *= self.dollar[r.frm]
            if o - cost(p) > best[0] - cost(best[1]):
                best = (o, p)
        return best


def via(path: tuple) -> str | None:
    """The token a two-swap path passes through (a key)."""
    if len(path) != 2 or path[0] is None:
        return None
    return next(iter({path[0].a, path[0].b} & {path[1].a, path[1].b}))


def usd_ladder(routes: DollarRoutes, price_usd: Decimal, cc_in_usd: Decimal) -> list[dict]:
    """What each venue returns at each dollar size: tokens when buying, dollars when selling."""
    rows = []
    for side in ("buy", "sell"):
        for size in SIZES_USD:
            usd = Decimal(size)
            qty = usd / price_usd
            unit = price_usd if side == "buy" else Decimal(1)  # dollars per unit received
            picks = {v: (routes.buy(v, usd, lambda path, v=v: path_fee_usd(v, path, cc_in_usd) / unit)
                         if side == "buy" else routes.sell(v, qty, lambda path, v=v: path_fee_usd(v, path, cc_in_usd)))
                     for v in routes.venues}
            filled = {v: o for v, (o, p) in picks.items() if o > 0}
            if not filled:
                continue
            best = max(filled, key=filled.get)
            fair = qty if side == "buy" else usd  # what a trade at the Canton price would return
            swaps = {v: len(picks[v][1]) for v in filled}
            through = {v: via(picks[v][1]) for v in filled if swaps[v] == 2}
            rows.append({
                "side": side, "size_usd": size, "amount_in": float(usd if side == "buy" else qty),
                "out": {v: (float(o) if o > 0 else None) for v, (o, _) in picks.items()},
                "cost_bps": {v: float((1 - o / fair) * 10_000) for v, o in filled.items()},
                "best": best,
                "edge_bps": runner_up_bps(filled),
                "swaps": swaps, "via": through,
                **net_of_fees(filled, {v: path_fee_usd(v, picks[v][1], cc_in_usd) for v in filled}, unit),
            })
    return rows


def usd_round_trip(routes: DollarRoutes, buy_on: str, sell_on: str, cc_in_usd: Decimal,
                   lo_usd: float = 10, hi_usd: float = 100_000, steps: int = 60,
                   dollar: str | None = None) -> tuple[Decimal, Decimal]:
    """Dollars in and gross dollar gain at the best size: buy on one venue, sell on the other, both
    in ``dollar`` (a trip that ends in another dollar token than it started with is no round trip)."""
    def gain(x: Decimal) -> Decimal:
        qty = routes.buy(buy_on, x, dollar=dollar)[0]
        back = routes.sell(sell_on, qty, dollar=dollar)[0] if qty else Decimal(0)
        return (back - x) if back > 0 else Decimal(-10**12)

    grid = [Decimal(lo_usd * (hi_usd / lo_usd) ** (i / (steps - 1))) for i in range(steps)]
    gains = [gain(x) for x in grid]
    i = max(range(steps), key=lambda k: gains[k])
    a, b = grid[max(i - 1, 0)], grid[min(i + 1, steps - 1)]
    phi = Decimal((math.sqrt(5) - 1) / 2)
    for _ in range(40):
        c, d = b - phi * (b - a), a + phi * (b - a)
        if gain(c) > gain(d):
            b = d
        else:
            a = c
    x = (a + b) / 2
    return x, gain(x)


def usd_scan(token: str, routes: DollarRoutes, cc_in_usd: Decimal) -> list[dict]:
    """Round trips that touch an order book, in the same shape as ``scan`` rows, each paid and paid
    back in one dollar token (``dollar``). Until 2026-10-15 a trip could pay USDCx (at CoinGecko's
    ~0.9996) and come back in USDC.B (at exactly $1): CBTC from Temple's USDCx book sold on Rocky's
    USDC.B book "netted" $54.87 on $99,686 in the channel, all of it the two valuations."""
    if not routes.books:
        return []
    found = []
    for buy in routes.venues:
        for sell in routes.venues:
            if buy == sell or not ({buy, sell} & set(routes.books)):
                continue  # pool-to-pool trips are already in ``scan``, priced in CC
            trips = []
            for d in routes.dollar:
                if not (any(r.frm == d for r in routes.routes[buy]) and any(r.frm == d for r in routes.routes[sell])):
                    continue
                x, g = usd_round_trip(routes, buy, sell, cc_in_usd, dollar=d)
                # each venue's best route at that size, every swap paying its venue's network fee
                qty, p_buy = routes.buy(buy, x, dollar=d)
                p_sell = routes.sell(sell, qty, dollar=d)[1] if qty > 0 else ()
                # a side that cannot fill at that size still costs its shortest route (the trip never clears)
                cost_cc = ((path_fee_usd(buy, p_buy, cc_in_usd) / cc_in_usd if p_buy
                            else routes.min_swaps(buy) * swap_cost_cc(buy, cc_in_usd))
                           + (path_fee_usd(sell, p_sell, cc_in_usd) / cc_in_usd if p_sell
                              else routes.min_swaps(sell) * swap_cost_cc(sell, cc_in_usd)))
                trips.append((g - cost_cc * cc_in_usd, d, x, g, cost_cc, p_buy, p_sell))
            if not trips:
                continue
            net_usd, d, x, g, cost_cc, p_buy, p_sell = max(trips, key=lambda t: t[0])
            fp = "|".join(h.fingerprint() for h in (*p_buy, *p_sell))
            found.append({
                "token": token, "buy_on": buy, "sell_on": sell,
                "size_cc": float(x / cc_in_usd), "size_usd": float(x),
                "gross_cc": float(g / cc_in_usd), "net_cc": float(net_usd / cc_in_usd),
                "net_usd": float(net_usd), "cost_cc": float(cost_cc), "via": "usd", "dollar": d, "fp": fp,
                "clears": net_usd >= MIN_ROUTE_USD,
            })
    return found


# === paper desk ============================================================


def route_order(pools: dict[str, VenuePool], side: str, size_usd: int,
                cc_in_usd: Decimal, mid: Decimal) -> dict | None:
    """One paper order, filled on whichever venue returns more."""
    if len(pools) < 2:
        return None
    cc_amount = Decimal(size_usd) / cc_in_usd
    amount = cc_amount if side == "sell" else cc_amount * mid
    gross = {v: p.out(side == "sell", amount) for v, p in pools.items()}
    # each venue after its own network fee, so the pick and the gap are what a trader keeps
    out_usd = cc_in_usd / mid if side == "sell" else cc_in_usd
    outs = {v: o - swap_fee_usd(v, cc_in_usd, pool_hop(pools[v])) / out_usd for v, o in gross.items()}
    # against the runner-up, not the worst: with four venues the worst is a straw man
    best, worst = sorted(outs, key=outs.get, reverse=True)[:2]
    extra = outs[best] - outs[worst]
    extra_usd = extra * out_usd
    return {
        "side": side,
        "amount_in": float(amount),
        "venue": best,
        "got": float(outs[best]),
        "other": worst,
        "other_got": float(outs[worst]),
        "edge_bps": edge_bps(outs[best], outs[worst]),
        "extra_usd": float(extra_usd),
    }


def router_stats(fills: list[dict]) -> dict:
    """The honest headline: the median gap between venues on one order, not a running sum.

    Summing the gap to the worse venue overstates: these are paper fills on pools that never
    move, against a venue a careful trader would not have picked anyway. Fills are net of each
    venue's network fee (``route_order``).
    """
    if not fills:
        return {}
    edges = [f["edge_bps"] for f in fills]
    return {"window": len(fills), "median_edge_bps": round(statistics.median(edges), 2),
            "median_extra_usd": round(statistics.median(f["extra_usd"] for f in fills), 4),
            "max_edge_bps": round(max(edges), 2)}


def fingerprint(*pools: VenuePool) -> str:
    """Identifies pool state, so one standing spread is booked once, not every tick."""
    return "|".join(f"{p.venue}:{p.cc_reserve:.6f}:{p.token_reserve:.6f}" for p in pools)


# === LP board ==============================================================


def fee_apr(volume_usd_24h: float, fee: float, lp_share: float, tvl_usd: float) -> float | None:
    """Annualised fee income to LPs from the last 24 hours of volume."""
    if tvl_usd <= 0:
        return None
    return volume_usd_24h * fee * lp_share / tvl_usd * 365


# === history ===============================================================


CHANGE_MAX_AGE_MS = 2 * 3_600_000


def change(series: list[tuple[int, float]], now_ms: int, window_ms: int,
           max_age_ms: int = CHANGE_MAX_AGE_MS) -> float | None:
    """Relative change over ``window_ms`` from a (time_ms, value) series.

    None when the series has no point in the last ``max_age_ms`` (a market that stopped trading: on
    2026-10-14 TRKXRWA's candles had ended on 7 Oct and its "24h change" read +1686%), or when its
    last point before the window starts is more than an eighth of the window older than that start.
    """
    if not series or now_ms - series[-1][0] > max_age_ms:
        return None
    target = now_ms - window_ms
    before = [(t, v) for t, v in series if t <= target]
    if not before or target - before[-1][0] > window_ms // 8:
        return None
    past = [v for _, v in before]
    # the median of the last three points before the window, so one wick in a thin pool (a
    # single trade that moved the price for an hour) is not taken as the starting price; with fewer
    # than three, the one nearest the window's start (with two, the "median" was the higher one: a
    # wick of 100x read as -99%)
    base = sorted(past[-3:])[1] if len(past) >= 3 else past[-1]
    if base <= 0:
        return None
    return series[-1][1] / base - 1


# === saving and rebuilding pools ===========================================


def _cantex_out(cc: Decimal, tok: Decimal, fee: Decimal):
    from cantonvenues.venues.cantex_public import swap_output

    def out(sell_cc: bool, amount: Decimal) -> Decimal:
        return swap_output(cc, tok, amount, fee) if sell_cc else swap_output(tok, cc, amount, fee)
    return out


def _tradecraft_out(cc: Decimal, tok: Decimal, fee: Decimal):
    from cantonvenues.venues.tradecraft import swap_output

    def out(sell_cc: bool, amount: Decimal) -> Decimal:
        return swap_output(cc, tok, amount, fee) if sell_cc else swap_output(tok, cc, amount, fee)
    return out


def _cp_out(cc: Decimal, tok: Decimal, fee: Decimal):
    from cantonvenues import constant_product_output

    def out(sell_cc: bool, amount: Decimal) -> Decimal:
        if sell_cc:
            return constant_product_output(cc, tok, amount, fee)
        return constant_product_output(tok, cc, amount, fee)
    return out


# cp: plain constant product, fee on the input (OneSwap, Pool Party; assumed, see their adapters)
FORMULAS = {"cantex": _cantex_out, "tradecraft": _tradecraft_out, "cp": _cp_out}


def pool_to_json(p: VenuePool) -> dict:
    return {"venue": p.venue, "token": p.token, "cc_reserve": str(p.cc_reserve),
            "token_reserve": str(p.token_reserve), "formula": p.formula,
            "fee_param": str(p.fee_param), "fee": str(p.fee), "lp_share": str(p.lp_share)}


def pool_from_json(d: dict) -> VenuePool:
    cc, tok, fee = Decimal(d["cc_reserve"]), Decimal(d["token_reserve"]), Decimal(d["fee_param"])
    return VenuePool(d["venue"], d["token"], cc, tok, Decimal(d["fee"]), Decimal(d["lp_share"]),
                     FORMULAS[d["formula"]](cc, tok, fee), d["formula"], fee)


# === routing any amount =====================================================


def best_leg(pools: dict[str, VenuePool], sell_cc: bool, amount: Decimal) -> tuple[str, Decimal, dict]:
    """The venue that returns most for one CC-paired swap, and every venue's output."""
    outs = {v: p.out(sell_cc, amount) for v, p in pools.items()}
    best = max(outs, key=outs.get)
    return best, outs[best], outs


def route(books: dict[str, dict[str, VenuePool]], sell: str, buy: str, amount: Decimal) -> dict:
    """Best route to sell ``amount`` of ``sell`` for ``buy``.

    Every pool is paired with CC, so a token-to-token trade goes through CC in
    two legs, and each leg is sent to its own best venue: the second leg's
    price depends only on the CC amount, not on where the first leg filled.
    """
    sell, buy = key(sell), key(buy)
    if sell == buy:
        raise ValueError("sell and buy are the same token")
    if amount <= 0:
        raise ValueError("amount must be positive")
    for t in (sell, buy):
        if t != CC and t not in books:
            raise ValueError(f"no Canton DEX pool for {t}")
    legs = []
    if sell == CC:
        v, got, outs = best_leg(books[buy], True, amount)
        legs.append({"sell": CC, "buy": buy, "amount_in": amount, "venue": v, "out": got, "all": outs})
    elif buy == CC:
        v, got, outs = best_leg(books[sell], False, amount)
        legs.append({"sell": sell, "buy": CC, "amount_in": amount, "venue": v, "out": got, "all": outs})
    else:
        v1, cc_mid, outs1 = best_leg(books[sell], False, amount)
        legs.append({"sell": sell, "buy": CC, "amount_in": amount, "venue": v1, "out": cc_mid, "all": outs1})
        v2, got, outs2 = best_leg(books[buy], True, cc_mid)
        legs.append({"sell": CC, "buy": buy, "amount_in": cc_mid, "venue": v2, "out": got, "all": outs2})
    return {"sell": sell, "buy": buy, "amount_in": amount, "amount_out": legs[-1]["out"], "legs": legs}
