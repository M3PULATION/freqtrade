from unittest.mock import MagicMock, PropertyMock

import ccxt
import pytest

from freqtrade.enums import MarginMode, TradingMode
from freqtrade.exceptions import OperationalException
from freqtrade.exchange.aster import Aster
from tests.conftest import get_patched_exchange


def _aster_markets() -> dict:
    return {
        "BTC/USDT:USDT": {
            "symbol": "BTC/USDT:USDT",
            "base": "BTC",
            "quote": "USDT",
            "settle": "USDT",
            "spot": False,
            "swap": True,
            "linear": True,
            "inverse": False,
            "contractSize": 1.0,
            "limits": {"leverage": {"min": None, "max": None}},
            "info": {"requiredMarginPercent": "5.0000", "maintMarginPercent": "2.5000"},
        },
        "ETH/USDT:USDT": {
            "symbol": "ETH/USDT:USDT",
            "spot": False,
            "swap": True,
            "linear": True,
            "inverse": False,
            "limits": {"leverage": {"min": None, "max": 50}},
            "info": {"requiredMarginPercent": "5.0000", "maintMarginPercent": "1.0000"},
        },
    }


@pytest.fixture
def aster_conf(default_conf_usdt):
    default_conf_usdt["trading_mode"] = TradingMode.FUTURES
    default_conf_usdt["margin_mode"] = MarginMode.ISOLATED
    default_conf_usdt["exchange"]["name"] = "aster"
    return default_conf_usdt


def test_aster_ccxt_config_and_order_params(aster_conf, mocker):
    exchange = get_patched_exchange(mocker, aster_conf, exchange="aster")
    config = exchange._ccxt_config
    config.pop("httpsProxy", None)
    config.pop("wsProxy", None)
    assert config["options"]["defaultType"] == "swap"
    assert config["options"]["defaultSettle"] == "USDT"
    assert config["options"]["createOrder"]["quoteOrderQty"] is False

    params = exchange._get_params("buy", "limit", 10, False, time_in_force="PO")
    assert params["positionSide"] == "BOTH"
    assert params["postOnly"] is True
    assert "timeInForce" not in params

    stop_params = exchange._get_stop_params("sell", "market", 100.0)
    assert stop_params["positionSide"] == "BOTH"
    assert stop_params["stopLossPrice"] == 100.0


def test_aster_reduce_only_is_string(aster_conf, mocker):
    seen: dict = {}

    def create_order(*args, **kwargs):
        seen["params"] = args[5] if len(args) > 5 else kwargs.get("params")

    api_mock = MagicMock()
    api_mock.create_order = create_order
    exchange = get_patched_exchange(mocker, aster_conf, api_mock, exchange="aster")
    exchange._api.create_order("BTC/USDT:USDT", "market", "sell", 1, None, {"reduceOnly": True})
    assert seen["params"]["reduceOnly"] == "true"
    exchange._api.create_order(
        symbol="BTC/USDT:USDT",
        type="market",
        side="sell",
        amount=1,
        price=None,
        params={"reduceOnly": False},
    )
    assert seen["params"]["reduceOnly"] == "false"


def test_aster_max_leverage_and_liquidation(aster_conf, mocker):
    exchange = get_patched_exchange(
        mocker, aster_conf, exchange="aster", mock_markets=_aster_markets()
    )
    assert exchange.get_max_leverage("BTC/USDT:USDT", 10) == 20
    assert exchange.get_max_leverage("ETH/USDT:USDT", 10) == 50

    long_price = exchange.dry_run_liquidation_price(
        "BTC/USDT:USDT", 100.0, False, 2.0, 20.0, 10, 1000, []
    )
    short_price = exchange.dry_run_liquidation_price(
        "BTC/USDT:USDT", 100.0, True, 2.0, 20.0, 10, 1000, []
    )
    assert long_price == pytest.approx(180 / 1.95)
    assert short_price == pytest.approx(220 / 2.05)


def test_aster_liquidation_uses_leverage_bracket(aster_conf, mocker):
    exchange = get_patched_exchange(
        mocker, aster_conf, exchange="aster", mock_markets=_aster_markets()
    )
    exchange._leverage_tiers["BTC/USDT:USDT"] = [
        {
            "minNotional": 0.0,
            "maxNotional": 80000.0,
            "maintenanceMarginRate": 0.0066,
            "maxLeverage": 75.0,
            "maintAmt": 0.0,
        }
    ]
    # Wallet observed on an isolated 2x DOGE short, scaled onto the test pair.
    price = exchange.dry_run_liquidation_price(
        "BTC/USDT:USDT", 0.08354, True, 62.0, 2.58952820, 2, 20, []
    )
    assert price == pytest.approx(0.12448498, abs=1e-6)
    assert exchange.get_max_leverage("BTC/USDT:USDT", 10) == 75


def test_aster_parses_leverage_brackets(aster_conf, mocker):
    api_mock = MagicMock()
    api_mock.fapiPrivateGetV3LeverageBracket.return_value = [
        {
            "symbol": "DOGEUSDT",
            "brackets": [
                {
                    "initialLeverage": 75,
                    "notionalCap": 80000,
                    "notionalFloor": 0,
                    "maintMarginRatio": 0.0066,
                    "cum": 0,
                }
            ],
        }
    ]
    api_mock.markets_by_id = {"DOGEUSDT": [{"symbol": "DOGE/USDT:USDT", "swap": True}]}
    exchange = get_patched_exchange(mocker, aster_conf, api_mock, exchange="aster")
    exchange._config["dry_run"] = False

    tiers = exchange.load_leverage_tiers()

    assert tiers["DOGE/USDT:USDT"][0]["maxLeverage"] == 75
    assert tiers["DOGE/USDT:USDT"][0]["maintenanceMarginRate"] == 0.0066
    assert tiers["DOGE/USDT:USDT"][0]["info"]["cum"] == 0


def test_aster_liquidation_rejects_cross_and_inverse(aster_conf, mocker):
    markets = _aster_markets()
    markets["BTC/USDT:USDT"]["inverse"] = True
    exchange = get_patched_exchange(mocker, aster_conf, exchange="aster", mock_markets=markets)
    with pytest.raises(OperationalException, match="inverse"):
        exchange.dry_run_liquidation_price(
            "BTC/USDT:USDT", 100.0, False, 2.0, 20.0, 10, 1000, []
        )

    markets["BTC/USDT:USDT"]["inverse"] = False
    exchange.margin_mode = MarginMode.CROSS
    with pytest.raises(OperationalException, match="isolated"):
        exchange.dry_run_liquidation_price(
            "BTC/USDT:USDT", 100.0, False, 2.0, 20.0, 10, 1000, []
        )


def test_aster_sets_one_way_mode_when_flat(aster_conf, mocker):
    api_mock = MagicMock()
    api_mock.fetch_position_mode.return_value = {"hedged": True}
    api_mock.fetch_positions.return_value = [{"contracts": 0}]
    aster_conf["dry_run"] = False
    get_patched_exchange(mocker, aster_conf, api_mock, exchange="aster")

    api_mock.set_position_mode.assert_called_once_with(False)


def test_aster_refuses_hedge_mode_with_open_position(aster_conf, mocker):
    api_mock = MagicMock()
    api_mock.fetch_position_mode.return_value = {"hedged": True}
    api_mock.fetch_positions.return_value = [{"contracts": 1.5}]
    aster_conf["dry_run"] = False
    with pytest.raises(OperationalException, match="hedge mode"):
        get_patched_exchange(mocker, aster_conf, api_mock, exchange="aster")
    api_mock.set_position_mode.assert_not_called()


def test_aster_margin_mode_already_set_is_success(aster_conf, mocker):
    api_mock = MagicMock()
    type(api_mock).has = PropertyMock(return_value={"setMarginMode": True})
    api_mock.set_margin_mode.side_effect = ccxt.NoChange(
        'aster {"code":-4046,"msg":"No need to change margin type."}'
    )
    exchange = get_patched_exchange(mocker, aster_conf, api_mock, exchange="aster")
    exchange._config["dry_run"] = False

    exchange.set_margin_mode("DOGE/USDT:USDT", MarginMode.ISOLATED)

    api_mock.set_margin_mode.assert_called_once()


def test_aster_dry_run_does_not_change_position_mode(aster_conf, mocker):
    api_mock = MagicMock()
    exchange = get_patched_exchange(mocker, aster_conf, api_mock, exchange="aster")
    assert isinstance(exchange, Aster)
    exchange.additional_exchange_init()
    api_mock.fetch_position_mode.assert_not_called()
