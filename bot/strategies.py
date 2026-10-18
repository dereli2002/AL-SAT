"""Fixed, causal strategy examples for closed Binance Spot candles.

These are candidates to compare, not recommendations or optimized parameters.
Every signal uses only the candle at that index and older candles. A caller must
execute it at the next candle's open, never at the close that created the signal.
"""

from decimal import Decimal, InvalidOperation


STRATEGIES = {
    "ema_cross": {
        "name": "EMA 12/26 kesişimi",
        "description": "12 EMA, 26 EMA'yı yukarı kesince al; aşağı kesince sat.",
    },
    "rsi_reversion": {
        "name": "RSI 14 dönüşü",
        "description": "Wilder RSI 14 ≤ 30 olduğunda al, ≥ 60 olduğunda sat.",
    },
    "breakout": {
        "name": "20 mum kırılımı",
        "description": "Önceki 20 mumun tepesini aşınca al; önceki 10 mumun dibinde sat.",
    },
}


def _number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Mum fiyatı geçerli bir sayı olmalı.") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError("Mum fiyatı pozitif ve sonlu olmalı.")
    return result


def _ema(values, period):
    result = [None] * len(values)
    if len(values) < period:
        return result
    average = sum(values[:period], Decimal(0)) / period
    result[period - 1] = average
    alpha = Decimal(2) / (period + 1)
    for index in range(period, len(values)):
        average += alpha * (values[index] - average)
        result[index] = average
    return result


def _rsi(values, period=14):
    result = [None] * len(values)
    if len(values) <= period:
        return result
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gain = sum((max(change, Decimal(0)) for change in changes[:period]), Decimal(0)) / period
    loss = sum((max(-change, Decimal(0)) for change in changes[:period]), Decimal(0)) / period

    def value():
        if not gain and not loss:
            return Decimal(50)
        if not loss:
            return Decimal(100)
        return Decimal(100) - Decimal(100) / (Decimal(1) + gain / loss)

    result[period] = value()
    for index in range(period + 1, len(values)):
        change = changes[index - 1]
        gain = (gain * (period - 1) + max(change, Decimal(0))) / period
        loss = (loss * (period - 1) + max(-change, Decimal(0))) / period
        result[index] = value()
    return result


def signals(candles, name):
    """Return one buy/sell/hold signal per closed candle, with no future reads."""
    if name not in STRATEGIES:
        raise ValueError("Bilinmeyen strateji: " + str(name))
    closes = [_number(candle["close"]) for candle in candles]
    result = ["hold"] * len(candles)
    if name == "ema_cross":
        fast, slow = _ema(closes, 12), _ema(closes, 26)
        for index in range(26, len(candles)):
            if fast[index - 1] <= slow[index - 1] and fast[index] > slow[index]:
                result[index] = "buy"
            elif fast[index - 1] >= slow[index - 1] and fast[index] < slow[index]:
                result[index] = "sell"
    elif name == "rsi_reversion":
        for index, value in enumerate(_rsi(closes)):
            if value is not None and value <= 30:
                result[index] = "buy"
            elif value is not None and value >= 60:
                result[index] = "sell"
    else:
        highs = [_number(candle["high"]) for candle in candles]
        lows = [_number(candle["low"]) for candle in candles]
        for index in range(20, len(candles)):
            if closes[index] > max(highs[index - 20:index]):
                result[index] = "buy"
            elif closes[index] < min(lows[index - 10:index]):
                result[index] = "sell"
    return result


def signal_for(name, candles):
    """Convenience adapter used by a live paper-trading loop."""
    result = signals(candles, name)
    return result[-1] if result else "hold"
