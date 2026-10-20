from datetime import datetime, timezone
from decimal import Decimal
from email.message import Message
from email.utils import format_datetime
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from bot.market import BinanceMarket, DemoMarket, MarketRateLimitError, valid_market, valid_chart


def pair(symbol='BTCUSDT', *, status='TRADING', spot=True, quote='USDT', filters=None):
    return dict(symbol=symbol, baseAsset=symbol[:-len(quote)], quoteAsset=quote,
                status=status, isSpotTradingAllowed=spot, filters=filters or [])


class FakeAPI:
    def __init__(self, pairs, tickers=None):
        self.pairs = pairs
        self.tickers = tickers or []
        self.calls = []

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        if path == '/api/v3/exchangeInfo':
            selected = self.pairs
            if 'symbol' in params:
                selected = [row for row in selected if row['symbol'] == params['symbol']]
            return {'symbols': selected}
        if path == '/api/v3/ticker/price':
            return self.tickers
        raise AssertionError('Unexpected market request: ' + path)

    def calls_for(self, path):
        return [params for called, params in self.calls if called == path]


class SymbolTests(unittest.TestCase):
    def test_accepts_short_long_and_unicode_alphanumeric_usdt_assets(self):
        for symbol in ('AUSDT', '1USDT', '1INCHUSDT', '币USDT', 'ÅUSDT', 'A'*40+'USDT'):
            with self.subTest(symbol=symbol):
                valid_market(symbol, '1m')

    def test_rejects_path_control_empty_asset_and_other_quote_symbols(self):
        for symbol in ('USDT', 'A'*41+'USDT', 'BTCUSDC', '../BTCUSDT', 'BTC/USDT',
                       'BTC_USDT', 'BTC-USDT', 'BTC USDT', 'BTCUSDT\n', '\x00BTCUSDT',
                       'BTC\u200bUSDT', '', None, 123):
            with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                valid_market(symbol, '1m')
        with self.assertRaises(ValueError):
            valid_market('BTCUSDT', '2m')

    def test_catalog_contains_only_explicitly_active_spot_usdt_pairs(self):
        missing_spot = pair('ADAUSDT')
        missing_spot.pop('isSpotTradingAllowed')
        api = FakeAPI([pair(), pair('币USDT'), pair('ETHUSDT', status='BREAK'),
                       pair('SOLUSDT', spot=False), missing_spot,
                       pair('ETHBTC', quote='BTC'), pair('BAD/USDT')])
        market = BinanceMarket()
        market._get = api
        catalog = market.symbols()
        self.assertEqual({'BTCUSDT', '币USDT'}, {row['symbol'] for row in catalog})
        self.assertEqual({'symbol': 'BTCUSDT', 'base_asset': 'BTC', 'quote_asset': 'USDT'},
                         next(row for row in catalog if row['symbol'] == 'BTCUSDT'))
        self.assertEqual([{}], api.calls_for('/api/v3/exchangeInfo'))

    def test_catalog_and_pair_filters_share_cache_and_force_refresh(self):
        api = FakeAPI([pair(filters=[dict(filterType='LOT_SIZE', stepSize='0.001',
                                          minQty='0.001', maxQty='10')])])
        market = BinanceMarket()
        market._get = api
        market.symbols()
        market.symbols()
        market.filters('BTCUSDT')
        market.filters('BTCUSDT')
        self.assertEqual([{}], api.calls_for('/api/v3/exchangeInfo'))
        api.pairs.append(pair('ETHUSDT'))
        self.assertEqual(1, len(market.symbols()))
        self.assertEqual(2, len(market.symbols(force=True)))
        self.assertEqual([{}, {}], api.calls_for('/api/v3/exchangeInfo'))

    def test_catalog_expires_at_600_seconds_not_before(self):
        api = FakeAPI([pair()])
        market = BinanceMarket()
        market._get = api
        with patch('bot.market.time.monotonic', return_value=100) as clock:
            self.assertEqual(1, len(market.symbols()))
            api.pairs.append(pair('ETHUSDT'))
            clock.return_value = 699
            self.assertEqual(1, len(market.symbols()))
            self.assertEqual(1, len(api.calls))
            clock.return_value = 700
            self.assertEqual(2, len(market.symbols()))
        self.assertEqual([{}, {}], api.calls_for('/api/v3/exchangeInfo'))

    def test_concurrent_catalog_reads_share_a_single_inflight_request(self):
        market = BinanceMarket()
        start = threading.Barrier(5)
        fetch_started = threading.Event()
        release_response = threading.Event()
        all_callers_started = threading.Event()
        counter_lock = threading.Lock()
        calls = []
        callers = 0

        def fake_get(path, params):
            with counter_lock:
                calls.append((path, dict(params)))
            fetch_started.set()
            if not release_response.wait(timeout=2):
                raise AssertionError('Test did not release fake network response')
            return {'symbols': [pair()]}

        def read_catalog():
            nonlocal callers
            start.wait(timeout=2)
            with counter_lock:
                callers += 1
                if callers == 4:
                    all_callers_started.set()
            return market.symbols()

        market._get = fake_get
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = [pool.submit(read_catalog) for _ in range(4)]
            try:
                start.wait(timeout=2)
                self.assertTrue(fetch_started.wait(timeout=2))
                self.assertTrue(all_callers_started.wait(timeout=2))
                # Keep the response in flight while every caller can enter
                # symbols(); Event.wait releases the GIL like network I/O.
                release_response.wait(timeout=.05)
            finally:
                release_response.set()
            results = [future.result(timeout=2) for future in pending]
        self.assertEqual([('/api/v3/exchangeInfo', {})], calls)
        self.assertTrue(all(result == results[0] for result in results))

    def test_catalog_and_filter_results_are_defensive_copies(self):
        market = BinanceMarket()
        market._get = FakeAPI([pair(filters=[dict(filterType='LOT_SIZE', stepSize='0.01')])])
        catalog = market.symbols()
        original = [dict(row) for row in catalog]
        catalog[0]['symbol'] = 'CORRUPTEDUSDT'
        catalog.append(dict(symbol='UNKNOWNUSDT'))
        filters = market.filters('BTCUSDT')
        expected_step = filters['step_size']
        filters['step_size'] = '999'
        self.assertEqual(original, market.symbols())
        self.assertEqual(expected_step, market.filters('BTCUSDT')['step_size'])


class PriceTests(unittest.TestCase):
    def market(self, tickers):
        api = FakeAPI([pair(), pair('ETHUSDT'), pair('SOLUSDT')], tickers)
        market = BinanceMarket()
        market._get = api
        return market, api

    def test_batch_prices_use_one_all_ticker_request_and_utc_timestamp(self):
        market, api = self.market([dict(symbol='BTCUSDT', price='61000.125'),
                                   dict(symbol='ETHUSDT', price='3100')])
        result = market.prices(['BTCUSDT', 'ETHUSDT'])
        self.assertEqual({'BTCUSDT': Decimal('61000.125'), 'ETHUSDT': Decimal('3100')},
                         {symbol: Decimal(value) for symbol, value in result['prices'].items()})
        self.assertTrue(all(isinstance(value, str) for value in result['prices'].values()))
        self.assertEqual([], result['missing_symbols'])
        timestamp = datetime.fromisoformat(result['updated_at'].replace('Z', '+00:00'))
        self.assertEqual(timezone.utc.utcoffset(timestamp), timestamp.utcoffset())
        self.assertEqual([{}], api.calls_for('/api/v3/ticker/price'))

    def test_invalid_prices_and_symbols_outside_spot_catalog_are_discarded(self):
        market, api = self.market([dict(symbol='BTCUSDT', price='10'),
                                   dict(symbol='ETHUSDT', price='NaN'),
                                   dict(symbol='SOLUSDT', price='Infinity'),
                                   dict(symbol='UNKNOWNUSDT', price='123'),
                                   dict(symbol='ETHBTC', price='0.5')])
        self.assertEqual({'BTCUSDT': Decimal('10')},
                         {key: Decimal(value) for key, value in market.prices()['prices'].items()})
        self.assertEqual([{}], api.calls_for('/api/v3/ticker/price'))

    def test_no_usable_price_is_an_error(self):
        for value in ('0', '-1', 'NaN', 'Infinity', '-Infinity', 'invalid'):
            market, _ = self.market([dict(symbol='BTCUSDT', price=value)])
            with self.subTest(value=value), self.assertRaises(ValueError):
                market.prices(['BTCUSDT'])

    def test_malformed_ticker_symbols_are_discarded_without_unhashable_errors(self):
        market, _ = self.market([dict(symbol=['BTCUSDT'], price='1'),
                                 dict(symbol={'name': 'BTCUSDT'}, price='2'),
                                 dict(symbol=None, price='3'),
                                 dict(symbol='BTCUSDT', price='10')])
        result = market.prices()['prices']
        self.assertEqual({'BTCUSDT': Decimal('10')},
                         {key: Decimal(value) for key, value in result.items()})

    def test_no_requested_price_or_malformed_market_is_an_error(self):
        for requested in (['BTCUSDT'], ['SCAMUSDT'], ['ETHBTC'], ['BTC/USDT']):
            market, _ = self.market([dict(symbol='ETHUSDT', price='2000')])
            with self.subTest(requested=requested), self.assertRaises(ValueError):
                market.prices(requested)

    def test_explicit_delisted_symbol_can_be_priced_without_entering_active_catalog(self):
        market, _ = self.market([dict(symbol='BTCUSDT', price='61000'),
                                 dict(symbol='OLDUSDT', price='0.12')])
        requested = market.prices(['OLDUSDT'])
        self.assertEqual({'OLDUSDT': Decimal('0.12')},
                         {key: Decimal(value) for key, value in requested['prices'].items()})
        self.assertEqual([], requested['missing_symbols'])
        self.assertEqual({'BTCUSDT'}, set(market.prices()['prices']))
        self.assertNotIn('OLDUSDT', {row['symbol'] for row in market.symbols()})

    def test_partial_requested_prices_report_sorted_missing_or_invalid_quotes(self):
        market, api = self.market([dict(symbol='BTCUSDT', price='61000'),
                                   dict(symbol='SOLUSDT', price='0')])
        result = market.prices(['SOLUSDT', 'BTCUSDT', 'SCAMUSDT', 'ETHUSDT'])
        self.assertEqual({'BTCUSDT'}, set(result['prices']))
        self.assertEqual(['ETHUSDT', 'SCAMUSDT', 'SOLUSDT'], result['missing_symbols'])
        self.assertEqual([{}], api.calls_for('/api/v3/ticker/price'))


class FilterTests(unittest.TestCase):
    def filters(self, rules):
        market = BinanceMarket()
        api = FakeAPI([pair(filters=rules)])
        market._get = api
        return market, api

    def test_market_order_intersection_obeys_both_lot_steps_and_tighter_limits(self):
        market, api = self.filters([
            dict(filterType='LOT_SIZE', stepSize='0.002', minQty='0.005', maxQty='10'),
            dict(filterType='MARKET_LOT_SIZE', stepSize='0.003', minQty='0.01', maxQty='3'),
            dict(filterType='MIN_NOTIONAL', minNotional='5', applyToMarket=True),
            dict(filterType='NOTIONAL', minNotional='10', maxNotional='500',
                 applyMinToMarket=True, applyMaxToMarket=True),
        ])
        result = market.filters('BTCUSDT')
        for name, value in dict(step_size='0.006', min_qty='0.01', max_qty='3',
                                min_notional='10', max_notional='500').items():
            self.assertEqual(Decimal(value), Decimal(result[name]), name)
        self.assertEqual(result, market.filters('BTCUSDT'))
        self.assertEqual(1, len(api.calls_for('/api/v3/exchangeInfo')))

    def test_disabled_market_constraints_do_not_override_applicable_filters(self):
        market, _ = self.filters([
            dict(filterType='LOT_SIZE', stepSize='0.01', minQty='0.02', maxQty='100'),
            dict(filterType='MARKET_LOT_SIZE', stepSize='0', minQty='0', maxQty='0'),
            dict(filterType='MIN_NOTIONAL', minNotional='5000', applyToMarket=False),
            dict(filterType='NOTIONAL', minNotional='20', maxNotional='1000',
                 applyMinToMarket=True, applyMaxToMarket=False),
        ])
        result = market.filters('BTCUSDT')
        self.assertEqual(Decimal('0.01'), Decimal(result['step_size']))
        self.assertEqual(Decimal('0.02'), Decimal(result['min_qty']))
        self.assertEqual(Decimal('100'), Decimal(result['max_qty']))
        self.assertEqual(Decimal('20'), Decimal(result['min_notional']))
        self.assertEqual(Decimal('0'), Decimal(result['max_notional']))

    def test_notional_minimum_can_be_disabled_independently_of_maximum(self):
        market, _ = self.filters([
            dict(filterType='MIN_NOTIONAL', minNotional='5', applyToMarket=True),
            dict(filterType='NOTIONAL', minNotional='100', maxNotional='1000',
                 applyMinToMarket=False, applyMaxToMarket=True),
        ])
        result = market.filters('BTCUSDT')
        self.assertEqual(Decimal('5'), Decimal(result['min_notional']))
        self.assertEqual(Decimal('1000'), Decimal(result['max_notional']))

    def test_inactive_pair_cannot_supply_filters(self):
        market = BinanceMarket()
        market._get = FakeAPI([pair(status='BREAK')])
        with self.assertRaises(ValueError):
            market.filters('BTCUSDT')

    def test_zero_step_constraints_remain_disabled(self):
        market, _ = self.filters([
            dict(filterType='LOT_SIZE', stepSize='0', minQty='0', maxQty='0'),
            dict(filterType='MARKET_LOT_SIZE', stepSize='0', minQty='0', maxQty='0'),
        ])
        result = market.filters('BTCUSDT')
        self.assertEqual(Decimal('0'), Decimal(result['step_size']))
        self.assertEqual(Decimal('0'), Decimal(result['min_qty']))
        self.assertEqual(Decimal('0'), Decimal(result['max_qty']))

    def test_pair_filter_cache_expires_at_600_seconds(self):
        market, api = self.filters([
            dict(filterType='LOT_SIZE', stepSize='0.01', minQty='0', maxQty='0'),
        ])
        with patch('bot.market.time.monotonic', return_value=100) as clock:
            self.assertEqual(Decimal('0.01'), Decimal(market.filters('BTCUSDT')['step_size']))
            api.pairs[0]['filters'][0]['stepSize'] = '0.1'
            clock.return_value = 699
            self.assertEqual(Decimal('0.01'), Decimal(market.filters('BTCUSDT')['step_size']))
            self.assertEqual(1, len(api.calls))
            clock.return_value = 700
            self.assertEqual(Decimal('0.1'), Decimal(market.filters('BTCUSDT')['step_size']))
        self.assertEqual(2, len(api.calls_for('/api/v3/exchangeInfo')))


class RateLimitTests(unittest.TestCase):
    def test_http_throttle_exposes_positive_retry_delay_without_network(self):
        for code, header, expected in ((429, '123', 123), (418, '7', 7),
                                       (429, None, 60), (429, 'invalid', 60),
                                       (429, '0', 60), (429, '-5', 60)):
            headers = Message()
            if header is not None:
                headers['Retry-After'] = header
            error = HTTPError('https://example.invalid', code, 'limited', headers, None)
            with self.subTest(code=code, header=header):
                with patch('bot.market.urlopen', side_effect=error) as request:
                    with self.assertRaises(MarketRateLimitError) as caught:
                        BinanceMarket()._get('/api/v3/ticker/price', {})
                self.assertEqual(expected, caught.exception.retry_after)
                self.assertIsInstance(caught.exception.retry_after, int)
                request.assert_called_once()

    def test_http_date_retry_after_uses_utc_delay(self):
        now = datetime(2026, 1, 1, 12, 0, 0, 250000, tzinfo=timezone.utc)
        headers = Message()
        headers['Retry-After'] = format_datetime(
            datetime(2026, 1, 1, 12, 1, 30, tzinfo=timezone.utc), usegmt=True)
        error = HTTPError('https://example.invalid', 429, 'limited', headers, None)
        with patch('bot.market.datetime') as clock:
            clock.now.return_value = now
            with patch('bot.market.urlopen', side_effect=error):
                with self.assertRaises(MarketRateLimitError) as caught:
                    BinanceMarket()._get('/api/v3/ticker/price', {})
        self.assertEqual(90, caught.exception.retry_after)

    def test_other_http_errors_are_not_misreported_as_rate_limits(self):
        error = HTTPError('https://example.invalid', 503, 'unavailable', Message(), None)
        with patch('bot.market.urlopen', side_effect=error):
            with self.assertRaises(HTTPError):
                BinanceMarket()._get('/api/v3/ticker/price', {})


class DemoMarketTests(unittest.TestCase):
    def test_demo_catalog_contains_ten_unique_popular_usdt_assets(self):
        catalog = DemoMarket().symbols()
        symbols = {row['symbol'] for row in catalog}
        self.assertEqual(10, len(catalog))
        self.assertEqual(10, len(symbols))
        self.assertTrue({'BTCUSDT', 'ETHUSDT', 'SOLUSDT'} <= symbols)
        self.assertTrue(all(row['quote_asset'] == 'USDT' for row in catalog))

    def test_one_batch_advances_once_and_its_prices_match_current_candles(self):
        market = DemoMarket()
        before = market.step
        result = market.prices(['BTCUSDT', 'ETHUSDT', 'SOLUSDT'])
        self.assertEqual([], result['missing_symbols'])
        self.assertEqual(before + 1, market.step)
        for symbol, price in result['prices'].items():
            self.assertEqual(Decimal(price), Decimal(market.candles(symbol, '1m', 500)[-1]['close']))
        self.assertEqual(before + 1, market.step)

    def test_candles_are_repeatable_per_step_including_timestamps(self):
        market = DemoMarket()
        market.prices(['BTCUSDT'])
        first = market.candles('BTCUSDT', '5m', 500)
        second = market.candles('BTCUSDT', '5m', 500)
        self.assertEqual(first, second)
        self.assertEqual(500, len(first))
        self.assertLess(first[0]['open_time'], first[-1]['open_time'])
        for candle in first:
            self.assertLessEqual(Decimal(candle['low']), Decimal(candle['open']))
            self.assertLessEqual(Decimal(candle['low']), Decimal(candle['close']))
            self.assertGreaterEqual(Decimal(candle['high']), Decimal(candle['open']))
            self.assertGreaterEqual(Decimal(candle['high']), Decimal(candle['close']))

    def test_assets_have_different_percentage_price_paths(self):
        market = DemoMarket()
        first = market.prices(['BTCUSDT', 'ETHUSDT'])['prices']
        second = market.prices(['BTCUSDT', 'ETHUSDT'])['prices']
        btc_return = Decimal(second['BTCUSDT']) / Decimal(first['BTCUSDT'])
        eth_return = Decimal(second['ETHUSDT']) / Decimal(first['ETHUSDT'])
        self.assertGreater(abs(btc_return - eth_return), Decimal('0.00000001'))

    def test_sample_remains_compatible_with_price_candles_and_filters(self):
        market = DemoMarket()
        result = market.sample('BTCUSDT', '1m')
        self.assertEqual(Decimal(result['price']), Decimal(result['candles'][-1]['close']))
        self.assertEqual(result['filters'], market.filters('BTCUSDT'))
        self.assertEqual(500, len(result['candles']))
        self.assertTrue(market.is_synthetic)
        self.assertEqual('demo', market.source)


class DisplayCandleTests(unittest.TestCase):
    def test_long_strategy_history_pages_closed_candles_without_changing_chart_limit(self):
        instant=datetime(2026,1,2,12,0,30,tzinfo=timezone.utc)
        minute=int(instant.timestamp())//60*60000
        rows=[[minute-index*60000,'100','102','99','101','10',minute-(index-1)*60000-1,str(index)+'.12']
              for index in range(1600,-1,-1)]
        calls=[]
        def api(path,params):
            calls.append((path,dict(params)))
            eligible=[row for row in rows if row[6]<=params['endTime']]
            return eligible[-params['limit']:]
        market=BinanceMarket()
        with patch.object(market,'_get',side_effect=api),patch('bot.market.datetime') as clock:
            clock.now.return_value=instant
            clock.fromtimestamp.side_effect=datetime.fromtimestamp
            result=market.candles('BTCUSDT','1m',1540)
            with self.assertRaises(ValueError):
                market.chart_candles('BTCUSDT','1m',1540)
        self.assertEqual(1540,len(result))
        self.assertEqual([1000,540],[params['limit'] for _,params in calls])
        self.assertEqual(minute-1,calls[0][1]['endTime'])
        self.assertEqual(minute-1000*60000-1,calls[1][1]['endTime'])
        self.assertEqual(datetime.fromtimestamp((minute-1540*60000)/1000,timezone.utc),
                         datetime.fromisoformat(result[0]['open_time']))
        self.assertEqual(datetime.fromtimestamp((minute-1)/1000,timezone.utc),
                         datetime.fromisoformat(result[-1]['close_time']))
        self.assertEqual(1540,len({row['open_time'] for row in result}))
        self.assertEqual('1540.12',result[0]['quote_volume'])
        self.assertEqual('1.12',result[-1]['quote_volume'])

    def test_exchange_quote_volume_is_preserved_exactly_and_never_invented(self):
        instant=datetime(2026,1,2,12,tzinfo=timezone.utc)
        epoch=int(instant.timestamp()*1000)
        rows=[[epoch-120000,'1','3','.5','2','10',epoch-60001,'123456.7890123456789'],
              [epoch-60000,'1','3','.5','2','10',epoch-1]]
        market=BinanceMarket()
        with patch.object(market,'_get',return_value=rows),patch('bot.market.datetime') as clock:
            clock.now.return_value=instant
            clock.fromtimestamp.side_effect=datetime.fromtimestamp
            result=market.candles('BTCUSDT','1m',100)
        self.assertEqual('123456.7890123456789',result[0]['quote_volume'])
        self.assertNotIn('quote_volume',result[1])

    def test_invalid_exchange_quote_volume_is_unknown_for_single_and_paged_history(self):
        instant=datetime(2026,1,2,12,tzinfo=timezone.utc)
        epoch=int(instant.timestamp()*1000)
        for limit in (100,1540):
            for volume in ('NaN','Infinity','-1','not-a-number',None):
                row=[epoch-60000,'1','3','.5','2','10',epoch-1,volume]
                market=BinanceMarket()
                with self.subTest(limit=limit,volume=volume),patch.object(market,'_get',return_value=[row]),patch('bot.market.datetime') as clock:
                    clock.now.return_value=instant
                    clock.fromtimestamp.side_effect=datetime.fromtimestamp
                    result=market.candles('BTCUSDT','1m',limit)
                    self.assertEqual(1,len(result))
                    self.assertIsNone(result[0]['quote_volume'])
                    self.assertEqual('2',result[0]['close'])

    def test_paged_strategy_history_rejects_overlap_future_or_unordered_response(self):
        instant=datetime(2026,1,2,12,tzinfo=timezone.utc)
        epoch=int(instant.timestamp()*1000)
        valid=[epoch-60000,'1','3','.5','2','10',epoch-1]
        for rows in ([valid,valid], [[epoch,'1','3','.5','2','10',epoch+59999]]):
            market=BinanceMarket()
            with self.subTest(rows=rows),patch.object(market,'_get',return_value=rows),patch('bot.market.datetime') as clock:
                clock.now.return_value=instant
                clock.fromtimestamp.side_effect=datetime.fromtimestamp
                with self.assertRaises(ValueError):
                    market.candles('BTCUSDT','1m',1540)

    def test_demo_extended_history_preserves_the_requested_suffix(self):
        market=DemoMarket()
        extended=market.candles('BTCUSDT','1m',1940)
        self.assertEqual(1940,len(extended))
        self.assertEqual(market.candles('BTCUSDT','1m',500),extended[-500:])
        self.assertTrue(all(Decimal(row['quote_volume'])>0 for row in extended))
        with self.assertRaises(ValueError):
            market.chart_candles('BTCUSDT','1m',1940)

    def test_second_interval_is_display_only(self):
        valid_chart('BTCUSDT', '1s')
        with self.assertRaises(ValueError):
            valid_market('BTCUSDT', '1s')
        with self.assertRaises(ValueError):
            BinanceMarket().candles('BTCUSDT', '1s', 100)
        with self.assertRaises(ValueError):
            DemoMarket().candles('BTCUSDT', '1s', 100)
        for interval in ('0s', '2s', '1d', None):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                valid_chart('BTCUSDT', interval)

    def test_actual_ohlc_preserved_and_unfinished_candle_never_enters_closed_series(self):
        instant=datetime(2026, 1, 1, 12, 0, 0, 500000, tzinfo=timezone.utc)
        epoch=int(instant.timestamp()*1000)
        rows=[[epoch-1500, '100', '104', '99', '102', '17', epoch-501],
              [epoch-500, '102', '106', '101', '105', '21', epoch+499],
              [epoch+500, '105', '110', '104', '109', '3', epoch+1499]]
        market=BinanceMarket()
        with patch.object(market,'_get',return_value=rows) as request:
            with patch('bot.market.datetime') as clock:
                clock.now.return_value=instant
                clock.fromtimestamp.side_effect=datetime.fromtimestamp
                result=market.chart_candles('BTCUSDT','1s',100)
        request.assert_called_once_with('/api/v3/klines',dict(symbol='BTCUSDT',interval='1s',limit=100))
        self.assertEqual(1,len(result['candles']))
        self.assertEqual('102',result['candles'][0]['close'])
        self.assertEqual(dict(open='102',high='106',low='101',close='105',volume='21'),
                         {key:result['live_candle'][key] for key in ('open','high','low','close','volume')})
        self.assertNotIn('109',[row['close'] for row in result['candles']])

    def test_strategy_candles_stay_closed_even_though_chart_includes_forming_candle(self):
        instant=datetime(2026,1,1,tzinfo=timezone.utc)
        epoch=int(instant.timestamp()*1000)
        rows=[[epoch-60000,'1','3','.5','2','10',epoch-1],
              [epoch,'2','5','1','4','11',epoch+59999]]
        market=BinanceMarket()
        with patch.object(market,'_get',return_value=rows),patch('bot.market.datetime') as clock:
            clock.now.return_value=instant
            clock.fromtimestamp.side_effect=datetime.fromtimestamp
            self.assertEqual(['2'],[row['close'] for row in market.candles('BTCUSDT','1m',100)])
            self.assertEqual('4',market.chart_candles('BTCUSDT','1m',100)['live_candle']['close'])

    def test_demo_second_chart_is_explicitly_synthetic_and_repeatable(self):
        market=DemoMarket()
        first=market.chart_candles('ETHUSDT','1s',100)
        self.assertEqual(first,market.chart_candles('ETHUSDT','1s',100))
        self.assertIsNone(first['live_candle'])
        self.assertEqual(100,len(first['candles']))
        beginning=datetime.fromisoformat(first['candles'][0]['open_time'])
        next_time=datetime.fromisoformat(first['candles'][1]['open_time'])
        self.assertEqual(1,(next_time-beginning).total_seconds())
        self.assertTrue(market.is_synthetic)


if __name__ == '__main__':
    unittest.main()
