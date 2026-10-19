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

from bot.service import BotService, Store
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
        self.assertEqual(4,len(report['results']))
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
        self.service.control('clear_emergency')
        self.assertFalse(self.service.state()['emergency']['active'])
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
            self.service.control('clear_emergency')
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
                with urlopen(base+'/api/symbols') as result:
                    self.assertEqual(3,len(json.load(result)['symbols']))
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
