import asyncio
from unittest.mock import AsyncMock, MagicMock

import ccxt
import ccxt.pro
import pytest

from freqtrade.exchange.aster_ws import AsterWs


@pytest.fixture
async def aster_ws():
    api = ccxt.pro.aster()
    symbol = "BTC/USDT:USDT"
    api.markets = {
        symbol: {"id": "BTCUSDT", "symbol": symbol, "type": "swap", "spot": False, "linear": True}
    }
    adapter = AsterWs(api, None)
    adapter.unsubscribe_timeout = 0.05
    url = api.urls["api"]["ws"]["public"]["swap"]
    api.open = MagicMock()
    messages = []
    server_streams = set()
    controls = {"ack": True, "reject": False}
    sent_unsubscribe = asyncio.Event()

    def new_client():
        client = api.client(url)
        client.connected.set_result(url)
        client.connection = MagicMock(closed=False)
        client.throttle = AsyncMock()

        async def close(*args):
            client.connection.closed = True

        client.close = AsyncMock(side_effect=close)

        async def send(message):
            messages.append(message)
            if message["method"] == "SUBSCRIBE":
                server_streams.update(message["params"])
                for channel in message["params"]:
                    if "@depth" in channel:
                        key = f"orderbook:{symbol}"
                    else:
                        key = f"ohlcv:{symbol}:5m"
                    client.resolve([1], key)
            else:
                sent_unsubscribe.set()
                if controls["reject"]:
                    api.handle_message(client, {"id": message["id"], "code": 2, "msg": "rejected"})
                else:
                    server_streams.difference_update(message["params"])
                    if controls["ack"]:
                        api.handle_message(client, {"id": message["id"], "result": None})

        client.send = AsyncMock(side_effect=send)
        return client

    client = new_client()
    yield api, adapter, client, new_client, messages, server_streams, controls, sent_unsubscribe
    await api.close()


def stream(kind):
    if kind == "orderbook":
        return "orderbook:BTC/USDT:USDT", "btcusdt@depth20"
    return "ohlcv:BTC/USDT:USDT:5m", "btcusdt@kline_5m"


async def watch(api, kind):
    key, channel = stream(kind)
    return await api.watch_multiple(
        api.urls["api"]["ws"]["public"]["swap"],
        [key],
        {"method": "SUBSCRIBE", "params": [channel]},
        [key],
    )


async def unwatch(api, kind):
    if kind == "orderbook":
        await api.un_watch_order_book_for_symbols(["BTC/USDT:USDT"])
    else:
        await api.un_watch_ohlcv_for_symbols([["BTC/USDT:USDT", "5m"]])


@pytest.mark.parametrize("kind", ["orderbook", "ohlcv"])
async def test_aster_ws_resubscribe(aster_ws, kind):
    api, _, client, _, messages, server, _, _ = aster_ws
    other = "ohlcv" if kind == "orderbook" else "orderbook"
    await watch(api, kind)
    await watch(api, other)
    await unwatch(api, kind)
    assert stream(kind)[1] not in server
    assert stream(other)[1] in server
    assert stream(kind)[0] not in client.subscriptions
    await asyncio.wait_for(watch(api, kind), 1)
    assert stream(kind)[1] in server
    assert [m["method"] for m in messages] == ["SUBSCRIBE", "SUBSCRIBE", "UNSUBSCRIBE", "SUBSCRIBE"]


@pytest.mark.parametrize("kind", ["orderbook", "ohlcv"])
async def test_aster_ws_concurrent_resubscribe(aster_ws, kind):
    api, adapter, client, _, messages, server, controls, sent = aster_ws
    adapter.unsubscribe_timeout = 1
    await watch(api, kind)
    controls["ack"] = False
    removing = asyncio.create_task(unwatch(api, kind))
    await asyncio.wait_for(sent.wait(), 1)
    watching = asyncio.create_task(watch(api, kind))
    await asyncio.sleep(0)
    assert not watching.done()
    api.handle_message(client, {"id": messages[-1]["id"], "result": None})
    await asyncio.wait_for(asyncio.gather(removing, watching), 1)
    assert stream(kind)[1] in server
    assert [m["method"] for m in messages] == ["SUBSCRIBE", "UNSUBSCRIBE", "SUBSCRIBE"]


@pytest.mark.parametrize("kind", ["orderbook", "ohlcv"])
@pytest.mark.parametrize("failure", ["timeout", "reject", "cancel", "send"])
async def test_aster_ws_uncertain_unsubscribe_reconnects(aster_ws, kind, failure):
    api, _, client, new_client, messages, server, controls, sent = aster_ws
    await watch(api, kind)
    controls["ack"] = False
    controls["reject"] = failure == "reject"
    if failure == "send":
        client.send = AsyncMock(side_effect=ConnectionError("socket closed"))
    removing = asyncio.create_task(unwatch(api, kind))
    if failure == "cancel":
        await asyncio.wait_for(sent.wait(), 1)
        removing.cancel()
    expected = {
        "timeout": TimeoutError,
        "reject": ccxt.ExchangeError,
        "cancel": asyncio.CancelledError,
        "send": ConnectionError,
    }[failure]
    with pytest.raises(expected):
        await removing
    assert client.url not in api.clients
    fresh = new_client()
    controls["ack"] = True
    await asyncio.wait_for(watch(api, kind), 1)
    assert stream(kind)[1] in server
    # A late reply for the discarded socket cannot remove the new subscription.
    old_request = next((m for m in messages if m["method"] == "UNSUBSCRIBE"), None)
    if old_request:
        api.handle_message(client, {"id": old_request["id"], "result": None})
    assert stream(kind)[0] in fresh.subscriptions


@pytest.mark.parametrize("kind", ["orderbook", "ohlcv"])
async def test_aster_ws_unwatch_before_first_frame(aster_ws, kind):
    api, _, client, _, messages, server, _, _ = aster_ws
    sending = asyncio.Event()
    release_send = asyncio.Event()
    original_send = client.send

    async def send(message):
        if message["method"] == "SUBSCRIBE":
            sending.set()
            await release_send.wait()
        await original_send(message)

    client.send = AsyncMock(side_effect=send)
    watching = asyncio.create_task(watch(api, kind))
    await asyncio.wait_for(sending.wait(), 1)
    removing = asyncio.create_task(unwatch(api, kind))
    await asyncio.sleep(0)
    assert not removing.done()
    release_send.set()
    await asyncio.wait_for(asyncio.gather(watching, removing), 1)
    assert [m["method"] for m in messages] == ["SUBSCRIBE", "UNSUBSCRIBE"]
    assert stream(kind)[1] not in server
    assert stream(kind)[0] not in client.subscriptions
    await asyncio.wait_for(watch(api, kind), 1)
    assert stream(kind)[1] in server


@pytest.mark.parametrize("kind", ["orderbook", "ohlcv"])
@pytest.mark.parametrize("failure", ["cancel", "send"])
async def test_aster_ws_failed_subscribe_reconnects(aster_ws, kind, failure):
    api, _, client, new_client, _, server, _, _ = aster_ws
    sending = asyncio.Event()

    async def send(message):
        sending.set()
        if failure == "send":
            raise ConnectionError("socket closed")
        await asyncio.Future()

    client.send = AsyncMock(side_effect=send)
    watching = asyncio.create_task(watch(api, kind))
    await asyncio.wait_for(sending.wait(), 1)
    if failure == "cancel":
        watching.cancel()
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else ConnectionError):
        await watching
    assert client.url not in api.clients
    fresh = new_client()
    await asyncio.wait_for(watch(api, kind), 1)
    assert stream(kind)[0] in fresh.subscriptions
    assert stream(kind)[1] in server


@pytest.mark.parametrize("kind", ["orderbook", "ohlcv"])
async def test_aster_ws_preexisting_watch_is_released(aster_ws, kind):
    api, _, client, _, _, _, _, _ = aster_ws
    await watch(api, kind)
    waiting = asyncio.create_task(watch(api, kind))
    await asyncio.sleep(0)
    assert not waiting.done()
    await unwatch(api, kind)
    with pytest.raises(ccxt.NetworkError, match="subscription removed"):
        await waiting
    await asyncio.wait_for(watch(api, kind), 1)
    assert stream(kind)[0] in client.subscriptions
