"""Tests for IBKR client functionality."""

import pytest
from unittest.mock import AsyncMock, patch

from ibkr_mcp_server.client import IBKRClient, _make_contract
from ibkr_mcp_server.utils import ConnectionError as IBKRConnectionError


class TestIBKRClient:
    """Test IBKR client functionality."""
    
    def test_account_switching(self, ibkr_client_mock):
        """Test account switching functionality."""
        # Test valid account switch
        assert ibkr_client_mock.switch_account('DU7654321') is True
        assert ibkr_client_mock.current_account == 'DU7654321'
        
        # Test invalid account switch
        assert ibkr_client_mock.switch_account('INVALID') is False
        assert ibkr_client_mock.current_account == 'DU7654321'  # Should remain unchanged
    
    def test_get_accounts(self, ibkr_client_mock):
        """Test getting account information."""
        accounts = ibkr_client_mock.get_accounts()
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

        with pytest.raises(IBKRConnectionError):
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
