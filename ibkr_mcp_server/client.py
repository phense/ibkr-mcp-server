"""IBKR Client with advanced trading capabilities."""

import asyncio
import logging
from typing import Dict, List, Optional, Union
from decimal import Decimal

from ib_async import IB, Stock, Index, Future, Option, Forex, Contract, util
from .config import settings
from .utils import rate_limit, retry_on_failure, safe_float, safe_int, ValidationError, ConnectionError as IBKRConnectionError


def _make_contract(
    symbol: str,
    sec_type: str = 'STK',
    exchange: str = 'SMART',
    currency: str = 'USD',
    expiry: str = '',
    strike: float = 0.0,
    right: str = '',
) -> Contract:
    """Build an ib_async Contract for any supported security type.

    sec_type: STK | IND | FUT | OPT | CASH (default: STK).
    For FUT/OPT, `expiry` is YYYYMMDD or YYYYMM. OPT additionally needs strike+right (C/P).
    """
    sec_type = sec_type.upper()
    if sec_type == 'STK':
        return Stock(symbol, exchange, currency)
    if sec_type == 'IND':
        # Indices need a real exchange — CBOE for SPX/VIX, NASDAQ for COMP/NDX
        return Index(symbol, exchange if exchange != 'SMART' else 'CBOE', currency)
    if sec_type == 'FUT':
        return Future(
            symbol,
            lastTradeDateOrContractMonth=expiry,
            exchange=exchange if exchange != 'SMART' else 'CME',
            currency=currency,
        )
    if sec_type == 'OPT':
        return Option(
            symbol,
            lastTradeDateOrContractMonth=expiry,
            strike=strike,
            right=right.upper(),
            exchange=exchange,
            currency=currency,
        )
    if sec_type == 'CASH':
        # Forex pairs: symbol is base currency, currency is quote
        return Forex(f'{symbol}{currency}', exchange if exchange != 'SMART' else 'IDEALPRO')
    # Generic fallback
    c = Contract()
    c.symbol = symbol
    c.secType = sec_type
    c.exchange = exchange
    c.currency = currency
    return c


class IBKRClient:
    """Enhanced IBKR client with multi-account and short selling support."""
    
    def __init__(self):
        self.ib: Optional[IB] = None
        self.logger = logging.getLogger(__name__)
        
        # Connection settings
        self.host = settings.ibkr_host
        self.port = settings.ibkr_port
        self.client_id = settings.ibkr_client_id
        self.max_reconnect_attempts = settings.max_reconnect_attempts
        self.reconnect_delay = settings.reconnect_delay
        self.reconnect_attempts = 0
        
        # Account management
        self.accounts: List[str] = []
        self.current_account: Optional[str] = settings.ibkr_default_account
        
        # Connection state
        self._connected = False
        self._connecting = False
    
    @property
    def is_paper(self) -> bool:
        """Check if this is a paper trading connection."""
        return self.port in [7497, 4002]  # Common paper trading ports
    
    async def _ensure_connected(self) -> bool:
        """Ensure IBKR connection is active, reconnect if needed."""
        if self.is_connected():
            return True
        
        try:
            await self.connect()
            return self.is_connected()
        except Exception as e:
            self.logger.error(f"Failed to ensure connection: {e}")
            return False
    
    @retry_on_failure(max_attempts=3)
    async def connect(self) -> bool:
        """Establish connection and discover accounts."""
        if self._connected and self.ib and self.ib.isConnected():
            return True
        
        if self._connecting:
            # Wait for ongoing connection attempt
            while self._connecting:
                await asyncio.sleep(0.1)
            return self._connected
        
        self._connecting = True
        
        try:
            self.ib = IB()
            
            self.logger.info(f"Connecting to IBKR at {self.host}:{self.port}...")
            await self.ib.connectAsync(
                host=self.host,
                port=self.port,
                clientId=self.client_id,
                timeout=10
            )
            
            # Setup event handlers
            self.ib.disconnectedEvent += self._on_disconnect
            self.ib.errorEvent += self._on_error

            # Apply market-data-type from settings (1=Live, 2=Frozen, 3=Delayed, 4=Delayed-Frozen).
            # With no realtime subscription on the linked account, type 3 still delivers data.
            try:
                self.ib.reqMarketDataType(settings.ibkr_market_data_type)
                self.logger.info(f"Market data type set to {settings.ibkr_market_data_type}")
            except Exception as e:
                self.logger.warning(f"reqMarketDataType({settings.ibkr_market_data_type}) failed: {e}")

            # Wait for connection to stabilize
            await asyncio.sleep(2)
            
            # Discover accounts
            self.accounts = self.ib.managedAccounts()
            if self.accounts:
                if not self.current_account or self.current_account not in self.accounts:
                    self.current_account = self.accounts[0]
                
                self.logger.info(f"Connected to IBKR. Accounts: {self.accounts}")
                self.logger.info(f"Current account: {self.current_account}")
            else:
                self.logger.warning("No managed accounts found")
            
            self._connected = True
            self.reconnect_attempts = 0
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to connect to IBKR: {e}")
            raise IBKRConnectionError(f"Connection failed: {e}")
        finally:
            self._connecting = False
    
    async def disconnect(self):
        """Clean disconnection."""
        if self.ib and self.ib.isConnected():
            self.ib.disconnect()
            self._connected = False
            self.logger.info("IBKR disconnected")
    
    def _on_disconnect(self):
        """Handle disconnection with automatic reconnection."""
        self._connected = False
        self.logger.warning("IBKR disconnected, scheduling reconnection...")
        asyncio.create_task(self._reconnect())
    
    def _on_error(self, reqId, errorCode, errorString, contract):
        """Centralized error logging."""
        # Don't log certain routine messages as errors
        if errorCode in [2104, 2106, 2158]:  # Market data warnings
            self.logger.debug(f"IBKR Info {errorCode}: {errorString}")
        else:
            self.logger.error(f"IBKR Error {errorCode}: {errorString} (reqId: {reqId})")
    
    async def _reconnect(self):
        """Background reconnection task."""
        try:
            await asyncio.sleep(self.reconnect_delay)
            await self.connect()
        except Exception as e:
            self.logger.error(f"Reconnection failed: {e}")
    
    def is_connected(self) -> bool:
        """Check connection status."""
        return self._connected and self.ib is not None and self.ib.isConnected()
    
    @rate_limit(calls_per_second=1.0)
    async def get_portfolio(self, account: Optional[str] = None) -> List[Dict]:
        """Get portfolio positions."""
        try:
            if not await self._ensure_connected():
                raise ConnectionError("Not connected to IBKR")
            
            account = account or self.current_account
            
            positions = await self.ib.reqPositionsAsync()
            
            portfolio = []
            for pos in positions:
                if not account or pos.account == account:
                    portfolio.append(self._serialize_position(pos))
            
            return portfolio
            
        except Exception as e:
            self.logger.error(f"Portfolio request failed: {e}")
            raise RuntimeError(f"IBKR API error: {str(e)}")
    
    @rate_limit(calls_per_second=1.0)
    async def get_account_summary(self, account: Optional[str] = None) -> List[Dict]:
        """Get account summary."""
        try:
            if not await self._ensure_connected():
                raise ConnectionError("Not connected to IBKR")
            
            account = account or self.current_account or ""

            wanted_tags = {
                'TotalCashValue', 'NetLiquidation', 'UnrealizedPnL', 'RealizedPnL',
                'GrossPositionValue', 'BuyingPower', 'EquityWithLoanValue',
                'PreviousDayEquityWithLoanValue', 'FullInitMarginReq', 'FullMaintMarginReq'
            }

            account_values = await self.ib.accountSummaryAsync(account)

            return [
                self._serialize_account_value(av)
                for av in account_values
                if av.tag in wanted_tags
            ]
            
        except Exception as e:
            self.logger.error(f"Account summary request failed: {e}")
            raise RuntimeError(f"IBKR API error: {str(e)}")
    
    @rate_limit(calls_per_second=0.5)
    async def get_shortable_shares(self, symbol: str, account: str = None) -> Dict:
        """Get short selling information for a symbol."""
        try:
            if not await self._ensure_connected():
                raise ConnectionError("Not connected to IBKR")
            
            contract = Stock(symbol, 'SMART', 'USD')
            
            # Qualify the contract
            qualified_contracts = await self.ib.reqContractDetailsAsync(contract)
            if not qualified_contracts:
                return {"error": "Contract not found"}
            
            qualified_contract = qualified_contracts[0].contract
            
            # Request shortable shares
            shortable_shares = await self.ib.reqShortableSharesAsync(qualified_contract)
            
            # Get current market data
            ticker = self.ib.reqMktData(qualified_contract, '', False, False)
            await asyncio.sleep(1.5)  # Wait for market data
            
            result = {
                "symbol": symbol,
                "shortable_shares": shortable_shares if shortable_shares != -1 else "Unlimited",
                "current_price": safe_float(ticker.last or ticker.close),
                "bid": safe_float(ticker.bid),
                "ask": safe_float(ticker.ask),
                "contract_id": qualified_contract.conId
            }
            
            # Clean up ticker
            self.ib.cancelMktData(qualified_contract)
            
            return result
            
        except Exception as e:
            self.logger.error(f"Error getting shortable shares for {symbol}: {e}")
            return {"error": str(e)}

    @retry_on_failure(max_attempts=2)
    async def get_margin_requirements(self, symbol: str, account: str = None) -> Dict:
        """Get margin requirements for a symbol."""
        try:
            if not await self._ensure_connected():
                raise ConnectionError("Not connected to IBKR")
                
            # Create contract
            contract = Stock(symbol, 'SMART', 'USD')
            await self.ib.qualifyContractsAsync([contract])
            
            if not contract.conId:
                return {"error": f"Invalid symbol: {symbol}"}
            
            # Get margin requirements - simplified for now
            # Note: IBKR API doesn't provide direct margin requirements
            # This would typically require additional market data subscriptions
            margin_info = {
                "symbol": symbol,
                "contract_id": contract.conId,
                "exchange": contract.exchange,
                "margin_requirement": "Market data subscription required",
                "note": "Use TWS for detailed margin calculations"
            }
            
            return margin_info
            
        except Exception as e:
            self.logger.error(f"Error getting margin info for {symbol}: {e}")
            return {"error": str(e)}

    async def short_selling_analysis(self, symbols: List[str], account: str = None) -> Dict:
        """Complete short selling analysis for multiple symbols."""
        try:
            if not await self._ensure_connected():
                raise ConnectionError("Not connected to IBKR")
            
            analysis = {
                "account": account or self.current_account,
                "symbols_analyzed": symbols,
                "shortable_data": {},
                "margin_data": {},
                "summary": {
                    "total_symbols": len(symbols),
                    "shortable_count": 0,
                    "errors": []
                }
            }
            
            # Get shortable shares data
            for symbol in symbols:
                try:
                    shortable_info = await self.get_shortable_shares(symbol, account)
                    analysis["shortable_data"][symbol] = shortable_info
                    
                    if "error" not in shortable_info:
                        analysis["summary"]["shortable_count"] += 1
                except Exception as e:
                    analysis["summary"]["errors"].append(f"{symbol}: {str(e)}")
            
            # Get margin requirements
            for symbol in symbols:
                try:
                    margin_info = await self.get_margin_requirements(symbol, account)
                    analysis["margin_data"][symbol] = margin_info
                except Exception as e:
                    analysis["summary"]["errors"].append(f"{symbol} margin: {str(e)}")
            
            return analysis
            
        except Exception as e:
            self.logger.error(f"Error in short selling analysis: {e}")
            return {"error": str(e)}
    
    async def switch_account(self, account_id: str) -> Dict:
        """Switch to a different IBKR account."""
        try:
            if account_id not in self.accounts:
                self.logger.error(f"Account {account_id} not found. Available: {self.accounts}")
                return {
                    "success": False,
                    "message": f"Account {account_id} not found",
                    "current_account": self.current_account,
                    "available_accounts": self.accounts
                }
            
            self.current_account = account_id
            self.logger.info(f"Switched to account: {account_id}")
            
            return {
                "success": True,
                "message": f"Switched to account: {account_id}",
                "current_account": self.current_account,
                "available_accounts": self.accounts
            }
            
        except Exception as e:
            self.logger.error(f"Error switching account: {e}")
            return {"success": False, "error": str(e)}

    @rate_limit(calls_per_second=2.0)
    async def get_market_data(
        self,
        symbol: str,
        sec_type: str = 'STK',
        exchange: str = 'SMART',
        currency: str = 'USD',
        expiry: str = '',
        strike: float = 0.0,
        right: str = '',
        snapshot: bool = False,
        generic_ticks: str = '100,101,104,106',
        wait_seconds: float = 2.5,
    ) -> Dict:
        """Get a market-data quote for any security type.

        Returns last/bid/ask/volume/high/low/close. For options, additionally
        returns delta/gamma/vega/theta/IV/underlying_price from model greeks.

        Without a market-data subscription, IBKR returns 15-min-delayed values via
        the `reqMarketDataType(3)` setting in .env. Snapshot mode does NOT accept
        a generic_ticks list — OI/Volume/HV/IV need streaming.
        """
        try:
            if not await self._ensure_connected():
                raise IBKRConnectionError("Not connected to IBKR")

            contract = _make_contract(
                symbol=symbol, sec_type=sec_type, exchange=exchange, currency=currency,
                expiry=expiry, strike=strike, right=right,
            )
            qualified = await self.ib.reqContractDetailsAsync(contract)
            if not qualified:
                return {"error": f"Contract not found for {symbol} ({sec_type})"}
            contract = qualified[0].contract

            # snapshot mode does not accept genericTickList
            ticks = '' if snapshot else generic_ticks
            ticker = self.ib.reqMktData(contract, ticks, snapshot, False)
            try:
                await asyncio.sleep(wait_seconds)
                result = {
                    "symbol": contract.symbol,
                    "secType": contract.secType,
                    "exchange": contract.exchange,
                    "currency": contract.currency,
                    "contract_id": contract.conId,
                    "last": safe_float(ticker.last),
                    "bid": safe_float(ticker.bid),
                    "ask": safe_float(ticker.ask),
                    "bid_size": safe_float(ticker.bidSize),
                    "ask_size": safe_float(ticker.askSize),
                    "volume": safe_float(ticker.volume),
                    "high": safe_float(ticker.high),
                    "low": safe_float(ticker.low),
                    "close": safe_float(ticker.close),
                    "halted": bool(getattr(ticker, 'halted', 0) or 0),
                    "delayed": ticker.last == 0 and ticker.close != 0,  # heuristic
                }
                # Option contracts: surface modelGreeks if present
                if contract.secType == 'OPT' and ticker.modelGreeks is not None:
                    mg = ticker.modelGreeks
                    result["greeks"] = {
                        "iv": safe_float(mg.impliedVol),
                        "delta": safe_float(mg.delta),
                        "gamma": safe_float(mg.gamma),
                        "vega": safe_float(mg.vega),
                        "theta": safe_float(mg.theta),
                        "underlying_price": safe_float(mg.undPrice),
                        "option_price": safe_float(mg.optPrice),
                    }
                # OI and HV/IV from generic ticks (only on streaming)
                if not snapshot:
                    result["open_interest_call"] = safe_float(getattr(ticker, 'callOpenInterest', 0))
                    result["open_interest_put"] = safe_float(getattr(ticker, 'putOpenInterest', 0))
                    result["historical_vol"] = safe_float(getattr(ticker, 'histVolatility', 0))
                    result["implied_vol_underlying"] = safe_float(getattr(ticker, 'impliedVolatility', 0))
                return result
            finally:
                if not snapshot:
                    self.ib.cancelMktData(contract)

        except Exception as e:
            self.logger.error(f"Market data request failed for {symbol}: {e}")
            return {"error": str(e)}

    @rate_limit(calls_per_second=0.5)
    async def get_historical_bars(
        self,
        symbol: str,
        sec_type: str = 'STK',
        exchange: str = 'SMART',
        currency: str = 'USD',
        expiry: str = '',
        strike: float = 0.0,
        right: str = '',
        duration: str = '30 D',
        bar_size: str = '1 day',
        what_to_show: str = 'TRADES',
        use_rth: bool = True,
        end_datetime: str = '',
    ) -> Dict:
        """Fetch historical OHLCV bars.

        Pacing limits (HARD — violation throws Error 162 or silent disconnect):
        - ≤ 60 historical-data requests per 10-minute window (BID_ASK counted 2×).
        - ≤ 6 identical (contract/endDate/barSize/whatToShow/RTH) requests in 2s.
        - ≤ 2 identical historical-data requests in 15s.
        - ≤ 50 simultaneous open historical-data requests.

        Common durations: '60 S', '1 D', '1 W', '1 M', '6 M', '1 Y', '5 Y'.
        Common bar sizes: '1 secs', '5 secs', '30 secs', '1 min', '5 mins',
        '15 mins', '1 hour', '1 day', '1 week', '1 month'.
        what_to_show: TRADES | MIDPOINT | BID | ASK | BID_ASK |
        HISTORICAL_VOLATILITY | OPTION_IMPLIED_VOLATILITY.
        """
        try:
            if not await self._ensure_connected():
                raise IBKRConnectionError("Not connected to IBKR")

            contract = _make_contract(
                symbol=symbol, sec_type=sec_type, exchange=exchange, currency=currency,
                expiry=expiry, strike=strike, right=right,
            )
            qualified = await self.ib.reqContractDetailsAsync(contract)
            if not qualified:
                return {"error": f"Contract not found for {symbol} ({sec_type})"}
            contract = qualified[0].contract

            bars = await self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime=end_datetime,
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow=what_to_show,
                useRTH=use_rth,
                formatDate=2,  # unix epoch — easier to parse than IB's localised strings
            )
            return {
                "symbol": contract.symbol,
                "secType": contract.secType,
                "exchange": contract.exchange,
                "currency": contract.currency,
                "duration": duration,
                "bar_size": bar_size,
                "what_to_show": what_to_show,
                "use_rth": use_rth,
                "bar_count": len(bars) if bars else 0,
                "bars": [
                    {
                        "date": str(b.date),
                        "open": safe_float(b.open),
                        "high": safe_float(b.high),
                        "low": safe_float(b.low),
                        "close": safe_float(b.close),
                        "volume": safe_float(b.volume),
                        "wap": safe_float(b.average),
                        "count": safe_int(b.barCount),
                    }
                    for b in (bars or [])
                ],
            }

        except Exception as e:
            self.logger.error(f"Historical bars request failed for {symbol}: {e}")
            return {"error": str(e)}

    async def get_accounts(self) -> Dict[str, Union[str, List[str]]]:
        """Get available accounts information."""
        try:
            if not await self._ensure_connected():
                await self.connect()
            
            return {
                "current_account": self.current_account,
                "available_accounts": self.accounts,
                "connected": self.is_connected(),
                "paper_trading": self.is_paper
            }
            
        except Exception as e:
            self.logger.error(f"Error getting accounts: {e}")
            return {"error": str(e)}
    
    def _serialize_position(self, position) -> Dict:
        """Convert Position to serializable dict."""
        return {
            "symbol": position.contract.symbol,
            "secType": position.contract.secType,
            "exchange": position.contract.exchange,
            "position": safe_float(position.position),
            "avgCost": safe_float(position.avgCost),
            "marketPrice": safe_float(getattr(position, 'marketPrice', 0)),
            "marketValue": safe_float(getattr(position, 'marketValue', 0)),
            "unrealizedPNL": safe_float(getattr(position, 'unrealizedPNL', 0)),
            "realizedPNL": safe_float(getattr(position, 'realizedPNL', 0)),
            "account": position.account
        }
    
    def _serialize_account_value(self, account_value) -> Dict:
        """Convert AccountValue to serializable dict."""
        return {
            "tag": account_value.tag,
            "value": account_value.value,
            "currency": account_value.currency,
            "account": account_value.account
        }


# Global client instance
ibkr_client = IBKRClient()
