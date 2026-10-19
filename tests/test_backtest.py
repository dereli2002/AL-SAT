import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

from bot.backtest import compare_strategies, run_backtest
from bot.core import PaperBroker
from bot.strategies import STRATEGIES, signal_details, signals


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
                for end in (14, 25, 26, 27, 40, 70):
                    self.assertEqual(signals(history[:end], strategy), full[:end])

    def test_candidates_have_documented_behavior(self):
        falling = candles([str(150 - index) for index in range(50)])
        self.assertEqual(signals(falling, "rsi_reversion")[-1], "buy")
        breakout = candles(["100"] * 25 + ["110"])
        self.assertEqual(signals(breakout, "breakout")[-1], "buy")
        self.assertTrue(all(value == "hold" for value in signals(breakout[:26], "ema_cross")))

    def test_hybrid_requires_two_consistent_indicator_votes(self):
        fixtures = (
            (["100", "101"] * 12 + ["100", "102"], "buy", {"ema_trend": 1, "rsi": 0, "breakout": 1}),
            (["100", "99"] * 12 + ["100", "98"], "sell", {"ema_trend": -1, "rsi": 0, "breakout": -1}),
        )
        for prices, expected, votes in fixtures:
            with self.subTest(signal=expected):
                history = candles(prices)
                details = signal_details(history, "hybrid")
                self.assertTrue(details["ready"])
                self.assertEqual(details["votes"], votes)
                self.assertEqual(details["signal"], expected)
                self.assertEqual(signals(history, "hybrid")[-1], expected)

    def test_hybrid_opposing_rsi_vote_vetoes_two_momentum_votes(self):
        for price, votes in (("101", {"ema_trend": 1, "rsi": -1, "breakout": 1}),
                             ("99", {"ema_trend": -1, "rsi": 1, "breakout": -1})):
            with self.subTest(price=price):
                history = candles(["100"] * 25 + [price])
                details = signal_details(history, "hybrid")
                self.assertEqual(details["votes"], votes)
                self.assertEqual(details["signal"], "hold")
                self.assertEqual(signals(history, "hybrid")[-1], "hold")

    def test_hybrid_waits_for_all_indicators_to_warm_up(self):
        history = candles(["100", "101"] * 12 + ["100", "102"])
        for length in (0, 1, 14, 20, 25):
            with self.subTest(candles=length):
                details = signal_details(history[:length], "hybrid")
                self.assertFalse(details["ready"])
                self.assertEqual(details["signal"], "hold")
                self.assertTrue(all(signal == "hold" for signal in signals(history[:length], "hybrid")))
        self.assertEqual(signals(history, "hybrid")[-1], "buy")
        # RSI and breakout can agree before EMA 26 is ready; that must not
        # bypass the shared warm-up requirement.
        early = candles(["1000"] + ["100"] * 23 + ["101"])
        self.assertEqual(signals(early, "hybrid")[-1], "hold")
        self.assertFalse(signal_details(early, "hybrid")["ready"])

    def test_hybrid_single_vote_and_equal_previous_high_do_not_trigger(self):
        history = candles(["100", "101"] * 12 + ["100", "101"])
        details = signal_details(history, "hybrid")
        self.assertEqual(details["votes"], {"ema_trend": 1, "rsi": 0, "breakout": 0})
        self.assertEqual(details["buy_votes"], 1)
        self.assertEqual(details["signal"], "hold")


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
        self.assertEqual(len(result["results"]), 4)
        self.assertEqual([row["strategy"] for row in result["results"]], list(STRATEGIES))
        self.assertTrue(all(row["test_start"] == result["test_start"] for row in result["results"]))
        self.assertEqual(len({row["hold_return_pct"] for row in result["results"]}), 1)
        self.assertEqual((history, settings), original)
        self.assertTrue(result["notes"])

    def test_real_hybrid_signal_opens_next_bar_and_closes_with_all_costs(self):
        # Only the final observation candle breaks above 20 prior highs while
        # EMA is rising and RSI stays neutral. No indicator or signal is mocked.
        history = candles(["100", "101"] * 34 + ["100", "102"] + ["103"] * 30)
        self.assertEqual(signals(history, "hybrid")[69], "buy")
        result = run_backtest(history, config(), "hybrid")
        expected = PaperBroker(config())
        expected.buy("103", history[70]["open_time"])
        expected.sell("103", history[-1]["close_time"])
        self.assertEqual(result["strategy"], "hybrid")
        self.assertEqual(result["strategy_name"], "Karma strateji")
        self.assertEqual(result["train_candles"], 70)
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["trades"][0]["opened_at"], history[70]["open_time"].replace("+00:00", "Z"))
        self.assertEqual(Decimal(result["final_equity"]), Decimal(expected.snapshot("103")["equity"]))
        self.assertLess(Decimal(result["net_pnl"]), 0)  # Flat fills still pay costs.
        self.assertTrue(any("Karma strateji" in note for note in result["notes"]))

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
