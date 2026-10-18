"""Decimal tabanlı, tek pozisyonlu Binance Spot deneme aracısı.

Bu modül ağ isteği yapmaz ve gerçek emir göndermez. Para/yüzde değerleri
JSON arayüzünde ondalık dizgelerdir. Ücretler deneme hesabında USDT ödenir.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_CONFIG = {
    "initial_cash": "10000",
    "order_quote_amount": "1000",
    "buy_fee_pct": "0.10",
    "sell_fee_pct": "0.10",
    "slippage_pct": "0.05",
    "stop_loss_pct": "2",
    "trailing_mode": "price",
    "trailing_distance_pct": "1",
    "profit_giveback_pct": "25",
    "trailing_activation_pct": "1",
    "daily_loss_limit_pct": "5",
    "day_timezone": "Europe/Istanbul",
    "step_size": "0",
    "min_qty": "0",
    "min_notional": "0",
    "max_qty": "0",
    "max_notional": "0",
}

_DECIMAL_KEYS = tuple(
    key for key in DEFAULT_CONFIG if key not in {"trailing_mode", "day_timezone"}
)
_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name}: geçerli bir ondalık sayı gerekli.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name}: geçerli bir ondalık sayı gerekli.") from exc
    if not result.is_finite():
        raise ValueError(f"{name}: sonlu bir sayı gerekli.")
    # This generous bound rejects pathological public inputs before arithmetic.
    if result and (result.adjusted() > 36 or result.adjusted() < -36):
        raise ValueError(f"{name}: desteklenen sayı aralığının dışında.")
    return result


def _text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value, "f")


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _text(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


def _timestamp(value: Any) -> datetime:
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise ValueError("timestamp ISO-8601 veya datetime olmalı.")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamp saat dilimi içermeli.")
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("timestamp: saat dilimi içeren ISO-8601 zamanı gerekli.") from exc


def normalize_config(config: dict | None = None) -> dict:
    """Validate known fields, retaining application-specific extra fields."""
    if config is not None and not isinstance(config, dict):
        raise ValueError("Ayarlar bir nesne olmalı.")
    result = deepcopy(DEFAULT_CONFIG)
    result.update(deepcopy(config or {}))
    values = {key: _decimal(result[key], key) for key in _DECIMAL_KEYS}
    for key in ("initial_cash", "order_quote_amount"):
        if values[key] <= 0:
            raise ValueError(f"{key}: sıfırdan büyük olmalı.")
    for key in ("buy_fee_pct", "sell_fee_pct", "slippage_pct", "stop_loss_pct"):
        if not 0 <= values[key] < 100:
            raise ValueError(f"{key}: 0 dahil, 100 hariç aralığında olmalı.")
    for key in ("trailing_distance_pct", "profit_giveback_pct"):
        if not 0 < values[key] < 100:
            raise ValueError(f"{key}: 0 ile 100 arasında olmalı.")
    if not 0 <= values["daily_loss_limit_pct"] <= 100:
        raise ValueError("daily_loss_limit_pct: 0 ile 100 arasında olmalı.")
    if values["trailing_activation_pct"] < 0:
        raise ValueError("trailing_activation_pct: negatif olamaz.")
    for key in ("step_size", "min_qty", "min_notional", "max_qty", "max_notional"):
        if values[key] < 0:
            raise ValueError(f"{key}: negatif olamaz.")
    for low, high in (("min_qty", "max_qty"), ("min_notional", "max_notional")):
        if values[high] and values[low] > values[high]:
            raise ValueError(f"{low}, {high} değerini geçemez.")
    if result["trailing_mode"] not in ("off", "price", "net_profit"):
        raise ValueError("trailing_mode: off, price veya net_profit olmalı.")
    try:
        ZoneInfo(result["day_timezone"])
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("day_timezone: geçerli bir IANA saat dilimi gerekli.") from exc
    result.update({key: _text(value) for key, value in values.items()})
    return result


validate_config = normalize_config


class PaperBroker:
    """A synchronous, serializable spot simulator with protective exits.

    ``tick`` must be called for every available market observation even when
    strategy entries are paused. Stops are evaluated before strategy signals.
    Equal timestamps are allowed for OHLC path simulations; older ones are not.
    ``snapshot`` never updates peaks, clocks, risk baselines or any other state.
    """

    def __init__(self, config: dict | None = None, state: dict | None = None):
        self.config = normalize_config(config)
        self._cfg = {key: Decimal(self.config[key]) for key in _DECIMAL_KEYS}
        self._tz = ZoneInfo(self.config["day_timezone"])
        self._state = {
            "version": 1,
            "initial_cash": self._cfg["initial_cash"],
            "cash": self._cfg["initial_cash"],
            "realized_pnl": _ZERO,
            "fees_paid": _ZERO,
            "position": None,
            "trades": [],
            "last_timestamp": None,
            "last_price": None,
            "day": None,
            "day_start_equity": None,
            "daily_halted": False,
            "next_trade_id": 1,
        }
        if state is not None:
            self._restore(state)

    def _restore(self, state: dict) -> None:
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("Desteklenmeyen deneme hesabı kayıt sürümü.")
        candidate = deepcopy(state)
        for key in ("initial_cash", "cash", "realized_pnl", "fees_paid"):
            if key not in candidate:
                raise ValueError(f"Kayıtta {key} eksik.")
            candidate[key] = _decimal(candidate[key], key)
        if candidate["initial_cash"] != self._cfg["initial_cash"]:
            raise ValueError("Başlangıç bakiyesini değiştirmek için hesabı sıfırlayın.")
        if candidate["cash"] < 0 or candidate["fees_paid"] < 0:
            raise ValueError("Kayıtlı bakiye veya komisyon geçersiz.")
        for key in ("last_price", "day_start_equity"):
            if candidate.get(key) is not None:
                candidate[key] = _decimal(candidate[key], key)
        if candidate.get("last_price") is not None and candidate["last_price"] <= 0:
            raise ValueError("Kayıtlı fiyat geçersiz.")
        candidate["last_timestamp"] = (
            _timestamp(candidate["last_timestamp"])
            if candidate.get("last_timestamp") is not None else None
        )
        if candidate.get("day") is not None:
            try:
                datetime.strptime(candidate["day"], "%Y-%m-%d")
            except (ValueError, TypeError) as exc:
                raise ValueError("Kayıtlı gün geçersiz.") from exc
        if not isinstance(candidate.get("daily_halted"), bool):
            raise ValueError("Kayıtlı günlük risk durumu geçersiz.")
        if not isinstance(candidate.get("trades"), list):
            raise ValueError("Kayıtlı işlem listesi geçersiz.")
        if not isinstance(candidate.get("next_trade_id"), int) or candidate["next_trade_id"] < 1:
            raise ValueError("Kayıtlı işlem numarası geçersiz.")
        position = candidate.get("position")
        if position is not None:
            if not isinstance(position, dict):
                raise ValueError("Kayıtlı pozisyon geçersiz.")
            for key in ("quantity", "entry_price", "entry_cost", "entry_fee", "highest_price", "highest_net_profit"):
                position[key] = _decimal(position.get(key), key)
            for key in ("quantity", "entry_price", "entry_cost", "highest_price"):
                if position[key] <= 0:
                    raise ValueError(f"Kayıtlı {key} sıfırdan büyük olmalı.")
            if position["entry_fee"] < 0 or position["highest_net_profit"] < 0:
                raise ValueError("Kayıtlı pozisyon ücret/kâr bilgisi geçersiz.")
            for key in ("fixed_stop_price", "trailing_stop_price"):
                position[key] = (
                    _decimal(position[key], key) if position.get(key) is not None else None
                )
            position["opened_at"] = _json(_timestamp(position.get("opened_at")))
            if not isinstance(position.get("trailing_active"), bool):
                raise ValueError("Kayıtlı takip durumu geçersiz.")
        self._state = candidate

    def _price(self, price: Any) -> Decimal:
        value = _decimal(price, "price")
        if value <= 0:
            raise ValueError("price: sıfırdan büyük olmalı.")
        return value

    def _inputs(self, price: Any, timestamp: Any) -> tuple[Decimal, datetime]:
        value, instant = self._price(price), _timestamp(timestamp)
        previous = self._state["last_timestamp"]
        if previous is not None and instant < previous:
            raise ValueError("Geçmiş zamanlı piyasa verisi işlenemez.")
        return value, instant

    def _run(self, action, price: Any, timestamp: Any) -> list[dict]:
        value, instant = self._inputs(price, timestamp)
        original = deepcopy(self._state)
        try:
            with localcontext() as context:
                context.prec = 28
                self._roll_day(value, instant)
                events = action(value, instant)
                self._state["last_timestamp"] = instant
                self._state["last_price"] = value
                return _json(events)
        except Exception:
            # Arithmetic errors, invalid inputs and unsupported operations must
            # never leave half an order or partially changed financial state.
            self._state = original
            raise

    def tick(self, price: Any, timestamp: Any, signal: str = "hold") -> list[dict]:
        if signal not in ("hold", "buy", "sell"):
            raise ValueError("signal: hold, buy veya sell olmalı.")

        def action(value: Decimal, instant: datetime) -> list[dict]:
            self._update_position(value)
            protected = self._protection(value, instant)
            if protected is not None:
                return protected
            if signal == "buy":
                return self._buy(value, instant)
            if signal == "sell":
                return self._sell(value, instant, "signal")
            return []

        return self._run(action, price, timestamp)

    def buy(self, price: Any, timestamp: Any) -> list[dict]:
        return self.tick(price, timestamp, signal="buy")

    def sell(self, price: Any, timestamp: Any, reason: str = "manual") -> list[dict]:
        if not isinstance(reason, str) or not reason or len(reason) > 100:
            raise ValueError("Geçerli bir satış nedeni gerekli.")

        def action(value: Decimal, instant: datetime) -> list[dict]:
            self._update_position(value)
            events = self._sell(value, instant, reason)
            # A manual exit still contributes to, and can trip, the daily cap.
            risk = self._daily_protection(value, instant)
            if risk:
                events.extend(risk)
            return events

        return self._run(action, price, timestamp)

    def _net_proceeds(self, price: Decimal) -> Decimal:
        position = self._state["position"]
        if position is None:
            return _ZERO
        fill = price * (_ONE - self._cfg["slippage_pct"] / _HUNDRED)
        return position["quantity"] * fill * (_ONE - self._cfg["sell_fee_pct"] / _HUNDRED)

    def _equity(self, price: Decimal) -> Decimal:
        return self._state["cash"] + self._net_proceeds(price)

    def _roll_day(self, price: Decimal, instant: datetime) -> None:
        day = instant.astimezone(self._tz).date().isoformat()
        if self._state["day"] != day:
            self._state["day"] = day
            # A carried position is marked at the first observation of the day.
            self._state["day_start_equity"] = self._equity(price)
            self._state["daily_halted"] = False

    def _update_position(self, price: Decimal) -> None:
        position = self._state["position"]
        if position is None:
            return
        profit = self._net_proceeds(price) - position["entry_cost"]
        position["highest_price"] = max(position["highest_price"], price)
        position["highest_net_profit"] = max(position["highest_net_profit"], profit)
        mode = self.config["trailing_mode"]
        if mode == "off":
            return
        if profit > 0 and profit / position["entry_cost"] * _HUNDRED >= self._cfg["trailing_activation_pct"]:
            position["trailing_active"] = True
        if not position["trailing_active"]:
            return
        if mode == "price":
            stop = position["highest_price"] * (_ONE - self._cfg["trailing_distance_pct"] / _HUNDRED)
        else:
            protected_profit = position["highest_net_profit"] * (
                _ONE - self._cfg["profit_giveback_pct"] / _HUNDRED
            )
            multiplier = (
                position["quantity"]
                * (_ONE - self._cfg["slippage_pct"] / _HUNDRED)
                * (_ONE - self._cfg["sell_fee_pct"] / _HUNDRED)
            )
            stop = (position["entry_cost"] + protected_profit) / multiplier
        previous = position["trailing_stop_price"]
        position["trailing_stop_price"] = max(previous, stop) if previous is not None else stop

    def _daily_protection(self, price: Decimal, instant: datetime) -> list[dict] | None:
        baseline = self._state["day_start_equity"]
        limit = self._cfg["daily_loss_limit_pct"]
        if self._state["daily_halted"] or baseline is None or baseline <= 0 or limit == 0:
            return None
        if self._equity(price) > baseline * (_ONE - limit / _HUNDRED):
            return None
        self._state["daily_halted"] = True
        events = []
        if self._state["position"] is not None:
            events.extend(self._sell(price, instant, "daily_loss_limit"))
        events.append({
            "type": "risk", "timestamp": instant, "reason": "daily_loss_limit",
            "day": self._state["day"], "daily_halted": True,
        })
        return events

    def _protection(self, price: Decimal, instant: datetime) -> list[dict] | None:
        events = self._daily_protection(price, instant)
        if events is not None:
            return events
        position = self._state["position"]
        if position is None:
            return None
        if position["fixed_stop_price"] is not None and price <= position["fixed_stop_price"]:
            return self._sell(price, instant, "fixed_stop")
        if (
            self.config["trailing_mode"] != "off"
            and position["trailing_active"]
            and position["trailing_stop_price"] is not None
            and price <= position["trailing_stop_price"]
        ):
            reason = "trailing_price" if self.config["trailing_mode"] == "price" else "trailing_profit"
            return self._sell(price, instant, reason)
        return None

    @staticmethod
    def _blocked(instant: datetime, reason: str) -> list[dict]:
        return [{"type": "blocked", "timestamp": instant, "reason": reason}]

    def _buy(self, price: Decimal, instant: datetime) -> list[dict]:
        if self._state["position"] is not None:
            return self._blocked(instant, "position_open")
        if self._state["daily_halted"]:
            return self._blocked(instant, "daily_loss_limit")
        budget = min(self._cfg["order_quote_amount"], self._state["cash"])
        if budget <= 0:
            return self._blocked(instant, "insufficient_cash")
        fill = price * (_ONE + self._cfg["slippage_pct"] / _HUNDRED)
        fee_rate = self._cfg["buy_fee_pct"] / _HUNDRED
        quantity = budget / (fill * (_ONE + fee_rate))
        if self._cfg["max_qty"]:
            quantity = min(quantity, self._cfg["max_qty"])
        if self._cfg["max_notional"]:
            quantity = min(quantity, self._cfg["max_notional"] / fill)
        step = self._cfg["step_size"]
        if step:
            quantity = (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step
        gross = quantity * fill
        fee = gross * fee_rate
        cost = gross + fee
        # Decimal division can round the ideal budget upward by a few ulps.
        # Never create a negative cash balance, including with no lot filter.
        if cost > budget:
            quantity -= step if step else Decimal(1).scaleb(quantity.adjusted() - 27)
            gross, fee = quantity * fill, quantity * fill * fee_rate
            cost = gross + fee
        if quantity <= 0 or quantity < self._cfg["min_qty"]:
            return self._blocked(instant, "min_qty")
        if gross < self._cfg["min_notional"]:
            return self._blocked(instant, "min_notional")
        trade_id = self._state["next_trade_id"]
        self._state["next_trade_id"] += 1
        self._state["cash"] -= cost
        self._state["fees_paid"] += fee
        self._state["position"] = {
            "trade_id": trade_id,
            "quantity": quantity,
            "entry_price": fill,
            "entry_cost": cost,
            "entry_fee": fee,
            "opened_at": _json(instant),
            "highest_price": price,
            "highest_net_profit": _ZERO,
            "trailing_active": False,
            "fixed_stop_price": (
                fill * (_ONE - self._cfg["stop_loss_pct"] / _HUNDRED)
                if self._cfg["stop_loss_pct"] else None
            ),
            "trailing_stop_price": None,
        }
        event = {
            "type": "buy", "timestamp": instant, "reason": "signal",
            "trade_id": trade_id, "market_price": price, "fill_price": fill,
            "quantity": quantity, "gross_quote": gross, "fee_quote": fee,
            "net_quote": cost,
        }
        events = [event]
        # High configured costs can consume a daily risk budget immediately.
        risk = self._daily_protection(price, instant)
        if risk:
            events.extend(risk)
        return events

    def _sell(self, price: Decimal, instant: datetime, reason: str) -> list[dict]:
        position = self._state["position"]
        if position is None:
            return self._blocked(instant, "no_position")
        fill = price * (_ONE - self._cfg["slippage_pct"] / _HUNDRED)
        gross = position["quantity"] * fill
        fee = gross * self._cfg["sell_fee_pct"] / _HUNDRED
        proceeds = gross - fee
        profit = proceeds - position["entry_cost"]
        profit_pct = profit / position["entry_cost"] * _HUNDRED
        self._state["cash"] += proceeds
        self._state["fees_paid"] += fee
        self._state["realized_pnl"] += profit
        self._state["trades"].append(_json({
            "id": position["trade_id"], "opened_at": position["opened_at"],
            "closed_at": instant, "quantity": position["quantity"],
            "entry_price": position["entry_price"], "exit_price": fill,
            "entry_cost": position["entry_cost"], "exit_proceeds": proceeds,
            "entry_fee": position["entry_fee"], "exit_fee": fee,
            "fees": position["entry_fee"] + fee,
            "pnl_quote": profit, "pnl_pct": profit_pct, "reason": reason,
        }))
        self._state["position"] = None
        return [{
            "type": "sell", "timestamp": instant, "reason": reason,
            "trade_id": position["trade_id"], "market_price": price,
            "fill_price": fill, "quantity": position["quantity"],
            "gross_quote": gross, "fee_quote": fee, "net_quote": proceeds,
            "pnl_quote": profit, "pnl_pct": profit_pct,
        }]

    def snapshot(self, price: Any) -> dict:
        value = self._price(price)
        with localcontext() as context:
            context.prec = 28
            position = self._state["position"]
            unrealized = self._net_proceeds(value) - position["entry_cost"] if position else _ZERO
            equity = self._equity(value)
            baseline = self._state["day_start_equity"]
            total = self._state["realized_pnl"] + unrealized
            result = {
                "mode": "paper", "initial_cash": self._state["initial_cash"],
                "cash": self._state["cash"], "equity": equity,
                "realized_pnl": self._state["realized_pnl"],
                "unrealized_pnl": unrealized, "total_pnl": total,
                "total_pnl_pct": total / self._state["initial_cash"] * _HUNDRED,
                "fees_paid": self._state["fees_paid"],
                "trade_count": len(self._state["trades"]),
                "win_count": sum(Decimal(trade["pnl_quote"]) > 0 for trade in self._state["trades"]),
                "last_price": value, "last_timestamp": self._state["last_timestamp"],
                "position": deepcopy(position), "trades": deepcopy(self._state["trades"]),
                "risk": {
                    "day": self._state["day"], "day_timezone": self.config["day_timezone"],
                    "day_start_equity": baseline,
                    "daily_pnl": equity - baseline if baseline is not None else _ZERO,
                    "daily_halted": self._state["daily_halted"],
                },
            }
            return _json(result)

    def export_state(self) -> dict:
        return _json(deepcopy(self._state))
