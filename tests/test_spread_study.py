"""The spread study's sample reads the collector's helpers as they return today (it broke on f2a22bf)."""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import collect
import spread_study

from cantonvenues.core.models import Instrument

CC = Instrument(admin="DSO::1220", id="Amulet")
USDCX = Instrument(admin="usdc::1220", id="USDCx")


def test_sample_runs_against_the_collector_helpers(tmp_path):
    c = collect.Collector(tmp_path)
    state = SimpleNamespace(symbol_a="CC", symbol_b="USDCx", token_a=CC, token_b=USDCX,
                            reserve_a=Decimal("4000000"), reserve_b=Decimal("480000"), fee=Decimal("0.003"))

    async def ret(value):
        return value

    c.cantex.pool_states = lambda: ret([state])
    c.tradecraft.pool_states = lambda: ret([])
    c.tradecraft.pools = lambda: ret([])
    c.optional = {}

    async def reserve_venues(books, cx):
        return None

    c._reserve_venues = reserve_venues
    # the shapes the collector's own tick unpacks: Rocky three values, Temple four
    c._rocky_spot = lambda books, cc_usd, usdcx: ret(({}, {}, None))
    c._temple_spot = lambda books, usdcx: ret(({}, {}, None, None))

    async def go():
        try:
            return await spread_study.sample(c)
        finally:
            await c.http.aclose()

    out = asyncio.run(go())
    assert out["routes"] == [] and out["t"] > 0


EDELX = Instrument(admin="edel::1220", id="EDELx")


def _two_venue_collector(tmp_path):
    """Cantex's CC/USDCx and CC/EDELx pools, and a Pool Party CC/EDELx pool 36% off Cantex's price,
    the shape of 10-12 Nov: every Pool Party pool standing still, the site marking it not trading."""
    c = collect.Collector(tmp_path)
    usd = SimpleNamespace(symbol_a="CC", symbol_b="USDCx", token_a=CC, token_b=USDCX,
                          reserve_a=Decimal("4000000"), reserve_b=Decimal("480000"), fee=Decimal("0.002"))
    edel = SimpleNamespace(symbol_a="CC", symbol_b="EDELx", token_a=CC, token_b=EDELX,
                           reserve_a=Decimal("1000000"), reserve_b=Decimal("5000000"), fee=Decimal("0.002"))

    async def ret(value):
        return value

    c.cantex.pool_states = lambda: ret([usd, edel])
    c.tradecraft.pool_states = lambda: ret([])
    c.tradecraft.pools = lambda: ret([])
    c.optional = {}

    async def reserve_venues(books, cx):
        books["EDELX"]["poolparty"] = spread_study.m.VenuePool(
            "poolparty", "EDELx", Decimal("340000"), Decimal("1080000"), Decimal("0.003"), Decimal(0),
            spread_study.m.FORMULAS["cp"](Decimal("340000"), Decimal("1080000"), Decimal("0.003")),
            "cp", Decimal("0.003"))

    c._reserve_venues = reserve_venues
    c._rocky_spot = lambda books, cc_usd, usdcx: ret(({}, {}, None))
    c._temple_spot = lambda books, usdcx: ret(({}, {}, None, None))
    return c


def _run(c):
    async def go():
        try:
            return await spread_study.sample(c)
        finally:
            await c.http.aclose()

    return asyncio.run(go())


def test_a_pool_the_site_marks_not_trading_books_no_route(tmp_path):
    out = tmp_path / "out"
    c = _two_venue_collector(out)
    collect.write_json(c.api / "pools.json", {"not_trading": [
        {"venue": "poolparty", "pair": "CC/EDELx", "since": 1794294143, "gap": 0.62}]})
    got = _run(c)
    assert not [r for r in got["routes"] if "poolparty" in r[0]]
    assert got["left_out"] == ["poolparty CC/EDELx"]


def test_without_the_sites_list_the_still_pool_reads_as_a_big_trip(tmp_path):
    """The same pools with no pools.json: the trip the v3 runs booked, so the test above can fail."""
    got = _run(_two_venue_collector(tmp_path / "out"))
    trips = [r for r in got["routes"] if r[0].startswith("EDELX:cantex>poolparty")]
    assert trips and trips[0][1] > 100
    assert got["left_out"] == []


def test_the_wrong_v3_runs_leave_the_file_when_a_v4_run_is_written(tmp_path):
    path = tmp_path / "spreads.json"
    before = {"runs": [{"start": 1794000000, "v": 3, "capture_usd": 202.0},
                       {"start": spread_study.V3_WRONG_FROM + 21000, "v": 3, "capture_usd": 3030.24}]}
    collect.write_json(path, before)
    data = spread_study.append_run(path, {"start": 1794500000, "end": 1794503600, "v": 4, "capture_usd": 90.0})
    assert [r["capture_usd"] for r in data["runs"]] == [202.0, 90.0]
    assert collect.load_json(path, {})["t"] == 1794503600
