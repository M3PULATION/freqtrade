from unittest.mock import ANY, AsyncMock, MagicMock, PropertyMock

import ccxt
import ccxt.pro
import pytest

from freqtrade.enums import CandleType, MarginMode, TradingMode
from freqtrade.exceptions import OperationalException, TemporaryError
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


@pytest.mark.parametrize("market_type", ["spot", "swap"])
@pytest.mark.parametrize("depth", [None, 5, 10, 20, 50])
async def test_aster_unwatch_orderbook(aster_conf, mocker, market_type, depth):
    api = ccxt.pro.aster()
    symbol = "BTC/USDT" if market_type == "spot" else "BTC/USDT:USDT"
    api.markets = {
        symbol: {
            "id": "BTCUSDT",
            "symbol": symbol,
            "type": market_type,
            "spot": market_type == "spot",
            "linear": market_type == "swap",
        }
    }
    url = api.urls["api"]["ws"]["public"][market_type]
    client = api.client(url)
    client.connected.set_result(url)
    client.connection = MagicMock(closed=False)

    async def send(message):
        if message["method"] == "SUBSCRIBE":
            client.resolve(MagicMock(), f"orderbook:{symbol}")
        else:
            api.handle_message(client, {"id": message["id"], "result": None})

    client.send = AsyncMock(side_effect=send)
    client.throttle = AsyncMock()
    client.close = AsyncMock()
    client.subscriptions = {
        f"ohlcv:{symbol}:5m": True,
        "orderbook:ETH/USDT:USDT": True,
    }
    api.load_markets = AsyncMock()
    aster_conf["exchange"]["enable_ws"] = True
    aster_conf["exchange"]["_ft_has_params"] = {"ws_orderbook_depth": depth}
    mocker.patch("freqtrade.exchange.exchange.ExchangeWS")
    exchange = get_patched_exchange(mocker, aster_conf, api, exchange="aster")
    mocker.patch.object(api, "open")

    try:
        await api.watch_order_book(symbol, depth)
        subscribed = client.send.call_args.args[0]["params"]
        client.send.reset_mock()
        await exchange._ws_async.un_watch_order_book_for_symbols([symbol])

        client.send.assert_awaited_once_with(
            {"id": ANY, "method": "UNSUBSCRIBE", "params": subscribed}
        )
        assert subscribed == [f"btcusdt@depth{depth if depth in (5, 10, 20) else 20}"]
        assert f"orderbook:{symbol}" not in client.subscriptions
        assert client.subscriptions == {
            f"ohlcv:{symbol}:5m": True,
            "orderbook:ETH/USDT:USDT": True,
        }
        api.load_markets.assert_not_awaited()

        client.send.reset_mock()
        await exchange._ws_async.un_watch_order_book_for_symbols([symbol])
        client.send.assert_not_awaited()
    finally:
        await api.close()


@pytest.mark.parametrize(
    "state", ["no_markets", "no_client", "closed", "send_error", "rejected", "timeout"]
)
async def test_aster_unwatch_orderbook_disconnected(aster_conf, mocker, state):
    api = ccxt.pro.aster()
    symbol = "BTC/USDT:USDT"
    api.markets = {
        symbol: {"id": "BTCUSDT", "symbol": symbol, "type": "swap", "spot": False, "linear": True}
    }
    client = MagicMock()
    client.closed.return_value = state == "closed"
    sent = []

    async def send(message):
        sent.append(message)
        if state == "send_error":
            raise ccxt.NetworkError("disconnected")
        if state == "rejected":
            api.handle_message(client, {"id": message["id"], "code": 2, "msg": "Invalid request"})

    client.send = AsyncMock(side_effect=send)
    client.throttle = AsyncMock()
    client.close = AsyncMock()
    client.subscriptions = {f"orderbook:{symbol}": True}
    if state != "no_client":
        api.clients[api.urls["api"]["ws"]["public"]["swap"]] = client
    if state == "no_markets":
        api.markets = None
    api.load_markets = AsyncMock()
    watch_multiple = AsyncMock()
    api.watch_multiple = watch_multiple
    aster_conf["exchange"]["enable_ws"] = True
    mocker.patch("freqtrade.exchange.exchange.ExchangeWS")
    exchange = get_patched_exchange(mocker, aster_conf, api, exchange="aster")

    try:
        errors = {
            "send_error": ccxt.NetworkError,
            "rejected": ccxt.ExchangeError,
            "timeout": TimeoutError,
        }
        if state in errors:
            with pytest.raises(errors[state]):
                await exchange._ws_async.un_watch_order_book_for_symbols([symbol])
            # Only a successful acknowledgement may remove an active subscription.
            assert client.subscriptions == {f"orderbook:{symbol}": True}
            api.handle_message(client, {"id": sent[0]["id"], "result": None})
            assert client.subscriptions == {f"orderbook:{symbol}": True}
        else:
            await exchange._ws_async.un_watch_order_book_for_symbols([symbol])
            client.send.assert_not_awaited()
            if state == "closed":
                assert client.subscriptions == {}
        api.load_markets.assert_not_awaited()
        watch_multiple.assert_not_awaited()
    finally:
        await api.close()


async def test_aster_orderbook_resubscribe(aster_conf, mocker):
    api = ccxt.pro.aster()
    symbol = "BTC/USDT:USDT"
    api.markets = {
        symbol: {"id": "BTCUSDT", "symbol": symbol, "type": "swap", "spot": False, "linear": True}
    }
    url = api.urls["api"]["ws"]["public"]["swap"]
    client = api.client(url)
    client.connected.set_result(url)
    client.connection = MagicMock(closed=False)
    client.close = AsyncMock()
    client.throttle = AsyncMock()
    api.clients[url] = client
    aster_conf["exchange"]["enable_ws"] = True
    mocker.patch("freqtrade.exchange.exchange.ExchangeWS")
    exchange = get_patched_exchange(mocker, aster_conf, api, exchange="aster")
    mocker.patch.object(api, "open")
    messages = []

    async def send(message):
        messages.append(message)
        if message["method"] == "SUBSCRIBE":
            client.resolve(MagicMock(), f"orderbook:{symbol}")
        else:
            api.handle_message(client, {"id": message["id"], "result": None})

    client.send = AsyncMock(side_effect=send)
    try:
        await api.watch_order_book(symbol)
        await exchange._ws_async.un_watch_order_book_for_symbols([symbol])
        await api.watch_order_book(symbol)
        assert [m["method"] for m in messages] == ["SUBSCRIBE", "UNSUBSCRIBE", "SUBSCRIBE"]
        assert all(m["params"] == ["btcusdt@depth20"] for m in messages)
    finally:
        await api.close()


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
        exchange.dry_run_liquidation_price("BTC/USDT:USDT", 100.0, False, 2.0, 20.0, 10, 1000, [])

    markets["BTC/USDT:USDT"]["inverse"] = False
    exchange.margin_mode = MarginMode.CROSS
    with pytest.raises(OperationalException, match="isolated"):
        exchange.dry_run_liquidation_price("BTC/USDT:USDT", 100.0, False, 2.0, 20.0, 10, 1000, [])


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


@pytest.mark.parametrize("is_short", [False, True])
@pytest.mark.parametrize("side", ["entry", "exit"])
def test_aster_ticker_pricing_without_orderbook(aster_conf, mocker, side, is_short):
    pair = "BTC/USDT:USDT"
    api = MagicMock()
    api.has = {"fetchBidsAsks": True}
    api.fetch_ticker.return_value = {"symbol": pair, "bid": None, "ask": None, "last": 100.0}
    api.fetch_bids_asks.return_value = {pair: {"bid": 99.0, "ask": 101.0}}
    markets = _aster_markets()
    markets[pair]["active"] = True
    exchange = get_patched_exchange(mocker, aster_conf, api, exchange="aster", mock_markets=markets)
    aster_conf[f"{side}_pricing"].update(use_order_book=False, price_side="same")
    assert exchange.get_rate(pair, refresh=True, side=side, is_short=is_short) == (
        101.0 if (side == "entry") == is_short else 99.0
    )
    api.fetch_bids_asks.assert_called_once_with([pair])
    assert exchange.normalize_leverage(2.5) == 2.0


@pytest.mark.parametrize("side,changed", [("sell", 91000), ("buy", 89000)])
def test_aster_stoploss_adjust_parsed_order(aster_conf, mocker, side, changed):
    exchange = get_patched_exchange(mocker, aster_conf, exchange="aster")
    market = {
        "id": "BTCUSDT",
        "symbol": "BTC/USDT:USDT",
        "base": "BTC",
        "quote": "USDT",
        "settle": "USDT",
        "spot": False,
        "swap": True,
        "type": "swap",
        "contractSize": 1,
    }
    api = ccxt.aster()
    api.set_markets([market])
    order = api.parse_order(
        {
            "symbol": "BTCUSDT",
            "orderId": "1",
            "type": "STOP_MARKET",
            "side": side.upper(),
            "stopPrice": "90000",
            "status": "NEW",
            "origQty": "1",
            "executedQty": "0",
        },
        market,
    )
    assert order["triggerPrice"] == 90000
    assert exchange.stoploss_adjust(90000, order, side) is False
    assert exchange.stoploss_adjust(changed, order, side) is True


def test_aster_builder_disabled_by_default(aster_conf, mocker):
    exchange = get_patched_exchange(mocker, aster_conf, exchange="aster")
    api = ccxt.aster(exchange._ccxt_config)
    query = mocker.patch.object(api, "fapiPrivateGetV3Builder")
    approve = mocker.patch.object(api, "fapiPrivatePostV3ApproveBuilder")
    assert api.initialize_client() is False
    query.assert_not_called()
    approve.assert_not_called()
    market = {
        "id": "BTCUSDT",
        "symbol": "BTC/USDT:USDT",
        "base": "BTC",
        "quote": "USDT",
        "spot": False,
        "swap": True,
        "type": "swap",
        "precision": {"amount": 0.001, "price": 0.1},
    }
    api.set_markets([market])
    order = api.create_order_request("BTC/USDT:USDT", "limit", "buy", 0.01, 90000)
    assert "builder" not in order
    assert "feeRate" not in order


@pytest.mark.parametrize("interval_hours", [1, 4, 8])
async def test_aster_funding_history_paginates_by_timestamp(aster_conf, mocker, interval_hours):
    exchange = get_patched_exchange(mocker, aster_conf, exchange="aster")
    step = interval_hours * 3600000
    rows = [[i * step, 0.0001] for i in range(2005)]

    async def page(pair, timeframe, candle_type, since):
        # Reverse pages to check ordering; since is inclusive, like the REST API.
        data = [row for row in rows if row[0] >= since][:1000]
        return pair, timeframe, candle_type, data[::-1], False

    fetch = mocker.patch.object(exchange, "_async_get_candle_history", side_effect=page)
    result = await exchange._async_get_historic_ohlcv(
        "BTC/USDT:USDT", "8h", 0, CandleType.FUNDING_RATE, until_ms=2005 * step
    )
    assert result[3] == rows
    assert [call.args[-1] for call in fetch.call_args_list] == [
        0,
        rows[999][0] + 1,
        rows[1999][0] + 1,
        rows[-1][0] + 1,
    ]


async def test_aster_funding_history_bounds_and_nonadvancing_page(aster_conf, mocker):
    exchange = get_patched_exchange(mocker, aster_conf, exchange="aster")
    pair = "BTC/USDT:USDT"
    fetch = mocker.patch.object(
        exchange,
        "_async_get_candle_history",
        return_value=(pair, "8h", CandleType.FUNDING_RATE, [[0, 0.1], [10, 0.2], [20, 0.3]], False),
    )
    result = await exchange._async_get_historic_ohlcv(
        pair, "8h", 5, CandleType.FUNDING_RATE, until_ms=20
    )
    assert result[3] == [[10, 0.2]]
    fetch.assert_awaited_once()
    with pytest.raises(TemporaryError, match="did not advance"):
        await exchange._async_get_historic_ohlcv(
            pair, "8h", 21, CandleType.FUNDING_RATE, until_ms=40
        )


def test_aster_dry_run_does_not_change_position_mode(aster_conf, mocker):
    api_mock = MagicMock()
    exchange = get_patched_exchange(mocker, aster_conf, api_mock, exchange="aster")
    assert isinstance(exchange, Aster)
    exchange.additional_exchange_init()
    api_mock.fetch_position_mode.assert_not_called()
