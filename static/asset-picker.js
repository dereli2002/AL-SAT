'use strict';

// A shared, read-only selector. Choosing a market never creates an order.
(() => {
  const SVG = 'http://www.w3.org/2000/svg';
  const CACHE_LIMIT = 128;
  const HISTORY_TTL = 60000;
  const OBSERVATION_TTL = 4500;
  const entries = new Map();
  const pending = new Map();
  const queue = [];
  let activeRequests = 0;
  let nextId = 0;
  let activePicker = null;
  const percentFormat = new Intl.NumberFormat('tr-TR', { maximumFractionDigits: 2, minimumFractionDigits: 2 });

  function number(value) {
    if (value === '' || value === null || value === undefined) return null;
    const result = Number(value);
    return Number.isFinite(result) ? result : null;
  }
  function stamp(value) {
    if (!value) return null;
    const result = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value).getTime();
    return Number.isFinite(result) ? result : null;
  }
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function cacheEntry(source, symbol) {
    const key = `${source}|${symbol}`;
    let entry = entries.get(key);
    if (!entry) entry = {};
    entries.delete(key); entries.set(key, entry);
    while (entries.size > CACHE_LIMIT) entries.delete(entries.keys().next().value);
    return entry;
  }
  function drain() {
    while (activeRequests < 3 && queue.length) {
      const task = queue.shift();
      if (!task.valid()) { task.resolve(null); continue; }
      activeRequests += 1;
      Promise.resolve().then(task.run).then(task.resolve, task.reject).finally(() => { activeRequests -= 1; drain(); });
    }
  }
  function limited(run, valid) {
    return new Promise((resolve, reject) => { queue.push({ run, valid, resolve, reject }); drain(); });
  }
  async function json(path) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path, { cache: 'no-store', signal: controller.signal });
      let data;
      try { data = await response.json(); } catch { throw new Error('Trend yanıtı okunamadı.'); }
      if (!response.ok) throw new Error(data.error || 'Trend verisi alınamadı.');
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('Trend isteği zaman aşımına uğradı.');
      throw error;
    } finally { clearTimeout(timer); }
  }
  function checkSource(data, source) {
    if ((data.source && data.source !== source) || (data.is_synthetic !== undefined && Boolean(data.is_synthetic) !== (source === 'demo'))) {
      throw new Error('Trend verisinin kaynağı değişti; yeniden yükleniyor.');
    }
  }
  async function observations(source, symbols, valid) {
    const missing = symbols.filter((symbol) => Date.now() - (cacheEntry(source, symbol).observedAt || 0) >= OBSERVATION_TTL);
    if (!missing.length) return;
    const key = `observations|${source}|${[...missing].sort().join(',')}`;
    if (!pending.has(key)) {
      const request = limited(async () => {
        const data = await json(`/api/trends?symbols=${encodeURIComponent(missing.join(','))}`);
        checkSource(data, source);
        (Array.isArray(data.trends) ? data.trends : []).forEach((trend) => {
          if (!missing.includes(trend.symbol)) return;
          Object.assign(cacheEntry(source, trend.symbol), { observed: trend, observedAt: Date.now(), observationError: '' });
        });
        return data;
      }, valid).catch((error) => {
        missing.forEach((symbol) => Object.assign(cacheEntry(source, symbol), { observedAt: Date.now(), observationError: error.message }));
        return null;
      }).finally(() => pending.delete(key));
      pending.set(key, request);
    }
    return pending.get(key);
  }
  async function history(source, symbol, valid) {
    const entry = cacheEntry(source, symbol);
    if (Date.now() - (entry.historyAt || 0) < HISTORY_TTL) return;
    const key = `history|${source}|${symbol}|5m`;
    if (!pending.has(key)) {
      const request = limited(async () => {
        const data = await json(`/api/chart?symbol=${encodeURIComponent(symbol)}&interval=5m`);
        checkSource(data, source);
        if (data.symbol !== symbol || data.interval !== '5m') throw new Error('Trend verisinin paritesi veya dönemi uyuşmuyor.');
        Object.assign(cacheEntry(source, symbol), { history: data, historyAt: Date.now(), historyError: data.error || '' });
        return data;
      }, valid).catch((error) => {
        Object.assign(cacheEntry(source, symbol), { historyAt: Date.now(), historyError: error.message });
        return null;
      }).finally(() => pending.delete(key));
      pending.set(key, request);
    }
    return pending.get(key);
  }

  class SpotAssetPicker {
    constructor({ trigger, anchor, title = 'Varlık seçin', onSelect, onRefresh } = {}) {
      this.trigger = typeof trigger === 'string' ? document.querySelector(trigger) : trigger;
      if (!this.trigger) throw new Error('Varlık seçici için bir açma düğmesi gerekli.');
      this.anchor = (typeof anchor === 'string' ? document.querySelector(anchor) : anchor) || this.trigger;
      this.onSelect = onSelect || (() => {});
      this.onRefresh = onRefresh;
      this.context = { catalogue: [], source: 'binance', markets: [], status: {}, networkHealthy: true };
      this.rows = new Map();
      this.filtered = [];
      this.opened = false;
      this.disposed = false;
      this.activeIndex = -1;
      this.catalogueKey = '';
      this.generation = 0;
      this.size = 1;
      try { this.size = Math.max(0, Math.min(2, Number(localStorage.getItem('spot.assetPicker.previewSize') || 1))); } catch { /* Storage is optional. */ }
      const id = `spot-asset-picker-${++nextId}`;
      this.popup = element('section', 'spot-asset-picker');
      this.popup.id = id;
      this.popup.hidden = true;
      this.popup.setAttribute('role', 'dialog');
      this.popup.setAttribute('aria-label', title);
      const heading = element('div', 'spot-picker-heading');
      heading.append(element('strong', '', title));
      this.closeButton = element('button', 'spot-picker-close', '×');
      this.closeButton.type = 'button';
      this.closeButton.setAttribute('aria-label', 'Varlık listesini kapat');
      heading.append(this.closeButton);
      const searchLabel = element('label', 'spot-picker-label', 'Parite veya varlık ara');
      searchLabel.htmlFor = `${id}-search`;
      this.search = element('input', 'spot-picker-search');
      this.search.id = `${id}-search`;
      this.search.placeholder = 'BTC, ETH, SOL…';
      this.search.autocomplete = 'off';
      this.search.spellcheck = false;
      this.search.setAttribute('role', 'combobox');
      this.search.setAttribute('aria-autocomplete', 'list');
      this.search.setAttribute('aria-haspopup', 'listbox');
      this.search.setAttribute('aria-expanded', 'false');
      this.search.setAttribute('aria-controls', `${id}-list`);
      const meta = element('div', 'spot-picker-meta');
      this.count = element('span', '');
      this.refresh = element('button', 'spot-picker-refresh', 'Yenile');
      this.refresh.type = 'button';
      meta.append(this.count, this.refresh);
      const previewToolbar = element('div', 'spot-picker-preview-toolbar');
      previewToolbar.append(element('span', '', 'Küçük trendler'));
      const sizes = element('div', 'spot-picker-size-controls');
      this.smaller = element('button', '', '−');
      this.smaller.type = 'button'; this.smaller.setAttribute('aria-label', 'Küçük trendleri küçült');
      this.sizeLabel = element('span', 'spot-picker-size-label');
      this.larger = element('button', '', '+');
      this.larger.type = 'button'; this.larger.setAttribute('aria-label', 'Küçük trendleri büyüt');
      sizes.append(this.smaller, this.sizeLabel, this.larger);
      previewToolbar.append(sizes);
      this.list = element('div', 'spot-picker-list');
      this.list.id = `${id}-list`;
      this.list.setAttribute('role', 'listbox');
      this.list.setAttribute('aria-label', 'Aktif USDT pariteleri ve küçük trendleri');
      this.status = element('p', 'spot-picker-status');
      this.popup.append(heading, searchLabel, this.search, meta, previewToolbar, this.list, this.status);
      document.body.append(this.popup);
      this.trigger.setAttribute('aria-haspopup', 'dialog');
      this.trigger.setAttribute('aria-expanded', 'false');
      this.trigger.setAttribute('aria-controls', id);
      this._toggle = () => this.opened ? this.close() : this.open();
      this._outside = (event) => { if (this.opened && !this.popup.contains(event.target) && !this.trigger.contains(event.target)) this.close(false); };
      this._position = () => { if (this.opened) this.position(); };
      this._scroll = () => {
        clearTimeout(this.scrollTimer);
        this.scrollTimer = setTimeout(() => this.refreshVisible(), 70);
      };
      this.trigger.addEventListener('click', this._toggle);
      this.closeButton.addEventListener('click', () => this.close());
      this.search.addEventListener('input', () => { this.renderList(true); });
      this.search.addEventListener('keydown', (event) => this.keydown(event));
      this.popup.addEventListener('keydown', (event) => { if (event.key === 'Escape') { event.preventDefault(); this.close(); } });
      this.list.addEventListener('scroll', this._scroll, { passive: true });
      this.refresh.addEventListener('click', () => {
        if (this.onRefresh) this.onRefresh();
        this.visibleSymbols().forEach((symbol) => Object.assign(cacheEntry(this.context.source, symbol), { historyAt: 0, observedAt: 0 }));
        this.refreshVisible();
      });
      this.smaller.addEventListener('click', () => this.setSize(this.size - 1));
      this.larger.addEventListener('click', () => this.setSize(this.size + 1));
      document.addEventListener('pointerdown', this._outside, true);
      document.addEventListener('focusin', this._outside);
      window.addEventListener('resize', this._position);
      window.addEventListener('scroll', this._position, true);
      this.setSize(this.size);
      this.timer = setInterval(() => { if (this.opened) this.refreshVisible(); }, 5000);
    }

    update(context = {}) {
      if (this.disposed) return;
      const previousSource = this.context.source;
      this.context = { ...this.context, ...context };
      this.context.catalogue = Array.isArray(this.context.catalogue) ? this.context.catalogue : [];
      this.context.markets = Array.isArray(this.context.markets) ? this.context.markets : [];
      if (previousSource !== this.context.source) { this.generation += 1; this.catalogueKey = ''; }
      if (this.opened) {
        this.renderList();
        this.paintVisible();
      }
    }

    open() {
      if (this.disposed) return;
      if (activePicker && activePicker !== this) activePicker.close(false);
      activePicker = this;
      this.opened = true;
      this.generation += 1;
      // An explicit arrow always opens the full market list, not the old input filter.
      this.search.value = '';
      this.catalogueKey = '';
      this.popup.hidden = false;
      this.trigger.setAttribute('aria-expanded', 'true');
      this.search.setAttribute('aria-expanded', 'true');
      this.renderList(true);
      this.position();
      this.search.focus({ preventScroll: true });
      this.refreshVisible();
      if (!this.context.catalogue.length && !this.context.loading && this.onRefresh) this.onRefresh();
    }

    close(restoreFocus = true) {
      if (!this.opened) return;
      this.opened = false;
      this.generation += 1;
      this.popup.hidden = true;
      this.trigger.setAttribute('aria-expanded', 'false');
      this.search.setAttribute('aria-expanded', 'false');
      this.search.removeAttribute('aria-activedescendant');
      if (activePicker === this) activePicker = null;
      if (restoreFocus && this.trigger.isConnected) this.trigger.focus({ preventScroll: true });
    }

    position() {
      const rect = this.anchor.getBoundingClientRect();
      const width = Math.min(440, window.innerWidth - 24);
      const left = Math.min(Math.max(12, rect.left), Math.max(12, window.innerWidth - width - 12));
      this.popup.style.width = `${width}px`;
      this.popup.style.left = `${left}px`;
      const below = window.innerHeight - rect.bottom - 14;
      const above = rect.top - 14;
      // Prefer the roomier side when the lower side would show only one or two
      // rows. A full list normally needs roughly 550 px including its controls.
      const useAbove = below < Math.min(550, window.innerHeight - 24) && above > below;
      const space = Math.max(180, Math.min(window.innerHeight - 24, useAbove ? above : below));
      this.popup.style.maxHeight = `${space}px`;
      this.popup.style.top = useAbove ? `${Math.max(12, rect.top - this.popup.offsetHeight - 8)}px` : `${Math.min(Math.max(12, rect.bottom + 8), window.innerHeight - Math.min(space, this.popup.offsetHeight) - 12)}px`;
    }

    setSize(size) {
      this.size = Math.max(0, Math.min(2, Number.isFinite(size) ? Math.round(size) : 1));
      this.popup.dataset.previewSize = String(this.size);
      this.sizeLabel.textContent = ['Küçük', 'Orta', 'Büyük'][this.size];
      this.smaller.disabled = this.size === 0;
      this.larger.disabled = this.size === 2;
      try { localStorage.setItem('spot.assetPicker.previewSize', String(this.size)); } catch { /* Storage is optional. */ }
      if (this.opened) { this.position(); this.refreshVisible(); }
    }

    renderList(reset = false) {
      const query = this.search.value.trim().toUpperCase();
      const catalogue = this.context.catalogue.map((entry) => typeof entry === 'string' ? { symbol: entry, base_asset: entry.replace(/USDT$/, '') } : entry).filter((entry) => entry?.symbol);
      this.filtered = catalogue.filter((entry) => `${entry.symbol} ${entry.base_asset || ''}`.toUpperCase().includes(query));
      this.count.textContent = this.context.loading ? 'Liste yükleniyor…' : `${this.filtered.length} / ${catalogue.length} varlık`;
      this.refresh.disabled = Boolean(this.context.loading);
      this.status.textContent = this.context.error ? `Liste hatası: ${this.context.error}` : this.context.source === 'demo' ? 'Sentetik demo verisi · trendler alım/satım sinyali değildir.' : 'Binance Spot · 5 dk kapanışları + güncel fiyat · trendler alım/satım sinyali değildir.';
      const key = `${this.context.source}|${query}|${this.filtered.map((entry) => entry.symbol).join(',')}|${Boolean(this.context.loading)}`;
      if (key !== this.catalogueKey) {
        const scrollTop = reset ? 0 : this.list.scrollTop;
        const activeSymbol = this.filtered[this.activeIndex]?.symbol;
        this.catalogueKey = key;
        this.rows.clear();
        const fragment = document.createDocumentFragment();
        this.filtered.forEach((entry, index) => {
          const row = element('button', 'spot-picker-option');
          row.type = 'button';
          row.id = `${this.popup.id}-option-${index}`;
          row.dataset.symbol = entry.symbol;
          row.tabIndex = -1;
          row.setAttribute('role', 'option');
          const label = element('span', 'spot-picker-asset');
          label.append(element('strong', '', entry.base_asset || entry.symbol.replace(/USDT$/, '')), element('small', '', entry.symbol));
          const preview = element('span', 'spot-picker-trend');
          preview.append(element('small', 'spot-picker-trend-empty', 'Trend yükleniyor…'));
          const check = element('span', 'spot-picker-check'); check.setAttribute('aria-hidden', 'true');
          row.append(label, preview, check);
          row.addEventListener('click', () => this.select(entry.symbol));
          row.addEventListener('pointermove', () => this.highlight(index, false));
          this.rows.set(entry.symbol, { row, preview, check, entry });
          fragment.append(row);
        });
        if (!this.filtered.length) fragment.append(element('p', 'spot-picker-empty', this.context.loading ? 'Pariteler yükleniyor…' : this.context.error ? 'Liste alınamadı; Yenile düğmesini kullanın.' : 'Aramayla eşleşen varlık bulunamadı.'));
        this.list.replaceChildren(fragment);
        this.list.scrollTop = scrollTop;
        this.activeIndex = reset ? (this.filtered.length ? 0 : -1) : Math.max(0, this.filtered.findIndex((entry) => entry.symbol === activeSymbol));
        this.highlight(this.activeIndex, false);
        requestAnimationFrame(() => { if (this.opened) { this.position(); this.refreshVisible(); } });
      }
      for (const [symbol, item] of this.rows) {
        const selected = symbol === this.context.selectedSymbol;
        item.row.setAttribute('aria-selected', String(selected));
        item.check.textContent = selected ? '✓' : '';
      }
    }

    highlight(index, scroll = true) {
      if (!this.filtered.length) { this.activeIndex = -1; this.search.removeAttribute('aria-activedescendant'); return; }
      this.activeIndex = Math.max(0, Math.min(this.filtered.length - 1, index));
      const active = this.rows.get(this.filtered[this.activeIndex].symbol)?.row;
      this.rows.forEach(({ row }) => row.classList.toggle('is-highlighted', row === active));
      if (active) {
        this.search.setAttribute('aria-activedescendant', active.id);
        if (scroll) active.scrollIntoView({ block: 'nearest' });
      }
    }

    keydown(event) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        this.highlight(this.activeIndex + (event.key === 'ArrowDown' ? 1 : -1));
      } else if (event.key === 'Enter' && this.filtered[this.activeIndex]) {
        event.preventDefault(); this.select(this.filtered[this.activeIndex].symbol);
      } else if (event.key === 'Home' && event.ctrlKey) {
        event.preventDefault(); this.highlight(0);
      } else if (event.key === 'End' && event.ctrlKey) {
        event.preventDefault(); this.highlight(this.filtered.length - 1);
      }
    }

    select(symbol) {
      if (!this.rows.has(symbol)) return;
      this.close();
      this.onSelect(symbol);
    }

    visibleSymbols() {
      if (!this.opened || !this.list.clientHeight) return [];
      const top = this.list.scrollTop;
      const bottom = top + this.list.clientHeight;
      const origin = this.list.getBoundingClientRect().top;
      const symbols = [];
      for (const [symbol, { row }] of this.rows) {
        const rowTop = row.getBoundingClientRect().top - origin + top;
        if (rowTop + row.offsetHeight > top && rowTop < bottom) symbols.push(symbol);
      }
      return symbols.slice(0, 50);
    }

    paintVisible() { this.visibleSymbols().forEach((symbol) => this.paint(symbol)); }

    async refreshVisible() {
      if (!this.opened || this.disposed) return;
      const source = this.context.source;
      const generation = this.generation;
      const symbols = this.visibleSymbols();
      if (!symbols.length) return;
      const valid = (symbol) => !this.disposed && this.opened && this.generation === generation && this.context.source === source && (!symbol || this.visibleSymbols().includes(symbol));
      this.paintVisible();
      // Quote observations are batched; exchange history is lazy and shares a
      // three-request ceiling across every selector instance on this page.
      observations(source, symbols, () => valid()).then(() => { if (valid()) this.paintVisible(); });
      symbols.forEach((symbol) => history(source, symbol, () => valid(symbol)).then(() => { if (valid(symbol)) this.paint(symbol); }));
    }

    paint(symbol) {
      const item = this.rows.get(symbol);
      if (!item) return;
      const entry = cacheEntry(this.context.source, symbol);
      const chart = entry.history;
      const observed = entry.observed;
      let points = (Array.isArray(chart?.candles) ? chart.candles : []).slice(-24).map((bar) => ({ time: stamp(bar.close_time), price: number(bar.close) })).filter((point) => point.time && point.price > 0);
      let basis = `5 dk × ${points.length} kapanış`;
      let stale = Boolean(chart?.history_stale || entry.historyError);
      if (points.length >= 2) {
        const last = points[points.length - 1];
        stale ||= Date.now() - last.time > 660000;
        const market = this.context.markets.find((candidate) => candidate.symbol === symbol);
        const quoteTime = stamp(market?.updated_at);
        const quote = number(market?.price);
        const freshQuote = this.context.networkHealthy !== false && this.context.status?.fresh && quote > 0 && quoteTime && Date.now() - quoteTime < 30000;
        if (freshQuote && quoteTime > last.time) {
          points.push({ time: quoteTime, price: quote });
          basis += ' + canlı';
        }
      } else {
        points = (Array.isArray(observed?.points) ? observed.points : []).map((point) => ({ time: stamp(point.time), price: number(point.price) })).filter((point) => point.time && point.price > 0);
        basis = observed?.basis === 'closed_candles' ? `${observed.interval || '5m'} × ${points.length} kapanış` : `5 sn × ${points.length} gözlem`;
        stale = Boolean(observed?.stale || entry.observationError);
      }
      stale ||= this.context.networkHealthy === false || this.context.status?.fresh === false;
      const preview = item.preview;
      preview.classList.toggle('is-stale', stale);
      if (points.length < 2) {
        const error = entry.historyError || entry.observationError;
        const message = error ? 'Trend verisi yok' : observed ? 'Fiyat birikiyor…' : 'Trend yükleniyor…';
        preview.replaceChildren(element('small', 'spot-picker-trend-empty', message));
        preview.title = error || 'Grafik yalnız gerçek fiyat gözlemleriyle çizilir.';
        item.row.setAttribute('aria-label', `${item.entry.symbol}; ${message}`);
        return;
      }
      const change = (points[points.length - 1].price / points[0].price - 1) * 100;
      const direction = change > 0 ? 'up' : change < 0 ? 'down' : 'flat';
      const directionLabel = change > 0 ? 'Yukarı' : change < 0 ? 'Aşağı' : 'Yatay';
      const percent = `${change > 0 ? '+' : change < 0 ? '−' : ''}%${percentFormat.format(Math.abs(change))}`;
      const minimum = Math.min(...points.map((point) => point.price));
      const maximum = Math.max(...points.map((point) => point.price));
      const range = maximum - minimum;
      const svg = document.createElementNS(SVG, 'svg');
      svg.setAttribute('viewBox', '0 0 120 40');
      svg.setAttribute('preserveAspectRatio', 'none');
      svg.setAttribute('aria-hidden', 'true');
      const path = document.createElementNS(SVG, 'path');
      path.setAttribute('d', points.map((point, index) => `${index ? 'L' : 'M'}${(2 + index / (points.length - 1) * 116).toFixed(2)},${(range ? 35 - (point.price - minimum) / range * 30 : 20).toFixed(2)}`).join(' '));
      svg.append(path);
      const chartBox = element('span', `spot-picker-sparkline trend-${direction}`); chartBox.append(svg);
      const detail = element('span', 'spot-picker-trend-detail');
      const changeLabel = element('strong', `trend-${direction}`);
      const directionMark = element('span', `spot-picker-direction direction-${direction}`);
      directionMark.setAttribute('aria-hidden', 'true');
      changeLabel.append(directionMark, document.createTextNode(`${directionLabel} ${percent}`));
      detail.append(changeLabel, element('small', '', stale ? 'Gecikmiş veri' : basis));
      preview.replaceChildren(chartBox, detail);
      const duration = Math.max(0, (points[points.length - 1].time - points[0].time) / 60000);
      const period = duration >= 1 ? `${Math.round(duration)} dk` : `${Math.round(duration * 60)} sn`;
      preview.title = `${basis} · ${points.length} gerçek nokta · ${period} boyunca ${percent}${stale ? ' · Veri güncel değil' : ''}`;
      item.row.setAttribute('aria-label', `${item.entry.symbol}; ${change > 0 ? 'yukarı' : change < 0 ? 'aşağı' : 'yatay'} eğilim ${percent}; ${basis}; ${period}${stale ? '; gecikmiş veri' : ''}`);
    }

    destroy() {
      this.close(false);
      this.disposed = true;
      clearInterval(this.timer); clearTimeout(this.scrollTimer);
      this.trigger.removeEventListener('click', this._toggle);
      document.removeEventListener('pointerdown', this._outside, true);
      document.removeEventListener('focusin', this._outside);
      window.removeEventListener('resize', this._position);
      window.removeEventListener('scroll', this._position, true);
      this.popup.remove();
    }
  }

  window.SpotAssetPicker = SpotAssetPicker;
})();
