from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import ThreadingHTTPServer
import json
from concurrent.futures import wait
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bot.service import BotService, Store, CHART_CACHE_SIZE, CHART_CACHE_TTL
from bot.market import DemoMarket
from bot.core import DEFAULT_CONFIG, PaperBroker
from app import Handler


class Feed:
    source='demo'
    is_synthetic=True

    def __init__(self):
        self.index=0
        self.price='100'
        self.fail=False

    def candles(self,symbol,interval,limit=100):
        start=datetime(2026,1,1,tzinfo=timezone.utc)
        return [dict(open_time=(start+timedelta(minutes=i)).isoformat(),
                     close_time=(start+timedelta(minutes=i+1,milliseconds=-1)).isoformat(),
                     open=str(200-i),high=str(201-i),low=str(198-i),close=str(199-i),volume='100')
                for i in range(100)]

    def symbols(self):
        return [dict(symbol=s,base_asset=s[:-4],quote_asset='USDT') for s in ('BTCUSDT','ETHUSDT','币安人生USDT')]

    def filters(self,symbol):
        return dict(step_size='0.00000001',min_qty='0.00000001',min_notional='1')

    def prices(self,symbols=None):
        if self.fail:
            raise ConnectionError('test feed unavailable')
        self.index+=1
        return dict(prices={s:self.price for s in symbols},updated_at=datetime.now(timezone.utc).isoformat())

    def sample(self,symbol,interval):
        if self.fail:
            raise ConnectionError('test feed unavailable')
        self.index+=1
        return dict(price=self.price,updated_at=(datetime(2026,1,2,tzinfo=timezone.utc)+timedelta(seconds=self.index)).isoformat(),
                    candles=self.candles(symbol,interval),filters={})


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.path=Path(self.directory.name)/'paper.sqlite3'
        self.feed=Feed()
        self.service=BotService(self.path,feed=self.feed)
        self.service.configure(dict(data_source='demo',universe_mode='selected',symbols=['BTCUSDT'],strategy='rsi_reversion',daily_loss_limit_pct='0'))

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def settle(self):
        self.service.step()
        wait([future for _,future in self.service._pending.values()],timeout=2)
        self.service.step()

    def enter(self):
        self.settle()
        self.service.control('start')
        self.service.step()
        self.assertIsNotNone(self.service.state()['position'])

    def test_automatic_entry_pause_keeps_stop_and_restores_ledger(self):
        self.enter()
        quantity=self.service.state()['position']['quantity']
        self.service.close()
        self.service=BotService(self.path,feed=self.feed)
        self.assertFalse(self.service.state()['status']['running'])
        self.assertEqual(quantity,self.service.state()['position']['quantity'])
        self.feed.price='95'
        self.service.step()  # paused, but fixed stop must still protect the position
        state=self.service.state()
        self.assertIsNone(state['position'])
        self.assertEqual(1,state['account']['trade_count'])
        self.assertLess(float(state['account']['realized_pnl']),0)

    def test_failed_market_read_does_not_change_ledger_or_trade(self):
        self.enter()
        before=self.service.broker.export_state()
        self.feed.fail=True
        self.feed.price='1'
        self.service.step()
        self.assertEqual(before,self.service.broker.export_state())
        self.assertFalse(self.service.state()['status']['fresh'])
        with self.assertRaises(ValueError):
            self.service.control('close_position')

    def test_invalid_config_and_position_restriction_are_atomic(self):
        before=self.service.state()['config']
        with self.assertRaises(ValueError):
            self.service.configure(dict(stop_loss_pct='NaN'))
        self.assertEqual(before,self.service.state()['config'])
        self.enter()
        self.service.control('pause')
        focused=self.service.configure(dict(symbol='ETHUSDT'))
        self.assertEqual('ETHUSDT',focused['config']['symbol'])
        self.assertEqual('BTCUSDT',focused['positions'][0]['symbol'])
        with self.assertRaises(ValueError):
            self.service.configure(dict(order_quote_amount='20'))

    def test_live_source_never_silently_becomes_demo(self):
        self.service.configure(dict(data_source='binance'))
        self.feed.fail=True
        self.service.step()
        self.assertEqual('binance',self.service.state()['market']['source'])
        self.assertFalse(self.service.state()['market']['is_synthetic'])

    def test_demo_backtest_uses_costs_and_produces_trades(self):
        self.service.feed=DemoMarket()
        report=self.service.test_history(dict(strategy='all',limit=200))
        self.assertTrue(report['is_synthetic'])
        self.assertEqual(5,len(report['results']))
        self.assertIn('trend_breakout', [result['strategy'] for result in report['results']])
        self.assertTrue(any(int(result['trade_count'])>0 for result in report['results']))
        self.assertEqual('estimate',report['fee_kind'])
        self.assertIsNotNone(self.service.state()['backtest'])


    def test_multiple_symbols_share_one_balance_and_close_independently(self):
        self.service.configure(dict(symbols=['BTCUSDT','ETHUSDT','币安人生USDT'],max_open_positions=2))
        self.enter()
        state=self.service.state()
        self.assertEqual(2,len(state['positions']))
        self.assertEqual(3,state['scanner']['total_symbols'])
        self.assertEqual(3,state['scanner']['scanned_symbols'])
        self.assertLess(float(state['account']['cash']),8100)
        self.assertGreater(float(state['account']['cash']),7900)
        one=state['positions'][0]['symbol']
        self.service.control('close_position',one)
        self.assertEqual(1,len(self.service.state()['positions']))
        self.assertEqual(one,self.service.state()['trades'][0]['symbol'])
        self.service.control('pause')
        self.service.control('close_all')
        self.assertEqual([],self.service.state()['positions'])

    def test_all_mode_discovers_exchange_catalog_and_unicode_symbols(self):
        self.service.configure(dict(universe_mode='all'))
        self.settle()
        state=self.service.state()
        self.assertEqual(3,state['scanner']['total_symbols'])
        self.assertIn('币安人生USDT',[market['symbol'] for market in state['markets']])
        self.assertEqual(3,len(self.service.symbols()['symbols']))

    def test_manual_buy_sell_while_automatic_running_uses_shared_cash(self):
        self.settle()
        self.service.control('start')
        # ETH is outside the selected strategy universe, but its batch quote is current.
        result=self.service.control('buy','ETHUSDT','200')
        self.assertTrue(result['status']['running'])
        self.assertEqual('ETHUSDT',result['positions'][0]['symbol'])
        self.assertEqual('manual',result['positions'][0]['entry_reason'])
        self.assertAlmostEqual(9800,float(result['account']['cash']),places=5)
        with self.assertRaises(ValueError):
            self.service.control('buy','ETHUSDT','200')
        sold=self.service.control('sell','ETHUSDT')
        self.assertEqual([],sold['positions'])
        self.assertEqual('manual_sell',sold['trades'][0]['reason'])
        self.assertIn('ETHUSDT',self.service._manual_overrides)
        with self.assertRaises(ValueError):
            self.service.control('sell','ETHUSDT')
        with self.assertRaises(ValueError):
            self.service.control('buy','BTCUSDT','NaN')
        with self.assertRaises(ValueError):
            self.service.control('buy','BTCUSDT','999999')

    def test_emergency_closes_everything_halts_and_requires_explicit_unlock(self):
        self.settle()
        self.service.control('buy','BTCUSDT','200')
        self.service.control('buy','ETHUSDT','200')
        self.service.control('start')
        closed=self.service.control('emergency_close')
        self.assertFalse(closed['status']['running'])
        self.assertTrue(closed['emergency']['active'])
        self.assertEqual([],closed['positions'])
        self.assertEqual([],closed['emergency']['pending_symbols'])
        self.assertEqual(2,len(closed['trades']))
        with self.assertRaises(ValueError):
            self.service.control('start')
        with self.assertRaises(ValueError):
            self.service.control('buy','BTCUSDT','100')
        self.service.control('clear_emergency',expected_emergency_revision=closed['emergency']['revision'])
        self.assertFalse(self.service.state()['emergency']['active'])
        self.assertFalse(self.service.state()['status']['running'])

    def test_delayed_unlock_cannot_clear_a_newer_emergency(self):
        first=self.service.control('emergency_close')
        revision=first['emergency']['revision']
        newer=self.service.control('emergency_close')
        self.assertEqual(revision+1,newer['emergency']['revision'])
        with self.assertRaisesRegex(ValueError,'Acil durum değişti'):
            self.service.control('clear_emergency',expected_emergency_revision=revision)
        state=self.service.state()
        self.assertTrue(state['emergency']['active'])
        self.assertEqual(revision+1,state['emergency']['revision'])
        self.assertFalse(state['status']['running'])
        unlocked=self.service.control('clear_emergency',expected_emergency_revision=revision+1)
        self.assertFalse(unlocked['emergency']['active'])
        self.assertFalse(unlocked['status']['running'])

    def test_unlock_requires_exact_integer_revision(self):
        state=self.service.control('emergency_close')
        revision=state['emergency']['revision']
        for invalid in (None,True,False,str(revision),float(revision),revision-1,revision+1):
            with self.subTest(revision=invalid), self.assertRaises(ValueError):
                self.service.control('clear_emergency',expected_emergency_revision=invalid)
            self.assertTrue(self.service.state()['emergency']['active'])
            self.assertEqual(revision,self.service.state()['emergency']['revision'])

    def test_emergency_revision_survives_restart_and_legacy_state_defaults_zero(self):
        self.assertEqual(0,self.service.state()['emergency']['revision'])
        self.service.control('emergency_close')
        self.service.control('emergency_close')
        self.service.close()
        self.service=BotService(self.path,feed=self.feed)
        state=self.service.state()
        self.assertTrue(state['emergency']['active'])
        self.assertEqual(2,state['emergency']['revision'])
        with self.assertRaises(ValueError):
            self.service.control('clear_emergency',expected_emergency_revision=1)
        self.service.control('clear_emergency',expected_emergency_revision=2)
        self.service.close()
        self.service=BotService(self.path,feed=self.feed)
        self.assertFalse(self.service.state()['emergency']['active'])
        self.assertEqual(2,self.service.state()['emergency']['revision'])
        old_payload=self.service.store.load()
        del old_payload['emergency_revision']
        self.service.store.save(old_payload)
        self.service.close()
        self.service=BotService(self.path,feed=self.feed)
        self.assertEqual(0,self.service.state()['emergency']['revision'])
        self.assertEqual(1,self.service.control('emergency_close')['emergency']['revision'])

    def test_emergency_revision_is_saved_before_liquidation(self):
        self.enter()
        def failed_sell(*args,**kwargs):
            persisted=self.service.store.load()
            self.assertTrue(persisted['emergency_active'])
            self.assertEqual(1,persisted['emergency_revision'])
            raise RuntimeError('liquidation interrupted')
        with patch.object(self.service.portfolio,'sell',side_effect=failed_sell):
            with self.assertRaisesRegex(RuntimeError,'liquidation interrupted'):
                self.service.control('emergency_close')
        self.assertTrue(self.service.state()['emergency']['active'])
        self.assertFalse(self.service.state()['status']['running'])

    def test_emergency_without_quotes_is_persisted_and_exits_when_prices_return(self):
        self.enter()
        self.feed.fail=True
        self.service.step()
        state=self.service.control('emergency_close')
        self.assertFalse(state['status']['running'])
        self.assertEqual(['BTCUSDT'],state['emergency']['pending_symbols'])
        self.assertEqual(0,state['account']['trade_count'])
        self.service.close()
        self.service=BotService(self.path,feed=self.feed)
        self.assertTrue(self.service.state()['emergency']['active'])
        self.assertEqual(1,len(self.service.state()['positions']))
        with self.assertRaises(ValueError):
            self.service.control('clear_emergency',expected_emergency_revision=state['emergency']['revision'])
        self.feed.fail=False
        self.service.step()
        state=self.service.state()
        self.assertEqual([],state['emergency']['pending_symbols'])
        self.assertEqual([],state['positions'])
        self.assertEqual(1,state['account']['trade_count'])
        self.assertEqual('emergency_close',state['trades'][0]['reason'])

    def test_removed_selected_symbol_never_blocks_other_protective_exits(self):
        self.service.configure(dict(symbols=['BTCUSDT','ETHUSDT']))
        self.enter()
        self.feed.symbols=lambda:[dict(symbol='BTCUSDT',base_asset='BTC',quote_asset='USDT')]
        self.feed.price='90'
        self.service.step()
        state=self.service.state()
        self.assertEqual([],state['positions'])
        self.assertTrue(state['status']['fresh'])
        self.assertIn('ETHUSDT',state['scanner']['warning'])
        self.assertEqual(2,state['account']['trade_count'])

    def test_metadata_outage_keeps_quote_based_emergency_exit_working(self):
        self.enter()
        self.feed.fail=True
        self.service.step()
        self.service.control('emergency_close')
        self.feed.fail=False
        def metadata_down():
            raise ConnectionError('catalog unavailable')
        self.feed.symbols=metadata_down
        self.service.step()
        state=self.service.state()
        self.assertEqual([],state['positions'])
        self.assertTrue(state['emergency']['active'])
        self.assertEqual([],state['emergency']['pending_symbols'])
        self.assertIn('yenilenemedi',state['scanner']['warning'])

    def test_slow_manual_metadata_cannot_fill_with_an_expired_quote(self):
        self.settle()
        original=self.feed.filters
        def slow_filters(symbol):
            self.service._last_success-=45
            return original(symbol)
        self.feed.filters=slow_filters
        before=self.service.portfolio.export_state()
        with self.assertRaises(ValueError):
            self.service.control('buy','ETHUSDT','100')
        self.assertEqual(before,self.service.portfolio.export_state())

    def test_backoff_applies_to_manual_buy_catalog_and_backtest(self):
        self.settle()
        self.service._backoff_until=__import__('time').monotonic()+60
        with patch.object(self.feed,'candles') as candles, patch.object(self.feed,'symbols') as symbols, patch.object(self.feed,'filters') as filters:
            with self.assertRaises(ValueError):
                self.service.test_history(dict(strategy='all',limit=200))
            self.assertIn('error',self.service.symbols())
            with self.assertRaises(ValueError):
                self.service.control('buy','ETHUSDT','100')
            candles.assert_not_called()
            symbols.assert_not_called()
            filters.assert_not_called()

    def test_backtest_throttle_is_recorded_for_other_public_calls(self):
        self.settle()
        class Limited(Exception):
            retry_after=60
        with patch.object(self.feed,'candles',side_effect=Limited('429')):
            with self.assertRaises(Limited):
                self.service.test_history(dict(strategy='all',limit=200))
        self.assertGreater(self.service._backoff_until,__import__('time').monotonic())
        self.assertFalse(self.service.state()['status']['fresh'])

    def test_chart_returns_immediate_history_without_affecting_open_portfolio(self):
        self.enter()
        before=self.service.portfolio.export_state()
        focused=self.service.configure(dict(symbol='ETHUSDT'))
        self.assertTrue(focused['status']['running'])
        with patch.object(self.feed,'candles',wraps=self.feed.candles) as candles:
            chart=self.service.chart('ETHUSDT','5m')
            self.assertEqual(100,len(chart['candles']))
            self.assertEqual('ETHUSDT',chart['symbol'])
            self.assertTrue(chart['quote_fresh'])
            self.assertFalse(chart['history_stale'])
            self.assertEqual('100',chart['price'])
            self.assertEqual(1,candles.call_count)
            again=self.service.chart('ETHUSDT','5m')
            self.assertTrue(again['cached'])
            self.assertEqual(1,candles.call_count)
        self.assertEqual(before,self.service.portfolio.export_state())
        self.assertTrue(self.service.state()['status']['running'])

    def test_chart_reuses_scanner_history_and_bounds_cache(self):
        self.settle()
        with patch.object(self.feed,'candles') as candles:
            chart=self.service.chart('BTCUSDT','5m')
            self.assertTrue(chart['cached'])
            self.assertEqual(100,len(chart['candles']))
            candles.assert_not_called()
        with self.service.lock:
            for index in range(CHART_CACHE_SIZE+10):
                self.service._remember_chart(f'COIN{index}USDT','1m',chart['candles'])
        self.assertEqual(CHART_CACHE_SIZE,len(self.service._chart_cache))
        self.assertNotIn(('BTCUSDT','5m'),self.service._chart_cache)

    def test_chart_focus_accepts_open_position_after_catalog_removal(self):
        self.enter()
        self.service._catalog=[pair for pair in self.service._catalog if pair['symbol']!='BTCUSDT']
        self.service.configure(dict(symbol='ETHUSDT'))
        state=self.service.configure(dict(symbol='BTCUSDT'))
        self.assertTrue(state['status']['running'])
        self.assertEqual('BTCUSDT',state['position']['symbol'])
        self.assertEqual('BTCUSDT',self.service.chart('BTCUSDT','5m')['symbol'])

    def test_chart_marks_cached_history_stale_when_network_fails_and_stale_quote(self):
        self.settle()
        self.service._chart_cache[('BTCUSDT','5m')]['fetched']-=CHART_CACHE_TTL+1
        self.service._last_success-=31
        with patch.object(self.feed,'candles',side_effect=ConnectionError('offline')):
            chart=self.service.chart('BTCUSDT','5m')
        self.assertEqual(100,len(chart['candles']))
        self.assertTrue(chart['history_stale'])
        self.assertFalse(chart['quote_fresh'])
        self.assertIn('offline',chart['error'])

    def test_chart_shares_backoff_and_records_rate_limits(self):
        self.settle()
        class Limited(Exception):
            retry_after=60
        with patch.object(self.feed,'candles',side_effect=Limited('429')) as candles:
            with self.assertRaises(Limited):
                self.service.chart('ETHUSDT','5m')
            self.assertGreater(self.service._backoff_until,__import__('time').monotonic())
            self.assertTrue(self.service.chart('BTCUSDT','5m')['history_stale'])
            with self.assertRaises(ValueError):
                self.service.chart('ETHUSDT','5m')
            self.assertEqual(1,candles.call_count)

    def test_chart_fetch_keeps_portfolio_lock_free_and_rejects_changed_source(self):
        self.settle()
        entered,release=threading.Event(),threading.Event()
        outcome=[]
        original=self.feed.candles
        def delayed(*args):
            entered.set()
            release.wait(2)
            return original(*args)
        def load():
            try:
                outcome.append(self.service.chart('ETHUSDT','5m'))
            except Exception as exc:
                outcome.append(exc)
        with patch.object(self.feed,'candles',side_effect=delayed):
            worker=threading.Thread(target=load)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(self.service.lock.acquire(timeout=.5))
                self.service.lock.release()
                state=self.service.control('emergency_close')
                self.assertTrue(state['emergency']['active'])
                self.service.configure(dict(data_source='binance'))
            finally:
                release.set()
                worker.join(2)
        self.assertEqual(1,len(outcome))
        self.assertIsInstance(outcome[0],ValueError)
        self.assertIn('ayarlar değişti',str(outcome[0]))
        self.assertEqual(0,len(self.service._chart_cache))

    def test_chart_rejects_invalid_symbol_and_interval(self):
        self.settle()
        for symbol,interval in (('BAD/USDT','5m'),('MISSINGUSDT','5m'),('ETHUSDT','2m')):
            with self.subTest(symbol=symbol,interval=interval), self.assertRaises(ValueError):
                self.service.chart(symbol,interval)

    def test_legacy_database_migrates_and_is_backed_up(self):
        config={**DEFAULT_CONFIG,'symbol':'BTCUSDT','interval':'5m','strategy':'ema_cross','data_source':'demo'}
        broker=PaperBroker(config)
        broker.buy('100','2026-01-01T00:00:00+00:00')
        broker.tick('105','2026-01-01T00:00:01+00:00')
        original=broker.snapshot('105')
        old_path=Path(self.directory.name)/'old.sqlite3'
        store=Store(old_path)
        store.save(dict(config=config,broker=broker.export_state(),last_candle=None,backtest=None))
        store.db.close()
        migrated=BotService(old_path,feed=self.feed)
        try:
            state=migrated.state()
            self.assertFalse(state['status']['running'])
            self.assertEqual(original['cash'],state['account']['cash'])
            self.assertEqual(original['position']['highest_price'],state['positions'][0]['highest_price'])
            self.assertEqual(original['position']['trailing_stop_price'],state['positions'][0]['trailing_stop_price'])
            self.assertTrue(Path(migrated.migration_backup).is_file())
            self.assertIn('portfolio',migrated.store.load())
            backup=Store(migrated.migration_backup)
            self.assertIn('broker',backup.load())
            backup.db.close()
        finally:
            migrated.close()

    def test_rate_limit_pauses_scanning_and_never_uses_previous_quote(self):
        self.enter()
        before=self.service.portfolio.export_state()
        class Limited(Exception):
            retry_after=60
        def blocked(symbols):
            raise Limited('HTTP 429')
        self.feed.prices=blocked
        self.service.step()
        self.assertEqual(before,self.service.portfolio.export_state())
        self.assertFalse(self.service.state()['status']['fresh'])
        self.service.step()
        self.assertIn('saniye',self.service.state()['status']['error'])


class HttpTests(unittest.TestCase):
    def test_emergency_unlock_http_checks_latest_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            service=BotService(Path(directory)/'db.sqlite3',feed=Feed())
            server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,service=service))
            worker=threading.Thread(target=server.serve_forever,daemon=True)
            worker.start()
            endpoint='http://127.0.0.1:'+str(server.server_port)+'/api/control'
            def post(payload):
                request=Request(endpoint,data=json.dumps(payload).encode(),method='POST',
                                headers={'Content-Type':'application/json'})
                with urlopen(request) as result:
                    return json.load(result)
            try:
                first=post(dict(action='emergency_close'))
                latest=post(dict(action='emergency_close'))
                for payload in (dict(action='clear_emergency'),
                                dict(action='clear_emergency',expected_emergency_revision=True),
                                dict(action='clear_emergency',expected_emergency_revision=first['emergency']['revision'])):
                    with self.subTest(payload=payload),self.assertRaises(HTTPError) as caught:
                        post(payload)
                    self.assertEqual(400,caught.exception.code)
                    self.assertTrue(service.state()['emergency']['active'])
                cleared=post(dict(action='clear_emergency',expected_emergency_revision=latest['emergency']['revision']))
                self.assertFalse(cleared['emergency']['active'])
                self.assertFalse(cleared['status']['running'])
            finally:
                server.shutdown()
                server.server_close()
                worker.join()
                service.close()

    def test_dashboard_api_validation_and_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            service=BotService(Path(directory)/'db.sqlite3',feed=Feed())
            server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,service=service))
            worker=threading.Thread(target=server.serve_forever,daemon=True)
            worker.start()
            base='http://127.0.0.1:'+str(server.server_port)
            try:
                with urlopen(base+'/api/state') as result:
                    self.assertEqual('estimate',json.load(result)['fee_info']['kind'])
                for path, expected in (('/positions.html',b'id="positionsGrid"'),
                                       ('/positions.js',b'close_position'),
                                       ('/positions.css',b'.positions-grid')):
                    with self.subTest(path=path), urlopen(base+path) as result:
                        self.assertEqual(200,result.status)
                        self.assertIn(expected,result.read())
                with urlopen(base+'/api/symbols') as result:
                    self.assertEqual(3,len(json.load(result)['symbols']))
                with urlopen(base+'/api/chart?symbol=ETHUSDT&interval=5m') as result:
                    chart=json.load(result)
                    self.assertEqual('ETHUSDT',chart['symbol'])
                    self.assertEqual(100,len(chart['candles']))
                with self.assertRaises(HTTPError) as caught:
                    urlopen(base+'/api/chart?symbol=INVALID&interval=5m')
                self.assertEqual(400,caught.exception.code)
                patch=Request(base+'/api/config',data=json.dumps(dict(data_source='demo')).encode(),
                              method='PATCH',headers={'Content-Type':'application/json'})
                with urlopen(patch) as result:
                    self.assertEqual('demo',json.load(result)['config']['data_source'])
                request=Request(base+'/api/control',data=b'{"action":"start"}',method='POST',
                                headers={'Content-Type':'application/json','Origin':'https://unrelated.example'})
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request)
                self.assertEqual(403,caught.exception.code)
                with self.assertRaises(HTTPError) as caught:
                    urlopen(base+'/.data/paper.sqlite3')
                self.assertEqual(404,caught.exception.code)
            finally:
                server.shutdown()
                server.server_close()
                worker.join()
                service.close()


if __name__=='__main__':
    unittest.main()
