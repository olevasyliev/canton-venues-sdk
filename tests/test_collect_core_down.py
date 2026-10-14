"""A core venue (Cantex, Tradecraft) that does not answer is left out of the tick, not the whole tick."""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import collect

from cantonvenues.core.models import Instrument

CC = Instrument(admin="DSO::1220", id="Amulet")


def _collector(tmp_path):
    c = collect.Collector(tmp_path)
    asyncio.run(c.http.aclose())
    return c


def test_cc_instrument_is_kept_from_the_last_cantex_read(tmp_path):
    c = _collector(tmp_path)
    with pytest.raises(RuntimeError, match="has not answered since the start"):
        c._cc_instrument([])  # Cantex never read: no way to tell which token is CC
    states = [SimpleNamespace(symbol_a="CC", symbol_b="USDCx", token_a=CC, token_b=Instrument("x", "USDCx"))]
    assert c._cc_instrument(states) == CC
    assert c._cc_instrument([]) == CC  # Cantex down on a later tick: the last one stands


def test_a_venue_left_out_shows_down_and_is_named_in_the_summary(tmp_path):
    c = _collector(tmp_path)
    c.down = {"tradecraft": 1_791_970_000}
    rows = {v["id"]: v["status"] for v in c._venues(4.4e6, None, [], 1_791_973_000)["venues"]}
    assert rows["tradecraft"] == "down" and rows["cantex"] == "priced"
    s = c._summary(Decimal("0.125"), {}, {}, {}, 1_791_973_000_000, {}, 1_791_973_000, 1.0)
    assert s["venues_priced"] == ["cantex"] and s["down_since"] == {"tradecraft": 1_791_970_000}
    assert s["cantex_24h"]["volume_usd"] == 0  # no stats at all: zero, not a crash
