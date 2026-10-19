from copy import deepcopy
from decimal import Decimal
import json
import unittest

from bot.core import PaperBroker
from bot.portfolio import PaperPortfolio


D = Decimal
T0 = "2026-10-18T10:00:00Z"
T1 = "2026-10-18T10:01:00Z"
T2 = "2026-10-18T10:02:00Z"
T3 = "2026-10-18T10:03:00Z"


def config(**changes):
    result = dict(initial_cash="1000", order_quote_amount="100", buy_fee_pct="0",
                  sell_fee_pct="0", slippage_pct="0", stop_loss_pct="0",
                  trailing_mode="off", daily_loss_limit_pct="0", max_open_positions=5)
    result.update(changes)
    return result


class PortfolioTests(unittest.TestCase):
    def invariant(self, portfolio, prices):
        state = portfolio.snapshot(prices)
        self.assertLess(abs(D(state["total_pnl"]) - (D(state["equity"]) - D(state["initial_cash"]))), D("1e-20"))
        self.assertLess(abs(D(state["realized_pnl"]) + D(state["unrealized_pnl"]) - D(state["total_pnl"])), D("1e-20"))
        self.assertGreaterEqual(D(state["cash"]), 0)

    def test_two_assets_share_cash_and_realized_accounting(self):
        portfolio = PaperPortfolio(config(order_quote_amount="100.1", buy_fee_pct="0.1", sell_fee_pct="0.1"))
        events = portfolio.tick({"BTCUSDT": "100", "ETHUSDT": "50"}, T0,
                                {"BTCUSDT": "buy", "ETHUSDT": "buy"})
        self.assertEqual([event["symbol"] for event in events], ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(D(portfolio.snapshot({"BTCUSDT": "100", "ETHUSDT": "50"})["cash"]), D("799.8"))
        self.assertEqual(len(portfolio.position_symbols), 2)
        self.invariant(portfolio, {"BTCUSDT": "110", "ETHUSDT": "45"})
        portfolio.tick({"BTCUSDT": "110", "ETHUSDT": "45"}, T1,
                       {"BTCUSDT": "sell", "ETHUSDT": "sell"})
        state = portfolio.snapshot({})
        self.assertEqual(D(state["realized_pnl"]), D("-0.4"))
        self.assertEqual(D(state["cash"]), D("999.6"))
        self.assertEqual(D(state["fees_paid"]), D("0.4"))
        self.assertEqual(state["trade_count"], 2)
        self.assertEqual(state["win_count"], 1)
        self.assertEqual({trade["symbol"] for trade in state["trades"]}, {"BTCUSDT", "ETHUSDT"})
        self.assertEqual({trade["id"] for trade in state["trades"]}, {1, 2})
        self.invariant(portfolio, {})

    def test_cash_is_not_multiplied_and_budget_capped_to_available(self):
        portfolio = PaperPortfolio(config(initial_cash="150", order_quote_amount="100"))
        events = portfolio.tick({"BTCUSDT": "100", "ETHUSDT": "100", "SOLUSDT": "100"}, T0,
                                {"BTCUSDT": "buy", "ETHUSDT": "buy", "SOLUSDT": "buy"})
        self.assertEqual(events[-1]["reason"], "insufficient_cash")
        self.assertEqual(D(events[1]["net_quote"]), 50)
        self.assertEqual(D(portfolio.snapshot({"BTCUSDT": "100", "ETHUSDT": "100"})["equity"]), 150)
        self.invariant(portfolio, {"BTCUSDT": "100", "ETHUSDT": "100"})

    def test_max_positions_and_no_duplicate_asset(self):
        portfolio = PaperPortfolio(config(max_open_positions=1))
        events = portfolio.tick({"BTCUSDT": "100", "ETHUSDT": "50"}, T0,
                                {"BTCUSDT": "buy", "ETHUSDT": "buy"})
        self.assertEqual(events[-1]["reason"], "max_open_positions")
        self.assertEqual(portfolio.tick({"BTCUSDT": "100"}, T1, {"BTCUSDT": "buy"})[0]["reason"], "position_open")
        portfolio.tick({"BTCUSDT": "100", "ETHUSDT": "50"}, T2,
                       {"ETHUSDT": "buy", "BTCUSDT": "sell"})
        self.assertEqual(portfolio.position_symbols, ("ETHUSDT",))

    def test_filters_are_per_symbol_and_unused_budget_stays_global(self):
        portfolio = PaperPortfolio(config())
        portfolio.tick({"BTCUSDT": "100", "ETHUSDT": "100"}, T0,
                       {"BTCUSDT": "buy", "ETHUSDT": "buy"},
                       {"BTCUSDT": {"step_size": "0.3"}, "ETHUSDT": {"step_size": "0.4"}})
        positions = {item["symbol"]: item for item in portfolio.snapshot({"BTCUSDT": "100", "ETHUSDT": "100"})["positions"]}
        self.assertEqual(D(positions["BTCUSDT"]["quantity"]), D("0.9"))
        self.assertEqual(D(positions["ETHUSDT"]["quantity"]), D("0.8"))
        self.assertEqual(D(portfolio.snapshot({})["cash"]), 830)
        self.invariant(portfolio, {"BTCUSDT": "110", "ETHUSDT": "50"})

    def test_core_protections_run_before_buy_and_hold_keeps_them_active(self):
        portfolio = PaperPortfolio(config(stop_loss_pct="2"))
        portfolio.tick({"BTCUSDT": "100", "ETHUSDT": "50"}, T0,
                       {"BTCUSDT": "buy", "ETHUSDT": "buy"})
        events = portfolio.tick({"BTCUSDT": "95", "ETHUSDT": "50"}, T1, {"BTCUSDT": "buy"})
        self.assertEqual(events[0]["reason"], "fixed_stop")
        self.assertEqual(events[-1]["reason"], "protective_exit")
        self.assertEqual(portfolio.position_symbols, ("ETHUSDT",))
        events = portfolio.tick({"ETHUSDT": "48"}, T2)
        self.assertEqual(events[0]["reason"], "fixed_stop")
        self.invariant(portfolio, {})

    def test_trailing_modes_reuse_core_math(self):
        for mode, price, pnl, reason in (("price", "107.8", "7.8", "trailing_price"),
                                         ("net_profit", "108", "8", "trailing_profit")):
            with self.subTest(mode=mode):
                portfolio = PaperPortfolio(config(trailing_mode=mode, trailing_distance_pct="2",
                                                  trailing_activation_pct="5", profit_giveback_pct="20"))
                portfolio.tick({"BTCUSDT": "100"}, T0, {"BTCUSDT": "buy"})
                portfolio.tick({"BTCUSDT": "110"}, T1)
                event = portfolio.tick({"BTCUSDT": price}, T2)[0]
                self.assertEqual(event["reason"], reason)
                self.assertEqual(D(event["pnl_quote"]), D(pnl))

    def test_global_daily_limit_uses_total_equity_and_resets_istanbul(self):
        portfolio = PaperPortfolio(config(order_quote_amount="500", daily_loss_limit_pct="5"))
        portfolio.tick({"BTCUSDT": "100", "ETHUSDT": "50"}, "2026-10-18T20:00:00Z",
                       {"BTCUSDT": "buy", "ETHUSDT": "buy"})
        events = portfolio.tick({"BTCUSDT": "95", "ETHUSDT": "47.5"}, "2026-10-18T20:01:00Z")
        self.assertEqual(len([e for e in events if e["type"] == "sell"]), 2)
        self.assertTrue(portfolio.snapshot({})["risk"]["daily_halted"])
        self.assertEqual(D(portfolio.snapshot({})["cash"]), 950)
        blocked = portfolio.tick({"BTCUSDT": "95"}, "2026-10-18T20:59:00Z", {"BTCUSDT": "buy"})
        self.assertEqual(blocked[0]["reason"], "daily_loss_limit")
        events = portfolio.tick({"BTCUSDT": "95"}, "2026-10-18T21:00:00Z", {"BTCUSDT": "buy"})
        self.assertEqual(events[0]["type"], "buy")
        self.assertEqual(portfolio.snapshot({"BTCUSDT": "95"})["risk"]["day"], "2026-10-19")

    def test_missing_quote_blocks_entries_and_never_executes_at_cached_price(self):
        portfolio = PaperPortfolio(config(stop_loss_pct="2"))
        portfolio.tick({"BTCUSDT": "100"}, T0, {"BTCUSDT": "buy"})
        before = deepcopy(portfolio.export_state()["brokers"]["BTCUSDT"])
        events = portfolio.tick({"ETHUSDT": "50"}, T1, {"BTCUSDT": "sell", "ETHUSDT": "buy"})
        self.assertTrue(all(event["type"] == "blocked" for event in events))
        self.assertEqual(before, portfolio.export_state()["brokers"]["BTCUSDT"])
        snapshot = portfolio.snapshot({"ETHUSDT": "50"})
        self.assertTrue(snapshot["positions"][0]["quote_missing"])
        self.assertEqual(snapshot["risk"]["missing_symbols"], ["BTCUSDT"])
        self.assertEqual(D(snapshot["positions"][0]["last_price"]), 100)
        self.assertEqual(portfolio.tick({"BTCUSDT": "90"}, T2)[0]["reason"], "fixed_stop")

    def test_daily_limit_closes_known_and_persists_pending_missing_position(self):
        portfolio = PaperPortfolio(config(order_quote_amount="500", daily_loss_limit_pct="5"))
        portfolio.tick({"BTCUSDT": "100", "ETHUSDT": "100"}, T0,
                       {"BTCUSDT": "buy", "ETHUSDT": "buy"})
        events = portfolio.tick({"BTCUSDT": "90"}, T1)
        self.assertEqual(events[0]["symbol"], "BTCUSDT")
        self.assertEqual(events[0]["reason"], "daily_loss_limit")
        self.assertEqual(portfolio.position_symbols, ("ETHUSDT",))
        self.assertEqual(portfolio.snapshot({})["risk"]["pending_risk_symbols"], ["ETHUSDT"])
        restored = PaperPortfolio(portfolio.config, json.loads(json.dumps(portfolio.export_state())))
        events = restored.tick({"ETHUSDT": "110", "SOLUSDT": "20"}, T2, {"SOLUSDT": "buy"})
        self.assertEqual(events[0]["reason"], "daily_loss_limit")
        self.assertEqual(events[-1]["type"], "blocked")
        self.assertEqual(restored.position_symbols, ())
        self.invariant(restored, {})

    def test_new_day_missing_quote_cannot_trigger_against_yesterdays_baseline(self):
        portfolio = PaperPortfolio(config(order_quote_amount="500", daily_loss_limit_pct="5"))
        portfolio.tick({"BTCUSDT": "100", "ETHUSDT": "100"}, "2026-10-18T20:58:00Z",
                       {"BTCUSDT": "buy", "ETHUSDT": "buy"})
        portfolio.tick({"BTCUSDT": "96", "ETHUSDT": "100"}, "2026-10-18T20:59:00Z")
        events = portfolio.tick({"BTCUSDT": "90"}, "2026-10-18T21:00:00Z")
        self.assertEqual(events, [])
        self.assertEqual(len(portfolio.position_symbols), 2)
        self.assertFalse(portfolio.snapshot({})["risk"]["daily_halted"])
        portfolio.tick({"BTCUSDT": "90", "ETHUSDT": "100"}, "2026-10-18T21:01:00Z")
        risk = portfolio.snapshot({"BTCUSDT": "90", "ETHUSDT": "100"})["risk"]
        self.assertEqual(risk["day"], "2026-10-19")
        self.assertEqual(D(risk["day_start_equity"]), 950)
        self.assertFalse(risk["daily_halted"])

    def test_pending_risk_survives_midnight_and_closes_on_fresh_quote(self):
        portfolio = PaperPortfolio(config(order_quote_amount="500", daily_loss_limit_pct="5"))
        portfolio.tick({"BTCUSDT": "100", "ETHUSDT": "100"}, "2026-10-18T20:58:00Z",
                       {"BTCUSDT": "buy", "ETHUSDT": "buy"})
        portfolio.tick({"BTCUSDT": "90"}, "2026-10-18T20:59:00Z")
        self.assertEqual(portfolio.tick({}, "2026-10-18T21:00:00Z"), [])
        self.assertEqual(portfolio.snapshot({})["risk"]["pending_risk_symbols"], ["ETHUSDT"])
        portfolio = PaperPortfolio(portfolio.config, portfolio.export_state())
        events = portfolio.tick({"ETHUSDT": "110", "SOLUSDT": "10"}, "2026-10-18T21:01:00Z", {"SOLUSDT": "buy"})
        self.assertEqual(events[0]["reason"], "daily_loss_limit")
        self.assertEqual(events[-1]["type"], "blocked")
        self.assertEqual(portfolio.position_symbols, ())
        self.assertFalse(portfolio.snapshot({})["risk"]["daily_halted"])
        self.invariant(portfolio, {})
        events = portfolio.tick({"SOLUSDT": "10"}, "2026-10-18T21:02:00Z", {"SOLUSDT": "buy"})
        self.assertEqual(events[0]["type"], "buy")

    def test_entry_costs_can_trip_global_limit_before_second_entry(self):
        portfolio = PaperPortfolio(config(order_quote_amount="1000", buy_fee_pct="10", daily_loss_limit_pct="1"))
        events = portfolio.tick({"BTCUSDT": "100", "ETHUSDT": "50"}, T0,
                                {"BTCUSDT": "buy", "ETHUSDT": "buy"})
        self.assertEqual([event["type"] for event in events], ["buy", "sell", "risk", "blocked"])
        self.assertEqual(events[1]["reason"], "daily_loss_limit")
        self.assertEqual(events[-1]["symbol"], "ETHUSDT")
        state = portfolio.snapshot({})
        self.assertEqual(state["positions"], [])
        self.assertTrue(state["risk"]["daily_halted"])
        self.assertLess(abs(D(state["fees_paid"]) + D(state["realized_pnl"])), D("1e-20"))
        self.invariant(portfolio, {})

    def test_restart_keeps_both_assets_cash_peaks_and_next_action(self):
        portfolio = PaperPortfolio(config(trailing_mode="price", trailing_activation_pct="5"))
        portfolio.tick({"BTCUSDT": "100", "ETHUSDT": "50"}, T0,
                       {"BTCUSDT": "buy", "ETHUSDT": "buy"})
        portfolio.tick({"BTCUSDT": "110", "ETHUSDT": "55"}, T1)
        restored = PaperPortfolio(portfolio.config, json.loads(json.dumps(portfolio.export_state())))
        self.assertEqual(portfolio.snapshot({"BTCUSDT": "110", "ETHUSDT": "55"}), restored.snapshot({"BTCUSDT": "110", "ETHUSDT": "55"}))
        prices = {"BTCUSDT": "105", "ETHUSDT": "54"}
        self.assertEqual(portfolio.tick(prices, T2), restored.tick(prices, T2))
        self.assertEqual(portfolio.export_state(), restored.export_state())

    def test_legacy_migration_keeps_closed_trade_open_position_and_peaks(self):
        settings = config(buy_fee_pct="0.1", sell_fee_pct="0.1", trailing_mode="price", trailing_activation_pct="5")
        legacy = PaperBroker(settings)
        legacy.buy("100", T0)
        legacy.sell("105", T1)
        legacy.buy("100", T2)
        legacy.tick("110", T3)
        saved = legacy.export_state()
        portfolio = PaperPortfolio.from_legacy(settings, saved, "BTCUSDT")
        old, new = legacy.snapshot("110"), portfolio.snapshot({"BTCUSDT": "110"})
        for key in ("cash", "equity", "realized_pnl", "unrealized_pnl", "total_pnl", "fees_paid", "trade_count"):
            self.assertEqual(old[key], new[key], key)
        for key, value in old["position"].items():
            self.assertEqual(value, new["positions"][0][key], key)
        self.assertEqual(new["trades"][0]["symbol"], "BTCUSDT")
        legacy.sell("108", "2026-10-18T10:04:00Z")
        portfolio.sell("BTCUSDT", "108", "2026-10-18T10:04:00Z")
        for key in ("cash", "realized_pnl", "fees_paid", "trade_count"):
            self.assertEqual(legacy.snapshot("108")[key], portfolio.snapshot({})[key], key)
        self.invariant(portfolio, {})

    def test_legacy_daily_lock_is_preserved(self):
        settings = config(order_quote_amount="1000", daily_loss_limit_pct="5")
        legacy = PaperBroker(settings)
        legacy.buy("100", T0)
        legacy.tick("95", T1)
        portfolio = PaperPortfolio.from_legacy(settings, legacy.export_state(), "BTCUSDT")
        self.assertTrue(portfolio.snapshot({})["risk"]["daily_halted"])
        self.assertEqual(portfolio.tick({"ETHUSDT": "50"}, T2, {"ETHUSDT": "buy"})[0]["reason"], "daily_loss_limit")

    def test_invalid_batch_is_atomic_including_filters_and_all_assets(self):
        portfolio = PaperPortfolio(config())
        portfolio.tick({"BTCUSDT": "100", "ETHUSDT": "50"}, T1,
                       {"BTCUSDT": "buy", "ETHUSDT": "buy"})
        before = portfolio.export_state()
        cases = [
            ({"BTCUSDT": "110", "ETHUSDT": "NaN"}, T2, {}, None),
            ({"BTCUSDT": "0"}, T2, {}, None),
            ({"BTCUSDT": "110"}, T0, {}, None),
            ({"BTCUSDT": "110"}, T2, {"ETHUSDT": "bad"}, None),
            ({"BTCUSDT": "110"}, T2, {}, {"BTCUSDT": {"step_size": "NaN"}}),
            ({"BTCUSDT": "110"}, T2, {}, {"BTCUSDT": {"initial_cash": "99999"}}),
        ]
        for prices, timestamp, signals, filters in cases:
            with self.subTest(prices=prices, signals=signals, filters=filters):
                with self.assertRaises(ValueError):
                    portfolio.tick(prices, timestamp, signals, filters)
                self.assertEqual(before, portfolio.export_state())
        portfolio.snapshot({"BTCUSDT": "200"})
        self.assertEqual(before, portfolio.export_state())

    def test_max_position_config_validation(self):
        for value in (0, 101, "1.5", True, "NaN"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    PaperPortfolio(config(max_open_positions=value))

    def test_manual_amount_is_per_order_and_entry_metadata_survives_close(self):
        portfolio = PaperPortfolio(config())
        original_config = deepcopy(portfolio.config)
        event = portfolio.buy("BTCUSDT", {"BTCUSDT": "100"}, T0, amount="150")[0]
        self.assertEqual(event["reason"], "manual_buy")
        self.assertEqual(D(event["net_quote"]), 150)
        self.assertEqual(D(event["quantity"]), D("1.5"))
        self.assertEqual(portfolio.snapshot({"BTCUSDT": "100"})["positions"][0]["entry_reason"], "manual")
        event = portfolio.tick({"BTCUSDT": "100", "ETHUSDT": "50"}, T1, {"ETHUSDT": "buy"})[0]
        self.assertEqual(event["reason"], "signal")
        self.assertEqual(D(event["net_quote"]), 100)
        self.assertEqual(portfolio.config, original_config)
        portfolio = PaperPortfolio(portfolio.config, portfolio.export_state())
        events = portfolio.sell("BTCUSDT", "110", T2, reason="manual_sell")
        self.assertEqual(events[0]["reason"], "manual_sell")
        self.assertEqual(events[0]["entry_reason"], "manual")
        self.assertEqual(portfolio.snapshot({"ETHUSDT": "50"})["trades"][0]["entry_reason"], "manual")
        self.invariant(portfolio, {"ETHUSDT": "50"})

    def test_manual_default_and_shared_cash_cap_and_duplicate_limit(self):
        portfolio = PaperPortfolio(config(initial_cash="150"))
        event = portfolio.buy("BTCUSDT", {"BTCUSDT": "100"}, T0)[0]
        self.assertEqual(D(event["net_quote"]), 100)
        prices = {"BTCUSDT": "100", "ETHUSDT": "50"}
        event = portfolio.buy("ETHUSDT", prices, T1, amount="10000")[0]
        self.assertEqual(D(event["net_quote"]), 50)
        self.assertEqual(D(portfolio.snapshot(prices)["cash"]), 0)
        self.assertEqual(portfolio.buy("BTCUSDT", prices, T2)[0]["reason"], "position_open")
        events = portfolio.buy("SOLUSDT", {**prices, "SOLUSDT": "10"}, T3)
        self.assertEqual(events[0]["reason"], "insufficient_cash")
        self.invariant(portfolio, prices)
        limited = PaperPortfolio(config(max_open_positions=1))
        limited.buy("BTCUSDT", {"BTCUSDT": "100"}, T0)
        self.assertEqual(limited.buy("ETHUSDT", prices, T1)[0]["reason"], "max_open_positions")

    def test_manual_entry_cannot_bypass_global_limit_or_missing_quotes(self):
        portfolio = PaperPortfolio(config(order_quote_amount="1000", daily_loss_limit_pct="5"))
        portfolio.buy("BTCUSDT", {"BTCUSDT": "100"}, T0)
        portfolio.tick({"BTCUSDT": "95"}, T1)
        self.assertEqual(portfolio.buy("ETHUSDT", {"ETHUSDT": "50"}, T2)[0]["reason"], "daily_loss_limit")
        portfolio = PaperPortfolio(config())
        portfolio.buy("BTCUSDT", {"BTCUSDT": "100"}, T0)
        events = portfolio.buy("ETHUSDT", {"ETHUSDT": "50"}, T1)
        self.assertEqual(events[0]["reason"], "missing_price")
        self.assertEqual(portfolio.position_symbols, ("BTCUSDT",))

    def test_manual_entry_runs_existing_stops_before_new_order(self):
        portfolio = PaperPortfolio(config(stop_loss_pct="2"))
        portfolio.buy("BTCUSDT", {"BTCUSDT": "100"}, T0)
        events = portfolio.buy("BTCUSDT", {"BTCUSDT": "95"}, T1, amount="50")
        self.assertEqual(events[0]["reason"], "fixed_stop")
        self.assertEqual(events[0]["entry_reason"], "manual")
        self.assertEqual(events[-1]["reason"], "protective_exit")
        self.assertEqual(portfolio.position_symbols, ())

    def test_invalid_manual_amounts_are_atomic(self):
        portfolio = PaperPortfolio(config())
        portfolio.buy("BTCUSDT", {"BTCUSDT": "100"}, T0)
        before, original_config = portfolio.export_state(), deepcopy(portfolio.config)
        for amount in ("NaN", "Infinity", "-1", "0", True, "not-a-number"):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    portfolio.buy("ETHUSDT", {"BTCUSDT": "120", "ETHUSDT": "50"}, T1, amount)
                self.assertEqual(portfolio.export_state(), before)
                self.assertEqual(portfolio.config, original_config)

    def test_manual_buy_accepts_verified_symbol_filters(self):
        portfolio = PaperPortfolio(config())
        event = portfolio.buy("BTCUSDT", {"BTCUSDT": "100"}, T0,
                              filters={"step_size": "0.3", "min_notional": "90"})[0]
        self.assertEqual(D(event["quantity"]), D("0.9"))
        event = portfolio.buy("ETHUSDT", {"BTCUSDT": "100", "ETHUSDT": "100"}, T1,
                              filters={"ETHUSDT": {"step_size": "0.4"}})[0]
        self.assertEqual(D(event["quantity"]), D("0.8"))
        self.assertEqual(D(portfolio.snapshot({})["cash"]), 830)
        self.invariant(portfolio, {"BTCUSDT": "100", "ETHUSDT": "100"})

    def test_manual_sell_marks_other_fresh_profit_before_daily_limit(self):
        portfolio = PaperPortfolio(config(order_quote_amount="500", daily_loss_limit_pct="5"))
        portfolio.tick({"BTCUSDT": "100", "ETHUSDT": "100"}, T0,
                       {"BTCUSDT": "buy", "ETHUSDT": "buy"})
        prices = {"BTCUSDT": "90", "ETHUSDT": "120"}
        events = portfolio.sell("BTCUSDT", "90", T1, reason="manual_sell", prices=prices)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reason"], "manual_sell")
        self.assertFalse(portfolio.snapshot(prices)["risk"]["daily_halted"])
        self.assertEqual(D(portfolio.snapshot(prices)["equity"]), 1050)
        self.assertEqual(portfolio.position_symbols, ("ETHUSDT",))
        self.invariant(portfolio, prices)

    def test_manual_sell_includes_other_fresh_loss_in_daily_limit(self):
        portfolio = PaperPortfolio(config(order_quote_amount="500", daily_loss_limit_pct="5"))
        portfolio.tick({"BTCUSDT": "100", "ETHUSDT": "100"}, T0,
                       {"BTCUSDT": "buy", "ETHUSDT": "buy"})
        prices = {"BTCUSDT": "110", "ETHUSDT": "80"}
        events = portfolio.sell("BTCUSDT", "110", T1, reason="manual_sell", prices=prices)
        self.assertEqual([event["type"] for event in events], ["sell", "sell", "risk"])
        self.assertEqual(events[1]["symbol"], "ETHUSDT")
        self.assertEqual(events[1]["reason"], "daily_loss_limit")
        self.assertTrue(portfolio.snapshot({})["risk"]["daily_halted"])
        self.assertEqual(D(portfolio.snapshot({})["equity"]), 950)
        self.invariant(portfolio, {})

    def test_manual_sell_mismatched_or_invalid_price_map_is_atomic(self):
        portfolio = PaperPortfolio(config())
        portfolio.buy("BTCUSDT", {"BTCUSDT": "100"}, T0)
        before = portfolio.export_state()
        for prices in ({"ETHUSDT": "50"}, {"BTCUSDT": "101"},
                       {"BTCUSDT": "100", "ETHUSDT": "NaN"}):
            with self.subTest(prices=prices):
                with self.assertRaises(ValueError):
                    portfolio.sell("BTCUSDT", "100", T1, prices=prices)
                self.assertEqual(portfolio.export_state(), before)

    def test_unicode_and_single_letter_exchange_symbols_work_and_restore(self):
        portfolio = PaperPortfolio(config())
        prices = {"币安人生USDT": "10", "TUSDT": "1"}
        events = portfolio.tick(prices, T0, {symbol: "buy" for symbol in prices})
        self.assertEqual({event["symbol"] for event in events}, set(prices))
        restored = PaperPortfolio(portfolio.config, json.loads(json.dumps(portfolio.export_state())))
        self.assertEqual(portfolio.snapshot(prices), restored.snapshot(prices))
        self.invariant(restored, prices)
        before = restored.export_state()
        for symbol in ("USDT", "../BTCUSDT", "BTC_USDT", "A" * 41 + "USDT", "A\u200bUSDT"):
            with self.subTest(symbol=symbol):
                with self.assertRaises(ValueError):
                    restored.tick({symbol: "1"}, T1)
                self.assertEqual(restored.export_state(), before)

    def test_open_return_weights_entry_cost_and_excludes_closed_trades(self):
        portfolio = PaperPortfolio(config())
        prices = {"BTCUSDT": "100", "ETHUSDT": "100"}
        portfolio.buy("BTCUSDT", prices, T0, amount="100")
        portfolio.buy("ETHUSDT", prices, T1, amount="300")
        marks = {"BTCUSDT": "110", "ETHUSDT": "90"}
        state = portfolio.snapshot(marks)
        self.assertEqual(D(state["open_entry_cost"]), 400)
        self.assertEqual(D(state["unrealized_pnl"]), -20)
        self.assertEqual(D(state["unrealized_pnl_pct"]), -5)
        self.assertEqual(D(state["total_pnl_pct"]), -2)
        portfolio.sell("BTCUSDT", "110", T2, prices=marks)
        state = portfolio.snapshot(marks)
        self.assertEqual(D(state["realized_pnl"]), 10)
        self.assertEqual(D(state["open_entry_cost"]), 300)
        self.assertEqual(D(state["unrealized_pnl"]), -30)
        self.assertEqual(D(state["unrealized_pnl_pct"]), -10)
        portfolio.sell("ETHUSDT", "90", T3)
        state = portfolio.snapshot({})
        self.assertEqual(D(state["realized_pnl"]), -20)
        for field in ("open_entry_cost", "unrealized_pnl", "unrealized_pnl_pct"):
            self.assertEqual(D(state[field]), 0)
        self.assertFalse(state["risk"]["valuation_stale"])

    def test_open_return_includes_both_fees_and_execution_slippage(self):
        portfolio = PaperPortfolio(config(buy_fee_pct="0.1", sell_fee_pct="0.2", slippage_pct="0.05"))
        prices = {"BTCUSDT": "100", "ETHUSDT": "50"}
        entries = [portfolio.buy("BTCUSDT", prices, T0, amount="100.1")[0],
                   portfolio.buy("ETHUSDT", prices, T1, amount="300.3")[0]]
        marks = {"BTCUSDT": "110", "ETHUSDT": "45"}
        entry_cost = sum((D(event["net_quote"]) for event in entries), D(0))
        proceeds = sum((D(event["quantity"]) * D(marks[event["symbol"]]) * D("0.9995") * D("0.998")
                        for event in entries), D(0))
        expected = proceeds - entry_cost
        state = portfolio.snapshot(marks)
        self.assertEqual(D(state["open_entry_cost"]), entry_cost)
        self.assertLess(abs(D(state["unrealized_pnl"]) - expected), D("1e-23"))
        self.assertLess(abs(D(state["unrealized_pnl_pct"]) - expected / entry_cost * 100), D("1e-23"))
        self.assertLess(D(portfolio.snapshot(prices)["unrealized_pnl_pct"]), 0)
        self.invariant(portfolio, marks)

    def test_open_return_preserves_decimal_precision(self):
        portfolio = PaperPortfolio(config())
        prices = {"BTCUSDT": "100", "ETHUSDT": "100"}
        portfolio.buy("BTCUSDT", prices, T0, amount="100.0000000000000001")
        portfolio.buy("ETHUSDT", prices, T1, amount="300.0000000000000003")
        marks = {"BTCUSDT": "110", "ETHUSDT": "90"}
        state = portfolio.snapshot(marks)
        self.assertIsInstance(state["open_entry_cost"], str)
        self.assertEqual(D(state["open_entry_cost"]), D("400.0000000000000004"))
        self.assertEqual(D(state["unrealized_pnl"]), D("-20.00000000000000002"))
        self.assertEqual(D(state["unrealized_pnl_pct"]), -5)

    def test_open_return_is_read_only_and_retains_missing_quote_metadata(self):
        portfolio = PaperPortfolio(config())
        prices = {"BTCUSDT": "100", "ETHUSDT": "100"}
        portfolio.buy("BTCUSDT", prices, T0, amount="100")
        portfolio.buy("ETHUSDT", prices, T1, amount="300")
        marks = {"BTCUSDT": "110", "ETHUSDT": "90"}
        portfolio.tick(marks, T2)
        before = portfolio.export_state()
        partial = portfolio.snapshot({"BTCUSDT": "110"})
        self.assertEqual(D(partial["open_entry_cost"]), 400)
        self.assertEqual(D(partial["unrealized_pnl_pct"]), -5)
        self.assertTrue(partial["risk"]["valuation_stale"])
        self.assertEqual(partial["risk"]["missing_symbols"], ["ETHUSDT"])
        self.assertTrue(next(p for p in partial["positions"] if p["symbol"] == "ETHUSDT")["quote_missing"])
        portfolio.snapshot({"BTCUSDT": "500", "ETHUSDT": "500"})
        self.assertEqual(portfolio.export_state(), before)
        restored = PaperPortfolio(portfolio.config, json.loads(json.dumps(before)))
        self.assertEqual(restored.snapshot({"BTCUSDT": "110"}), partial)
        self.assertEqual(restored.export_state(), before)


if __name__ == "__main__":
    unittest.main()
