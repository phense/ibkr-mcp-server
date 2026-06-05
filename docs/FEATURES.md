# Features

This page is a human-readable catalog of everything the IBKR MCP server actually does, grouped by theme. It is meant to be skimmed to understand the surface — what each capability is for, how mature it is, and where the rough edges are. The project is a Model Context Protocol (MCP) server for Interactive Brokers, written in Python on top of [`ib_async`](https://github.com/ib-api-reloaded/ib-async), that speaks the TWS API directly over the IB Gateway / TWS socket and exposes fourteen tools to a Claude client. Every feature below is grounded in the current code (`ibkr_mcp_server/`); where a feature is stubbed, unverified, or only partially wired, it carries an explicit status note rather than a clean bill of health.

## Connection & Account

### Lazy, self-healing TWS/Gateway connection

The server does not connect to IBKR at startup. Instead it brings the MCP protocol up immediately and establishes the socket to IB Gateway / TWS only on the first tool call that needs data, via an internal `_ensure_connected()` guard that every data tool runs before it touches the API (`client.py`). This matters because Claude clients spawn the MCP process eagerly and often, long before any market question is asked — connecting lazily means a spawned-but-idle server doesn't hold a Gateway client slot or fail loudly when the Gateway happens to be down. The connection layer also registers a disconnect handler that schedules a background reconnect, and the `connect()` path is wrapped in a retry-with-backoff decorator (three attempts), so a transient Gateway hiccup self-heals rather than wedging the session.

### Connection-status reporting (`get_connection_status`)

This tool answers "are we actually talking to IBKR, and as whom?" — returning the host, port, client ID, the currently selected account, the full list of available accounts, and the paper-trading flag. It is deliberately the one read tool that *forces* a lazy connect before reporting, so that calling it right after the MCP starts reflects the intended live state instead of a pre-connect false negative. The paper-vs-live determination is inferred from the socket port (4002 and 7497 are treated as paper ports), which is a convention rather than an authoritative signal from IBKR, so trust it as a strong hint rather than a guarantee.

### Multi-account discovery and switching (`get_accounts`, `switch_account`)

On connect, the server discovers every account the logged-in Gateway session manages and selects one as the current account (the configured default if present, otherwise the first discovered). `get_accounts` lists what is available alongside the current selection and the connection/paper flags; `switch_account` changes which account subsequent tools act against, validating the requested ID against the discovered set and refusing unknown IDs with a structured error rather than silently no-op'ing. Every data tool accepts an optional per-call `account` override, so switching is a convenience for a default rather than the only way to target a specific account.

### Account summary (`get_account_summary`)

Returns the key balance and risk metrics for an account: net liquidation value, total cash, buying power, gross position value, equity-with-loan value, the previous day's equity-with-loan value, realized and unrealized PnL, and the full initial and maintenance margin requirements. The implementation filters IBKR's verbose `accountSummary` feed down to this curated tag set so the caller gets a compact, relevant snapshot rather than the hundreds of raw tags the API emits. This is the foundational read for any sizing or risk decision, and it was specifically patched in this fork to match the `ib_async` 2.1 `accountSummaryAsync` signature.

### Portfolio positions (`get_portfolio`)

Lists current positions for the selected (or a specified) account: symbol, security type, exchange, quantity, average cost, current market price and market value, and both unrealized and realized PnL per position. It reads from IBKR's positions feed and serializes each position into a flat, JSON-friendly shape. Combined with the account summary this gives a complete picture of "what do we hold and how is it doing" — the standard starting point before proposing any new trade against the book.

## Market Data

### Real-time / delayed quotes (`get_market_data`)

Fetches a quote — last, bid, ask, bid/ask sizes, volume, day high/low, and prior close — for essentially any instrument the API supports: stocks, indices, futures, options, forex pairs, and the German leverage products (IOPT/WAR). For option contracts it additionally surfaces the model Greeks (delta, gamma, vega, theta), implied volatility, and the underlying price, and when streaming (not snapshot) it also pulls call/put open interest and historical/implied volatility via generic ticks. The tool qualifies the contract first so ambiguous symbols resolve to a concrete contract, then briefly streams (or one-shot snapshots) before reading the tick. Note the returned `delayed` field is a heuristic (it infers delay from an empty last with a non-empty close) and is known to be unreliable — it may read false even when the data is genuinely 10–20 minutes delayed, so do not gate logic on it. *Status: shipped and exercised, but the per-tool live-tick behaviour was last verified against a closed market; a regular-trading-hours retest is still outstanding.*

### Historical OHLCV bars (`get_historical_bars`)

Pulls historical open/high/low/close/volume bars for a stock, index, future, option, forex pair, or German leverage product, with the usual IBKR knobs: duration strings like `30 D` / `1 Y`, bar sizes from `1 secs` up to `1 month`, a `what_to_show` selector (TRADES, MIDPOINT, BID, ASK, BID_ASK, and the volatility series), and a regular-trading-hours toggle. Dates are returned as unix epochs (`formatDate=2`) so the caller never has to parse IBKR's localized date strings. This is the backbone for any backtest input, indicator calculation, or trend read. It is also the tool that respects IBKR's historical-data pacing the most carefully (see Operational characteristics), and is one of the two tools wired for ISIN-based German-cert resolution.

### Per-call rate limiting

Every data method carries a rate-limit decorator tuned to its IBKR cost: market-data quotes at two calls per second, historical bars and option/fundamental/news pulls at a half to a fifth of a call per second, and shortable/margin lookups at the slower end (`utils.py`, `client.py`). This is a client-side throttle layered on top of IBKR's own pacing rules — it smooths bursts so a chatty agent doesn't trip the server-side limits that would otherwise throw Error 162 or silently drop the socket. It is a per-process, per-method throttle rather than a global token bucket, so it bounds the common case rather than guaranteeing compliance under heavy parallel use.

## Options & Derivatives

### Option chain with Greeks (`get_option_chain`)

Retrieves an option chain for an underlying — strikes, per-contract bid/ask/last, volume, open interest, and the full model Greeks plus implied volatility for each strike. It resolves the underlying, asks IBKR for the option parameters, and then does real work to pick the *right* chain: underlyings like SPY expose several trading classes per exchange, and this tool prefers the SMART-routed class whose `tradingClass` matches the symbol and carries the most strikes, which avoids latching onto stale secondary classes like `2SPY` that only hold a handful of legacy strikes. An explicit `trading_class` override is supported for cases like SPX weeklies (`SPXW`). The caller can pre-prune by strike window and by right (calls, puts, or both), and a `max_strikes` cap keeps the request under IBKR's 100-simultaneous-market-data-line limit — with both rights this becomes up to twice the cap in streaming contracts, so the cap is the lever that keeps a chain pull legal. *Known limitation: when the surviving strike list is larger than the cap, the tool truncates around the middle of the list as an at-the-money heuristic, which misfires for indices whose strike list is not centered on spot (an SPX pull has returned strikes far from the actual index level). Center-on-spot is a known open item.*

### Multi-leg combo orders for credit spreads (`place_combo_order`)

Builds and (when fully unlocked) places an atomic multi-leg BAG order, designed for credit-spread structures — e.g. selling a bull-put spread as a single net-credit order rather than two legged-in trades. The caller supplies the legs as conId + action (+ ratio), a BAG-level action and limit price, time-in-force, and quantity. By default the tool runs in `dry_run` mode: it validates the legs, assembles the BAG contract and order, and returns exactly what *would* be submitted without sending anything — making it safe to call for plan-and-confirm workflows. Live submission is fenced behind three software gates (the `ENABLE_LIVE_TRADING` flag, the `MAX_ORDER_SIZE` quantity cap, and an explicit `dry_run=false`) and a fourth gate that is *not* in the server's control: IB Gateway's Read-Only API flag, which when on (the project's default) causes IBKR to reject the order with Error 201. The tool surfaces that condition with a clear hint rather than failing opaquely. *Status: this is the only write/execution tool and it has never been run end-to-end with `dry_run=false`, even on paper — it is coded and dry-run-validated, not execution-proven. Treat live order placement as unverified until a paper round-trip confirms it.*

### German leverage products via ISIN (IOPT / WAR on Börse Stuttgart)

The contract builder understands the IOPT and WAR security types used by German Faktor-Optionsscheine and Zertifikate, which do not resolve on SMART/IBIS/FWB/GETTEX and instead must be addressed by ISIN on Börse Stuttgart (SWB) in EUR (`client.py`). When the caller passes an ISIN and leaves the exchange at its default, the builder automatically targets the SWB/EUR pair and sets the `ISIN` security-ID type; an explicit exchange override hands currency control back to the caller. This is what makes the server usable for the factor-certificate workflow at all. *Status: the read path is shipped and was verified resolving a real German cert by ISIN, with the caveat that SWB historical bars need `MIDPOINT` (not TRADES) and that realtime SWB quotes require a Stuttgart/EUWAX data subscription that isn't purchased, so off-hours quotes come back NaN/halted. ISIN/IOPT/WAR are currently threaded only into `get_market_data` and `get_historical_bars` — the option-chain and combo-order tools still default to US stock/USD, so certs are read-only and single-instrument for now.*

## Shortability & Risk

### Short-availability check (`check_shortable_shares`)

Reports whether a symbol can be shorted, the available shortable share count (with "Unlimited" surfaced for the IBKR -1 sentinel), and the current price/bid/ask alongside the contract ID. Because `ib_async` 2.x removed the dedicated shortable-shares call, this tool reconstructs the data the modern way: it subscribes to generic tick 236 on a market-data request and reads the `shortable` and `shortableShares` fields off the ticker after a brief settle, mapping IBKR's numeric codes to readable labels (Available / Hard to borrow / Not available). It accepts a comma-separated symbol list and reports per symbol. *Status: shipped; like the other quote-dependent tools, the live populated values were last seen against a closed market (returning "Unknown"), so a regular-hours retest is pending.*

### Margin requirements via what-if orders (`get_margin_requirements`)

Returns the initial and maintenance margin impact of a position, plus the equity-with-loan change and commission estimates. IBKR has no direct "what's the margin for this symbol" call, so the tool does the canonical thing: it submits a what-if order (validated, never placed) for one share and reads the margin-change fields off the resulting order state, with the per-share numbers meant to be scaled linearly for size. Crucially, it handles the Read-Only-API case gracefully — a what-if order is rejected with Error 321 when the Gateway is read-only, and `ib_async` would otherwise hang waiting for an event that never fires, so the tool wraps the call in a hard timeout and returns a structured `blocked: true` explanation pointing at the exact Gateway setting to change. *Status: the read path returns blocked-with-hint under the project's default read-only Gateway; real margin numbers are only available once Read-Only API is turned off, which is itself gated behind external clearances.*

### Composite short-selling analysis (`short_selling_analysis`)

A convenience aggregator that runs both the shortable check and the margin lookup across a list of symbols and returns them together with a small summary (symbol count, how many came back shortable, and any per-symbol errors). It is purely a composition of the two tools above — no new IBKR call — so it inherits both of their maturity caveats, but it saves a round of orchestration when assessing a basket of short candidates at once.

## News & Fundamentals

### Ticker news headlines (`get_news`)

Pulls news headlines tied to a stock via IBKR's historical-news endpoint, returning each headline's timestamp, provider code, article ID, and text, with an optional `fetch_bodies` mode that retrieves the full article body per headline (slow, one extra API call each). It defaults to the three news providers that are free with API access — Briefing.com general (BRFG) and analyst actions (BRFUPDN), plus Dow Jones (DJNL) — and the tool documents that Reuters and the full Dow Jones newswire are not API-exposed (TWS panel only), with paid add-ons like Benzinga Pro and Fly on the Wall available if richer coverage is needed. A time window can be specified or left open for "as far back as available up to now."

### Reuters/Refinitiv fundamentals (`get_fundamentals`)

Fetches Reuters/Refinitiv fundamental reports for a stock and returns the raw XML for the caller to parse. Six report types are supported: ReportSnapshot (P/E, ratios, market cap, beta), ReportsFinSummary (four-quarter EPS/revenue history), ReportRatios, ReportsFinStatements (balance sheet / income statement / cash flow), RESC (analyst EPS estimates), and CalendarReport (earnings dates within roughly ±3 weeks). The tool returns the XML unparsed by design — fundamentals XML is dense and varied, and leaving parsing to the consumer keeps the tool honest about what IBKR actually sent. *Note: this requires the Reuters Worldwide Fundamentals subscription (around USD 11/month for non-pro users); without it the call returns a structured error with that hint rather than empty data.*

## Order Execution

The server's only execution capability is the multi-leg combo order documented under **Options & Derivatives** (`place_combo_order`). There is no single-leg equity, single-leg option, or single-leg IOPT/WAR order tool today — those are roadmap items, not shipped code. Worth stating plainly because it shapes how the server should be used: every other tool in the inventory is read-only, and the one write tool defaults to a dry run and is fenced behind both software gates and a Gateway-level Read-Only flag. In its current configuration the server effectively *cannot* place a real order even if asked, which is the intended safety posture for a paper-first, clearance-gated trading workflow. The forward-looking execution roadmap (single-leg cert orders, an end-to-end paper round-trip of the combo path) lives in `BACKLOG.md`.

## Operational characteristics

### Paper-first, delayed-data-by-default

By default the server connects to a paper account and runs at delayed market-data tier — on connect it issues `reqMarketDataType(3)` (15-minute delayed), which still delivers data even when the linked account holds no realtime subscriptions. This means quotes, chains, and shortable data are typically 10–20 minutes stale unless a US Securities snapshot/streaming subscription (or the paper data-sharing toggle) is enabled, and the German-cert (SWB) realtime path needs a separate Stuttgart/EUWAX subscription that isn't purchased. The market-data type is configurable (`IBKR_MARKET_DATA_TYPE`), so the delayed default is a setting, not a hard wall — but treat delayed-by-default as the operating assumption.

### IBKR pacing-limit awareness

The server is built around IBKR's published TWS API pacing limits rather than fighting them: roughly 50 messages per second to the socket, a 100-line cap on simultaneous market-data subscriptions (which is why the option-chain tool exposes a strike cap), and the historical-data pacing rules of at most 60 requests per rolling 10-minute window (BID_ASK counted twice), no more than 6 identical requests in 2 seconds, and no more than 50 simultaneous open historical requests. These limits are documented inline in the tools that touch them and are softened in practice by the per-method client-side rate limiting described under Market Data. The practical takeaway is that the server is shaped to stay within IBKR's good graces, but a sufficiently aggressive parallel caller can still exceed the server-side limits.

### Read-only safety posture

The server layers safety in depth. Order placement is the only mutating operation and it is gated four ways (the live-trading flag, a max-order-size quantity cap, an explicit non-dry-run argument, and the Gateway's own Read-Only API flag). The recommended setup keeps IB Gateway in Read-Only API mode, which means even margin what-if orders are rejected — the server detects that and explains it rather than hanging. Crucially, turning any of these off is deliberately *outside* the server's automatic control: the Read-Only flag is a Gateway setting a human must change, and the project treats enabling live order routing as gated on external clearances. *Caution: `config.py` ships `MAX_ORDER_SIZE` defaulting to 1000 while the project intent is a cap of 1 contract — reconcile this before any order routing is enabled, since the quantity gate is the last line of defense against a fat-finger combo order.*

### Configuration anchored to the install directory

All runtime settings load from a `.env` at the repository root, and the config loader pins that path via `Path(__file__)` rather than the current working directory (`config.py`). This is a deliberate fix: a Claude Code or Claude Desktop MCP launcher spawns the server with a working directory of its own choosing, and without anchoring, the settings loader would pick up an unrelated `.env` from the host's cwd and crash on unexpected keys. The loader also ignores extra keys for the same robustness reason. Connection host/port/client-ID, the paper flag, the market-data type, account defaults, logging, and the trading-safety knobs are all driven from this file.

### Graceful lifecycle and MCP-clean logging

The server installs SIGINT/SIGTERM handlers for graceful shutdown and always disconnects cleanly from the Gateway on exit. Logging is MCP-aware: when running as an MCP server it routes all log output to stderr (never stdout) so that nothing pollutes the JSON-RPC stream the Claude client reads, while in `--test` mode it uses a rich console. A `--test` entrypoint connects, discovers accounts, and confirms the tool count, giving a quick "is this install healthy" check independent of any Claude client.

### Soft-fork model

This repository is a soft fork of `phense/ibkr-mcp-server`, itself based on the archived `ArjunDivecha/ibkr-mcp-server` (MIT). Eight tools — connection, accounts, account summary, portfolio, shortability, margin, and the short-selling composite — are inherited from upstream; six were added by this fork for a credit-spread and German-cert workflow: market data, historical bars, option chain, fundamentals, news, and the combo order. The fork's distinguishing fixes adapt the smoke test to the current `mcp` library, pin `.env` loading to the repo root, and update the account-summary call for `ib_async` 2.1. Extensions land as ordinary commits on this fork's `main` (there is no `patches/` directory), and upstream is preserved as a git remote for attribution. The license remains MIT. *Note: the inline `docs/API.md` is an upstream stub that predates this fork — it lists tools that don't exist here and omits the six added ones; this FEATURES.md, the README tool inventory, and the tool schemas in `tools.py` are the accurate sources.*
