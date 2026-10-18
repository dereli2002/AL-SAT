"""Persistent, single-market paper trading service."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import threading
import time

from .core import DEFAULT_CONFIG, PaperBroker
from .market import BinanceMarket, DemoMarket, valid_market
from .strategies import signals
from .backtest import compare_strategies, run_backtest

APP_DEFAULTS = dict(symbol='BTCUSDT', interval='5m', strategy='ema_cross', data_source='binance')
STRATEGIES = ('ema_cross', 'rsi_reversion', 'breakout')


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        self.db.commit()

    def load(self):
        row = self.db.execute('SELECT payload FROM state WHERE id=1').fetchone()
        return json.loads(row[0]) if row else None

    def save(self, payload, events=()):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state(id,payload) VALUES(1,?)', (json.dumps(payload),))
            self.db.executemany('INSERT INTO events(payload) VALUES(?)', [(json.dumps(e),) for e in events])


class BotService:
    def __init__(self, db_path, feed=None):
        self.lock = threading.RLock()
        self.store = Store(db_path)
        saved = self.store.load()
        self.config = {**DEFAULT_CONFIG, **APP_DEFAULTS, **(saved['config'] if saved else {})}
        self.broker = PaperBroker(self.config, saved.get('broker') if saved else None)
        self.last_candle = saved.get('last_candle') if saved else None
        self.running = False  # A restart restores the ledger, never auto-enables new entries.
        self.error = None
        self.market = dict(source=self.config['data_source'], is_synthetic=self.config['data_source']=='demo',
                           symbol=self.config['symbol'], price=None, updated_at=None, error=None)
        self.backtest = saved.get('backtest') if saved else None
        self._feed_override = feed
        self.feed = feed or self._new_feed()
        self._generation = 0
        self._stop = threading.Event()
        self._worker = None
        self._last_success = None

    def _new_feed(self):
        return DemoMarket() if self.config['data_source'] == 'demo' else BinanceMarket()

    def _save(self, events=()):
        self.store.save(dict(config=self.config, broker=self.broker.export_state(),
                             last_candle=self.last_candle, backtest=self.backtest), events)

    def start_worker(self):
        if self._worker is None:
            self._worker = threading.Thread(target=self._loop, daemon=True, name='paper-market')
            self._worker.start()

    def _loop(self):
        while not self._stop.is_set():
            self.step()
            self._stop.wait(5)

    def close(self):
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=20)
        with self.lock:
            self.store.db.close()

    def step(self):
        with self.lock:
            generation, config, feed = self._generation, deepcopy(self.config), self.feed
        try:
            sample = feed.sample(config['symbol'], config['interval'])
            with self.lock:
                if generation != self._generation:
                    return
                # Refresh exchange quantity/notional rules without discarding the ledger.
                filters = sample.get('filters', {})
                if any(str(self.config.get(k)) != str(v) for k,v in filters.items()):
                    merged = {**self.config, **filters}
                    self.broker = PaperBroker(merged, self.broker.export_state())
                    self.config = merged
                bars = sample['candles']
                candle_id = bars[-1]['close_time']
                signal = 'hold'
                if self.running and candle_id != self.last_candle:
                    signal = signals(bars, self.config['strategy'])[-1]
                    self.last_candle = candle_id
                events = self.broker.tick(sample['price'], sample['updated_at'], signal=signal)
                self.market = dict(source=feed.source, is_synthetic=feed.is_synthetic,
                                   symbol=self.config['symbol'], price=sample['price'], updated_at=sample['updated_at'], error=None)
                self.error = None
                self._last_success = time.monotonic()
                self._save(events)
        except Exception as exc:
            with self.lock:
                if generation == self._generation:
                    self.error = f'{type(exc).__name__}: {exc}'
                    self.market['error'] = self.error
                    # Preserve the last quote for display only. Never trade on a failed read.

    def state(self):
        with self.lock:
            price = self.market['price']
            if price is None:
                position = self.broker.export_state().get('position')
                price = position.get('entry_price', '1') if position else '1'
            account = self.broker.snapshot(price)
            return dict(config=deepcopy(self.config), status=dict(running=self.running, error=self.error,
                         fresh=not self.market['error'] and self._last_success is not None and time.monotonic()-self._last_success < 30),
                        market=deepcopy(self.market), account={k:v for k,v in account.items() if k not in ('position','trades')},
                        position=account.get('position'), trades=account.get('trades', []), backtest=deepcopy(self.backtest),
                        fee_info=dict(kind='estimate', source='user_config',
                            description='Komisyonlar kullanıcı tahminidir; Binance hesabının güncel oranı API ile doğrulanmadı.'))

    def configure(self, patch):
        if not isinstance(patch, dict):
            raise ValueError('Ayarlar bir JSON nesnesi olmalı.')
        allowed = set(DEFAULT_CONFIG) | set(APP_DEFAULTS)
        if set(patch) - allowed:
            raise ValueError('Bilinmeyen ayar: ' + ', '.join(sorted(set(patch)-allowed)))
        with self.lock:
            if self.running or self.state()['position']:
                raise ValueError('Ayarlar için botu duraklatın ve açık pozisyonu kapatın.')
            merged = {**self.config, **patch}
            valid_market(merged['symbol'], merged['interval'])
            if merged['strategy'] not in STRATEGIES or merged['data_source'] not in ('binance','demo'):
                raise ValueError('Geçersiz strateji veya veri kaynağı.')
            current = self.broker.snapshot('1')
            if Decimal(str(merged['initial_cash'])) != Decimal(str(self.config['initial_cash'])):
                if current.get('trade_count', 0):
                    raise ValueError('Başlangıç bakiyesini değiştirmeden önce deneme defterini sıfırlayın.')
                broker = PaperBroker(merged)
            else:
                broker = PaperBroker(merged, self.broker.export_state())
            changed_market = any(merged[k] != self.config[k] for k in ('symbol','interval','data_source'))
            if changed_market and current.get('trade_count', 0):
                raise ValueError('Veri kaynağı/çifti değişmeden önce deneme defterini sıfırlayın; farklı piyasalar karıştırılmaz.')
            self.config, self.broker = merged, broker
            if changed_market:
                self._generation += 1
                self.feed = self._feed_override or self._new_feed()
                self.last_candle = None
                self.market = dict(source=merged['data_source'],is_synthetic=merged['data_source']=='demo',symbol=merged['symbol'],price=None,updated_at=None,error=None)
                self._last_success = None
                self.backtest = None
            self._save()
        return self.state()

    def control(self, action):
        with self.lock:
            events = []
            if action == 'start':
                if self.market['error'] or self._last_success is None or time.monotonic()-self._last_success >= 30:
                    raise ValueError('Başlatmak için güncel ve başarılı fiyat akışı gerekli.')
                if self.state()['account'].get('risk',{}).get('daily_halted'):
                    raise ValueError('Günlük zarar limiti nedeniyle yeni işlemler kilitli.')
                self.running = True
                self.last_candle = None
            elif action == 'pause':
                self.running = False
            elif action == 'close_position':
                if self._last_success is None or time.monotonic()-self._last_success >= 30 or self.market['error']:
                    raise ValueError('Deneme satışında güncel fiyat gerekli.')
                events = self.broker.sell(self.market['price'], datetime.now(timezone.utc).isoformat(), reason='manual')
            elif action == 'reset':
                if self.running or self.state()['position']:
                    raise ValueError('Sıfırlamak için botu duraklatın ve pozisyonu kapatın.')
                self.broker = PaperBroker(self.config)
                self.last_candle = None
                self.backtest = None
            else:
                raise ValueError('Bilinmeyen komut.')
            self._save(events)
        return self.state()

    def test_history(self, payload):
        with self.lock:
            config = deepcopy(self.config)
            generation = self._generation
            feed = self.feed
        symbol, interval = payload.get('symbol',config['symbol']), payload.get('interval',config['interval'])
        valid_market(symbol, interval)
        limit = int(payload.get('limit',500))
        strategy = payload.get('strategy','all')
        if strategy not in STRATEGIES + ('all',):
            raise ValueError('Geçersiz strateji.')
        bars = feed.candles(symbol, interval, limit)
        if isinstance(feed, BinanceMarket):
            config.update(feed.filters(symbol))
        if strategy == 'all':
            report = compare_strategies(bars, config)
        else:
            result = run_backtest(bars,config,strategy)
            report = dict(results=[result],train_candles=int(len(bars)*.7),test_candles=len(bars)-int(len(bars)*.7),notes=result.get('notes',[]))
        report.update(data_source=config['data_source'],is_synthetic=feed.is_synthetic,symbol=symbol,interval=interval,
                      fee_kind='estimate',generated_at=datetime.now(timezone.utc).isoformat())
        with self.lock:
            if generation != self._generation:
                raise ValueError('Test sırasında veri kaynağı değişti; testi yeniden çalıştırın.')
            self.backtest = report
            self._save()
        return report
