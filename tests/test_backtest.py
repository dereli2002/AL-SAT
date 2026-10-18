import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

from bot.backtest import compare_strategies, run_backtest
from bot.core import PaperBroker
from bot.strategies import STRATEGIES, signals


def config(**overrides):
    result = {
        "initial_cash": "1000",
        "order_quote_amount": "100",
        "buy_fee_pct": "0.1",
        "sell_fee_pct": "0.1",
        "slippage_pct": "0.05",
        "stop_loss_pct": "50",
        "trailing_mode": "price",
        "trailing_distance_pct": "5",
        "profit_giveback_pct": "20",
        "trailing_activation_pct": "20",
        "daily_loss_limit_pct": "99",
        "day_timezone": "Europe/Istanbul",
    }
    result.update(overrides)
    return result


def candles(prices=None, count=60):
    prices = prices or ["100"] * count
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    result = []
    for index, price in enumerate(prices):
        timestamp = start + timedelta(hours=index)
        result.append({
            "open_time": timestamp.isoformat(),
            "close_time": (timestamp + timedelta(minutes=59, seconds=59)).isoformat(),
            "open": str(price),
            "high": str(price),
            "low": str(price),
            "close": str(price),
            "volume": "10",
        })
    return result


class StrategyTests(unittest.TestCase):
    def test_future_candles_cannot_change_an_earlier_signal(self):
        prices = [str(100 + (index % 17) * 2 - (index % 7)) for index in range(85)]
        history = candles(prices)
        for strategy in STRATEGIES:
            with self.subTest(strategy=strategy):
                full = signals(history, strategy)
                for end in (14, 27, 40, 70):
                    self.assertEqual(signals(history[:end], strategy), full[:end])

    def test_candidates_have_documented_behavior(self):
        falling = candles([str(150 - index) for index in range(50)])
        self.assertEqual(signals(falling, "rsi_reversion")[-1], "buy")
        breakout = candles(["100"] * 25 + ["110"])
        self.assertEqual(signals(breakout, "breakout")[-1], "buy")
        self.assertTrue(all(value == "hold" for value in signals(breakout[:26], "ema_cross")))


class BacktestTests(unittest.TestCase):
    def test_signal_fills_at_next_open_and_training_never_trades(self):
        history = candles()
        split = len(history) * 7 // 10
        for key in ("open", "high", "low", "close"):
            history[split][key] = "101"
            history[split + 1][key] = "110"
        generated = ["hold"] * len(history)
        generated[1] = "buy"  # Observation-period signals cannot create positions.
        generated[2] = "sell"
        generated[split - 1] = "buy"
        generated[split] = "sell"
        settings = config()
        expected = PaperBroker(settings)
        expected.buy("101", history[split]["open_time"])
        expected.sell("110", history[split + 1]["open_time"])
        with patch("bot.backtest.signals", return_value=generated):
            result = run_backtest(history, settings)
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(Decimal(result["final_equity"]), Decimal(expected.snapshot("110")["equity"]))
        self.assertEqual(result["train_candles"], 42)
        self.assertEqual(result["test_candles"], 18)
        self.assertEqual(result["test_start"], history[split]["open_time"].replace("+00:00", "Z"))

    def test_flat_price_loses_fees_and_slippage_and_final_position_is_closed(self):
        history = candles()
        generated = ["hold"] * len(history)
        generated[41] = "buy"
        settings = config()
        expected = PaperBroker(settings)
        expected.buy("100", history[42]["open_time"])
        expected.sell("100", history[-1]["close_time"])
        with patch("bot.backtest.signals", return_value=generated):
            result = run_backtest(history, settings)
        self.assertEqual(result["trade_count"], 1)
        self.assertLess(Decimal(result["net_return_pct"]), 0)
        self.assertGreater(Decimal(result["fees_paid"]), 0)
        self.assertEqual(Decimal(result["final_equity"]), Decimal(expected.snapshot("100")["equity"]))
        self.assertEqual(Decimal(result["hold_return_pct"]), Decimal(result["net_return_pct"]))

    def test_adverse_intrabar_path_and_gap_price_are_used(self):
        history = candles(["100"] * 42 + ["110"] * 18)
        history[42].update(open="100", high="120", low="90", close="110")
        generated = ["hold"] * len(history)
        generated[41] = "buy"
        settings = config(stop_loss_pct="20", trailing_activation_pct="1")
        expected = PaperBroker(settings)
        expected.buy("100", history[42]["open_time"])
        expected.sell("90", history[42]["close_time"])
        with patch("bot.backtest.signals", return_value=generated):
            result = run_backtest(history, settings)
        # high→low activates a trailing stop before the low. It fills at the
        # observed 90 with slippage, not at the more favorable old stop level.
        self.assertGreaterEqual(result["intrabar_paths"]["high_low"], 1)
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(Decimal(result["final_equity"]), Decimal(expected.snapshot("110")["equity"]))
        self.assertGreater(Decimal(result["max_drawdown_pct"]), 0)

    def test_comparison_preserves_inputs_and_uses_identical_test_window(self):
        history, settings = candles(), config()
        original = copy.deepcopy((history, settings))
        result = compare_strategies(history, settings)
        self.assertEqual([row["strategy"] for row in result["results"]], list(STRATEGIES))
        self.assertTrue(all(row["test_start"] == result["test_start"] for row in result["results"]))
        self.assertEqual(len({row["hold_return_pct"] for row in result["results"]}), 1)
        self.assertEqual((history, settings), original)
        self.assertTrue(result["notes"])

    def test_invalid_ohlc_or_overlapping_candles_are_rejected(self):
        bad = candles()
        bad[42]["low"] = "101"
        with self.assertRaises(ValueError):
            run_backtest(bad, config())
        bad = candles()
        bad[42]["open_time"] = bad[41]["open_time"]
        with self.assertRaises(ValueError):
            run_backtest(bad, config())


if __name__ == "__main__":
    unittest.main()
