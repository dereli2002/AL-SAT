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
    "hybrid": {
        "name": "Karma strateji",
        "description": "EMA 12/26 trendi, RSI 14 (≤35 al / ≥65 sat) ve önceki 20 tepe / 10 dip kırılımı oy verir. En az iki aynı yönlü oy ve sıfır karşı oy varsa işlem; çelişkide veya ilk 26 mum dolmadan bekle.",
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


def _hybrid_details(candles, closes=None):
    """Causal voting; an opposing vote vetoes an entry or a strategy exit.

    EMA uses its current trend rather than just the one-candle crossing event.
    RSI and breakout votes deliberately keep their own fixed thresholds. Broker
    stop-loss/trailing protections remain independent of a strategy's hold vote.
    """
    closes = closes if closes is not None else [_number(candle["close"]) for candle in candles]
    fast, slow, rsi = _ema(closes, 12), _ema(closes, 26), _rsi(closes)
    highs = [_number(candle["high"]) for candle in candles]
    lows = [_number(candle["low"]) for candle in candles]
    result = []
    for index in range(len(candles)):
        votes = {"ema_trend": 0, "rsi": 0, "breakout": 0}
        ready = index >= 25  # The longest prerequisite is the 26-candle EMA.
        if ready:
            votes["ema_trend"] = 1 if fast[index] > slow[index] else -1 if fast[index] < slow[index] else 0
            votes["rsi"] = 1 if rsi[index] <= 35 else -1 if rsi[index] >= 65 else 0
            if closes[index] > max(highs[index - 20:index]):
                votes["breakout"] = 1
            elif closes[index] < min(lows[index - 10:index]):
                votes["breakout"] = -1
        buy_votes = sum(vote > 0 for vote in votes.values())
        sell_votes = sum(vote < 0 for vote in votes.values())
        decision = "hold"
        if ready and buy_votes >= 2 and sell_votes == 0:
            decision = "buy"
        elif ready and sell_votes >= 2 and buy_votes == 0:
            decision = "sell"
        result.append({"strategy": "hybrid", "signal": decision, "ready": ready,
                       "votes": votes, "buy_votes": buy_votes, "sell_votes": sell_votes})
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
    elif name == "breakout":
        highs = [_number(candle["high"]) for candle in candles]
        lows = [_number(candle["low"]) for candle in candles]
        for index in range(20, len(candles)):
            if closes[index] > max(highs[index - 20:index]):
                result[index] = "buy"
            elif closes[index] < min(lows[index - 10:index]):
                result[index] = "sell"
    else:
        result = [detail["signal"] for detail in _hybrid_details(candles, closes)]
    return result


def signal_details(candles, name="hybrid"):
    """Explain the latest closed-candle signal; votes are -1, 0 or +1.

    Hybrid votes require all indicators to be warm. For an individual strategy,
    the sole vote is that strategy's existing signal, with no altered thresholds.
    """
    if name not in STRATEGIES:
        raise ValueError("Bilinmeyen strateji: " + str(name))
    if name == "hybrid":
        details = _hybrid_details(candles)
        return details[-1] if details else {"strategy": name, "signal": "hold", "ready": False,
                                           "votes": {"ema_trend": 0, "rsi": 0, "breakout": 0},
                                           "buy_votes": 0, "sell_votes": 0}
    sequence = signals(candles, name)
    decision = sequence[-1] if sequence else "hold"
    vote = {"buy": 1, "hold": 0, "sell": -1}[decision]
    minimum = {"ema_cross": 27, "rsi_reversion": 15, "breakout": 21}[name]
    return {"strategy": name, "signal": decision, "ready": len(candles) >= minimum,
            "votes": {name: vote}, "buy_votes": int(vote > 0), "sell_votes": int(vote < 0)}


def signal_for(name, candles):
    """Convenience adapter used by a live paper-trading loop."""
    result = signals(candles, name)
    return result[-1] if result else "hold"
