"""Live MEXC futures order checks.

Places at most one DOGE/USDT contract (10x isolated). Aborts when that contract
would require more than 1 USDT of margin. Always flattens the DOGE position.

Skipped unless ``MEXC_LIVE_ORDER_TEST=1`` and exchange keys are present in ``.env``.
"""

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from time import sleep

import pytest

from freqtrade.resolvers.exchange_resolver import ExchangeResolver
from tests.conftest import get_default_conf_usdt


logger = logging.getLogger(__name__)

PAIR = "DOGE/USDT:USDT"
LEVERAGE = 10
MAX_MARGIN = 1.0
MAX_CONTRACTS = 1


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _contracts(position: dict) -> float:
    return abs(float(position.get("contracts") or 0))


pytestmark = [pytest.mark.longrun]


def test_mexc_live_doge_orders(testdatadir):
    if os.environ.get("MEXC_LIVE_ORDER_TEST") != "1":
        pytest.skip("Set MEXC_LIVE_ORDER_TEST=1 to place the 1 USDT DOGE order checks.")
    _load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    api_key = os.environ.get("FREQTRADE__EXCHANGE__KEY")
    secret = os.environ.get("FREQTRADE__EXCHANGE__SECRET")
    if not api_key or not secret:
        pytest.skip("MEXC API key and secret are not set.")

    config = get_default_conf_usdt(testdatadir)
    config["dry_run"] = False
    config["trading_mode"] = "futures"
    config["margin_mode"] = "isolated"
    config["stake_currency"] = "USDT"
    config["exchange"]["name"] = "mexc"
    config["exchange"]["key"] = api_key
    config["exchange"]["api_key"] = api_key
    config["exchange"]["secret"] = secret
    config["exchange"]["enable_ws"] = False
    config["exchange"]["pair_whitelist"] = [PAIR]

    exchange = ExchangeResolver.load_exchange(config, validate=True, load_leverage_tiers=True)
    try:
        _exercise_doge_orders(exchange)
    finally:
        _flatten_doge(exchange)
        exchange.close()


def _exercise_doge_orders(exchange) -> None:
    mode = exchange._api.fetch_position_mode()
    if mode.get("hedged"):
        pytest.fail("MEXC account is in hedge mode. Flatten it and switch to one-way first.")

    market = exchange.markets[PAIR]
    contract_size = float(market["contractSize"])
    base_amount = contract_size * MAX_CONTRACTS
    ticker = exchange.fetch_ticker(PAIR)
    price = float(ticker["last"])
    notional = base_amount * price
    margin = notional / LEVERAGE
    # One contract must stay above MEXC's 5 USDT minimum and within 1 USDT of margin.
    if notional < 5:
        pytest.fail(f"1 DOGE contract is worth {notional:.4f} USDT, below the 5 USDT minimum.")
    if margin > MAX_MARGIN:
        pytest.fail(f"1 DOGE contract needs {margin:.4f} USDT margin at 10x, above the 1 USDT cap.")
    min_limit = 5.0 / base_amount
    max_limit = (MAX_MARGIN * LEVERAGE) / base_amount
    buy_rate = max(min_limit * 1.05, price * 0.8)
    sell_rate = min(price * 1.2, max_limit)
    if not (min_limit <= buy_rate < price < sell_rate):
        pytest.fail(
            f"No resting limit keeps notional >= 5 USDT and margin <= 1 USDT at price {price}."
        )

    balances = exchange.get_balances()
    free = float(balances.get("USDT", {}).get("free") or 0)
    if free < margin:
        pytest.fail(
            f"USDT available balance is {free}, below the {margin:.4f} USDT margin for 1 contract."
        )

    _limit_roundtrip(exchange, base_amount, buy_rate, "buy")
    _limit_roundtrip(exchange, base_amount, sell_rate, "sell")
    _market_roundtrip_with_stops(exchange, base_amount, price, is_short=False)
    _market_roundtrip_with_stops(exchange, base_amount, price, is_short=True)


def _limit_roundtrip(exchange, amount: float, rate: float, side: str) -> None:
    order = exchange.create_order(
        pair=PAIR,
        ordertype="limit",
        side=side,
        amount=amount,
        rate=rate,
        leverage=LEVERAGE,
    )
    order_id = order["id"]
    fetched = exchange.fetch_order(order_id, PAIR)
    assert fetched["id"] == order_id
    exchange.cancel_order(order_id, PAIR)
    _wait_until_flat(exchange)


def _market_roundtrip_with_stops(exchange, amount: float, price: float, is_short: bool) -> None:
    entry_side = "sell" if is_short else "buy"
    exit_side = "buy" if is_short else "sell"
    order = exchange.create_order(
        pair=PAIR,
        ordertype="market",
        side=entry_side,
        amount=amount,
        rate=price,
        leverage=LEVERAGE,
    )
    fetched = _wait_for_order(exchange, order["id"])
    assert fetched.get("filled")
    trades = exchange.get_trades_for_order(order["id"], PAIR, datetime.now(UTC))
    assert isinstance(trades, list)
    positions = exchange.fetch_positions(PAIR)
    assert any(_contracts(position) > 0 for position in positions)

    # Keep the trigger on the safe side of the market without dropping under 5 USDT.
    stop_price = price * (1.1 if is_short else 0.9)
    for stoploss in ("limit", "market"):
        stop = exchange.create_stoploss(
            PAIR,
            amount,
            stop_price,
            {
                "stoploss": stoploss,
                "stoploss_price_type": "last",
                "stoploss_on_exchange_limit_ratio": 0.99,
            },
            exit_side,
            LEVERAGE,
        )
        loaded = exchange.fetch_stoploss_order(stop["id"], PAIR)
        assert loaded["id"] == stop["id"]
        exchange.cancel_stoploss_order(stop["id"], PAIR)

    exchange.create_order(
        pair=PAIR,
        ordertype="market",
        side=exit_side,
        amount=amount,
        rate=price,
        leverage=LEVERAGE,
        reduceOnly=True,
    )
    _wait_until_flat(exchange)


def _wait_for_order(exchange, order_id: str) -> dict:
    last = {}
    for _ in range(10):
        last = exchange.fetch_order(order_id, PAIR)
        if last.get("filled"):
            return last
        sleep(0.5)
    return last


def _wait_until_flat(exchange) -> None:
    for _ in range(10):
        positions = exchange.fetch_positions(PAIR)
        if not any(_contracts(position) > 0 for position in positions):
            return
        sleep(0.5)
    pytest.fail("DOGE position was still open after the close order.")


def _flatten_doge(exchange) -> None:
    api = exchange._api
    for params in ({}, {"trigger": True}):
        try:
            open_orders = api.fetch_open_orders(PAIR, params=params)
        except Exception:
            open_orders = []
        for order in open_orders or []:
            try:
                api.cancel_order(order["id"], PAIR, params=params)
            except Exception:
                logger.warning("Could not cancel DOGE order %s during cleanup.", order.get("id"))
    try:
        positions = api.fetch_positions([PAIR])
    except Exception:
        return
    for position in positions or []:
        contracts = _contracts(position)
        if not contracts:
            continue
        side = "sell" if position.get("side") in ("long", "buy") else "buy"
        contract_size = float(exchange.markets[PAIR]["contractSize"])
        exchange.create_order(
            pair=PAIR,
            ordertype="market",
            side=side,
            amount=contracts * contract_size,
            rate=float(exchange.fetch_ticker(PAIR)["last"]),
            leverage=LEVERAGE,
            reduceOnly=True,
        )
