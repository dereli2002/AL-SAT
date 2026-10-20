"""Fixed, causal strategy examples for closed Binance Spot candles.

These are candidates to compare, not recommendations or optimized parameters.
Every signal uses only the candle at that index and older candles. A caller must
execute it at the next candle's open, never at the close that created the signal.
"""

from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
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
    "rsi_band": {
        "name": "RSI BANT",
        "description": "Bollinger (20, 2) alt bandına aynı mumda temas ve Stochastic RSI (14, 14, 3, 3) %K değerinin 20 üzerine geçişinde al; üst banda temas ve %K'nın 80 altına geçişinde sat. K/D kesişimi ayrıca aranmaz. Spot satış açık pozisyonu kapatır; stoplar bağımsızdır.",
    },
    "vwap_ema": {
        "name": "WAP EMA",
        "description": "Kapanış EMA9 ve günlük VWAP'ın birlikte üstüne geçince al, birlikte altına geçince sat. VWAP HLC3 × hacim ile UTC 00.00'da (Türkiye 03.00) sıfırlanır. Eksik gün, hacimsiz mum ve günün ilk mumunda bekler. 1 veya 5 dakikalık kullanım içindir; Spot satış pozisyonu kapatır, stoplar bağımsızdır.",
    },
    "scalping": {
        "name": "SCALPİNG",
        "description": "Kapanış EMA20 üzerinde ve CMF20 > 0 iken Stochastic RSI (14, 14, 3, 3) %K, %D'yi ikisi de 20 altında yukarı keserse al. Ayrı strateji satış sinyali yoktur; çıkışlar yapılandırılmış sabit stop, kâr takibi veya manuel SAT ile yapılır.",
    },
}

MIN_QUOTE_VOLUME_24H = Decimal("10000000")


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


def _sma(values, period):
    """An undefined input invalidates the whole smoothing window."""
    result = [None] * len(values)
    for index in range(period - 1, len(values)):
        window = values[index - period + 1:index + 1]
        if all(value is not None for value in window):
            result[index] = sum(window, Decimal(0)) / period
    return result


def _bollinger(values, period=20, multiplier=2):
    middle = _sma(values, period)
    upper, lower = [None] * len(values), [None] * len(values)
    for index, average in enumerate(middle):
        if average is not None:
            window = values[index - period + 1:index + 1]
            deviation = (sum(((value - average) ** 2 for value in window), Decimal(0)) / period).sqrt()
            upper[index], lower[index] = average + multiplier * deviation, average - multiplier * deviation
    return middle, upper, lower


def _stoch_rsi(values, rsi_period=14, stoch_period=14, smooth_k=3, smooth_d=3):
    rsi = _rsi(values, rsi_period)
    raw = [None] * len(values)
    for index in range(rsi_period + stoch_period - 1, len(values)):
        window = rsi[index - stoch_period + 1:index + 1]
        bottom, top = min(window), max(window)
        # A zero RSI range is undefined, not a fabricated oversold reading.
        if top > bottom:
            raw[index] = 100 * (rsi[index] - bottom) / (top - bottom)
    k = _sma(raw, smooth_k)
    return k, _sma(k, smooth_d)


def _volumes(candles):
    result = []
    for candle in candles:
        try:
            volume = Decimal(str(candle.get("volume", "0")))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError("Mum hacmi geçerli bir sayı olmalı.") from exc
        if not volume.is_finite() or volume < 0:
            raise ValueError("Mum hacmi negatif olmayan sonlu bir sayı olmalı.")
        result.append(volume)
    return result


def _cmf(candles, period=20):
    """Chaikin money flow measures volume-weighted closing pressure."""
    volumes = _volumes(candles)
    flow = []
    for candle, volume in zip(candles, volumes):
        high, low, close = (_number(candle[key]) for key in ("high", "low", "close"))
        if high < low or not low <= close <= high:
            raise ValueError("CMF mumunun yüksek/düşük fiyatı kapanışı kapsamalı.")
        multiplier = ((close - low) - (high - close)) / (high - low) if high > low else Decimal(0)
        flow.append(multiplier * volume)
    result = [None] * len(candles)
    for index in range(period - 1, len(candles)):
        total_volume = sum(volumes[index - period + 1:index + 1], Decimal(0))
        if total_volume > 0:
            result[index] = sum(flow[index - period + 1:index + 1], Decimal(0)) / total_volume
    return result


def _candle_time(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ValueError("Saat dilimi içeren mum zamanları gerekli.") from exc
    if parsed.tzinfo is None:
        raise ValueError("Saat dilimi içeren mum zamanları gerekli.")
    return parsed.astimezone(timezone.utc)


def _rolling_quote_volume(candles):
    """Actual USDT quote volume over a complete, contiguous 24h of bars.

    No price-times-base-volume estimate is substituted for a missing exchange
    quote-volume field. The window ends at the signal candle, never in future.
    """
    result, window = [], deque()
    total, missing = Decimal(0), 0
    previous_start, previous_end, previous_duration = None, None, None
    day = timedelta(days=1)
    for candle in candles:
        start, end = _candle_time(candle["open_time"]), _candle_time(candle["close_time"])
        if end <= start or (previous_end is not None and start <= previous_end):
            raise ValueError("Hacim mumları zaman sırasıyla ve çakışmadan gelmeli.")
        duration = timedelta(seconds=int((end - start).total_seconds()) + 1)
        if previous_start is not None and (start != previous_start + previous_duration or duration != previous_duration):
            window.clear()
            total, missing = Decimal(0), 0
        raw = candle.get("quote_volume")
        volume = None
        if raw is not None:
            try:
                volume = Decimal(str(raw))
            except (InvalidOperation, ValueError, TypeError):
                volume = None
            if volume is not None and (not volume.is_finite() or volume < 0):
                volume = None
        window.append((start, volume))
        total += volume if volume is not None else Decimal(0)
        missing += volume is None
        boundary = start + duration - day
        while window and window[0][0] < boundary:
            _, oldest = window.popleft()
            total -= oldest if oldest is not None else Decimal(0)
            missing -= oldest is None
        complete = (bool(window) and window[0][0] == boundary and not missing
                    and day.total_seconds() % duration.total_seconds() == 0
                    and len(window) == day // duration)
        result.append(total if complete else None)
        previous_start, previous_end, previous_duration = start, end, duration
    return result


def _session_vwap(candles):
    """UTC daily HLC3 VWAP; an incomplete/gapped session stays unready.

    The initial partial day is never silently treated as an anchored session.
    Session completeness recovers only at a genuine midnight opening candle.
    """
    values, ready, sessions = [], [], []
    total_value, total_volume = Decimal(0), Decimal(0)
    previous_start, previous_end, previous_duration, session = None, None, None, None
    complete = False
    for candle, volume in zip(candles, _volumes(candles)):
        start, end = _candle_time(candle["open_time"]), _candle_time(candle["close_time"])
        if end <= start or (previous_end is not None and start <= previous_end):
            raise ValueError("VWAP mumları zaman sırasıyla ve çakışmadan gelmeli.")
        duration = timedelta(seconds=int((end - start).total_seconds()) + 1)
        day = start.date()
        if day != session:
            total_value, total_volume = Decimal(0), Decimal(0)
            complete = start == datetime.combine(day, datetime.min.time(), timezone.utc)
            session = day
        elif start != previous_start + previous_duration or duration != previous_duration:
            complete = False
        typical = (_number(candle["high"]) + _number(candle["low"]) + _number(candle["close"])) / 3
        total_value += typical * volume
        total_volume += volume
        values.append(total_value / total_volume if total_volume > 0 else None)
        ready.append(complete and total_volume > 0)
        sessions.append(day)
        previous_start, previous_end, previous_duration = start, end, duration
    return values, ready, sessions


def _single_detail(name, decision, ready, indicators, checks):
    vote = {"buy": 1, "hold": 0, "sell": -1}[decision]
    return {"strategy": name, "signal": decision, "ready": bool(ready),
            "votes": {name: vote}, "buy_votes": int(vote > 0), "sell_votes": int(vote < 0),
            "entry_checks": checks,
            "indicators": {key: str(value) if value is not None else None for key, value in indicators.items()}}


def _rsi_band_details(candles, closes=None):
    closes = closes if closes is not None else [_number(candle["close"]) for candle in candles]
    middle, upper, lower = _bollinger(closes)
    k, d = _stoch_rsi(closes)
    result = []
    for index, candle in enumerate(candles):
        ready = index > 0 and all(value is not None for value in (lower[index], k[index - 1], k[index], d[index]))
        checks, decision = {}, "hold"
        if ready:
            checks = {"lower_band_touch": _number(candle["low"]) <= lower[index],
                      "k_cross_up_20": k[index - 1] <= 20 < k[index],
                      "upper_band_touch": _number(candle["high"]) >= upper[index],
                      "k_cross_down_80": k[index - 1] >= 80 > k[index]}
            if checks["lower_band_touch"] and checks["k_cross_up_20"]:
                decision = "buy"
            elif checks["upper_band_touch"] and checks["k_cross_down_80"]:
                decision = "sell"
        result.append(_single_detail("rsi_band", decision, ready,
                                    {"bb_middle": middle[index], "bb_upper": upper[index], "bb_lower": lower[index],
                                     "stoch_rsi_k": k[index], "stoch_rsi_d": d[index]}, checks))
    return result


def _vwap_ema_details(candles, closes=None):
    closes = closes if closes is not None else [_number(candle["close"]) for candle in candles]
    ema = _ema(closes, 9)
    vwap, session_ready, sessions = _session_vwap(candles)
    volumes = _volumes(candles)
    result = []
    for index in range(len(candles)):
        ready = (index > 0 and ema[index - 1] is not None and session_ready[index] and session_ready[index - 1]
                 and sessions[index] == sessions[index - 1] and volumes[index] > 0)
        decision, checks = "hold", {}
        if ready:
            above = closes[index] > ema[index] and closes[index] > vwap[index]
            below = closes[index] < ema[index] and closes[index] < vwap[index]
            was_above = closes[index - 1] > ema[index - 1] and closes[index - 1] > vwap[index - 1]
            was_below = closes[index - 1] < ema[index - 1] and closes[index - 1] < vwap[index - 1]
            checks = {"above_both": above, "newly_above_both": above and not was_above,
                      "below_both": below, "newly_below_both": below and not was_below}
            if checks["newly_above_both"]:
                decision = "buy"
            elif checks["newly_below_both"]:
                decision = "sell"
        result.append(_single_detail("vwap_ema", decision, ready,
                                    {"ema9": ema[index], "vwap": vwap[index]}, checks))
    return result


def _scalping_details(candles, closes=None):
    closes = closes if closes is not None else [_number(candle["close"]) for candle in candles]
    ema, cmf = _ema(closes, 20), _cmf(candles)
    k, d = _stoch_rsi(closes)
    result = []
    for index in range(len(candles)):
        ready = index > 0 and all(value is not None for value in
                                  (ema[index], cmf[index], k[index - 1], d[index - 1], k[index], d[index]))
        decision, checks = "hold", {}
        if ready:
            checks = {"above_ema20": closes[index] > ema[index], "positive_cmf20": cmf[index] > 0,
                      "k_cross_up_d": k[index - 1] <= d[index - 1] and k[index] > d[index],
                      "cross_below_20": k[index] < 20 and d[index] < 20}
            if all(checks.values()):
                decision = "buy"
        result.append(_single_detail("scalping", decision, ready,
                                    {"ema20": ema[index], "cmf20": cmf[index],
                                     "stoch_rsi_k": k[index], "stoch_rsi_d": d[index]}, checks))
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
    elif name == "trend_breakout":
        result = [detail["signal"] for detail in _trend_breakout_details(candles, closes)]
    elif name == "rsi_band":
        result = [detail["signal"] for detail in _rsi_band_details(candles, closes)]
    elif name == "vwap_ema":
        result = [detail["signal"] for detail in _vwap_ema_details(candles, closes)]
    elif name == "scalping":
        result = [detail["signal"] for detail in _scalping_details(candles, closes)]
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
    if name in ("rsi_band", "vwap_ema", "scalping"):
        details = {"rsi_band": _rsi_band_details, "vwap_ema": _vwap_ema_details,
                   "scalping": _scalping_details}[name](candles)
        return details[-1] if details else _single_detail(name, "hold", False, {}, {})
    sequence = signals(candles, name)
    decision = sequence[-1] if sequence else "hold"
    vote = {"buy": 1, "hold": 0, "sell": -1}[decision]
    minimum = {"ema_cross": 27, "rsi_reversion": 15, "breakout": 21}[name]
    return {"strategy": name, "signal": decision, "ready": len(candles) >= minimum,
            "votes": {name: vote}, "buy_votes": int(vote > 0), "sell_votes": int(vote < 0)}


def signal_for(name, candles, context_candles=(), volume_filter_enabled=True):
    """Convenience adapter used by a live paper-trading loop."""
    result = entry_signals(candles, name, context_candles, volume_filter_enabled)
    return result[-1] if result else "hold"


def entry_signals(candles, name, context_candles=(), volume_filter_enabled=True):
    """Apply the common liquidity gate to automatic buys, never to exits.

    The earlier context supplies the full 24h volume window and session VWAP.
    Other technical indicators keep their original requested warmup window.
    """
    if type(volume_filter_enabled) is not bool:
        raise ValueError("Hacim filtresi açık/kapalı değeri boolean olmalı.")
    context = list(context_candles)
    history = context + list(candles)
    sequence = signals(history, name)[len(context):] if name == "vwap_ema" else signals(candles, name)
    if not volume_filter_enabled:
        return sequence
    volume = _rolling_quote_volume(history)[len(context):]
    return ["hold" if signal == "buy" and (amount is None or amount < MIN_QUOTE_VOLUME_24H) else signal
            for signal, amount in zip(sequence, volume)]


def apply_volume_filter(detail, volume_filter_enabled=True):
    """Reapply only the entry gate, including to cached technical decisions."""
    if type(volume_filter_enabled) is not bool:
        raise ValueError("Hacim filtresi açık/kapalı değeri boolean olmalı.")
    result = deepcopy(detail)
    technical = result.get("technical_signal", result["signal"])
    liquidity = result.setdefault("liquidity", {})
    liquidity["enabled"] = volume_filter_enabled
    eligible = bool(liquidity.get("eligible", False))
    result["technical_signal"] = technical
    result["entry_ready"] = bool(result["ready"] and (not volume_filter_enabled or eligible))
    result["signal"] = "hold" if volume_filter_enabled and technical == "buy" and not eligible else technical
    return result


def entry_signal_details(candles, name, context_candles=(), volume_filter_enabled=True):
    """Explain the technical signal and the shared automatic-entry filter."""
    context = list(context_candles)
    history = context + list(candles)
    detail = signal_details(history if name == "vwap_ema" else candles, name)
    amounts = _rolling_quote_volume(history)
    amount = amounts[-1] if amounts and candles else None
    eligible = amount is not None and amount >= MIN_QUOTE_VOLUME_24H
    technical = detail["signal"]
    detail.update(technical_signal=technical,
                  liquidity={"quote_volume_24h": str(amount) if amount is not None else None,
                             "minimum_quote_volume_24h": str(MIN_QUOTE_VOLUME_24H),
                             "eligible": eligible, "basis": "closed_24h_quote_volume",
                             "evaluated_at": candles[-1]["close_time"] if candles else None})
    return apply_volume_filter(detail, volume_filter_enabled)
