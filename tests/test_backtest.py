import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

from bot.backtest import compare_strategies, run_backtest
from bot.core import PaperBroker
from bot.strategies import STRATEGIES, _wilder_trend, signal_details, signals


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


def trend_candles(count=70, jump="1.1"):
    """Established, moderate uptrend with a volume-confirmed final breakout."""
    history = candles([str(Decimal(100) + Decimal("0.1") * index) for index in range(count)])
    for candle in history:
        price = Decimal(candle["close"])
        candle.update(high=str(price + 1), low=str(price - 1))
    price = Decimal(history[-1]["close"]) + Decimal(jump)
    history[-1].update(open=str(price), high=str(price + 1), low=str(price - 1),
                       close=str(price), volume="11")
    return history


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

    def test_wilder_atr_adx_seeds_and_flat_trend(self):
        closes = [Decimal(100 + index) for index in range(35)]
        highs, lows = [price + 1 for price in closes], [price - 1 for price in closes]
        atr, adx = _wilder_trend(highs, lows, closes)
        self.assertTrue(all(value is None for value in atr[:14]))
        self.assertEqual(atr[14:], [Decimal(2)] * 21)
        self.assertTrue(all(value is None for value in adx[:27]))
        self.assertEqual(adx[27:], [Decimal(100)] * 8)
        flat = [Decimal(100)] * 35
        flat_atr, flat_adx = _wilder_trend(flat, flat, flat)
        self.assertEqual(flat_atr[14:], [Decimal(0)] * 21)
        self.assertEqual(flat_adx[27:], [Decimal(0)] * 8)

    def test_wilder_smoothing_uses_averaged_directional_movement_and_gap_range(self):
        closes = [Decimal(value) for value in (100, 101, 102, 103, 102, 101, 100)]
        _, adx = _wilder_trend([price + 1 for price in closes],
                               [price - 1 for price in closes], closes, period=3)
        # DX at indices 3,4,5 is 100,100/3,100/9. The ADX seed is
        # their average; the next DX is 1100/27 after another down move.
        self.assertAlmostEqual(adx[5], Decimal(1300) / 27, places=20)
        self.assertAlmostEqual(adx[6], Decimal(3700) / 81, places=20)
        gap = [Decimal(value) for value in (100, 101, 102, 103, 107)]
        atr, _ = _wilder_trend([price + 1 for price in gap],
                              [price - 1 for price in gap], gap, period=3)
        self.assertEqual(atr[3], 2)
        self.assertEqual(atr[4], 3)  # (2*2 + gap true range 5)/3.

    def test_trend_breakout_requires_51_candles_and_all_entry_filters(self):
        history = trend_candles()
        for count in (0, 1, 14, 27, 49, 50):
            with self.subTest(candles=count):
                details = signal_details(history[:count], "trend_breakout")
                self.assertFalse(details["ready"])
                self.assertEqual(details["signal"], "hold")
        first_ready = signal_details(trend_candles(51), "trend_breakout")
        self.assertTrue(first_ready["ready"])
        self.assertEqual(first_ready["signal"], "buy")
        details = signal_details(history, "trend_breakout")
        self.assertEqual(details["signal"], "buy")
        self.assertTrue(all(details["entry_checks"].values()))
        self.assertEqual(Decimal(details["indicators"]["adx14"]), 100)
        self.assertEqual(signals(history, "trend_breakout")[-1], "buy")

    def test_trend_breakout_requires_real_volume_confirmation(self):
        for volume in ("10", "0", None):
            with self.subTest(volume=volume):
                history = trend_candles()
                if volume is None:
                    history[-1].pop("volume")
                else:
                    history[-1]["volume"] = volume
                details = signal_details(history, "trend_breakout")
                self.assertFalse(details["entry_checks"]["volume_confirmation"])
                self.assertEqual(details["signal"], "hold")
        # The exact previous 20 candles include this older spike; replacing
        # that window with the current candle must not lower the threshold.
        history = trend_candles()
        history[-21]["volume"] = "50"
        self.assertEqual(signals(history, "trend_breakout")[-1], "hold")
        history[-21]["volume"] = "10"
        history[-22]["volume"] = "50"  # Outside the reference window.
        self.assertEqual(signals(history, "trend_breakout")[-1], "buy")
        for volume in ("-1", "NaN", "Infinity", "invalid"):
            with self.subTest(invalid_volume=volume):
                history[-1]["volume"] = volume
                with self.assertRaises(ValueError):
                    signals(history, "trend_breakout")

    def test_trend_breakout_rejects_equal_high_and_excessive_atr_extension(self):
        history = trend_candles(jump="0.9")  # Equal to the preceding high.
        details = signal_details(history, "trend_breakout")
        self.assertFalse(details["entry_checks"]["prior_high_breakout"])
        self.assertEqual(details["signal"], "hold")
        history = trend_candles(jump="20")
        details = signal_details(history, "trend_breakout")
        self.assertTrue(details["entry_checks"]["prior_high_breakout"])
        self.assertFalse(details["entry_checks"]["within_atr_extension"])
        self.assertEqual(details["signal"], "hold")

    def test_trend_breakout_blocks_flat_and_weak_new_trends_and_falling_market(self):
        flat = candles(count=70)
        self.assertEqual(signals(flat, "trend_breakout")[-1], "hold")
        # A single spike after a flat period has ADX below 25 despite a
        # rising EMA and a breakout; it must not be mistaken for strong trend.
        weak = candles(["100"] * 69 + ["102"])
        weak[-1]["volume"] = "11"
        details = signal_details(weak, "trend_breakout")
        self.assertFalse(details["entry_checks"]["strong_adx"])
        self.assertEqual(details["signal"], "hold")
        falling = candles([str(200 - index) for index in range(70)])
        self.assertNotIn("buy", signals(falling, "trend_breakout"))
        self.assertEqual(signals(falling, "trend_breakout")[-1], "sell")

    def test_trend_breakout_exit_does_not_require_volume_or_strong_adx(self):
        history = trend_candles()
        history[-1].update(open="100", high="101", low="99", close="100", volume="0")
        details = signal_details(history, "trend_breakout")
        self.assertFalse(details["entry_checks"]["volume_confirmation"])
        self.assertEqual(details["signal"], "sell")

    def test_future_collapse_does_not_change_confirmed_trend_breakout(self):
        observation = trend_candles()
        history = candles([row["close"] for row in observation] + ["1"] * 20)
        for index, row in enumerate(observation):
            history[index].update({key: row[key] for key in ("open", "high", "low", "close", "volume")})
        full = signals(history, "trend_breakout")
        self.assertEqual(full[69], "buy")
        self.assertEqual(full[:70], signals(observation, "trend_breakout"))
        for end in (28, 50, 51, 68, 69):
            with self.subTest(end=end):
                self.assertEqual(full[:end], signals(history[:end], "trend_breakout"))


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
        with patch("bot.backtest.entry_signals", return_value=generated):
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
        with patch("bot.backtest.entry_signals", return_value=generated):
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
        with patch("bot.backtest.entry_signals", return_value=generated):
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
        self.assertEqual(len(result["results"]), len(STRATEGIES))
        self.assertEqual([row["strategy"] for row in result["results"]], list(STRATEGIES))
        self.assertTrue(all(row["test_start"] == result["test_start"] for row in result["results"]))
        self.assertEqual(len({row["hold_return_pct"] for row in result["results"]}), 1)
        self.assertEqual((history, settings), original)
        self.assertTrue(result["notes"])

    def test_real_hybrid_signal_opens_next_bar_and_closes_with_all_costs(self):
        # Only the final observation candle breaks above 20 prior highs while
        # EMA is rising and RSI stays neutral. No indicator or signal is mocked.
        history = candles(["100", "101"] * 34 + ["100", "102"] + ["103"] * 30)
        for row in history:
            row["volume"] = str(Decimal(row["volume"]) * 1000)
            row["quote_volume"] = str(Decimal(row["close"]) * Decimal(row["volume"]))
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

    def test_real_trend_breakout_fills_next_open_and_pays_all_costs(self):
        observation = trend_candles()
        history = candles([row["close"] for row in observation] + ["108"] * 30)
        for index, row in enumerate(observation):
            history[index].update({key: row[key] for key in ("open", "high", "low", "close", "volume")})
        for row in history:
            row["volume"] = str(Decimal(row["volume"]) * 1000)
            row["quote_volume"] = str(Decimal(row["close"]) * Decimal(row["volume"]))
        self.assertEqual(signals(history, "trend_breakout")[69], "buy")
        result = run_backtest(history, config(), "trend_breakout")
        expected = PaperBroker(config())
        expected.buy("108", history[70]["open_time"])
        expected.sell("108", history[-1]["close_time"])
        self.assertEqual(result["strategy"], "trend_breakout")
        self.assertEqual(result["strategy_name"], "ADX filtreli trend kırılımı")
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["trades"][0]["opened_at"], history[70]["open_time"].replace("+00:00", "Z"))
        self.assertEqual(Decimal(result["final_equity"]), Decimal(expected.snapshot("108")["equity"]))
        self.assertGreater(Decimal(result["fees_paid"]), 0)
        self.assertLess(Decimal(result["net_pnl"]), 0)

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
