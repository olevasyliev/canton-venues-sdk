# SDK integration point provenance

Every point where `cantonvenues` touches `cantex_sdk` is listed here with the
source location it was verified against. Line numbers refer to the SDK at
commit cloned from `https://github.com/caviarnine/cantex_sdk`, file
`src/cantex_sdk/_sdk.py` (unless noted otherwise).

## Configuration & credentials

| Integration point | Adapter site | SDK source |
|---|---|---|
| Env var `CANTEX_OPERATOR_KEY` (operator Ed25519 hex) | `cantex.OPERATOR_KEY_ENV` | `examples/example.py:11,50` |
| Env var `CANTEX_TRADING_KEY` (intent secp256k1 hex) | `cantex.TRADING_KEY_ENV` | `examples/example.py:11,54` |
| Env var `CANTEX_BASE_URL` | `cantex.BASE_URL_ENV` | `examples/example.py:48` |
| Default base URL `https://api.testnet.cantex.io` | `cantex.DEFAULT_BASE_URL` | `_sdk.py:1286` (`CantexSDK.__init__` default) |
| `OperatorKeySigner.from_env(name)` | `CantexAdapter.__init__` | `_sdk.py:573-579` (defined on `BaseSigner`), class `_sdk.py:646` |
| `IntentTradingKeySigner.from_env(name)` | `CantexAdapter.__init__` | `_sdk.py:573-579`, class `_sdk.py:714` |
| `CantexSDK(operator, intent, base_url=..., api_key_path=...)` | `CantexAdapter.__init__` | `_sdk.py:1281-1305` |
| `sdk.public_key` (env-wiring test) | test only | `_sdk.py:1362-1365` |
| `sdk._intent_signer` (env-wiring test) | test only | `_sdk.py:1284,1294` |

## Lifecycle

| Integration point | Adapter site | SDK source |
|---|---|---|
| `await sdk.authenticate()` (challenge-response, Ed25519) | `CantexAdapter.connect` | `_sdk.py:1470-1515` |
| `await sdk.close()` | `CantexAdapter.close` | `_sdk.py:1325-1332` |

## Read path

| Integration point | Adapter site | SDK source |
|---|---|---|
| `await sdk.get_pool_info() -> PoolsInfo` | `CantexAdapter.pools` | `_sdk.py:1665-1668` |
| `await sdk.get_account_info() -> AccountInfo` | `CantexAdapter.balances` | `_sdk.py:1655-1658` |
| `await sdk.get_swap_quote(sell_amount, sell_id, buy_id) -> SwapQuote` | `CantexAdapter.quote` | `_sdk.py:1670-1688` |

## Write path

| Integration point | Adapter site | SDK source |
|---|---|---|
| `await sdk.swap_and_confirm(sell_amount, sell_id, buy_id, *, max_network_fee) -> SwapExecutedEvent` | `CantexAdapter.swap` | `_sdk.py:1846-1918` |
| `await sdk.transfer(amount, instrument_id, receiver, memo) -> dict` | `CantexAdapter.transfer` | `_sdk.py:1706-1728` |

## Domain model field shapes (mirrored by `core/models.py`)

| Core model | Field(s) | SDK model → source |
|---|---|---|
| `Instrument(admin, id)` | `admin`, `id` | `InstrumentId` `_sdk.py:104-113` |
| `Pool(contract_id, token_a, token_b)` | all | `Pool` `_sdk.py:257-277`; list via `PoolsInfo.pools` `_sdk.py:280-297` |
| `Balance(instrument, symbol, name, unlocked, locked)` | `symbol`←`instrument_symbol`, `name`←`instrument_name`, `unlocked`←`unlocked_amount`, `locked`←`locked_amount` | `TokenBalance` `_sdk.py:121-158`; list via `AccountInfo.tokens` `_sdk.py:161-201` |
| `Quote.returned_amount / returned_instrument` | ←`SwapQuote.returned` (`QuoteLeg`) | `_sdk.py:422`, `QuoteLeg` `_sdk.py:300-315` |
| `Quote.trade_price / slippage` | ←`SwapQuote.prices.trade / .slippage` | `QuotePrices` `_sdk.py:318-336` |
| `Quote.fee_percentage` | ←`SwapQuote.fees.fee_percentage` | `QuoteFees` `_sdk.py:389-410` |
| `Quote.sell_amount / sell_instrument / buy_instrument / estimated_time_seconds` | direct | `SwapQuote` `_sdk.py:413-500` |
| `SwapResult(input_amount, input_instrument, output_amount, output_instrument, price, admin_fee_amount, liquidity_fee_amount, market)` | all | `SwapExecutedEvent` `_sdk.py:887-925` |

## Exceptions (mapped in `venue.py` hierarchy)

| SDK exception | Mapped to | SDK source |
|---|---|---|
| `CantexAuthError` (401/403) | `VenueAuthError` | `_sdk.py:84-89` |
| `CantexAPIError`, `CantexTimeoutError`, `CantexError` | `VenueRequestError` | `_sdk.py:71-89` |

## Notes / judgment calls

- `swap()` maps onto the SDK's `swap_and_confirm` (not the fire-and-forget
  `swap`) so the adapter can return a typed `SwapResult`; the fire-and-forget
  `swap` (`_sdk.py:1816-1844`) returns an untyped `dict`.
- `transfer()` returns the SDK's raw submit `dict`; the SDK provides no typed
  model for transfer results, so no fields are invented.
- Default base URL follows the SDK class default (testnet, `_sdk.py:1286`).
  The example script defaults to mainnet (`api.cantex.io`, `examples/example.py:48`);
  the adapter deliberately defaults to testnet per the toolkit's scope.

---

# Reference DEX adapter (`venues/dexref.py`)

Upstream: [srikanth-bitdynamics/Canton-Dex-Reference-Implementation](https://github.com/srikanth-bitdynamics/Canton-Dex-Reference-Implementation),
`services/operator-backend` (no vendor SDK; the adapter speaks the HTTP API
directly). Verified against a live demo-mode backend + source on 2026-07-18.

## Endpoints consumed

| Adapter call | Endpoint | Upstream source |
|---|---|---|
| `connect()` | `GET /v1/context`, `GET /v1/status` | `http/index.ts` |
| `pools()` | `GET /v1/pools` | `http/index.ts`, shapes `types.ts` |
| `quote()` | `POST /v1/swaps/quote` | `http/index.ts:941`; NB request field `poolId` takes the pool **contract id** |
| `balances()` | `GET /v1/holdings?owner=` | `http/index.ts`; per-contract rows, aggregated client-side |
| `swap()` | `POST /v1/pools/swap/request` (only when the authorizer needs a spec) + `POST /v1/pools/swap` | `pool/index.ts:37-84` (inputs), `:345-454` (flow) |
| `transfer()` | none — venue has no transfer surface; always raises | — |

## Model mapping / judgment calls

- `Instrument(admin, id)` ← pool `admin` + `baseInstrumentId`/`quoteInstrumentId`
  (holdings rows carry `admin` + `instrumentId`). No instrument-metadata
  endpoint exists, so `Balance.symbol = Balance.name = instrument id`.
- `Quote`: venue returns only `outputAmount`. `trade_price` = out/in;
  `slippage` = 1 − realized/spot with spot from pool `reserves`;
  `fee_percentage` = `feeBps`/10000 (fraction, matching the Cantex
  convention); `estimated_time_seconds` = 0 (not provided).
- `SwapResult`: `amountOut` from the `PoolRules_Swap` result;
  `admin_fee_amount` = 0 and `liquidity_fee_amount` = input × fee fraction
  (pool fee accrues entirely to LPs, `dev-server.ts:115`); `market` =
  pool `poolId` (e.g. `BTC-USDC`).
- Amount strings are ≤10-dp fixed-point per `DECIMAL_RE`
  (`http/validate.ts:25`); party ids must be canonical
  `hint::hexfingerprint{8,}` unless the server runs with
  `DEX_ALLOW_BARE_PARTIES=1` (`http/validate.ts:38-49`).
- The swapper-side allocation is wallet-authored on this venue
  (`pool/index.ts:69-84`); the adapter delegates it to a pluggable
  `AllocationAuthorizer`. The in-memory demo mock ignores the allocation cid
  (`dev-server.ts:94-160`), so `DemoAllocationAuthorizer` supplies a
  synthetic one; a testnet authorizer will drive the wallet relay
  (`http/index.ts:655`, needs `DEX_DEV_WALLET_RELAY=1` + a real ledger).
- Errors: HTTP 401 → `VenueAuthError`; other 4xx/5xx and transport errors →
  `VenueRequestError`, carrying the server's `{code, error}` envelope.

---

# Ekiden adapter (`venues/ekiden.py`) — read-only market data

Perpetuals venue on Canton. No SDK is used: the gateway (`ekiden-gateway`,
Rust/Axum) is called directly over HTTP. Shapes below were captured from live
responses on the Canton testnet gateway `https://api.cnt.ekiden.fi` on
2026-07-27, **not** from the published OpenAPI document — see the caveat at the
bottom. **MainNet** (`https://api.ekiden.fi`, the adapter's default since 2026-10-03,
listed as "Production" at docs.ekiden.fi/api-reference/integration/configuration) serves
the same routes and payloads; `/api/v1/info` there reports validator
`canton-grpc.validator.cnm.ekiden.fi` and MainNet USDCx (`…12208115…`). Staging is `https://api.canton.ekiden.fi`; public WebSocket streams
exist at `wss://api.cnt.ekiden.fi/ws/public` and are not consumed yet.

Public market data needs no credentials. Trading requires an Ed25519-signed
session (`AUTHORIZE|<timestamp_ms>|<nonce>`) plus API keys, and a root-whitelist
surface exists (`/api/v1/authorize/whitelist/*`); none of that is implemented
here, so this adapter cannot place an order.

## Endpoints consumed

| Adapter method | Endpoint | Notes |
|---|---|---|
| `connect()` | `GET /api/v1/info` | asserts `runtime_manifest_phase == "ready"` |
| `markets()` | `GET /api/v1/market/instruments-info` | no params; `{"list": [...]}` envelope |
| `tickers()` | `GET /api/v1/market/tickers` | optional `symbol`; returns all markets when omitted |
| `order_book()` | `GET /api/v1/market/orderbook` | `symbol` + `depth`; **`depth` ∈ {10, 50, 200} only** |
| `recent_trades()` | `GET /api/v1/market/recent-trade` | `symbol`, `limit`; rows use single-letter keys |
| `funding_history()` | `GET /api/v1/market/funding/history` | `symbol`, `limit` |

## Model mapping / judgment calls

- Perps have no pools and nothing to swap, so this implements the separate
  `MarketDataAdapter` interface rather than `VenueAdapter`, against new models
  (`Market`, `OrderBook`, `BookLevel`, `Trade`, `Ticker`, `FundingRate`).
- `Market` carries the constraints an order must satisfy: `tick_size` from
  `price_filter`, `min_order_size`/`size_step`/`min_notional` from
  `lot_size_filter`, `max_leverage` from `leverage_filter`.
- Book rows are bare `[price, size]` pairs under `result.b` / `result.a`;
  trade rows key on `i`/`s`/`S`/`v`/`p`/`seq`/`T`. `Trade.side` is the
  aggressor's side.
- All amounts are decimal strings parsed exactly; funding rates arrive with up
  to 28 significant digits, so nothing may pass through a float.
- Timestamps are epoch milliseconds, surfaced as timezone-aware UTC datetimes.
- `OrderBook.mid_price` / `.spread` return `None` on a one-sided book, which is
  a real state here (the CC book had zero offers on 2026-07-27 11:22 UTC after
  a run of buys).

## Venue quirks the adapter absorbs

- **`depth` is an undeclared enum.** Only 10/50/200 are accepted; the OpenAPI
  parameter carries no schema. The adapter rejects other values before the
  request.
- **Query-string rejections come back as plain text**, not the JSON error
  envelope every other route uses, so the error mapper handles both.
- **An empty book side is reported as `"0"`/`"0"` in the ticker's
  `best_ask_price`/`best_ask_size`**, not as null. Passed through, a caller
  reads a best ask of zero as a real price. `_top()` maps non-positive
  price-or-size to `None`.
- **The published `api-reference/openapi.json` does not parse** — trailing
  comma after the staging server entry — so client generation from it fails at
  step one.
- `GET /api/v1/info` reports the Canton validator under a field named
  `aptos_network`, left over from their migration off Aptos.

---

# Tradecraft adapter (`venues/tradecraft.py`) — read-only spot AMM

Constant-product AMM on Canton **mainnet**, run by Obsidian Systems
(`tradecraft.validator.dev.canton.obsidian.systems` serves its devnet). No SDK
exists; the public HTTP API is called directly. Shapes below were captured from
live responses against `https://api.tradecraft.fi/v1` on **2026-08-11**, and
cross-checked against the published OpenAPI fragments in the docs export
(`docs.tradecraft.fi/llms-full.txt`) and the DAR integration guide v1.1.13.

Reading and pricing need no credentials. Orders settle by exercising Daml
choices on the venue's own package (`AMMRules_CreateSwapOrder` and friends),
which needs a validator node, a party of ours, and the package itself, which
the docs say is available on request. None of that is implemented, so this
adapter implements the read-only `PoolDataAdapter` interface and cannot place
an order.

## Endpoints consumed

| Adapter method | Endpoint | Notes |
|---|---|---|
| `connect()` | `GET /health` | asserts `status == "ok"` |
| `pool_states()` | `GET /pools` | `{"pools": [...]}`; reserves, LP supply, both fee constants, 24h yield |
| `pools()` | `GET /tokenA/{a}/{b}`, `GET /tokenB/{a}/{b}` | resolves symbols to Canton instruments, cached per symbol |
| `inspect()` | `GET /inspect/{a}/{b}` | live reserves, `k`, `unclaimed_operator_fees`, `updated_at` |
| `fees()` | `GET /feeAmount/{a}/{b}` | fractions here, percents on `/pools` |
| `quote_symbols()` | `GET /quoteForFixedInput/{a}/{b}?givingAmount=` | the one route family that honours path order |
| `quote_for_output()` | `GET /quoteForFixedOutput/{a}/{b}?gettingAmount=` | exact inverse of the above |
| `liquidity_deposit_quote()` | `GET /quoteLPDeposit/{a}/{b}` | `instrument1Amount`, `instrument2Amount` |
| `liquidity_withdrawal_quote()` | `GET /quoteLPWithdrawal/{a}/{b}` | `lpTokenAmount` |
| `pool_yield()` / `pool_volume_usd()` | `GET /yield/{a}/{b}`, `GET /volume/{a}/{b}` | keyed by lookback (`1h`, `1d`, `7d`, `14d`, `30d`) |
| `yield_history()` / `volume_history()` | `GET /yield_history`, `GET /volume_history` | `window`; **different vocabularies per route** |

Not consumed: `GET /ammid` (the id is `lp_token_name`), `GET /lpToken`,
`GET /ratio` (see below), `GET /disclosures` and `POST /vault-holdings` (write
path only).

## The fee model, measured

The API publishes two constants per pool — an LP fee and an operator fee,
0.2% + 0.1% on 15 of the 23 pools. The engine charges **half their sum on each
leg**: the input is netted by `(lp + op) / 2`, and so is the output. Realized
fee is therefore `1 - (1 - t/2)²`, slightly under the published `t`
(0.299775% where 0.3% is advertised).

This is a measurement, not a reading of their source: `swap_output()` is fitted
to nothing and `scripts/tradecraft_market_smoke.py` prices **45 of 45
directions across all 23 pools** and reproduces the venue's own quote to
~1e-16 relative, which is float64's own precision. A single fee on one leg does
not fit (it is off by ~2e-6 relative, well outside that).

## Venue quirks the adapter absorbs

- **Path order is honoured by the quote routes and silently ignored by
  everything else.** `/quoteForFixedInput/USDCx/CC` sells USDCx, but
  `/inspect`, `/ratio`, `/tokenA`, `/tokenB` and `/quoteLPDeposit` answer in
  the pool's canonical order however the path is written — no error, no hint.
  Sizing an LP deposit off the reversed path inverts the pair:
  `/quoteLPDeposit/USDCx/CC?instrument1Amount=1000` returns
  `instrument_1_to_deposit: 1000` meaning 1000 **CC**. The adapter orients
  every response by the `token_a_id`/`token_b_id` the venue returns, and
  `LiquidityQuote` is labelled by symbol rather than by position.
- **Symbol is not instrument id, for exactly one token.** Canton Coin is `CC`
  on every route and `Amulet` as an instrument — the same trap Cantex has.
  `quote()` translates instruments to symbols; `_symbol_for()` refuses rather
  than guesses.
- **The same two fee constants ship in two units**: `/pools` gives
  `lp_fee_percent: 0.2` (percent), `/feeAmount` gives `fee_amount: 0.002`
  (fraction) under a schema that calls it a percentage. Fractions everywhere
  here.
- **`/ratio`'s description is the inverse of its own formula** — "the price of
  token B expressed in token A (tokenB_holdings / tokenA_holdings)". The value
  follows the formula. Combined with the orientation quirk, the route is not
  worth calling: `PoolState.price(base, quote)` is computed from reserves.
- **The two history routes take different windows.** `yield_history` accepts
  hour/day/week/month/year, `volume_history` only hour/day/week, and the
  published spec declares hour/day/week for both. Rejected client-side.
- **Zero and negative amounts are quoted, not refused**: the venue answers
  `200 {"user_gets": 0}`. Guarded before the request goes out.
- **Amounts cross the wire as JSON numbers**, and eleven of the 69 numeric
  pool fields are already large enough that a float64 cannot step at the
  ledger's 1e-10 (worst: 18,273,526.46980026 HECTO, step 3.7e-9; `k` at
  4.69e12, step ~1e-3). Responses are parsed with `parse_float=Decimal` so the
  digits the venue sent are kept rather than round-tripped through a float a
  second time.
- **The published token enum is stale**: seven symbols declared, thirteen
  traded, so a client generated from the spec cannot reach FRXUSD.B, HECTO,
  TRKXRWA, USDM1, eXAG or eXAU. The adapter takes its token list from
  `/pools`, never from the spec.
- **An unknown path returns `text/plain`** while every documented error is the
  JSON envelope, so the error mapper handles both.
- `POST /vault-holdings`, required by step 5a of the venue's own DAR
  integration guide, is absent from the published OpenAPI document.

# Cantex public market data (`venues/cantex_public.py`)

No account and no key: these are Cantex's public routes, documented at
`https://docs.cantex.io/developers/public-api/`. Shapes verified against
`https://api.cantex.io` on 2026-10-02.

## Endpoints consumed

| Route | Used by | What it returns |
|---|---|---|
| `GET /v1/public/pools/state` | `pool_states`, `pools`, `quote` | reserves, `fee_rate`, `price` (token_b per token_a), `tvl_cc`, both instruments with symbols |
| `GET /v1/public/tokens/info` | `tokens` | every token with its `coingecko_id` (null when unpriceable) |
| `GET /v1/public/volume` | `volume` | trailing-24h `volume_cc`, `fees_cc`, `lp_fees_cc`, `swap_count` |
| `GET /v1/public/stats` | `stats` | daily CC volume series, active traders |
| `GET /v1/public/markets/info` | `markets` | market symbols, `source` (`cantex` or `external`), channel names |
| `GET /v1/public/coingecko/tickers` | `tickers` | per pair last price and 24h base/target volume (JSON numbers) |
| `wss://…/v1/ws/public`, `market.<SYMBOL>.candles.<PERIOD>` | `candles` | a snapshot of recent bars on subscribe; 500 hourly bars on 2026-10-02 |

## The pricing, measured

`quote()` prices locally: constant product with the fee on the input. Against
the authenticated `POST /v2/pools/quote` it agreed to the 10th decimal on
CC/USDCx and CBTC/CC at 100 and 10,000 CC, both directions (2026-10-02). The
authenticated quote adds a flat CC network fee (0.82–1.24 CC in those runs)
that `/pools/state` does not carry, so it is not in `quote()`. The public
`POST /v1/public/connect/quote` prices the Connect transfer-with-memo path
instead (0.25% fee, 2 CC network fee that day), so it is not used as the API
trader's price.

---

# Rocky adapter (`venues/rocky.py`) — read-only market data

Spot and perpetual order books on Canton. Public REST, Binance-compatible, no key:
`https://api.rocky.exchange/api/v3/*` (spot) and `/fapi/v1/*` (perps). Shapes below were
captured from live MainNet responses on 2026-10-03. DefiLlama's Rocky adapter reads the same
public routes.

| Method | Route | Notes |
|---|---|---|
| `connect` / `markets` | `GET /exchangeInfo` | `symbols[]` with `PRICE_FILTER`, `LOT_SIZE`, `NOTIONAL` filters (spot); perps list `symbol`/`pair`/`baseAsset`/`quoteAsset` only |
| `tickers` | `GET /ticker/24hr` | `lastPrice`, `volume` (base), `quoteVolume`; `bidPrice`/`askPrice` are always `"0"`, so top of book is not taken from here |
| `order_book` | `GET /depth?symbol=&limit=` | `bids`/`asks` as `[price, qty]` strings; perps add `E`/`T` ms timestamps |
| `recent_trades` | `GET /trades?symbol=&limit=` | `id` (UUID), `price`, `qty`, `time` ms, `isBuyerMaker` |
| `funding_history` | `GET /fapi/v1/fundingRate` | answered 200 with an empty body on 2026-10-03 |

Spot symbols on 2026-10-03: CBTC-USDCX, CBTC-USDCB, CETH-USDCB, CETH-CBTC. Perps: BTCUSDT,
ETHUSDT, CCUSDT. `/fapi/v1/premiumIndex` was empty and `/fapi/v1/openInterest` returned `"0"`.

---

# OneSwap (`venues/oneswap.py`) and Pool Party (`venues/poolparty.py`) — reserves only

Both are AMMs that publish reserves without a key and no keyless quote route, so pools are priced
locally as constant product with the fee on the input (`venues/reserves.py`). Verified live on MainNet,
2026-10-03.

| Venue | Route | Notes |
|---|---|---|
| OneSwap | `GET https://api.oneswap.cc/swapv2/api/rt/pools` | `assetX`/`assetY` with instrument `admin` + `id`; reserves as numbers and, under `accounting`, as decimal strings (used); `feeBps` 30 |
| OneSwap | `GET /api/rt/tokens` | symbol, admin, id, registry URL |
| Pool Party | `GET https://api-mainnet.cantonwallet.com/canton/pool-party/public/v1/tvl` | `pools: {"<idA>-<idB>": {id: reserve}}`; ids only, no issuer; empty pools listed |
| Pool Party | `GET …/volume?period=24h|7d` | `perPool[name].volume` per token, in token units |

Fees: OneSwap's docs state "the pool's 0.30% swap fee" plus a per-swap network fee carved from the
input, "typically around $1.5–2"; the collector charges $1.75 per OneSwap swap in the scanner. Pool Party
publishes no fee or curve; 0.30% is CCTools' `feeRate` for every Send pool (third party). Quotes
(`POST /api/rt/pool/{id}/quote`) need an OneSwap `sk_live_` key and are not used.

---

# Temple adapter (`venues/temple.py`)

Spot order books on Canton; the largest venue by volume.

- **No key:** `GET https://api.templedigitalgroup.com/api/exchange/settled_volume?start_time=<RFC3339>&end_time=<RFC3339>`
  returns `total_volume_usd`, `by_quote`, `trade_count` and `markets[]` (`symbol` like `CBTC/USDCx`,
  `quote_volume` in USD, `trade_count`). Verified live 2026-10-03 (epoch seconds and milliseconds are
  rejected: "start_time must be RFC3339"). DefiLlama's Temple adapter reads the same route.
- **Key required (`X-API-Key`):** `/api/v1/market/ticker`, `/api/v1/market/orderbook` (`symbol`, `levels`,
  `precision`), `/api/v1/market/trades` (`symbol`, `limit` ≤ 500). Taken from the SDK
  `@temple-digital-group/temple-canton-js` 2.1.10: `dist/api/index.js` (routes, `normalizeSymbol` CC→Amulet),
  `dist/api/tokenStore.js` (`X-API-Key` header), `dist/api/types.d.ts` (shapes), `src/config/index.js`
  (mainnet `https://api.templedigitalgroup.com`, testnet `https://api-testnet.templedigitalgroup.com`).
  Without a key the REST routes answer `API_KEY_AUTH_FAILED` and the WebSocket handshake 401 (checked
  2026-10-03). Keys are issued per account in the app (Settings > API Keys, `/settings?tab=api-keys` in
  the MainNet app bundle). **Verified live 2026-10-04 with a key; the payloads differ from the SDK types:**
  `ticker` → `{"count", "tickers": [{symbol, last_price, volume_24h, quote_volume_24h_usd, trade_count_24h,
  vwap_24h, …}]}` (numbers, no bid/ask); `orderbook` → `{"orderbook": {symbol, timestamp, sequence, best_bid,
  best_ask, spread, bids/asks: [{price, quantity, available_quantity, cumulative_quantity, order_count}]}}`
  (`levels` up to at least 200; `CC/USDCx` and `Amulet/USDCx` both accepted, answered as `CC/USDCx`);
  `trades` → `{"count", "trades": [{trade_id, symbol, quantity, price, side, status, created_at}]}`.
  Ten markets on 2026-10-04: CC, CBTC, eXAU, eXAG against USDCx (live) and USDA (idle), plus CBTC/eXAU and
  USDCx/USDA. `GET /api/fees` gives the fee asset (USDCx) but no rate.
- **Fees:** taker 1 bp, maker 0.5 bp, prepaid in USDCx; deposits/withdrawals 5-12 CC via partner wallets
  (help.templedigitalgroup.com/en/articles/12810727-fees-rebates, read 2026-10-04).

**Temple trading (`TempleAdapter(trading=True)`).** Routes from SDK 2.1.10 `dist/api/index.js`:
`POST /api/trading/orders` with `{symbol, side, quantity, price, order_type: "limit", order_subtype?:
"post_only", expires_at?}` (numbers, as the SDK sends them), `POST /api/trading/orders/{id}/cancel`,
`POST /api/trading/orders/cancel-all` with optional `{symbol}`. Read routes verified live 2026-10-04:
`GET /api/trading/balances` → `{"balances": null | [...], "fee_balances": [{asset, available, in_flight,
locked, updated_at}]}`; `GET /api/trading/delegation` → `{"delegations": [], "linked_parties": []}`;
`GET /api/trading/orders/active` → `{"count", "has_more", "limit", "orders": null | [...], "total_count"}`.
Empty lists arrive as `null`. Order, cancel and cancel-all responses are **unverified** until the first
live order.

---

# Canborsa (`venues/canborsa.py`) — perpetuals, read-only, no key

An account-based exchange on Canton (one balance, deposits from six networks) with perpetuals on about 87
assets against USDT, USDC, USDCx and USDG, and swaps. No documented API (its roadmap lists one for Q4 2026);
these are the routes its web app reads without a session, from `app.canborsa.com/assets/index-*.js` (fetch
helper base `"/api/v1"`, socket `wss://app.canborsa.com` path `/socket.io`). Checked live on MainNet,
2026-10-28, from Spain and from the collector's server in Germany.

| Method | Source | Notes |
|---|---|---|
| `markets` | `GET https://app.canborsa.com/api/v1/perpetuals/markets` | 696 rows, every market twice (`isShort` false and true) with the same `volume30d`; `openingFee`/`closingFee` 0.00055, `maxLeverage`, `minAmountUSD`/`maxAmountUSD` (order size limits on position size, its docs), `settlementPeriodInHours` 1 |
| `funding_rates` | `GET /api/v1/perpetuals/funding-rates` | `{"cc/usdcx": {"rate", "annualRate", "settlementPeriodInHours", "updatedAt"}}`; `rate` per period |
| `stream` | socket.io `rates3` | subscribe `42["sub", {"name": "rates3", "symbols": [...], "product": "perpetual"}]`; pushes `["perpetual", [[symbol, price, bid, ask, iso time, 24h change], ...]]` about once a second, numbers as JSON floats |
| `stream` | socket.io `execution-depth` | subscribe `42["sub", {"name": "execution-depth", "symbols": [...]}]`; pushes `{symbol: {status, bids, asks, generatedAt, expiresAt, ...}}`, 20 levels a side on CC, one to four on BTC, ETH, gold; `"unavailable"` for a swap-only token |

The socket is engine.io v4 over a websocket: `0{...}` open, `40` to join the namespace, `2`/`3` ping and
pong, `42[event, ...]` events. It answered only with a browser `Origin` header. The depth belongs to the
perp, not to a spot book: CC/USDCx and CC/USDC returned the same levels. The swap (`rates` event, one price
per pair) has no depth, so it is not compared with the spot venues. No 24h volume, open interest or trade
list is public.
