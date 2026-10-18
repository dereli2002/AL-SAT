"""Fee-aware, chronological paper backtests with conservative OHLC handling."""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .core import DEFAULT_CONFIG, PaperBroker
from .strategies import STRATEGIES, signals


NOTES = [
    "İlk %70 yalnızca gösterge ısınması/gözlem, son %30 değerlendirme içindir; parametre optimizasyonu yapılmaz.",
    "Sinyal yalnızca kapanmış mumlardan üretilir ve sonraki mumun açılışında uygulanır.",
    "Mum içinde açılış→düşük→yüksek→kapanış ve açılış→yüksek→düşük→kapanış yolları ayrı simüle edilir; kapanıştaki net tasfiye değeri daha düşük yol seçilir. OHLC gerçek fiyat sırasını göstermez ve bu olası tüm fiyat yollarının en kötüsü değildir.",
    "Stop gerçekleşmeleri gözlenen simülasyon fiyatına kayma uygulanarak hesaplanır; stop eşiğinde dolum garantisi varsayılmaz. Seçilen yolun ara fiyatları azami düşüşe dahildir.",
    "Komisyon ve kayma yapılandırmadaki varsayımlardır; Binance hesabınızın güncel ücretini otomatik doğruladığı anlamına gelmez. Komisyonların karşılık para biriminde ödendiği varsayılır.",
    "Al-tut karşılaştırması ilk test açılışında aynı emir bütçesiyle alır, son kapanışta satar; aynı ücret/kayma ve atıl nakit dahildir, stop kullanmaz.",
    "Son test kapanışında açık pozisyon tasfiye edilir. Geçmiş sonuçlar gelecekteki kârı garanti etmez; en yüksek test getirisi geleceğin en iyi stratejisini belirlemez.",
]


def _decimal(value, label="değer"):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(label + " geçerli bir sayı olmalı.") from exc
    if not result.is_finite():
        raise ValueError(label + " sonlu olmalı.")
    return result


def _time(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ValueError("Mum zamanları ISO 8601 biçiminde olmalı.") from exc
    if parsed.tzinfo is None:
        raise ValueError("Mum zamanları saat dilimi içermeli.")
    return parsed.astimezone(timezone.utc)


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


def _string(value):
    result = format(Decimal(value), "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result or "0"


def _validated(candles):
    if len(candles) < 40:
        raise ValueError("Isınma ve örneklem dışı test için en az 40 kapanmış mum gerekli.")
    normalized = []
    previous_close = None
    for source in candles:
        try:
            start, end = _time(source["open_time"]), _time(source["close_time"])
            values = {key: _decimal(source[key], key) for key in ("open", "high", "low", "close")}
            volume = _decimal(source.get("volume", "0"), "volume")
        except KeyError as exc:
            raise ValueError("Eksik mum alanı: " + str(exc.args[0])) from exc
        if end <= start:
            raise ValueError("Mum kapanışı açılıştan sonra olmalı.")
        if previous_close is not None and start <= previous_close:
            raise ValueError("Mumlar zaman sırasıyla ve çakışmadan gelmeli.")
        if any(value <= 0 for value in values.values()) or volume < 0:
            raise ValueError("Mum fiyatları pozitif, hacim negatif olmayan değerler olmalı.")
        if values["low"] > min(values["open"], values["close"]) or values["high"] < max(values["open"], values["close"]) or values["low"] > values["high"]:
            raise ValueError("Mumun yüksek/düşük fiyatları açılış ve kapanışı kapsamalı.")
        normalized.append({**{key: _string(value) for key, value in values.items()}, "volume": _string(volume), "open_time": _iso(start), "close_time": _iso(end)})
        previous_close = end
    return normalized


def _split(candles):
    return len(candles) * 7 // 10


def _metadata(candles, split):
    return {
        "train_candles": split,
        "test_candles": len(candles) - split,
        "train_start": candles[0]["open_time"],
        "train_end": candles[split - 1]["close_time"],
        "test_start": candles[split]["open_time"],
        "test_end": candles[-1]["close_time"],
    }


def _fee_assumption(config):
    return {key: str(config.get(key, DEFAULT_CONFIG[key])) for key in ("buy_fee_pct", "sell_fee_pct", "slippage_pct")}


def _benchmark(candles, config):
    broker = PaperBroker(config)
    broker.buy(candles[0]["open"], candles[0]["open_time"])
    broker.sell(candles[-1]["close"], candles[-1]["close_time"], reason="benchmark_end")
    return broker.snapshot(candles[-1]["close"])


def _simulate_path(config, state, candle, first, second):
    """Both paths begin with the identical state after the open signal."""
    broker = PaperBroker(config, state=deepcopy(state))
    start, end = _time(candle["open_time"]), _time(candle["close_time"])
    span = end - start
    samples, events = [], []
    for key, timestamp in ((first, start + span / 3), (second, start + span * 2 / 3), ("close", end)):
        events.extend(broker.tick(candle[key], _iso(timestamp)))
        samples.append((timestamp, _decimal(broker.snapshot(candle[key])["equity"])))
    equity = samples[-1][1]
    return equity, broker, samples, events


def _run(candles, config, strategy):
    if strategy not in STRATEGIES:
        raise ValueError("Bilinmeyen strateji: " + str(strategy))
    split = _split(candles)
    sequence = signals(candles, strategy)
    broker = PaperBroker(config)
    initial_cash = _decimal(broker.snapshot(candles[split]["open"])["equity"])
    if initial_cash <= 0:
        raise ValueError("Başlangıç bakiyesi pozitif olmalı.")
    samples = [(candles[split]["open_time"], initial_cash)]
    events = []
    paths = {"low_high": 0, "high_low": 0}
    for index in range(split, len(candles)):
        candle = candles[index]
        events.extend(broker.tick(candle["open"], candle["open_time"], sequence[index - 1]))
        samples.append((candle["open_time"], _decimal(broker.snapshot(candle["open"])["equity"])))
        state = broker.export_state()
        low_high = _simulate_path(config, state, candle, "low", "high")
        high_low = _simulate_path(config, state, candle, "high", "low")
        if low_high[0] <= high_low[0]:
            _, broker, branch_samples, branch_events = low_high
            paths["low_high"] += 1
        else:
            _, broker, branch_samples, branch_events = high_low
            paths["high_low"] += 1
        samples.extend((_iso(timestamp), equity) for timestamp, equity in branch_samples)
        events.extend(branch_events)
    events.extend(broker.sell(candles[-1]["close"], candles[-1]["close_time"], reason="backtest_end"))
    final = broker.snapshot(candles[-1]["close"])
    samples.append((candles[-1]["close_time"], _decimal(final["equity"])))
    peak, max_drawdown = initial_cash, Decimal(0)
    curve = []
    for timestamp, equity in samples:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
        curve.append({"time": timestamp, "equity": _string(equity)})
    pnl = _decimal(final["equity"]) - initial_cash
    trade_count = int(final["trade_count"])
    wins = int(final.get("win_count", 0))
    hold = _benchmark(candles[split:], config)
    return {
        "strategy": strategy,
        "strategy_name": STRATEGIES[strategy]["name"],
        "net_pnl": _string(pnl),
        "net_return_pct": _string(pnl / initial_cash * 100),
        "max_drawdown_pct": _string(max_drawdown),
        "trade_count": trade_count,
        "win_rate_pct": _string(Decimal(wins) / trade_count * 100 if trade_count else Decimal(0)),
        "fees_paid": str(final["fees_paid"]),
        "hold_return_pct": _string((_decimal(hold["equity"]) - initial_cash) / initial_cash * 100),
        "hold_net_pnl": _string(_decimal(hold["equity"]) - initial_cash),
        "hold_fees_paid": str(hold["fees_paid"]),
        "initial_cash": _string(initial_cash),
        "final_equity": str(final["equity"]),
        "fee_assumption": _fee_assumption(config),
        "intrabar_paths": paths,
        "trades": final.get("trades", []),
        "events": events,
        "equity_curve": curve,
        **_metadata(candles, split),
        "notes": list(NOTES),
    }


def run_backtest(candles, config, strategy="ema_cross"):
    """Evaluate one fixed strategy only in the final 30% of validated candles."""
    return _run(_validated(candles), config, strategy)


def compare_strategies(candles, config):
    """Compare the three candidates on identical candles, capital and costs."""
    candles = _validated(candles)
    return {
        "results": [_run(candles, config, strategy) for strategy in STRATEGIES],
        **_metadata(candles, _split(candles)),
        "fee_assumption": _fee_assumption(config),
        "notes": list(NOTES),
    }
