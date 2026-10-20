"""Indicator references, real entry/exit fixtures and accounting for new rules."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from bot.backtest import run_backtest
from bot.core import PaperBroker
from bot.strategies import (
    STRATEGIES, _bollinger, _cmf, _rolling_quote_volume, _session_vwap, _stoch_rsi,
    entry_signal_details, entry_signals, signal_details, signals,
)


D = Decimal
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def minute_candles(prices, start=START):
    result = []
    for index, value in enumerate(prices):
        opened = start + timedelta(minutes=index)
        result.append({
            "open_time": opened.isoformat(),
            "close_time": (opened + timedelta(minutes=1, milliseconds=-1)).isoformat(),
            "open": str(value), "high": str(value), "low": str(value),
            "close": str(value), "volume": "10",
        })
    return result


def band_fixture(count=80):
    # Prices are independent of the indicator implementation. The two wicks
    # create real band touches without changing the close-based oscillators.
    history = minute_candles([100 + (i % 17) * 2 - (i % 7) for i in range(count)])
    if count > 34:
        history[34]["high"] = "160"
    if count > 37:
        history[37]["low"] = "80"
    return history


def scalping_fixture():
    prices = [
        1000, 1001, 1002, 994, 993, 996, 998, 999, 998, 1000,
        1000, 1005, 1003, 1006, 1003, 1002, 999, 994, 999, 998,
        1001, 1014, 1019, 1016, 1015, 1010, 1023, 1018, 1026, 1026,
        1028, 1031, 1026, 1026, 1027, 1027, 1032, 1040, 1038, 1041,
        1043, 1045, 1048, 1047, 1039, 1042, 1034, 1029, 1042,
    ]
    history = minute_candles(prices)
    for candle in history:
        candle["low"] = str(D(candle["close"]) - 1)
    return history


def liquid_history(history):
    """Supply exact quote-volume fields plus an earlier, disjoint full day."""
    history = deepcopy(history)
    beginning = datetime.fromisoformat(history[0]["open_time"])
    context = minute_candles([history[0]["close"]] * 1440,
                             beginning - timedelta(days=1))
    for candle in context + history:
        candle["quote_volume"] = "10000"
    return history, context


def automatic_buy_fixtures():
    trend = minute_candles([str(D(100) + D("0.1") * index) for index in range(70)])
    for candle in trend:
        price = D(candle["close"])
        candle.update(high=str(price + 1), low=str(price - 1))
    trend[-1].update(open="108", high="109", low="107", close="108", volume="11")
    return {
        "ema_cross": minute_candles([100] * 30 + [90] * 30 + [110]),
        "rsi_reversion": minute_candles([100] * 30 + [90]),
        "breakout": minute_candles([100] * 30 + [110]),
        "hybrid": minute_candles([100, 101] * 12 + [100, 102]),
        "trend_breakout": trend,
        "rsi_band": band_fixture(38),
        "vwap_ema": minute_candles([100] * 9 + [90, 110]),
        "scalping": scalping_fixture(),
    }


def accounting_config():
    return {
        "initial_cash": "1000", "order_quote_amount": "100",
        "buy_fee_pct": "0.1", "sell_fee_pct": "0.1", "slippage_pct": "0.05",
        "stop_loss_pct": "0", "trailing_mode": "off", "daily_loss_limit_pct": "0",
    }


class AddedIndicatorTests(unittest.TestCase):
    def assertDecimalNear(self, actual, expected, tolerance="1e-22"):
        self.assertIsNotNone(actual)
        self.assertLessEqual(abs(D(actual) - D(expected)), D(tolerance))

    def test_bollinger_population_deviation_and_twenty_close_warmup(self):
        # For 1..20 the mean is 10.5 and population variance is 33.25.
        middle, upper, lower = _bollinger([D(i) for i in range(1, 22)])
        self.assertTrue(all(value is None for value in middle[:19]))
        self.assertTrue(all(value is None for value in upper[:19]))
        self.assertTrue(all(value is None for value in lower[:19]))
        self.assertEqual(middle[19], D("10.5"))
        self.assertDecimalNear(upper[19], "22.0325625946707958893541832388178725")
        self.assertDecimalNear(lower[19], "-1.0325625946707958893541832388178725")
        self.assertEqual(middle[20], D("11.5"))

    def test_stochastic_rsi_matches_independent_rational_reference(self):
        # Reference constants were calculated with exact Fraction arithmetic,
        # Wilder RSI14, fourteen RSI observations, then SMA3 and SMA3.
        values = [D(100 + (i % 17) * 2 - (i % 7)) for i in range(60)]
        k, d = _stoch_rsi(values)
        self.assertTrue(all(value is None for value in k[:29]))
        self.assertTrue(all(value is None for value in d[:31]))
        self.assertDecimalNear(k[29], "39.0859466972634304263304193741211145")
        self.assertDecimalNear(k[31], "81.3971206739125178358719675327458631")
        self.assertDecimalNear(d[31], "60.9416559439854117545937265598065596")
        self.assertDecimalNear(k[36], "14.8234836718677471238157851830672274")
        self.assertDecimalNear(k[37], "23.5004896269662904514859810349260499")
        self.assertDecimalNear(d[37], "26.2196813235972869867288997208538139")

    def test_flat_rsi_range_does_not_manufacture_oscillator_crosses(self):
        k, d = _stoch_rsi([D(100)] * 80)
        self.assertTrue(all(value is None for value in k))
        self.assertTrue(all(value is None for value in d))
        history = minute_candles([100] * 80)
        self.assertEqual(signals(history, "rsi_band"), ["hold"] * 80)

    def test_vwap_uses_hlc3_and_volume_not_mean_close(self):
        history = minute_candles([12, 24, 36])
        for candle, high, low, volume in zip(history, (12, 24, 36), (6, 12, 18), (1, 3, 2)):
            candle.update(high=str(high), low=str(low), volume=str(volume))
        # Typical prices are 10, 20 and 30; cumulative values are 10, 17.5, 65/3.
        vwap, ready, sessions = _session_vwap(history)
        self.assertEqual(vwap[:2], [D(10), D("17.5")])
        self.assertDecimalNear(vwap[2], "21.66666666666666666666666666666667")
        self.assertEqual(ready, [True, True, True])
        self.assertEqual(sessions[0], sessions[-1])

    def test_utc_reset_uses_open_time_even_in_another_timezone(self):
        history = minute_candles([100, 200], START + timedelta(hours=23, minutes=59))
        # A partial first session is unavailable. The next day's complete first
        # bar must reset VWAP to 200, irrespective of its ISO timezone notation.
        offset = timezone(timedelta(hours=3))
        for candle in history:
            for key in ("open_time", "close_time"):
                candle[key] = datetime.fromisoformat(candle[key]).astimezone(offset).isoformat()
        vwap, ready, sessions = _session_vwap(history)
        self.assertFalse(ready[0])
        self.assertTrue(ready[1])
        self.assertEqual(vwap[1], D(200))
        self.assertNotEqual(sessions[0], sessions[1])

    def test_missing_session_start_or_gap_blocks_vwap_for_remainder_of_day(self):
        for history in (
            minute_candles([100] * 9 + [90, 110], START + timedelta(minutes=1)),
            minute_candles([100] * 9 + [90, 110])[:5]
            + minute_candles([100] * 9 + [90, 110])[6:],
        ):
            with self.subTest(first=history[0]["open_time"], count=len(history)):
                _, ready, _ = _session_vwap(history)
                self.assertFalse(ready[-1])
                self.assertEqual(signals(history, "vwap_ema")[-1], "hold")

    def test_zero_total_volume_has_no_vwap_and_bad_volume_is_rejected(self):
        history = minute_candles([100] * 12)
        for candle in history:
            candle["volume"] = "0"
        vwap, _, _ = _session_vwap(history)
        self.assertTrue(all(value is None for value in vwap))
        self.assertEqual(signals(history, "vwap_ema"), ["hold"] * len(history))
        for bad in ("-1", "NaN", "Infinity", "not a number"):
            invalid = deepcopy(history)
            invalid[-1]["volume"] = bad
            with self.subTest(volume=bad), self.assertRaises(ValueError):
                signals(invalid, "vwap_ema")

    def test_cmf_weights_closing_location_by_volume_and_handles_flat_bar(self):
        history = minute_candles([11, 18, 30])
        history[0].update(high="12", low="8", volume="2")
        history[1].update(high="24", low="16", volume="3")
        history[2].update(high="30", low="30", volume="5")
        # Flow=(+0.5*2)+(-0.5*3)+(0*5)=-0.5; volume=10.
        self.assertEqual(_cmf(history, period=3), [None, None, D("-0.05")])
        full = scalping_fixture()
        cmf = _cmf(full)
        self.assertTrue(all(value is None for value in cmf[:19]))
        self.assertTrue(all(value == 1 for value in cmf[19:]))
        for candle in history:
            candle["volume"] = "0"
        self.assertIsNone(_cmf(history, period=3)[-1])

    def test_cmf_rejects_invalid_volume_and_impossible_price_range(self):
        for volume in ("-1", "NaN", "Infinity", "bad"):
            history = scalping_fixture()
            history[-1]["volume"] = volume
            with self.subTest(volume=volume), self.assertRaises(ValueError):
                signals(history, "scalping")
        history = scalping_fixture()
        history[-1]["high"] = "1"
        with self.assertRaises(ValueError):
            _cmf(history)


class AddedStrategyTests(unittest.TestCase):
    def test_rsi_band_requires_band_touch_and_threshold_cross_together(self):
        history = band_fixture()
        sequence = signals(history, "rsi_band")
        self.assertEqual(sequence[34], "sell")
        self.assertEqual(sequence[37], "buy")
        self.assertEqual(signal_details(history[:38], "rsi_band")["signal"], "buy")
        # K is still below D at the actual buy: K/D crossing is not an extra rule.
        k, d = _stoch_rsi([D(candle["close"]) for candle in history])
        self.assertLess(k[37], d[37])
        no_touch = deepcopy(history)
        no_touch[34]["high"] = no_touch[34]["close"]
        no_touch[37]["low"] = no_touch[37]["close"]
        self.assertEqual(signals(no_touch, "rsi_band")[34], "hold")
        self.assertEqual(signals(no_touch, "rsi_band")[37], "hold")
        no_cross = deepcopy(history)
        no_cross[35]["high"] = "160"
        no_cross[38]["low"] = "80"
        self.assertEqual(signals(no_cross, "rsi_band")[35], "hold")
        self.assertEqual(signals(no_cross, "rsi_band")[38], "hold")

    def test_rsi_band_accepts_exact_wick_touch(self):
        history = band_fixture()
        _, upper, lower = _bollinger([D(candle["close"]) for candle in history])
        history[34]["high"] = str(upper[34])
        history[37]["low"] = str(lower[37])
        self.assertEqual(signals(history, "rsi_band")[34], "sell")
        self.assertEqual(signals(history, "rsi_band")[37], "buy")

    def test_wap_ema_crosses_both_lines_once_in_each_direction(self):
        history = minute_candles([100] * 9 + [90, 110, 120, 80, 70])
        sequence = signals(history, "vwap_ema")
        self.assertEqual(sequence[10:14], ["buy", "hold", "sell", "hold"])
        self.assertEqual(signal_details(history[:11], "vwap_ema")["signal"], "buy")
        zero_volume = deepcopy(history)
        zero_volume[10]["volume"] = "0"
        self.assertEqual(signals(zero_volume, "vwap_ema")[10], "hold")
        self.assertEqual(signals(minute_candles([100] * 20), "vwap_ema"), ["hold"] * 20)

    def test_wap_ema_does_not_trade_a_cross_caused_by_session_reset(self):
        history = minute_candles([100] * 1439 + [90, 110])
        history[-1]["low"] = "80"  # First new-day VWAP=100, below close=110.
        self.assertEqual(signals(history, "vwap_ema")[-1], "hold")

    def test_new_strategies_are_causal_and_do_not_mutate_candles(self):
        history = band_fixture()
        original = deepcopy(history)
        for strategy in ("rsi_band", "vwap_ema", "scalping"):
            full = signals(history, strategy)
            for length in (0, 1, 9, 29, 31, 35, 38, 60):
                with self.subTest(strategy=strategy, count=length):
                    self.assertEqual(signals(history[:length], strategy), full[:length])
        self.assertEqual(history, original)

    def test_real_rsi_band_signal_fills_next_open_and_pays_roundtrip_costs(self):
        history = band_fixture(40)
        for index in (38, 39):
            for key in ("open", "high", "low", "close"):
                history[index][key] = "105"
        self.assertEqual(signals(history, "rsi_band")[37], "buy")
        settings = accounting_config()
        history, context = liquid_history(history)
        result = run_backtest(history, settings, "rsi_band", context_candles=context)
        expected = PaperBroker(settings)
        expected.buy("105", history[38]["open_time"])
        expected.sell("105", history[39]["close_time"])
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["trades"][0]["opened_at"], history[38]["open_time"].replace("+00:00", "Z"))
        self.assertEqual(D(result["final_equity"]), D(expected.snapshot("105")["equity"]))
        self.assertGreater(D(result["fees_paid"]), 0)
        self.assertLess(D(result["net_pnl"]), 0)

    def test_scalping_real_oversold_k_d_cross_with_positive_cmf_and_uptrend(self):
        history = scalping_fixture()
        details = signal_details(history, "scalping")
        self.assertEqual(details["signal"], "buy")
        self.assertTrue(details["ready"])
        values = details["indicators"]
        self.assertGreater(D(history[-1]["close"]), D(values["ema20"]))
        self.assertGreater(D(values["cmf20"]), 0)
        self.assertLess(D(values["stoch_rsi_d"]), D(values["stoch_rsi_k"]))
        self.assertLess(D(values["stoch_rsi_k"]), 20)
        # Independently checked values from this fixed price sequence.
        self.assertAlmostEqual(float(values["stoch_rsi_k"]), 13.316363351497545, places=10)
        self.assertAlmostEqual(float(values["stoch_rsi_d"]), 8.281534148842608, places=10)
        self.assertTrue(all(value == "hold" for value in signals(history[:32], "scalping")))
        self.assertNotIn("sell", signals(history, "scalping"))

    def test_scalping_rejects_each_failed_filter_without_extra_sell_rule(self):
        # CMF=0 and CMF=-1 independently veto an otherwise valid oscillator and trend.
        for flow in (0, -1):
            history = scalping_fixture()
            for candle in history:
                price = D(candle["close"])
                candle["high"] = str(price + 1)
                candle["low"] = str(price - 1 if flow == 0 else price)
            with self.subTest(cmf=flow):
                details = signal_details(history, "scalping")
                self.assertEqual(details["signal"], "hold")
                self.assertTrue(details["entry_checks"]["above_ema20"])
                self.assertTrue(details["entry_checks"]["k_cross_up_d"])
                self.assertTrue(details["entry_checks"]["cross_below_20"])
        # A higher old price regime keeps EMA20 above the final close while
        # preserving the actual K/D crossing below 20 and positive CMF.
        history = scalping_fixture()
        for candle in history[:40]:
            for key in ("open", "high", "low", "close"):
                candle[key] = str(D(candle[key]) + 50)
        details = signal_details(history, "scalping")
        self.assertEqual(details["signal"], "hold")
        self.assertTrue(details["entry_checks"]["positive_cmf20"])
        self.assertTrue(details["entry_checks"]["k_cross_up_d"])
        self.assertTrue(details["entry_checks"]["cross_below_20"])
        self.assertFalse(details["entry_checks"]["above_ema20"])
        # A stronger last close raises K above 20: the crossing alone is insufficient.
        history = scalping_fixture()
        history[-1].update(open="1060", high="1060", low="1059", close="1060")
        details = signal_details(history, "scalping")
        self.assertEqual(details["signal"], "hold")
        self.assertTrue(details["entry_checks"]["above_ema20"])
        self.assertTrue(details["entry_checks"]["positive_cmf20"])
        self.assertTrue(details["entry_checks"]["k_cross_up_d"])
        self.assertFalse(details["entry_checks"]["cross_below_20"])
        # With all other filters true, a pre-cross sample must still wait.
        history = scalping_fixture()[:-1]
        self.assertEqual(signals(history, "scalping")[-1], "hold")

    def test_scalping_next_open_accounting_and_no_automatic_strategy_sell(self):
        history = scalping_fixture()
        continuation = minute_candles([1042] * 21, START + timedelta(minutes=49))
        history.extend(continuation)
        self.assertEqual(signals(history, "scalping")[48], "buy")
        self.assertNotIn("sell", signals(history, "scalping"))
        settings = accounting_config()
        history, context = liquid_history(history)
        result = run_backtest(history, settings, "scalping", context_candles=context)
        expected = PaperBroker(settings)
        expected.buy("1042", history[49]["open_time"])
        expected.sell("1042", history[-1]["close_time"])
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["trades"][0]["opened_at"], history[49]["open_time"].replace("+00:00", "Z"))
        self.assertEqual(D(result["final_equity"]), D(expected.snapshot("1042")["equity"]))
        self.assertGreater(D(result["fees_paid"]), 0)
        self.assertLess(D(result["net_pnl"]), 0)

    def test_real_wap_ema_signal_fills_next_open_and_closes_on_next_sell(self):
        history = minute_candles([100] * 41 + [110] + [100] * 18)
        self.assertEqual(signals(history, "vwap_ema")[41:43], ["buy", "sell"])
        settings = accounting_config()
        history, context = liquid_history(history)
        result = run_backtest(history, settings, "vwap_ema", context_candles=context)
        expected = PaperBroker(settings)
        expected.buy("100", history[42]["open_time"])
        expected.sell("100", history[43]["open_time"])
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["trades"][0]["opened_at"], history[42]["open_time"].replace("+00:00", "Z"))
        self.assertEqual(D(result["final_equity"]), D(expected.snapshot("100")["equity"]))
        self.assertGreater(D(result["fees_paid"]), 0)
        self.assertLess(D(result["net_pnl"]), 0)


class AutomaticLiquidityTests(unittest.TestCase):
    def test_rolling_volume_is_exactly_twenty_four_hours_and_discards_oldest(self):
        history = minute_candles([100] * 1441)
        for index, candle in enumerate(history):
            candle["quote_volume"] = str(index + 1)
        amounts = _rolling_quote_volume(history)
        self.assertTrue(all(value is None for value in amounts[:1439]))
        self.assertEqual(amounts[1439], D(1037520))  # Sum 1..1440.
        self.assertEqual(amounts[1440], D(1038960))  # Sum 2..1441.
        five_minute = minute_candles([100] * 288)
        for index, candle in enumerate(five_minute):
            candle["open_time"] = (START + timedelta(minutes=5 * index)).isoformat()
            candle["close_time"] = (START + timedelta(minutes=5 * (index + 1), milliseconds=-1)).isoformat()
            candle["quote_volume"] = "1000"
        amounts = _rolling_quote_volume(five_minute)
        self.assertTrue(all(value is None for value in amounts[:-1]))
        self.assertEqual(amounts[-1], D(288000))

    def test_missing_actual_quote_volume_or_time_gap_cannot_be_estimated(self):
        history = minute_candles([100] * 1441)
        for candle in history:
            candle["quote_volume"] = "10000"
            candle["volume"] = "1000000000"  # A base-volume estimate would wrongly pass.
        del history[0]["quote_volume"]
        amounts = _rolling_quote_volume(history)
        self.assertIsNone(amounts[1439])
        self.assertEqual(amounts[1440], D(14400000))
        missing_inside = deepcopy(history)
        del missing_inside[1000]["quote_volume"]
        self.assertIsNone(_rolling_quote_volume(missing_inside)[-1])
        gapped = deepcopy(history[:1000] + history[1001:])
        self.assertIsNone(_rolling_quote_volume(gapped)[-1])
        for candle in history:
            candle["quote_volume"] = "0"
        self.assertEqual(_rolling_quote_volume(history)[-1], 0)

    def test_invalid_quote_volume_blocks_buys_but_preserves_technical_sells(self):
        for invalid in ("NaN", "Infinity", "-1", "bad"):
            for strategy, original in automatic_buy_fixtures().items():
                history, context = liquid_history(original)
                history[-1]["quote_volume"] = invalid
                with self.subTest(strategy=strategy, quote_volume=invalid, signal="buy"):
                    self.assertIsNone(_rolling_quote_volume(context + history)[-1])
                    self.assertEqual(entry_signals(history, strategy, context_candles=context)[-1], "hold")
                    details = entry_signal_details(history, strategy, context_candles=context)
                    self.assertEqual(details["technical_signal"], "buy")
                    self.assertFalse(details["entry_ready"])
                    self.assertFalse(details["liquidity"]["eligible"])
                    self.assertIsNone(details["liquidity"]["quote_volume_24h"])
                raw = signals(original, strategy)
                for index, decision in enumerate(raw):
                    if decision != "sell":
                        continue
                    sell_history, sell_context = liquid_history(original[:index + 1])
                    sell_history[-1]["quote_volume"] = invalid
                    with self.subTest(strategy=strategy, quote_volume=invalid, signal="sell", index=index):
                        self.assertEqual(entry_signals(sell_history, strategy, context_candles=sell_context)[-1], "sell")
                        details = entry_signal_details(sell_history, strategy, context_candles=sell_context)
                        self.assertEqual(details["signal"], "sell")
                        self.assertEqual(details["technical_signal"], "sell")
                        self.assertFalse(details["entry_ready"])
                        self.assertIsNone(details["liquidity"]["quote_volume_24h"])

    def test_every_strategy_buy_requires_at_least_ten_million_usdt(self):
        fixtures = automatic_buy_fixtures()
        self.assertEqual(set(fixtures), set(STRATEGIES))
        for strategy, original in fixtures.items():
            self.assertEqual(signals(original, strategy)[-1], "buy", strategy)
            history, context = liquid_history(original)
            for candle in history + context:
                candle["quote_volume"] = "0"
            for amount, expected in (("9999999.9999", "hold"), ("10000000", "buy"), ("10000000.0001", "buy")):
                history[-1]["quote_volume"] = amount
                with self.subTest(strategy=strategy, volume=amount):
                    self.assertEqual(entry_signals(history, strategy, context_candles=context)[-1], expected)
                    detail = entry_signal_details(history, strategy, context_candles=context)
                    self.assertEqual(detail["technical_signal"], "buy")
                    self.assertEqual(detail["signal"], expected)
                    self.assertEqual(D(detail["liquidity"]["quote_volume_24h"]), D(amount))
                    self.assertEqual(detail["liquidity"]["eligible"], expected == "buy")
                    self.assertEqual(detail["entry_ready"], expected == "buy")

    def test_volume_filter_does_not_block_technical_sells(self):
        observed_sells = 0
        for strategy, history in automatic_buy_fixtures().items():
            raw = signals(history, strategy)
            filtered = entry_signals(history, strategy)
            for index, decision in enumerate(raw):
                with self.subTest(strategy=strategy, index=index):
                    if decision == "sell":
                        self.assertEqual(filtered[index], "sell")
                        observed_sells += 1
                    elif decision == "buy":
                        self.assertEqual(filtered[index], "hold")
        self.assertGreater(observed_sells, 0)

    def test_missing_history_blocks_all_buys_and_preserves_technical_explanation(self):
        for strategy, history in automatic_buy_fixtures().items():
            original = deepcopy(history)
            detail = entry_signal_details(history, strategy)
            with self.subTest(strategy=strategy):
                self.assertEqual(detail["technical_signal"], "buy")
                self.assertEqual(detail["signal"], "hold")
                self.assertFalse(detail["entry_ready"])
                self.assertFalse(detail["liquidity"]["eligible"])
                self.assertIsNone(detail["liquidity"]["quote_volume_24h"])
                self.assertEqual(history, original)

    def test_liquidity_filter_is_causal_and_keeps_context_outside_requested_window(self):
        for strategy, original in automatic_buy_fixtures().items():
            history, context = liquid_history(original)
            frozen = deepcopy((history, context))
            full = entry_signals(history, strategy, context_candles=context)
            self.assertEqual(len(full), len(history))
            self.assertEqual(full[-1], "buy")
            for length in (0, 1, len(history) - 1):
                with self.subTest(strategy=strategy, count=length):
                    self.assertEqual(entry_signals(history[:length], strategy, context_candles=context), full[:length])
            self.assertEqual((history, context), frozen)


if __name__ == "__main__":
    unittest.main()
