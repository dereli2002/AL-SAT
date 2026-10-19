"""Persistent multi-asset paper portfolio and rate-aware Spot scanner."""
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import threading
import time

from .core import DEFAULT_CONFIG, normalize_config
from .portfolio import PaperPortfolio
from .market import BinanceMarket, DemoMarket, INTERVALS, valid_market
from .strategies import STRATEGIES, signals, signal_details
from .backtest import compare_strategies, run_backtest

APP_DEFAULTS = dict(symbol='BTCUSDT', symbols=['BTCUSDT','ETHUSDT','SOLUSDT'],
                    universe_mode='all', max_open_positions=5, interval='5m',
                    strategy='hybrid', data_source='binance')
SCAN_BATCH_SIZE = 12
CHART_CACHE_SIZE = 64
CHART_CACHE_TTL = 30


def validated_config(config):
    result = normalize_config(config)
    valid_market(result['symbol'], result['interval'])
    if result['strategy'] not in STRATEGIES or result['data_source'] not in ('binance','demo'):
        raise ValueError('Geçersiz strateji veya veri kaynağı.')
    if result['universe_mode'] not in ('all','selected'):
        raise ValueError('Tarama kapsamı all veya selected olmalı.')
    symbols = result['symbols']
    if not isinstance(symbols,list) or len(symbols)>2000 or (not symbols and result['universe_mode']=='selected'):
        raise ValueError('Seçili tarama için en az bir işlem çifti seçin (en çok 2000).')
    for symbol in symbols:
        valid_market(symbol,result['interval'])
    result['symbols'] = list(dict.fromkeys(symbols))
    maximum = result['max_open_positions']
    if isinstance(maximum,bool) or not str(maximum).isdigit() or not 1<=int(maximum)<=100:
        raise ValueError('Açık pozisyon sınırı 1–100 arasında tam sayı olmalı.')
    result['max_open_positions'] = int(maximum)
    return result


class Store:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        self.db.commit()

    def load(self):
        row=self.db.execute('SELECT payload FROM state WHERE id=1').fetchone()
        return json.loads(row[0]) if row else None

    def backup_legacy(self):
        path=self.path.with_name(self.path.stem+'.v1-backup-'+str(time.time_ns())+'.sqlite3')
        with sqlite3.connect(path) as target:
            self.db.backup(target)
        return path

    def save(self,payload,events=()):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state(id,payload) VALUES(1,?)',(json.dumps(payload),))
            self.db.executemany('INSERT INTO events(payload) VALUES(?)',[(json.dumps(event),) for event in events])


class BotService:
    def __init__(self,db_path,feed=None):
        self.lock=threading.RLock()
        self.store=Store(db_path)
        saved=self.store.load()
        self.config=validated_config({**DEFAULT_CONFIG,**deepcopy(APP_DEFAULTS),**(saved['config'] if saved else {})})
        self.migration_backup=None
        if saved and 'portfolio' in saved:
            self.portfolio=PaperPortfolio(self.config,saved['portfolio'])
        elif saved and saved.get('broker'):
            self.migration_backup=str(self.store.backup_legacy())
            self.portfolio=PaperPortfolio.from_legacy(self.config,saved['broker'],self.config['symbol'])
        else:
            self.portfolio=PaperPortfolio(self.config)
        self.broker=self.portfolio  # Historical internal alias; there is one shared account.
        self.last_candles=saved.get('last_candles',{}) if saved else {}
        self.running=False
        self.emergency_active=bool(saved.get('emergency_active',False)) if saved else False
        self.emergency_revision=saved.get('emergency_revision',0) if saved else 0
        self._manual_overrides=saved.get('manual_overrides',{}) if saved else {}
        self.error=None
        self.backtest=saved.get('backtest') if saved else None
        self._feed_override=feed
        self.feed=feed or self._new_feed()
        self._generation=0
        self._stop=threading.Event()
        self._worker=None
        self._executor=ThreadPoolExecutor(max_workers=4,thread_name_prefix='candle-scan')
        self._pending={}
        self._cache={}
        self._chart_cache=OrderedDict()
        self._next_due={}
        self._cursor=0
        self._catalog=[]
        self._catalog_ok=False
        self._catalog_warning=None
        self._universe=[]
        self._prices={}
        self._quote_time=None
        self._last_success=None
        self._scan_errors=0
        self._last_symbol=None
        self._last_scan_at=None
        self._backoff_until=0
        self._symbol_errors={}
        self.market=self._empty_market()
        if self.migration_backup:
            self._save()

    def _new_feed(self):
        return DemoMarket() if self.config['data_source']=='demo' else BinanceMarket()

    def _empty_market(self):
        return dict(source=self.config['data_source'],is_synthetic=self.config['data_source']=='demo',
                    symbol=self.config['symbol'],price=None,updated_at=None,error=None)

    def _save(self,events=()):
        self.store.save(dict(version=2,config=self.config,portfolio=self.portfolio.export_state(),
                             last_candles=self.last_candles,backtest=self.backtest,
                             emergency_active=self.emergency_active,emergency_revision=self.emergency_revision,
                             manual_overrides=self._manual_overrides),events)

    def start_worker(self):
        if self._worker is None:
            self._worker=threading.Thread(target=self._loop,daemon=True,name='portfolio-prices')
            self._worker.start()

    def _loop(self):
        while not self._stop.is_set():
            self.step()
            self._stop.wait(5)

    def close(self):
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=30)
        self._executor.shutdown(wait=True,cancel_futures=True)
        with self.lock:
            self.store.db.close()

    def symbols(self):
        with self.lock:
            feed,generation=self.feed,self._generation
            if time.monotonic()<self._backoff_until:
                return dict(symbols=deepcopy(self._catalog),source=feed.source,is_synthetic=feed.is_synthetic,
                            error='Borsa istek sınırı nedeniyle liste yenilemesi bekliyor.')
        try:
            catalog=feed.symbols()
            with self.lock:
                if generation==self._generation:
                    self._catalog=catalog
                    self._catalog_ok=True
            return dict(symbols=catalog,source=feed.source,is_synthetic=feed.is_synthetic)
        except Exception as exc:
            self._record_throttle(exc)
            return dict(symbols=deepcopy(self._catalog),source=feed.source,is_synthetic=feed.is_synthetic,error=str(exc))

    def _record_throttle(self,exc):
        if hasattr(exc,'retry_after'):
            with self.lock:
                self._backoff_until=max(self._backoff_until,time.monotonic()+exc.retry_after)
                self.error='Borsa istek sınırı: yeni istekler ve alımlar bekliyor.'
                self.market['error']=self.error

    def _remember_chart(self,symbol,interval,bars):
        """Called with the service lock; chart reads never alter trade signals."""
        key=(symbol,interval)
        self._chart_cache[key]=dict(candles=deepcopy(bars),fetched=time.monotonic(),
                                    updated_at=datetime.now(timezone.utc).isoformat())
        self._chart_cache.move_to_end(key)
        while len(self._chart_cache)>CHART_CACHE_SIZE:
            self._chart_cache.popitem(last=False)

    def _chart_payload(self,symbol,interval,entry,*,cached,error=None):
        quote_fresh=(not self.market['error'] and self._last_success is not None
                     and time.monotonic()-self._last_success<30
                     and time.monotonic()>=self._backoff_until
                     and symbol in self._prices)
        stale=bool(error) or time.monotonic()-entry['fetched']>=CHART_CACHE_TTL
        if not self.feed.is_synthetic and entry['candles']:
            close=datetime.fromisoformat(entry['candles'][-1]['close_time'].replace('Z','+00:00'))
            stale=stale or (datetime.now(timezone.utc)-close).total_seconds()>INTERVALS[interval]*2+60
        return dict(symbol=symbol,interval=interval,source=self.feed.source,is_synthetic=self.feed.is_synthetic,
                    candles=deepcopy(entry['candles']),price=self._prices.get(symbol),
                    quote_time=self._quote_time,quote_fresh=quote_fresh,
                    updated_at=entry['updated_at'],cached=cached,history_stale=stale,error=error)

    def chart(self,symbol=None,interval=None):
        """Fetch display history independently of the selected trading universe.

        Candle I/O runs outside the portfolio lock so opening a chart cannot
        delay stops or the emergency command. Previous history is explicitly
        marked stale when the exchange cannot refresh it.
        """
        with self.lock:
            symbol=symbol or self.config['symbol']
            interval=interval or self.config['interval']
            valid_market(symbol,interval)
            available={item['symbol'] for item in self._catalog}|set(self.portfolio.position_symbols)
            if available and symbol not in available:
                raise ValueError('Seçilen sembol aktif USDT Spot listesinde veya açık pozisyonlarda yok.')
            key=(symbol,interval)
            entry=self._chart_cache.get(key)
            if time.monotonic()<self._backoff_until:
                if entry:
                    return self._chart_payload(symbol,interval,entry,cached=True,
                                               error='Borsa istek sınırı nedeniyle grafik yenilemesi bekliyor.')
                raise ValueError('Borsa istek sınırı beklenirken grafik isteği gönderilemez.')
            if entry and time.monotonic()-entry['fetched']<CHART_CACHE_TTL:
                self._chart_cache.move_to_end(key)
                return self._chart_payload(symbol,interval,entry,cached=True)
            generation,feed=self._generation,self.feed
        try:
            bars=feed.candles(symbol,interval,100)
            if not bars:
                raise ValueError('Grafik için geçmiş mum verisi bulunamadı.')
        except Exception as exc:
            self._record_throttle(exc)
            with self.lock:
                if generation!=self._generation:
                    raise ValueError('Grafik yüklenirken ayarlar değişti; yeniden deneyin.') from exc
                entry=self._chart_cache.get(key)
                if entry:
                    return self._chart_payload(symbol,interval,entry,cached=True,error=str(exc))
            raise
        with self.lock:
            if generation!=self._generation:
                raise ValueError('Grafik yüklenirken ayarlar değişti; yeniden deneyin.')
            self._remember_chart(symbol,interval,bars)
            return self._chart_payload(symbol,interval,self._chart_cache[key],cached=False)

    @staticmethod
    def _scan(feed,symbol,interval):
        bars=feed.candles(symbol,interval,100)
        if len(bars)<60:
            raise ValueError('Sinyal için en az 60 kapanmış mum gerekli.')
        if isinstance(feed,BinanceMarket):
            close=datetime.fromisoformat(bars[-1]['close_time'].replace('Z','+00:00'))
            if (datetime.now(timezone.utc)-close).total_seconds()>INTERVALS[interval]*2+60:
                raise ValueError('Mum verisi güncel değil; eski sinyalle işlem yapılmadı.')
        return dict(bars=bars,filters=feed.filters(symbol))

    def _collect(self,feed,config):
        for symbol,(generation,future) in list(self._pending.items()):
            if not future.done():
                continue
            del self._pending[symbol]
            if generation!=self._generation:
                continue
            try:
                sample=future.result()
                bars=sample['bars']
                self._remember_chart(symbol,config['interval'],bars)
                details=signal_details(bars,config['strategy'])
                self._cache[symbol]=dict(candle=bars[-1]['close_time'],signal=details['signal'],
                                         details=details,filters=sample['filters'])
                self._last_symbol=symbol
                self._last_scan_at=datetime.now(timezone.utc).isoformat()
                self._symbol_errors.pop(symbol,None)
                close=datetime.fromisoformat(bars[-1]['close_time'].replace('Z','+00:00'))
                until_next=close.timestamp()+INTERVALS[config['interval']]+1-time.time()
                self._next_due[symbol]=time.monotonic()+max(5,min(INTERVALS[config['interval']],until_next))
            except Exception as exc:
                self._scan_errors+=1
                self._symbol_errors[symbol]=str(exc)
                self._next_due[symbol]=time.monotonic()+60
                if hasattr(exc,'retry_after'):
                    self._record_throttle(exc)

    def _schedule(self,feed,config):
        if self.emergency_active or time.monotonic()<self._backoff_until:
            return
        priority=list(self.portfolio.position_symbols)
        if self._universe:
            offset=self._cursor%len(self._universe)
            priority+=self._universe[offset:]+self._universe[:offset]
        now=time.monotonic()
        for symbol in dict.fromkeys(priority):
            if len(self._pending)>=SCAN_BATCH_SIZE:
                break
            if symbol in self._pending or self._next_due.get(symbol,0)>now:
                continue
            self._pending[symbol]=(self._generation,self._executor.submit(self._scan,feed,symbol,config['interval']))
            if symbol in self._universe:
                self._cursor=(self._universe.index(symbol)+1)%len(self._universe)

    def step(self):
        with self.lock:
            if time.monotonic()<self._backoff_until:
                self.error='Binance istek sınırı: '+str(int(self._backoff_until-time.monotonic())+1)+' saniye bekleniyor; yeni işlem açılmıyor.'
                self.market['error']=self.error
                return
            generation,config,feed=self._generation,deepcopy(self.config),self.feed
        try:
            catalog_warning=None
            catalog_ok=True
            try:
                catalog=feed.symbols()
            except Exception as exc:
                if hasattr(exc,'retry_after'):
                    raise
                with self.lock:
                    catalog=deepcopy(self._catalog)
                    holdings=list(self.portfolio.position_symbols)
                if not catalog and not holdings:
                    raise
                catalog_ok=False
                catalog_warning='Parite listesi yenilenemedi; yeni alımlar bekliyor. Mevcut pozisyonların fiyat takibi sürüyor: '+str(exc)
            available={item['symbol'] for item in catalog}
            if config['universe_mode']=='selected':
                missing=set(config['symbols'])-available
                if missing:
                    catalog_warning='Aktif listeden çıkarılan pariteler yeni alımlarda atlandı: '+', '.join(sorted(missing))
                universe=[symbol for symbol in config['symbols'] if symbol in available]
            else:
                universe=[item['symbol'] for item in catalog]
            with self.lock:
                # The exchange's single batch contains the whole catalog anyway.
                # Keep every quote available for manual trades and chart search.
                requested=available|set(self.portfolio.position_symbols)|{config['symbol']}
            quote=feed.prices(sorted(requested))
            prices=quote['prices']
            if not prices:
                raise ValueError('Güncel fiyat alınamadı.')
            with self.lock:
                if generation!=self._generation:
                    return
                self._catalog,self._universe=catalog,universe
                self._catalog_ok,self._catalog_warning=catalog_ok,catalog_warning
                instant=datetime.now(timezone.utc).isoformat()
                # Protective exits run before candle I/O and before new signals.
                events=self.portfolio.tick(prices,instant)
                if self.emergency_active:
                    for symbol in list(self.portfolio.position_symbols):
                        if symbol in prices:
                            events+=self.portfolio.sell(symbol,prices[symbol],instant,
                                                        reason='emergency_close',prices=prices)
                exited={event.get('symbol') for event in events if event.get('type')=='sell'}
                self._collect(feed,config)
                new_signals={}
                filters={}
                if self.running and catalog_ok and not self.emergency_active and time.monotonic()>=self._backoff_until:
                    for symbol,sample in self._cache.items():
                        if symbol not in set(universe)|set(self.portfolio.position_symbols) or symbol not in prices or symbol in exited:
                            continue
                        if sample['candle']==self.last_candles.get(symbol):
                            continue
                        closed=datetime.fromisoformat(sample['candle'].replace('Z','+00:00')).timestamp()
                        if isinstance(feed,BinanceMarket) and time.time()-closed>INTERVALS[config['interval']]*2+60:
                            continue
                        if closed+0.002<self._manual_overrides.get(symbol,0):
                            continue
                        # A cache belongs to the current interval/strategy generation.
                        new_signals[symbol]=sample['signal']
                        self._manual_overrides.pop(symbol,None)
                        filters[symbol]=sample['filters']
                        self.last_candles[symbol]=sample['candle']
                    if new_signals:
                        events+=self.portfolio.tick(prices,instant,signals=new_signals,filters=filters)
                self._prices=prices
                self._quote_time=quote['updated_at']
                self.market=dict(source=feed.source,is_synthetic=feed.is_synthetic,symbol=self.config['symbol'],
                                 price=prices.get(self.config['symbol']),updated_at=quote['updated_at'],error=None)
                self.error=None if time.monotonic()>=self._backoff_until else 'Borsa istek sınırı: yeni istekler ve alımlar bekliyor.'
                self.market['error']=self.error
                self._last_success=time.monotonic()
                self._schedule(feed,config)
                self._save(events)
        except Exception as exc:
            with self.lock:
                if generation==self._generation:
                    self.error=f'{type(exc).__name__}: {exc}'
                    self.market['error']=self.error
                    if hasattr(exc,'retry_after'):
                        self._backoff_until=max(self._backoff_until,time.monotonic()+exc.retry_after)

    def state(self):
        with self.lock:
            fresh=not self.market['error'] and self._last_success is not None and time.monotonic()-self._last_success<30 and time.monotonic()>=self._backoff_until
            account=self.portfolio.snapshot(self._prices if fresh else {})
            positions=account.get('positions',[])
            markets=[dict(symbol=symbol,price=self._prices.get(symbol),updated_at=self._quote_time,
                          last_signal=self._cache.get(symbol,{}).get('signal','hold'),
                          signal_details=self._cache.get(symbol,{}).get('details'),error=self._symbol_errors.get(symbol))
                     for symbol in dict.fromkeys([self.config['symbol']]+[pair['symbol'] for pair in self._catalog]+list(self.portfolio.position_symbols))]
            return dict(config=deepcopy(self.config),status=dict(running=self.running,error=self.error,fresh=fresh),
                        market=deepcopy(self.market),markets=markets,
                        account={key:value for key,value in account.items() if key not in ('positions','trades')},
                        positions=positions,position=next((p for p in positions if p['symbol']==self.config['symbol']),None),
                        trades=account.get('trades',[]),backtest=deepcopy(self.backtest),
                        scanner=dict(total_symbols=len(self._universe),scanned_symbols=len(set(self._cache)&set(self._universe)),
                                     last_symbol=self._last_symbol,last_scan_at=self._last_scan_at,batch_size=SCAN_BATCH_SIZE,
                                     pending_scans=len(self._pending),errors=self._scan_errors,mode=self.config['universe_mode'],
                                     warning=self._catalog_warning),
                        emergency=dict(active=self.emergency_active,revision=self.emergency_revision,
                                       pending_symbols=list(self.portfolio.position_symbols) if self.emergency_active else [],
                                       last_error=self.error if self.emergency_active and self.portfolio.position_symbols else None),
                        fee_info=dict(kind='estimate',source='user_config',description='Komisyonlar kullanıcı tahminidir; Binance hesabının güncel oranı API ile doğrulanmadı.'))

    def configure(self,patch):
        if not isinstance(patch,dict):
            raise ValueError('Ayarlar bir JSON nesnesi olmalı.')
        if set(patch)-(set(DEFAULT_CONFIG)|set(APP_DEFAULTS)):
            raise ValueError('Bilinmeyen ayar.')
        with self.lock:
            merged=validated_config({**self.config,**patch})
            available={item['symbol'] for item in self._catalog}
            if set(patch)=={'symbol'}:
                if available and merged['symbol'] not in available|set(self.portfolio.position_symbols):
                    raise ValueError('Seçilen sembol aktif USDT Spot listesinde veya açık pozisyonlarda yok.')
                self.config=merged
                self.market.update(symbol=merged['symbol'],price=self._prices.get(merged['symbol']))
                self._save()
                return self.state()
            if available and merged['data_source']==self.config['data_source']:
                wanted={merged['symbol']} | (set(merged['symbols']) if merged['universe_mode']=='selected' else set())
                if wanted-available:
                    raise ValueError('Seçilen sembol aktif USDT Spot listesinde yok.')
            account=self.portfolio.snapshot(self._prices)
            if self.running or account['positions']:
                raise ValueError('Ayarlar için botu duraklatın ve açık pozisyonları kapatın.')
            source_changed=merged['data_source']!=self.config['data_source']
            cash_changed=Decimal(merged['initial_cash'])!=Decimal(self.config['initial_cash'])
            if account['trade_count'] and (source_changed or cash_changed):
                raise ValueError('Veri kaynağı veya başlangıç bakiyesi değişmeden önce deneme defterini sıfırlayın.')
            portfolio=PaperPortfolio(merged,None if cash_changed else self.portfolio.export_state())
            self.config,self.portfolio=merged,portfolio
            self.broker=portfolio
            self._generation+=1
            for _,future in self._pending.values():
                future.cancel()
            self._pending.clear()
            self._cache.clear()
            self._chart_cache.clear()
            self._next_due.clear()
            self.last_candles={}
            self._cursor=0
            if source_changed:
                self.feed=self._feed_override or self._new_feed()
                self._catalog,self._universe=[],[]
                self._prices={}
                self._quote_time=None
                self._last_success=None
                self._backoff_until=0
                self.market=self._empty_market()
                self.error=None
                self.backtest=None
            self._save()
        return self.state()

    def control(self,action,symbol=None,amount=None,expected_emergency_revision=None):
        prepared_filters=None
        if action=='buy':
            with self.lock:
                generation,feed=self._generation,self.feed
                target=symbol or self.config['symbol']
                valid_market(target,self.config['interval'])
                if self.emergency_active or time.monotonic()<self._backoff_until:
                    raise ValueError('Acil durum veya borsa beklemesi nedeniyle yeni alım kilitli.')
                if target not in {pair['symbol'] for pair in self._catalog}:
                    raise ValueError('Sembol aktif USDT Spot listesinde yok.')
            # Metadata can involve a network request. Do not block price stops
            # while it runs; freshness and the kill switch are checked again.
            try:
                prepared_filters={target:feed.filters(target)}
            except Exception as exc:
                self._record_throttle(exc)
                raise
        with self.lock:
            if action=='buy' and generation!=self._generation:
                raise ValueError('İstek sırasında ayarlar değişti; manuel işlemi yeniden deneyin.')
            events=[]
            fresh=self._last_success is not None and time.monotonic()-self._last_success<30 and not self.market['error']
            now=datetime.now(timezone.utc).isoformat()
            if action=='emergency_close':
                # Persist the kill switch BEFORE any quote-dependent liquidation.
                self.running=False
                self.emergency_active=True
                self.emergency_revision+=1
                self._save()
                if fresh:
                    for target in list(self.portfolio.position_symbols):
                        if target in self._prices:
                            events+=self.portfolio.sell(target,self._prices[target],now,
                                                        reason='emergency_close',prices=self._prices)
            elif action=='clear_emergency':
                # An older tab/request must never unlock a newer emergency.
                # Check the persisted revision under the same lock as the latch.
                if (type(expected_emergency_revision) is not int
                        or expected_emergency_revision!=self.emergency_revision):
                    raise ValueError('Acil durum değişti veya sürümü eksik; güncel durumu yenileyip kilidi tekrar kaldırın.')
                if self.portfolio.position_symbols:
                    raise ValueError('Acil kilidi kaldırmak için tüm bekleyen pozisyonların kapanması gerekli.')
                self.emergency_active=False
                self.running=False
            elif action=='start':
                if self.emergency_active:
                    raise ValueError('Önce acil durum kilidini kaldırın; otomasyon kendiliğinden yeniden başlamaz.')
                if not fresh:
                    raise ValueError('Başlatmak için güncel ve başarılı fiyat akışı gerekli.')
                if time.monotonic()<self._backoff_until:
                    raise ValueError('Borsa istek sınırı beklenirken yeni işlemler başlatılamaz.')
                if self.portfolio.snapshot(self._prices)['risk']['daily_halted']:
                    raise ValueError('Günlük zarar limiti nedeniyle yeni işlemler kilitli.')
                self.running=True
                self.last_candles={}
            elif action=='pause':
                self.running=False
            elif action in ('buy','sell','close_position','close_all'):
                if action=='close_all':
                    self.running=False
                    self._save()
                if not fresh:
                    raise ValueError('Manuel işlem için güncel fiyat akışı gerekli; eski fiyatla işlem yapılmadı.')
                if action=='buy':
                    if self.emergency_active:
                        raise ValueError('Acil durum kilidi açıkken yeni alım yapılamaz.')
                    if not self._catalog_ok:
                        raise ValueError('Parite listesi doğrulanmadan yeni alım yapılamaz; mevcut pozisyon takibi sürüyor.')
                    if time.monotonic()<self._backoff_until:
                        raise ValueError('Borsa istek sınırı beklenirken yeni alım yapılamaz.')
                    target=symbol or self.config['symbol']
                    valid_market(target,self.config['interval'])
                    if target not in {item['symbol'] for item in self._catalog}:
                        raise ValueError('Sembol aktif USDT Spot listesinde yok.')
                    if target not in self._prices:
                        raise ValueError('Seçilen varlığın güncel fiyatı alınamadı.')
                    budget=Decimal(str(amount if amount is not None else self.config['order_quote_amount']))
                    if not budget.is_finite() or budget<=0:
                        raise ValueError('Manuel alış tutarı pozitif ve sonlu olmalı.')
                    if budget>Decimal(self.portfolio.snapshot(self._prices)['cash']):
                        raise ValueError('Manuel alış tutarı kullanılabilir USDT bakiyesini aşıyor.')
                    events=self.portfolio.buy(target,self._prices,now,amount=budget,filters=prepared_filters)
                    bought=any(event.get('type')=='buy' and event.get('symbol')==target for event in events)
                    if not bought:
                        self._save(events)
                        reason=next((event.get('reason') for event in events if event.get('type')=='blocked'),'risk_limit')
                        raise ValueError('Manuel alış gerçekleşmedi: '+str(reason))
                    duration=INTERVALS[self.config['interval']]
                    self._manual_overrides[target]=(int(time.time())//duration+1)*duration
                else:
                    targets=list(self.portfolio.position_symbols) if action=='close_all' else [symbol or self.config['symbol']]
                    if any(target not in self._prices for target in targets):
                        raise ValueError('Pozisyonun güncel fiyatı alınamadı; eski fiyatla satış yapılmadı.')
                    if action!='close_all' and targets[0] not in self.portfolio.position_symbols:
                        raise ValueError('Seçilen varlıkta satılacak açık pozisyon yok.')
                    for target in targets:
                        events+=self.portfolio.sell(target,self._prices[target],now,
                                                    reason='manual_sell',prices=self._prices)
                        duration=INTERVALS[self.config['interval']]
                        self._manual_overrides[target]=(int(time.time())//duration+1)*duration
            elif action=='reset':
                if self.running or self.portfolio.position_symbols:
                    raise ValueError('Sıfırlamak için botu duraklatın ve pozisyonları kapatın.')
                self.portfolio=PaperPortfolio(self.config)
                self.broker=self.portfolio
                self.last_candles={}
                self._manual_overrides={}
                self.backtest=None
            else:
                raise ValueError('Bilinmeyen komut.')
            self._save(events)
        return self.state()

    def test_history(self,payload):
        with self.lock:
            if time.monotonic()<self._backoff_until:
                raise ValueError('Borsa istek sınırı beklenirken geçmiş test isteği gönderilemez.')
            config=deepcopy(self.config)
            generation,feed=self._generation,self.feed
        symbol,interval=payload.get('symbol',config['symbol']),payload.get('interval',config['interval'])
        valid_market(symbol,interval)
        limit=int(payload.get('limit',500))
        strategy=payload.get('strategy','all')
        if strategy not in tuple(STRATEGIES)+('all',):
            raise ValueError('Geçersiz strateji.')
        try:
            bars=feed.candles(symbol,interval,limit)
            config.update(feed.filters(symbol))
        except Exception as exc:
            self._record_throttle(exc)
            raise
        if strategy=='all':
            report=compare_strategies(bars,config)
        else:
            result=run_backtest(bars,config,strategy)
            report=dict(results=[result],train_candles=int(len(bars)*.7),test_candles=len(bars)-int(len(bars)*.7),notes=result.get('notes',[]))
        report.update(data_source=config['data_source'],is_synthetic=feed.is_synthetic,symbol=symbol,interval=interval,
                      fee_kind='estimate',generated_at=datetime.now(timezone.utc).isoformat())
        with self.lock:
            if generation!=self._generation:
                raise ValueError('Test sırasında ayarlar değişti; testi yeniden çalıştırın.')
            self.backtest=report
            self._save()
        return report
