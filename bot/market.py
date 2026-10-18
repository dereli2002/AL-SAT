"""Public Binance market data and an explicitly selected synthetic feed.

No authenticated endpoints or order submission exist in this application.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import math
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = 'https://data-api.binance.vision'
INTERVALS = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600}


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds')


def valid_market(symbol, interval):
    if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9]{2,16}USDT', symbol):
        raise ValueError('İşlem çifti USDT ile biten bir Binance Spot sembolü olmalı.')
    if interval not in INTERVALS:
        raise ValueError('Mum aralığı 1m, 5m, 15m veya 1h olmalı.')


class BinanceMarket:
    source = 'binance'
    is_synthetic = False

    def __init__(self):
        self._filters = {}

    def _get(self, path, params):
        request = Request(BASE_URL + path + '?' + urlencode(params),
                          headers={'User-Agent': 'SpotPaperBot/1.0', 'Accept': 'application/json'})
        with urlopen(request, timeout=12) as response:
            result = json.load(response)
        if isinstance(result, dict) and 'code' in result:
            raise ValueError('Binance veri isteğini reddetti: ' + str(result.get('msg', '')))
        return result

    def candles(self, symbol, interval, limit=500):
        valid_market(symbol, interval)
        if not 100 <= limit <= 1000:
            raise ValueError('Mum sayısı 100–1000 arasında olmalı.')
        raw = self._get('/api/v3/klines', {'symbol': symbol, 'interval': interval, 'limit': limit})
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        return [dict(open_time=iso(datetime.fromtimestamp(row[0]/1000, timezone.utc)),
                     close_time=iso(datetime.fromtimestamp(row[6]/1000, timezone.utc)),
                     open=str(row[1]), high=str(row[2]), low=str(row[3]),
                     close=str(row[4]), volume=str(row[5]))
                for row in raw if row[6] < now_ms]

    def filters(self, symbol):
        if symbol not in self._filters:
            data = self._get('/api/v3/exchangeInfo', {'symbol': symbol})
            pairs = data.get('symbols', [])
            if not pairs or pairs[0].get('status') != 'TRADING' or not pairs[0].get('isSpotTradingAllowed', True):
                raise ValueError('Sembol aktif bir Spot işlem çifti değil.')
            filters = {item['filterType']: item for item in pairs[0]['filters']}
            lot = filters.get('LOT_SIZE', {})
            notional = filters.get('NOTIONAL', filters.get('MIN_NOTIONAL', {}))
            self._filters[symbol] = dict(step_size=lot.get('stepSize', '0.00000001'),
                                        min_qty=lot.get('minQty', '0'),
                                        min_notional=notional.get('minNotional', '0'))
        return self._filters[symbol]

    def sample(self, symbol, interval):
        bars = self.candles(symbol, interval)
        ticker = self._get('/api/v3/ticker/price', {'symbol': symbol})
        price = Decimal(str(ticker['price']))
        if not price.is_finite() or price <= 0 or len(bars) < 60:
            raise ValueError('Yeterli ve geçerli piyasa verisi alınamadı.')
        return dict(price=str(price), updated_at=iso(datetime.now(timezone.utc)),
                    candles=bars, filters=self.filters(symbol))


class DemoMarket:
    """Deterministic accelerated candles; never represented as exchange data."""
    source = 'demo'
    is_synthetic = True

    def __init__(self):
        self.step = 0
        self.anchor = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    @staticmethod
    def _price(index, symbol):
        base = {'BTCUSDT': 60000, 'ETHUSDT': 3000, 'SOLUSDT': 150}.get(symbol, 100)
        return base * (1 + .055*math.sin(index/12) + .015*math.sin(index/3.7) + index*.00002)

    def candles(self, symbol, interval, limit=500):
        valid_market(symbol, interval)
        if not 100 <= limit <= 1000:
            raise ValueError('Mum sayısı 100–1000 arasında olmalı.')
        duration = INTERVALS[interval]
        output = []
        anchor = datetime.now(timezone.utc)
        for offset in range(limit):
            i = self.step + offset
            start = anchor + timedelta(seconds=(offset-limit)*duration)
            opened, closed = self._price(i, symbol), self._price(i+1, symbol)
            output.append(dict(open_time=iso(start), close_time=iso(start+timedelta(seconds=duration)-timedelta(milliseconds=1)),
                               open=f'{opened:.8f}', high=f'{max(opened,closed)*1.002:.8f}',
                               low=f'{min(opened,closed)*.998:.8f}', close=f'{closed:.8f}', volume='100'))
        return output

    def sample(self, symbol, interval):
        self.step += 1
        bars = self.candles(symbol, interval)
        return dict(price=bars[-1]['close'], updated_at=iso(datetime.now(timezone.utc)),
                    candles=bars, filters=dict(step_size='0.00000001', min_qty='0.00000001', min_notional='5'))
