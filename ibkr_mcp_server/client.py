"""IBKR Client with advanced trading capabilities."""

import asyncio
import logging
from typing import Dict, List, Optional, Union
from decimal import Decimal

from ib_async import IB, Stock, Index, Future, Option, Forex, Contract, ComboLeg, Order, util
from ib_async import ExecutionFilter
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
    isin: str = '',
) -> Contract:
    """Build an ib_async Contract for any supported security type.

    sec_type: STK | IND | FUT | OPT | CASH | IOPT | WAR (default: STK).
    For FUT/OPT, `expiry` is YYYYMMDD or YYYYMM. OPT additionally needs strike+right (C/P).
    For IOPT/WAR (German leverage products), pass `isin` — resolves on SWB/EUR.
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
    if sec_type in ('IOPT', 'WAR'):
        # German leverage products (Faktor-OS / Zertifikate) resolve by ISIN
        # on Börse Stuttgart (SWB) in EUR. SMART/IBIS/FWB/GETTEX do NOT resolve.
        # When the caller leaves exchange at the generic default (SMART), default
        # both exchange and currency to the SWB/EUR pair. An explicit exchange
        # override implies the caller also controls currency.
        c = Contract()
        if exchange != 'SMART':
            c.exchange = exchange
            c.currency = currency
        else:
            c.exchange = 'SWB'
            c.currency = 'EUR'
        c.secType = sec_type
        if isin:
            c.secIdType = 'ISIN'
            c.secId = isin
        if symbol:
            c.symbol = symbol
        return c
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
            await self._connect_with_client_id_fallback()

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

    # Client ids 1-9 are the MCP's reserved band on this gateway (the trading
    # engine's id family starts at 20). A gateway rejects a duplicate clientId
    # with Error 326, which ib_async surfaces as a connect TimeoutError — a
    # parallel session's MCP or an orphaned process can therefore wedge the
    # read-back surface on a fixed id. Walk the band instead of failing.
    CLIENT_ID_BAND = 9

    async def _connect_with_client_id_fallback(self):
        base = self.client_id
        last_exc: Exception = RuntimeError("no connect attempt made")
        for offset in range(self.CLIENT_ID_BAND):
            candidate = base + offset
            try:
                await self.ib.connectAsync(
                    host=self.host,
                    port=self.port,
                    clientId=candidate,
                    timeout=10
                )
                if candidate != base:
                    self.logger.warning(
                        f"clientId {base} unavailable (likely in use); connected as {candidate}")
                self.client_id = candidate
                return
            except (TimeoutError, asyncio.TimeoutError, ConnectionRefusedError) as e:
                last_exc = e
                if isinstance(e, ConnectionRefusedError):
                    break  # gateway down — other ids won't help
                self.logger.warning(f"connectAsync clientId={candidate} failed: {type(e).__name__}")
        raise last_exc

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

    @rate_limit(calls_per_second=1.0)
    async def get_open_orders(self, account: Optional[str] = None) -> List[Dict]:
        """Get ALL open/working orders on the gateway, across every API client id.

        Uses reqAllOpenOrders so orders placed by OTHER clients (e.g. an
        execution engine on its own client ids) are visible too — this is a
        read-back surface for verifying the broker's actual order book, not a
        view of this client's own orders.
        """
        try:
            if not await self._ensure_connected():
                raise ConnectionError("Not connected to IBKR")

            trades = await self.ib.reqAllOpenOrdersAsync()
            out = []
            for t in trades:
                order_account = getattr(t.order, 'account', '')
                if account and order_account and order_account != account:
                    continue
                out.append(self._serialize_open_order(t))
            return out

        except Exception as e:
            self.logger.error(f"Open-orders request failed: {e}")
            raise RuntimeError(f"IBKR API error: {str(e)}")

    @rate_limit(calls_per_second=1.0)
    async def get_executions(self, account: Optional[str] = None,
                             symbol: Optional[str] = None) -> List[Dict]:
        """Get TODAY's executions (fills) with commissions, across all API clients.

        IBKR only serves current-day executions over the API (older fills live in
        Flex reports). Read-back surface for verifying what actually filled after
        any order mutation.
        """
        try:
            if not await self._ensure_connected():
                raise ConnectionError("Not connected to IBKR")

            filt = ExecutionFilter()
            if account:
                filt.acctCode = account
            if symbol:
                filt.symbol = symbol
            fills = await self.ib.reqExecutionsAsync(filt)
            return [self._serialize_fill(f) for f in fills]

        except Exception as e:
            self.logger.error(f"Executions request failed: {e}")
            raise RuntimeError(f"IBKR API error: {str(e)}")

    @rate_limit(calls_per_second=0.5)
    async def get_shortable_shares(self, symbol: str, account: str = None) -> Dict:
        """Get short selling availability for a symbol.

        ib_async 2.x removed the standalone reqShortableSharesAsync. Shortable
        data now arrives as generic ticks on reqMktData:
          - tick 236 → ticker.shortable    (1=NotAvail, 2=HardToBorrow, 3=Available)
          - tick 236 → ticker.shortableShares  (numeric available count; -1=unlimited)
        We subscribe with generic tick "236" and read the ticker after a short wait.
        """
        try:
            if not await self._ensure_connected():
                raise ConnectionError("Not connected to IBKR")

            contract = Stock(symbol, 'SMART', 'USD')

            # Qualify the contract
            qualified_contracts = await self.ib.reqContractDetailsAsync(contract)
            if not qualified_contracts:
                return {"error": "Contract not found"}

            qualified_contract = qualified_contracts[0].contract

            # Subscribe with generic tick 236 for Shortable + ShortableShares.
            ticker = self.ib.reqMktData(qualified_contract, '236', False, False)
            try:
                await asyncio.sleep(2.0)  # Allow ticks to land

                # ticker.shortable: 1=NotAvail, 2=HardToBorrow, 3=Available
                shortable_code = getattr(ticker, 'shortable', None)
                shortable_label = {
                    1: "Not available",
                    2: "Hard to borrow",
                    3: "Available",
                }.get(int(shortable_code) if shortable_code and shortable_code > 0 else 0, "Unknown")

                # ticker.shortableShares: numeric count (-1 means "unlimited")
                shortable_shares_raw = getattr(ticker, 'shortableShares', None)
                shortable_shares = (
                    "Unlimited"
                    if shortable_shares_raw is not None and shortable_shares_raw == -1
                    else safe_float(shortable_shares_raw)
                )

                result = {
                    "symbol": symbol,
                    "shortable": shortable_label,
                    "shortable_code": safe_float(shortable_code),
                    "shortable_shares": shortable_shares,
                    "current_price": safe_float(ticker.last or ticker.close),
                    "bid": safe_float(ticker.bid),
                    "ask": safe_float(ticker.ask),
                    "contract_id": qualified_contract.conId,
                }
                return result
            finally:
                # Clean up ticker
                try:
                    self.ib.cancelMktData(qualified_contract)
                except Exception:
                    pass

        except Exception as e:
            self.logger.error(f"Error getting shortable shares for {symbol}: {e}")
            return {"error": str(e)}

    @retry_on_failure(max_attempts=2)
    async def get_margin_requirements(
        self,
        symbol: str,
        account: str = None,
        action: str = 'BUY',
        quantity: int = 1,
    ) -> Dict:
        """Get init/maint margin requirements for a single share via whatIfOrderAsync.

        IBKR's API has no direct "what's the margin for this symbol" call. The
        canonical way is to submit a what-if order (validates without placing)
        and read the OrderState's margin-change fields.

        Args:
          action: 'BUY' (long) or 'SELL' (short).
          quantity: number of shares for the whatIf — defaults to 1 so the
            returned margin numbers are per-share and easy to scale.
        """
        try:
            if not await self._ensure_connected():
                raise ConnectionError("Not connected to IBKR")

            # qualifyContractsAsync is varargs — pass Contract, NOT a list, otherwise
            # ib_async raises `'list' object has no attribute 'includeExpired'`.
            contract = Stock(symbol, 'SMART', 'USD')
            await self.ib.qualifyContractsAsync(contract)

            if not contract.conId:
                return {"error": f"Invalid symbol: {symbol}"}

            order = Order()
            order.action = action.upper()
            order.orderType = 'MKT'
            order.totalQuantity = quantity
            order.tif = 'DAY'
            if account or self.current_account:
                order.account = account or self.current_account

            # ib_async's whatIfOrderAsync awaits an event that never fires when the
            # order is rejected by Error 321 (Read-Only API mode). Wrap in a hard
            # timeout so we can surface that condition instead of hanging forever.
            ro_blocked = False
            ro_msg = ""

            def _on_err(reqId, errorCode, errorString, contract_):
                nonlocal ro_blocked, ro_msg
                if errorCode == 321 or 'Read-Only' in errorString:
                    ro_blocked = True
                    ro_msg = errorString

            self.ib.errorEvent += _on_err
            try:
                order_state = await asyncio.wait_for(
                    self.ib.whatIfOrderAsync(contract, order),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                if ro_blocked:
                    return {
                        "symbol": symbol,
                        "contract_id": contract.conId,
                        "blocked": True,
                        "reason": "IB Gateway is in Read-Only API mode — whatIfOrder rejected (Error 321).",
                        "ibkr_message": ro_msg,
                        "hint": (
                            "Disable 'Read-Only API' in Gateway → Configuration → API → "
                            "Settings, then restart the Gateway. Real margin numbers are only "
                            "available once Read-Only is OFF."
                        ),
                    }
                return {
                    "symbol": symbol,
                    "contract_id": contract.conId,
                    "error": "whatIfOrderAsync timed out without IB response",
                    "hint": "Gateway responsive but order-state event never fired. Check Gateway logs.",
                }
            except Exception as wif_e:
                msg = str(wif_e)
                if 'Read-Only' in msg or 'read-only' in msg.lower():
                    return {
                        "symbol": symbol,
                        "contract_id": contract.conId,
                        "blocked": True,
                        "reason": "IB Gateway is in Read-Only API mode — whatIfOrder rejected.",
                        "hint": (
                            "Disable 'Read-Only API' in Gateway → Configuration → API → "
                            "Settings, then restart the Gateway."
                        ),
                    }
                raise
            finally:
                try:
                    self.ib.errorEvent -= _on_err
                except Exception:
                    pass

            return {
                "symbol": symbol,
                "contract_id": contract.conId,
                "exchange": contract.exchange,
                "action": order.action,
                "quantity": quantity,
                "init_margin_change": safe_float(getattr(order_state, 'initMarginChange', None)),
                "maint_margin_change": safe_float(getattr(order_state, 'maintMarginChange', None)),
                "equity_with_loan_change": safe_float(getattr(order_state, 'equityWithLoanChange', None)),
                "commission": safe_float(getattr(order_state, 'commission', None)),
                "commission_currency": getattr(order_state, 'commissionCurrency', None),
                "min_commission": safe_float(getattr(order_state, 'minCommission', None)),
                "max_commission": safe_float(getattr(order_state, 'maxCommission', None)),
                "warning_text": getattr(order_state, 'warningText', None),
                "note": (
                    "Margins are absolute deltas (USD/EUR per account currency) for the "
                    f"whatIf {order.action} of {quantity} share(s). Scale linearly for size."
                ),
            }

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
        isin: str = '',
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
                expiry=expiry, strike=strike, right=right, isin=isin,
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
        isin: str = '',
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
                expiry=expiry, strike=strike, right=right, isin=isin,
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

    @rate_limit(calls_per_second=0.2)
    async def get_option_chain(
        self,
        underlying: str,
        expiry: str = 'nearest',
        min_strike: Optional[float] = None,
        max_strike: Optional[float] = None,
        right: str = 'BOTH',
        underlying_sec_type: str = 'STK',
        underlying_exchange: str = 'SMART',
        underlying_currency: str = 'USD',
        max_strikes: int = 40,
        wait_seconds: float = 3.0,
        trading_class: Optional[str] = None,
    ) -> Dict:
        """Get option chain for an underlying with per-strike Greeks/IV/OI/Volume.

        - `expiry`: 'nearest' (default — earliest expiration), or 'YYYYMMDD' / 'YYYYMM'
          (first match if a prefix).
        - `min_strike` / `max_strike`: optional window for strike filter.
        - `right`: 'C' (calls only), 'P' (puts only), or 'BOTH'.
        - `max_strikes`: cap on strikes to stream — respects the 100-line cap.
          With both rights this means up to 2 × max_strikes contracts streaming.
        - `trading_class`: explicit override (e.g. 'SPXW' for SPX weeklies).
          When omitted, picks the chain where tradingClass matches the underlying
          symbol with the largest strike list (avoids secondary classes like '2SPY'
          that only carry a handful of legacy strikes).

        Without OPRA Top-of-Book subscription, IBKR returns 15-min-delayed quotes
        via the `reqMarketDataType(3)` setting in .env. Greeks/IV from modelGreeks
        are computed regardless of subscription tier.
        """
        try:
            if not await self._ensure_connected():
                raise IBKRConnectionError("Not connected to IBKR")

            # Resolve underlying conId
            underlying_contract = _make_contract(
                symbol=underlying, sec_type=underlying_sec_type,
                exchange=underlying_exchange, currency=underlying_currency,
            )
            qualified = await self.ib.reqContractDetailsAsync(underlying_contract)
            if not qualified:
                return {"error": f"Underlying contract not found: {underlying}"}
            underlying_contract = qualified[0].contract

            # Get the chain (expirations + strikes per exchange × tradingClass)
            chains = await self.ib.reqSecDefOptParamsAsync(
                underlyingSymbol=underlying_contract.symbol,
                futFopExchange='',
                underlyingSecType=underlying_contract.secType,
                underlyingConId=underlying_contract.conId,
            )
            if not chains:
                return {"error": f"No options chain available for {underlying}"}

            # Chain picker: SPY/SPX/etc. expose multiple chains per exchange/tradingClass.
            # The primary chain has the most strikes; secondary classes (e.g. '2SPY')
            # have <10 stale legacy strikes. Default: prefer tradingClass==symbol AND
            # SMART exchange, then largest-strike-list SMART, then largest overall.
            smart_chains = [c for c in chains if c.exchange == 'SMART']
            pool = smart_chains if smart_chains else chains

            chain = None
            if trading_class:
                tc_matches = [c for c in pool if c.tradingClass == trading_class]
                if not tc_matches:
                    return {
                        "error": f"No chain with tradingClass='{trading_class}'",
                        "available_classes": sorted({c.tradingClass for c in pool}),
                    }
                chain = max(tc_matches, key=lambda c: len(c.strikes))
            else:
                symbol_matches = [c for c in pool if c.tradingClass == underlying_contract.symbol]
                if symbol_matches:
                    chain = max(symbol_matches, key=lambda c: len(c.strikes))
                else:
                    chain = max(pool, key=lambda c: len(c.strikes))

            # Pick expiry
            expirations = sorted(chain.expirations)
            if expiry == 'nearest':
                target_expiry = expirations[0] if expirations else ''
            else:
                matches = [e for e in expirations if e.startswith(expiry)]
                if not matches:
                    return {
                        "error": f"No expiry matches '{expiry}'",
                        "available_expirations": expirations[:20],
                    }
                target_expiry = matches[0]
            if not target_expiry:
                return {"error": f"No expirations available for {underlying}"}

            # Filter strikes
            strikes = sorted(float(s) for s in chain.strikes)
            if min_strike is not None:
                strikes = [s for s in strikes if s >= min_strike]
            if max_strike is not None:
                strikes = [s for s in strikes if s <= max_strike]
            if len(strikes) > max_strikes:
                # Truncate around the middle of the surviving window (ATM heuristic)
                mid = len(strikes) // 2
                half = max_strikes // 2
                strikes = strikes[max(0, mid - half):mid + half]

            rights = ['C', 'P'] if right.upper() == 'BOTH' else [right.upper()]

            # Build and qualify option contracts
            raw_contracts: List[Contract] = []
            for k in strikes:
                for r in rights:
                    opt = Option(
                        underlying_contract.symbol,
                        lastTradeDateOrContractMonth=target_expiry,
                        strike=k,
                        right=r,
                        exchange=chain.exchange,
                        currency=underlying_contract.currency,
                    )
                    opt.tradingClass = chain.tradingClass
                    opt.multiplier = chain.multiplier
                    raw_contracts.append(opt)

            await self.ib.qualifyContractsAsync(*raw_contracts)
            contracts = [c for c in raw_contracts if c.conId]
            if not contracts:
                return {
                    "error": "No option contracts qualified",
                    "tried_strikes": strikes,
                    "tried_expiry": target_expiry,
                }

            # Stream market data for each contract with OI/Vol generic ticks.
            # Greeks/IV come automatically via tick types 10-13.
            tickers = [
                self.ib.reqMktData(c, '100,101', False, False)
                for c in contracts
            ]
            try:
                await asyncio.sleep(wait_seconds)
                entries = []
                for tk in tickers:
                    c = tk.contract
                    oi = (
                        safe_float(getattr(tk, 'callOpenInterest', 0))
                        if c.right == 'C'
                        else safe_float(getattr(tk, 'putOpenInterest', 0))
                    )
                    entry = {
                        "strike": safe_float(c.strike),
                        "right": c.right,
                        "expiry": c.lastTradeDateOrContractMonth,
                        "contract_id": c.conId,
                        "bid": safe_float(tk.bid),
                        "ask": safe_float(tk.ask),
                        "last": safe_float(tk.last),
                        "volume": safe_float(tk.volume),
                        "open_interest": oi,
                    }
                    if tk.modelGreeks is not None:
                        mg = tk.modelGreeks
                        entry.update({
                            "iv": safe_float(mg.impliedVol),
                            "delta": safe_float(mg.delta),
                            "gamma": safe_float(mg.gamma),
                            "vega": safe_float(mg.vega),
                            "theta": safe_float(mg.theta),
                            "underlying_price": safe_float(mg.undPrice),
                            "option_price": safe_float(mg.optPrice),
                        })
                    entries.append(entry)
                return {
                    "underlying": underlying_contract.symbol,
                    "underlying_conId": underlying_contract.conId,
                    "expiry": target_expiry,
                    "exchange": chain.exchange,
                    "trading_class": chain.tradingClass,
                    "multiplier": chain.multiplier,
                    "strike_count": len(strikes),
                    "contract_count": len(contracts),
                    "expirations_available": expirations,
                    "strikes_in_window": strikes,
                    "contracts": entries,
                }
            finally:
                for c in contracts:
                    try:
                        self.ib.cancelMktData(c)
                    except Exception:
                        pass

        except Exception as e:
            self.logger.error(f"Option chain request failed for {underlying}: {e}")
            return {"error": str(e)}

    @rate_limit(calls_per_second=0.5)
    async def get_fundamentals(
        self,
        symbol: str,
        report_type: str = 'ReportSnapshot',
        exchange: str = 'SMART',
        currency: str = 'USD',
    ) -> Dict:
        """Get Reuters/Refinitiv fundamental data via reqFundamentalData.

        report_type:
          - ReportSnapshot — P/E, ratios, market cap, beta
          - ReportsFinSummary — 4-quarter EPS/revenue history
          - ReportRatios — financial ratios
          - ReportsFinStatements — balance sheet / income / cash flow
          - RESC — analyst EPS estimates
          - CalendarReport — earnings dates ± 3 weeks

        Requires Reuters Worldwide Fundamentals subscription (USD 11/mo non-pro,
        waived ≥ USD 30/mo commissions). Returns raw XML — caller parses.
        """
        try:
            if not await self._ensure_connected():
                raise IBKRConnectionError("Not connected to IBKR")
            contract = _make_contract(
                symbol=symbol, sec_type='STK', exchange=exchange, currency=currency,
            )
            qualified = await self.ib.reqContractDetailsAsync(contract)
            if not qualified:
                return {"error": f"Contract not found: {symbol}"}
            contract = qualified[0].contract

            xml = await self.ib.reqFundamentalDataAsync(contract, report_type)
            if not xml:
                return {
                    "error": f"No fundamental data returned for {symbol} / {report_type}",
                    "hint": "Reuters Worldwide Fundamentals subscription required (USD 11/mo non-pro).",
                }
            return {
                "symbol": contract.symbol,
                "contract_id": contract.conId,
                "report_type": report_type,
                "xml": xml,
                "xml_length": len(xml),
            }
        except Exception as e:
            self.logger.error(f"Fundamentals request failed for {symbol}: {e}")
            return {"error": str(e)}

    @rate_limit(calls_per_second=0.5)
    async def get_news(
        self,
        symbol: str,
        max_results: int = 20,
        providers: str = 'BRFG+BRFUPDN+DJNL',
        start_datetime: str = '',
        end_datetime: str = '',
        fetch_bodies: bool = False,
        exchange: str = 'SMART',
        currency: str = 'USD',
    ) -> Dict:
        """Get news headlines tied to a stock ticker via reqHistoricalNews.

        Default providers (BRFG / BRFUPDN / DJNL) are free with API access.
        Reuters and the full Dow Jones newswire are NOT API-exposed (TWS panel
        only). Optional API add-ons: Benzinga Pro USD 99/mo, Fly on the Wall
        USD 30/mo.

        With `fetch_bodies=True` calls reqNewsArticle per headline — slow for
        large `max_results`.
        """
        try:
            if not await self._ensure_connected():
                raise IBKRConnectionError("Not connected to IBKR")
            contract = _make_contract(
                symbol=symbol, sec_type='STK', exchange=exchange, currency=currency,
            )
            qualified = await self.ib.reqContractDetailsAsync(contract)
            if not qualified:
                return {"error": f"Contract not found: {symbol}"}
            contract = qualified[0].contract

            news = await self.ib.reqHistoricalNewsAsync(
                conId=contract.conId,
                providerCodes=providers,
                startDateTime=start_datetime,
                endDateTime=end_datetime,
                totalResults=max_results,
            )

            headlines = []
            for h in (news or []):
                entry = {
                    "time": str(h.time),
                    "provider": h.providerCode,
                    "article_id": h.articleId,
                    "headline": h.headline,
                }
                if fetch_bodies:
                    try:
                        article = await self.ib.reqNewsArticleAsync(h.providerCode, h.articleId)
                        entry["article_text"] = getattr(article, 'articleText', None) if article else None
                        entry["article_type"] = getattr(article, 'articleType', None) if article else None
                    except Exception as ae:
                        entry["article_fetch_error"] = str(ae)
                headlines.append(entry)

            return {
                "symbol": contract.symbol,
                "contract_id": contract.conId,
                "providers_requested": providers,
                "max_results": max_results,
                "headline_count": len(headlines),
                "headlines": headlines,
            }
        except Exception as e:
            self.logger.error(f"News request failed for {symbol}: {e}")
            return {"error": str(e)}

    async def place_combo_order(
        self,
        underlying: str,
        legs: List[Dict],
        order_action: str = 'BUY',
        quantity: int = 1,
        limit_price: float = 0.0,
        order_type: str = 'LMT',
        tif: str = 'DAY',
        account: Optional[str] = None,
        currency: str = 'USD',
        exchange: str = 'SMART',
        dry_run: bool = True,
    ) -> Dict:
        """Place a multi-leg combo order (BAG contract). Designed for credit spreads.

        SAFETY GATES (all must pass):
          1. settings.enable_live_trading must be True (set via ENABLE_LIVE_TRADING=true in .env).
          2. quantity must be ≤ settings.max_order_size (MAX_ORDER_SIZE in .env).
          3. dry_run=True (default) validates without placing — explicitly pass False to submit.
          4. IB Gateway's "Read-Only API" must be OFF. Cannot be detected from the API side; if
             still ON, the order will be rejected by IBKR with Error 201.

        `legs`: list of {conId: int, ratio: int (default 1), action: 'BUY'|'SELL', exchange?: str}.
        Example bull-put credit spread on SPY (sell 450P, buy 445P):
          legs = [
              {"conId": 12345, "ratio": 1, "action": "SELL"},   # short put (higher strike)
              {"conId": 67890, "ratio": 1, "action": "BUY"},    # long put  (lower strike)
          ]
          order_action="SELL"  # selling the bag = net credit
          limit_price=1.25      # minimum net credit (positive number)
        """
        try:
            # Gate 1: live-trading flag
            if not settings.enable_live_trading:
                return {
                    "blocked": True,
                    "reason": "ENABLE_LIVE_TRADING=false in .env",
                    "hint": "Set ENABLE_LIVE_TRADING=true in ~/Agents/ibkr-mcp-server/.env and restart the MCP. After that, dry_run=true is still the default — you must also explicitly pass dry_run=false to submit.",
                }
            # Gate 2: quantity cap
            if quantity > settings.max_order_size:
                return {
                    "blocked": True,
                    "reason": f"quantity {quantity} exceeds MAX_ORDER_SIZE={settings.max_order_size}",
                }
            # Gate 3: validate legs
            if not legs or len(legs) < 2:
                return {"error": "combo order requires at least 2 legs"}
            normalized_legs = []
            for leg in legs:
                if 'conId' not in leg or 'action' not in leg:
                    return {"error": "each leg needs conId + action (BUY/SELL)"}
                act = str(leg['action']).upper()
                if act not in ('BUY', 'SELL'):
                    return {"error": f"invalid leg action: {leg['action']}"}
                normalized_legs.append({
                    "conId": int(leg['conId']),
                    "ratio": int(leg.get('ratio', 1)),
                    "action": act,
                    "exchange": leg.get('exchange', exchange),
                })

            if not await self._ensure_connected():
                raise IBKRConnectionError("Not connected to IBKR")

            # Build BAG contract
            bag = Contract()
            bag.symbol = underlying
            bag.secType = 'BAG'
            bag.currency = currency
            bag.exchange = exchange
            bag.comboLegs = [
                ComboLeg(
                    conId=leg['conId'],
                    ratio=leg['ratio'],
                    action=leg['action'],
                    exchange=leg['exchange'],
                )
                for leg in normalized_legs
            ]

            # Build Order
            order = Order()
            order.action = order_action.upper()
            order.orderType = order_type
            order.totalQuantity = quantity
            if order_type == 'LMT':
                order.lmtPrice = limit_price
            order.tif = tif
            if account or self.current_account:
                order.account = account or self.current_account

            # Dry-run: validate + describe, don't place
            if dry_run:
                return {
                    "dry_run": True,
                    "would_submit": True,
                    "bag_contract": {
                        "underlying": bag.symbol,
                        "secType": bag.secType,
                        "exchange": bag.exchange,
                        "currency": bag.currency,
                        "combo_legs": normalized_legs,
                    },
                    "order": {
                        "action": order.action,
                        "orderType": order.orderType,
                        "totalQuantity": order.totalQuantity,
                        "lmtPrice": getattr(order, 'lmtPrice', None),
                        "tif": order.tif,
                        "account": order.account,
                    },
                    "hint": "Re-call with dry_run=false to submit. NOTE: IB Gateway's Read-Only-API must be OFF — currently expected to be ON per project setup.",
                }

            # Actually place
            self.logger.warning(
                f"PLACING COMBO ORDER: {bag.symbol} {order.action} qty={quantity} "
                f"limit={limit_price} legs={len(normalized_legs)} account={order.account}"
            )
            trade = self.ib.placeOrder(bag, order)
            await asyncio.sleep(2.5)  # let initial events propagate

            return {
                "dry_run": False,
                "order_id": trade.order.orderId,
                "perm_id": getattr(trade.order, 'permId', None),
                "status": trade.orderStatus.status,
                "filled": safe_float(trade.orderStatus.filled),
                "remaining": safe_float(trade.orderStatus.remaining),
                "avg_fill_price": safe_float(trade.orderStatus.avgFillPrice),
                "why_held": getattr(trade.orderStatus, 'whyHeld', None),
                "last_log": [str(le) for le in trade.log[-5:]] if trade.log else [],
                "error_201_hint": "If the order was immediately rejected/cancelled with Error 201, the IB Gateway's Read-Only-API flag is still ON. Disable it in Gateway → Configuration → API → Settings (then restart the Gateway).",
            }

        except Exception as e:
            self.logger.error(f"Combo order placement failed: {e}")
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
    
    def _serialize_contract_brief(self, c) -> Dict:
        """Compact contract identity for order/fill read-backs."""
        d = {
            "symbol": getattr(c, 'symbol', ''),
            "secType": getattr(c, 'secType', ''),
            "localSymbol": getattr(c, 'localSymbol', ''),
            "exchange": getattr(c, 'exchange', ''),
            "currency": getattr(c, 'currency', ''),
        }
        if getattr(c, 'lastTradeDateOrContractMonth', ''):
            d["expiry"] = c.lastTradeDateOrContractMonth
        if safe_float(getattr(c, 'strike', 0)):
            d["strike"] = safe_float(c.strike)
        if getattr(c, 'right', ''):
            d["right"] = c.right
        legs = getattr(c, 'comboLegs', None) or []
        if legs:
            d["comboLegs"] = [
                {"conId": safe_int(getattr(leg, 'conId', 0)),
                 "ratio": safe_int(getattr(leg, 'ratio', 0)),
                 "action": getattr(leg, 'action', '')}
                for leg in legs
            ]
        return d

    def _serialize_open_order(self, trade) -> Dict:
        """Convert an ib_async Trade (open order) to a serializable dict."""
        o = trade.order
        st = getattr(trade, 'orderStatus', None)
        return {
            "orderId": safe_int(getattr(o, 'orderId', 0)),
            "permId": safe_int(getattr(o, 'permId', 0)),
            "clientId": safe_int(getattr(o, 'clientId', 0)),
            "account": getattr(o, 'account', ''),
            "orderRef": getattr(o, 'orderRef', ''),
            "action": getattr(o, 'action', ''),
            "totalQuantity": safe_float(getattr(o, 'totalQuantity', 0)),
            "orderType": getattr(o, 'orderType', ''),
            "lmtPrice": safe_float(getattr(o, 'lmtPrice', 0)),
            "auxPrice": safe_float(getattr(o, 'auxPrice', 0)),
            "tif": getattr(o, 'tif', ''),
            "status": getattr(st, 'status', '') if st else '',
            "filled": safe_float(getattr(st, 'filled', 0)) if st else 0.0,
            "remaining": safe_float(getattr(st, 'remaining', 0)) if st else 0.0,
            "avgFillPrice": safe_float(getattr(st, 'avgFillPrice', 0)) if st else 0.0,
            "contract": self._serialize_contract_brief(trade.contract),
        }

    def _serialize_fill(self, fill) -> Dict:
        """Convert an ib_async Fill to a serializable dict (incl. commission)."""
        ex = fill.execution
        cr = getattr(fill, 'commissionReport', None)

        def _pnl_or_none(value):
            # IBKR reports DBL_MAX when a per-fill value is unset
            v = safe_float(value)
            return None if abs(v) > 1e300 else v

        return {
            "execId": getattr(ex, 'execId', ''),
            "time": str(getattr(ex, 'time', '')),
            "account": getattr(ex, 'acctNumber', ''),
            "side": getattr(ex, 'side', ''),
            "shares": safe_float(getattr(ex, 'shares', 0)),
            "price": safe_float(getattr(ex, 'price', 0)),
            "permId": safe_int(getattr(ex, 'permId', 0)),
            "clientId": safe_int(getattr(ex, 'clientId', 0)),
            "orderId": safe_int(getattr(ex, 'orderId', 0)),
            "orderRef": getattr(ex, 'orderRef', ''),
            "cumQty": safe_float(getattr(ex, 'cumQty', 0)),
            "avgPrice": safe_float(getattr(ex, 'avgPrice', 0)),
            "commission": _pnl_or_none(getattr(cr, 'commission', None)) if cr else None,
            "realizedPNL": _pnl_or_none(getattr(cr, 'realizedPNL', None)) if cr else None,
            "contract": self._serialize_contract_brief(fill.contract),
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
