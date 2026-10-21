from unittest.mock import MagicMock, PropertyMock

import pytest

from freqtrade.enums import MarginMode, TradingMode
from freqtrade.exceptions import ExchangeError, OperationalException
from freqtrade.exchange.mexc import Mexc
from tests.conftest import EXMS, get_patched_exchange
from tests.exchange.test_exchange import ccxt_exceptionhandlers


def _futures_conf(default_conf):
    default_conf["trading_mode"] = TradingMode.FUTURES
    default_conf["margin_mode"] = MarginMode.ISOLATED
    return default_conf


def test_mexc_ws_enabled_on_futures(default_conf, mocker):
    spot = get_patched_exchange(mocker, default_conf, exchange="mexc")
    assert spot._ft_has["ws_enabled"] == {"ohlcv": False, "orderbook": False}
    exchange = get_patched_exchange(mocker, _futures_conf(default_conf), exchange="mexc")
    assert exchange._ft_has["ws_enabled"] == {"ohlcv": True, "orderbook": True}
    assert exchange._ft_has["ohlcv_candle_limit"] == 2000
    assert exchange._ft_has["ohlcv_volume_in_contracts"] is True
    assert exchange._ft_has["stoploss_on_exchange"] is True


def test_patch_swap_ohlcv_volume_uses_contract_quantity():
    class _FakeCcxt:
        def parse_ws_ohlcv(self, ohlcv, market=None):
            return [1, 2, 3, 4, 5, None]

        def safe_number(self, data, key):
            return float(data[key])

    ccxt_obj = _FakeCcxt()
    Mexc._patch_swap_ohlcv_volume(Mexc, ccxt_obj)
    patched = ccxt_obj.parse_ws_ohlcv({"q": "3"}, {"swap": True})
    assert patched[5] == 3.0

    spot = ccxt_obj.parse_ws_ohlcv({"q": "9", "v": "1"}, {"swap": False, "spot": True})
    assert spot[5] is None


def test_additional_exchange_init_mexc(default_conf, mocker, caplog):
    default_conf["dry_run"] = False
    _futures_conf(default_conf)
    api_mock = MagicMock()
    api_mock.set_position_mode = MagicMock(return_value={"code": 0})
    api_mock.fetch_positions = MagicMock(return_value=[])

    exchange = get_patched_exchange(mocker, default_conf, exchange="mexc", api_mock=api_mock)
    assert api_mock.set_position_mode.call_count == 1
    api_mock.set_position_mode.assert_called_with(False)

    api_mock.set_position_mode.reset_mock()
    api_mock.fetch_positions = MagicMock(return_value=[{"symbol": "BTC/USDT:USDT", "contracts": 1}])
    exchange = get_patched_exchange(mocker, default_conf, exchange="mexc", api_mock=api_mock)
    assert api_mock.set_position_mode.call_count == 0
    assert exchange._futures_account_is_flat() is False

    api_mock.fetch_positions = MagicMock(return_value=[])
    ccxt_exceptionhandlers(
        mocker,
        default_conf,
        api_mock,
        "mexc",
        "additional_exchange_init",
        "set_position_mode",
    )


def test__lev_prep_mexc(default_conf, mocker):
    api_mock = MagicMock()
    type(api_mock).has = PropertyMock(return_value={"setMarginMode": True, "setLeverage": True})
    exchange = get_patched_exchange(mocker, default_conf, api_mock, exchange="mexc")
    exchange._lev_prep("DOGE/USDT:USDT", 10, "buy")
    assert api_mock.set_margin_mode.call_count == 0
    assert api_mock.set_leverage.call_count == 0

    default_conf["dry_run"] = False
    _futures_conf(default_conf)
    api_mock.fetch_positions = MagicMock(return_value=[])
    exchange = get_patched_exchange(mocker, default_conf, api_mock, exchange="mexc")
    api_mock.set_margin_mode.reset_mock()
    api_mock.set_leverage.reset_mock()

    exchange._lev_prep("DOGE/USDT:USDT", 10.9, "buy")
    api_mock.set_margin_mode.assert_called_once_with(
        "isolated",
        "DOGE/USDT:USDT",
        {"leverage": 10, "direction": "long"},
    )
    assert api_mock.set_leverage.call_count == 0

    api_mock.set_margin_mode.reset_mock()
    exchange._lev_prep("DOGE/USDT:USDT", 10, "sell")
    api_mock.set_margin_mode.assert_called_once_with(
        "isolated",
        "DOGE/USDT:USDT",
        {"leverage": 10, "direction": "short"},
    )


def test__get_params_mexc_strips_time_in_force(default_conf, mocker):
    _futures_conf(default_conf)
    exchange = get_patched_exchange(mocker, default_conf, exchange="mexc")
    params = exchange._get_params(
        side="buy",
        ordertype="limit",
        leverage=10,
        reduceOnly=False,
        time_in_force="PO",
    )
    assert params["postOnly"] is True
    assert "timeInForce" not in params
    assert params["marginMode"] == "isolated"
    assert params["leverage"] == 10
    assert params["hedged"] is False

    ioc = exchange._get_params(
        side="sell",
        ordertype="limit",
        leverage=10,
        reduceOnly=True,
        time_in_force="IOC",
    )
    assert "timeInForce" not in ioc
    assert ioc["reduceOnly"] is True


def test__get_stop_params_mexc(default_conf, mocker):
    _futures_conf(default_conf)
    exchange = get_patched_exchange(mocker, default_conf, exchange="mexc")
    exchange._mexc_order_leverage = 10

    long_stop = exchange._get_stop_params("sell", "market", 0.05)
    assert long_stop["stopPrice"] == 0.05
    assert long_stop["triggerType"] == 2
    assert long_stop["orderType"] == 5
    assert long_stop["executeCycle"] == 2
    assert long_stop["leverage"] == 10
    assert long_stop["hedged"] is False
    assert long_stop["marginMode"] == "isolated"
    assert "trend" not in long_stop

    short_stop = exchange._get_stop_params("buy", "limit", 0.2)
    assert short_stop["triggerType"] == 1
    assert short_stop["orderType"] == 1


def test_fetch_and_cancel_stoploss_use_trigger(default_conf, mocker):
    default_conf["dry_run"] = False
    _futures_conf(default_conf)
    api_mock = MagicMock()
    type(api_mock).has = PropertyMock(return_value={"fetchOrder": True, "cancelOrder": True})
    api_mock.fetch_open_orders = MagicMock(
        return_value=[{"id": "1", "symbol": "DOGE/USDT:USDT", "status": "open"}]
    )
    api_mock.cancel_order = MagicMock(return_value={"id": "1", "status": "canceled"})
    mocker.patch(f"{EXMS}._order_contracts_to_amount", side_effect=lambda order: order)
    exchange = get_patched_exchange(mocker, default_conf, api_mock, exchange="mexc")

    exchange.fetch_stoploss_order("1", "DOGE/USDT:USDT")
    assert api_mock.fetch_open_orders.call_args.kwargs["params"]["trigger"] is True

    exchange.cancel_stoploss_order("1", "DOGE/USDT:USDT")
    assert api_mock.cancel_order.call_args.kwargs["params"]["trigger"] is True


def test_maintenance_ratio_uses_first_tier(default_conf, mocker):
    exchange = get_patched_exchange(mocker, _futures_conf(default_conf), exchange="mexc")
    exchange._leverage_tiers = {
        "DOGE/USDT:USDT": [
            {
                "minNotional": 0,
                "maxNotional": 10000,
                "maintenanceMarginRate": 0.0023,
                "maxLeverage": 300,
                "maintAmt": None,
            },
            {
                "minNotional": 10000,
                "maxNotional": 20000,
                "maintenanceMarginRate": 0.05,
                "maxLeverage": 20,
                "maintAmt": None,
            },
        ]
    }
    assert exchange.get_maintenance_ratio_and_amt("DOGE/USDT:USDT", 50000) == (0.0023, None)
    with pytest.raises(ExchangeError):
        exchange.get_maintenance_ratio_and_amt("BTC/USDT:USDT", 1)


def test_dry_run_liquidation_price_mexc(default_conf, mocker):
    exchange = get_patched_exchange(mocker, _futures_conf(default_conf), exchange="mexc")
    exchange.markets["DOGE/USDT:USDT"] = {"inverse": False, "swap": True}
    exchange._leverage_tiers = {
        "DOGE/USDT:USDT": [
            {
                "minNotional": 0,
                "maxNotional": 10000,
                "maintenanceMarginRate": 0.0023,
                "maxLeverage": 300,
                "maintAmt": None,
            }
        ]
    }
    long_price = exchange.dry_run_liquidation_price(
        "DOGE/USDT:USDT", 0.08, False, 100, 0.8, 10, 0.8, []
    )
    short_price = exchange.dry_run_liquidation_price(
        "DOGE/USDT:USDT", 0.08, True, 100, 0.8, 10, 0.8, []
    )
    assert long_price == pytest.approx(7.2 / 99.77)
    assert short_price == pytest.approx(8.8 / 100.23)
    assert long_price < 0.08 < short_price

    exchange.markets["BTC/USD:BTC"] = {"inverse": True, "swap": True}
    with pytest.raises(OperationalException, match="inverse"):
        exchange.dry_run_liquidation_price("BTC/USD:BTC", 1, False, 1, 1, 1, 1, [])
