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
