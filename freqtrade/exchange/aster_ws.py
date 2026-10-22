"""Aster subscription lifecycle compatibility for CCXT 4.5.88/89."""

import asyncio
from itertools import count

import ccxt


class AsterWs:
    unsubscribe_timeout = 2.0

    def __init__(self, api, orderbook_depth: int | None) -> None:
        self.api = api
        self.orderbook_depth = orderbook_depth
        self._request_ids = count(api.milliseconds())
        self._pending: dict = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._channels: dict[tuple[str, str], str] = {}
        self._handle_message = api.handle_message
        self._watch_multiple = api.watch_multiple
        api.handle_message = self.handle_message
        api.watch_multiple = self.watch_multiple
        api.un_watch_order_book_for_symbols = self.unwatch_orderbook
        api.un_watch_ohlcv_for_symbols = self.unwatch_ohlcv

    def _lock(self, url: str) -> asyncio.Lock:
        return self._locks.setdefault(url, asyncio.Lock())

    def handle_message(self, client, message) -> None:
        future = self._pending.get((id(client), message.get("id")))
        if future is not None and not future.done():
            if "result" in message and message["result"] is None:
                future.set_result(None)
            else:
                future.set_exception(ccxt.ExchangeError(f"Aster unsubscribe failed: {message}"))
            return
        self._handle_message(client, message)

    async def watch_multiple(
        self, url, message_hashes, message=None, subscribe_hashes=None, subscription=None
    ):
        # CCXT queues sends after connecting. Send under the lock ourselves so an
        # immediate unwatch cannot overtake SUBSCRIBE before the first data frame.
        async with self._lock(url):
            client = self.api.client(url)
            missing = any(key not in client.subscriptions for key in subscribe_hashes or [])
            future = self._watch_multiple(url, message_hashes, None, subscribe_hashes, subscription)
            if missing and message:
                try:
                    await asyncio.shield(client.connected)
                    if self.api.enableRateLimit:
                        await client.throttle(self.api.options.get("ws", {}).get("cost", 1))
                    await client.send(message)
                    if message.get("method") == "SUBSCRIBE":
                        for key, channel in zip(
                            subscribe_hashes or [], message["params"], strict=True
                        ):
                            self._channels[url, key] = channel
                except (Exception, asyncio.CancelledError):
                    future.cancel()
                    client.on_error(ccxt.NetworkError("Aster subscription could not be sent"))
                    raise
        # Never hold the subscription lock while waiting for market data.
        return await future

    async def unwatch_orderbook(self, symbols, params=None) -> None:
        if not self.api.markets or not symbols:
            return
        symbols = self.api.market_symbols(symbols, None, True, True, True)
        params = params or {}
        depth = params.get("limit", self.orderbook_depth)
        depth = depth if depth in (5, 10, 20) else 20
        streams = {
            f"orderbook:{symbol}": f"{self.api.market(symbol)['id'].lower()}@depth{depth}"
            for symbol in symbols
        }
        await self._unsubscribe(symbols[0], streams, params)

    async def unwatch_ohlcv(self, symbols_and_timeframes, params=None) -> None:
        if not self.api.markets or not symbols_and_timeframes:
            return
        streams = {}
        for symbol, timeframe in symbols_and_timeframes:
            market = self.api.market(symbol)
            interval = self.api.timeframes.get(timeframe, timeframe)
            streams[f"ohlcv:{market['symbol']}:{timeframe}"] = (
                f"{market['id'].lower()}@kline_{interval}"
            )
        await self._unsubscribe(symbols_and_timeframes[0][0], streams, params or {})

    async def _unsubscribe(self, symbol, streams, params) -> None:
        market = self.api.market(symbol)
        url = self.api.urls["api"]["ws"]["public"][market["type"]]
        async with self._lock(url):
            client = self.api.clients.get(url)
            if client is None:
                return
            streams = {
                key: channel for key, channel in streams.items() if key in client.subscriptions
            }
            if not streams:
                return
            if not client.closed():
                await self._send_unsubscribe(client, url, streams, params)
            for key in streams:
                client.subscriptions.pop(key, None)
                self._channels.pop((url, key), None)
                # A watch started before unwatch must wake up and be rescheduled.
                future = client.futures.pop(key, None)
                if future is not None and not future.done():
                    future.reject(ccxt.NetworkError(f"Aster subscription removed: {key}"))

    async def _send_unsubscribe(self, client, url, streams, params) -> None:
        request_id = next(self._request_ids)
        key = (id(client), request_id)
        future = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        try:
            async with asyncio.timeout(self.unsubscribe_timeout):
                if self.api.enableRateLimit:
                    await client.throttle(self.api.options.get("ws", {}).get("cost", 1))
                await client.send(
                    {
                        **self.api.omit(params, ["limit", "callerMethodName"]),
                        "id": request_id,
                        "method": "UNSUBSCRIBE",
                        "params": [
                            self._channels.get((url, name), channel)
                            for name, channel in streams.items()
                        ],
                    }
                )
                await future
        except (Exception, asyncio.CancelledError):
            # The server may have unsubscribed even when its reply was lost. Drop
            # the ambiguous connection so all its watchers can resubscribe safely.
            client.on_error(ccxt.NetworkError("Aster unsubscribe was not acknowledged"))
            raise
        finally:
            self._pending.pop(key, None)
            future.cancel()
