import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from bot.core import DEFAULT_CONFIG, PaperBroker, normalize_config


D = Decimal
T0 = "2026-10-18T10:00:00Z"
T1 = "2026-10-18T10:01:00Z"
T2 = "2026-10-18T10:02:00Z"
T3 = "2026-10-18T10:03:00Z"


def broker(**overrides):
    config = {
        "initial_cash": "1000", "order_quote_amount": "100",
        "buy_fee_pct": "0", "sell_fee_pct": "0", "slippage_pct": "0",
        "stop_loss_pct": "0", "trailing_mode": "off", "daily_loss_limit_pct": "0",
    }
    config.update(overrides)
    return PaperBroker(config)


class PaperBrokerTests(unittest.TestCase):
    def assert_money_equal(self, left, right):
        self.assertLessEqual(abs(D(left) - D(right)), D("1e-23"))

    def assert_invariant(self, instance, price):
        snapshot = instance.snapshot(price)
        realized, unrealized = D(snapshot["realized_pnl"]), D(snapshot["unrealized_pnl"])
        self.assert_money_equal(realized + unrealized, D(snapshot["equity"]) - D(snapshot["initial_cash"]))
        self.assertGreaterEqual(D(snapshot["cash"]), 0)

    def test_commission_round_trip_uses_both_fees(self):
        instance = broker(order_quote_amount="100.1", buy_fee_pct="0.1", sell_fee_pct="0.1")
        buy = instance.buy("100", T0)[0]
        self.assertEqual(D(buy["quantity"]), 1)
        self.assertEqual(D(buy["fee_quote"]), D("0.1"))
        self.assertEqual(D(instance.snapshot("100")["unrealized_pnl"]), D("-0.2"))
        self.assert_invariant(instance, "100")
        sell = instance.sell("110", T1)[0]
        self.assertEqual(D(sell["pnl_quote"]), D("9.79"))
        snapshot = instance.snapshot("110")
        self.assertEqual(D(snapshot["cash"]), D("1009.79"))
        self.assertEqual(D(snapshot["fees_paid"]), D("0.21"))
        self.assertEqual(snapshot["trade_count"], 1)
        self.assertEqual(snapshot["win_count"], 1)
        self.assertEqual(D(snapshot["trades"][0]["fees"]), D("0.21"))
        self.assert_invariant(instance, "110")

    def test_slippage_reduces_pnl_on_both_sides(self):
        instance = broker(order_quote_amount="101", slippage_pct="1")
        self.assertEqual(D(instance.buy("100", T0)[0]["fill_price"]), 101)
        event = instance.sell("100", T1)[0]
        self.assertEqual(D(event["fill_price"]), 99)
        self.assertEqual(D(event["pnl_quote"]), -2)
        self.assert_invariant(instance, "100")

    def test_lot_step_rounding_and_filter_rejection(self):
        instance = broker(step_size="0.3", min_notional="90")
        event = instance.buy("100", T0)[0]
        self.assertEqual(D(event["quantity"]), D("0.9"))
        self.assertEqual(D(instance.snapshot("100")["cash"]), 910)
        rejected = broker(step_size="0.3", min_notional="91")
        events = rejected.buy("100", T0)
        self.assertEqual(events[0]["reason"], "min_notional")
        self.assertEqual(rejected.snapshot("100")["position"], None)
        self.assertEqual(D(rejected.snapshot("100")["fees_paid"]), 0)
        minimum = broker(min_qty="2")
        self.assertEqual(minimum.buy("100", T0)[0]["reason"], "min_qty")

    def test_budget_never_overdraws_even_with_decimal_rounding(self):
        instance = broker(initial_cash="100", order_quote_amount="100", buy_fee_pct="0.1")
        for index, price in enumerate(("3", "7", "0.123456789")):
            instant = f"2026-10-18T10:0{index}:00Z"
            instance.buy(price, instant)
            self.assertGreaterEqual(D(instance.snapshot(price)["cash"]), 0)
            instance.sell(price, instant)
            self.assert_invariant(instance, price)

    def test_fixed_stop_threshold_and_gap_execution(self):
        instance = broker(stop_loss_pct="2", slippage_pct="1", order_quote_amount="101")
        instance.buy("100", T0)
        self.assertEqual(instance.tick("99", T1), [])
        # Fill at 101 gives fixed stop 98.98; a gap must fill at today's price.
        event = instance.tick("95", T2, signal="buy")[0]
        self.assertEqual(event["reason"], "fixed_stop")
        self.assertEqual(D(event["fill_price"]), D("94.05"))
        self.assertIsNone(instance.snapshot("95")["position"])
        exact = broker(stop_loss_pct="2")
        exact.buy("100", T0)
        self.assertEqual(exact.tick("98", T1)[0]["reason"], "fixed_stop")

    def test_price_trailing_activates_and_does_not_move_down(self):
        instance = broker(trailing_mode="price", trailing_distance_pct="2", trailing_activation_pct="5")
        instance.buy("100", T0)
        instance.tick("104", T1)
        self.assertFalse(instance.snapshot("104")["position"]["trailing_active"])
        instance.tick("110", T2)
        self.assertEqual(D(instance.snapshot("110")["position"]["trailing_stop_price"]), D("107.8"))
        instance.tick("109", T3)
        self.assertEqual(D(instance.snapshot("109")["position"]["trailing_stop_price"]), D("107.8"))
        event = instance.tick("107.8", "2026-10-18T10:04:00Z")[0]
        self.assertEqual(event["reason"], "trailing_price")
        self.assertEqual(D(event["pnl_quote"]), D("7.8"))

    def test_net_profit_trailing_is_not_price_drawdown(self):
        instance = broker(trailing_mode="net_profit", profit_giveback_pct="20", trailing_activation_pct="5")
        instance.buy("100", T0)
        instance.tick("110", T1)
        snapshot = instance.snapshot("110")
        self.assertEqual(D(snapshot["position"]["highest_net_profit"]), 10)
        self.assertEqual(D(snapshot["position"]["trailing_stop_price"]), 108)
        self.assertEqual(instance.tick("108.01", T2), [])
        event = instance.tick("108", T3)[0]
        self.assertEqual(event["reason"], "trailing_profit")
        self.assertEqual(D(event["pnl_quote"]), 8)

    def test_trailing_activation_and_profit_protection_include_commissions(self):
        instance = broker(order_quote_amount="100.1", buy_fee_pct="0.1", sell_fee_pct="0.1",
                          trailing_mode="net_profit", profit_giveback_pct="20", trailing_activation_pct="5")
        instance.buy("100", T0)
        instance.tick("105", T1)
        self.assertFalse(instance.snapshot("105")["position"]["trailing_active"])
        instance.tick("110", T2)
        snapshot = instance.snapshot("110")
        protected_profit = D(snapshot["position"]["highest_net_profit"]) * D("0.8")
        stop = snapshot["position"]["trailing_stop_price"]
        event = instance.tick(stop, T3)[0]
        self.assertEqual(event["reason"], "trailing_profit")
        self.assert_money_equal(event["pnl_quote"], protected_profit)

    def test_open_position_buy_is_blocked_and_hold_still_protects(self):
        instance = broker(stop_loss_pct="2")
        instance.buy("100", T0)
        self.assertEqual(instance.buy("101", T1)[0]["reason"], "position_open")
        self.assertEqual(D(instance.snapshot("101")["position"]["quantity"]), 1)
        self.assertEqual(instance.tick("98", T2, signal="hold")[0]["reason"], "fixed_stop")

    def test_daily_limit_includes_unrealized_and_locks_until_istanbul_midnight(self):
        instance = broker(order_quote_amount="1000", daily_loss_limit_pct="5")
        instance.buy("100", "2026-10-18T20:00:00Z")
        events = instance.tick("95", "2026-10-18T20:01:00Z", signal="buy")
        self.assertEqual([event["type"] for event in events], ["sell", "risk"])
        self.assertEqual(events[0]["reason"], "daily_loss_limit")
        self.assertTrue(instance.snapshot("95")["risk"]["daily_halted"])
        self.assertEqual(instance.buy("95", "2026-10-18T20:59:59Z")[0]["reason"], "daily_loss_limit")
        self.assertEqual(instance.buy("95", "2026-10-18T21:00:00Z")[0]["type"], "buy")
        risk = instance.snapshot("95")["risk"]
        self.assertEqual(risk["day"], "2026-10-19")
        self.assertFalse(risk["daily_halted"])
        self.assertEqual(D(risk["day_start_equity"]), 950)

    def test_manual_losses_trip_daily_cap(self):
        instance = broker(order_quote_amount="1000", daily_loss_limit_pct="5")
        instance.buy("100", T0)
        events = instance.sell("94", T1)
        self.assertEqual(events[0]["reason"], "manual")
        self.assertEqual(events[-1]["type"], "risk")
        self.assertTrue(instance.snapshot("94")["risk"]["daily_halted"])

    def test_persistence_preserves_peaks_cash_fees_and_next_action(self):
        instance = broker(buy_fee_pct="0.1", sell_fee_pct="0.1", trailing_mode="price",
                          trailing_distance_pct="2", trailing_activation_pct="5")
        instance.buy("100", T0)
        instance.tick("110", T1)
        state = json.loads(json.dumps(instance.export_state()))
        restored = PaperBroker(instance.config, state)
        self.assertEqual(restored.snapshot("110"), instance.snapshot("110"))
        self.assertEqual(restored.tick("107", T2), instance.tick("107", T2))
        self.assertEqual(restored.export_state(), instance.export_state())
        second = PaperBroker(restored.config, restored.export_state())
        second.buy("100", T3)
        self.assertEqual(second.snapshot("100")["position"]["trade_id"], 2)

    def test_persistence_preserves_daily_halt(self):
        instance = broker(order_quote_amount="1000", daily_loss_limit_pct="5")
        instance.buy("100", T0)
        instance.tick("95", T1)
        restored = PaperBroker(instance.config, instance.export_state())
        self.assertEqual(restored.buy("95", T2)[0]["reason"], "daily_loss_limit")

    def test_snapshot_is_read_only_and_timestamps_may_be_equal(self):
        instance = broker(trailing_mode="price")
        instance.buy("100", T0)
        before = instance.export_state()
        instance.snapshot("150")
        self.assertEqual(instance.export_state(), before)
        self.assertFalse(instance.snapshot("100")["position"]["trailing_active"])
        instance.tick("110", T0)
        self.assertTrue(instance.snapshot("110")["position"]["trailing_active"])
        instance.sell("110", datetime(2026, 10, 18, 10, tzinfo=timezone.utc))

    def test_invalid_market_data_never_mutates_state(self):
        instance = broker()
        instance.buy("100", T1)
        before = instance.export_state()
        cases = [
            ("NaN", T2, "hold"), ("Infinity", T2, "hold"), ("-1", T2, "hold"),
            ("0", T2, "hold"), (True, T2, "hold"), ("1e10000", T2, "hold"),
            ("100", "2026-10-18", "hold"), ("100", T0, "hold"),
            ("100", T2, "unknown"),
        ]
        for price, timestamp, signal in cases:
            with self.subTest(price=price, timestamp=timestamp, signal=signal):
                with self.assertRaises(ValueError):
                    instance.tick(price, timestamp, signal)
                self.assertEqual(instance.export_state(), before)
        with self.assertRaises(ValueError):
            instance.snapshot("NaN")
        self.assertEqual(instance.export_state(), before)

    def test_config_validates_known_fields_and_preserves_app_fields(self):
        result = normalize_config({"symbol": "BTCUSDT", "strategy": "trend", "buy_fee_pct": 0.1})
        self.assertEqual(result["symbol"], "BTCUSDT")
        self.assertEqual(result["buy_fee_pct"], "0.1")
        self.assertEqual(DEFAULT_CONFIG["buy_fee_pct"], "0.10")
        for key, value in (("buy_fee_pct", "NaN"), ("slippage_pct", "100"),
                           ("initial_cash", "0"), ("trailing_mode", "unknown"),
                           ("day_timezone", "No/SuchZone"), ("profit_giveback_pct", "100")):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    broker(**{key: value})


if __name__ == "__main__":
    unittest.main()
