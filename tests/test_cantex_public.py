"""Offline tests for CantexPublicData: parsing and local pricing."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from cantonvenues import CantexPublicData, Instrument
from cantonvenues.core.venue import VenueRequestError
from cantonvenues.venues.cantex_public import swap_output

CC = {"admin": "DSO::1220", "id": "Amulet", "symbol": "CC"}
USD = {"admin": "usdc::1220", "id": "USDCx", "symbol": "USDCx"}

STATE = {"data": {"pools": [{
    "contract_id": "00ab", "pool_id": "1", "symbol": "CC-USDCX", "fee_rate": "0.0005000000",
    "price": "0.1225276182", "reserve_a": "1613381.8277082216", "reserve_b": "197683.8326573317",
    "token_a_instrument": CC, "token_b_instrument": USD, "tvl_cc": "3226763.6554164432",
}]}}


def _adapter(routes: dict) -> CantexPublicData:
    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(request.url.path)
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404, text="nope")

    return CantexPublicData(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_pool_states_parse_decimals_and_instruments():
    states = await _adapter({"/v1/public/pools/state": STATE}).pool_states()
    s = states[0]
    assert s.symbol == "CC-USDCX" and s.symbol_a == "CC"
    assert s.token_a == Instrument("DSO::1220", "Amulet")
    assert s.reserve_b == Decimal("197683.8326573317") and s.fee == Decimal("0.0005")


async def test_unfunded_pool_with_null_price_does_not_fail_the_read():
    # live /pools/state on 2026-10-13: a new CBNB-CC pool with zero reserves and price null
    bnb = {"admin": "bnb::1220", "id": "CBNB", "symbol": "CBNB"}
    empty = {"contract_id": "00cd", "pool_id": "5898460812258591496", "symbol": "CBNB-CC",
             "fee_rate": "0.0010000000", "price": None, "reserve_a": "0.0000000000",
             "reserve_b": "0.0000000000", "token_a_instrument": bnb, "token_b_instrument": CC,
             "tvl_cc": "0.0000000000"}
    broken = {**empty, "contract_id": "00ef", "symbol": "X-CC", "reserve_a": None}
    body = {"data": {"pools": STATE["data"]["pools"] + [empty, broken]}}
    states = await _adapter({"/v1/public/pools/state": body}).pool_states()
    assert [s.symbol for s in states] == ["CC-USDCX", "CBNB-CC"]  # the malformed pool alone is left out
    assert states[1].price == 0 and states[1].tvl_cc == 0


async def test_quote_is_constant_product_with_fee_on_input():
    c = _adapter({"/v1/public/pools/state": STATE})
    cc, usd = Instrument("DSO::1220", "Amulet"), Instrument("usdc::1220", "USDCx")
    q = await c.quote(Decimal(1000), cc, usd)
    expected = swap_output(Decimal("1613381.8277082216"), Decimal("197683.8326573317"),
                           Decimal(1000), Decimal("0.0005"))
    assert q.returned_amount == expected
    assert q.fee_percentage == Decimal("0.0005")
    assert q.network_fee is None  # not in /pools/state; documented as excluded
    back = await c.quote(q.returned_amount, usd, cc)
    assert back.returned_amount < Decimal(1000)  # a round trip pays the fee twice


async def test_quote_rejects_unknown_pairs_and_bad_amounts():
    c = _adapter({"/v1/public/pools/state": STATE})
    cc = Instrument("DSO::1220", "Amulet")
    with pytest.raises(VenueRequestError):
        await c.quote(Decimal(1), cc, Instrument("x::1", "NOPE"))
    with pytest.raises(VenueRequestError):
        await c.quote(Decimal(0), cc, Instrument("usdc::1220", "USDCx"))


async def test_http_errors_become_venue_errors():
    with pytest.raises(VenueRequestError):
        await _adapter({}).volume()
