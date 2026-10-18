from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bot.service import BotService
from bot.market import DemoMarket
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
        self.service.configure(dict(data_source='demo',strategy='rsi_reversion',daily_loss_limit_pct='0'))

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def enter(self):
        self.service.step()
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
        with self.assertRaises(ValueError):
            self.service.configure(dict(symbol='ETHUSDT'))

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
        self.assertEqual(3,len(report['results']))
        self.assertTrue(any(int(result['trade_count'])>0 for result in report['results']))
        self.assertEqual('estimate',report['fee_kind'])
        self.assertIsNotNone(self.service.state()['backtest'])


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
