/* Actual OHLC display only. This component never changes bot configuration. */
(() => {
  'use strict';
  const INTERVALS = Object.freeze({ '1s': 1, '1m': 60, '5m': 300, '15m': 900, '1h': 3600 });
  const LABELS = { '1s': '1 saniye', '1m': '1 dakika', '5m': '5 dakika', '15m': '15 dakika', '1h': '1 saat' };
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const instances = new Set();
  const cache = new Map();
  const jobs = new Map();
  const queue = [];
  let active = 0;
  const MAX_CONCURRENCY = 3;
  const CACHE_LIMIT = 160;
  const datetime = new Intl.DateTimeFormat('tr-TR', { timeZone: 'Europe/Istanbul', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const clocktime = new Intl.DateTimeFormat('tr-TR', { timeZone: 'Europe/Istanbul', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const isNumber = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
  const number = (value) => isNumber(value) ? Number(value) : null;
  const instant = (value) => typeof value === 'number' ? value : Date.parse(value || '') || 0;
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  function price(value) {
    if (!isNumber(value)) return '—';
    const absolute = Math.abs(Number(value));
    if (absolute > 0 && absolute < 1e-12) return Number(value).toExponential(4).replace('.', ',');
    const digits = absolute >= 1000 ? 2 : absolute >= 1 ? 5 : absolute ? clamp(Math.ceil(-Math.log10(absolute)) + 4, 5, 16) : 2;
    return Number(value).toLocaleString('tr-TR', { maximumFractionDigits: digits });
  }
  function element(tag, attributes = {}, content) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
    if (content !== undefined) node.textContent = content;
    return node;
  }
  function boundedCache(key, data) {
    cache.delete(key); cache.set(key, data);
    while (cache.size > CACHE_LIMIT) cache.delete(cache.keys().next().value);
  }
  function refreshMilliseconds(interval) { return interval === '1s' ? 1000 : interval === '15m' || interval === '1h' ? 5000 : 3000; }
  function wake() {
    for (const instance of instances) instance.tick();
  }
  // A single queue bounds requests across the main chart and all position cards.
  function pump() {
    if (document.hidden) return;
    while (active < MAX_CONCURRENCY && queue.length) {
      const job = queue.shift();
      for (const client of [...job.clients]) if (!client.alive || client.key !== job.key || !client.canPoll()) job.clients.delete(client);
      if (!job.clients.size) { jobs.delete(job.key); continue; }
      active += 1;
      job.controller = new AbortController();
      const timeout = setTimeout(() => job.controller.abort(), 15000);
      fetch(`/api/chart?symbol=${encodeURIComponent(job.symbol)}&interval=${encodeURIComponent(job.interval)}`, { signal: job.controller.signal, cache: 'no-store', headers: { Accept: 'application/json' } })
        .then(async (response) => {
          let data;
          try { data = await response.json(); } catch (_) { throw new Error('Grafik sunucusunun yanıtı okunamadı.'); }
          if (!response.ok) throw new Error(data?.error || `Grafik isteği başarısız (${response.status}).`);
          if (data.symbol !== job.symbol || data.interval !== job.interval || data.source !== job.source || Boolean(data.is_synthetic) !== (job.source === 'demo')) throw new Error('Grafik paritesi, zaman aralığı veya veri kaynağı değişti; yenileme bekleniyor.');
          const record = { data, receivedAt: Date.now(), retryAt: Date.now() + refreshMilliseconds(job.interval), error: data.error || '' };
          boundedCache(job.key, record);
          for (const client of job.clients) if (client.alive && client.key === job.key) client.receive(record);
        })
        .catch((error) => {
          if (!job.clients.size) return;
          const previous = cache.get(job.key);
          const failures = Math.min(4, (previous?.failures || 0) + 1);
          const record = { data: previous?.data || null, receivedAt: previous?.receivedAt || 0, retryAt: Date.now() + Math.min(30000, 3000 * 2 ** (failures - 1)), failures, error: error.name === 'AbortError' ? 'Grafik isteği zaman aşımına uğradı.' : error.message };
          boundedCache(job.key, record);
          for (const client of job.clients) if (client.alive && client.key === job.key) client.receive(record);
        })
        .finally(() => { clearTimeout(timeout); active -= 1; if (jobs.get(job.key) === job) jobs.delete(job.key); pump(); });
    }
  }
  function request(client) {
    const existing = jobs.get(client.key);
    if (existing) { existing.clients.add(client); return; }
    const job = { key: client.key, symbol: client.symbol, source: client.source, interval: client.interval, clients: new Set([client]), controller: null };
    jobs.set(job.key, job); queue.push(job); pump();
  }
  function leaveJob(client, key) {
    const job = jobs.get(key);
    if (!job) return;
    job.clients.delete(client);
    if (!job.clients.size && job.controller) job.controller.abort();
  }
  function normalCandle(raw, live = false) {
    const values = ['open', 'high', 'low', 'close'].map((field) => number(raw?.[field]));
    const time = instant(raw?.open_time);
    if (!time || values.some((value) => !(value > 0)) || values[1] < Math.max(values[0], values[3]) || values[2] > Math.min(values[0], values[3]) || values[2] > values[1]) return null;
    return { ...raw, time, open: values[0], high: values[1], low: values[2], close: values[3], live };
  }

  class SpotMarketChart {
    constructor(container, options = {}) {
      if (!(container instanceof HTMLElement)) throw new TypeError('Grafik için bir HTML kapsayıcı gerekli.');
      this.container = container;
      this.interval = Object.hasOwn(INTERVALS, options.interval) ? options.interval : '5m';
      this.height = clamp(Number(options.height) || 260, 160, 500);
      this.count = clamp(Number(options.visibleCandles) || 60, 12, 100);
      this.offset = 0;
      this.alive = true;
      this.visible = true;
      this.width = 800;
      this.record = null;
      this.pollTimer = null;
      this.symbol = '';
      this.source = '';
      this.key = '';
      this.input = {};
      this.onIntervalChange = options.onIntervalChange;
      this.hoverIndex = null;
      this.nodes = {};
      this.build();
      instances.add(this);
      if (typeof ResizeObserver === 'function') {
        this.resizeObserver = new ResizeObserver((entries) => {
          const width = entries[0]?.contentRect.width;
          if (width > 0 && Math.abs(width - this.width) > 1) { this.width = width; this.draw(); }
        });
        this.resizeObserver.observe(this.nodes.stage);
      }
      if (typeof IntersectionObserver === 'function') {
        this.visibilityObserver = new IntersectionObserver((entries) => {
          this.visible = entries[0]?.isIntersecting !== false;
          if (this.visible) this.tick();
          else this.clearPollTimer();
        }, { rootMargin: '160px' });
        this.visibilityObserver.observe(container);
      }
      if (options.symbol) this.update(options);
    }
    build() {
      const root = document.createElement('section');
      root.className = 'spot-market-chart';
      root.setAttribute('aria-label', 'Canlı mum grafiği');
      // All HTML below is fixed; symbols, errors and exchange data use textContent.
      root.innerHTML = '<div class="smc-toolbar"><label class="smc-interval-label">Mum aralığı <select class="smc-interval" aria-label="Grafik mum aralığı"></select></label><div class="smc-tools" role="group" aria-label="Grafiği yakınlaştır ve taşı"><button type="button" data-action="zoom-in" title="Daha az mum, daha yakın görünüm" aria-label="Mumları yakınlaştır">＋</button><button type="button" data-action="zoom-out" title="Daha çok mum, daha uzak görünüm" aria-label="Mumları uzaklaştır">−</button><button type="button" data-action="older" title="Daha eski mumları göster" aria-label="Önceki mumlara git">‹</button><button type="button" data-action="newer" title="Daha yeni mumları göster" aria-label="Sonraki mumlara git">›</button><button type="button" data-action="latest" title="Son mumları göster ve yakınlaştırmayı sıfırla">Son mumlar</button></div><div class="smc-tools" role="group" aria-label="Grafik yüksekliği"><button type="button" data-action="height-down" aria-label="Grafiği küçült" title="Grafik yüksekliğini azalt">Küçült</button><button type="button" data-action="height-up" aria-label="Grafiği büyüt" title="Grafik yüksekliğini artır">Büyüt</button><button type="button" data-action="expand" aria-label="Grafiği tam ekran aç" title="Tam ekran">Tam ekran</button></div></div><p class="smc-hint">Görünüm aralığı stratejinin işlem aralığını değiştirmez. Fareyi mumun üzerine getirin; yakınlaştırmak için + / − kullanın.</p><div class="smc-stage" tabindex="0" role="group" aria-label="Mum grafiği, artı eksi ile yakınlaştırın, oklarla geçmişe gidin"><svg class="smc-svg" role="img" aria-label="Açılış, en yüksek, en düşük ve kapanış fiyatları"></svg><div class="smc-empty">Fiyat geçmişi yükleniyor…</div><div class="smc-tooltip" hidden></div></div><div class="smc-footer"><span class="smc-range">—</span><span class="smc-scale">—</span></div><p class="smc-status" aria-live="polite">Grafik için varlık bekleniyor.</p>';
      this.container.replaceChildren(root);
      this.root = root;
      for (const name of ['interval', 'stage', 'svg', 'empty', 'tooltip', 'range', 'scale', 'status']) this.nodes[name] = root.querySelector(`.smc-${name}`);
      for (const [value, label] of Object.entries(LABELS)) {
        const option = document.createElement('option'); option.value = value; option.textContent = label; this.nodes.interval.append(option);
      }
      this.nodes.interval.value = this.interval;
      this.nodes.interval.addEventListener('change', () => this.setInterval(this.nodes.interval.value));
      root.querySelectorAll('[data-action]').forEach((button) => button.addEventListener('click', () => this.action(button.dataset.action)));
      this.nodes.stage.addEventListener('keydown', (event) => {
        const action = { '+': 'zoom-in', '=': 'zoom-in', '-': 'zoom-out', ArrowLeft: 'older', ArrowRight: 'newer', Home: 'latest' }[event.key];
        if (action) { event.preventDefault(); this.action(action); }
      });
      this.nodes.stage.addEventListener('pointermove', (event) => this.pointer(event));
      this.nodes.stage.addEventListener('pointerleave', () => { this.hoverIndex = null; this.nodes.tooltip.hidden = true; this.draw(); });
      this.nodes.stage.style.height = `${this.height}px`;
    }
    canPoll() { return this.alive && !document.hidden && this.visible && this.container.isConnected && Boolean(this.symbol && this.source); }
    update(input = {}) {
      if (!this.alive) return;
      this.input = { ...this.input, ...input };
      const symbol = this.input.symbol || '';
      const source = this.input.source || this.input.market?.source || 'binance';
      const key = `${source}|${symbol}|${this.interval}`;
      if (this.key !== key) {
        this.clearPollTimer();
        leaveJob(this, this.key);
        this.symbol = symbol; this.source = source; this.key = key;
        this.offset = 0; this.hoverIndex = null; this.nodes.tooltip.hidden = true;
        this.record = cache.get(key) || null;
        this.root.setAttribute('aria-label', `${symbol} canlı mum grafiği`);
        this.nodes.svg.setAttribute('aria-label', `${symbol} ${LABELS[this.interval]} açılış, en yüksek, en düşük ve kapanış fiyatları`);
      }
      this.draw(); this.tick();
    }
    setInterval(interval) {
      if (!Object.hasOwn(INTERVALS, interval) || interval === this.interval || !this.alive) return;
      this.interval = interval; this.nodes.interval.value = interval;
      this.update();
      if (typeof this.onIntervalChange === 'function') this.onIntervalChange(interval);
    }
    action(action) {
      if (!this.alive) return;
      if (action === 'zoom-in') this.count = clamp(Math.round(this.count / 1.4), 12, 100);
      if (action === 'zoom-out') this.count = clamp(Math.round(this.count * 1.4), 12, 100);
      if (action === 'older') this.offset += Math.max(1, Math.floor(this.count / 3));
      if (action === 'newer') this.offset -= Math.max(1, Math.floor(this.count / 3));
      if (action === 'latest') { this.offset = 0; this.count = 60; }
      if (action === 'height-down') this.height = clamp(this.height - 50, 160, 500);
      if (action === 'height-up') this.height = clamp(this.height + 50, 160, 500);
      if (action === 'expand') {
        if (document.fullscreenElement === this.root) document.exitFullscreen?.().catch(() => {});
        else if (this.root.requestFullscreen) this.root.requestFullscreen().catch(() => { this.height = 500; this.draw(); });
        else this.height = this.height >= 500 ? 260 : 500;
      }
      this.hoverIndex = null; this.nodes.tooltip.hidden = true; this.draw();
    }
    clearPollTimer() {
      if (this.pollTimer !== null) clearTimeout(this.pollTimer);
      this.pollTimer = null;
    }
    schedulePoll() {
      this.clearPollTimer();
      const due = this.record?.retryAt;
      if (!this.canPoll() || !due || due <= Date.now()) return;
      // A response may finish between global ticks: refresh at its actual due time.
      this.pollTimer = setTimeout(() => { this.pollTimer = null; this.tick(); }, Math.max(1, due - Date.now()));
    }
    tick() {
      if (!this.canPoll()) { this.clearPollTimer(); return; }
      const record = cache.get(this.key);
      if (record && record !== this.record) this.receive(record);
      if (!record || Date.now() >= record.retryAt) { this.clearPollTimer(); request(this); }
      else { this.status(); this.schedulePoll(); }
    }
    receive(record) {
      if (!this.alive) return;
      // Keep a historical viewport anchored to its last candle as new bars arrive.
      const anchor = this.offset > 0 ? this.plotted?.[this.plotted.length - 1]?.time : null;
      this.record = record;
      if (anchor) {
        const bars = this.candles();
        const index = bars.findIndex((bar) => bar.time === anchor);
        if (index >= 0) this.offset = bars.length - index - 1;
      }
      this.draw(); this.schedulePoll();
    }
    candles() {
      const data = this.record?.data;
      const candles = (Array.isArray(data?.candles) ? data.candles : []).map((raw) => normalCandle(raw)).filter(Boolean);
      const live = data?.live_candle ? normalCandle(data.live_candle, true) : null;
      if (live) candles.push(live);
      return [...new Map(candles.sort((a, b) => a.time - b.time).map((candle) => [candle.time, candle])).values()];
    }
    currentQuote() {
      const now = Date.now();
      const market = this.input.market || {};
      const marketTime = instant(market.updated_at || market.quote_at || market.quote_time);
      const marketFresh = this.input.networkHealthy !== false && this.input.statusFresh !== false && market.quote_missing !== true && (!market.symbol || market.symbol === this.symbol) && (!market.source || market.source === this.source) && number(market.price) > 0 && marketTime > 0 && now - marketTime < 30000 && now - marketTime >= -5000;
      const data = this.record?.data;
      const quoteTime = instant(data?.quote_time);
      const chartFresh = data?.quote_fresh === true && number(data?.price) > 0 && quoteTime > 0 && now - quoteTime < 30000 && now - quoteTime >= -5000;
      if (chartFresh && (!marketFresh || quoteTime > marketTime)) return { price: number(data.price), time: quoteTime };
      if (marketFresh) return { price: number(market.price), time: marketTime };
      return null;
    }
    status() {
      const data = this.record?.data;
      const closedCount = Array.isArray(data?.candles) ? data.candles.length : 0;
      const lastClose = instant(data?.candles?.[closedCount - 1]?.close_time);
      const age = this.record?.receivedAt ? Date.now() - this.record.receivedAt : Infinity;
      const stale = Boolean(this.record?.error || data?.history_stale || age > Math.max(15000, refreshMilliseconds(this.interval) * 3) || (lastClose && this.source !== 'demo' && Date.now() - lastClose > INTERVALS[this.interval] * 2000 + 10000));
      let message = !this.symbol ? 'Grafik için varlık bekleniyor.' : !data ? this.record?.error ? `Grafik alınamadı: ${this.record.error}` : 'Gerçek OHLC mumları yükleniyor…' : `${closedCount} kapanmış mum · ${LABELS[this.interval]} · ${this.source === 'demo' ? 'sentetik demo' : 'Binance Spot OHLC'}`;
      if (data) {
        if (data.live_candle && !stale) message += ' · oluşan mum';
        if (this.record?.error) message += ` · Yenileme başarısız: ${this.record.error}`;
        else if (stale) message += ' · geçmiş güncel değil, yenileme bekleniyor';
        else message += ` · ${clocktime.format(new Date(this.record.receivedAt))}`;
        if (!this.currentQuote()) message += ' · güncel kotasyon bekleniyor';
      }
      if (document.hidden) message += ' · pencere görünür olduğunda yenilenir';
      this.nodes.status.textContent = message;
      this.nodes.status.classList.toggle('smc-warning', stale && Boolean(data || this.record?.error));
      this.nodes.empty.textContent = this.record?.error ? `Mum verisi alınamadı: ${this.record.error}` : 'OHLC mumları yükleniyor…';
    }
    draw() {
      if (!this.alive) return;
      const { svg, stage } = this.nodes;
      const fullscreen = document.fullscreenElement === this.root;
      const height = fullscreen ? Math.max(260, window.innerHeight - 180) : this.height;
      stage.style.height = `${height}px`;
      const width = Math.max(260, stage.clientWidth || this.width || 800);
      this.width = width;
      svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
      const all = this.candles();
      this.offset = clamp(this.offset, 0, Math.max(0, all.length - Math.min(this.count, all.length)));
      const end = all.length - this.offset;
      const bars = all.slice(Math.max(0, end - this.count), end);
      this.plotted = bars;
      this.nodes.empty.hidden = bars.length > 0;
      svg.replaceChildren();
      this.nodes.scale.textContent = `${bars.length} mum · ${height}px${this.offset ? ' · geçmiş görünümü' : ''}`;
      this.root.querySelector('[data-action="zoom-in"]').disabled = this.count <= 12;
      this.root.querySelector('[data-action="zoom-out"]').disabled = this.count >= 100;
      this.root.querySelector('[data-action="older"]').disabled = this.offset >= Math.max(0, all.length - Math.min(this.count, all.length));
      this.root.querySelector('[data-action="newer"]').disabled = this.offset <= 0;
      this.root.querySelector('[data-action="height-down"]').disabled = this.height <= 160;
      this.root.querySelector('[data-action="height-up"]').disabled = this.height >= 500;
      if (!bars.length) { this.nodes.range.textContent = '—'; this.status(); return; }
      const left = 8, top = 14, right = width < 450 ? 77 : 96, bottom = 26;
      const plotWidth = Math.max(80, width - right - left), plotHeight = height - top - bottom;
      const position = this.input.position;
      const refs = [
        { value: number(position?.entry_price), name: 'Alış', className: 'smc-entry' },
        { value: number(position?.fixed_stop_price), name: 'Stop', className: 'smc-stop' },
        { value: position?.trailing_active ? number(position?.trailing_stop_price) : null, name: 'Takip', className: 'smc-trailing' },
      ].filter((ref) => ref.value > 0);
      const quote = this.currentQuote();
      const quotePrice = quote?.price > 0 ? quote.price : null;
      const values = bars.flatMap((bar) => [bar.high, bar.low]);
      // References outside the visible price range stay at the edge, so distant stops cannot flatten small moves.
      let low = Math.min(...values), high = Math.max(...values);
      if (!this.offset && quotePrice) { low = Math.min(low, quotePrice); high = Math.max(high, quotePrice); }
      const padding = Math.max((high - low) * 0.1, high * 0.00005, Number.MIN_VALUE);
      low = Math.max(0, low - padding); high += padding;
      const y = (value) => top + (high - value) / (high - low || 1) * plotHeight;
      const step = plotWidth / bars.length;
      const bodyWidth = Math.max(1, Math.min(18, step * 0.64));
      this.geometry = { left, top, plotWidth, plotHeight, step, height, width, y };
      const fragment = document.createDocumentFragment();
      for (let i = 0; i < 4; i += 1) {
        const value = high - (high - low) * i / 3;
        const positionY = y(value);
        fragment.append(element('line', { x1: left, y1: positionY, x2: left + plotWidth, y2: positionY, class: 'smc-grid' }));
        fragment.append(element('text', { x: left + plotWidth + 7, y: positionY + 4, class: 'smc-price-axis' }, price(value)));
      }
      for (const ref of refs) {
        const edge = ref.value < low || ref.value > high;
        const positionY = clamp(y(ref.value), top + 9, top + plotHeight - 7);
        fragment.append(element('line', { x1: left, y1: positionY, x2: left + plotWidth, y2: positionY, class: `smc-reference ${ref.className}${edge ? ' smc-reference-outside' : ''}` }));
        fragment.append(element('text', { x: left + 5, y: positionY - 4, class: `smc-reference-label ${ref.className}` }, `${ref.name} ${price(ref.value)}${edge ? ref.value > high ? ' ↑' : ' ↓' : ''}`));
      }
      bars.forEach((bar, index) => {
        const x = left + (index + 0.5) * step;
        const className = `${bar.close >= bar.open ? 'smc-up' : 'smc-down'}${bar.live ? ' smc-forming' : ''}`;
        const group = element('g', { class: className });
        group.append(element('line', { x1: x, x2: x, y1: y(bar.high), y2: y(bar.low), class: 'smc-wick' }));
        group.append(element('rect', { x: x - bodyWidth / 2, y: Math.min(y(bar.open), y(bar.close)) - (bar.open === bar.close ? 0.7 : 0), width: bodyWidth, height: Math.max(1.4, Math.abs(y(bar.open) - y(bar.close))), class: 'smc-body' }));
        fragment.append(group);
      });
      if (quotePrice && !this.offset) {
        const positionY = y(quotePrice);
        fragment.append(element('line', { x1: left, y1: positionY, x2: left + plotWidth, y2: positionY, class: 'smc-quote-line' }));
        fragment.append(element('rect', { x: left + plotWidth + 2, y: positionY - 9, width: right - 5, height: 18, rx: 3, class: 'smc-quote-box' }));
        fragment.append(element('text', { x: left + plotWidth + 6, y: positionY + 4, class: 'smc-quote-text' }, price(quotePrice)));
      }
      const indexes = bars.length > 20 && width > 550 ? [0, Math.floor((bars.length - 1) / 2), bars.length - 1] : [0, bars.length - 1];
      for (const index of indexes) {
        const anchor = index === 0 ? 'start' : index === bars.length - 1 ? 'end' : 'middle';
        fragment.append(element('text', { x: index === 0 ? left : index === bars.length - 1 ? left + plotWidth : left + (index + 0.5) * step, y: height - 5, 'text-anchor': anchor, class: 'smc-time-axis' }, clocktime.format(new Date(bars[index].time))));
      }
      if (this.hoverIndex !== null && bars[this.hoverIndex]) {
        const bar = bars[this.hoverIndex], x = left + (this.hoverIndex + 0.5) * step;
        fragment.append(element('line', { x1: x, x2: x, y1: top, y2: top + plotHeight, class: 'smc-crosshair' }));
        fragment.append(element('circle', { cx: x, cy: y(bar.close), r: 3, class: 'smc-crosshair-dot' }));
      }
      svg.append(fragment);
      this.nodes.range.textContent = `${datetime.format(new Date(bars[0].time))} — ${datetime.format(new Date(bars[bars.length - 1].time))}`;
      this.status();
    }
    pointer(event) {
      if (!this.geometry || !this.plotted?.length) return;
      const rect = this.nodes.stage.getBoundingClientRect();
      const { left, plotWidth, step } = this.geometry;
      const px = event.clientX - rect.left;
      if (px < left || px > left + plotWidth) { this.nodes.tooltip.hidden = true; return; }
      const index = clamp(Math.floor((px - left) / step), 0, this.plotted.length - 1);
      this.hoverIndex = index;
      const bar = this.plotted[index];
      this.nodes.tooltip.textContent = `${datetime.format(new Date(bar.time))}${bar.live ? ' · Oluşuyor' : ' · Kapandı'}\nAçılış: ${price(bar.open)}  Yüksek: ${price(bar.high)}\nDüşük: ${price(bar.low)}  Kapanış: ${price(bar.close)} USDT`;
      this.nodes.tooltip.hidden = false;
      const tooltipWidth = Math.min(340, rect.width - 12);
      this.nodes.tooltip.style.maxWidth = `${tooltipWidth}px`;
      this.nodes.tooltip.style.left = `${clamp(px + 12, 5, Math.max(5, rect.width - tooltipWidth - 5))}px`;
      this.nodes.tooltip.style.top = `${clamp(event.clientY - rect.top + 14, 6, this.geometry.height - 78)}px`;
      this.draw();
    }
    dispose() {
      if (!this.alive) return;
      this.alive = false;
      this.clearPollTimer();
      leaveJob(this, this.key);
      instances.delete(this);
      this.resizeObserver?.disconnect(); this.visibilityObserver?.disconnect();
      this.container.replaceChildren();
    }
  }
  window.SpotMarketChart = SpotMarketChart;
  setInterval(wake, 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { wake(); pump(); } });
  document.addEventListener('fullscreenchange', () => { for (const instance of instances) instance.draw(); });
})();
