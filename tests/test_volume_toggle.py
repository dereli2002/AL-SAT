"""User-controlled liquidity filtering, live safety and honest backtest modes."""

from concurrent.futures import wait
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import threading
import unittest

from bot.backtest import compare_strategies, run_backtest
from bot.service import BotService
from bot.strategies import entry_signal_details, entry_signals, signals
from tests.test_added_strategies import (
    START, accounting_config, automatic_buy_fixtures, band_fixture,
    liquid_history, minute_candles,
)
from tests.test_service import Feed


class VolumeToggleSignalTests(unittest.TestCase):
    def test_off_allows_actual_buys_in_all_eight_strategies_without_liquidity(self):
        for strategy, history in automatic_buy_fixtures().items():
            frozen = deepcopy(history)
            with self.subTest(strategy=strategy):
                self.assertEqual(signals(history, strategy)[-1], 'buy')
                self.assertEqual(entry_signals(history, strategy)[-1], 'hold')
                self.assertEqual(entry_signals(history, strategy, volume_filter_enabled=False)[-1], 'buy')
                detail = entry_signal_details(history, strategy, volume_filter_enabled=False)
                self.assertEqual(detail['technical_signal'], 'buy')
                self.assertEqual(detail['signal'], 'buy')
                self.assertTrue(detail['entry_ready'])
                self.assertFalse(detail['liquidity']['enabled'])
                self.assertFalse(detail['liquidity']['eligible'])
                self.assertIsNone(detail['liquidity']['quote_volume_24h'])
                self.assertEqual(history, frozen)

    def test_on_and_off_keep_low_volume_facts_and_technical_sells(self):
        for strategy, original in automatic_buy_fixtures().items():
            history, context = liquid_history(original)
            for candle in history + context:
                candle['quote_volume'] = '1'
            with self.subTest(strategy=strategy):
                on = entry_signal_details(history, strategy, context_candles=context)
                off = entry_signal_details(history, strategy, context_candles=context,
                                           volume_filter_enabled=False)
                self.assertTrue(on['liquidity']['enabled'])
                self.assertEqual(on['signal'], 'hold')
                self.assertEqual(off['signal'], 'buy')
                self.assertEqual(on['liquidity']['quote_volume_24h'], off['liquidity']['quote_volume_24h'])
                self.assertFalse(off['liquidity']['eligible'])
                for index, decision in enumerate(signals(original, strategy)):
                    if decision == 'sell':
                        for enabled in (True, False):
                            self.assertEqual(entry_signals(original[:index + 1], strategy,
                                                           volume_filter_enabled=enabled)[-1], 'sell')

    def test_disabled_liquidity_filter_does_not_bypass_missing_vwap_session(self):
        history = minute_candles([100] * 9 + [90, 110], START + timedelta(minutes=1))
        detail = entry_signal_details(history, 'vwap_ema', volume_filter_enabled=False)
        self.assertEqual(detail['signal'], 'hold')
        self.assertFalse(detail['ready'])
        self.assertFalse(detail['entry_ready'])
        self.assertFalse(detail['liquidity']['enabled'])

    def test_signal_api_rejects_non_boolean_toggle_values(self):
        history = band_fixture(38)
        for value in (0, 1, 'false', 'true', None, [], {}):
            for method in (entry_signals, entry_signal_details):
                with self.subTest(method=method.__name__, value=value), self.assertRaises(ValueError):
                    method(history, 'rsi_band', volume_filter_enabled=value)


class VolumeToggleServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'paper.sqlite3'
        self.feed = Feed()
        self.service = BotService(self.path, feed=self.feed)
        self.service.configure(dict(data_source='demo', universe_mode='selected', symbols=['BTCUSDT'],
                                    strategy='rsi_reversion', daily_loss_limit_pct='0'))

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def settle(self):
        self.service.step()
        wait([future for _, future in self.service._pending.values()], timeout=3)
        self.service.step()

    def thin_feed(self):
        original = self.feed.candles

        def candles(symbol, interval, limit=100):
            bars = original(symbol, interval, limit)
            for row in bars:
                row['quote_volume'] = '1'
            return bars

        self.feed.candles = candles

    def test_old_saved_configuration_defaults_to_on_and_false_survives_restart(self):
        self.assertIs(self.service.config['volume_filter_enabled'], True)
        saved = self.service.store.load()
        del saved['config']['volume_filter_enabled']  # A real pre-toggle ledger shape.
        self.service.store.save(saved)
        self.service.close()
        self.service = BotService(self.path, feed=self.feed)
        self.assertIs(self.service.config['volume_filter_enabled'], True)
        ledger = deepcopy(self.service.portfolio.export_state())
        self.service.configure({'volume_filter_enabled': False})
        self.service.close()
        self.service = BotService(self.path, feed=self.feed)
        self.assertIs(self.service.config['volume_filter_enabled'], False)
        self.assertFalse(self.service.running)
        self.assertEqual(self.service.portfolio.export_state(), ledger)

    def test_invalid_toggle_is_atomic_and_cannot_be_combined_with_other_live_changes(self):
        self.settle()
        self.service.control('start')
        self.service.step()
        self.assertEqual(len(self.service.state()['positions']), 1)
        before = deepcopy((self.service.config, self.service.portfolio.export_state(), self.service.running))
        for value in (0, 1, 'false', 'true', None, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.service.configure({'volume_filter_enabled': value})
            self.assertEqual((self.service.config, self.service.portfolio.export_state(), self.service.running), before)
        with self.assertRaises(ValueError):
            self.service.configure({'volume_filter_enabled': False, 'strategy': 'breakout'})
        self.assertEqual((self.service.config, self.service.portfolio.export_state(), self.service.running), before)

    def test_hot_toggle_preserves_running_open_position_and_protective_state(self):
        self.settle()
        self.service.control('start')
        self.service.step()
        self.assertEqual(len(self.service.state()['positions']), 1)
        ledger = deepcopy(self.service.portfolio.export_state())
        guards = deepcopy((self.service.last_candles, self.service._manual_overrides,
                           self.service.emergency_active, self.service.emergency_revision))
        for enabled in (False, True, False):
            result = self.service.configure({'volume_filter_enabled': enabled})
            self.assertIs(result['config']['volume_filter_enabled'], enabled)
            self.assertTrue(result['status']['running'])
            self.assertEqual(self.service.portfolio.export_state(), ledger)
            self.assertEqual((self.service.last_candles, self.service._manual_overrides,
                              self.service.emergency_active, self.service.emergency_revision), guards)
        self.service.close()
        self.service = BotService(self.path, feed=self.feed)
        self.assertFalse(self.service.running)
        self.assertIs(self.service.config['volume_filter_enabled'], False)
        self.assertEqual(self.service.portfolio.export_state(), ledger)

    def test_reenabling_before_a_cached_buy_executes_blocks_that_buy(self):
        self.thin_feed()
        self.service.configure({'volume_filter_enabled': False})
        self.settle()
        self.assertEqual(self.service._cache['BTCUSDT']['details']['technical_signal'], 'buy')
        self.assertEqual(self.service._cache['BTCUSDT']['signal'], 'buy')
        self.service.control('start')
        self.service.configure({'volume_filter_enabled': True})
        self.service.step()
        self.assertTrue(self.service.running)
        self.assertEqual(self.service.state()['positions'], [])
        self.assertEqual(self.service.state()['account']['fees_paid'], '0')

    def test_toggle_during_quote_io_cannot_execute_an_old_off_mode_buy(self):
        self.thin_feed()
        self.service.configure({'volume_filter_enabled': False})
        self.settle()
        self.service.control('start')
        prices = self.feed.prices

        def quote_then_enable(symbols=None):
            self.service.configure({'volume_filter_enabled': True})
            return prices(symbols)

        self.feed.prices = quote_then_enable
        self.service.step()
        self.assertIs(self.service.config['volume_filter_enabled'], True)
        self.assertEqual(self.service.state()['positions'], [])
        self.assertEqual(self.service.state()['account']['fees_paid'], '0')

    def test_disabling_does_not_replay_a_consumed_candle_but_new_candle_can_buy(self):
        self.thin_feed()
        self.settle()
        self.service.control('start')
        self.service.step()
        self.assertEqual(self.service.state()['positions'], [])
        consumed = deepcopy(self.service.last_candles)
        self.service.configure({'volume_filter_enabled': False})
        self.assertEqual(self.service.state()['positions'], [])
        self.assertEqual(self.service.last_candles, consumed)
        self.settle()
        self.assertEqual(self.service.state()['positions'], [])
        old = self.feed.candles

        def next_candle(symbol, interval, limit=100):
            bars = old(symbol, interval, limit)
            for row in bars:
                for field in ('open_time', 'close_time'):
                    row[field] = (datetime.fromisoformat(row[field]) + timedelta(minutes=5)).isoformat()
            return bars

        self.feed.candles = next_candle
        self.service._next_due.clear()
        self.settle()
        self.assertEqual(len(self.service.state()['positions']), 1)

    def test_changing_mode_during_backtest_rejects_the_old_mode_report(self):
        entered, release = threading.Event(), threading.Event()
        original = self.feed.candles

        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('Test did not release its controlled candle request.')
            return original(*args, **kwargs)

        self.feed.candles = delayed
        outcome = []

        def backtest():
            try:
                outcome.append(self.service.test_history({'strategy': 'all', 'limit': 100}))
            except Exception as error:
                outcome.append(error)

        thread = threading.Thread(target=backtest)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            self.service.configure({'volume_filter_enabled': False})
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], ValueError)
        self.assertIsNone(self.service.backtest)
        self.assertFalse(self.service.running)
        self.assertEqual(self.service.state()['positions'], [])


class VolumeToggleBacktestTests(unittest.TestCase):
    def test_same_rsi_band_window_trades_only_with_filter_off_and_reports_mode(self):
        history = band_fixture(40)
        for index in (38, 39):
            for key in ('open', 'high', 'low', 'close'):
                history[index][key] = '105'
        history, context = liquid_history(history)
        for candle in history + context:
            candle['quote_volume'] = '1'
        settings = accounting_config()
        frozen = deepcopy((history, context, settings))
        on = run_backtest(history, {**settings, 'volume_filter_enabled': True}, 'rsi_band', context_candles=context)
        off = run_backtest(history, {**settings, 'volume_filter_enabled': False}, 'rsi_band', context_candles=context)
        self.assertEqual(on['trade_count'], 0)
        self.assertEqual(off['trade_count'], 1)
        self.assertIs(on['volume_filter_enabled'], True)
        self.assertIs(off['volume_filter_enabled'], False)
        self.assertTrue(any('AÇIK' in note for note in on['notes']))
        self.assertTrue(any('KAPALI' in note for note in off['notes']))
        self.assertEqual(on['fee_assumption'], off['fee_assumption'])
        self.assertEqual((on['test_start'], on['test_end']), (off['test_start'], off['test_end']))
        self.assertEqual((off['train_candles'], off['test_candles']), (28, 12))
        self.assertGreater(float(off['fees_paid']), 0)
        for enabled in (True, False):
            report = compare_strategies(history, {**settings, 'volume_filter_enabled': enabled}, context_candles=context)
            self.assertEqual(len(report['results']), 8)
            self.assertIs(report['volume_filter_enabled'], enabled)
            self.assertTrue(all(row['volume_filter_enabled'] is enabled for row in report['results']))
            self.assertTrue(all((row['test_start'], row['test_end']) == (off['test_start'], off['test_end'])
                                for row in report['results']))
        self.assertEqual((history, context, settings), frozen)


if __name__ == '__main__':
    unittest.main()
