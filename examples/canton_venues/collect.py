"""Canton Venues collector: one loop that writes the public JSON API.

Sources, all public, no keys:
  Cantex      /v1/public REST + websocket candles   (CantexPublicData)
  Tradecraft  /v1 REST                               (TradecraftAdapter)
  Rocky       spot and perp order books              (RockyAdapter)
  Ekiden      MainNet perpetuals                     (EkidenAdapter)
  OneSwap     pool reserves                          (OneSwapPublicData)
  Pool Party  pool reserves and volume               (PoolPartyPublicData)
  Temple      settled volume per market, no key      (TempleAdapter)
  DefiLlama   Canton chain TVL, and DEX volume as an internal cross-check
  CoinGecko   outside prices for the premium board (gold and silver: Ekiden's spot index)

Every tick (default 300 s) prices both venues from reserves; every third tick
also refreshes candles, per-pool volumes and DefiLlama. Output goes to
``<out>/api/v1/*.json``, written atomically, which the page and anyone else
reads, plus a static page per venue under ``<out>/venues/`` (venue_pages.py).

    python collect.py --out /var/www/canton-venues --interval 300
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import math
import os
import statistics
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import alerts as al
import channel
import exec_history
import httpx
import model as m
import venue_history
import venue_pages
from model import CC, VenuePool
from publish_guard import PublishGuard

from cantonvenues import (
    CantexPublicData,
    EkidenAdapter,
    Instrument,
    OneSwapPublicData,
    PoolPartyPublicData,
    RockyAdapter,
    TempleAdapter,
    TradecraftAdapter,
)

log = logging.getLogger("venues")

USDCX = "USDCX"
USDCX_GECKO = "xreserve-bridged-usdc-canton"
SLOW_EVERY = 3
# read every tick; one that does not answer is left out of that tick, the rest is published
CORE = ("cantex", "tradecraft")
# while Cantex does not answer, its last 24h volume and tickers stand in for this long
CX_STATS_MAX_AGE_S = 3600
# a slow cache past its limit is dropped, not shown as current (summary.json ``sources`` keeps each
# one's last good read for the watchdog): CoinGecko's last answer stands in for a failed request this
# long; Tradecraft's and Temple's 24h volumes and the candles an hour; DefiLlama six hours
GECKO_MAX_AGE_S = 600
VOLUME_MAX_AGE_S = 3600
LLAMA_MAX_AGE_S = 6 * 3600
# Temple's row reads "down" once its keyless settled volume has not been read for this long
TEMPLE_MAX_AGE_S = 45 * 60
# Cantex's network fee per pair from the quote check (quotecheck.py, hourly) stands this long
PAIR_FEE_MAX_AGE_S = 3 * 3600
HISTORY_KEEP_S = 8 * 24 * 3600
DESK_KEEP = 400
LLAMA = "https://api.llama.fi"
GECKO = "https://api.coingecko.com/api/v3/simple/price"
GECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
# Cantex maps these to their underlying asset's CoinGecko id; that logo would
# say they ARE that asset, so they get none.
NO_LOGO = {"USX", "USDXLR"}
# What a perp tracks outside Canton, for its basis. CoinGecko ids; gold and silver are their spot
# price instead (``METALS``), not a token's.
PERP_SPOT = {"BTC": "bitcoin", "ETH": "ethereum", "CC": "canton-network", "HYPE": "hyperliquid"}
# Spot gold and silver: Ekiden's index for its XAU and XAG perps, read with its tickers every tick
# (model.REFERENCES "spot:XAU"). Taken only while the spot market trades (model.metals_open), and
# not once it has stood still this long in market hours: an index that stops is not spot.
METALS = ("XAU", "XAG")
METAL_STILL_MAX_S = 900
# Each step in how a published series is computed, recorded once in history.json "steps" with the
# time of the first tick computed the new way, so a chart or a weekly median can tell the step from
# a market move. "premium": the step changes these keys' premium readings.
STEPS = [
    {"id": "cc-price-depth-weighted", "keys": ["CC"], "premium": True,
     "what": "CC price weighted by depth across every CC/USDCx market, Temple's order book included; "
             "until then the pools' mid weighted by CC reserve. Every price converted through CC moves with it."},
    {"id": "metals-against-spot", "keys": ["EXAU", "EXAG"], "premium": True,
     "what": "eXAU and eXAG measured against gold and silver spot while the spot market trades; until "
             "then against PAXG and KAG, tokens with their own premium."},
    {"id": "stock-tokens-no-reference", "keys": ["SPYE", "QQQE"], "premium": True,
     "what": "SPYe and QQQe no longer measured against SPYx and QQQx, tokens with their own premium."},
    # From 16:10 to the next deploy on 2026-10-14, pool tokens were valued at the market CC price, so
    # every stablecoin read the pools' CC gap to Temple (-54 bp then) as a peg gap: those samples of every
    # token but CC are dropped (``_mark_steps``), so no chart or weekly post takes them as readings.
    {"id": "pool-tokens-in-venue", "keys": [], "premium": False, "drops_since": "cc-price-depth-weighted",
     "what": "A token in a pool is valued through its own venue's CC/USDCx pool (what it fetches in USDCx "
             "there), not at the market CC price; samples of every token but CC since the CC step are dropped."},
]
# Alerts our own method made, taken out of the site's feed (founder 2026-10-15): (time, key). Between the
# CC price step and the per-venue valuation fix on 14.10, pool tokens were valued at the market CC price
# and three stablecoins read off their peg by the pools' gap to Temple's book; none reached the channel.
WITHDRAWN_ALERTS = {(1791994233, "peg:USDM1:above"), (1791995181, "peg:FRXUSD.B:above"),
                    (1791995181, "peg:USDXLR:above")}
MIN_MARKET_USD = m.MIN_MARKET_USD  # a pool under it is no market for a new route (the $1K floor)
# A pool whose reserves have not moved for STILL_POOL_S has not traded (any swap moves them); once its
# price is also STILL_POOL_GAP or more off the market, it is marked "not trading" and left out of every
# price, comparison, route, spread and alert, still listed with its reserves. 2026-10-15: CC fell 12.5%
# overnight and Tradecraft's CC/USDCx pool (4.36M CC) stood still at +6.75%, so the site showed it as
# the best place to sell CC, a $434 spread and a channel post, with nothing saying nobody traded there.
STILL_POOL_S = 2 * 3600
STILL_POOL_GAP = Decimal("0.01")
# why a CC pool is read but not quoted (lp.json unpriced_pools)
UNNAMED = "token not named by any venue we price"
SECOND_POOL = "a second pool for the same token"
# Rocky quote asset (upper-cased; the venue mixes cases) -> our token key
ROCKY_QUOTES = {"USDCX": USDCX, "USDC.B": "USDC.B"}

# Every Canton trading venue we know of and what we read from it (research 2026-10-03,
# canton/docs/2026-10-03-venue-coverage-research.md in the private workspace). Volumes are
# filled in live; "llama" names the DefiLlama protocol a volume comes from.
VENUES = [
    {"id": "temple", "name": "Temple", "kind": "Spot order book", "status": "volume",
     "llama": "Temple", "note": "Settled volume per market from Temple's public API. Prices and book need an account key."},
    {"id": "cantex", "name": "Cantex", "kind": "Spot AMM", "status": "priced",
     "note": "Reserves, volume and candles from the public API."},
    {"id": "tradecraft", "name": "Tradecraft", "kind": "Spot AMM", "status": "priced",
     "note": "Reserves and per-pool volume from the public API."},
    {"id": "rocky", "name": "Rocky", "kind": "Spot order book", "status": "priced",
     "note": "Order books and 24h tickers from the public API."},
    {"id": "rocky_perp", "name": "Rocky perps", "kind": "Perpetuals", "status": "priced",
     "note": "Order books and 24h tickers from the public API."},
    {"id": "ekiden", "name": "Ekiden", "kind": "Perpetuals", "status": "priced",
     "note": "MainNet tickers, mark, index and funding from the public API."},
    {"id": "poolparty", "name": "Pool Party", "kind": "Spot AMM", "status": "priced",
     "note": "Reserves and volume from the public API. CC pairs priced; fee (0.30%) from CCTools."},
    {"id": "oneswap", "name": "OneSwap", "kind": "Spot AMM", "status": "priced",
     "note": "Reserves from the public API. CC pairs priced. Volume is not published."},
    {"id": "canborsa", "name": "Canborsa", "kind": "Perpetuals", "status": "waiting",
     "note": "Only the web app's internal API; a public API is on their Q4 roadmap."},
    {"id": "silvana", "name": "Silvana", "kind": "Private order book", "status": "closed",
     "note": "Prices need a KYC-onboarded account."},
    {"id": "ibex", "name": "Ibex", "kind": "Perpetuals", "status": "testnet", "note": "Testnet only."},
    {"id": "titan", "name": "Titan", "kind": "Perpetuals", "status": "testnet",
     "note": "Testnet and a MainNet waitlist."},
]


def _finite(x, where: list):
    """``x`` with every NaN or infinite float replaced by None; ``where`` collects their paths."""
    if isinstance(x, float) and not math.isfinite(x):
        where.append(x)
        return None
    if isinstance(x, Decimal):
        return _finite(float(x), where)
    if isinstance(x, dict):
        return {k: _finite(v, where) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_finite(v, where) for v in x]
    return x


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # A NaN or infinite number is written as null, with a warning: the file stays valid JSON and the
    # tick goes on. Until 2026-10-15 it failed the write, so one "NaN" in any venue's field (13 of 17
    # tried) stopped every tick; the adapters now refuse such a value at the row, this is the last net.
    bad: list = []
    clean = _finite(data, bad)
    if bad:
        log.warning("%s: %d non-finite number(s) written as null", path.name, len(bad))
    tmp.write_text(json.dumps(clean, separators=(",", ":"), default=float, allow_nan=False))
    os.replace(tmp, path)


def _num(x) -> Decimal | None:
    """A venue's number (string or number), or None when it is missing, null or not a number."""
    try:
        d = Decimal(str(x)) if x is not None and x != "" else None
    except ArithmeticError:
        return None
    return d if d is not None and d.is_finite() else None


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def r(x, n=6):
    return None if x is None else round(float(x), n)


def _book_of(p):
    """The order book a venue's quote trades the token on (a ``Hop``), or None for a pool."""
    if isinstance(p, m.Route):
        return p.book()
    return m.pool_hop(p) if isinstance(p, m.CCBook) else None


def _tc_swap(st):
    """A Tradecraft pool of two tokens, priced the venue's way (half its fee on each leg)."""
    from cantonvenues.venues.tradecraft import swap_output

    def swap(a_to_b: bool, amount: Decimal) -> Decimal:
        rin, rout = (st.reserve_a, st.reserve_b) if a_to_b else (st.reserve_b, st.reserve_a)
        return swap_output(rin, rout, amount, st.total_fee)
    return swap


def _trip_pools(route: dict) -> set:
    """The markets a scanner round trip trades on: (venue, market) pairs from its fingerprint, or its
    two venues' pools of the token for a pool-to-pool trip priced in CC."""
    if route.get("fp"):
        return {tuple(part.split(":")[:2]) for part in route["fp"].split("|")}
    return {(route["buy_on"], route["token"]), (route["sell_on"], route["token"])}


def _gecko_usd(gecko: dict, gid: str, now: int) -> float | None:
    """A CoinGecko price, or None when it is missing or CoinGecko last updated it more than
    ``model.REFERENCE_MAX_AGE_S`` ago (KAG's stood still for up to 2 h 40 min in October)."""
    row = gecko.get(gid) or {}
    t, usd = row.get("last_updated_at"), row.get("usd")
    # a 0 or a non-number is no price (a 0 read as "unit mismatch, excluded" before 2026-10-14)
    if not isinstance(usd, int | float) or not math.isfinite(usd) or usd <= 0 or t is None \
            or now - t > m.REFERENCE_MAX_AGE_S:
        return None
    return usd


class Collector:
    def __init__(self, out: Path, state: Path | None = None) -> None:
        self.out = out
        # records kept off the web root: the hourly best-execution history (exec_history.py)
        self.state = state or out.with_name(out.name + "-state")
        self.api = out / "api" / "v1"
        self.cantex = CantexPublicData()
        self.tradecraft = TradecraftAdapter()
        # order-book venues: optional, each fails alone and reconnects on a later tick
        self.optional = {"rocky": RockyAdapter(), "rocky_perp": RockyAdapter("perp"),
                         "ekiden": EkidenAdapter(), "oneswap": OneSwapPublicData(),
                         "poolparty": PoolPartyPublicData(), "temple": TempleAdapter()}
        self.live: set[str] = set()
        # core venue -> unix time of the first failed read in a row; empty when both answered
        self.down: dict[str, int] = {}
        self.cc_inst = None  # CC's instrument, from Cantex's pools; kept while Cantex is down
        self.cx_stats: dict = {}  # Cantex's last 24h volume and tickers, with the time they were read
        self.cc_usd_last = Decimal(0)
        self.temple_priced = False
        self.rocky_books: dict[str, float] = {}  # each Rocky spot book's 24h turnover in dollars, this tick
        self.metal_spot: dict[str, Decimal] = {}  # gold and silver spot this tick (METALS), when usable
        self.metal_seen: dict[str, tuple[Decimal, int]] = {}  # each index's last value and since when
        # each outside source's last good read (unix time), for summary.json and the watchdog; carried
        # over from the last summary, so a source broken by a deploy ages and is told (it was empty
        # after every restart, and a source absent from it was never a trouble)
        self.sources: dict[str, int] = {
            k: v for k, v in (load_json(out / "api" / "v1" / "summary.json", {}).get("sources") or {}).items()
            if isinstance(v, int) and k != "llama"}
        self.fail_times: list[int] = []  # ticks that failed, for summary.json failed_ticks_1h
        self.last_error: dict | None = None  # the last failed tick's error and time
        self.temple_cc_book = False  # Temple's CC/USDCx book read this tick (it weighs most in the CC price)
        self.vol_seen: dict[str, dict] = {"tc": {}, "pp": {}}  # per-pool 24h volume, last good read and when
        self.started = int(time.time())
        self.usdcx_last = Decimal(1)  # CoinGecko's last good USDCx price
        self.temple_error: str | None = None  # why Temple's keyed books were not read, when they were not
        # each venue's own CC price this tick, from its CC/USDCx market (``_venue_usd``)
        self.cc_at: dict[str, Decimal] = {}
        # pools read this tick that pair two tokens other than CC, with their reserves (lp.json
        # other_pools) and, where both tokens are named, as a market for the routes inside the venue
        self.other_pools: list[dict] = []
        self.http = httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "canton-venues/1"})
        self.tick_no = 0
        self.slow: dict = {}
        self.history = load_json(self.api / "history.json", {"t": [], "usd": {}, "premium": {}})
        self.desk = load_json(self.api / "desk.json", {})
        self.desk.setdefault("router", {"fills": [], "n": 0, "extra_usd": 0.0, "by_venue": {}})
        self.desk.setdefault("arb", {})
        self.desk.setdefault("pnl", [])
        # CC pools read but not quoted this tick (a token no venue names, a second pool for a token);
        # their liquidity still counts, from the CC side, in every pool total (lp.json unpriced_pools)
        self.unpriced: list[dict] = []
        # row id -> unix time of the last successful read of that venue's data, for the publish guard
        self.fresh: dict[str, int] = {}
        self.guard = PublishGuard(out / "venues" / "published.json")
        # per-venue daily volume and our hourly readings, for the venue pages' charts (venue_history.py)
        self.vhist = load_json(self.api / "venue_history.json", {})
        # each pool's reserves as last read and since when they stood still, kept across restarts
        # ("venue:A/B" -> [reserves, since]); pools marked not trading this tick, by (venue, token key)
        # for CC pools and on their ``other_pools`` entry for the rest
        self.pool_still: dict[str, list] = load_json(self.state / "pools_still.json", {})
        # CC's instrument as last read from Cantex, kept on disk: a restart while Cantex is down failed
        # every tick until Cantex answered
        if (inst := load_json(self.state / "cc_instrument.json", None)) and {"admin", "id"} <= set(inst):
            self.cc_inst = Instrument(inst["admin"], inst["id"])
        self.still: dict[tuple[str, str], dict] = {}

    async def start(self) -> None:
        await self.cantex.connect()
        await self.tradecraft.connect()

    async def stop(self) -> None:
        await self.cantex.close()
        await self.tradecraft.close()
        for a in self.optional.values():
            await a.close()
        await self.http.aclose()

    # === sources ===========================================================

    async def _json(self, url: str, params: dict | None = None):
        resp = await self.http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def _refresh_slow(self, tc_states, cantex_markets) -> None:
        """Candles, per-pool Tradecraft volume, DefiLlama. Each part fails alone."""
        symbols = [mk["symbol"] for mk in cantex_markets if mk.get("source") == "cantex" and mk.get("symbol")]
        try:
            if symbols:  # none while Cantex is down: the last candles stay, for their hour
                got = await self.cantex.candles(symbols, 3600, timeout=40)
                if not got:  # the websocket wait ran out with nothing: no read, the last candles stand
                    raise ValueError("no candles before the timeout")
                self.slow["candles"] = got
                self.sources["candles"] = int(time.time())
        except Exception as exc:  # noqa: BLE001
            log.warning("candles: %s", exc)
        if tc_states:  # none while Tradecraft is down: its last volumes stay, for their hour
            got = {}
            for st in tc_states:
                try:
                    v = _num((await self.tradecraft.pool_volume_usd(st.token_a, st.token_b)).get("1d"))
                    if v is None:  # a null or a non-number is unknown, not $0
                        raise ValueError("1d volume missing")
                    got[st.amm_id] = float(v)
                except Exception as exc:  # noqa: BLE001
                    log.warning("tradecraft volume %s: %r", st.amm_id, exc)
            vols, missing = self._volume_seen("tc", got, [st.amm_id for st in tc_states])
            if vols:
                self.slow["tc_volume"] = vols
            # stamped only when every pool has a figure: one failing pool cut the venue's total 41%
            # with nothing said; now the total ages out after an hour and the watchdog tells
            if vols and not missing:
                self.sources["tc_volume"] = self.fresh["tradecraft"] = int(time.time())
        try:
            # the per-protocol daily breakdown feeds each venue page's volume history
            dex = await self._json(f"{LLAMA}/overview/dexs/Canton", {"excludeTotalDataChart": "true"})
            # Canton's TVL as DefiLlama's dated daily point, so the page can say which day it is
            tvl = (await self._json(f"{LLAMA}/v2/historicalChainTvl/Canton") or [{}])[-1]
            now = int(time.time())
            # An internal cross-check and the venue pages' "on Canton" gate; the public figures are our
            # own (venues.json), and only the TVL, which we do not measure, is shown, with its day
            self.slow["llama"] = {
                "t": now,
                "dex_volume_24h": dex.get("total24h"),
                "dex_volume_7d": dex.get("total7d"),
                "dex_change_1d": dex.get("change_1d"),
                "venues": sorted(({"name": p["name"], "volume_24h": p.get("total24h")}
                                  for p in dex.get("protocols", [])),
                                 key=lambda p: -(p["volume_24h"] or 0)),
                "tvl": tvl.get("tvl"),
                "tvl_day": datetime.fromtimestamp(tvl["date"], UTC).date().isoformat() if "date" in tvl else None,
            }
            per, total = venue_history.llama_daily(dex, now)
            for slug, pts in per.items():
                venue_history.set_daily(self.vhist, slug, "defillama", pts, now)
            if total:
                self.vhist["daily_total"] = total
        except Exception as exc:  # noqa: BLE001
            log.warning("defillama: %s", exc)
        # Tradecraft's own hourly volume per pool, last week, summed into complete days; once an hour
        if tc_states and time.time() - self.slow.get("tc_hist_t", 0) >= 3600:
            try:
                hist = [await self.tradecraft.volume_history(st.token_a, st.token_b, "week") for st in tc_states]
                venue_history.set_daily(self.vhist, "tradecraft", "tradecraft",
                                        venue_history.hourly_to_days(hist), int(time.time()))
                self.slow["tc_hist_t"] = time.time()
            except Exception as exc:  # noqa: BLE001
                log.warning("tradecraft volume history: %s", exc)
        temple = await self._venue("temple")
        if temple is not None:
            try:
                vol = await temple.settled_volume(24)
                total = _num(vol.get("total_volume_usd"))
                if total is None:
                    raise ValueError("total_volume_usd missing or not a number")
                markets = {}
                for mk in vol["markets"]:
                    # one market that will not parse is left out, not Temple's whole volume
                    if (q := _num(mk.get("quote_volume"))) is not None and mk.get("symbol"):
                        markets[mk["symbol"]] = float(q)
                    else:
                        log.warning("temple volume %s: not a number", mk.get("symbol"))
                self.slow["temple_volume"] = markets
                self.slow["temple_total"] = float(total)
                self.sources["temple_volume"] = self.fresh["temple"] = int(time.time())
            except Exception as exc:  # noqa: BLE001
                # the keyless volume failing says nothing of the keyed books: Temple stays connected
                # (it was dropped, and its books went unread for the tick); its volume ages out
                log.warning("temple settled volume: %r", exc)
        try:
            self.slow["tokens_info"] = await self.cantex.tokens()
        except Exception as exc:  # noqa: BLE001
            log.warning("tokens info: %s", exc)
        try:
            ids = {t["coingecko_id"] for t in self.slow.get("tokens_info", []) if t.get("coingecko_id")}
            if ids and "images" not in self.slow:
                rows = await self._json(GECKO_MARKETS, {"vs_currency": "usd", "ids": ",".join(sorted(ids))})
                by_id = {c["id"]: c["image"] for c in rows}
                self.slow["images"] = {
                    m.key(t["instrument_symbol"]): by_id.get(t.get("coingecko_id"))
                    for t in self.slow["tokens_info"]
                    if m.key(t["instrument_symbol"]) not in NO_LOGO and by_id.get(t.get("coingecko_id"))
                }
        except Exception as exc:  # noqa: BLE001
            log.warning("coingecko images: %s", exc)

    async def _venue(self, name: str):
        """An optional adapter, connected; None while its venue is unreachable."""
        if name not in self.live:
            try:
                await self.optional[name].connect()
                self.live.add(name)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: connect: %s", name, exc)
                await self.optional[name].close()
                return None
        return self.optional[name]

    def _core_down(self, venue: str, now: int, exc: Exception) -> None:
        log.warning("%s: not read, left out of this tick: %r", venue, exc)
        self.down.setdefault(venue, now)

    def _cc_instrument(self, cx_states):
        """CC's instrument, which every venue's tokens are matched against: from Cantex's pools,
        or the last one seen while Cantex does not answer."""
        found = next((s.token_a if s.symbol_a == CC else s.token_b
                      for s in cx_states if CC in (s.symbol_a, s.symbol_b)), None)
        if found is not None:
            if found != self.cc_inst:
                write_json(self.state / "cc_instrument.json", {"admin": found.admin, "id": found.id})
            self.cc_inst = found
        if self.cc_inst is None:
            raise RuntimeError("CC's instrument is unknown: Cantex has not answered since the start")
        return self.cc_inst

    def _volume_seen(self, venue: str, got: dict[str, float], pools: list[str]) -> tuple[dict[str, float], list[str]]:
        """Each pool's 24h volume: this read's, or its last good one while under an hour old. Returns
        the figures and the pools left without one (a pool that does not answer once no longer drops
        out of the venue's total)."""
        now = time.time()
        seen = self.vol_seen[venue]
        seen.update({k: (v, now) for k, v in got.items()})
        vols = {k: v for k, (v, t) in seen.items() if now - t <= VOLUME_MAX_AGE_S}
        self.vol_seen[venue] = {k: x for k, x in seen.items() if now - x[1] <= 86400}
        return vols, [k for k in pools if k not in vols]

    def _drop(self, name: str, exc: Exception) -> None:
        log.warning("%s: %s", name, exc)
        self.live.discard(name)

    async def _rocky_spot(self, books, cc_usd, usdcx_usd) -> tuple[dict, dict, float | None]:
        """Rocky's dollar-quoted books: per token its quote over every book (depth-weighted, the deepest
        named), the books themselves ({token: {market: (book, quote key)}}, also the markets of the
        routes inside Rocky, ``_hops``), and Rocky spot volume."""
        self.rocky_books = {}
        rocky = await self._venue("rocky")
        if rocky is None:
            return {}, {}, None
        try:
            tickers = {t.symbol: t for t in await rocky.tickers()}
            markets = await rocky.markets()
            # A dollar token is worth its peg here, not its price in Canton's pools: valuing USDC.B at
            # its pool premium (0.28% on 2026-10-04) made a CBTC trip through Rocky's USDC.B book look
            # like $150 per $100K when the same books at par lost $128. Off its peg by more than 1% (the
            # dollar routes' guard), USDC.B is its pool price here too, not $1.
            usdcb = m.weighted_usd([(self._venue_usd(v, p, cc_usd), m.amm_depth_usd(p, cc_usd))
                                    for v, p in books.get("USDC.B", {}).items()])
            par = usdcb is None or abs(usdcb - 1) <= Decimal("0.01")
            quote_usd = {q: (usdcx_usd if k == USDCX else Decimal(1) if par else usdcb) for q, k in ROCKY_QUOTES.items()}
            found: dict = {}
            kept: dict = {}
            tried = failed = 0
            for mk in markets:  # every dollar-quoted book of a token we price
                quote, k = m.key(mk.quote), m.key(mk.base)
                if quote not in quote_usd or k not in books:
                    continue
                tried += 1
                try:  # one book that does not answer is left out, not every Rocky book
                    book = await rocky.order_book(mk.symbol, 100)
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    log.warning("rocky book %s: %r", mk.symbol, exc)
                    continue
                if book.mid_price is None:
                    continue
                bv = m.BookVenue("rocky", mk.symbol, tuple((lv.price, lv.size) for lv in book.bids),
                                 tuple((lv.price, lv.size) for lv in book.asks), quote_usd[quote],
                                 m.BOOK_TAKER_FEE["rocky"])
                kept.setdefault(k, {})[mk.symbol] = (bv, ROCKY_QUOTES[quote])
                found.setdefault(k, []).append({
                    "symbol": mk.symbol, "quote": ROCKY_QUOTES[quote],
                    "price_usd": book.mid_price * quote_usd[quote],
                    "depth_usd": m.book_depth_usd(book.bids, book.asks, book.mid_price, quote_usd[quote]),
                    "spread_bps": r((book.best_ask.price / book.best_bid.price - 1) * 10_000, 2)})
            if tried and not found:  # every book failed or one-sided: Rocky was not read, not "priced"
                raise ValueError(f"no usable book ({failed} of {tried} failed)")
            # a token's depth on Rocky is every one of its books (CBTC: a USDCx and a USDC.B book, each
            # ~$35M within 1%, only the deeper one counted before 2026-10-14); the deepest is named
            out: dict = {}
            for k, bs in found.items():
                top, depth = max(bs, key=lambda b: b["depth_usd"]), sum(b["depth_usd"] for b in bs)
                out[k] = {**top, "price_usd": r(m.weighted_usd([(b["price_usd"], b["depth_usd"]) for b in bs])
                                               or top["price_usd"], 8),
                          "depth_usd": r(depth, 2), "books": len(bs)}
            volume = Decimal(0)
            books_usd: dict[str, float] = {}
            for mk in markets:  # volume over every book, a CBTC-quoted one through the CBTC price
                t, quote, k = tickers.get(mk.symbol), m.key(mk.quote), m.key(mk.base)
                q_usd = quote_usd.get(quote) or (Decimal(str(out[quote]["price_usd"])) if quote in out else None)
                if not (t and q_usd):
                    continue
                volume += t.turnover_24h * q_usd
                books_usd[mk.symbol] = float(t.turnover_24h * q_usd)
                if k in out:
                    out[k]["volume_24h_usd"] = r((out[k].get("volume_24h_usd") or 0) + float(t.turnover_24h * q_usd), 2)
            self.fresh["rocky"] = int(time.time())
            self.rocky_books = books_usd
            return {k: {"rocky": v} for k, v in out.items()}, kept, float(volume)
        except Exception as exc:  # noqa: BLE001
            self._drop("rocky", exc)
            return {}, {}, None

    async def _temple_spot(self, books, usdcx_usd) -> tuple[dict, dict, dict | None, m.BookVenue | None]:
        """Temple's USDCx books, in the same shape as Rocky's, plus its CC/USDCx quote for the premium
        and its CC/USDCx book, which joins every CC/USDCx comparison (``m.CCBook``).

        Needs TEMPLE_API_KEY; without it Temple stays volume-only (settled volume needs no key).
        Volume is not taken from here: the keyless settled-volume route already counts it.
        """
        temple = await self._venue("temple")
        self.temple_priced = self.temple_cc_book = False
        if temple is None or not temple.has_key:
            return {}, {}, None, None
        try:
            out, kept, cc, cc_book, errors = {}, {}, None, None, []
            for t in await temple.tickers():
                base, _, quote = t.symbol.partition("/")
                k = m.key(base)
                if quote != "USDCx" or (k not in books and k != CC):
                    continue
                book = None
                for attempt in range(2):  # one book that fails is asked again, then left out alone (one
                    try:                  # 502 dropped every Temple book, its CC book included)
                        book = await temple.order_book(t.symbol, 200)
                        break
                    except Exception as exc:  # noqa: BLE001
                        if attempt:
                            errors.append(f"{t.symbol}: {type(exc).__name__}: {exc}"[:120])
                if book is None or book.mid_price is None:
                    continue
                if book.best_bid.price >= book.best_ask.price:  # a crossed book is no market
                    errors.append(f"{t.symbol}: crossed book")
                    continue
                depth = m.book_depth_usd(book.bids, book.asks, book.mid_price, usdcx_usd)
                info = {"symbol": t.symbol, "quote": USDCX, "price_usd": r(book.mid_price * usdcx_usd, 8),
                        "depth_usd": r(depth, 2),
                        "spread_bps": r((book.best_ask.price / book.best_bid.price - 1) * 10_000, 2),
                        "volume_24h_usd": None}
                bv = m.BookVenue("temple", t.symbol, tuple((lv.price, lv.size) for lv in book.bids),
                                 tuple((lv.price, lv.size) for lv in book.asks), usdcx_usd,
                                 m.BOOK_TAKER_FEE["temple"])
                if k == CC:
                    cc, cc_book = info, bv
                    continue
                out[k] = {"temple": info}
                kept[k] = {"temple": bv}
            self.temple_priced, self.temple_cc_book = bool(out), cc_book is not None
            now = int(time.time())
            if self.temple_priced:  # the keyed read, stamped apart from the keyless settled volume
                self.sources["temple_book"] = now
            if self.temple_cc_book:  # the deepest CC/USDCx market: without it CC is priced by the pools
                self.sources["temple_cc_book"] = now
            self.temple_error = "; ".join(errors)[:200] or None
            return out, kept, cc, cc_book
        except Exception as exc:  # noqa: BLE001
            self.temple_error = f"{type(exc).__name__}: {exc}"[:200]  # a key that stops working is seen
            self._drop("temple", exc)
            return {}, {}, None, None

    def _metal(self, base: str, index: Decimal | None, now: int) -> Decimal | None:
        """Ekiden's index for a metal as its spot price, or None: market closed, no index, or an index
        that has not moved for ``METAL_STILL_MAX_S`` while the market trades."""
        if not index:
            return None
        last = self.metal_seen.get(base)
        if last is None or last[0] != index:
            self.metal_seen[base] = last = (index, now)
        if not m.metals_open(now) or now - last[1] > METAL_STILL_MAX_S:
            return None
        self.metal_spot[base] = index
        return index

    async def _perps(self, gecko, cc_usd, now: int | None = None) -> list[dict]:
        """Every perp market on Rocky and Ekiden, with its basis to the outside spot price."""
        rows = []
        now = now or int(time.time())
        self.metal_spot = {}

        def spot(base: str):
            if base == "CC":
                return float(cc_usd)
            if base in METALS:  # only Ekiden lists them; its own index, below
                return None
            return _gecko_usd(gecko, PERP_SPOT.get(base, ""), now)

        rocky = await self._venue("rocky_perp")
        if rocky is not None:
            try:
                markets = {mk.symbol: mk for mk in await rocky.markets()}
                read = []  # kept only once the whole read succeeds: a failure halfway shows the venue down
                for t in await rocky.tickers():
                    mk = markets.get(t.symbol)
                    if mk is None:
                        continue
                    book = await rocky.order_book(t.symbol, 20)
                    bid, ask = book.best_bid, book.best_ask
                    read.append(self._perp_row("rocky_perp", t.symbol, mk.base, mk.quote or "?", t.last_price,
                                               None, None, None, None, t.turnover_24h, bid, ask,
                                               spot(mk.base), None))
                rows += read
                self.fresh["rocky_perp"] = int(time.time())
            except Exception as exc:  # noqa: BLE001
                self._drop("rocky_perp", exc)
        ekiden = await self._venue("ekiden")
        if ekiden is not None:
            try:
                for t in await ekiden.tickers():
                    base, _, quote = t.symbol.partition("-")
                    # dollars resting within 1% of mid, the quote token counted at $1 like turnover
                    book = await ekiden.order_book(t.symbol, 200)
                    depth = (m.book_depth_usd(book.bids, book.asks, book.mid_price, Decimal(1))
                             if book.mid_price else None)
                    ref = self._metal(base, t.index_price, now) if base in METALS else spot(base)
                    rows.append(self._perp_row("ekiden", t.symbol, base, quote, t.last_price, t.mark_price,
                                               t.index_price, t.funding_rate, t.open_interest * t.mark_price,
                                               t.turnover_24h, t.best_bid, t.best_ask,
                                               float(ref) if ref is not None else None, depth))
                self.fresh["ekiden"] = int(time.time())
            except Exception as exc:  # noqa: BLE001
                self._drop("ekiden", exc)
        return sorted(rows, key=lambda x: -(x["turnover_24h_usd"] or 0))

    @staticmethod
    def _perp_row(venue, symbol, base, quote, last, mark, index, funding, oi_usd, turnover,
                  bid, ask, spot_usd, depth=None) -> dict:
        ref = mark if mark else last
        return {
            "venue": venue, "symbol": symbol, "base": base, "quote": quote,
            "last": r(last, 8), "mark": r(mark, 8), "index": r(index, 8),
            "spot_usd": r(spot_usd, 8),
            "basis": r(float(ref) / spot_usd - 1, 6) if spot_usd and ref else None,
            "funding_rate": r(funding, 8), "open_interest_usd": r(oi_usd, 2),
            "turnover_24h_usd": r(turnover, 2),
            "bid": r(bid.price, 8) if bid else None, "ask": r(ask.price, 8) if ask else None,
            "spread_bps": r((ask.price / bid.price - 1) * 10_000, 2) if bid and ask else None,
            "depth_1pct_usd": r(depth, 2) if depth is not None else None,
        }

    def _venue_usd(self, venue: str, pool, cc_usd) -> Decimal:
        """A token in a CC pool, in dollars: what it fetches in USDCx on the same venue, through that
        venue's CC/USDCx market (the route inside the venue), or at the market CC price on a venue
        without one. Valued at the market CC price instead, every stablecoin read the pools' CC gap to
        Temple's book as a peg gap (+0.5 to +0.75% on 2026-10-14, the pools 54 bp under the book)."""
        return self.cc_at.get(venue, cc_usd) / pool.mid

    # === pools that stopped trading ========================================

    def _still_since(self, books, now: int) -> dict[str, int]:
        """Every pool read this tick against its reserves at the last read: since when each has stood
        still. Returns the pools that have stood still for ``STILL_POOL_S`` or more ("venue:A/B" ->
        since). A pool not read this tick keeps its record; one unseen for a day is forgotten."""
        seen = {f"{v}:{CC}/{k}": f"{p.cc_reserve}/{p.token_reserve}" for k, pools in books.items()
                for v, p in pools.items()}
        seen |= {self._other_key(o): f"{o['ra']}/{o['rb']}" for o in self.other_pools}
        for key, fp in seen.items():
            prev = self.pool_still.get(key)
            since = prev[1] if prev and prev[0] == fp else now
            self.pool_still[key] = [fp, since, now]
        self.pool_still = {k: v for k, v in self.pool_still.items() if now - v[2] <= 86400}
        return {k: self.pool_still[k][1] for k in seen if now - self.pool_still[k][1] >= STILL_POOL_S}

    @staticmethod
    def _other_key(o: dict) -> str:
        return f"{o['venue']}:{m.key(o['a'])}/{m.key(o['b'])}"

    def _mark_still(self, venue: str, k: str, pair: str, since: int, gap: Decimal, now: int) -> None:
        self.still[(venue, k)] = {"pair": pair, "since": since, "gap": r(gap, 6)}
        log.info("%s %s not trading: still since %s, %+.2f%% off the market", venue, pair,
                 datetime.fromtimestamp(since, UTC).strftime("%H:%M"), float(gap) * 100)

    def _still_cc_markets(self, cc_markets: dict, candidates: dict[str, int], usdcx_usd, now: int) -> dict:
        """CC/USDCx markets without the pools that stood still while CC moved: each still pool against
        the CC price of the other markets. With no other market to judge by, nothing is marked."""
        still = {v: p for v, p in cc_markets.items()
                 if not isinstance(p, m.CCBook) and f"{v}:{CC}/{USDCX}" in candidates}
        rest = {v: p for v, p in cc_markets.items() if v not in still}
        if not still or not rest:
            return cc_markets
        ref = m.cc_price_usd(rest, usdcx_usd)
        out = dict(cc_markets)
        for v, p in still.items():
            gap = p.mid * usdcx_usd / ref - 1
            if abs(gap) >= STILL_POOL_GAP:
                self._mark_still(v, USDCX, f"CC/{p.token}", candidates[f"{v}:{CC}/{USDCX}"], gap, now)
                out.pop(v)
        return out

    def _still_pools(self, books, candidates: dict[str, int], cc_usd, ob, now: int) -> None:
        """CC pools of other tokens that stood still: each against the token's price on the venues
        that did not (other pools, order books). A token priced nowhere else cannot be judged."""
        for k, pools in books.items():
            if k == USDCX:
                continue
            for v, p in pools.items():
                key = f"{v}:{CC}/{k}"
                if key not in candidates:
                    continue
                quotes = [(self._venue_usd(w, q, cc_usd), m.amm_depth_usd(q, cc_usd)) for w, q in pools.items()
                          if w != v and f"{w}:{CC}/{k}" not in candidates]
                quotes += [(Decimal(str(i["price_usd"])), Decimal(str(i["depth_usd"]))) for i in ob.get(k, {}).values()]
                ref = m.weighted_usd(quotes)
                if ref:
                    gap = self._venue_usd(v, p, cc_usd) / ref - 1
                    if abs(gap) >= STILL_POOL_GAP:
                        self._mark_still(v, k, f"CC/{p.token}", candidates[key], gap, now)

    def _still_others(self, candidates: dict[str, int], prices, now: int) -> None:
        """Pools of two other tokens that stood still while their price ratio left the market's."""
        for o in self.other_pools:
            key, a, b = self._other_key(o), m.key(o["a"]), m.key(o["b"])
            if key in candidates and prices.get(a) and prices.get(b) and o["ra"] > 0:
                gap = (o["rb"] / o["ra"]) / (prices[a] / prices[b]) - 1
                if abs(gap) >= STILL_POOL_GAP:
                    o["still"] = {"pair": f"{o['a']}/{o['b']}", "since": candidates[key], "gap": r(gap, 6)}
                    log.info("%s %s/%s not trading: still since %s, %+.2f%% off the market", o["venue"],
                             o["a"], o["b"], datetime.fromtimestamp(candidates[key], UTC).strftime("%H:%M"),
                             float(gap) * 100)

    def _still_json(self) -> list[dict]:
        """Every pool marked not trading this tick: venue, pair, since when its reserves stood still,
        and how far its price sat off the market (a fraction)."""
        rows = [{"venue": v, **info} for (v, _), info in self.still.items()]
        rows += [{"venue": o["venue"], **o["still"]} for o in self.other_pools if o.get("still")]
        return sorted(rows, key=lambda x: (x["venue"], x["pair"]))

    def _prices(self, books, cc_usd, ob) -> dict[str, Decimal]:
        """Each token's Canton price across every venue that prices it, weighted by depth; a pool marked
        not trading weighs nothing (unless nothing else prices the token)."""
        out = {}
        for sym, pools in books.items():
            quotes = [(self._venue_usd(v, p, cc_usd), m.amm_depth_usd(p, cc_usd)) for v, p in pools.items()
                      if (v, sym) not in self.still]
            for info in ob.get(sym, {}).values():
                quotes.append((Decimal(str(info["price_usd"])), Decimal(str(info["depth_usd"]))))
            out[sym] = m.weighted_usd(quotes) or m.token_usd(list(pools.values()), cc_usd)
        return out

    def _fresh(self, source: str, now: int, limit: int = VOLUME_MAX_AGE_S) -> bool:
        return now - self.sources.get(source, 0) <= limit

    def _venues(self, cx_volume_usd, rocky_spot_vol, perps, now) -> dict:
        perp_vol: dict[str, float] = {}
        for p in perps:
            perp_vol[p["venue"]] = perp_vol.get(p["venue"], 0) + (p["turnover_24h_usd"] or 0)
        # a 24h volume read over an hour ago is unknown, not shown as current (it froze for days before
        # 2026-10-14 when one pool or market stopped answering)
        tc_vol = self.slow.get("tc_volume") if self._fresh("tc_volume", now) else None
        own = {"cantex": cx_volume_usd, "tradecraft": sum(tc_vol.values()) if tc_vol else None,
               "rocky": rocky_spot_vol, **perp_vol}
        temple_live = self._fresh("temple_volume", now, TEMPLE_MAX_AGE_S)
        own["temple"] = self.slow.get("temple_total") if temple_live else None
        if "poolparty" in self.live and self.slow.get("pp_volume") is not None:
            own["poolparty"] = self._pp_volume_usd(self.cc_usd_last)[1]
        rows = []
        for v in VENUES:
            vol = own.get(v["id"])
            live = (v["status"] != "priced" or v["id"] in self.live
                    or (v["id"] in CORE and v["id"] not in self.down))
            if v["id"] == "temple":  # Temple can read down too: its settled volume not read for 45 min
                live = temple_live
            status = v["status"] if live else "down"
            note = v["note"]
            if v["id"] == "temple" and self.temple_priced:
                status = "priced"
                note = "Order books from Temple's API (account key); settled volume per market. Taker fee 1 bp."
            rows.append({k: v[k] for k in ("id", "name", "kind")} | {
                "note": note, "status": status,
                "volume_24h_usd": r(vol, 2) if vol is not None else None})
            if v["id"] == "temple" and temple_live and self.slow.get("temple_volume"):
                # Temple's own list of markets that settled in 24h, so a page can say how many we price
                rows[-1]["markets_24h"] = sorted(self.slow["temple_volume"])
                # and each one's settled volume in its quote token, as Temple reports it
                rows[-1]["markets_24h_usd"] = {k: r(x, 2) for k, x in sorted(self.slow["temple_volume"].items())}
            if v["id"] == "rocky" and rocky_spot_vol is not None and self.rocky_books:
                # each spot book's turnover, in dollars the same way the venue total is
                rows[-1]["markets_24h_usd"] = {k: r(x, 2) for k, x in sorted(self.rocky_books.items())}
        spot = [x for x in rows if x["kind"] != "Perpetuals" and x["volume_24h_usd"]]
        total = sum(x["volume_24h_usd"] for x in spot)
        priced = sum(x["volume_24h_usd"] for x in spot if x["status"] == "priced")
        return {"t": now, "venues": rows, "spot_volume_24h_usd": r(total, 2),
                # our own hourly readings, a day apart (the home page's headline; no outside figure)
                "spot_volume_change_24h": r(venue_history.total_change(self.vhist, now)),
                "spot_priced_share": r(priced / total, 4) if total else None,
                "perp_volume_24h_usd": r(sum(perp_vol.values()), 2)}

    async def _gecko(self) -> dict:
        ids = sorted({g for g, _ in m.REFERENCES.values() if not g.startswith("spot:")}
                     | {USDCX_GECKO} | set(PERP_SPOT.values()))
        try:
            got = await self._json(GECKO, {"ids": ",".join(ids), "vs_currencies": "usd",
                                           "include_24hr_change": "true", "include_last_updated_at": "true"})
            # an answer without CC's or USDCx's price is no read: every premium and peg hangs on them
            usable = isinstance(got, dict) and all(
                isinstance((got.get(g) or {}).get("usd"), int | float) and math.isfinite(got[g]["usd"])
                for g in ("canton-network", USDCX_GECKO))
            if usable:
                self.sources["gecko"] = int(time.time())
                return got
            raise ValueError("an answer without CC's or USDCx's price")
        except Exception as exc:  # noqa: BLE001
            log.warning("coingecko: %s", exc)
            # the last answer stands in for a few minutes only; past that every reference is missing
            fresh = time.time() - self.sources.get("gecko", 0) <= GECKO_MAX_AGE_S
            return self.slow.get("gecko_last", {}) if fresh else {}

    # === building the books ================================================

    def _books(self, cx_states, tc_states, tc_pools, lp_share_cx):
        """symbol -> venue -> VenuePool, CC-paired pools only, matched by instrument. Starts this read's
        list of other pools (``other_pools``) with Tradecraft's pools of two tokens."""
        self.other_pools = []
        cc_inst = self._cc_instrument(cx_states)
        names: dict = {}
        for t in self.slow.get("tokens_info", []):
            names[(t["instrument_admin"], t["instrument_id"])] = t["instrument_symbol"]
        books: dict[str, dict[str, VenuePool]] = {}
        symbol_of: dict = {}

        for s in cx_states:
            if cc_inst not in (s.token_a, s.token_b):
                continue
            cc_is_a = s.token_a == cc_inst
            tok_inst, sym = (s.token_b, s.symbol_b) if cc_is_a else (s.token_a, s.symbol_a)
            sym = names.get((tok_inst.admin, tok_inst.id), sym)
            symbol_of[tok_inst] = sym
            cc_res, tok_res = (s.reserve_a, s.reserve_b) if cc_is_a else (s.reserve_b, s.reserve_a)
            if cc_res <= 0 or tok_res <= 0:
                continue

            pool = VenuePool("cantex", sym, cc_res, tok_res, s.fee, lp_share_cx,
                             m.FORMULAS["cantex"](cc_res, tok_res, s.fee), "cantex", s.fee)
            prev = books.setdefault(m.key(sym), {}).get("cantex")
            if prev is None or pool.cc_reserve > prev.cc_reserve:
                books[m.key(sym)]["cantex"] = pool
            if prev is not None:  # one pool per token is quoted; the other still holds liquidity
                self._unquoted("cantex", sym, min(prev.cc_reserve, pool.cc_reserve), SECOND_POOL)

        tc_inst = {p.contract_id: (p.token_a, p.token_b) for p in tc_pools}
        for st in tc_states:
            if CC not in (st.token_a, st.token_b):
                if st.amm_id in tc_inst and st.reserve_a > 0 and st.reserve_b > 0:
                    a, b = (symbol_of.get(i) or names.get((i.admin, i.id), s)
                            for i, s in zip(tc_inst[st.amm_id], (st.token_a, st.token_b)))
                    self._other("tradecraft", a, b, st.reserve_a, st.reserve_b, _tc_swap(st),
                                formula="tradecraft", fee=st.total_fee)
                continue
            tok_sym = st.token_b if st.token_a == CC else st.token_a
            cc_res, tok_res = st.reserves_for(CC, tok_sym)
            if cc_res <= 0 or tok_res <= 0:
                continue
            if st.amm_id not in tc_inst:  # no instrument to match the token by
                self._unquoted("tradecraft", tok_sym, cc_res, UNNAMED)
                continue
            ia, ib = tc_inst[st.amm_id]
            tok_inst = ib if st.token_a == CC else ia
            sym = symbol_of.get(tok_inst) or names.get((tok_inst.admin, tok_inst.id), tok_sym)
            prev = books.get(m.key(sym), {}).get("tradecraft")
            if prev is not None:
                self._unquoted("tradecraft", sym, min(prev.cc_reserve, cc_res), SECOND_POOL)
                if prev.cc_reserve >= cc_res:
                    continue

            share = st.lp_fee / st.total_fee if st.total_fee else Decimal(0)
            books.setdefault(m.key(sym), {})["tradecraft"] = VenuePool(
                "tradecraft", sym, cc_res, tok_res, st.realized_fee, share,
                m.FORMULAS["tradecraft"](cc_res, tok_res, st.total_fee), "tradecraft", st.total_fee)
        return books

    async def _reserve_venues(self, books, cx_states) -> None:
        """OneSwap and Pool Party CC pools into ``books``; each venue fails alone.

        OneSwap names issuers, so its tokens match by instrument. Pool Party names only instrument
        ids, so its tokens match by id, and only where that id is unambiguous across Cantex tokens.
        """
        cc_inst = self._cc_instrument(cx_states)
        by_inst, by_id = {}, {}
        for t in self.slow.get("tokens_info", []):
            by_inst[(t["instrument_admin"], t["instrument_id"])] = t["instrument_symbol"]
            by_id.setdefault(t["instrument_id"], set()).add(t["instrument_symbol"])
        self.slow["pp_pool_of"] = {}
        for venue in ("oneswap", "poolparty"):
            src = await self._venue(venue)
            if src is None:
                continue
            try:
                pools = await src.pools()
                if venue == "poolparty":
                    got = await src.volume()  # a pool whose volume will not parse keeps its last good one
                    vols, _ = self._volume_seen("pp", got, list(got))
                    self.slow["pp_volume"] = vols
                self.fresh[venue] = int(time.time())
            except Exception as exc:  # noqa: BLE001
                self._drop(venue, exc)
                continue
            def name(tok):
                if tok.admin:
                    return by_inst.get((tok.admin, tok.id))
                ids = by_id.get(tok.id, set())
                return next(iter(ids)) if len(ids) == 1 else None

            for p in pools:
                if cc_inst.id not in (p.token_a.id, p.token_b.id):
                    if p.reserve_a > 0 and p.reserve_b > 0:  # a pool of two other tokens
                        self._other(venue, name(p.token_a) or (p.symbol_a or "?")[:8], name(p.token_b) or (p.symbol_b or "?")[:8],
                                    p.reserve_a, p.reserve_b, p.output, named=bool(name(p.token_a) and name(p.token_b)),
                                    formula="cp", fee=p.fee)
                    continue
                cc_is_a = p.token_a.id == cc_inst.id
                if venue == "oneswap" and (p.token_a if cc_is_a else p.token_b) != cc_inst:
                    continue  # an "Amulet" from another issuer is not CC
                tok = p.token_b if cc_is_a else p.token_a
                if tok.admin:
                    sym = by_inst.get((tok.admin, tok.id))
                else:
                    names = by_id.get(tok.id, set())
                    sym = next(iter(names)) if len(names) == 1 else None
                cc_res, tok_res = (p.reserve_a, p.reserve_b) if cc_is_a else (p.reserve_b, p.reserve_a)
                if cc_res <= 0 or tok_res <= 0:  # listed before it is funded, or drained: no price
                    continue
                if sym is None:  # a token no venue we price can name: not quoted, still liquidity
                    self._unquoted(venue, ((p.symbol_b if cc_is_a else p.symbol_a) or "?")[:8], cc_res, UNNAMED)
                    continue
                prev = books.get(m.key(sym), {}).get(venue)
                if prev is not None:
                    self._unquoted(venue, sym, min(prev.cc_reserve, cc_res), SECOND_POOL)
                    if prev.cc_reserve >= cc_res:
                        continue
                # lp_share 0: neither venue publishes the LP cut, so no fee APR is shown for them
                books.setdefault(m.key(sym), {})[venue] = VenuePool(
                    venue, sym, cc_res, tok_res, p.fee, Decimal(0),
                    m.FORMULAS["cp"](cc_res, tok_res, p.fee), "cp", p.fee)
                self.slow.setdefault("pp_pool_of", {})[(venue, m.key(sym))] = p.pool_id

    def _pp_volume_usd(self, cc_usd) -> tuple[dict[str, float], float]:
        """Pool Party volume per CC-paired token key, and its total, in dollars.

        The total is the CC side of every CC pool (``Amulet`` legs, named token or not) at our CC
        price: Pool Party reports token amounts, not dollars. Its other legs (EDELx, USDC.B, ...)
        may be the other side of the same trades, so adding them could count a trade twice.
        """
        per_pool = self.slow.get("pp_volume") or {}
        total, by_key = 0.0, {}
        for name, vol in per_pool.items():
            total += float(vol.get("Amulet", 0)) * float(cc_usd)
        for (venue, key), pool_id in (self.slow.get("pp_pool_of") or {}).items():
            vol = per_pool.get(pool_id) or {}
            if venue == "poolparty" and "Amulet" in vol:
                by_key[key] = float(vol["Amulet"]) * float(cc_usd)
        return by_key, total

    def _other(self, venue: str, a: str, b: str, ra, rb, swap, named: bool = True, formula: str = "",
               fee=Decimal(0)) -> None:
        """A pool of two tokens other than CC: a market for the routes inside its venue (when both
        tokens are named) and liquidity on its venue (``_other_json``). ``formula`` and ``fee`` rebuild
        its pricing from pools.json (``m.FORMULAS``, ``m.venue_hops``)."""
        self.other_pools.append({"venue": venue, "a": a, "b": b, "ra": ra, "rb": rb, "formula": formula, "fee": fee,
                                 "hop": m.Hop(venue, m.key(a), m.key(b), rb / ra, swap) if named else None})

    @staticmethod
    def _other_tvl(p, prices) -> Decimal | None:
        """A pool of two other tokens at our prices: both sides when both are priced, twice the priced
        side when one is, None when neither is."""
        sides = [res * prices[m.key(t)] for t, res in ((p["a"], p["ra"]), (p["b"], p["rb"])) if m.key(t) in prices]
        return sum(sides) if len(sides) == 2 else 2 * sides[0] if sides else None

    def _other_json(self, prices) -> list[dict]:
        """Pools without CC (lp.json other_pools), valued by ``_other_tvl``."""
        rows = [{"venue": p["venue"], "pair": f"{p['a']}/{p['b']}", "reason": "pairs two tokens, not CC",
                 "tvl_usd": r(self._other_tvl(p, prices), 2),
                 **({"not_trading_since": p["still"]["since"]} if p.get("still") else {})} for p in self.other_pools]
        return sorted(rows, key=lambda x: -(x["tvl_usd"] or 0))

    def _unquoted(self, venue: str, token: str, cc_reserve, reason: str) -> None:
        if cc_reserve and cc_reserve > 0:
            self.unpriced.append({"venue": venue, "pair": f"CC/{token}", "cc_reserve": cc_reserve,
                                  "reason": reason})

    def _unpriced_json(self, cc_usd) -> list[dict]:
        return sorted(({"venue": p["venue"], "pair": p["pair"], "reason": p["reason"],
                        "tvl_usd": round(float(2 * p["cc_reserve"] * cc_usd), 2)} for p in self.unpriced),
                      key=lambda x: -x["tvl_usd"])

    def _usdcx(self, gecko: dict) -> Decimal:
        """USDCx in dollars from CoinGecko: a positive number, else the last good one (a null or a 0
        stopped every tick before 2026-10-14)."""
        got = _num((gecko.get(USDCX_GECKO) or {}).get("usd"))
        if got is not None and got > 0:
            self.usdcx_last = got
        return self.usdcx_last

    def _usd_series(self, now_ms: int, usdcx_usd: Decimal) -> dict[str, list]:
        """Hourly dollar series per token from Cantex candles (X-CC times CC-USDCX). A market whose last
        bar ended over ``m.CHANGE_MAX_AGE_MS`` ago is left out, so its token falls back to our own
        five-minute samples: a pool that stopped trading keeps its last candle for days."""
        fresh_read = time.time() - self.sources.get("candles", 0) <= VOLUME_MAX_AGE_S
        candles = (self.slow.get("candles") or {}) if fresh_read else {}
        base = next((v for k, v in candles.items() if k.upper() == "CC-USDCX"), None)
        fresh = lambda bars: bars and now_ms - (max(c.start_ms for c in bars) + 3_600_000) <= m.CHANGE_MAX_AGE_MS  # noqa: E731
        if not fresh(base):
            return {}
        # a close belongs to the END of its bar
        cc_at = {c.start_ms + 3_600_000: c.close for c in base}
        series = {CC: [(t, float(p * usdcx_usd)) for t, p in sorted(cc_at.items())]}
        for market, bars in candles.items():
            b, _, q = market.upper().rpartition("-")
            if q != CC or not b or not fresh(bars):
                continue
            pts = [(c.start_ms + 3_600_000, float(c.close * cc_at[c.start_ms + 3_600_000] * usdcx_usd))
                   for c in sorted(bars, key=lambda c: c.start_ms) if c.start_ms + 3_600_000 in cc_at]
            if pts:
                series[b] = pts
        return series

    # === one tick ==========================================================

    async def tick(self) -> None:
        """One tick. ``tick_no`` advances even when it fails: a failed slow tick used to repeat the slow
        refresh (40 s of candles, a call per Tradecraft pool) on every tick until it passed."""
        try:
            await self._tick()
        finally:
            self.tick_no += 1

    def _pair_fees(self, now: int) -> None:
        """Cantex's network fee per pair, as its own quotes named it (``<state>/quote_check.json``),
        while the record is fresh; otherwise every pair takes the venue's point (model.NETWORK_FEE)."""
        rec = load_json(self.state / "quote_check.json", {})
        m.PAIR_FEE_CC.clear()
        if now - rec.get("t", 0) <= PAIR_FEE_MAX_AGE_S:
            for tok, fee in (rec.get("cantex_fee_cc") or {}).items():
                if (d := _num(fee)) is not None and d > 0:
                    m.PAIR_FEE_CC[("cantex", frozenset((CC, m.key(tok))))] = d
            # OneSwap's differs by direction (carved from what is sold): "CC>USDCX", "USDCX>CC"
            for pair, fee in (rec.get("oneswap_fee_cc") or {}).items():
                frm, _, to = pair.partition(">")
                if to and (d := _num(fee)) is not None and d > 0:
                    m.PAIR_FEE_CC[("oneswap", frm, to)] = d

    async def _tick(self) -> None:
        started = time.monotonic()
        now = int(time.time())
        now_ms = now * 1000
        self._pair_fees(now)
        # A core venue that does not answer is left out of this tick: none of its prices are shown (an
        # older read is never passed off as current), its row says down, and the rest is published.
        cx_states, cx_volume, tickers = [], {}, []
        try:
            cx_states = await self.cantex.pool_states()
            cx_volume = await self.cantex.volume()
            tickers = await self.cantex.tickers()
            self.cx_stats = {"t": now, "volume": cx_volume, "tickers": tickers}
            self.fresh["cantex"] = now
            self.down.pop("cantex", None)
        except Exception as exc:  # noqa: BLE001
            self._core_down("cantex", now, exc)
            cx_states = []
            # its 24h volume and tickers are rolling day figures: the last read stands in for an hour
            if self.cx_stats and now - self.cx_stats["t"] <= CX_STATS_MAX_AGE_S:
                cx_volume, tickers = self.cx_stats["volume"], self.cx_stats["tickers"]
        try:
            tc_states = await self.tradecraft.pool_states()
            tc_pools = await self.tradecraft.pools()
            self.down.pop("tradecraft", None)
        except Exception as exc:  # noqa: BLE001
            self._core_down("tradecraft", now, exc)
            tc_states, tc_pools = [], []
        self.unpriced = []
        slow = self.tick_no % SLOW_EVERY == 0 or not self.slow
        if slow:
            markets = []
            if "cantex" not in self.down:
                try:
                    markets = await self.cantex.markets()
                except Exception as exc:  # noqa: BLE001
                    log.warning("cantex markets: %s", exc)
            await self._refresh_slow(tc_states, markets)
        gecko = await self._gecko()
        if gecko:
            self.slow["gecko_last"] = gecko

        fees_cc, lp_fees_cc = _num(cx_volume.get("fees_cc")), _num(cx_volume.get("lp_fees_cc"))
        lp_share_cx = lp_fees_cc / fees_cc if fees_cc and lp_fees_cc is not None else Decimal("0.9")
        books = self._books(cx_states, tc_states, tc_pools, lp_share_cx)
        await self._reserve_venues(books, cx_states)
        candidates = self._still_since(books, now)
        usdcx_usd = self._usdcx(gecko)
        stable = books.get(USDCX)
        if not stable:
            raise RuntimeError("no CC/USDCx pool on any venue")
        t_ob, t_books, _, temple_cc_book = await self._temple_spot(books, usdcx_usd)
        # Temple's CC/USDCx book is one of the CC/USDCx markets: it weighs in the CC price by its depth
        # (as every token's price weighs its venues) and joins every CC/USDCx comparison (best
        # execution, the scanner, the paper router, the dollar routes' CC leg); the token list and the
        # pool tables stay the pools'
        cc_markets = stable
        if temple_cc_book is not None and temple_cc_book.bids and temple_cc_book.asks:
            cc_markets = {**stable, "temple": m.CCBook("temple", next(iter(stable.values())).token, temple_cc_book)}
        self.still = {}
        cc_markets = self._still_cc_markets(cc_markets, candidates, usdcx_usd, now)
        cc_usd = m.cc_price_usd(cc_markets, usdcx_usd)
        self.cc_usd_last = cc_usd
        # a venue's tokens are valued through its own CC/USDCx market only when that market holds $1K or
        # more: a $47 pool 20% off moved every price on its venue 20% (replay, 2026-10-15)
        self.cc_at = {v: p.mid * usdcx_usd for v, p in cc_markets.items()
                      if isinstance(p, m.CCBook) or 2 * p.cc_reserve * cc_usd >= MIN_MARKET_USD}

        ob, ob_books, rocky_spot_vol = await self._rocky_spot(books, cc_usd, usdcx_usd)
        for k, v in t_ob.items():
            ob.setdefault(k, {}).update(v)
        for k, v in t_books.items():  # Temple's token books, quoted in USDCx, in Rocky's shape
            for b in v.values():
                ob_books.setdefault(k, {})[b.symbol] = (b, USDCX)
        perps = await self._perps(gecko, cc_usd, now)
        self._still_pools(books, candidates, cc_usd, ob, now)
        # the comparisons leave out every pool marked not trading (its rows still list it)
        cmp_books = {k: {v: p for v, p in pools.items() if (v, k) not in self.still}
                     for k, pools in {**books, USDCX: cc_markets}.items()}
        prices = self._prices(books, cc_usd, ob)
        self._still_others(candidates, prices, now)
        hops = self._hops(books, cc_markets, ob_books, prices)

        series = self._usd_series(now_ms, usdcx_usd)
        self._mark_steps(now)
        self._record(now, cc_usd, prices)
        own = self._own_series()

        tokens = self._tokens(books, cc_usd, tickers, series, own, now_ms, ob, prices)
        premium = self._premium(books, cc_usd, gecko, ob, prices, now, quiet=m.idle(tokens))
        dollar = self._dollar_routes(books, hops, ob_books, prices, usdcx_usd)
        cc_pairs = self._execution(self._cc_routes(books, hops), cc_usd, prices)
        # CC/USDCx stays the default view; order-book tokens in dollars come right after it
        execution = cc_pairs[:1] + self._usd_execution(books, dollar, prices, cc_usd) + cc_pairs[1:]
        scan = m.scan({k: v for k, v in cmp_books.items() if len(v) > 1}, cc_usd)
        for k, routes in dollar.items():
            scan += m.usd_scan(k, routes, cc_usd)
        scan.sort(key=lambda x: -x["net_usd"])
        if not self.down:  # the paper desk trades only on a full view of the venues
            self._paper(cmp_books, cc_usd, cc_usd / prices[USDCX], scan, now)
        lp = self._lp(books, cc_usd, tickers, tc_states)
        summary = self._summary(cc_usd, cx_volume, gecko, series, now_ms, books, now,
                                time.monotonic() - started)
        venues = self._venues(summary["cantex_24h"]["volume_usd"], rocky_spot_vol, perps, now)

        write_json(self.api / "tokens.json", {"t": now, "tokens": tokens})
        write_json(self.api / "premium.json", {"t": now, "assets": premium})
        write_json(self.api / "execution.json", {"t": now, "sizes_usd": list(m.SIZES_USD),
                                                  "network_fee": m.fee_table_json(), "pairs": execution})
        try:
            # an hour without a core venue is left unrecorded: it would credit its rivals with that hour
            if not self.down:
                exec_history.record(self.state / "exec", now, execution, m.fee_table_json())
        except Exception:  # noqa: BLE001
            log.exception("execution history")
        # each route carries its own cost_cc: every venue's network fee, plus the CC/USDCx leg's on a
        # route through a pool
        write_json(self.api / "scan.json", {"t": now,
                                            "swap_cost_cc": {v: float(m.swap_cost_cc(v, cc_usd)) for v in
                                                             ("cantex", "tradecraft", "oneswap", "poolparty", "rocky",
                                                              "temple")},
                                            "min_usd": float(m.MIN_ROUTE_USD), "routes": scan})
        write_json(self.api / "desk.json", self.desk)
        write_json(self.state / "pools_still.json", self.pool_still)
        write_json(self.api / "lp.json", {"t": now, "pools": lp, "unpriced_pools": self._unpriced_json(cc_usd),
                                          "other_pools": self._other_json(prices)})
        write_json(self.api / "pools.json", {
            "t": now, "cc_usd": float(cc_usd),
            "pools": {k: {v: m.pool_to_json(p) for v, p in pools.items()} for k, pools in books.items()},
            # every dollar-quoted book by token and market (Rocky's in USDCx and USDC.B, Temple's in
            # USDCx); CC's own book on Temple sits under CC, so the MCP quote prices CC/USDCx on it too
            "books": {k: {name: m.book_to_json(b, q) for name, (b, q) in vs.items()} for k, vs in ob_books.items()}
            | ({CC: {temple_cc_book.symbol: m.book_to_json(temple_cc_book, USDCX)}}
               if temple_cc_book is not None else {}),
            # pools of two other tokens a route can take (named, priced the venue's way: m.venue_hops)
            "other_pools": [{"venue": o["venue"], "a": m.key(o["a"]), "b": m.key(o["b"]), "ra": str(o["ra"]),
                             "rb": str(o["rb"]), "formula": o["formula"], "fee": str(o["fee"]),
                             "usd": r(self._other_tvl(o, prices), 2)}
                            for o in self.other_pools if o["hop"] is not None and o["formula"] and not o.get("still")
                            and (self._other_tvl(o, prices) or 0) >= MIN_MARKET_USD],
            # pools whose reserves stood still while their price left the market: listed above, priced
            # by nothing, in no route
            "not_trading": self._still_json()})
        write_json(self.api / "history.json", self.history)
        write_json(self.api / "venues.json", venues)
        write_json(self.api / "perps.json", {"t": now, "markets": perps})
        pages = {"venues": venues, "tokens": {"t": now, "tokens": tokens},
                 "execution": {"t": now, "pairs": execution},
                 "lp": {"t": now, "pools": lp, "unpriced_pools": self._unpriced_json(cc_usd),
                        "other_pools": self._other_json(prices)},
                 "perps": {"t": now, "markets": perps}, "summary": summary}
        try:  # one hourly reading of every venue's figures, for its page's charts
            live = {s: f for s, f in venue_pages.facts(pages).items() if f["status"] in venue_pages.LIVE}
            # written on every slow refresh too, so the weekly card (weekly.py, its own timer) reads the
            # daily record within minutes of a UTC day closing, not up to an hour later
            if venue_history.record_hourly(self.vhist, now, live) or slow:
                write_json(self.api / "venue_history.json", self.vhist)
        except Exception:  # noqa: BLE001
            log.exception("venue history")
        pages["history"] = self.vhist
        try:  # a page per venue, its share card redrawn every few ticks
            venue_pages.build(self.out, pages, now, cards=self.tick_no % SLOW_EVERY == 0, guard=self.guard,
                              fresh=self.fresh)
        except Exception:  # one bad page must not fail the tick
            log.exception("venue pages")
        if not self.down:  # alerts and the channel's post of the day wait for a tick with every core venue
            await self._alerts(tokens, premium, scan, now)
            await self._morning(tokens)
        # the tick's heartbeat, written last: a fresh ``t`` means every file above was written (the
        # watchdog reads it; a tick that failed earlier leaves the last one, then marks it an error)
        write_json(self.api / "summary.json", summary)
        log.info("tick %d: %d tokens, %d on 2 venues, %d routes clear, %.1fs", self.tick_no + 1,
                 len(tokens), sum(len(v) > 1 for v in books.values()),
                 sum(s["clears"] for s in scan), time.monotonic() - started)

    # === alerts ============================================================

    def _price_hour_ago(self, now: int) -> dict[str, float]:
        h = self.history
        idx = next((i for i in range(len(h["t"]) - 1, -1, -1) if h["t"][i] <= now - 3000), None)
        if idx is None or h["t"][idx] < now - 4200:
            return {}  # no sample between 50 and 70 minutes ago
        return {k: col[idx] for k, col in h["usd"].items() if idx < len(col) and col[idx]}

    async def _alerts(self, tokens, premium, scan, now) -> None:
        path = self.api / "alerts.json"
        state = load_json(path, {})
        for feed in (state.get("feed") or [], (state.get("channel") or {}).get("feed") or []):
            gone = [x for x in feed if (x.get("t"), x.get("key")) in WITHDRAWN_ALERTS]
            for x in gone:  # once: the next write no longer has them
                feed.remove(x)
                log.info("alert withdrawn from the feed: %s %s", x["t"], x["key"])
        current = al.evaluate(tokens, premium, scan, self._price_hour_ago(now))
        if "active" not in state:  # first run: learn what is already true, send nothing
            state["active"] = sorted({a.key for a in current})
            state.setdefault("feed", [])
            write_json(path, state)
            return
        al.fire(state, current, now)  # the site's feed: every alert
        for k in ("pending", "next_batch"):  # the 4-hour batches, before 2026-10-08
            state.pop(k, None)
        if "channel" not in state:  # the channel's first run: what is already loud is not news
            state["channel"] = {"active": sorted(a.key for a in current if a.loud)}
        # every loud alert is a price claim: none in the channel without a passing quote check, and a
        # spread only between two venues whose prices are checked (channel.checked_venues)
        checked = channel.checked_venues(load_json(self.api / "quotecheck.json", None), now)
        allowed = {f"route:{r['token']}:{r['buy_on']}" for r in scan
                   if r.get("clears") and {r["buy_on"], r["sell_on"]} <= checked}
        loud = al.channel(state.setdefault("channel", {}),
                          [a for a in current if a.kind != "route" or a.key in allowed], now) if checked else []
        write_json(path, state)  # recorded before sending: a failed send never duplicates
        if loud:
            await self._telegram(al.channel_html(loud))

    async def _morning(self, tokens) -> None:
        """The channel's post of the day (channel.py): its kind follows the weekday."""
        path = self.state / "channel.json"
        state = load_json(path, {})
        now = datetime.now(UTC)
        if not state:  # first run: start with tomorrow's post, not one at whatever hour this deploys
            write_json(path, {"date": now.date().isoformat(), "posts": []})
            return
        checked = channel.checked_venues(load_json(self.api / "quotecheck.json", None), now.timestamp())
        kind = channel.due(state, now, checked)
        if kind is None:
            return
        t = int(now.timestamp())
        post = None
        try:
            if kind == "weekly":
                post = channel.weekly_post(load_json(self.api / "weekly.json", None), now)
            elif kind == "venue":
                card_v = {p.parent.name: int(p.stat().st_mtime) for p in (self.out / "venues").glob("*/card.png")}
                post, state["venue_turn"] = channel.venue_post(
                    load_json(self.out / "venues" / "published.json", {}), state.get("venue_turn", 0), card_v, checked)
            elif kind == "execution":
                post = channel.execution_post(channel.read_exec(self.state / "exec", t), checked)
            elif kind == "pegs":
                post = channel.pegs_post(self.history, tokens, {x["key"]: x["symbol"] for x in tokens}, t)
        except Exception:  # noqa: BLE001
            log.exception("channel post %s", kind)
        state["date"] = now.date().isoformat()
        state["posts"] = (state.get("posts", []) + [{"t": t, "kind": kind, "sent": post is not None}])[-60:]
        write_json(path, state)  # recorded before sending: a failed send is not retried into a duplicate
        if post is None:
            log.info("channel: nothing to post for %s today", kind)
            return
        await self._telegram(post.html, post.photo)

    async def _telegram(self, body: str, photo: str | None = None) -> None:
        token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
        if not (token and chat):
            return
        try:
            # the URL carries the bot token: never log it (httpx is at WARNING)
            if photo:  # Telegram fetches the image from our site; the text goes as its caption
                resp = await self.http.post(f"https://api.telegram.org/bot{token}/sendPhoto", data={
                    "chat_id": chat, "photo": photo, "caption": body, "parse_mode": "HTML"})
            else:
                resp = await self.http.post(f"https://api.telegram.org/bot{token}/sendMessage", data={
                    "chat_id": chat, "text": body, "parse_mode": "HTML",
                    "disable_web_page_preview": "true"})
            if resp.status_code != 200:
                log.warning("telegram: HTTP %s", resp.status_code)
        except httpx.HTTPError as exc:
            log.warning("telegram: %s", type(exc).__name__)

    # === sections ==========================================================

    def _mark_steps(self, now: int) -> None:
        """Record each method step (``STEPS``) once, at the first tick computed the new way. A step with
        ``drops_since`` blanks every token's samples but CC's from that earlier step's time on."""
        h = self.history
        steps = h.setdefault("steps", [])
        for s in STEPS:
            if s["id"] in {x["id"] for x in steps}:
                continue
            since = next((x["t"] for x in steps if x["id"] == s.get("drops_since")), None)
            if since is not None:
                for series in (h["usd"], h["premium"]):
                    for k, col in series.items():
                        if k != CC:
                            series[k] = [None if t >= since else v for t, v in zip(h["t"], col)] + col[len(h["t"]):]
            steps.append({**s, "t": now})

    def _record(self, now: int, cc_usd: Decimal, prices) -> None:
        h = self.history
        h["t"].append(now)
        n = len(h["t"])
        usd = {CC: float(cc_usd)}
        for sym, price in prices.items():
            usd[sym] = float(price)
        for sym in set(h["usd"]) | set(usd):
            col = h["usd"].setdefault(sym, [None] * (n - 1))
            col.append(r(usd.get(sym), 8))
        cut = next((i for i, t in enumerate(h["t"]) if t >= now - HISTORY_KEEP_S), 0)
        if cut:
            h["t"] = h["t"][cut:]
            for k in h["usd"]:
                h["usd"][k] = h["usd"][k][cut:]
            for k in h["premium"]:
                h["premium"][k] = h["premium"][k][cut:]

    def _own_series(self) -> dict[str, list]:
        h = self.history
        return {sym: [(t * 1000, v) for t, v in zip(h["t"], col) if v is not None]
                for sym, col in h["usd"].items()}

    def _tokens(self, books, cc_usd, tickers, series, own, now_ms, ob, prices) -> list[dict]:
        vol_cx: dict[str, float] = {}
        for t in tickers:
            base, target = m.key(t["base_currency"]), m.key(t["target_currency"])
            if target == CC:
                vol_cx[base] = vol_cx.get(base, 0) + float(_num(t.get("target_volume")) or 0) * float(cc_usd)
            elif base == CC:
                vol_cx[target] = vol_cx.get(target, 0) + float(_num(t.get("base_volume")) or 0) * float(cc_usd)
        # a volume past its age limit is unknown here too, as in venues.json (it stood in for as long as
        # the read failed): a token on that venue then shows no "not traded"
        now = now_ms // 1000
        tc_live = self._fresh("tc_volume", now)
        temple_live = self._fresh("temple_volume", now, TEMPLE_MAX_AGE_S)
        tc_vol = self.slow.get("tc_volume", {}) if tc_live else {}
        pp_vol, _ = self._pp_volume_usd(cc_usd)
        # Temple quotes everything in USDCx, which the settled-volume route already reports in dollars.
        # Its CC/USDCx market is credited to USDCx, as every CC/USDCx pool's volume is
        temple_vol: dict[str, float] = {}
        for s, v in ((self.slow.get("temple_volume") or {}) if temple_live else {}).items():
            base, _, quote = s.partition("/")
            if quote == "USDCx":
                k = m.key(quote) if m.key(base) == CC else m.key(base)
                temple_vol[k] = temple_vol.get(k, 0.0) + v
        out = []
        for sym, pools in books.items():
            price = prices[sym]
            venues = {}
            for v, p in pools.items():
                venues[v] = {"price_usd": r(self._venue_usd(v, p, cc_usd), 8),
                             "liquidity_usd": r(2 * p.cc_reserve * cc_usd, 2),
                             "depth_1pct_usd": r(m.amm_depth_usd(p, cc_usd), 2),
                             **({"not_trading_since": st["since"]} if (st := self.still.get((v, sym))) else {})}
            vol = vol_cx.get(sym, 0.0) + pp_vol.get(sym, 0.0) + temple_vol.get(sym, 0.0)
            for v, info in ob.get(sym, {}).items():
                # a book's liquidity is what rests within 1% of mid, not a pool's full reserves
                venues[v] = {"price_usd": info["price_usd"], "liquidity_usd": info["depth_usd"],
                             "depth_1pct_usd": info["depth_usd"], "market": info["symbol"],
                             "spread_bps": info["spread_bps"],
                             # depth summed over several books (Rocky's CBTC: USDCx and USDC.B), the
                             # deepest named in ``market``: said, so no page names one book for the sum
                             **({"books": info["books"]} if (info.get("books") or 1) > 1 else {})}
                vol += info.get("volume_24h_usd") or 0  # a book with no ticker row yet has none
            if "tradecraft" in pools:
                vol += sum(v for k, v in tc_vol.items()
                           if k.upper().replace("TC ", "").replace(" LP", "") in
                           (f"CC/{sym}", f"{sym}/CC"))
            src = series.get(sym) or own.get(sym) or []
            week = [(t, v) for t, v in src if t >= now_ms - 7 * 86400_000]
            if sym not in series:  # own samples every 5 min: the last of each hour, so 168 points span
                hourly: dict[int, tuple] = {}  # the week (its last 168 samples were 14 hours)
                for t, v in week:
                    hourly[t // 3_600_000] = (t, v)
                week = list(hourly.values())
            spark = [round(v, 8) for t, v in week][-168:]
            unknown = ("oneswap" in venues or ("tradecraft" in venues and not tc_live)
                       or ("temple" in venues and not temple_live))
            traded = None if vol == 0 and unknown else vol > 0
            # under $1K traded in 24h its price is CC's times a frozen pool ratio: a 24h "move" would be
            # CC's (USDM1 -6.6%, six $20 pools -7.1% on 2026-10-15, CC -12.5%), so none is published
            quiet = traded is False or (traded and vol < m.MIN_TRADED_USD)
            out.append({
                "symbol": pools[next(iter(pools))].token,
                "key": sym,
                "image": self.slow.get("images", {}).get(sym),
                "price_usd": r(price, 8),
                "liquidity_usd": r(sum(x["liquidity_usd"] for x in venues.values()), 2),
                "volume_24h_usd": r(vol, 2),
                # False when it traded nowhere in 24h (OneSwap publishes no volume, so a token there is
                # unknown): its price is then CC's times a frozen ratio, and alerts and the pegs post
                # skip it (USDM1's pool did not trade for 8 days while CC walked it "off its peg")
                "traded_24h": traded,
                "change_24h": None if quiet else r(m.change(src, now_ms, 86400_000)),
                "change_7d": r(m.change(src, now_ms, 7 * 86400_000)),
                "history_source": "cantex candles" if sym in series else "own samples",
                "venues": venues,
                "spark_7d": spark,
            })
        return sorted(out, key=lambda t: -(t["liquidity_usd"] or 0))

    def _premium(self, books, cc_usd, gecko, ob, prices, now: int | None = None,
                 quiet: set[str] = frozenset()) -> list[dict]:
        """Each token against its outside price or its $1 peg. A ``quiet`` token (under $1K traded in
        24h, ``m.idle``) shows no figure: its price only follows CC through a frozen pool ratio (USX and
        USDM1 read -4% "off their peg" on 2026-10-15 having traded $252 and $132)."""
        rows = []
        h = self.history
        n = len(h["t"])
        now = now or int(time.time())

        def keep(sym, val):
            col = h["premium"].setdefault(sym, [None] * (n - 1))
            col.extend([None] * (n - 1 - len(col)))
            col.append(r(val, 6))

        candidates = [(CC, None)] + [(s, p) for s, p in books.items()]
        for sym, pools in candidates:
            missing = "no reference price"
            if sym in m.REFERENCES:
                gid, label = m.REFERENCES[sym]
                if gid.startswith("spot:"):  # a metal's spot price, while its market trades (_metal)
                    ref = self.metal_spot.get(gid[5:])
                    if not m.metals_open(now):
                        missing = "spot market closed"
                else:  # a CoinGecko price, while it is fresh
                    ref = _gecko_usd(gecko, gid, now)
                kind = "reference"
            elif sym in m.NO_SPOT:
                rows.append({"key": sym, "kind": "reference", "reference": m.NO_SPOT[sym],
                             "status": "no spot price connected"})
                continue
            elif sym in m.STABLES:
                gid, label, ref, kind = None, "1.00 peg", 1.0, "peg"
            else:
                continue
            if sym != CC and sym in quiet:
                rows.append({"key": sym, "symbol": next(iter(pools.values())).token, "kind": kind,
                             "reference": label, "status": "traded under $1K in 24h"})
                continue
            canton = cc_usd if sym == CC else prices[sym]
            if ref is None:
                rows.append({"key": sym, "kind": kind, "reference": label, "status": missing})
                continue
            prem = m.premium(canton, Decimal(str(ref)))
            per_venue = {}
            if pools:
                for v, p in pools.items():
                    if (v, sym) in self.still:  # not trading: its price is no Canton price
                        continue
                    pv = m.premium(self._venue_usd(v, p, cc_usd), Decimal(str(ref)))
                    per_venue[v] = r(pv, 6)
            for v, info in ob.get(sym, {}).items():
                per_venue[v] = r(m.premium(Decimal(str(info["price_usd"])), Decimal(str(ref))), 6)
            if sym == CC:  # each CC/USDCx market on its own: the pools' gap to Temple's book shows here
                per_venue = {v: r(m.premium(px, Decimal(str(ref))), 6) for v, px in self.cc_at.items()}
            keep(sym, prem)
            rows.append({
                "key": sym,
                "symbol": next(iter(pools.values())).token if pools else CC,
                "kind": kind,
                "reference": label,
                "reference_id": gid,
                "canton_usd": r(canton, 8),
                "reference_usd": r(ref, 8),
                "premium": r(prem, 6),
                "premium_by_venue": per_venue,
                "status": "ok" if prem is not None else "unit mismatch, excluded",
            })
        for k, col in h["premium"].items():
            col.extend([None] * (n - len(col)))
        return rows

    def _hops(self, books, cc_markets, ob_books, prices) -> dict[str, list]:
        """Every market each venue has this tick, as ``m.Hop``s: its CC pools (Temple's CC book among
        them), its order books, and its pools of two other tokens holding $1K or more at our prices (a
        $353 Tradecraft USDCx/CBTC pool, its price far off, read as a $49 spread on a $10 trade). Each
        pool carries its liquidity, so a route of two swaps skips one under the floor too."""
        hops: dict[str, list] = {}
        for k, pools in {**books, USDCX: cc_markets}.items():
            for v, p in pools.items():
                if (v, k) in self.still:  # not trading: no market for any route
                    continue
                # a CC pool is as deep as its thinner side: sized by CC alone, a pool drained of its token
                # passed the $1K floor and paid out its whole CC side for $1 (re-audit 2026-10-15)
                usd = p.depth_usd() if isinstance(p, m.CCBook) else 2 * min(
                    p.cc_reserve * self.cc_usd_last,
                    p.token_reserve * prices[k] if prices.get(k) else p.cc_reserve * self.cc_usd_last)
                hops.setdefault(v, []).append(dataclasses.replace(m.pool_hop(p), usd=usd))
        for p in self.other_pools:
            if p["hop"] is not None and not p.get("still") and (tvl := self._other_tvl(p, prices) or 0) >= MIN_MARKET_USD:
                hops.setdefault(p["venue"], []).append(dataclasses.replace(p["hop"], usd=tvl))
        for k, bs in ob_books.items():
            for b, quote in bs.values():
                hops.setdefault(b.venue, []).append(m.book_hop(b, k, quote))
        return hops

    def _cc_routes(self, books, hops) -> dict[str, dict]:
        """For each token: every venue's best route between CC and it, one or two swaps on its own
        markets (``m.Route``), so a venue without a CC pool for the token still competes."""
        out = {}
        for sym, pools in books.items():
            label = next(iter(pools.values())).token
            routes = {}
            for v, hs in hops.items():
                paths = m.venue_paths(hs, CC, sym, MIN_MARKET_USD)
                if paths:
                    routes[v] = m.Route(v, label, CC, sym, tuple(paths),
                                        pools[v].cc_reserve if v in pools else Decimal(0))
            out[sym] = routes
        return out

    def _dollar_routes(self, books, hops, ob_books, prices, usdcx_usd) -> dict:
        """For each token with an order book somewhere: every venue's best route from a dollar token to
        it on its own markets. USDC.B counts as USDCx while it holds within 1% of $1: the same dollar,
        valued alike (at $1 against USDCx at CoinGecko's ~0.9996 it made phantom round trips)."""
        dollar = {USDCX: usdcx_usd}
        if "USDC.B" in prices and abs(prices["USDC.B"] - 1) <= Decimal("0.01"):
            dollar["USDC.B"] = usdcx_usd
        out = {}
        for sym in sorted(ob_books):
            label = next(iter(books[sym].values())).token
            routes = {v: [m.Route(v, label, d, sym, tuple(paths)) for d in dollar
                          if (paths := m.venue_paths(hs, d, sym, MIN_MARKET_USD))] for v, hs in hops.items()}
            out[sym] = m.DollarRoutes(routes, dollar)
        return out

    def _execution(self, routes, cc_usd, prices) -> list[dict]:
        """A CC pair per token two or more venues can trade with CC, each by its best route. A dollar
        size becomes token amounts at the market prices of CC and the token (the prices every other
        figure uses): at the pools' blend by CC reserve, which Temple's book does not weigh in, a
        "$50K" buy spent 52.2K USDCx and Temple "cost" 4.4% on 2026-10-15, the pools 4.3% over it."""
        out = []
        for sym, pools in routes.items():
            if len(pools) < 2:
                continue
            mid = cc_usd / prices[sym] if prices.get(sym) else m.blended_mid(list(pools.values()))
            rows = m.ladder(pools, cc_usd, mid)
            out.append({
                "key": sym,
                "kind": "cc",
                "venues": sorted(pools),
                "symbol": next(iter(pools.values())).token,
                "mid": {v: r(p.mid, 12) for v, p in pools.items()},
                # token per CC at market prices: what sizes are converted at and cost_bps is against
                "market_mid": r(mid, 12),
                # venues whose pool for this pair is marked not trading: left out of the comparison
                **({"not_trading": nt} if (nt := {v: st for (v, k), st in self.still.items() if k == sym}) else {}),
                # a venue that trades this token on an order book (Temple's CC/USDCx, its CBTC/USDCx
                # after a CC/USDCx order), as a dollar pair names it
                **({"books": bk} if (bk := {v: {"market": b.market, "fee": float(b.fee)}
                                            for v, p in pools.items() if (b := _book_of(p))}) else {}),
                "rows": rows,
                "crossover_usd": {s: m.crossover_usd(rows, s, "cantex", "tradecraft")
                                  for s in ("sell", "buy")},
            })
        return sorted(out, key=lambda p: (p["key"] != USDCX, p["key"]))

    def _usd_execution(self, books, dollar, prices, cc_usd) -> list[dict]:
        """Tokens that also trade on an order book: every venue against dollars, by its best route."""
        out = []
        for sym, routes in sorted(dollar.items()):
            if len(routes.venues) < 2:
                continue
            rows = m.usd_ladder(routes, prices[sym], cc_usd)
            out.append({
                "key": sym + ":USD", "kind": "usd", "token": sym,
                "symbol": next(iter(books[sym].values())).token,
                "venues": routes.venues,
                "books": {v: {"market": h.market, "fee": float(h.fee)} for v, h in routes.books.items()},
                **({"not_trading": nt} if (nt := {v: st for (v, k), st in self.still.items() if k == sym}) else {}),
                "rows": rows,
            })
        return out

    def _paper(self, books, cc_usd, mid, scan, now) -> None:
        """The paper desk: one $1K router order a tick (``mid``: USDCx per CC at the market price) and
        every clearing spread booked once."""
        router = self.desk["router"]
        if router.get("v") != 4:
            # v1 (to 2026-10-08) picked and scored fills before network fees; v2 nets each venue's
            # fee (model.NETWORK_FEE) first; v3 (2026-10-14) starts after Temple's CC/USDCx book joined
            # the routing (348 of the 400 v2 fills shown at 15:25 UTC were made without it); v4
            # (2026-10-15) sizes a "$1K" buy at the market CC price (v3 spent ~4% more, at the pools'
            # blend). The record restarts at each change.
            router = self.desk["router"] = {"v": 4, "fills": [], "n": 0, "extra_usd": 0.0, "by_venue": {}}
        pools = books.get(USDCX, {})
        side = "sell" if router["n"] % 2 == 0 else "buy"
        fill = m.route_order(pools, side, m.ROUTER_SIZE_USD, cc_usd, mid) if pools else None
        if fill:
            fill["t"] = now
            router["n"] += 1
            router["extra_usd"] = round(router["extra_usd"] + fill["extra_usd"], 4)
            router["by_venue"][fill["venue"]] = router["by_venue"].get(fill["venue"], 0) + 1
            router.setdefault("since", now)
            router["fills"] = (router["fills"] + [fill])[-DESK_KEEP:]
            router.update(m.router_stats(router["fills"]))

        arb = self.desk["arb"]
        if arb.get("v") != 4:
            # v1 re-booked a standing spread whenever either pool moved at all; v2 (to 2026-10-04)
            # valued Rocky's USDC.B book at the pool premium and booked trips that did not exist; v3
            # (to 2026-10-15) paid USDCx and got USDC.B back at par (54 phantom trips, $221.81 booked).
            # The record restarts at each fix.
            arb = self.desk["arb"] = {"v": 4, "trades": [], "pnl_usd": 0.0, "open": [], "since": now}
        clearing = {f'{r["token"]}:{r["buy_on"]}>{r["sell_on"]}': r for r in scan if r["clears"]}
        # one spread per pool per tick, the best first: four trips selling into the same $6.2K OneSwap
        # pool were booked at once on 14.10 (42 trips, $49.12 on pools a better trip had already used)
        used: set = set()
        for k, route in sorted(clearing.items(), key=lambda kv: -kv[1]["net_usd"]):
            pools = _trip_pools(route)
            if pools & used:
                continue
            used |= pools
            if k in arb["open"]:
                continue  # still the same spread as last tick; booked once, when it opened
            arb["pnl_usd"] = round(arb["pnl_usd"] + route["net_usd"], 4)
            arb["trades"] = (arb["trades"] + [{**route, "t": now}])[-DESK_KEEP:]
        arb["open"] = sorted(clearing)
        day = [t["net_usd"] for t in arb["trades"] if t["t"] >= now - 86400]
        arb["last_24h"] = {"trips": len(day), "net_usd": round(sum(day), 2),
                           "median_net_usd": round(statistics.median(day), 2) if day else None}
        self.desk["pnl"] = (self.desk["pnl"] + [[now, round(router["extra_usd"], 4),
                                                 round(arb["pnl_usd"], 4)]])[-2400:]

    def _lp(self, books, cc_usd, tickers, tc_states) -> list[dict]:
        cx_vol = {}
        for t in tickers:
            pair = {m.key(t["base_currency"]), m.key(t["target_currency"])}
            if CC in pair:
                other = (pair - {CC}).pop() if len(pair) > 1 else CC
                v = float(_num(t.get("target_volume") if m.key(t["target_currency"]) == CC
                               else t.get("base_volume")) or 0)
                cx_vol[other] = cx_vol.get(other, 0) + v * float(cc_usd)
        tc_vol = self.slow.get("tc_volume", {}) if self._fresh("tc_volume", int(time.time())) else {}
        pp_vol, _ = self._pp_volume_usd(cc_usd)
        tc_by_sym = {}
        for st in tc_states:
            if CC in (st.token_a, st.token_b):
                tc_by_sym[m.key(st.token_b if st.token_a == CC else st.token_a)] = st.amm_id
        rows = []
        for sym, pools in books.items():
            for v, p in pools.items():
                tvl = float(2 * p.cc_reserve * cc_usd)
                vol = (cx_vol.get(sym) if v == "cantex" else tc_vol.get(tc_by_sym.get(sym, ""), None)
                       if v == "tradecraft" else pp_vol.get(sym) if v == "poolparty" else None)
                rows.append({
                    **({"not_trading_since": st["since"]} if (st := self.still.get((v, sym))) else {}),
                    "venue": v, "pair": f"CC/{p.token}", "tvl_usd": round(tvl, 2),
                    "volume_24h_usd": r(vol, 2), "fee": r(p.fee, 6),
                    "fee_apr": r(m.fee_apr(vol, float(p.fee), float(p.lp_share), tvl), 6)
                    if vol is not None and p.lp_share > 0 else None,
                })
        return sorted(rows, key=lambda x: -x["tvl_usd"])

    def _optional_down(self, now: int) -> dict[str, int]:
        """Each venue read every tick, other than the core two, that has not been read for 10 min: the
        time of its last good read (the start, if none since)."""
        out = {}
        for v in self.optional:
            last = self.fresh.get(v) or self.started
            if v != "temple" and now - last > 600:
                out[v] = last
        return out

    def _summary(self, cc_usd, cx_volume, gecko, series, now_ms, books, now, took) -> dict:
        # CC's change from our own samples of the price shown beside it (depth-weighted, Temple's book
        # in it) once a full day of them follows that step; Cantex's CC-USDCX candles before (its pools
        # read -13.5% on 2026-10-15 while the price shown fell 12.9%)
        step = next((x["t"] for x in self.history.get("steps", []) if x["id"] == "cc-price-depth-weighted"), None)
        own_cc = self._own_series().get(CC, [])
        cc_series = (own_cc if step is not None and now - step >= 86400 + 3600 and own_cc
                     else series.get(CC) or own_cc)
        volume_cc, swaps = _num(cx_volume.get("volume_cc")), _num(cx_volume.get("swap_count"))
        llama = self.slow.get("llama")
        return {
            "t": now,
            "took_s": round(took, 1),
            "status": "ok",
            "cc_usd": r(cc_usd, 8),
            "cc_change_24h": r(m.change(cc_series, now_ms, 86400_000)),
            "cc_global_usd": r(_gecko_usd(gecko, "canton-network", now), 8),
            "cc_image": self.slow.get("images", {}).get(CC),
            # unknown (Cantex not read for over an hour, or a field missing) is None, never $0 or 0 swaps
            "cantex_24h": {"volume_cc": r(volume_cc, 2),
                           "volume_usd": r(volume_cc * cc_usd, 2) if volume_cc is not None else None,
                           "swaps": int(swaps) if swaps is not None else None,
                           "fees_cc": r(_num(cx_volume.get("fees_cc")), 2)},
            # DefiLlama's figures, while their read is under six hours old
            "ecosystem": llama if llama and now - llama.get("t", 0) <= LLAMA_MAX_AGE_S else None,
            "tokens": len(books),
            "on_two_venues": sum(len(v) > 1 for v in books.values()),
            "venues_priced": [v for v in CORE if v not in self.down] + sorted(self.live),
            # a core venue left out since this time (watchdog.py tells the owner after 30 min)
            "down_since": dict(self.down),
            # any other venue read every tick, out since its last good read; Temple's keyed books, and
            # each outside source's last good read (the watchdog's age limits)
            "optional_down": self._optional_down(now),
            "temple_priced": self.temple_priced,
            "temple_cc_book": self.temple_cc_book,
            # ticks that failed in the last hour (of about 12): the watchdog sees every other one at most
            "failed_ticks_1h": sum(1 for t in self.fail_times if now - t <= 3600),
            "last_error": self.last_error if self.last_error and now - self.last_error["t"] <= 3600 else None,
            # pools whose reserves stood still while their price left the market: in no comparison
            "pools_not_trading": self._still_json(),
            **({"temple_error": self.temple_error} if self.temple_error else {}),
            "sources": dict(sorted({**self.sources, **({"llama": llama["t"]} if llama else {})}.items())),
        }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--state", type=Path, help="private records (default: <out>-state, beside the web root)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    c = Collector(args.out, args.state)
    await c.start()
    try:
        while True:
            began = time.monotonic()
            try:
                await c.tick()
            except Exception as exc:  # one bad tick must not stop the service
                log.exception("tick failed")
                now = int(time.time())
                c.fail_times = [t for t in c.fail_times if now - t <= 3600] + [now]
                c.last_error = {"t": now, "error": str(exc)[:300]}
                s = load_json(c.api / "summary.json", {})
                s.update(status="error", error=str(exc)[:300], error_t=now, failed_ticks_1h=len(c.fail_times))
                write_json(c.api / "summary.json", s)
            if args.once:
                break
            await asyncio.sleep(max(5, args.interval - (time.monotonic() - began)))
    finally:
        await c.stop()


if __name__ == "__main__":
    asyncio.run(main())
