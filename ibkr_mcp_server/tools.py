"""MCP tools for IBKR functionality."""

import json
from typing import Any, Sequence

from mcp.server import Server
from mcp.types import Tool, TextContent, CallToolRequest

from .client import ibkr_client
from .utils import validate_symbols, IBKRError


# Create the server instance
server = Server("ibkr-mcp")


# Define all tools
TOOLS = [
    Tool(
        name="get_portfolio",
        description="Retrieve current portfolio positions and P&L from IBKR",
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "Account ID (optional, uses current account if not specified)"}
            },
            "additionalProperties": False
        }
    ),
    Tool(
        name="get_account_summary", 
        description="Get account balances and key metrics from IBKR",
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "Account ID (optional, uses current account if not specified)"}
            },
            "additionalProperties": False
        }
    ),
    Tool(
        name="switch_account",
        description="Switch between IBKR accounts",
        inputSchema={
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Account ID to switch to"}
            },
            "required": ["account_id"],
            "additionalProperties": False
        }
    ),
    Tool(
        name="get_accounts",
        description="Get available IBKR accounts and current account", 
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False}
    ),
    Tool(
        name="check_shortable_shares",
        description="Check short selling availability for securities",
        inputSchema={
            "type": "object",
            "properties": {
                "symbols": {"type": "string", "description": "Comma-separated list of symbols"},
                "account": {"type": "string", "description": "Account ID (optional, uses current account if not specified)"}
            },
            "required": ["symbols"],
            "additionalProperties": False
        }
    ),
    Tool(
        name="get_margin_requirements",
        description="Get margin requirements for securities",
        inputSchema={
            "type": "object",
            "properties": {
                "symbols": {"type": "string", "description": "Comma-separated list of symbols"},
                "account": {"type": "string", "description": "Account ID (optional, uses current account if not specified)"}
            },
            "required": ["symbols"],
            "additionalProperties": False
        }
    ),
    Tool(
        name="short_selling_analysis",
        description="Complete short selling analysis: availability, margin requirements, and summary",
        inputSchema={
            "type": "object",
            "properties": {
                "symbols": {"type": "string", "description": "Comma-separated list of symbols"},
                "account": {"type": "string", "description": "Account ID (optional, uses current account if not specified)"}
            },
            "required": ["symbols"],
            "additionalProperties": False
        }
    ),
    Tool(
        name="get_connection_status",
        description="Check IBKR TWS/Gateway connection status and account information",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False}
    ),
    Tool(
        name="get_option_chain",
        description=(
            "Get option chain for an underlying — strikes, Greeks (delta/gamma/vega/theta), "
            "IV, bid/ask, volume, open interest per contract. Pre-prune with min_strike/max_strike/right "
            "to respect the 100-line market-data cap. Without OPRA subscription, returns 15-min-delayed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "underlying": {"type": "string", "description": "Underlying ticker (e.g. SPY, AAPL)."},
                "expiry": {"type": "string", "default": "nearest", "description": "'nearest' (earliest available) or 'YYYYMMDD' / 'YYYYMM' (prefix match)."},
                "min_strike": {"type": ["number", "null"], "default": None},
                "max_strike": {"type": ["number", "null"], "default": None},
                "right": {"type": "string", "enum": ["C", "P", "BOTH"], "default": "BOTH"},
                "underlying_sec_type": {"type": "string", "default": "STK"},
                "underlying_exchange": {"type": "string", "default": "SMART"},
                "underlying_currency": {"type": "string", "default": "USD"},
                "max_strikes": {"type": "integer", "default": 40, "description": "Cap on strikes streamed (respects 100-line cap; with both rights this becomes 2×)."},
                "wait_seconds": {"type": "number", "default": 3.0, "description": "How long to wait for ticks/Greeks to populate before snapshotting."}
            },
            "required": ["underlying"],
            "additionalProperties": False
        }
    ),
    Tool(
        name="get_historical_bars",
        description=(
            "Fetch historical OHLCV bars for a stock/index/future/option/forex. "
            "Respects IBKR pacing limits (60 requests per 10 minutes, 6 identical per 2 seconds). "
            "Durations like '30 D', '1 Y'. Bar sizes like '1 day', '5 mins'. "
            "what_to_show: TRADES | MIDPOINT | BID | ASK | BID_ASK | HISTORICAL_VOLATILITY | OPTION_IMPLIED_VOLATILITY."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "sec_type": {"type": "string", "enum": ["STK", "IND", "FUT", "OPT", "CASH"], "default": "STK"},
                "exchange": {"type": "string", "default": "SMART"},
                "currency": {"type": "string", "default": "USD"},
                "expiry": {"type": "string", "description": "YYYYMMDD (OPT) or YYYYMM (FUT)."},
                "strike": {"type": "number"},
                "right": {"type": "string", "enum": ["C", "P", ""]},
                "duration": {"type": "string", "default": "30 D", "description": "e.g. '60 S', '1 D', '1 W', '1 M', '6 M', '1 Y'."},
                "bar_size": {"type": "string", "default": "1 day", "description": "e.g. '1 min', '5 mins', '1 hour', '1 day', '1 week'."},
                "what_to_show": {"type": "string", "default": "TRADES"},
                "use_rth": {"type": "boolean", "default": True, "description": "Regular trading hours only."},
                "end_datetime": {"type": "string", "default": "", "description": "Empty for now; or 'YYYYMMDD HH:MM:SS UTC'."}
            },
            "required": ["symbol"],
            "additionalProperties": False
        }
    ),
    Tool(
        name="get_market_data",
        description=(
            "Get a market-data quote (last/bid/ask/volume/high/low/close) for a stock, index, "
            "future, option, or forex pair. For options, also returns delta/gamma/vega/theta/IV. "
            "Without a market-data subscription, IBKR returns 15-min-delayed values."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker symbol (e.g. AAPL, SPX, ES, EUR)"},
                "sec_type": {"type": "string", "enum": ["STK", "IND", "FUT", "OPT", "CASH"], "default": "STK"},
                "exchange": {"type": "string", "default": "SMART", "description": "Exchange routing. 'SMART' for stocks, 'CBOE' for SPX, 'CME' for ES, 'IDEALPRO' for forex."},
                "currency": {"type": "string", "default": "USD"},
                "expiry": {"type": "string", "description": "YYYYMMDD (OPT) or YYYYMM (FUT). Empty for STK/IND/CASH."},
                "strike": {"type": "number", "description": "Strike price (OPT only)."},
                "right": {"type": "string", "enum": ["C", "P", ""], "description": "Call (C) or Put (P) for options."},
                "snapshot": {"type": "boolean", "default": False, "description": "True for one-off snapshot (no Greeks/OI/HV). False for streaming with full tick list."},
                "generic_ticks": {"type": "string", "default": "100,101,104,106", "description": "Generic tick types: 100=OptVol, 101=OptOI, 104=HistVol, 106=ImpVol. Ignored in snapshot mode."},
                "wait_seconds": {"type": "number", "default": 2.5, "description": "How long to wait for streaming ticks before reading the snapshot."}
            },
            "required": ["symbol"],
            "additionalProperties": False
        }
    )
]


# Register tools list handler
@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return TOOLS


# Register tool call handler  
@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> Sequence[TextContent]:
    """Handle tool calls."""
    try:
        if name == "get_portfolio":
            account = arguments.get("account")
            positions = await ibkr_client.get_portfolio(account)
            return [TextContent(
                type="text",
                text=json.dumps(positions, indent=2)
            )]
            
        elif name == "get_account_summary":
            account = arguments.get("account")
            summary = await ibkr_client.get_account_summary(account)
            return [TextContent(
                type="text", 
                text=json.dumps(summary, indent=2)
            )]
            
        elif name == "switch_account":
            account_id = arguments["account_id"]
            result = await ibkr_client.switch_account(account_id)
            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]
            
        elif name == "get_accounts":
            accounts = await ibkr_client.get_accounts()
            return [TextContent(
                type="text",
                text=json.dumps(accounts, indent=2)
            )]
            
        elif name == "check_shortable_shares":
            symbols = arguments["symbols"]
            account = arguments.get("account")
            try:
                symbol_list = validate_symbols(symbols)
                results = []
                for symbol in symbol_list:
                    shortable_info = await ibkr_client.get_shortable_shares(symbol, account)
                    results.append({
                        "symbol": symbol,
                        "shortable_shares": shortable_info
                    })
                return [TextContent(
                    type="text",
                    text=json.dumps(results, indent=2)
                )]
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=f"Error checking shortable shares: {str(e)}"
                )]
                
        elif name == "get_margin_requirements":
            symbols = arguments["symbols"]
            account = arguments.get("account")
            try:
                symbol_list = validate_symbols(symbols)
                results = []
                for symbol in symbol_list:
                    margin_info = await ibkr_client.get_margin_requirements(symbol, account)
                    results.append({
                        "symbol": symbol,
                        "margin_requirements": margin_info
                    })
                return [TextContent(
                    type="text",
                    text=json.dumps(results, indent=2)
                )]
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=f"Error getting margin requirements: {str(e)}"
                )]
                
        elif name == "short_selling_analysis":
            symbols = arguments["symbols"]
            account = arguments.get("account")
            try:
                symbol_list = validate_symbols(symbols)
                analysis = await ibkr_client.short_selling_analysis(symbol_list, account)
                return [TextContent(
                    type="text",
                    text=json.dumps(analysis, indent=2)
                )]
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=f"Error performing short selling analysis: {str(e)}"
                )]
                
        elif name == "get_connection_status":
            status = {
                "connected": ibkr_client.is_connected(),
                "host": ibkr_client.host,
                "port": ibkr_client.port,
                "client_id": ibkr_client.client_id,
                "current_account": ibkr_client.current_account,
                "available_accounts": ibkr_client.accounts,
                "paper_trading": ibkr_client.is_paper
            }
            return [TextContent(
                type="text",
                text=json.dumps(status, indent=2)
            )]

        elif name == "get_option_chain":
            result = await ibkr_client.get_option_chain(
                underlying=arguments["underlying"],
                expiry=arguments.get("expiry", "nearest"),
                min_strike=arguments.get("min_strike"),
                max_strike=arguments.get("max_strike"),
                right=arguments.get("right", "BOTH"),
                underlying_sec_type=arguments.get("underlying_sec_type", "STK"),
                underlying_exchange=arguments.get("underlying_exchange", "SMART"),
                underlying_currency=arguments.get("underlying_currency", "USD"),
                max_strikes=arguments.get("max_strikes", 40),
                wait_seconds=arguments.get("wait_seconds", 3.0),
            )
            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2, default=str)
            )]

        elif name == "get_historical_bars":
            result = await ibkr_client.get_historical_bars(
                symbol=arguments["symbol"],
                sec_type=arguments.get("sec_type", "STK"),
                exchange=arguments.get("exchange", "SMART"),
                currency=arguments.get("currency", "USD"),
                expiry=arguments.get("expiry", ""),
                strike=arguments.get("strike", 0.0),
                right=arguments.get("right", ""),
                duration=arguments.get("duration", "30 D"),
                bar_size=arguments.get("bar_size", "1 day"),
                what_to_show=arguments.get("what_to_show", "TRADES"),
                use_rth=arguments.get("use_rth", True),
                end_datetime=arguments.get("end_datetime", ""),
            )
            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2, default=str)
            )]

        elif name == "get_market_data":
            result = await ibkr_client.get_market_data(
                symbol=arguments["symbol"],
                sec_type=arguments.get("sec_type", "STK"),
                exchange=arguments.get("exchange", "SMART"),
                currency=arguments.get("currency", "USD"),
                expiry=arguments.get("expiry", ""),
                strike=arguments.get("strike", 0.0),
                right=arguments.get("right", ""),
                snapshot=arguments.get("snapshot", False),
                generic_ticks=arguments.get("generic_ticks", "100,101,104,106"),
                wait_seconds=arguments.get("wait_seconds", 2.5),
            )
            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2, default=str)
            )]
        
        else:
            return [TextContent(
                type="text",
                text=f"Unknown tool: {name}"
            )]
            
    except Exception as e:
        return [TextContent(
            type="text",
            text=f"Error executing tool {name}: {str(e)}"
        )]
