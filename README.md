# IBKR MCP Server (phense fork)

A Model Context Protocol (MCP) server for Interactive Brokers, written in Python on top of `ib_async`. Speaks the TWS API directly over the IB Gateway / TWS socket (port 4002 paper / 4001 live by default).

> **Soft fork.** Originally based on [ArjunDivecha/ibkr-mcp-server](https://github.com/ArjunDivecha/ibkr-mcp-server) (MIT, archived 2025). This fork (a) fixes API drift in newer dependencies, (b) corrects the documented tool surface, and (c) adds six market-data / fundamentals / execution tools (get_market_data, get_historical_bars, get_option_chain, get_fundamentals, get_news, place_combo_order) needed for a credit-spread trading workflow. Upstream is preserved as the `upstream` git remote for attribution. The MIT license is unchanged.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Tool inventory (16 tools)

**Account + risk (8, inherited from upstream):**
| Tool | Returns |
|---|---|
| `get_connection_status` | host / port / client_id / current_account / paper_trading flag |
| `get_accounts` | list of available account IDs and currently selected one |
| `switch_account` | switches the currently selected account |
| `get_account_summary` | NetLiq, Cash, BuyingPower, GrossPosValue, FullInit/MaintMarginReq, EquityWithLoanValue |
| `get_portfolio` | positions: symbol, qty, avgCost, marketPrice, marketValue, unrealized/realized PnL |
| `check_shortable_shares` | shortable shares available + borrow fee rate |
| `get_margin_requirements` | initial + maintenance margin per symbol |
| `short_selling_analysis` | composite shortable + margin summary |

**Market data, fundamentals, execution (6, added in this fork):**
| Tool | Returns |
|---|---|
| `get_market_data` | last/bid/ask/volume/high/low/close for any security type; Greeks + IV for OPT contracts |
| `get_historical_bars` | OHLCV bars; respects IBKR pacing (60/10min, 6 identical/2s, BID_ASK 2× counted) |
| `get_option_chain` | strikes + per-strike Bid/Ask + Delta/Gamma/Vega/Theta + IV + OI + Volume, pre-prunable |
| `get_fundamentals` | Reuters/Refinitiv reports: ReportSnapshot / ReportsFinSummary / ReportRatios / RESC / CalendarReport (raw XML) |
| `get_news` | News headlines via reqHistoricalNews (default BRFG/BRFUPDN/DJNL); optional article bodies |
| `place_combo_order` | atomic multi-leg BAG order for credit spreads. Triple-gated (ENABLE_LIVE_TRADING + MAX_ORDER_SIZE + dry_run=false) |

**Broker read-back surface (2, added in this fork):**
| Tool | Returns |
|---|---|
| `get_open_orders` | ALL open/working orders across every API client id on the gateway (reqAllOpenOrders) — verifies the broker's actual order book incl. orders placed by other clients (e.g. an execution engine) |
| `get_executions` | TODAY's fills across all API clients (reqExecutions), incl. per-fill commission and realized PnL where reported. IBKR serves current-day fills only |

**Client-id resilience:** if the configured `IBKR_CLIENT_ID` is already registered on the gateway (Error 326 — a parallel session or an orphaned process), connect walks the reserved id band (base..base+8) instead of failing, and logs the substitution.

**Data tier:** Without market-data subscriptions, the MCP runs at `reqMarketDataType(3)` (15-min delayed) — applied automatically on connect. See [docs/API.md](docs/API.md) for the per-tool quirks and subscription tiers needed for realtime quotes.

## Quick start

### Prerequisites
- Python 3.10 or higher (project tested with 3.13)
- Interactive Brokers account with TWS or IB Gateway running (paper or live)
- Claude Desktop or Claude Code

### Installation

```bash
git clone https://github.com/phense/ibkr-mcp-server.git
cd ibkr-mcp-server
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # then edit .env with your IBKR settings
```

### Verify

Start IB Gateway / TWS, ensure the API is enabled, then:

```bash
python -m ibkr_mcp_server.main --test
```

Expected: `Loaded 14 tools, All tests passed`.

If clientId 1 is already taken by a running MCP instance, run with a unique ID: `IBKR_CLIENT_ID=99 python -m ibkr_mcp_server.main --test`.

### Claude Code integration

In your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "ibkr": {
      "command": "/absolute/path/to/ibkr-mcp-server/.venv/bin/python",
      "args": ["-m", "ibkr_mcp_server.main"],
      "cwd": "/absolute/path/to/ibkr-mcp-server"
    }
  }
}
```

Restart Claude Code; `/mcp` should list `ibkr` as connected.

### Claude Desktop integration

Add the same block to `claude_desktop_config.json` (location varies by OS).

## Configuration

`.env` controls all runtime settings:

```env
# IBKR Connection
IBKR_HOST=127.0.0.1
IBKR_PORT=4002          # 4002=Gateway Paper, 4001=Gateway Live, 7497=TWS Paper, 7496=TWS Live
IBKR_CLIENT_ID=1
IBKR_IS_PAPER=true

# Safety
ENABLE_LIVE_TRADING=false   # MUST stay false until order tools are implemented + you've explicitly cleared them
MAX_ORDER_SIZE=1            # cap, in contracts/shares

# Logging
LOG_LEVEL=INFO
```

`.env` should be `chmod 600` and is `.gitignore`d.

### TWS / Gateway setup

1. Start IB Gateway (recommended over TWS for headless / API-only use)
2. Configure → API → Settings:
   - ✅ Enable ActiveX and Socket Clients
   - ✅ Read-Only API (keep ON until you explicitly need order placement)
   - Socket port: 4002 (paper) or 4001 (live)
   - Trusted IPs: add `127.0.0.1`
3. The Gateway logs out daily around midnight ET. Auto-restart is not configured in this repo — restart manually or set up a scheduled relauncher.

## Safety

⚠️ **Read this before pointing this at a live account.**

- Paper-trade first. Always.
- The current build cannot place orders even if you flip `ENABLE_LIVE_TRADING=true` — order tools are on the roadmap, not implemented.
- When order tools land, they will be gated behind `ENABLE_LIVE_TRADING=true` AND `MAX_ORDER_SIZE` AND the IB Gateway's "Read-Only API" flag. All three must be set permissively for a live order to even attempt to route.
- IBKR market-data subscriptions on the linked live account are inherited by paper accounts. Without subscriptions, expect 15-minute-delayed data.
- This software is for educational and personal trading use. No warranty. Use at your own risk.

## Changes from upstream (Arjun's repo)

This fork's `main` includes three fixes on top of upstream `main` (commit hashes will change as the history is consolidated):

1. **`fix(test)`** — adapt smoke test to use the `TOOLS` constant directly instead of calling the `@server.list_tools()` decorator (the decorator no longer returns the list directly in current `mcp` library versions).
2. **`fix(config)`** — pin `.env` loading to the repo root and ignore extra keys, so the MCP doesn't crash when the launcher's working directory differs from the install location.
3. **`fix(client)`** — adapt `account_summary` to `ib_async` 2.1, which changed the `reqAccountSummaryAsync(account, tags)` signature.

Plus the roadmap tools listed above will land as additional commits on this fork's `main`.

## Development

```bash
# Run tests
pytest tests/ -v

# Lint / format
black ibkr_mcp_server/
isort ibkr_mcp_server/
mypy ibkr_mcp_server/
```

## Documentation

- [API Reference](docs/API.md)
- [Deployment Guide](docs/DEPLOYMENT.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)

## License

MIT License — see [LICENSE](LICENSE). Original copyright © 2024 Arjun Divecha; fork changes © 2026 Peter Hense, distributed under the same MIT terms.

## Credits

- [ArjunDivecha/ibkr-mcp-server](https://github.com/ArjunDivecha/ibkr-mcp-server) — original project, archived 2025
- [`ib_async`](https://github.com/ib-api-reloaded/ib-async) — the maintained successor to `ib_insync`, the awaitable Python wrapper around the TWS API
- [Model Context Protocol](https://modelcontextprotocol.io) — the spec that lets MCPs talk to Claude
