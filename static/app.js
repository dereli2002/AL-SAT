'use strict';

const $ = (id) => document.getElementById(id);
const configForm = $('configForm');
const strategies = {
  ema_cross: { name: 'EMA kesişimi', description: 'Kısa ve uzun dönem ortalamalarının kesişimini izler.' },
  rsi_reversion: { name: 'RSI dönüşü', description: 'Aşırı satım sonrası toparlanma sinyallerini izler.' },
  breakout: { name: 'Fiyat kırılımı', description: 'Önceki fiyat aralığının üzerindeki kırılımı izler.' },
};
const reasonNames = { stop_loss: 'Alış altı stop', fixed_stop: 'Alış altı stop', trailing_stop: 'Kâr takibi stop', trailing_price: 'Fiyat takibi stop', trailing_profit: 'Net kâr takibi', strategy: 'Strateji sinyali', signal: 'Strateji sinyali', sell_signal: 'Satış sinyali', manual: 'Manuel kapatma', manual_close: 'Manuel kapatma', daily_loss: 'Günlük risk limiti', daily_loss_limit: 'Günlük risk limiti', end_of_test: 'Test sonu', backtest_end: 'Test sonu' };
const formatNumber = new Intl.NumberFormat('tr-TR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const formatPrice = new Intl.NumberFormat('tr-TR', { maximumFractionDigits: 8 });
const formatQuantity = new Intl.NumberFormat('tr-TR', { maximumFractionDigits: 8 });
let state = null;
let configDirty = false;
let hasHydrated = false;
let requestBusy = false;
let backtestBusy = false;
let fetching = false;
let toastTimer;
let chartKey = '';
let chartPoints = [];

function numeric(value) {
  if (value === null || value === undefined || value === '') return null;
  const result = Number(value);
  return Number.isFinite(result) ? result : null;
}
function money(value, signed = false) {
  const n = numeric(value);
  if (n === null) return '—';
  return `${signed && n > 0 ? '+' : ''}${formatNumber.format(n)}`;
}
function price(value) {
  const n = numeric(value);
  return n === null ? '—' : formatPrice.format(n);
}
function percent(value, signed = false) { const n = numeric(value); return n === null ? '—' : `${signed && n > 0 ? '+' : ''}%${formatNumber.format(n)}`; }
function dateTime(value) {
  if (!value) return '—';
  const parsed = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
  if (Number.isNaN(parsed.getTime())) return '—';
  return new Intl.DateTimeFormat('tr-TR', { timeZone: 'Europe/Istanbul', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(parsed);
}
function text(id, value) { $(id).textContent = value ?? '—'; }
function paintValue(id, value, options = {}) {
  const el = $(id);
  el.textContent = `${money(value, options.signed)}${options.suffix || ''}`;
  if (options.color) {
    el.classList.remove('positive', 'negative', 'neutral');
    const n = numeric(value);
    el.classList.add(n > 0 ? 'positive' : n < 0 ? 'negative' : 'neutral');
  }
}
function toast(message, isError = false) {
  const el = $('toast');
  clearTimeout(toastTimer);
  el.textContent = message;
  el.classList.toggle('error', isError);
  el.hidden = false;
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 9000 : 4500);
}
async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), path.includes('backtest') ? 120000 : 30000);
  try {
    const response = await fetch(path, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      signal: controller.signal,
      cache: 'no-store',
    });
    let data;
    try { data = await response.json(); } catch { throw new Error('Sunucudan beklenen yanıt alınamadı.'); }
    if (!response.ok) throw new Error(data.error || data.message || `İstek başarısız (${response.status}).`);
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('İstek zaman aşımına uğradı. Botun durumunu kontrol edin.');
    throw error;
  } finally { clearTimeout(timer); }
}

function updateChart(market) {
  const n = numeric(market.price);
  const key = `${market.symbol}|${market.source}|${state.config?.interval}`;
  if (chartKey !== key) { chartKey = key; chartPoints = []; }
  if (n !== null && n > 0) {
    const stamp = String(market.updated_at ?? '');
    const last = chartPoints[chartPoints.length - 1];
    if (!last || (stamp ? last.stamp !== stamp : last.price !== n)) chartPoints.push({ price: n, stamp });
    if (chartPoints.length > 80) chartPoints.shift();
  }
  const ready = chartPoints.length > 1;
  $('chartPlaceholder').hidden = ready;
  if (!ready) { $('chartLine').setAttribute('d', ''); $('chartArea').setAttribute('d', ''); return; }
  const values = chartPoints.map((point) => point.price);
  const low = Math.min(...values);
  const high = Math.max(...values);
  const spread = high - low || high * 0.002 || 1;
  const coords = values.map((value, i) => `${(i / (values.length - 1) * 700).toFixed(2)},${(130 - ((value - low) / spread) * 110).toFixed(2)}`);
  const line = `M${coords.join(' L')}`;
  $('chartLine').setAttribute('d', line);
  $('chartArea').setAttribute('d', `${line} L700,150 L0,150 Z`);
}

function hydrateConfig(config) {
  if (configDirty && hasHydrated) return;
  Object.entries(config).forEach(([name, value]) => {
    const input = configForm.elements.namedItem(name);
    if (input && value !== null && value !== undefined) input.value = String(value);
  });
  hasHydrated = true;
  updateCostEstimate();
  text('settingsState', 'Kayıtlı ayarlar');
}

function updateCostEstimate() {
  const read = (name) => numeric(configForm.elements.namedItem(name).value) || 0;
  text('roundTripCost', percent(read('buy_fee_pct') + read('sell_fee_pct') + read('slippage_pct') * 2));
  const isProfit = configForm.elements.namedItem('trailing_mode').value === 'net_profit';
  for (const name of ['trailing_distance_pct', 'profit_giveback_pct']) {
    const el = configForm.elements.namedItem(name);
    el.parentElement.style.opacity = name === 'trailing_distance_pct' ? (isProfit ? '.55' : '1') : (isProfit ? '1' : '.55');
  }
}

function render(current) {
  state = current;
  const config = current.config || {};
  const account = current.account || {};
  const market = current.market || {};
  const status = current.status || {};
  const risk = account.risk || current.risk || {};
  const position = current.position;
  const trades = Array.isArray(current.trades) ? current.trades : [];
  const isDemo = Boolean(market.is_synthetic) || config.data_source === 'demo';
  hydrateConfig(config);

  text('equity', money(account.equity));
  text('initialCash', `Başlangıç: ${money(account.initial_cash)} USDT`);
  paintValue('totalPnl', account.total_pnl, { signed: true, color: true });
  text('totalPnlPct', percent(account.total_pnl_pct, true));
  $('totalPnlPct').className = numeric(account.total_pnl) < 0 ? 'negative' : numeric(account.total_pnl) > 0 ? 'positive' : '';
  paintValue('realizedPnl', account.realized_pnl, { signed: true, color: true });
  text('tradeCount', `${trades.length} kapanan işlem`);
  text('feesPaid', money(account.fees_paid));
  text('feeSummary', `Alış ${percent(config.buy_fee_pct)} · Satış ${percent(config.sell_fee_pct)}`);
  text('cashBalance', `${money(account.cash)} USDT`);
  text('marketTitle', `${String(market.symbol || config.symbol || '—').replace(/USDT$/, ' / USDT')} `);
  const interval = document.createElement('span'); interval.className = 'subtle-tag'; interval.textContent = config.interval || '—'; $('marketTitle').append(interval);
  text('marketPrice', price(market.price));
  text('dataBadge', isDemo ? 'SENTETİK DEMO' : 'GERÇEK PİYASA VERİSİ');
  $('dataBadge').classList.toggle('demo', isDemo);
  $('demoBanner').hidden = !isDemo;
  text('marketSource', `Kaynak: ${isDemo ? 'Sentetik demo' : market.source || 'Binance'}`);
  text('marketTime', `Son fiyat: ${dateTime(market.updated_at)}`);
  text('lastUpdated', `Son fiyat ${dateTime(market.updated_at)}`);
  text('botStatus', status.running ? 'Bot çalışıyor' : 'Bot duraklatıldı');
  $('runningDot').classList.toggle('running', Boolean(status.running));
  const strategy = strategies[config.strategy] || { name: config.strategy || '—', description: '' };
  text('activeStrategy', strategy.name);
  text('strategyDescription', strategy.description);
  text('backtestContext', `${config.symbol || '—'} · ${config.interval || '—'} · ${isDemo ? 'Sentetik demo' : 'Binance piyasa verisi'}`);
  const dailyPnl = numeric(risk.daily_pnl);
  text('dailyRisk', risk.daily_halted ? 'Zarar limiti: yeni alışlar durdu' : dailyPnl === null ? 'Limit etkin' : `${money(dailyPnl, true)} USDT · Limit ${percent(config.daily_loss_limit_pct)}`);
  $('dailyRisk').className = risk.daily_halted ? 'negative' : '';
  const errors = [...new Set([status.error, market.error].filter(Boolean))];
  $('errorBanner').hidden = !errors.length;
  text('errorBanner', errors.join(' · '));

  const updated = market.updated_at ? new Date(typeof market.updated_at === 'number' && market.updated_at < 1e12 ? market.updated_at * 1000 : market.updated_at) : null;
  const stale = updated && Number.isFinite(updated.getTime()) && Date.now() - updated.getTime() > 120000;
  $('connectionStatus').className = `connection ${errors.length || stale ? 'offline' : status.fresh ? 'online' : ''}`;
  $('connectionStatus').replaceChildren(document.createElement('i'), document.createTextNode(errors.length ? 'Veri hatası' : stale ? 'Veri gecikiyor' : status.fresh ? 'Veri akışı bağlı' : 'Veri bekleniyor'));

  $('emptyPosition').hidden = Boolean(position);
  $('positionDetails').hidden = !position;
  text('positionBadge', position ? '1 aktif pozisyon' : 'Yok');
  if (position) {
    text('entryPrice', `${price(position.entry_price)} USDT`);
    text('positionQuantity', formatQuantity.format(numeric(position.quantity) || 0));
    paintValue('unrealizedPnl', account.unrealized_pnl, { signed: true, color: true, suffix: ' USDT' });
    text('fixedStop', `${price(position.fixed_stop_price)} USDT`);
    text('trailingStop', position.trailing_active ? `${price(position.trailing_stop_price)} USDT` : 'Etkinleşmesi bekleniyor');
    text('trailingState', position.trailing_active ? (config.trailing_mode === 'net_profit' ? 'En yüksek net kâr takip ediliyor' : 'En yüksek fiyat takip ediliyor') : `Net getiri ${percent(config.trailing_activation_pct)} olunca etkinleşir`);
    text('highestPrice', `${price(position.highest_price)} USDT`);
  }
  renderTrades(trades);
  renderBacktest(current.backtest);
  updateChart(market);
  updateButtons();
}

function updateButtons() {
  const current = state || {};
  const running = Boolean(current.status?.running);
  $('startButton').disabled = requestBusy || !state || running || configDirty;
  $('startButton').title = configDirty ? 'Başlatmadan önce değişiklikleri kaydedin.' : '';
  $('pauseButton').disabled = requestBusy || !state || !running;
  $('closeButton').disabled = requestBusy || !current.position;
  $('resetButton').disabled = requestBusy || !state || running || Boolean(current.position);
  $('saveButton').disabled = requestBusy || !state || running || Boolean(current.position);
  $('saveButton').title = running || current.position ? 'Ayarlar için botu duraklatın ve açık pozisyonu kapatın.' : '';
  $('backtestButton').disabled = backtestBusy || !state;
  configForm.elements.namedItem('initial_cash').disabled = Boolean(current.position) || (Array.isArray(current.trades) && current.trades.length > 0);
}

function addCell(row, value, className) {
  const cell = document.createElement('td');
  cell.textContent = value;
  if (className) cell.className = className;
  row.append(cell);
  return cell;
}
function renderTrades(trades) {
  const tbody = $('tradesBody');
  tbody.replaceChildren();
  if (!trades.length) {
    const row = document.createElement('tr'); const cell = addCell(row, 'Henüz kapanan işlem yok.', 'empty-cell'); cell.colSpan = 7; tbody.append(row); return;
  }
  [...trades].reverse().slice(0, 100).forEach((trade) => {
    const row = document.createElement('tr');
    addCell(row, dateTime(trade.closed_at || trade.exit_at || trade.timestamp || trade.time));
    addCell(row, trade.symbol || state.config?.symbol || '—');
    addCell(row, `${price(trade.entry_price)} / ${price(trade.exit_price)}`);
    addCell(row, price(trade.quantity ?? trade.qty));
    addCell(row, money(trade.total_fees ?? trade.fees_paid ?? trade.fees ?? ((numeric(trade.entry_fee) || 0) + (numeric(trade.exit_fee) || 0))));
    const net = trade.net_pnl ?? trade.pnl_quote ?? trade.pnl;
    const pnl = addCell(row, money(net, true), numeric(net) > 0 ? 'positive' : numeric(net) < 0 ? 'negative' : '');
    const returnPct = trade.net_pnl_pct ?? trade.return_pct ?? trade.pnl_pct;
    if (returnPct !== null && returnPct !== undefined) { const detail = document.createElement('small'); detail.textContent = percent(returnPct, true); pnl.append(detail); }
    const reasonCell = document.createElement('td'); const reason = document.createElement('span'); reason.className = 'trade-reason'; const reasonValue = trade.exit_reason || trade.reason; reason.textContent = reasonNames[reasonValue] || reasonValue || '—'; reasonCell.append(reason); row.append(reasonCell);
    tbody.append(row);
  });
}

function renderBacktest(report) {
  const results = report?.results;
  const available = Array.isArray(results) && results.length > 0;
  $('backtestResults').hidden = !available;
  $('backtestEmpty').hidden = available || backtestBusy;
  if (!available) return;
  const tbody = $('backtestBody'); tbody.replaceChildren();
  results.forEach((result) => {
    const row = document.createElement('tr');
    addCell(row, strategies[result.strategy]?.name || result.strategy || '—');
    addCell(row, money(result.net_pnl, true), numeric(result.net_pnl) > 0 ? 'positive' : numeric(result.net_pnl) < 0 ? 'negative' : '');
    addCell(row, percent(result.net_return_pct, true));
    addCell(row, percent(result.max_drawdown_pct));
    addCell(row, `${result.trade_count ?? '—'} / ${percent(result.win_rate_pct)}`);
    addCell(row, money(result.fees_paid));
    addCell(row, percent(result.hold_return_pct, true));
    tbody.append(row);
  });
  const notes = Array.isArray(report.notes) ? report.notes.join('\n') : report.notes || '';
  const period = results[0]?.test_start ? `Değerlendirme dönemi: ${dateTime(results[0].test_start)} – ${dateTime(results[0].test_end)} (İstanbul).` : '';
  text('backtestNotes', `${report.train_candles ?? '—'} gözlem/ısınma mumu · ${report.test_candles ?? '—'} değerlendirme mumu. ${period}\n${notes}`.trim());
}

async function fetchState() {
  if (fetching) return;
  fetching = true;
  try { render(await api('/api/state')); }
  catch (error) {
    $('connectionStatus').className = 'connection offline';
    $('connectionStatus').replaceChildren(document.createElement('i'), document.createTextNode('Bağlantı yok'));
    $('errorBanner').hidden = false;
    text('errorBanner', `Sunucuya bağlanılamadı: ${error.message} Gösterilen değerler güncel olmayabilir.`);
  } finally { fetching = false; }
}

async function control(action) {
  if (requestBusy) return;
  requestBusy = true; updateButtons();
  try {
    await api('/api/control', { method: 'POST', body: JSON.stringify({ action }) });
    if (action === 'reset') { configDirty = false; hasHydrated = false; chartPoints = []; }
    await fetchState();
    toast({ start: 'Bot başlatıldı. Yalnızca deneme işlemleri yapılır.', pause: 'Yeni strateji işlemleri duraklatıldı. Açık pozisyonun stop takibi sürer.', close_position: 'Deneme pozisyonu kapatıldı.', reset: 'Deneme defteri sıfırlandı.' }[action]);
  } catch (error) { toast(error.message, true); }
  finally { requestBusy = false; updateButtons(); }
}

configForm.addEventListener('input', () => { configDirty = true; text('settingsState', 'Kaydedilmemiş değişiklikler'); updateCostEstimate(); updateButtons(); });
configForm.addEventListener('change', () => { configDirty = true; text('settingsState', 'Kaydedilmemiş değişiklikler'); updateCostEstimate(); updateButtons(); });
configForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (requestBusy || !configForm.reportValidity()) return;
  const payload = {};
  for (const [name, value] of new FormData(configForm)) payload[name] = ['symbol', 'interval', 'strategy', 'data_source', 'trailing_mode'].includes(name) ? String(value) : Number(value);
  payload.symbol = payload.symbol.trim().toUpperCase();
  requestBusy = true; updateButtons(); text('settingsState', 'Kaydediliyor…');
  try {
    await api('/api/config', { method: 'PATCH', body: JSON.stringify(payload) });
    configDirty = false; hasHydrated = false;
    await fetchState();
    toast('Ayarlar kaydedildi.');
  } catch (error) { toast(error.message, true); text('settingsState', 'Kaydedilemedi · değişiklikler korunuyor'); }
  finally { requestBusy = false; updateButtons(); }
});
$('startButton').addEventListener('click', () => control('start'));
$('pauseButton').addEventListener('click', () => control('pause'));
$('closeButton').addEventListener('click', () => control('close_position'));
$('resetButton').addEventListener('click', () => { $('resetDialog').returnValue = ''; $('resetDialog').showModal(); });
$('resetDialog').addEventListener('cancel', () => { $('resetDialog').returnValue = 'cancel'; });
$('resetDialog').addEventListener('close', () => { if ($('resetDialog').returnValue === 'confirm') control('reset'); });
$('backtestForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (backtestBusy || !state) return;
  backtestBusy = true; updateButtons();
  text('backtestButton', 'Geçmiş test çalışıyor…'); $('backtestEmpty').hidden = true;
  try {
    const result = await api('/api/backtest', { method: 'POST', body: JSON.stringify({ symbol: state.config.symbol, interval: state.config.interval, limit: 500, strategy: $('backtestStrategy').value }) });
    if (result.results) renderBacktest(result);
    else if (result.backtest) renderBacktest(result.backtest);
    await fetchState();
    toast('Geçmiş test tamamlandı. Sonuçlar komisyon ve kayma içerir.');
  } catch (error) { toast(error.message, true); $('backtestEmpty').hidden = Boolean(state.backtest?.results?.length); }
  finally { backtestBusy = false; text('backtestButton', 'Geçmiş testi çalıştır'); updateButtons(); }
});
document.querySelectorAll('nav a').forEach((link) => link.addEventListener('click', () => { document.querySelectorAll('nav a').forEach((item) => item.classList.remove('active')); link.classList.add('active'); }));
updateButtons();
fetchState();
setInterval(fetchState, 3000);
