"""One USDT cash ledger and independent, existing PaperBroker protections.

Only fresh quotes can execute orders. Missing quotes block all new entries;
snapshots explicitly mark cached valuations. For risk checks, an unquoted
position contributes its entry cost (unknown P&L is zero), never a stale quote.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from .core import PaperBroker, _decimal, _json, _timestamp, normalize_config


PORTFOLIO_DEFAULTS = {"max_open_positions": 5}
FILTER_KEYS = ("step_size", "min_qty", "min_notional", "max_qty", "max_notional")
ZERO = Decimal(0)
HUNDRED = Decimal(100)


def _symbol(value):
    prefix = value[:-4] if isinstance(value, str) and value.endswith("USDT") else ""
    if not 1 <= len(prefix) <= 40 or not prefix.isalnum():
        raise ValueError("Geçerli bir Binance Spot USDT sembolü gerekli.")
    return value


def _prices(values):
    if not isinstance(values, dict):
        raise ValueError("Fiyatlar sembol/fiyat nesnesi olmalı.")
    result = {}
    for symbol, value in values.items():
        _symbol(symbol)
        price = _decimal(value, symbol)
        if price <= 0:
            raise ValueError(f"{symbol}: fiyat sıfırdan büyük olmalı.")
        result[symbol] = price
    return result


class PaperPortfolio:
    """Serializable multi-asset paper account; no network or real orders."""

    def __init__(self, config=None, state=None):
        self.config = normalize_config({**PORTFOLIO_DEFAULTS, **(config or {})})
        maximum = _decimal(self.config["max_open_positions"], "max_open_positions")
        if not 1 <= maximum <= 100 or maximum != maximum.to_integral_value():
            raise ValueError("max_open_positions: 1 ile 100 arasında tam sayı olmalı.")
        self.config["max_open_positions"] = int(maximum)
        self._tz = ZoneInfo(self.config["day_timezone"])
        initial = Decimal(self.config["initial_cash"])
        self._state = dict(
            version=2, initial_cash=initial, cash=initial, realized_pnl=ZERO,
            fees_paid=ZERO, brokers={}, filters={}, trades=[], last_prices={},
            last_timestamp=None, day=None, day_start_equity=None,
            daily_halted=False, halt_day=None, pending_risk_symbols=[], next_trade_id=1,
        )
        if state is not None:
            self._restore(state)

    @property
    def position_symbols(self):
        return tuple(self._state["brokers"])

    @property
    def symbols(self):
        return self.position_symbols

    def _sub_config(self, symbol, state=None, container=None):
        container = container if container is not None else self._state
        result = {**self.config, **{key: "0" for key in FILTER_KEYS}}
        result.update(container["filters"].get(symbol, {}))
        result["daily_loss_limit_pct"] = "0"
        if state is not None:
            result["initial_cash"] = state["initial_cash"]
        return result

    def _broker(self, symbol):
        state = self._state["brokers"][symbol]
        return PaperBroker(self._sub_config(symbol, state), state)

    def _validated_filters(self, filters):
        if filters is None:
            return {}
        if not isinstance(filters, dict):
            raise ValueError("Filtreler sembol/filtre nesnesi olmalı.")
        result = {}
        for symbol, values in filters.items():
            _symbol(symbol)
            if not isinstance(values, dict) or set(values) - set(FILTER_KEYS):
                raise ValueError(f"{symbol}: geçersiz miktar/tutar filtresi.")
            normalized = normalize_config({**self.config, **{key: "0" for key in FILTER_KEYS}, **values})
            result[symbol] = {key: normalized[key] for key in FILTER_KEYS}
        return result

    def _restore(self, state):
        if not isinstance(state, dict) or state.get("version") != 2:
            raise ValueError("Desteklenmeyen portföy kayıt sürümü.")
        candidate = deepcopy(state)
        for key in ("initial_cash", "cash", "realized_pnl", "fees_paid"):
            candidate[key] = _decimal(candidate.get(key), key)
        if candidate["initial_cash"] != Decimal(self.config["initial_cash"]):
            raise ValueError("Başlangıç bakiyesini değiştirmek için hesabı sıfırlayın.")
        if candidate["cash"] < 0 or candidate["fees_paid"] < 0:
            raise ValueError("Kayıtlı bakiye/ücret geçersiz.")
        if not isinstance(candidate.get("brokers"), dict):
            raise ValueError("Kayıtlı pozisyonlar geçersiz.")
        candidate["filters"] = self._validated_filters(candidate.get("filters", {}))
        candidate["last_prices"] = _prices(candidate.get("last_prices", {}))
        candidate["last_timestamp"] = (
            _timestamp(candidate["last_timestamp"]) if candidate.get("last_timestamp") else None
        )
        if candidate.get("day_start_equity") is not None:
            candidate["day_start_equity"] = _decimal(candidate["day_start_equity"], "day_start_equity")
        for field in ("day", "halt_day"):
            value = candidate.get(field)
            if value is not None:
                from datetime import date
                try:
                    date.fromisoformat(value)
                except (ValueError, TypeError) as exc:
                    raise ValueError("Kayıtlı gün geçersiz.") from exc
        if not isinstance(candidate.get("daily_halted"), bool):
            raise ValueError("Kayıtlı günlük limit geçersiz.")
        pending = candidate.get("pending_risk_symbols", [])
        if not isinstance(pending, list) or set(pending) - set(candidate["brokers"]):
            raise ValueError("Kayıtlı bekleyen risk pozisyonları geçersiz.")
        candidate["pending_risk_symbols"] = list(dict.fromkeys(pending))
        next_id = candidate.get("next_trade_id")
        if isinstance(next_id, bool) or not isinstance(next_id, int) or next_id < 1:
            raise ValueError("Kayıtlı işlem sayacı geçersiz.")
        if not isinstance(candidate.get("trades"), list):
            raise ValueError("Kayıtlı işlemler geçersiz.")
        for trade in candidate["trades"]:
            if not isinstance(trade, dict):
                raise ValueError("Kayıtlı işlem geçersiz.")
            _symbol(trade.get("symbol"))
            _decimal(trade.get("pnl_quote"), "pnl_quote")
        for symbol, child_state in candidate["brokers"].items():
            _symbol(symbol)
            child = PaperBroker(self._sub_config(symbol, child_state, candidate), child_state)
            saved = child.export_state()
            if saved["position"] is None or Decimal(saved["cash"]) != 0 or Decimal(saved["realized_pnl"]) != 0:
                raise ValueError("Pozisyon alt hesabı yalnızca açık işlem içermeli.")
            candidate["brokers"][symbol] = saved
        self._state = candidate

    @classmethod
    def from_legacy(cls, config, legacy_broker_state, symbol):
        """Migrate the single-position account without changing money or peaks."""
        _symbol(symbol)
        legacy = PaperBroker(config, legacy_broker_state).export_state()
        result = cls(config)
        state = result.export_state()
        for key in ("initial_cash", "cash", "realized_pnl", "fees_paid", "trades",
                    "last_timestamp", "day", "day_start_equity", "daily_halted", "next_trade_id"):
            state[key] = deepcopy(legacy[key])
        state["trades"] = [{**trade, "symbol": symbol} for trade in legacy["trades"]]
        state["halt_day"] = legacy["day"] if legacy["daily_halted"] else None
        position = legacy["position"]
        if legacy["last_price"] is not None:
            state["last_prices"][symbol] = legacy["last_price"]
        if position is not None:
            child = deepcopy(legacy)
            child.update(initial_cash=position["entry_cost"], cash="0", realized_pnl="0",
                         fees_paid=position["entry_fee"], trades=[], daily_halted=False)
            state["brokers"][symbol] = child
            state["filters"][symbol] = {key: result.config[key] for key in FILTER_KEYS}
            state["last_prices"].setdefault(symbol, position["entry_price"])
            if legacy["daily_halted"]:
                state["pending_risk_symbols"] = [symbol]
        return cls(config, state)

    def _run(self, prices, timestamp, action):
        fresh = _prices(prices)
        instant = _timestamp(timestamp)
        previous = self._state["last_timestamp"]
        if previous is not None and instant < previous:
            raise ValueError("Geçmiş zamanlı piyasa verisi işlenemez.")
        original = deepcopy(self._state)
        try:
            with localcontext() as context:
                context.prec = 28
                self._state["last_prices"].update(fresh)
                self._roll_day(fresh, instant)
                events = action(fresh, instant)
                self._state["last_timestamp"] = instant
                return _json(events)
        except Exception:
            self._state = original
            raise

    def _risk_equity(self, prices):
        value = self._state["cash"]
        for symbol in self.position_symbols:
            if symbol in prices:
                value += Decimal(self._broker(symbol).snapshot(prices[symbol])["equity"])
            else:
                value += Decimal(self._state["brokers"][symbol]["position"]["entry_cost"])
        return value

    def _roll_day(self, prices, instant):
        day = instant.astimezone(self._tz).date().isoformat()
        # Do not anchor a new day's risk baseline to cached market data.
        if self._state["day"] != day and all(symbol in prices for symbol in self.position_symbols):
            self._state["day"] = day
            self._state["day_start_equity"] = self._risk_equity(prices)
            self._state["daily_halted"] = (
                self._state["halt_day"] == day or bool(self._state["pending_risk_symbols"])
            )

    @staticmethod
    def _blocked(symbol, instant, reason):
        return [{"type": "blocked", "symbol": symbol, "timestamp": instant, "reason": reason}]

    def _apply(self, symbol, broker, events):
        tagged = [{**event, "symbol": symbol} for event in events]
        for event in tagged:
            if event["type"] == "sell":
                entry_reason = self._state["brokers"][symbol]["position"].get("entry_reason", "strategy")
                event["entry_reason"] = entry_reason
                self._state["cash"] += Decimal(event["net_quote"])
                self._state["realized_pnl"] += Decimal(event["pnl_quote"])
                self._state["fees_paid"] += Decimal(event["fee_quote"])
                self._state["trades"].append({**broker.export_state()["trades"][-1], "symbol": symbol,
                                             "entry_reason": entry_reason})
        saved = broker.export_state()
        if saved["position"] is None:
            self._state["brokers"].pop(symbol, None)
            self._state["pending_risk_symbols"] = [
                item for item in self._state["pending_risk_symbols"] if item != symbol
            ]
        else:
            self._state["brokers"][symbol] = saved
        return tagged

    def _close(self, symbol, price, instant, reason):
        if symbol not in self._state["brokers"]:
            return self._blocked(symbol, instant, "no_position")
        broker = self._broker(symbol)
        return self._apply(symbol, broker, broker.sell(price, instant, reason=reason))

    def _enforce_daily(self, prices, instant):
        baseline = self._state["day_start_equity"]
        limit = Decimal(self.config["daily_loss_limit_pct"])
        today = instant.astimezone(self._tz).date().isoformat()
        breached = (
            self._state["day"] == today
            and baseline is not None and baseline > 0 and limit > 0
            and self._risk_equity(prices) <= baseline * (1 - limit / HUNDRED)
        )
        if not self._state["daily_halted"] and not breached:
            return []
        newly_halted = not self._state["daily_halted"]
        self._state["daily_halted"] = True
        if newly_halted:
            self._state["halt_day"] = today
        self._state["pending_risk_symbols"] = list(self.position_symbols)
        events = []
        for symbol in self.position_symbols:
            if symbol in prices:
                events.extend(self._close(symbol, prices[symbol], instant, "daily_loss_limit"))
        if newly_halted:
            events.append({"type": "risk", "symbol": None, "timestamp": instant,
                           "reason": "daily_loss_limit", "daily_halted": True})
        return events

    def _buy(self, symbol, price, instant, amount=None, reason="signal"):
        if symbol in self._state["brokers"]:
            return self._blocked(symbol, instant, "position_open")
        if len(self.position_symbols) >= self.config["max_open_positions"]:
            return self._blocked(symbol, instant, "max_open_positions")
        budget = min(amount if amount is not None else Decimal(self.config["order_quote_amount"]),
                     self._state["cash"])
        if budget <= 0:
            return self._blocked(symbol, instant, "insufficient_cash")
        config = self._sub_config(symbol)
        config.update(initial_cash=str(budget), order_quote_amount=str(budget))
        broker = PaperBroker(config)
        events = broker.buy(price, instant)
        if not events or events[0]["type"] != "buy":
            return [{**event, "symbol": symbol} for event in events]
        event = events[0]
        cost = Decimal(event["net_quote"])
        saved = broker.export_state()
        trade_id = self._state["next_trade_id"]
        self._state["next_trade_id"] += 1
        saved.update(initial_cash=str(cost), cash="0")
        saved["position"]["trade_id"] = trade_id
        saved["position"]["entry_reason"] = "manual" if reason == "manual_buy" else "strategy"
        saved["next_trade_id"] = trade_id + 1
        self._state["brokers"][symbol] = saved
        self._state["cash"] -= cost
        self._state["fees_paid"] += Decimal(event["fee_quote"])
        return [{**event, "trade_id": trade_id, "symbol": symbol, "reason": reason}]

    def tick(self, prices, timestamp, signals=None, filters=None):
        return self._tick(prices, timestamp, signals, filters)

    def buy(self, symbol, prices, timestamp, amount=None, filters=None):
        """Manual entry using the same protective checks as an automatic tick.

        ``prices`` must contain fresh quotes for every currently open position;
        an optional USDT amount applies only to this order, including its fee.
        Filters can be a symbol/filter map or this symbol's plain filter dict.
        """
        _symbol(symbol)
        amount = _decimal(self.config["order_quote_amount"] if amount is None else amount, "amount")
        if amount <= 0:
            raise ValueError("amount: sıfırdan büyük olmalı.")
        if isinstance(filters, dict) and filters and set(filters) <= set(FILTER_KEYS):
            filters = {symbol: filters}
        return self._tick(prices, timestamp, {symbol: "buy"}, filters=filters,
                          entry_amounts={symbol: amount})

    def _tick(self, prices, timestamp, signals=None, filters=None, entry_amounts=None):
        if signals is None:
            signals = {}
        if not isinstance(signals, dict):
            raise ValueError("Sinyaller sembol/sinyal nesnesi olmalı.")
        for symbol, signal in signals.items():
            _symbol(symbol)
            if signal not in ("buy", "sell", "hold"):
                raise ValueError("Sinyal buy, sell veya hold olmalı.")
        checked_filters = self._validated_filters(filters)

        def action(fresh, instant):
            self._state["filters"].update(checked_filters)
            events, protected = [], set()
            pending_exits = False
            for symbol in self.position_symbols:
                if symbol not in fresh:
                    continue
                if symbol in self._state["pending_risk_symbols"]:
                    events.extend(self._close(symbol, fresh[symbol], instant, "daily_loss_limit"))
                    pending_exits = True
                else:
                    broker = self._broker(symbol)
                    events.extend(self._apply(symbol, broker, broker.tick(fresh[symbol], instant)))
                if symbol not in self._state["brokers"]:
                    protected.add(symbol)
            today = instant.astimezone(self._tz).date().isoformat()
            if not self._state["pending_risk_symbols"] and self._state["halt_day"] != today:
                self._state["daily_halted"] = False
            events.extend(self._enforce_daily(fresh, instant))
            protected.update(event["symbol"] for event in events if event["type"] == "sell")
            # Strategy exits release shared cash before any new entries.
            for symbol, signal in signals.items():
                if signal == "sell" and symbol not in protected:
                    if symbol not in fresh:
                        events.extend(self._blocked(symbol, instant, "missing_price"))
                    else:
                        events.extend(self._close(symbol, fresh[symbol], instant, "signal"))
            events.extend(self._enforce_daily(fresh, instant))
            for symbol, signal in signals.items():
                if signal != "buy":
                    continue
                if self._state["daily_halted"] or pending_exits:
                    events.extend(self._blocked(symbol, instant, "daily_loss_limit"))
                elif symbol in protected:
                    events.extend(self._blocked(symbol, instant, "protective_exit"))
                elif symbol not in fresh or any(item not in fresh for item in self.position_symbols):
                    events.extend(self._blocked(symbol, instant, "missing_price"))
                else:
                    amount = entry_amounts.get(symbol) if entry_amounts is not None else None
                    reason = "manual_buy" if entry_amounts is not None and symbol in entry_amounts else "signal"
                    events.extend(self._buy(symbol, fresh[symbol], instant, amount, reason))
                    events.extend(self._enforce_daily(fresh, instant))
            return events

        return self._run(prices, timestamp, action)

    def sell(self, symbol, price, timestamp, reason="manual", prices=None):
        _symbol(symbol)
        if not isinstance(reason, str) or not reason or len(reason) > 100:
            raise ValueError("Geçerli bir satış nedeni gerekli.")
        selected = _prices({symbol: price})
        if prices is None:
            prices = selected
        else:
            prices = _prices(prices)
            if symbol not in prices or prices[symbol] != selected[symbol]:
                raise ValueError("Satış fiyatı güncel sembol/fiyat haritasıyla eşleşmeli.")

        def action(fresh, instant):
            actual_reason = "daily_loss_limit" if symbol in self._state["pending_risk_symbols"] else reason
            events = self._close(symbol, fresh[symbol], instant, actual_reason)
            events.extend(self._enforce_daily(fresh, instant))
            return events

        return self._run(prices, timestamp, action)

    def snapshot(self, prices):
        fresh = _prices(prices)
        with localcontext() as context:
            context.prec = 28
            positions, missing, unrealized, equity = [], [], ZERO, self._state["cash"]
            for symbol in self.position_symbols:
                saved = self._state["brokers"][symbol]
                price = fresh.get(symbol, self._state["last_prices"].get(symbol, saved["position"]["entry_price"]))
                child = self._broker(symbol).snapshot(price)
                profit = Decimal(child["unrealized_pnl"])
                entry_cost = Decimal(child["position"]["entry_cost"])
                position = {**child["position"], "symbol": symbol, "last_price": price,
                            "unrealized_pnl": profit, "unrealized_pnl_pct": profit / entry_cost * HUNDRED,
                            "net_value": child["equity"], "quote_missing": symbol not in fresh}
                positions.append(position)
                unrealized += profit
                equity += Decimal(child["equity"])
                if symbol not in fresh:
                    missing.append(symbol)
            total = self._state["realized_pnl"] + unrealized
            baseline = self._state["day_start_equity"]
            return _json(dict(
                mode="paper", initial_cash=self._state["initial_cash"], cash=self._state["cash"],
                equity=equity, realized_pnl=self._state["realized_pnl"], unrealized_pnl=unrealized,
                total_pnl=total, total_pnl_pct=total / self._state["initial_cash"] * HUNDRED,
                fees_paid=self._state["fees_paid"], trade_count=len(self._state["trades"]),
                win_count=sum(Decimal(trade["pnl_quote"]) > 0 for trade in self._state["trades"]),
                positions=positions, trades=deepcopy(self._state["trades"]),
                last_timestamp=self._state["last_timestamp"], max_open_positions=self.config["max_open_positions"],
                risk=dict(day=self._state["day"], day_timezone=self.config["day_timezone"],
                          day_start_equity=baseline, daily_pnl=equity - baseline if baseline is not None else ZERO,
                          daily_halted=self._state["daily_halted"], missing_symbols=missing,
                          valuation_stale=bool(missing), pending_risk_symbols=list(self._state["pending_risk_symbols"])),
            ))

    def export_state(self):
        return _json(deepcopy(self._state))
