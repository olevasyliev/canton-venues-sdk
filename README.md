# Canton Venues SDK

The Canton Algorithmic Trading Toolkit: an open-source Python SDK for trading and reading every venue on
the [Canton Network](https://www.canton.network/). It powers [cantonvenues.com](https://cantonvenues.com).
Install and import it as `cantonvenues`.

The core (`cantonvenues.core`) is venue-agnostic: a small typed domain model
and four adapter interfaces, all with a shared `VenueError` hierarchy.

- `PoolDataAdapter` — read and price an AMM: `Instrument`, `Pool`, `Quote`.
- `VenueAdapter` — a `PoolDataAdapter` you can also trade: `Balance`,
  `SwapResult`.
- `MarketDataAdapter` — read-only market state for venues shaped as a symbol
  and a book rather than a pool and a swap: `Market`, `OrderBook`, `Trade`,
  `Ticker`, `FundingRate`.
- `OrderTradingAdapter` — a `MarketDataAdapter` you can also trade: limit
  orders (post-only, with expiry), cancels, open orders, balances. Trading is
  off unless the adapter is built with `trading=True`, so a market-data reader
  holding the same key can never place an order.

Eight venues, four market structures, one client:

| Adapter | Venue | Shape | Surface |
|---|---|---|---|
| `TempleAdapter` | [Temple](https://templedigitalgroup.com/) | spot order book | settled volume without a key; ticker, book and trades with an account key; limit orders and cancels, opt-in (order path awaiting its first live run) |
| `CantexAdapter` | [Cantex](https://cantex.io/) | spot AMM | read + swap, mainnet |
| `CantexPublicData` | [Cantex](https://cantex.io/) | spot AMM | keyless reserves, volume, candles and tickers, mainnet |
| `RockyAdapter` | [Rocky](https://rocky.exchange/) | spot and perp order books | market data, mainnet, no key |
| `TradecraftAdapter` | [Tradecraft](https://tradecraft.fi/) | spot AMM | read + pricing, mainnet |
| `OneSwapPublicData` | [OneSwap](https://oneswap.cc/) | spot AMM | keyless reserves, mainnet |
| `PoolPartyPublicData` | [Pool Party](https://cantonwallet.com/) | spot AMM | keyless reserves and volume, mainnet |
| `EkidenAdapter` | [Ekiden](https://ekiden.fi/) | perpetual futures | market data, mainnet (testnet available) |
| `CanborsaPublicData` | [Canborsa](https://canborsa.com/) | perpetual futures | keyless markets, funding, prices and execution depth, mainnet (web app routes, undocumented) |
| `DexRefAdapter` | [Canton DEX reference implementation](https://github.com/srikanth-bitdynamics/Canton-Dex-Reference-Implementation) | spot order book + RFQ | read + swap, its hosted testnet |

`CantexAdapter` wraps the official
[`cantex_sdk`](https://github.com/caviarnine/cantex_sdk) async client, so auth,
signing and transport live in the SDK and the adapter only translates its models
and exceptions. The other venues ship no Python SDK (or, like Temple, only a
JavaScript one) and are called directly over HTTP. Every integration point is mapped to the source or the live response
it came from in [`SOURCES.md`](SOURCES.md).

## Install

`cantex_sdk` is not published on PyPI, so it is declared as a git dependency
and pulled directly from GitHub:

```bash
pip install "cantonvenues @ git+https://github.com/olevasyliev/canton-venues-sdk"
# or, from a checkout:
pip install -e ".[dev]"
```

## Environment variables

Credentials are read only from the environment (never hard-coded). The names
are the SDK's own:

| Variable | Required | Description |
|---|---|---|
| `CANTEX_OPERATOR_KEY` | yes | Operator Ed25519 private key (hex). Used for challenge-response auth and ledger signing. |
| `CANTEX_TRADING_KEY` | for swaps | Intent-trading secp256k1 private key (hex). Required for `swap()`. |
| `CANTEX_BASE_URL` | no | API base URL. Defaults to `https://api.testnet.cantex.io`. |
| `TRADECRAFT_BASE_URL` | no | Tradecraft API base. Defaults to mainnet, `https://api.tradecraft.fi/v1`. Reading it needs no credentials. |
| `TEMPLE_API_KEY` | for Temple books | Account key from the Temple app (Settings > API Keys). Settled volume needs none. |
| `EKIDEN_BASE_URL` | no | Ekiden gateway. Defaults to mainnet, `https://api.ekiden.fi`. |

## Usage

```python
import asyncio
from decimal import Decimal
from cantonvenues import CantexAdapter, Instrument

async def main():
    async with CantexAdapter() as venue:  # reads keys from the environment
        pools = await venue.pools()
        quote = await venue.quote(
            Decimal("1"),
            pools[0].token_a,
            pools[0].token_b,
        )
        print(quote.returned_amount, quote.trade_price)

asyncio.run(main())
```

Market data from an order-book venue, no key needed:

```python
from cantonvenues import RockyAdapter

async def top_of_book():
    async with RockyAdapter() as rocky:          # RockyAdapter("perp") for perpetuals
        book = await rocky.order_book("CBTC-USDCX", 20)
        print(book.best_bid, book.best_ask, book.mid_price)
```

## Live showcase

[Canton Venues](https://cantonvenues.com) runs on this toolkit
([`examples/canton_venues`](examples/canton_venues)): prices for every token across
the venues above, Canton's premium to outside markets, best execution by trade size
across pools and order books, a cross-venue spread scanner, a daily study of how fast
spreads close, an open JSON API and an MCP server for AI agents.

## Run tests

```bash
pip install -e ".[dev]"
pytest        # fully offline; the SDK client is mocked at its method boundary
ruff check .
```

## Live smoke

`scripts/live_smoke.py` is a read-only check against the real Cantex API: it
authenticates (`connect()`), lists pools, and prices one small quote. It never
calls `swap()` or `transfer()`.

Create (or fill) a `.env` at the toolkit root with the real keys (same names as the [Environment variables](#environment-variables)
table above):

```
CANTEX_OPERATOR_KEY=...
CANTEX_TRADING_KEY=...
CANTEX_BASE_URL=...
```

Then run:

```bash
python scripts/live_smoke.py
```

The script never prints credential values, only step results and, on
failure, whether it was an auth error or a request error (exit code 1
either way).

## Status

Live-validated on every venue. Tests are fully offline and run against the
real response shapes captured from each venue.

- **Temple** (mainnet, 2026-10-04) — settled volume per market without a key;
  ticker, order book (up to 200 levels) and trades with an account key. The live
  payloads differ from the types in Temple's own JavaScript SDK, and the adapter
  follows the live ones; see [`SOURCES.md`](SOURCES.md). Trading: balances,
  open orders and `trading_status` (linked wallet, delegation, fee balance) are
  verified live; placing and cancelling are implemented from the SDK and
  `scripts/temple_order_smoke.py` (dry run unless `--execute`; a post-only
  order priced never to fill, cancelled at once) is the check that will close
  them, on Temple's testnet first.
- **Rocky** (mainnet, 2026-10-03) — spot and perp markets, 24h tickers, depth and
  trades over its public Binance-style API. The ticker reports bid and ask as 0,
  so top of book comes from depth.
- **OneSwap and Pool Party** (mainnet, 2026-10-03) — pool reserves (and, for Pool
  Party, per-pool volume) without a key, priced locally as constant product.
- **Ekiden** moved to mainnet as the default on 2026-10-03 (live since 2026-09-08);
  the same routes answer there as on testnet.
- **Canborsa** (mainnet, 2026-10-28) — perp markets, funding, 30-day volume, and
  prices and execution depth from the socket its web app reads; no documented API yet.

- **Cantex** (mainnet, 2026-07-17) — challenge-response auth, pools, quoting,
  and an executed swap with real funds: 10 CC sold for 1.2981357151 USDCx on
  the `CC-USDC` market.
- **Reference DEX** (hosted testnet, 2026-07-29) — swaps in both directions
  through the adapter, with balance deltas and pool reserves reconciling
  exactly. Two swap routes are supported: the documented wallet-authored
  allocation path, and deployments that host the trader's party themselves.
  `scripts/dexref_testnet_report.py` additionally exercises the venue's own
  hosted routes end to end: multi-level two-sided quoting from one party, the
  RFQ lifecycle with its best-execution receipt, and liquidity provision with
  the LP token.
- **Ekiden** (Canton testnet gateway, 2026-07-27) — markets, tickers, order
  book, recent trades and funding history across all three of its perpetual
  markets. Trading needs an Ed25519-signed session and is not implemented.
- **Tradecraft** (mainnet, 2026-08-11) — 23 pools across 13 tokens, read and
  priced end to end. `scripts/tradecraft_market_smoke.py` prices **45 of 45
  directions** and reproduces the venue's own quote to ~1e-16 relative from
  pool state alone, which is how the fee model in
  [`SOURCES.md`](SOURCES.md) was established rather than assumed: the engine
  charges half the pool's total fee on *each* leg, so the realized fee is
  0.299775% where 0.3% is published. Orders settle through Daml choices on the
  venue's package and need a validator node, so the adapter reads and prices
  but cannot trade.

Run the live checks with `scripts/live_swap_smoke.py` (Cantex, dry-run by
default), `scripts/dexref_testnet_smoke.py` and
`scripts/dexref_testnet_report.py` (reference DEX, read-only unless given
`--execute`), `scripts/ekiden_market_smoke.py` (Ekiden) and
`scripts/tradecraft_market_smoke.py` (Tradecraft) — the last two are read-only
by construction.
