"""Aster exchange subclass"""

import logging
import os

import ccxt

from freqtrade.constants import BuySell
from freqtrade.enums import MarginMode, PriceType, TradingMode
from freqtrade.exceptions import DDosProtection, OperationalException, TemporaryError
from freqtrade.exchange import Exchange
from freqtrade.exchange.common import retrier
from freqtrade.exchange.exchange_types import FtHas, Tickers
from freqtrade.misc import deep_merge_dicts


logger = logging.getLogger(__name__)


class Aster(Exchange):
    """Aster exchange class.

    Aster authenticates private calls with an Ethereum private key, like Hyperliquid.
    The futures REST API follows the Binance USD-M shape: isolated margin, one-way
    positions, and stop orders triggered by last or mark price.
    """

    _ft_has: FtHas = {
        "ohlcv_candle_limit": 1500,
        "l2_limit_range": [5, 10, 20, 50, 100],
        "order_time_in_force": ["GTC", "FOK", "IOC", "PO"],
        "stoploss_on_exchange": False,
        "ws_enabled": {"ohlcv": True, "orderbook": True},
    }
    _ft_has_futures: FtHas = {
        "ohlcv_candle_limit": 1500,
        "funding_fee_candle_limit": 1000,
        "funding_fee_timeframe": "8h",
        "stoploss_on_exchange": True,
        "stoploss_order_types": {"limit": "limit", "market": "market"},
        "stoploss_blocks_assets": False,
        "stop_price_type_field": "workingType",
        "stop_price_type_value_mapping": {
            PriceType.LAST: "CONTRACT_PRICE",
            PriceType.MARK: "MARK_PRICE",
        },
        "uses_leverage_tiers": False,
        # fetch_ohlcv accepts price=mark even though ccxt has.fetchMarkOHLCV is unset.
        "exchange_has_overrides": {"fetchMarkOHLCV": True},
    }

    _supported_trading_mode_margin_pairs: list[tuple[TradingMode, MarginMode]] = [
        (TradingMode.SPOT, MarginMode.NONE),
        (TradingMode.FUTURES, MarginMode.ISOLATED),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._coerce_reduce_only(self._api)

    @property
    def _ccxt_config(self) -> dict:
        # ccxt Aster defaults to spot and turns a priced spot market order into quoteOrderQty.
        config: dict = {"options": {"createOrder": {"quoteOrderQty": False}}}
        proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
        if proxy:
            config["httpsProxy"] = proxy
            config["wsProxy"] = proxy
        if self.trading_mode == TradingMode.SPOT:
            config["options"]["defaultType"] = "spot"
        elif self.trading_mode == TradingMode.FUTURES:
            config["options"]["defaultSettle"] = self._config["stake_currency"]
        return deep_merge_dicts(config, super()._ccxt_config)

    def get_tickers(
        self,
        symbols: list[str] | None = None,
        *,
        cached: bool = False,
        market_type: TradingMode | None = None,
    ) -> Tickers:
        tickers = super().get_tickers(symbols=symbols, cached=cached, market_type=market_type)
        if self.trading_mode == TradingMode.FUTURES:
            # Futures tickers omit bid/ask. The book endpoint has them.
            bidsasks = self.fetch_bids_asks(symbols, cached=cached)
            tickers = deep_merge_dicts(bidsasks, tickers, allow_null_overrides=False)
        return tickers

    def _coerce_reduce_only(self, api) -> None:
        """Aster swap orders require reduceOnly as the string 'true' or 'false'."""
        original = api.create_order

        def create_order(*args, **kwargs):
            # create_order() is positional; create_stoploss() passes type= as a keyword.
            params = kwargs.get("params")
            params_in_args = params is None and len(args) >= 6 and isinstance(args[5], dict)
            if params_in_args:
                params = args[5]
            if isinstance(params, dict) and isinstance(params.get("reduceOnly"), bool):
                params = {
                    **params,
                    "reduceOnly": "true" if params["reduceOnly"] else "false",
                }
                if params_in_args:
                    args = (*args[:5], params, *args[6:])
                else:
                    kwargs["params"] = params
            return original(*args, **kwargs)

        api.create_order = create_order

    @retrier
    def additional_exchange_init(self) -> None:
        """
        Switch a live futures account to one-way mode.
        Hedge mode cannot be changed while a position is open.
        """
        try:
            if self._config["dry_run"] or self.trading_mode != TradingMode.FUTURES:
                return
            mode = self._api.fetch_position_mode()
            if not mode.get("hedged"):
                return
            positions = self._api.fetch_positions()
            if any(float(position.get("contracts") or 0) != 0 for position in positions):
                raise OperationalException(
                    "Aster is in hedge mode and still has open positions. "
                    "Close them before starting the bot so it can switch to one-way mode."
                )
            self._log_exchange_response(
                "set_position_mode", self._api.set_position_mode(False)
            )
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Error in additional_exchange_init due to {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    def _get_params(
        self,
        side: BuySell,
        ordertype: str,
        leverage: float,
        reduceOnly: bool,
        time_in_force: str = "GTC",
    ) -> dict:
        params = super()._get_params(side, ordertype, leverage, reduceOnly, time_in_force)
        if self.trading_mode == TradingMode.FUTURES:
            # One-way mode. Hedge mode is switched off during startup.
            params["positionSide"] = "BOTH"
        if params.get("timeInForce") == "PO":
            # Passing PO through overwrites the GTX flag ccxt sets for post-only.
            params.pop("timeInForce")
            params["postOnly"] = True
        return params

    def _get_stop_params(self, side: BuySell, ordertype: str, stop_price: float) -> dict:
        params = super()._get_stop_params(side, ordertype, stop_price)
        if self.trading_mode == TradingMode.FUTURES:
            params["positionSide"] = "BOTH"
        return params

    @retrier
    def set_margin_mode(
        self,
        pair: str,
        margin_mode: MarginMode,
        accept_fail: bool = False,
        params: dict | None = None,
    ):
        """
        Aster returns NoChange when the symbol is already in the requested margin mode.
        That is a successful no-op, not a failed request.
        """
        if self._config["dry_run"] or not self.exchange_has("setMarginMode"):
            return
        try:
            result = self._api.set_margin_mode(margin_mode.value, pair, params or {})
            self._log_exchange_response("set_margin_mode", result)
        except (ccxt.NoChange, ccxt.MarginModeAlreadySet):
            logger.debug("Margin mode for %s is already %s.", pair, margin_mode.value)
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.BadRequest, ccxt.OperationRejected) as e:
            if not accept_fail:
                raise TemporaryError(
                    f"Could not set margin mode due to {e.__class__.__name__}. Message: {e}"
                ) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Could not set margin mode due to {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    def _lev_prep(self, pair: str, leverage: float, side: BuySell, accept_fail: bool = False):
        if self.trading_mode != TradingMode.SPOT:
            # setLeverage rejects anything outside 1..125 and requires an integer.
            self.set_margin_mode(pair, self.margin_mode, accept_fail)
            self._set_leverage(int(leverage), pair, accept_fail)

    @retrier
    def load_leverage_tiers(self) -> dict[str, list[dict]]:
        """
        One private call returns every symbol's leverage brackets.
        Dry-run has no key, so backtests keep the exchangeInfo fallback.
        """
        if self.trading_mode != TradingMode.FUTURES or self._config.get("dry_run"):
            return {}
        try:
            rows = self._api.fapiPrivateGetV3LeverageBracket()
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Could not load leverage tiers due to {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

        tiers: dict[str, list[dict]] = {}
        for row in rows:
            symbol = self._swap_symbol_for_market_id(row.get("symbol"))
            if symbol is None:
                continue
            parsed = []
            for bracket in row.get("brackets") or []:
                parsed.append(
                    {
                        "minNotional": float(bracket["notionalFloor"]),
                        "maxNotional": float(bracket["notionalCap"]),
                        "maintenanceMarginRate": float(bracket["maintMarginRatio"]),
                        "maxLeverage": float(bracket["initialLeverage"]),
                        "info": {"cum": float(bracket.get("cum") or 0)},
                    }
                )
            if parsed:
                tiers[symbol] = parsed
        return tiers

    def _swap_symbol_for_market_id(self, symbol_id: str | None) -> str | None:
        markets = self._api.markets_by_id.get(symbol_id) if symbol_id else None
        if isinstance(markets, dict):
            markets = [markets]
        for market in markets or []:
            if market.get("swap"):
                return market.get("symbol")
        return None

    def get_max_leverage(self, pair: str, stake_amount: float | None) -> float:
        """
        Live mode uses the leverage brackets. Without them, exchangeInfo.requiredMarginPercent
        is only the margin at the deepest tier, so the cap is lower than the first tier allows.
        """
        if self.trading_mode != TradingMode.FUTURES:
            return 1.0
        if pair in self._leverage_tiers:
            return super().get_max_leverage(pair, stake_amount)

        market = self.markets[pair]
        max_leverage = market.get("limits", {}).get("leverage", {}).get("max")
        if max_leverage:
            return float(max_leverage)

        required_margin = (market.get("info") or {}).get("requiredMarginPercent")
        if required_margin is not None and float(required_margin) > 0:
            return 100.0 / float(required_margin)
        return 1.0

    def _maintenance_margin_ratio(self, pair: str) -> float:
        info = self.markets[pair].get("info") or {}
        maintenance = info.get("maintMarginPercent")
        if maintenance is not None and float(maintenance) > 0:
            return float(maintenance) / 100.0
        # Same fallback Hyperliquid documents: half of initial margin at max leverage.
        return 1.0 / self.get_max_leverage(pair, 0.0) / 2.0

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
        Isolated liquidation using the maintenance margin published on the market.
        Bracket maintenance amounts are private, so this ignores the cumulative amount.
        """
        market = self.markets[pair]
        if market.get("inverse"):
            raise OperationalException("Aster inverse contracts are not supported.")
        if self.margin_mode != MarginMode.ISOLATED:
            raise OperationalException(
                "Aster liquidation price is implemented for isolated margin."
            )
        if self.trading_mode != TradingMode.FUTURES:
            raise OperationalException(
                "Freqtrade only supports isolated futures for leverage trading"
            )

        notional = open_rate * amount
        maintenance_ratio, maintenance_amt = self._margin_ratio_and_amount(pair, notional)
        # Same isolated formula as Binance. A DOGE short matched Aster's liquidationPrice
        # when maintenance came from the first leverage bracket and the cumulative amount was 0.
        side = -1 if is_short else 1
        return ((stake_amount + maintenance_amt) - (side * notional)) / (
            (amount * maintenance_ratio) - (side * amount)
        )

    def _margin_ratio_and_amount(self, pair: str, notional: float) -> tuple[float, float]:
        tiers = self._leverage_tiers.get(pair) or []
        for tier in reversed(tiers):
            if notional >= tier["minNotional"]:
                return tier["maintenanceMarginRate"], tier["maintAmt"] or 0.0
        return self._maintenance_margin_ratio(pair), 0.0
