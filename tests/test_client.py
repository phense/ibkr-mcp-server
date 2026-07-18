"""Tests for IBKR client functionality."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from ibkr_mcp_server.client import IBKRClient, _make_contract
from ibkr_mcp_server.utils import ConnectionError as IBKRConnectionError


class TestIBKRClient:
    """Test IBKR client functionality."""
    
    @pytest.mark.asyncio
    async def test_account_switching(self, ibkr_client_mock):
        """Test account switching functionality (async dict API)."""
        # Test valid account switch
        result = await ibkr_client_mock.switch_account('DU7654321')
        assert result['success'] is True
        assert ibkr_client_mock.current_account == 'DU7654321'

        # Test invalid account switch
        result = await ibkr_client_mock.switch_account('INVALID')
        assert result['success'] is False
        assert ibkr_client_mock.current_account == 'DU7654321'  # Should remain unchanged

    @pytest.mark.asyncio
    async def test_get_accounts(self, ibkr_client_mock):
        """Test getting account information (async dict API)."""
        accounts = await ibkr_client_mock.get_accounts()
        assert accounts['current_account'] == 'DU1234567'
        assert 'DU1234567' in accounts['available_accounts']
        assert 'DU7654321' in accounts['available_accounts']
    
    def test_is_connected(self, ibkr_client_mock):
        """Test connection status check."""
        # Mock the ib.isConnected method properly
        ibkr_client_mock.ib.isConnected.return_value = True
        assert ibkr_client_mock.is_connected() is True
        
        # Test disconnected state
        ibkr_client_mock._connected = False
        assert ibkr_client_mock.is_connected() is False
    
    @pytest.mark.asyncio
    async def test_get_portfolio_not_connected(self):
        """Test portfolio request when not connected."""
        client = IBKRClient()
        client._connected = False
        # Never let a unit test attempt a REAL gateway session (with .env now
        # pointing at the live port, an unstubbed connect() hangs in handshake
        # retries against the live gateway).
        client.connect = AsyncMock(return_value=False)

        with pytest.raises((IBKRConnectionError, RuntimeError)):
            await client.get_portfolio()


class TestMakeContractLeverageProducts:
    """IOPT/WAR (German leverage products) resolve by ISIN on SWB/EUR."""

    def test_iopt_with_isin_defaults_swb_eur(self):
        c = _make_contract(symbol='', sec_type='IOPT', isin='DE000VU2G8P1')
        assert c.secType == 'IOPT'
        assert c.exchange == 'SWB'
        assert c.currency == 'EUR'
        assert c.secIdType == 'ISIN'
        assert c.secId == 'DE000VU2G8P1'

    def test_war_with_isin(self):
        c = _make_contract(symbol='', sec_type='WAR', isin='DE000GW0YPP1')
        assert c.secType == 'WAR'
        assert c.exchange == 'SWB'
        assert c.currency == 'EUR'
        assert c.secIdType == 'ISIN'
        assert c.secId == 'DE000GW0YPP1'

    def test_explicit_exchange_and_currency_win(self):
        c = _make_contract(symbol='', sec_type='IOPT', isin='DE000SD3VE72',
                           exchange='IBIS', currency='USD')
        assert c.exchange == 'IBIS'
        assert c.currency == 'USD'

    def test_no_isin_leaves_secid_unset(self):
        c = _make_contract(symbol='GW0YPP', sec_type='WAR')
        assert c.secType == 'WAR'
        assert c.symbol == 'GW0YPP'
        assert not getattr(c, 'secIdType', '')
        assert not getattr(c, 'secId', '')


class TestReadBackSurface:
    """get_open_orders / get_executions — the broker read-back verification surface.

    These must see orders/fills from ALL API client ids (the trading engine's
    family 20-44), not just this client's own — that is the whole point.
    """

    @staticmethod
    def _fake_trade(client_id=23, account='U1234567', status='Submitted'):
        from types import SimpleNamespace
        return SimpleNamespace(
            contract=SimpleNamespace(
                symbol='SPY', secType='BAG', localSymbol='', exchange='SMART',
                currency='USD', lastTradeDateOrContractMonth='', strike=0.0, right='',
                comboLegs=[SimpleNamespace(conId=101, ratio=1, action='BUY'),
                           SimpleNamespace(conId=102, ratio=1, action='SELL')],
            ),
            order=SimpleNamespace(
                orderId=7, permId=900123, clientId=client_id, account=account,
                orderRef='zephyr-20260718-r1', action='BUY', totalQuantity=2,
                orderType='LMT', lmtPrice=1.25, auxPrice=0.0, tif='DAY',
            ),
            orderStatus=SimpleNamespace(status=status, filled=0.0, remaining=2.0,
                                        avgFillPrice=0.0),
        )

    @staticmethod
    def _fake_fill(client_id=20, account='U1234567', commission=1.05,
                   realized=1.7976931348623157e+308):
        from types import SimpleNamespace
        return SimpleNamespace(
            contract=SimpleNamespace(
                symbol='SPY', secType='OPT', localSymbol='SPY 260718P00625000',
                exchange='SMART', currency='USD',
                lastTradeDateOrContractMonth='20260718', strike=625.0, right='P',
                comboLegs=[],
            ),
            execution=SimpleNamespace(
                execId='0000e1a7.687a1c2b.01.01', time='2026-07-17 15:30:01',
                acctNumber=account, side='SLD', shares=1, price=0.42,
                permId=900124, clientId=client_id, orderId=9,
                orderRef='tc-20260717-r3', cumQty=1, avgPrice=0.42,
            ),
            commissionReport=SimpleNamespace(commission=commission,
                                             realizedPNL=realized),
        )

    @pytest.mark.asyncio
    async def test_get_open_orders_sees_engine_clients(self, ibkr_client_mock):
        ibkr_client_mock.ib.reqAllOpenOrdersAsync = AsyncMock(
            return_value=[self._fake_trade(client_id=23)])
        out = await ibkr_client_mock.get_open_orders()
        assert len(out) == 1
        assert out[0]['clientId'] == 23          # engine router order, not our own
        assert out[0]['status'] == 'Submitted'
        assert out[0]['orderRef'] == 'zephyr-20260718-r1'
        assert out[0]['lmtPrice'] == 1.25
        assert len(out[0]['contract']['comboLegs']) == 2

    @pytest.mark.asyncio
    async def test_get_open_orders_account_filter(self, ibkr_client_mock):
        ibkr_client_mock.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[
            self._fake_trade(account='U1234567'),
            self._fake_trade(account='DU7654321'),
        ])
        out = await ibkr_client_mock.get_open_orders(account='U1234567')
        assert len(out) == 1
        assert out[0]['account'] == 'U1234567'

    @pytest.mark.asyncio
    async def test_get_open_orders_not_connected(self):
        client = IBKRClient()
        client._connected = False
        client.connect = AsyncMock(return_value=False)  # no real gateway session from unit tests
        with pytest.raises((IBKRConnectionError, RuntimeError)):
            await client.get_open_orders()

    @pytest.mark.asyncio
    async def test_get_executions_includes_commission(self, ibkr_client_mock):
        ibkr_client_mock.ib.reqExecutionsAsync = AsyncMock(
            return_value=[self._fake_fill()])
        out = await ibkr_client_mock.get_executions()
        assert len(out) == 1
        assert out[0]['clientId'] == 20
        assert out[0]['commission'] == 1.05
        assert out[0]['price'] == 0.42
        assert out[0]['contract']['localSymbol'] == 'SPY 260718P00625000'

    @pytest.mark.asyncio
    async def test_get_executions_filters_unset_realized_pnl_sentinel(self, ibkr_client_mock):
        # IBKR reports realizedPNL as DBL_MAX when unset — must map to None, not a number
        ibkr_client_mock.ib.reqExecutionsAsync = AsyncMock(
            return_value=[self._fake_fill(realized=1.7976931348623157e+308)])
        out = await ibkr_client_mock.get_executions()
        assert out[0]['realizedPNL'] is None

    @pytest.mark.asyncio
    async def test_get_executions_passes_account_and_symbol_filter(self, ibkr_client_mock):
        captured = {}

        async def fake_req(filt):
            captured['filt'] = filt
            return []

        ibkr_client_mock.ib.reqExecutionsAsync = fake_req
        await ibkr_client_mock.get_executions(account='U1234567', symbol='SPY')
        assert captured['filt'].acctCode == 'U1234567'
        assert captured['filt'].symbol == 'SPY'


class TestClientIdFallback:
    """Duplicate clientId (Error 326 → connect TimeoutError) walks the 1-9 band."""

    @pytest.mark.asyncio
    async def test_falls_back_to_next_client_id_when_base_is_taken(self):
        client = IBKRClient()
        client.client_id = 1
        client.ib = MagicMock()
        attempts = []

        async def fake_connect(host, port, clientId, timeout):
            attempts.append(clientId)
            if clientId == 1:
                raise TimeoutError()  # gateway holds id 1 (Error 326 path)

        client.ib.connectAsync = fake_connect
        await client._connect_with_client_id_fallback()
        assert attempts == [1, 2]
        assert client.client_id == 2

    @pytest.mark.asyncio
    async def test_gateway_down_fails_fast_without_walking_band(self):
        client = IBKRClient()
        client.client_id = 1
        client.ib = MagicMock()
        attempts = []

        async def fake_connect(host, port, clientId, timeout):
            attempts.append(clientId)
            raise ConnectionRefusedError()  # nothing listening — other ids won't help

        client.ib.connectAsync = fake_connect
        with pytest.raises(ConnectionRefusedError):
            await client._connect_with_client_id_fallback()
        assert attempts == [1]

    @pytest.mark.asyncio
    async def test_exhausted_band_raises_last_timeout(self):
        client = IBKRClient()
        client.client_id = 1
        client.ib = MagicMock()

        async def fake_connect(host, port, clientId, timeout):
            raise TimeoutError()

        client.ib.connectAsync = fake_connect
        with pytest.raises(TimeoutError):
            await client._connect_with_client_id_fallback()
