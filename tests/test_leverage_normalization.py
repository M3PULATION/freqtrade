from unittest.mock import MagicMock

import pandas as pd
import pytest

from freqtrade.enums import SignalDirection
from freqtrade.optimize.backtesting import Backtesting
from freqtrade.persistence import LocalTrade, Trade
from freqtrade.rpc import RPC
from tests.conftest import EXMS, get_patched_freqtradebot, patch_exchange


LEVERAGE_CASES = [
    pytest.param(True, 2.5, 10.0, 2.0, id="fractional"),
    pytest.param(True, 5.0, 10.0, 5.0, id="integer"),
    pytest.param(False, 2.5, 10.0, 2.5, id="default-exchange"),
    pytest.param(True, 5.0, 2.5, 2.0, id="fractional-maximum"),
    pytest.param(True, 0.5, 10.0, 1.0, id="minimum"),
]


@pytest.fixture
def futures_config(default_conf_usdt):
    default_conf_usdt.update(trading_mode="futures", margin_mode="isolated", stake_amount=100.0)
    default_conf_usdt["exchange"]["pair_whitelist"] = ["ETH/USDT:USDT"]
    return default_conf_usdt


def filled_entry_order(**kwargs):
    return {
        "id": "normalized-entry",
        "symbol": kwargs["pair"],
        "type": kwargs["ordertype"],
        "side": kwargs["side"],
        "status": "closed",
        "price": kwargs["rate"],
        "average": kwargs["rate"],
        "amount": kwargs["amount"],
        "filled": kwargs["amount"],
        "remaining": 0.0,
        "cost": kwargs["amount"] * kwargs["rate"],
    }


@pytest.mark.parametrize("requires_integer,requested,max_leverage,expected", LEVERAGE_CASES)
@pytest.mark.parametrize("force_entry", [False, True])
@pytest.mark.parametrize("is_short", [False, True])
def test_entry_normalizes_leverage_before_sizing(
    mocker,
    futures_config,
    fee,
    requires_integer,
    requested,
    max_leverage,
    expected,
    force_entry,
    is_short,
):
    if requires_integer:
        futures_config["exchange"]["_ft_has_params"] = {"leverage_requires_integer": True}
    futures_config["force_entry_enable"] = True
    bot = get_patched_freqtradebot(mocker, futures_config)
    create_order = mocker.patch(f"{EXMS}.create_order", side_effect=filled_entry_order)
    min_stake = mocker.patch(f"{EXMS}.get_min_pair_stake_amount", return_value=1.0)
    max_stake = mocker.patch(f"{EXMS}.get_max_pair_stake_amount", return_value=float("inf"))
    mocker.patch.multiple(
        EXMS,
        get_fee=fee,
        get_rate=MagicMock(return_value=10.0),
        get_max_leverage=MagicMock(return_value=max_leverage),
        get_funding_fees=MagicMock(return_value=0.0),
        get_maintenance_ratio_and_amt=MagicMock(return_value=(0.01, 0.0)),
    )
    bot.strategy.leverage = MagicMock(return_value=requested)
    custom_stake = mocker.spy(bot.strategy, "custom_stake_amount")
    confirm_entry = mocker.spy(bot.strategy, "confirm_trade_entry")
    pair = "ETH/USDT:USDT"
    rate = 10.0
    stake = 100.0

    if force_entry:
        trade = RPC(bot)._rpc_force_entry(
            pair,
            rate,
            stake_amount=stake,
            leverage=requested,
            order_side=SignalDirection.SHORT if is_short else SignalDirection.LONG,
        )
        bot.strategy.leverage.assert_not_called()
    else:
        assert bot.execute_entry(pair, stake, price=rate, is_short=is_short)
        trade = Trade.get_trades().first()
        bot.strategy.leverage.assert_called_once()

    assert trade is not None
    amount = stake * expected / rate
    assert create_order.call_args.kwargs["leverage"] == expected
    assert create_order.call_args.kwargs["amount"] == pytest.approx(amount)
    assert confirm_entry.call_args.kwargs["amount"] == pytest.approx(amount)
    assert custom_stake.call_args.kwargs["leverage"] == expected
    assert min_stake.call_args.args[-1] == expected
    assert max_stake.call_args.args[-1] == expected
    assert trade.leverage == expected
    assert trade.amount == pytest.approx(amount)
    assert trade.stake_amount == pytest.approx(stake)
    assert trade.orders[0].stake_amount == pytest.approx(stake)
    assert trade.is_short is is_short


@pytest.mark.parametrize("requires_integer,requested,max_leverage,expected", LEVERAGE_CASES)
@pytest.mark.parametrize("direction", ["long", "short"])
def test_backtest_normalizes_leverage_before_sizing(
    mocker,
    request,
    futures_config,
    requires_integer,
    requested,
    max_leverage,
    expected,
    direction,
):
    if requires_integer:
        futures_config["exchange"]["_ft_has_params"] = {"leverage_requires_integer": True}
    patch_exchange(mocker)
    min_stake = mocker.patch(f"{EXMS}.get_min_pair_stake_amount", return_value=1.0)
    max_stake = mocker.patch(f"{EXMS}.get_max_pair_stake_amount", return_value=float("inf"))
    mocker.patch(f"{EXMS}.get_max_leverage", return_value=max_leverage)
    mocker.patch(f"{EXMS}.get_maintenance_ratio_and_amt", return_value=(0.01, 0.0))
    mocker.patch("freqtrade.optimize.backtesting.Backtesting._run_funding_fees")
    request.addfinalizer(Backtesting.cleanup)
    backtesting = Backtesting(futures_config)
    backtesting._set_strategy(backtesting.strategylist[0])
    backtesting.strategy.leverage = MagicMock(return_value=requested)
    custom_stake = mocker.spy(backtesting.strategy, "custom_stake_amount")
    confirm_entry = mocker.spy(backtesting.strategy, "confirm_trade_entry")
    row = [
        pd.Timestamp("2020-01-01T05:00:00Z"),
        10.0,  # Open
        11.0,  # High
        9.0,  # Low
        10.0,  # Close
        1,  # enter_long
        0,  # exit_long
        1,  # enter_short
        0,  # exit_short
        "",  # enter_tag
        "",  # exit_tag
    ]

    trade = backtesting._enter_trade("ETH/USDT:USDT", row=row, direction=direction)

    assert isinstance(trade, LocalTrade)
    amount = 100.0 * expected / 10.0
    assert trade.leverage == expected
    assert trade.amount == pytest.approx(amount)
    assert trade.amount_requested == pytest.approx(amount)
    assert trade.stake_amount == pytest.approx(100.0)
    assert trade.orders[0].stake_amount == pytest.approx(100.0)
    assert confirm_entry.call_args.kwargs["amount"] == pytest.approx(amount)
    assert custom_stake.call_args.kwargs["leverage"] == expected
    assert min_stake.call_args.kwargs["leverage"] == expected
    assert max_stake.call_args.kwargs["leverage"] == expected
