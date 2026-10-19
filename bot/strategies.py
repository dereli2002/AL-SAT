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
    "trend_breakout": {
        "name": "ADX filtreli trend kırılımı",
        "description": "Alım için kapanış > EMA20 > EMA50, yükselen EMA50, Wilder ADX14 ≥ 25, önceki 20 mum tepesinin kırılması ve önceki 20 mum ortalamasından yüksek pozitif hacim birlikte gerekir. Kapanış EMA20'nin en çok 2 × Wilder ATR14 üstünde olabilir. Kapanış EMA20 altına veya EMA20 EMA50 altına düşerse sat; en az 51 mum gerekir. Stoplar bağımsızdır; kâr garantisi yoktur.",
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


def _wilder_trend(highs, lows, closes, period=14):
    """Return causal Wilder ATR and ADX, seeded with simple averages.

    True range and directional movement start at index 1. The first ATR is at
    index ``period``; ADX first averages ``period`` DX samples at 2*period-1.
    A flat series has zero DX/ADX, not an artificial strong trend.
    """
    atr, adx = [None] * len(closes), [None] * len(closes)
    if len(closes) <= period:
        return atr, adx
    true_ranges, positive_moves, negative_moves = [], [], []
    for index in range(1, len(closes)):
        true_ranges.append(max(highs[index] - lows[index],
                               abs(highs[index] - closes[index - 1]),
                               abs(lows[index] - closes[index - 1])))
        up = highs[index] - highs[index - 1]
        down = lows[index - 1] - lows[index]
        positive_moves.append(up if up > down and up > 0 else Decimal(0))
        negative_moves.append(down if down > up and down > 0 else Decimal(0))
    average_range = sum(true_ranges[:period], Decimal(0)) / period
    positive = sum(positive_moves[:period], Decimal(0)) / period
    negative = sum(negative_moves[:period], Decimal(0)) / period
    initial_dx = []
    for index in range(period, len(closes)):
        if index > period:
            average_range = (average_range * (period - 1) + true_ranges[index - 1]) / period
            positive = (positive * (period - 1) + positive_moves[index - 1]) / period
            negative = (negative * (period - 1) + negative_moves[index - 1]) / period
        atr[index] = average_range
        # The shared ATR denominator cancels in the directional DX ratio.
        dx = (Decimal(100) * abs(positive - negative) / (positive + negative)
              if positive + negative else Decimal(0))
        if index < 2 * period:
            initial_dx.append(dx)
        if index == 2 * period - 1:
            adx[index] = sum(initial_dx, Decimal(0)) / period
        elif index >= 2 * period:
            adx[index] = (adx[index - 1] * (period - 1) + dx) / period
    return atr, adx


def _trend_breakout_details(candles, closes=None):
    """Conjunctive long entry filters; trend exits do not await entry filters."""
    closes = closes if closes is not None else [_number(candle["close"]) for candle in candles]
    highs = [_number(candle["high"]) for candle in candles]
    lows = [_number(candle["low"]) for candle in candles]
    volumes = []
    for candle in candles:
        try:
            volume = Decimal(str(candle.get("volume", "0")))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError("Mum hacmi geçerli bir sayı olmalı.") from exc
        if not volume.is_finite() or volume < 0:
            raise ValueError("Mum hacmi negatif olmayan sonlu bir sayı olmalı.")
        volumes.append(volume)
    fast, slow = _ema(closes, 20), _ema(closes, 50)
    atr, adx = _wilder_trend(highs, lows, closes)
    result = []
    for index in range(len(candles)):
        ready = index >= 50  # EMA50 and its preceding value must both exist.
        decision, checks = "hold", {}
        if ready:
            previous_volume = sum(volumes[index - 20:index], Decimal(0)) / 20
            checks = {
                "uptrend": closes[index] > fast[index] > slow[index],
                "rising_ema50": slow[index] > slow[index - 1],
                "strong_adx": adx[index] >= 25,
                "prior_high_breakout": closes[index] > max(highs[index - 20:index]),
                "volume_confirmation": volumes[index] > 0 and volumes[index] > previous_volume,
                "within_atr_extension": closes[index] - fast[index] <= 2 * atr[index],
            }
            if closes[index] < fast[index] or fast[index] < slow[index]:
                decision = "sell"
            elif all(checks.values()):
                decision = "buy"
        vote = {"buy": 1, "hold": 0, "sell": -1}[decision]
        result.append({"strategy": "trend_breakout", "signal": decision, "ready": ready,
                       "votes": {"trend_breakout": vote}, "buy_votes": int(vote > 0),
                       "sell_votes": int(vote < 0), "entry_checks": checks,
                       "indicators": {"ema20": str(fast[index]) if fast[index] is not None else None,
                                      "ema50": str(slow[index]) if slow[index] is not None else None,
                                      "adx14": str(adx[index]) if adx[index] is not None else None,
                                      "atr14": str(atr[index]) if atr[index] is not None else None}})
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
    elif name == "hybrid":
        result = [detail["signal"] for detail in _hybrid_details(candles, closes)]
    else:
        result = [detail["signal"] for detail in _trend_breakout_details(candles, closes)]
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
    if name == "trend_breakout":
        details = _trend_breakout_details(candles)
        return details[-1] if details else {"strategy": name, "signal": "hold", "ready": False,
                                           "votes": {name: 0}, "buy_votes": 0, "sell_votes": 0,
                                           "entry_checks": {}, "indicators": {}}
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
