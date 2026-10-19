"""Public Binance market data and an explicitly selected synthetic feed.

No authenticated endpoints or order submission exist in this application.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from email.utils import parsedate_to_datetime
import json
import math
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = 'https://data-api.binance.vision'
INTERVALS = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600}
CHART_INTERVALS = {'1s': 1, **INTERVALS}
METADATA_TTL = 600


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds')


def valid_market(symbol, interval):
    # Binance also lists asset names containing non-ASCII letters. Structural
    # validation permits those names; the current exchange catalog determines
    # whether an actual, active Spot pair exists.
    if (not isinstance(symbol, str) or not symbol.endswith('USDT')
            or not 1 <= len(symbol[:-4]) <= 40 or not symbol[:-4].isalnum()):
        raise ValueError('İşlem çifti USDT ile biten bir Binance Spot sembolü olmalı.')
    if not isinstance(interval, str) or interval not in INTERVALS:
        raise ValueError('Mum aralığı 1m, 5m, 15m veya 1h olmalı.')


def valid_chart(symbol, interval):
    # Display-only second candles must never become an automation timeframe.
    valid_market(symbol, '1m')
    if not isinstance(interval, str) or interval not in CHART_INTERVALS:
        raise ValueError('Grafik aralığı 1s, 1m, 5m, 15m veya 1h olmalı.')


class MarketRateLimitError(RuntimeError):
    """A public-data throttle whose retry interval the worker must respect."""

    def __init__(self, message='Binance piyasa verisi istek sınırına ulaşıldı.', retry_after=60):
        super().__init__(message)
        try:
            delay = int(retry_after)
            self.retry_after = delay if delay > 0 else 60
        except (ValueError, TypeError, OverflowError):
            self.retry_after = 60


def _retry_after(headers):
    value = headers.get('Retry-After') if headers is not None else None
    if value is None:
        return 60
    try:
        delay = Decimal(str(value))
        if delay.is_finite() and 0 < delay <= 604800:
            return max(1, int(delay.to_integral_value(rounding=ROUND_CEILING)))
    except InvalidOperation:
        pass
    try:
        instant = parsedate_to_datetime(str(value))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        delay = (instant - datetime.now(timezone.utc)).total_seconds()
        if 0 <= delay <= 604800:
            return max(1, math.ceil(delay))
    except (ValueError, TypeError, OverflowError):
        pass
    return 60


def _nonnegative(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('Borsa miktar veya tutar kuralı geçersiz.') from exc
    if not result.is_finite() or result < 0 or (result and abs(result.adjusted()) > 36):
        raise ValueError('Borsa miktar veya tutar kuralı geçersiz.')
    return result


def _filter_values(pair):
    rules = {item['filterType']: item for item in pair.get('filters', [])
             if isinstance(item, dict) and isinstance(item.get('filterType'), str)}
    lots = [rules[name] for name in ('LOT_SIZE', 'MARKET_LOT_SIZE') if name in rules]
    minimums = [_nonnegative(lot.get('minQty', '0')) for lot in lots]
    maximums = [_nonnegative(lot.get('maxQty', '0')) for lot in lots]
    maximums = [value for value in maximums if value]
    steps = [_nonnegative(lot.get('stepSize', '0')) for lot in lots]
    steps = [value for value in steps if value]
    if steps:
        # An order must satisfy both nonzero lot grids. Choosing only the
        # larger step would fail for rules such as 0.002 and 0.003.
        precision = max(0, max(-value.as_tuple().exponent for value in steps))
        scale = Decimal(10) ** precision
        step = Decimal(math.lcm(*(int(value * scale) for value in steps))) / scale
    else:
        step = Decimal(0) if lots else Decimal('0.00000001')

    min_notionals = []
    max_notionals = []
    minimum = rules.get('MIN_NOTIONAL', {})
    if minimum and minimum.get('applyToMarket', True):
        min_notionals.append(_nonnegative(minimum.get('minNotional', '0')))
    notional = rules.get('NOTIONAL', {})
    if notional:
        if notional.get('applyMinToMarket', True):
            min_notionals.append(_nonnegative(notional.get('minNotional', '0')))
        if notional.get('applyMaxToMarket', True):
            value = _nonnegative(notional.get('maxNotional', '0'))
            if value:
                max_notionals.append(value)
    result = dict(step_size=step, min_qty=max(minimums, default=Decimal(0)),
                  max_qty=min(maximums, default=Decimal(0)),
                  min_notional=max(min_notionals, default=Decimal(0)),
                  max_notional=min(max_notionals, default=Decimal(0)))
    for low, high in (('min_qty', 'max_qty'), ('min_notional', 'max_notional')):
        if result[high] and result[low] > result[high]:
            raise ValueError('Borsa alt ve üst işlem sınırları çelişiyor.')
    return {key: format(value, 'f') if value else '0' for key, value in result.items()}


def _catalog_pair(pair):
    if (not isinstance(pair, dict) or pair.get('status') != 'TRADING'
            or pair.get('isSpotTradingAllowed') is not True or pair.get('quoteAsset') != 'USDT'):
        return None
    symbol, base = pair.get('symbol'), pair.get('baseAsset')
    try:
        valid_market(symbol, '1m')
    except ValueError:
        return None
    if not isinstance(base, str) or symbol != base + 'USDT':
        return None
    return dict(symbol=symbol, base_asset=base, quote_asset='USDT')


def _selected_symbols(symbols, available, require_available=True):
    if symbols is None:
        return set(available)
    if not isinstance(symbols, list):
        raise ValueError('İşlem çiftleri bir liste olmalı.')
    for symbol in symbols:
        valid_market(symbol, '1m')
        if require_available and symbol not in available:
            raise ValueError('Sembol aktif bir USDT Spot işlem çifti değil.')
    return set(symbols)


class BinanceMarket:
    source = 'binance'
    is_synthetic = False

    def __init__(self):
        self._metadata_lock = threading.RLock()
        self._filters = {}
        self._filter_expires = {}
        self._symbols = None
        self._metadata_expires = 0.0

    def _get(self, path, params):
        query = '?' + urlencode(params) if params else ''
        request = Request(BASE_URL + path + query,
                          headers={'User-Agent': 'SpotPaperBot/1.0', 'Accept': 'application/json'})
        try:
            with urlopen(request, timeout=12) as response:
                if getattr(response, 'status', None) in (418, 429):
                    raise MarketRateLimitError(retry_after=_retry_after(response.headers))
                result = json.load(response)
        except HTTPError as exc:
            if exc.code in (418, 429):
                raise MarketRateLimitError(retry_after=_retry_after(exc.headers)) from exc
            raise
        if isinstance(result, dict) and 'code' in result:
            raise ValueError('Binance veri isteğini reddetti: ' + str(result.get('msg', '')))
        return result

    def symbols(self, force=False):
        with self._metadata_lock:
            if not force and self._symbols is not None and time.monotonic() < self._metadata_expires:
                return [dict(pair) for pair in self._symbols]
            data = self._get('/api/v3/exchangeInfo', {})
            if not isinstance(data, dict) or not isinstance(data.get('symbols'), list):
                raise ValueError('Borsa işlem çifti kataloğu geçersiz.')
            catalog, filters = {}, {}
            for pair in data['symbols']:
                entry = _catalog_pair(pair)
                if entry is not None:
                    catalog[entry['symbol']] = entry
                    filters[entry['symbol']] = _filter_values(pair)
            expires = time.monotonic() + METADATA_TTL
            self._symbols = [catalog[symbol] for symbol in sorted(catalog)]
            self._filters = filters
            self._filter_expires = {symbol: expires for symbol in filters}
            self._metadata_expires = expires
            return [dict(pair) for pair in self._symbols]

    def prices(self, symbols=None):
        # Explicit exit quotes must not depend on a metadata refresh succeeding.
        available = {pair['symbol'] for pair in self.symbols()} if symbols is None else set()
        # Explicitly requested old holdings may have left the active catalog.
        # Their exchange quotes may still protect exits; only catalog members
        # are eligible for discovery and new entries in the service.
        selected = _selected_symbols(symbols, available, require_available=False)
        if not selected:
            return dict(prices={}, updated_at=iso(datetime.now(timezone.utc)), missing_symbols=[])
        # The all-symbol endpoint is one request even for thousands of pairs.
        raw = self._get('/api/v3/ticker/price', {})
        if not isinstance(raw, list):
            raise ValueError('Borsa toplu fiyat yanıtı geçersiz.')
        prices = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            symbol = item.get('symbol')
            if not isinstance(symbol, str) or symbol not in selected:
                continue
            try:
                price = Decimal(str(item.get('price')))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if price.is_finite() and price > 0 and abs(price.adjusted()) <= 36:
                prices[symbol] = str(price)
        if not prices:
            raise ValueError('Geçerli ve güncel piyasa fiyatı alınamadı.')
        return dict(prices=prices, updated_at=iso(datetime.now(timezone.utc)),
                    missing_symbols=sorted(selected - prices.keys()))

    def chart_candles(self, symbol, interval, limit=100):
        """Actual exchange OHLC, with the unfinished candle kept separate.

        Strategy and backtest callers use candles(), which cannot consume this
        candle before its close time and do not accept the display-only 1s.
        """
        valid_chart(symbol, interval)
        if not 100 <= limit <= 1000:
            raise ValueError('Mum sayısı 100–1000 arasında olmalı.')
        raw = self._get('/api/v3/klines', {'symbol': symbol, 'interval': interval, 'limit': limit})
        if not isinstance(raw, list):
            raise ValueError('Borsa mum yanıtı geçersiz.')
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        closed, current = [], None
        for row in raw:
            if not isinstance(row, list) or len(row) < 7:
                raise ValueError('Borsa mum satırı geçersiz.')
            candle = dict(open_time=iso(datetime.fromtimestamp(row[0]/1000, timezone.utc)),
                          close_time=iso(datetime.fromtimestamp(row[6]/1000, timezone.utc)),
                          open=str(row[1]), high=str(row[2]), low=str(row[3]),
                          close=str(row[4]), volume=str(row[5]))
            if row[6] < now_ms:
                closed.append(candle)
            elif row[0] <= now_ms:
                current = candle
        return dict(candles=closed, live_candle=current)

    def candles(self, symbol, interval, limit=500):
        valid_market(symbol, interval)
        return self.chart_candles(symbol, interval, limit)['candles']

    def filters(self, symbol):
        valid_market(symbol, '1m')
        with self._metadata_lock:
            now = time.monotonic()
            if symbol in self._filters and now < self._filter_expires.get(symbol, 0):
                return dict(self._filters[symbol])
            if (self._symbols is not None and now < self._metadata_expires
                    and symbol not in {pair['symbol'] for pair in self._symbols}):
                raise ValueError('Sembol aktif bir USDT Spot işlem çifti değil.')
            data = self._get('/api/v3/exchangeInfo', {'symbol': symbol})
            pairs = data.get('symbols', []) if isinstance(data, dict) else []
            pair = next((pair for pair in pairs if _catalog_pair(pair) is not None
                         and pair['symbol'] == symbol), None)
            if pair is None:
                raise ValueError('Sembol aktif bir Spot işlem çifti değil.')
            self._filters[symbol] = _filter_values(pair)
            self._filter_expires[symbol] = time.monotonic() + METADATA_TTL
            return dict(self._filters[symbol])

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
    _BASES = {'BTC': 60000, 'ETH': 3000, 'SOL': 150, 'BNB': 600, 'XRP': .6,
              'ADA': .4, 'DOGE': .15, 'AVAX': 35, 'LINK': 15, 'DOT': 7}

    def __init__(self):
        self._lock = threading.RLock()
        self.step = 0
        self.anchor = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    @staticmethod
    def _price(index, symbol):
        asset = symbol[:-4]
        base = DemoMarket._BASES[asset]
        phase = list(DemoMarket._BASES).index(asset) * .73
        return base * (1 + .055*math.sin(index/12 + phase)
                       + .015*math.sin(index/3.7 + phase*.6) + index*.00002)

    def symbols(self, force=False):
        return [dict(symbol=asset + 'USDT', base_asset=asset, quote_asset='USDT')
                for asset in self._BASES]

    def prices(self, symbols=None):
        selected = _selected_symbols(symbols, {pair['symbol'] for pair in self.symbols()})
        with self._lock:
            self.step += 1
            return dict(prices={symbol: f'{self._price(500+self.step, symbol):.8f}'
                                for symbol in sorted(selected)},
                        updated_at=iso(datetime.now(timezone.utc)), missing_symbols=[])

    def filters(self, symbol):
        _selected_symbols([symbol], {pair['symbol'] for pair in self.symbols()})
        return dict(step_size='0.00000001', min_qty='0.00000001', min_notional='5',
                    max_qty='0', max_notional='0')

    def _display_candles(self, symbol, interval, limit=500):
        valid_chart(symbol, interval)
        if not 100 <= limit <= 1000:
            raise ValueError('Mum sayısı 100–1000 arasında olmalı.')
        _selected_symbols([symbol], {pair['symbol'] for pair in self.symbols()})
        duration = CHART_INTERVALS[interval]
        output = []
        with self._lock:
            step, anchor = self.step, self.anchor
        for offset in range(limit):
            i = 500 + step + offset - limit
            start = anchor + timedelta(seconds=(step+offset-limit)*duration)
            opened, closed = self._price(i, symbol), self._price(i+1, symbol)
            output.append(dict(open_time=iso(start), close_time=iso(start+timedelta(seconds=duration)-timedelta(milliseconds=1)),
                               open=f'{opened:.8f}', high=f'{max(opened,closed)*1.002:.8f}',
                               low=f'{min(opened,closed)*.998:.8f}', close=f'{closed:.8f}', volume='100'))
        return output

    def candles(self, symbol, interval, limit=500):
        valid_market(symbol, interval)
        return self._display_candles(symbol, interval, limit)

    def chart_candles(self, symbol, interval, limit=100):
        return dict(candles=self._display_candles(symbol, interval, limit), live_candle=None)

    def sample(self, symbol, interval):
        valid_market(symbol, interval)
        with self._lock:
            batch = self.prices([symbol])
            bars = self.candles(symbol, interval)
            return dict(price=batch['prices'][symbol], updated_at=batch['updated_at'],
                        candles=bars, filters=self.filters(symbol))
