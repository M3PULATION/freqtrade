"""MEXC exchange subclass.

Spot and USDT-M isolated futures share the ccxt id ``mexc``. Futures orders are
swaps (``defaultType=swap``). Isolated orders must carry an integer leverage,
and one-way mode maps buy/sell through ``hedged=False``.
"""

import logging
import os
from typing import Any

import ccxt

from freqtrade.constants import BuySell, MakerTaker
from freqtrade.enums import MarginMode, PriceType, TradingMode
from freqtrade.exceptions import (
    DDosProtection,
    ExchangeError,
    OperationalException,
    RetryableOrderError,
    TemporaryError,
)
from freqtrade.exchange import Exchange
from freqtrade.exchange.common import retrier
from freqtrade.exchange.exchange_types import CcxtOrder, FtHas
from freqtrade.misc import deep_merge_dicts


logger = logging.getLogger(__name__)


class Mexc(Exchange):
    """MEXC exchange class.

    Contains adjustments needed for Freqtrade to work with this exchange.
    """

    _ft_has: FtHas = {
        "ohlcv_candle_limit": 500,
        # Spot wss://wbs-api.mexc.com drops the keepalive. Futures uses
        # wss://contract.mexc.com/edge, which is enabled below.
        "ws_enabled": {"ohlcv": False, "orderbook": False},
    }
    _ft_has_futures: FtHas = {
        "ohlcv_candle_limit": 2000,
        "ws_enabled": {"ohlcv": True, "orderbook": True},
        # Contract kline "vol" / websocket "q" are in contracts, not base currency.
        "ohlcv_volume_in_contracts": True,
        "funding_fee_candle_limit": 100,
        "stoploss_on_exchange": True,
        "stoploss_order_types": {"limit": "limit", "market": "market"},
        "stop_price_param": "stopPrice",
        "stop_price_prop": "stopPrice",
        "stop_price_type_field": "trend",
        "stop_price_type_value_mapping": {
            # 1 last price, 2 fair/mark price, 3 index price.
            PriceType.LAST: 1,
            PriceType.MARK: 2,
            PriceType.INDEX: 3,
        },
        "stoploss_blocks_assets": False,
    }

    _supported_trading_mode_margin_pairs: list[tuple[TradingMode, MarginMode]] = [
        (TradingMode.SPOT, MarginMode.NONE),
        (TradingMode.FUTURES, MarginMode.ISOLATED),
    ]

    _mexc_order_leverage: float | None = None

    @property
    def _ccxt_config(self) -> dict:
        config: dict = {}
        # Direct connections to api.mexc.com time out on some networks. Honor the
        # process proxy so both REST and the contract websocket go through it.
        proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
        if proxy:
            config["httpsProxy"] = proxy
            config["wsProxy"] = proxy
        # A futures-only key is rejected by the private spot currency endpoint,
        # which otherwise blocks load_markets. Public market data does not need it.
        config["has"] = {"fetchCurrencies": False}
        if self.trading_mode == TradingMode.FUTURES:
            config["options"] = {"defaultSettle": self._config["stake_currency"]}
        return deep_merge_dicts(config, super()._ccxt_config)

    def _patch_swap_ohlcv_volume(self, ccxt_obj: Any) -> None:
        """Fill contract kline volume from ``q`` when ccxt leaves it empty."""
        if ccxt_obj is None or getattr(ccxt_obj, "_ft_mexc_ohlcv_patched", False):
            return
        if not hasattr(ccxt_obj, "parse_ws_ohlcv"):
            return
        original = ccxt_obj.parse_ws_ohlcv

        def parse_ws_ohlcv(ohlcv, market=None):
            row = original(ohlcv, market)
            if (
                market
                and market.get("swap")
                and row[5] is None
                and isinstance(ohlcv, dict)
                and ohlcv.get("q") is not None
            ):
                row[5] = ccxt_obj.safe_number(ohlcv, "q")
            return row

        ccxt_obj.parse_ws_ohlcv = parse_ws_ohlcv
        ccxt_obj._ft_mexc_ohlcv_patched = True

    def _futures_account_is_flat(self) -> bool:
        """True when no contract position is open. Used before switching position mode."""
        positions = self._api.fetch_positions()
        for position in positions or []:
            contracts = position.get("contracts") or 0
            if contracts:
                return False
        return True

    @retrier
    def additional_exchange_init(self) -> None:
        """
        Patch websocket candle volume and, on a flat live futures account,
        switch the whole account to one-way mode.
        """
        try:
            self._patch_swap_ohlcv_volume(self._ws_async)
            if self._config["dry_run"] or self.trading_mode != TradingMode.FUTURES:
                return
            if not self._futures_account_is_flat():
                logger.info(
                    "MEXC: open positions exist, leaving the current position mode unchanged."
                )
                return
            position_mode = self._api.set_position_mode(False)
            self._log_exchange_response("set_position_mode", position_mode)
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Error in additional_exchange_init due to {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    def _lev_prep(self, pair: str, leverage: float, side: BuySell, accept_fail: bool = False):
        if self.trading_mode == TradingMode.SPOT:
            return
        # set_margin_mode and set_leverage hit the same endpoint and both require
        # leverage plus a direction when no position exists yet.
        params = {
            "leverage": int(leverage),
            "direction": "short" if side == "sell" else "long",
        }
        self.set_margin_mode(pair, self.margin_mode, accept_fail=True, params=params)

    def _get_params(
        self,
        side: BuySell,
        ordertype: str,
        leverage: float,
        reduceOnly: bool,
        time_in_force: str = "GTC",
    ) -> dict:
        params = super()._get_params(
            side=side,
            ordertype=ordertype,
            leverage=leverage,
            reduceOnly=reduceOnly,
            time_in_force=time_in_force,
        )
        if self.trading_mode != TradingMode.FUTURES or not self.margin_mode:
            return params
        params["marginMode"] = self.margin_mode.value
        params["leverage"] = int(leverage)
        params["hedged"] = False
        # ccxt forwards unknown keys into the contract body. MEXC order types
        # encode IOC/FOK/post-only; a raw timeInForce string is rejected.
        tif = params.pop("timeInForce", None)
        if tif == "PO":
            params["postOnly"] = True
        return params

    def _get_stop_params(self, side: BuySell, ordertype: str, stop_price: float) -> dict:
        params = super()._get_stop_params(side=side, ordertype=ordertype, stop_price=stop_price)
        if self.trading_mode != TradingMode.FUTURES:
            return params
        # triggerType 1: price >= trigger (short stop). 2: price <= trigger (long stop).
        # executeCycle 2 is the longest plan-order lifetime ccxt documents (7 days).
        params.update(
            {
                "marginMode": self.margin_mode.value,
                "hedged": False,
                "executeCycle": 2,
                "triggerType": 2 if side == "sell" else 1,
                "orderType": 5 if ordertype == "market" else 1,
            }
        )
        if self._mexc_order_leverage is not None:
            params["leverage"] = int(self._mexc_order_leverage)
        return params

    def create_stoploss(
        self,
        pair: str,
        amount: float,
        stop_price: float,
        order_types: dict,
        side: BuySell,
        leverage: float,
    ) -> CcxtOrder:
        self._mexc_order_leverage = leverage
        try:
            return super().create_stoploss(pair, amount, stop_price, order_types, side, leverage)
        finally:
            self._mexc_order_leverage = None

    def fetch_stoploss_order(
        self, order_id: str, pair: str, params: dict | None = None
    ) -> CcxtOrder:
        # ccxt fetch_order() always hits the regular order endpoint and cannot see plan orders.
        if self._config["dry_run"]:
            return self.fetch_dry_run_order(order_id)
        params = {} if params is None else params.copy()
        params["trigger"] = True
        for order in self._api.fetch_open_orders(pair, params=params):
            if str(order.get("id")) == str(order_id):
                return self._order_contracts_to_amount(order)
        raise RetryableOrderError(f"Stoploss order not found (pair: {pair} id: {order_id}).")

    def cancel_stoploss_order(self, order_id: str, pair: str, params: dict | None = None) -> dict:
        params = {} if params is None else params.copy()
        params["trigger"] = True
        return super().cancel_stoploss_order(order_id, pair, params)

    def get_fee(
        self,
        symbol: str,
        order_type: str = "",
        side: str = "",
        amount: float = 1,
        price: float = 1,
        taker_or_maker: MakerTaker = "maker",
    ) -> float:
        fee = super().get_fee(symbol, order_type, side, amount, price, taker_or_maker)
        # MEXC publishes a 0 maker fee. Limit-order estimates would otherwise be free.
        if fee:
            return fee
        taker = (self.markets.get(symbol) or {}).get("taker")
        return float(taker) if taker else fee

    def _tiers_from_risk_limit(self, rows: list[dict]) -> list[dict]:
        """
        ccxt collapses MEXC's ``riskLimitCustom`` into one tier with null bounds
        when ``riskIncrVol`` is 0. Rebuild the levels from the raw payload.
        Bounds stay in contracts, which is what the exchange risk limit uses.
        """
        if not rows:
            return rows
        info = rows[0].get("info") or {}
        custom = info.get("riskLimitCustom") or []
        if not custom:
            rate = info.get("maintenanceMarginRate")
            max_leverage = info.get("maxLeverage") or rows[0].get("maxLeverage")
            if rate is None or max_leverage is None:
                return rows
            return [
                {
                    "minNotional": 0.0,
                    "maxNotional": None,
                    "maintenanceMarginRate": float(rate),
                    "maxLeverage": float(max_leverage),
                    "maintAmt": None,
                }
            ]
        parsed: list[dict] = []
        floor = 0.0
        for level in custom:
            cap = float(level["maxVol"])
            parsed.append(
                {
                    "minNotional": floor,
                    "maxNotional": cap,
                    "maintenanceMarginRate": float(level["mmr"]),
                    "maxLeverage": float(level["maxLeverage"]),
                    "maintAmt": None,
                }
            )
            floor = cap
        return parsed

    def get_leverage_tiers(self) -> dict[str, list[dict]]:
        tiers = super().get_leverage_tiers()
        return {pair: self._tiers_from_risk_limit(rows) for pair, rows in tiers.items()}

    def get_maintenance_ratio_and_amt(
        self, pair: str, notional_value: float
    ) -> tuple[float, float | None]:
        """
        Use the first leverage tier.

        ccxt reports MEXC tier bounds in contracts (``riskIncrVol``), while callers
        pass a stake amount in settle currency. Comparing the two selects the wrong
        tier. One-way mode also resets the risk limit to level 1.
        """
        if pair not in self._leverage_tiers:
            raise ExchangeError(
                f"Maintenance margin rate for {pair} is unavailable for {self.name}"
            )
        tier = self._leverage_tiers[pair][0]
        rate = tier.get("maintenanceMarginRate")
        if rate is None:
            raise ExchangeError(
                f"Maintenance margin rate for {pair} is unavailable for {self.name}"
            )
        return rate, tier.get("maintAmt")

    def dry_run_liquidation_price(
        self,
        pair: str,
        open_rate: float,
        is_short: bool,
        amount: float,
        stake_amount: float,
        leverage: float,
        wallet_balance: float,
        open_trades: list,
    ) -> float | None:
        """
        USDT-M isolated liquidation price.

        Long:
            (entry * size - margin) / (size * (1 - maintenance margin rate))
        Short:
            (entry * size + margin) / (size * (1 + maintenance margin rate))

        ``amount`` is the position size in base currency (contracts * contract size).
        ``stake_amount`` is the isolated margin. Must only read cached values.
        """
        market = self.markets[pair]
        if market.get("inverse"):
            raise OperationalException("Freqtrade does not yet support inverse contracts")
        if self.trading_mode != TradingMode.FUTURES or self.margin_mode != MarginMode.ISOLATED:
            raise OperationalException(
                "Freqtrade only supports isolated futures for leverage trading"
            )
        if amount == 0:
            raise OperationalException(
                f"Cannot calculate liquidation price for zero amount on {pair}"
            )

        mm_ratio, _ = self.get_maintenance_ratio_and_amt(pair, stake_amount)
        if is_short:
            return (open_rate * amount + stake_amount) / (amount * (1 + mm_ratio))
        return (open_rate * amount - stake_amount) / (amount * (1 - mm_ratio))
