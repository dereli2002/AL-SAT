'use strict';

const $ = (id) => document.getElementById(id);
const configForm = $('configForm');
const strategies = {
  hybrid: { name: 'Karma strateji', description: 'EMA, RSI ve kırılım oylarını birleştirir. En az iki uyumlu oy ve karşı oy yoksa işlem yapar.' },
  ema_cross: { name: 'EMA kesişimi', description: 'Kısa ve uzun dönem ortalamalarının kesişimini izler.' },
  rsi_reversion: { name: 'RSI dönüşü', description: 'RSI 14 ≤ 30 olduğunda alış, ≥ 60 olduğunda satış arar. Düşük RSI tek başına yükseliş onayı değildir.' },
  breakout: { name: 'Fiyat kırılımı', description: 'Önceki fiyat aralığının üzerindeki kırılımı izler.' },
  trend_breakout: { name: 'ADX filtreli trend kırılımı', description: 'EMA 20/50 yükselişi, ADX ≥ 25 ve hacim onaylı kırılım arar. ATR filtresi aşırı uzaklaşmış fiyatları eler; uygun koşul yoksa bekler.' },
};
const reasonNames = { stop_loss: 'Alış altı stop', fixed_stop: 'Alış altı stop', trailing_stop: 'Kâr takibi stop', trailing_price: 'Fiyat takibi stop', trailing_profit: 'Net kâr takibi', strategy: 'Strateji sinyali', signal: 'Strateji sinyali', sell_signal: 'Satış sinyali', manual: 'Manuel', manual_buy: 'Manuel alış', manual_sell: 'Manuel satış', manual_close: 'Manuel kapatma', emergency: 'Acil kapatma', emergency_close: 'Acil kapatma', daily_loss: 'Günlük risk limiti', daily_loss_limit: 'Günlük risk limiti', end_of_test: 'Test sonu', backtest_end: 'Test sonu' };
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
let catalogue = [];
let selectedSymbols = new Set();
let symbolsLoading = false;
let catalogueRequestedFor = '';
let symbolListKey = '';
let marketSummaryKey = '';
let lastStateSuccessAt = null;
let networkHealthy = false;
let manualSymbolTouched = false;
let manualAmountTouched = false;
let focusDirty = false;
let emergencyBusy = false;
let fillsInitialized = false;
let seenEntries = new Set();
let seenExits = new Set();
let previousClosedCount = 0;
let fillNotificationVersion = 0;
let emergencyVersion = 0;
let chartFocusSymbol = '';
let chartFocusSource = '';
let focusSaving = false;
let focusToSave = null;
let chartPickerKey = '';
let chartPickerIndex = 0;
let catalogueError = '';
const chartHistories = new Map();
const chartSessions = new Map();
const chartRequests = new Set();

function focusedSymbol(current = state) { return chartFocusSymbol || current?.config?.symbol || ''; }

function instant(value) {
  if (!value) return null;
  const parsed = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
  return Number.isNaN(parsed.getTime()) ? null : parsed.getTime();
}

function entryKey(item) {
  return `${item.symbol || state?.config?.symbol}|${item.trade_id ?? item.id ?? ''}|${item.opened_at || ''}`;
}

function observeFills(current) {
  const positions = positionsOf(current);
  const trades = Array.isArray(current.trades) ? current.trades : [];
  if (trades.length < previousClosedCount) { fillsInitialized = false; seenEntries = new Set(); seenExits = new Set(); }
  const messages = [];
  const addEntry = (item) => {
    const key = entryKey(item);
    if (!seenEntries.has(key) && fillsInitialized) messages.push(`AL ${item.symbol || current.config?.symbol} · ${price(item.entry_price)} USDT`);
    seenEntries.add(key);
  };
  positions.forEach(addEntry);
  trades.forEach((trade) => {
    const key = `${entryKey(trade)}|${trade.closed_at || trade.exit_at || ''}`;
    if (!seenExits.has(key)) {
      // Also report an entry that opened and closed between two polling reads.
      addEntry(trade);
      if (fillsInitialized) messages.push(`SAT ${trade.symbol || current.config?.symbol} · net ${money(trade.pnl_quote ?? trade.net_pnl ?? trade.pnl, true)} USDT · ${reasonNames[trade.reason] || trade.reason || 'Kapatıldı'}`);
    }
    seenExits.add(key);
  });
  fillsInitialized = true;
  previousClosedCount = trades.length;
  if (messages.length) {
    const summary = `Yeni deneme işlemi: ${messages.slice(0, 3).join(' | ')}${messages.length > 3 ? ` | +${messages.length - 3} işlem` : ''}`;
    text('latestExecution', `${dateTime(Date.now())} · ${summary}`);
    $('latestExecution').hidden = false;
    fillNotificationVersion += 1;
    toast(summary);
  }
}

function manualContext() {
  const symbol = $('manualSymbol').value.trim().toUpperCase();
  const market = marketsOf().find((entry) => entry.symbol === symbol) || (state?.market?.symbol === symbol ? state.market : {});
  const position = positionsOf().find((entry) => entry.symbol === symbol);
  const stamp = instant(market.updated_at);
  const fresh = Boolean(networkHealthy && state?.status?.fresh && numeric(market.price) > 0 && stamp && Date.now() - stamp < 30000 && lastStateSuccessAt && Date.now() - lastStateSuccessAt < 30000);
  const known = catalogue.length > 0 && catalogue.some((entry) => entry.symbol === symbol);
  return { symbol, market, position, fresh, known };
}

function updateManualControls() {
  const context = manualContext();
  const cash = numeric(state?.account?.cash);
  const amount = numeric($('manualAmount').value);
  const risk = state?.account?.risk || {};
  const emergency = Boolean(state?.emergency?.active);
  const maxPositions = numeric(state?.config?.max_open_positions) || 1;
  const buyIssue = emergency ? 'Acil kilit etkin: yeni alışlar engellendi.' : !context.symbol ? 'İşlem yapmak için parite seçin.' : !context.known ? 'Katalogdan geçerli bir USDT paritesi seçin.' : !context.fresh ? 'Seçili parite için güncel ve başarılı fiyat akışı bekleniyor.' : context.position ? 'Bu paritede açık pozisyon var. Önce tamamını satabilirsiniz.' : risk.daily_halted ? 'Günlük zarar sınırı nedeniyle yeni alışlar kilitli.' : positionsOf().length >= maxPositions ? 'Açık pozisyon sınırına ulaşıldı.' : amount === null || amount <= 0 ? 'Pozitif bir USDT alış tutarı girin.' : cash === null || amount > cash ? 'Alış tutarı kullanılabilir USDT bakiyesini aşıyor.' : '';
  $('manualBuyButton').disabled = requestBusy || emergencyBusy || Boolean(buyIssue);
  $('manualBuyButton').title = buyIssue;
  $('manualSellButton').disabled = requestBusy || emergencyBusy || !context.position || !context.fresh;
  $('manualSellButton').title = !context.position ? 'Bu paritede açık pozisyon yok.' : !context.fresh ? 'Satış için güncel fiyat gerekli.' : 'Seçili pozisyonun tamamını sat.';
  if (cash !== null) $('manualAmount').max = String(cash); else $('manualAmount').removeAttribute('max');
  text('manualQuote', numeric(context.market.price) > 0 ? `Son fiyat: ${price(context.market.price)} USDT · ${context.fresh ? 'güncel' : 'güncel veri bekleniyor'}${context.market.error ? ' · strateji taramasında hata var' : ''}` : 'Güncel fiyat bekleniyor');
  text('manualCash', `Kullanılabilir bakiye: ${money(cash)} USDT`);
  text('manualAvailability', buyIssue || 'AL ve SAT kontrolü sizde. Emirler bu deneme hesabında uygulanır.');
  text('manualPositionInfo', context.position ? `SAT: ${formatQuantity.format(numeric(context.position.quantity) || 0)} ${context.symbol.replace(/USDT$/, '')} miktarının tamamını kapatır.` : 'SAT, seçili paritenin açık miktarının tamamını kapatır.');
}

function renderEmergency(current) {
  const emergency = current.emergency || {};
  const pending = Array.isArray(emergency.pending_symbols) ? emergency.pending_symbols : [];
  const open = positionsOf(current).length;
  $('emergencyBanner').hidden = !emergency.active;
  text('emergencyDetails', pending.length ? `${pending.length} pozisyon kapatma bekliyor: ${pending.join(', ')}. Güncel fiyat akışı geldiğinde kapatma yeniden denenir. Bot durduruldu; yeni alışlar kilitli.` : open ? `${open} açık pozisyon var. Kapatma durumu izleniyor; yeni alışlar kilitli.` : 'Tüm pozisyonlar kapalı. Yeni alışlar kilitli; otomasyon kendiliğinden yeniden başlamaz.');
  $('emergencyError').hidden = !emergency.last_error;
  text('emergencyError', emergency.last_error);
}

function positionsOf(current = state) {
  if (Array.isArray(current?.positions)) return current.positions;
  return current?.position ? [{ symbol: current.config?.symbol, ...current.position, unrealized_pnl: current.account?.unrealized_pnl }] : [];
}

function marketsOf(current = state) {
  if (Array.isArray(current?.markets)) return current.markets;
  return current?.market?.symbol ? [current.market] : [];
}

function focusedMarket(current) {
  const symbol = focusedSymbol(current);
  const found = marketsOf(current).find((market) => market.symbol === symbol);
  const legacy = current.market?.symbol === symbol ? current.market : {};
  return { ...legacy, ...found, symbol, source: found?.source || legacy.source || current.config?.data_source, is_synthetic: current.config?.data_source === 'demo' };
}

function markConfigDirty() {
  configDirty = true;
  text('settingsState', 'Kaydedilmemiş değişiklikler');
  updateCostEstimate();
  updateButtons();
}

async function loadSymbols(force = false) {
  const source = state?.config?.data_source || 'binance';
  if (symbolsLoading || (!force && catalogueRequestedFor === source)) return;
  catalogueRequestedFor = source;
  symbolsLoading = true;
  catalogue = [];
  catalogueError = '';
  $('symbolOptions').replaceChildren();
  symbolListKey = '';
  text('catalogueStatus', 'İşlem yapılabilir USDT pariteleri yükleniyor…');
  $('refreshSymbolsButton').disabled = true;
  renderUniversePicker();
  renderChartPicker();
  try {
    const data = await api('/api/symbols');
    if (source !== state?.config?.data_source) { catalogueRequestedFor = ''; return; }
    catalogue = (Array.isArray(data.symbols) ? data.symbols : []).map((entry) => typeof entry === 'string' ? { symbol: entry, base_asset: entry.replace(/USDT$/, ''), quote_asset: 'USDT' } : entry).filter((entry) => entry.symbol).sort((a, b) => a.symbol.localeCompare(b.symbol));
    catalogueError = data.error || '';
    const options = document.createDocumentFragment();
    catalogue.forEach((entry) => { const option = document.createElement('option'); option.value = entry.symbol; option.label = `${entry.base_asset || entry.symbol.replace(/USDT$/, '')} / USDT`; options.append(option); });
    $('symbolOptions').replaceChildren(options);
    text('catalogueStatus', data.error || (data.is_synthetic ? 'Sentetik demo listesi: yalnız örnek pariteler. Binance’in tüm piyasa listesi değildir.' : `${catalogue.length} işlem yapılabilir Binance Spot USDT paritesi. Liste borsadan alınır.`));
  } catch (error) {
    catalogueError = error.message;
    text('catalogueStatus', `Parite listesi alınamadı: ${error.message} Listeyi yenileyerek tekrar deneyin.`);
  } finally {
    symbolsLoading = false;
    $('refreshSymbolsButton').disabled = false;
    symbolListKey = '';
    renderUniversePicker();
    renderChartPicker();
    updateManualControls();
  }
}

function renderUniversePicker() {
  const mode = configForm.elements.namedItem('universe_mode').value;
  const all = mode === 'all';
  const query = $('symbolSearch').value.trim().toUpperCase();
  const key = `${all}|${query}|${[...selectedSymbols].sort().join(',')}|${catalogue.map((entry) => entry.symbol).join(',')}|${symbolsLoading}`;
  text('catalogueCount', catalogue.length ? `${catalogue.length} parite` : symbolsLoading ? 'Yükleniyor' : 'Liste yok');
  text('universeHelp', all ? 'Listelenen tüm işlem yapılabilir USDT pariteleri gruplar hâlinde taranır.' : 'İzlenecek pariteleri işaretleyin. En az bir parite seçilmelidir.');
  text('selectionCount', all ? `${catalogue.length || '—'} paritenin tamamı izlenecek` : `${selectedSymbols.size} parite seçildi`);
  $('clearSelectionButton').disabled = all || selectedSymbols.size === 0;
  if (key === symbolListKey) return;
  symbolListKey = key;
  const visible = catalogue.filter((entry) => `${entry.symbol} ${entry.base_asset || ''}`.toUpperCase().includes(query));
  const fragment = document.createDocumentFragment();
  visible.forEach((entry) => {
    const label = document.createElement('label'); label.className = 'asset-option';
    const input = document.createElement('input'); input.type = 'checkbox'; input.checked = all || selectedSymbols.has(entry.symbol); input.disabled = all; input.setAttribute('aria-label', `${entry.symbol} paritesini izle`);
    input.addEventListener('change', (event) => {
      event.stopPropagation();
      if (input.checked) selectedSymbols.add(entry.symbol); else selectedSymbols.delete(entry.symbol);
      markConfigDirty();
      // Preserve keyboard focus while updating selection counts and chips.
      renderUniversePicker();
      [...$('symbolList').querySelectorAll('input')].find((item) => item.getAttribute('aria-label') === input.getAttribute('aria-label'))?.focus({ preventScroll: true });
    });
    const name = document.createElement('span'); name.textContent = entry.base_asset || entry.symbol.replace(/USDT$/, '');
    const pair = document.createElement('small'); pair.textContent = entry.symbol;
    label.append(input, name, pair); fragment.append(label);
  });
  if (!visible.length) { const empty = document.createElement('p'); empty.className = 'symbol-list-empty'; empty.textContent = symbolsLoading ? 'Parite listesi yükleniyor…' : query ? 'Aramanızla eşleşen parite yok.' : 'Parite listesi henüz alınamadı.'; fragment.append(empty); }
  $('symbolList').replaceChildren(fragment);
  const chips = document.createDocumentFragment();
  if (!all) [...selectedSymbols].sort().forEach((symbol) => { const chip = document.createElement('button'); chip.type = 'button'; chip.className = 'asset-chip'; chip.textContent = `${symbol} ×`; chip.setAttribute('aria-label', `${symbol} seçimini kaldır`); chip.addEventListener('click', () => { selectedSymbols.delete(symbol); markConfigDirty(); renderUniversePicker(); }); chips.append(chip); });
  $('selectedChips').replaceChildren(chips);
}

function chartPickerOptions() { return [...$('chartAssetList').querySelectorAll('[role="option"]')]; }

function highlightChartOption(index, scroll = false) {
  const options = chartPickerOptions();
  chartPickerIndex = Math.max(0, Math.min(index, options.length - 1));
  options.forEach((option, i) => option.classList.toggle('is-highlighted', i === chartPickerIndex));
  const active = options[chartPickerIndex];
  if (active) {
    $('chartAssetSearch').setAttribute('aria-activedescendant', active.id);
    if (scroll) active.scrollIntoView({ block: 'nearest' });
  } else $('chartAssetSearch').removeAttribute('aria-activedescendant');
}

function renderChartPicker() {
  const query = $('chartAssetSearch').value.trim().toUpperCase();
  const visible = catalogue.filter((entry) => `${entry.symbol} ${entry.base_asset || ''}`.toUpperCase().includes(query));
  const symbol = focusedSymbol();
  const key = `${query}|${symbol}|${symbolsLoading}|${catalogueError}|${catalogue.map((entry) => entry.symbol).join(',')}`;
  text('chartAssetCount', symbolsLoading ? 'Liste yükleniyor…' : `${visible.length} / ${catalogue.length} varlık`);
  text('chartAssetStatus', catalogueError ? `Liste alınırken hata: ${catalogueError}` : state?.config?.data_source === 'demo' ? 'Sentetik demo: yalnızca örnek varlıklar.' : 'Binance Spot · tüm aktif USDT pariteleri');
  $('chartPickerRefresh').disabled = symbolsLoading;
  if (chartPickerKey === key) return;
  chartPickerKey = key;
  const fragment = document.createDocumentFragment();
  visible.forEach((entry, index) => {
    const option = document.createElement('button');
    option.type = 'button'; option.className = 'chart-asset-option'; option.id = `chart-asset-${index}`;
    option.setAttribute('role', 'option'); option.setAttribute('aria-selected', String(entry.symbol === symbol)); option.tabIndex = -1;
    option.dataset.symbol = entry.symbol;
    const base = document.createElement('strong'); base.textContent = entry.base_asset || entry.symbol.replace(/USDT$/, '');
    const pair = document.createElement('span'); pair.textContent = entry.symbol;
    const check = document.createElement('span'); check.className = 'chart-asset-check'; check.textContent = entry.symbol === symbol ? '✓' : '';
    option.append(base, pair, check);
    option.addEventListener('mousedown', (event) => event.preventDefault());
    option.addEventListener('click', () => { closeChartPicker(); selectChartSymbol(entry.symbol); });
    fragment.append(option);
  });
  if (!visible.length) {
    const empty = document.createElement('p'); empty.className = 'chart-picker-empty';
    empty.textContent = symbolsLoading ? 'Varlık listesi yükleniyor…' : query ? 'Eşleşen varlık bulunamadı.' : 'Varlık listesi alınamadı. Yenile düğmesiyle tekrar deneyin.';
    fragment.append(empty);
  }
  $('chartAssetList').replaceChildren(fragment);
  highlightChartOption(query ? 0 : Math.max(0, visible.findIndex((entry) => entry.symbol === symbol)));
}

function openChartPicker() {
  $('chartAssetSearch').value = '';
  chartPickerKey = '';
  $('chartAssetPicker').hidden = false;
  $('chartPickerButton').setAttribute('aria-expanded', 'true');
  $('chartAssetSearch').setAttribute('aria-expanded', 'true');
  renderChartPicker();
  $('chartAssetList').scrollTop = 0;
  $('chartAssetSearch').focus({ preventScroll: true });
  if (!catalogue.length && !symbolsLoading) loadSymbols(true);
}

function closeChartPicker(restoreFocus = true) {
  $('chartAssetPicker').hidden = true;
  $('chartPickerButton').setAttribute('aria-expanded', 'false');
  $('chartAssetSearch').setAttribute('aria-expanded', 'false');
  if (restoreFocus) $('chartPickerButton').focus({ preventScroll: true });
}

function selectChartSymbol(symbol, scroll = false) {
  if (!state) return;
  chartFocusSymbol = symbol;
  focusDirty = false;
  $('focusSymbol').value = symbol;
  $('focusSymbol').setCustomValidity('');
  $('manualSymbol').value = symbol;
  manualSymbolTouched = true;
  render(state, true);
  if (scroll) {
    $('marketChart').scrollIntoView({ behavior: 'instant', block: 'start' });
    $('marketChart').focus({ preventScroll: true });
  }
  focusToSave = symbol;
  saveChartFocus();
}

async function saveChartFocus() {
  if (focusSaving) return;
  focusSaving = true;
  try {
    while (focusToSave) {
      const symbol = focusToSave;
      focusToSave = null;
      try {
        // Focus writes are queued; a slower response cannot replace a newer local selection.
        await api('/api/config', { method: 'PATCH', body: JSON.stringify({ symbol }) });
      } catch (error) {
        if (focusedSymbol() === symbol) toast(`Grafik seçildi; varsayılan parite kaydedilemedi: ${error.message}`, true);
      }
    }
    await fetchState();
  } finally { focusSaving = false; if (focusToSave) saveChartFocus(); }
}

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
function text(id, value) { const element = $(id); const content = String(value ?? '—'); if (element.textContent !== content) element.textContent = content; }
function paintValue(id, value, options = {}) {
  const el = $(id);
  el.textContent = `${money(value, options.signed)}${options.suffix || ''}`;
  if (options.color) {
    el.classList.remove('positive', 'negative', 'neutral');
    const n = numeric(value);
    el.classList.add(n > 0 ? 'positive' : n < 0 ? 'negative' : 'neutral');
  }
}

function renderLivePnl(current = state) {
  const positions = positionsOf(current);
  const account = current?.account || {};
  const missing = positions.filter((position) => position.quote_missing).map((position) => position.symbol);
  const stamps = marketsOf(current).filter((market) => positions.some((position) => position.symbol === market.symbol)).map((market) => instant(market.updated_at));
  const quotesOld = stamps.length !== positions.length || stamps.some((stamp) => !stamp || Date.now() - stamp >= 30000);
  const connected = Boolean(current && networkHealthy && lastStateSuccessAt && Date.now() - lastStateSuccessAt < 30000);
  const fresh = connected && (positions.length === 0 || (numeric(account.unrealized_pnl) !== null && current?.status?.fresh && !account.risk?.valuation_stale && !missing.length && !quotesOld));
  const pnl = fresh ? positions.length ? account.unrealized_pnl : 0 : null;
  const cost = numeric(account.open_entry_cost) ?? positions.reduce((sum, position) => sum + (numeric(position.entry_cost) || 0), 0);
  const pnlPct = fresh ? positions.length ? numeric(account.unrealized_pnl_pct) ?? (cost > 0 ? numeric(pnl) / cost * 100 : null) : 0 : null;
  paintValue('livePnl', pnl, { signed: true, color: true });
  text('livePnlPct', percent(pnlPct, true));
  $('livePnlPct').className = pnlPct > 0 ? 'positive' : pnlPct < 0 ? 'negative' : '';
  text('livePositionCount', current ? `${positions.length} açık pozisyon` : 'Pozisyonlar bekleniyor');
  const stamp = stamps.length && stamps.every(Boolean) ? Math.min(...stamps) : null;
  text('livePnlStatus', !current ? 'Hesap verisi bekleniyor' : !connected ? 'Bağlantı yok · güncel veri bekleniyor' : !positions.length ? 'Açık işlem yok' : !fresh ? missing.length ? `Güncel fiyat eksik: ${missing.join(', ')}` : 'Güncel fiyat bekleniyor' : `Son fiyat: ${dateTime(stamp)}`);
  $('livePnlStatus').classList.toggle('negative', !fresh);
  $('livePnlCard').classList.toggle('is-stale', !fresh);
  $('livePnlCard').title = 'Yalnız açık pozisyonların alış maliyeti ve tahmini satış komisyonu/kayması sonrası net sonucu. Yüzde, açık pozisyonların toplam alış maliyetine göredir.';
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

function chartIdentity(market) { return `${market.symbol}|${state?.config?.data_source}|${state?.config?.interval}`; }

function rememberChart(map, key, value) {
  map.delete(key); map.set(key, value);
  if (map.size > 64) map.delete(map.keys().next().value);
}

async function loadChartHistory(market, key) {
  if (chartRequests.has(key)) return;
  const previous = chartHistories.get(key);
  if (previous && Date.now() - previous.requestedAt < 30000) return;
  const requestedAt = Date.now();
  const source = state.config.data_source;
  const interval = state.config.interval;
  chartRequests.add(key);
  try {
    const data = await api(`/api/chart?symbol=${encodeURIComponent(market.symbol)}&interval=${encodeURIComponent(interval)}`);
    if (data.symbol !== market.symbol || data.interval !== interval || Boolean(data.is_synthetic) !== (source === 'demo')) throw new Error('Grafik verisinin paritesi veya kaynağı uyuşmuyor.');
    rememberChart(chartHistories, key, { ...data, requestedAt });
  } catch (error) {
    rememberChart(chartHistories, key, { ...previous, requestedAt, history_stale: true, error: error.message });
  } finally {
    chartRequests.delete(key);
    if (state && chartIdentity(focusedMarket(state)) === key) updateChart(focusedMarket(state));
  }
}

function updateChart(market) {
  const key = chartIdentity(market);
  if (chartKey !== key) { chartKey = key; chartPoints = chartSessions.get(key) || []; }
  const history = chartHistories.get(key);
  const now = Date.now();
  const quoteTime = instant(market.updated_at);
  const n = numeric(market.price);
  const fresh = Boolean(n > 0 && networkHealthy && state?.status?.fresh && quoteTime && now - quoteTime < 30000 && lastStateSuccessAt && now - lastStateSuccessAt < 30000);
  if (fresh && n > 0) {
    const last = chartPoints[chartPoints.length - 1];
    if (!last || last.time !== quoteTime) chartPoints.push({ price: n, time: quoteTime });
    if (chartPoints.length > 80) chartPoints.shift();
    rememberChart(chartSessions, key, chartPoints);
  }
  const candles = (history?.candles || []).map((bar) => ({ price: numeric(bar.close), time: instant(bar.close_time) })).filter((point) => point.price > 0 && point.time);
  const latestCandle = candles[candles.length - 1]?.time || 0;
  const samples = [...candles, ...chartPoints.filter((point) => point.time > latestCandle)].sort((a, b) => a.time - b.time);
  const ready = samples.length > 1;
  $('chartPlaceholder').hidden = ready;
  text('chartPlaceholder', history?.error ? `Fiyat geçmişi alınamadı: ${history.error}` : 'Seçili varlığın fiyat geçmişi yükleniyor…');
  text('chartHistoryStatus', history?.error || history?.history_stale ? `Geçmiş güncel değil${history.error ? ` · ${history.error}` : ' · yenileme bekleniyor'}` : candles.length ? `${candles.length} kapanmış mum · ${state.config.interval}${fresh ? ' · canlı fiyat' : ' · güncel fiyat bekleniyor'}` : 'Fiyat geçmişi yükleniyor…');
  $('chartHistoryStatus').classList.toggle('negative', Boolean(history?.error || history?.history_stale));
  text('chartLegend', candles.length ? 'Mum kapanışları + fiyat akışı' : 'Bu oturumdaki fiyat akışı');
  const position = positionsOf().find((entry) => entry.symbol === market.symbol);
  const entryPrice = numeric(position?.entry_price);
  $('chartEntryLabel').hidden = !ready || !entryPrice;
  if (!ready) {
    ['chartLine', 'chartArea', 'chartEntryLine'].forEach((id) => $(id).setAttribute('d', ''));
    text('chartHigh', '—'); text('chartStartTime', '—'); text('chartEndTime', '—');
  } else {
    const values = samples.map((point) => point.price);
    const low = Math.min(...values, ...(entryPrice ? [entryPrice] : []));
    const high = Math.max(...values, ...(entryPrice ? [entryPrice] : []));
    const spread = high - low || high * 0.002 || 1;
    const minTime = samples[0].time;
    const duration = samples[samples.length - 1].time - minTime || 1;
    const y = (value) => high === low ? 75 : 130 - ((value - low) / spread) * 110;
    const coords = samples.map((point) => `${((point.time - minTime) / duration * 700).toFixed(2)},${y(point.price).toFixed(2)}`);
    const line = `M${coords.join(' L')}`;
    $('chartLine').setAttribute('d', line);
    $('chartArea').setAttribute('d', `${line} L700,150 L0,150 Z`);
    $('chartEntryLine').setAttribute('d', entryPrice ? `M0 ${y(entryPrice).toFixed(2)}H700` : '');
    text('chartEntryLabel', `Alış ${price(entryPrice)}`);
    $('chartEntryLabel').style.top = `${y(entryPrice || low) / 150 * 100}%`;
    text('chartHigh', `${price(low)} – ${price(high)} USDT`);
    text('chartStartTime', dateTime(samples[0].time)); text('chartEndTime', dateTime(samples[samples.length - 1].time));
  }
  loadChartHistory(market, key);
}

function renderChartPosition(market, positions) {
  const position = positions.find((entry) => entry.symbol === market.symbol);
  $('chartPosition').hidden = !position;
  if (!position) return;
  text('chartPositionEntry', `${price(position.entry_price)} USDT`);
  paintValue('chartPositionPnl', position.quote_missing ? null : position.unrealized_pnl ?? position.net_pnl, { signed: true, color: true, suffix: ' USDT' });
  text('chartPositionStop', numeric(position.fixed_stop_price) === null ? 'Kapalı' : `${price(position.fixed_stop_price)} USDT`);
  text('chartPositionQuoteStatus', position.quote_missing ? 'Güncel kotasyon eksik; net kâr / zarar hesaplanamıyor.' : '');
  $('chartPositionQuoteStatus').hidden = !position.quote_missing;
  text('chartClosePosition', `${market.symbol} pozisyonunu kapat`);
}

function hydrateConfig(config) {
  if (configDirty && hasHydrated) return;
  Object.entries(config).forEach(([name, value]) => {
    if (name === 'symbols') return;
    const input = configForm.elements.namedItem(name);
    if (input && value !== null && value !== undefined) input.value = String(value);
  });
  if (!config.universe_mode) configForm.elements.namedItem('universe_mode').value = 'selected';
  selectedSymbols = new Set(Array.isArray(config.symbols) ? config.symbols : [config.symbol || 'BTCUSDT']);
  hasHydrated = true;
  updateCostEstimate();
  renderUniversePicker();
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

function render(current, local = false) {
  state = current;
  if (!local) { networkHealthy = true; lastStateSuccessAt = Date.now(); }
  if (chartFocusSource && chartFocusSource !== current.config?.data_source) chartFocusSymbol = '';
  chartFocusSource = current.config?.data_source;
  if (!local) {
    text('lastRefreshTime', `Son başarılı güncelleme: ${dateTime(lastStateSuccessAt)}`);
    text('refreshStatus', 'Panel güncel · otomatik yenileme açık');
    $('refreshStatus').className = '';
  }
  const config = current.config || {};
  const account = current.account || {};
  const market = focusedMarket(current);
  const status = current.status || {};
  const risk = account.risk || current.risk || {};
  const positions = positionsOf(current);
  const trades = Array.isArray(current.trades) ? current.trades : [];
  const isDemo = Boolean(market.is_synthetic) || config.data_source === 'demo';
  hydrateConfig(config);
  if (!focusDirty && document.activeElement !== $('focusSymbol')) $('focusSymbol').value = focusedSymbol(current);
  if (!manualSymbolTouched && document.activeElement !== $('manualSymbol')) $('manualSymbol').value = config.symbol || '';
  if (!manualAmountTouched && document.activeElement !== $('manualAmount')) $('manualAmount').value = config.order_quote_amount || '';
  if (catalogueRequestedFor !== (config.data_source || 'binance') && !symbolsLoading) loadSymbols();

  text('equity', money(account.equity));
  text('initialCash', `Başlangıç: ${money(account.initial_cash)} USDT`);
  paintValue('totalPnl', account.total_pnl, { signed: true, color: true });
  renderLivePnl(current);
  text('totalPnlPct', percent(account.total_pnl_pct, true));
  $('totalPnlPct').className = numeric(account.total_pnl) < 0 ? 'negative' : numeric(account.total_pnl) > 0 ? 'positive' : '';
  paintValue('realizedPnl', account.realized_pnl, { signed: true, color: true });
  text('tradeCount', `${account.trade_count ?? trades.length} kapanan işlem · tüm pariteler`);
  text('feesPaid', money(account.fees_paid));
  text('feeSummary', `Alış ${percent(config.buy_fee_pct)} · Satış ${percent(config.sell_fee_pct)}`);
  text('cashBalance', `${money(account.cash)} USDT`);
  text('marketTitle', String(market.symbol || '—').replace(/USDT$/, ' / USDT'));
  text('chartInterval', config.interval || '—');
  $('chartPickerButton').setAttribute('aria-label', `${market.symbol || 'Varlık'} grafiği, tüm varlıkları göster`);
  renderChartPicker();
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
  text('backtestContext', `${market.symbol || '—'} · ${config.interval || '—'} · ${isDemo ? 'Sentetik demo' : 'Binance'} · tek parite`);
  const dailyPnl = numeric(risk.daily_pnl);
  text('dailyRisk', risk.daily_halted ? 'Zarar limiti: yeni alışlar durdu' : dailyPnl === null ? 'Limit etkin' : `${money(dailyPnl, true)} USDT · Limit ${percent(config.daily_loss_limit_pct)}`);
  $('dailyRisk').className = risk.daily_halted ? 'negative' : '';
  const errors = [...new Set([status.error, market.error, !networkHealthy ? 'Sunucu bağlantısı yok. Gösterilen değerler güncel olmayabilir.' : null].filter(Boolean))];
  $('errorBanner').hidden = !errors.length;
  text('errorBanner', errors.join(' · '));

  const updated = market.updated_at ? new Date(typeof market.updated_at === 'number' && market.updated_at < 1e12 ? market.updated_at * 1000 : market.updated_at) : null;
  const stale = updated && Number.isFinite(updated.getTime()) && Date.now() - updated.getTime() > 120000;
  $('connectionStatus').className = `connection ${errors.length || stale ? 'offline' : status.fresh ? 'online' : ''}`;
  $('connectionStatus').replaceChildren(document.createElement('i'), document.createTextNode(errors.length ? 'Veri hatası' : stale ? 'Veri gecikiyor' : status.fresh ? 'Veri akışı bağlı' : 'Veri bekleniyor'));

  $('emptyPosition').hidden = positions.length > 0;
  $('positionDetails').hidden = positions.length === 0;
  text('positionBadge', `${positions.length} aktif pozisyon`);
  text('positionLimit', config.max_open_positions ?? '1');
  paintValue('unrealizedPnl', account.unrealized_pnl, { signed: true, color: true, suffix: ' USDT' });
  renderPositions(positions);
  renderChartPosition(market, positions);
  renderEmergency(current);
  renderScanner(current);
  renderTrades(trades);
  renderBacktest(current.backtest);
  updateChart(market);
  observeFills(current);
  updateButtons();
}

function updateButtons() {
  const current = state || {};
  const running = Boolean(current.status?.running);
  const positions = positionsOf(current);
  const emergency = current.emergency || {};
  const pending = Array.isArray(emergency.pending_symbols) ? emergency.pending_symbols : [];
  $('startButton').disabled = requestBusy || emergencyBusy || !state || running || configDirty || Boolean(emergency.active);
  $('startButton').title = emergency.active ? 'Başlatmadan önce acil kilidini kaldırın.' : configDirty ? 'Başlatmadan önce değişiklikleri kaydedin.' : '';
  $('pauseButton').disabled = requestBusy || !state || !running;
  $('closeButton').disabled = requestBusy || positions.length === 0;
  $('resetButton').disabled = requestBusy || !state || running || positions.length > 0;
  $('saveButton').disabled = requestBusy || !state || running || positions.length > 0;
  $('saveButton').title = running || positions.length ? 'Ayarlar için botu duraklatın ve tüm açık pozisyonları kapatın.' : '';
  $('focusButton').disabled = requestBusy || !state;
  $('positionsBody').querySelectorAll('[data-close-position]').forEach((button) => { button.disabled = requestBusy; });
  $('chartClosePosition').disabled = requestBusy || emergencyBusy || !state;
  $('backtestButton').disabled = backtestBusy || !state;
  $('emergencyButton').disabled = emergencyBusy;
  $('clearEmergencyButton').disabled = requestBusy || emergencyBusy || !emergency.active || pending.length > 0 || positions.length > 0;
  configForm.elements.namedItem('initial_cash').disabled = positions.length > 0 || (Array.isArray(current.trades) && current.trades.length > 0);
  updateManualControls();
}

function renderPositions(positions) {
  const tbody = $('positionsBody'); tbody.replaceChildren();
  const marketMap = new Map(marketsOf().map((market) => [market.symbol, market]));
  positions.forEach((position) => {
    const row = document.createElement('tr');
    const symbol = position.symbol || state.config?.symbol || '—';
    row.className = `position-row${symbol === focusedSymbol() ? ' is-chart-selected' : ''}`;
    row.dataset.symbol = symbol;
    row.tabIndex = 0;
    row.setAttribute('aria-label', `${symbol} pozisyonunun grafiğini aç`);
    row.addEventListener('click', (event) => { if (!event.target.closest('button, a, input')) selectChartSymbol(symbol, true); });
    row.addEventListener('keydown', (event) => {
      if (event.target === row && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); selectChartSymbol(symbol, true); }
    });
    const pairCell = addCell(row, '');
    const chartButton = document.createElement('button'); chartButton.type = 'button'; chartButton.className = 'position-chart-button'; chartButton.textContent = `${symbol} ↗`;
    chartButton.setAttribute('aria-label', `${symbol} grafiğini aç`);
    chartButton.addEventListener('click', (event) => { event.stopPropagation(); selectChartSymbol(symbol, true); });
    pairCell.append(chartButton);
    const quantity = document.createElement('small'); quantity.textContent = `${formatQuantity.format(numeric(position.quantity) || 0)} ${symbol.replace(/USDT$/, '')}`; pairCell.append(quantity);
    const market = marketMap.get(symbol) || {};
    const lastPrice = position.last_price ?? market.price;
    const quoteMissing = Boolean(position.quote_missing) || numeric(lastPrice) === null;
    const priceCell = addCell(row, `${price(position.entry_price)} / ${price(lastPrice)}`);
    if (quoteMissing) { const warning = document.createElement('small'); warning.className = 'negative'; warning.textContent = 'Güncel kotasyon eksik'; priceCell.append(warning); }
    let pnl = position.unrealized_pnl ?? position.net_pnl;
    if (pnl === undefined && !quoteMissing) {
      const cfg = state.config || {};
      pnl = numeric(position.quantity) * numeric(lastPrice) * (1 - (numeric(cfg.slippage_pct) || 0) / 100) * (1 - (numeric(cfg.sell_fee_pct) || 0) / 100) - numeric(position.entry_cost);
    }
    const pnlCell = addCell(row, quoteMissing ? '—' : money(pnl, true), quoteMissing ? '' : numeric(pnl) > 0 ? 'positive' : numeric(pnl) < 0 ? 'negative' : '');
    const pnlHint = document.createElement('small'); pnlHint.textContent = quoteMissing ? 'Son değerle toplamda yer alabilir' : 'Satış maliyeti düşülmüş'; pnlCell.append(pnlHint);
    addCell(row, numeric(position.fixed_stop_price) === null ? 'Kapalı' : price(position.fixed_stop_price));
    const trailing = addCell(row, position.trailing_active ? price(position.trailing_stop_price) : 'Eşik bekleniyor');
    const trailingHint = document.createElement('small'); trailingHint.textContent = position.trailing_active ? state.config?.trailing_mode === 'net_profit' ? 'Net kâr takibi etkin' : 'Fiyat takibi etkin' : `${percent(state.config?.trailing_activation_pct)} net getiri eşiği`; trailing.append(trailingHint);
    addCell(row, price(position.highest_price));
    const action = document.createElement('td'); const button = document.createElement('button'); button.type = 'button'; button.dataset.closePosition = symbol; button.className = 'button button-danger button-small'; button.textContent = 'Kapat'; button.setAttribute('aria-label', `${symbol} deneme pozisyonunu kapat`); button.addEventListener('click', (event) => { event.stopPropagation(); control('close_position', symbol); }); action.append(button); row.append(action);
    tbody.append(row);
  });
}

function renderScanner(current) {
  const scanner = current.scanner || {};
  const cfg = current.config || {};
  const total = numeric(scanner.total_symbols);
  const scanned = numeric(scanner.scanned_symbols);
  const all = (scanner.mode || cfg.universe_mode) === 'all';
  text('universeBadge', all ? 'Tüm USDT pariteleri' : 'Seçili pariteler');
  text('scannerCount', `${scanned ?? '—'} / ${total ?? '—'} parite`);
  $('scanProgress').value = total > 0 && scanned !== null ? Math.min(100, Math.max(0, scanned / total * 100)) : 0;
  const pending = scanner.pending_scans !== undefined ? ` · Bekleyen ${scanner.pending_scans}` : '';
  text('scannerLast', `${scanner.last_symbol ? `Son taranan: ${scanner.last_symbol}` : 'Tarama verisi bekleniyor'}${pending}${scanner.last_scan_at ? ` · ${dateTime(scanner.last_scan_at)}` : ''}`);
  const errors = Array.isArray(scanner.errors) ? scanner.errors.length : numeric(scanner.errors) || 0;
  text('scannerErrors', `${scanner.batch_size ?? '—'} parite / grup · ${errors} tarama hatası${scanner.warning ? ` · ${scanner.warning}` : ''}`);
  const markets = marketsOf(current);
  text('marketSummaryCount', markets.length);
  const key = JSON.stringify(markets);
  if (key === marketSummaryKey) return;
  marketSummaryKey = key;
  const tbody = $('marketsBody'); tbody.replaceChildren();
  if (!markets.length) { const row = document.createElement('tr'); const cell = addCell(row, 'Henüz taranan piyasa yok.', 'empty-cell'); cell.colSpan = 5; tbody.append(row); return; }
  markets.forEach((market) => {
    const row = document.createElement('tr');
    addCell(row, market.symbol || '—'); addCell(row, price(market.price));
    const signal = Object.prototype.hasOwnProperty.call(market, 'signal_details') && !market.signal_details ? null : market.last_signal;
    addCell(row, { buy: 'Alış', sell: 'Satış', hold: 'Bekle' }[signal] || signal || 'Henüz taranmadı', signal === 'buy' ? 'positive' : signal === 'sell' ? 'negative' : '');
    addCell(row, dateTime(market.updated_at));
    const cell = addCell(row, market.error ? 'Hata' : numeric(market.price) === null ? 'Veri bekleniyor' : 'Kotasyon alındı', market.error ? 'negative' : '');
    if (market.error) cell.title = market.error;
    tbody.append(row);
  });
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
  const identity = `Son test: ${report.symbol || state?.config?.symbol || '—'} · ${report.interval || state?.config?.interval || '—'} · ${report.is_synthetic ? 'SENTETİK DEMO' : 'Binance verisi'}. Tek parite sonucu; portföy simülasyonu değildir.`;
  text('backtestNotes', `${identity}\n${report.train_candles ?? '—'} gözlem/ısınma mumu · ${report.test_candles ?? '—'} değerlendirme mumu. ${period}\n${notes}`.trim());
}

async function fetchState() {
  if (fetching) return;
  fetching = true;
  const version = emergencyVersion;
  try { const current = await api('/api/state'); if (version === emergencyVersion) render(current); }
  catch (error) {
    networkHealthy = false;
    renderLivePnl();
    $('connectionStatus').className = 'connection offline';
    $('connectionStatus').replaceChildren(document.createElement('i'), document.createTextNode('Bağlantı yok'));
    $('errorBanner').hidden = false;
    text('errorBanner', `Sunucuya bağlanılamadı: ${error.message} Gösterilen değerler güncel olmayabilir.`);
    text('refreshStatus', 'Bağlantı yok · otomatik yeniden deneniyor');
    $('refreshStatus').className = 'negative';
    updateButtons();
    if (state) updateChart(focusedMarket(state));
  } finally { fetching = false; }
}

async function control(action, symbol) {
  if (requestBusy) return;
  requestBusy = true; updateButtons();
  const version = emergencyVersion;
  try {
    const result = await api('/api/control', { method: 'POST', body: JSON.stringify({ action, ...(symbol ? { symbol } : {}), ...(action === 'clear_emergency' ? { expected_emergency_revision: state?.emergency?.revision } : {}) }) });
    if (action === 'reset') { configDirty = false; hasHydrated = false; chartPoints = []; fillsInitialized = false; seenEntries = new Set(); seenExits = new Set(); $('latestExecution').hidden = true; }
    if (result.config && version === emergencyVersion) render(result);
    await fetchState();
    if (version === emergencyVersion) toast({ start: 'Bot başlatıldı. Yalnızca deneme işlemleri yapılır.', pause: 'Yeni strateji işlemleri duraklatıldı. Açık pozisyonların stop takibi sürer.', close_position: `${symbol || ''} deneme pozisyonu kapatıldı.`, close_all: 'Otomasyon duraklatıldı ve tüm deneme pozisyonları kapatıldı.', reset: 'Deneme defteri sıfırlandı.', clear_emergency: 'Acil kilit kaldırıldı. Bot duraklatılmış durumda; otomasyon için ayrıca Başlat seçin.' }[action]);
  } catch (error) { toast(error.message, true); }
  finally { requestBusy = false; updateButtons(); }
}

async function manualTrade(action) {
  if (requestBusy || emergencyBusy || !state) return;
  const context = manualContext();
  if (!context.fresh) { toast('İşlem için seçili paritenin güncel fiyatı gerekli.', true); return; }
  if (action === 'buy' && (!context.known || $('manualBuyButton').disabled || !$('manualTradeForm').reportValidity())) return;
  if (action === 'sell' && !context.position) { toast('Seçili paritede satılacak açık pozisyon yok.', true); return; }
  const body = { action, symbol: context.symbol };
  if (action === 'buy') body.amount = $('manualAmount').value;
  const version = emergencyVersion;
  const notificationVersion = fillNotificationVersion;
  requestBusy = true; updateButtons();
  try {
    const result = await api('/api/control', { method: 'POST', body: JSON.stringify(body) });
    if (result.config && version === emergencyVersion) render(result);
    await fetchState();
    if (version === emergencyVersion && notificationVersion === fillNotificationVersion) toast(`${context.symbol} manuel ${action === 'buy' ? 'alış' : 'satış'} işlemi uygulandı (deneme).`);
  } catch (error) { toast(error.message, true); }
  finally { requestBusy = false; updateButtons(); }
}

async function emergencyClose() {
  if (emergencyBusy) return;
  if (!window.confirm('ACİL DURDURMA\n\nBot durdurulacak ve yeni alışlar kalıcı olarak kilitlenecek. Güncel fiyatı olan tüm deneme pozisyonları kapatılacak; fiyatı eksik olanlar veri akışı geldiğinde kapatılmak üzere bekleyecek.\n\nDevam edilsin mi?')) return;
  emergencyBusy = true;
  emergencyVersion += 1;
  updateButtons();
  text('emergencyButton', 'ACİL DURDURMA UYGULANIYOR…');
  try {
    const result = await api('/api/control', { method: 'POST', body: JSON.stringify({ action: 'emergency_close' }) });
    emergencyVersion += 1;
    if (result.config) render(result);
    await fetchState();
    const emergency = state?.emergency || result.emergency || {};
    const pending = Array.isArray(emergency.pending_symbols) ? emergency.pending_symbols.length : 0;
    toast(pending ? `Acil kilit etkin. ${pending} pozisyon güncel fiyatla kapatılmayı bekliyor.` : positionsOf().length ? 'Acil kilit etkin. Açık pozisyonların kapatma durumu izleniyor.' : 'Acil kilit etkin. Tüm deneme pozisyonları kapalı; bot durduruldu.');
  } catch (error) {
    emergencyVersion += 1;
    toast(`Acil durum isteğinin sonucu doğrulanamadı: ${error.message} Panel durumu yeniden sorgulanıyor.`, true);
    await fetchState();
  } finally {
    emergencyBusy = false;
    text('emergencyButton', 'ACİL DURDUR / TÜMÜNÜ KAPAT');
    updateButtons();
  }
}

function configChanged(event) {
  if (event.target.matches('[data-local-control]')) return;
  markConfigDirty();
  if (event.target.name === 'universe_mode') renderUniversePicker();
}
configForm.addEventListener('input', configChanged);
configForm.addEventListener('change', configChanged);
configForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (requestBusy || !configForm.reportValidity()) return;
  const payload = {};
  for (const [name, value] of new FormData(configForm)) payload[name] = ['interval', 'strategy', 'data_source', 'trailing_mode', 'universe_mode'].includes(name) ? String(value) : Number(value);
  payload.symbols = [...selectedSymbols].sort();
  if (payload.universe_mode === 'selected' && payload.symbols.length === 0) { toast('İzlemek için en az bir parite seçin.', true); return; }
  requestBusy = true; updateButtons(); text('settingsState', 'Kaydediliyor…');
  const version = emergencyVersion;
  try {
    const result = await api('/api/config', { method: 'PATCH', body: JSON.stringify(payload) });
    configDirty = false; hasHydrated = false;
    if (result.config && version === emergencyVersion) render(result);
    await fetchState();
    if (version === emergencyVersion) toast('Ayarlar kaydedildi.');
  } catch (error) { toast(error.message, true); text('settingsState', 'Kaydedilemedi · değişiklikler korunuyor'); }
  finally { requestBusy = false; updateButtons(); }
});
$('startButton').addEventListener('click', () => control('start'));
$('pauseButton').addEventListener('click', () => control('pause'));
$('manualTradeForm').addEventListener('submit', (event) => { event.preventDefault(); manualTrade('buy'); });
$('manualSellButton').addEventListener('click', () => manualTrade('sell'));
$('manualSymbol').addEventListener('input', () => { manualSymbolTouched = true; $('manualSymbol').setCustomValidity(''); updateManualControls(); });
$('manualSymbol').addEventListener('change', updateManualControls);
$('manualAmount').addEventListener('input', () => { manualAmountTouched = true; updateManualControls(); });
$('emergencyButton').addEventListener('click', emergencyClose);
$('clearEmergencyButton').addEventListener('click', () => control('clear_emergency'));
$('closeButton').addEventListener('click', () => control(Array.isArray(state?.positions) ? 'close_all' : 'close_position', Array.isArray(state?.positions) ? undefined : state?.config?.symbol));
$('refreshSymbolsButton').addEventListener('click', () => loadSymbols(true));
$('symbolSearch').addEventListener('input', renderUniversePicker);
$('clearSelectionButton').addEventListener('click', () => { selectedSymbols.clear(); markConfigDirty(); renderUniversePicker(); });
$('focusSymbol').addEventListener('input', () => { focusDirty = true; $('focusSymbol').setCustomValidity(''); });
$('focusForm').addEventListener('submit', (event) => {
  event.preventDefault();
  if (!state) return;
  const symbol = $('focusSymbol').value.trim().toUpperCase();
  if (catalogue.length && !catalogue.some((entry) => entry.symbol === symbol) && !positionsOf().some((entry) => entry.symbol === symbol)) {
    $('focusSymbol').setCustomValidity('Geçerli veri kaynağının listesinden bir USDT paritesi seçin.');
    $('focusSymbol').reportValidity(); return;
  }
  selectChartSymbol(symbol);
});
$('chartPickerButton').addEventListener('click', () => { if ($('chartAssetPicker').hidden) openChartPicker(); else closeChartPicker(); });
$('chartPickerClose').addEventListener('click', () => closeChartPicker());
$('chartPickerRefresh').addEventListener('click', () => loadSymbols(true));
$('chartAssetSearch').addEventListener('input', () => { chartPickerIndex = 0; renderChartPicker(); });
$('chartAssetSearch').addEventListener('keydown', (event) => {
  if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
    event.preventDefault();
    highlightChartOption(event.key === 'Home' ? 0 : event.key === 'End' ? chartPickerOptions().length - 1 : chartPickerIndex + (event.key === 'ArrowDown' ? 1 : -1), true);
  } else if (event.key === 'Enter') {
    event.preventDefault(); chartPickerOptions()[chartPickerIndex]?.click();
  }
});
$('chartAssetPicker').addEventListener('keydown', (event) => { if (event.key === 'Escape') { event.preventDefault(); closeChartPicker(); } });
document.addEventListener('click', (event) => { if (!$('chartAssetPicker').hidden && !event.target.closest('.chart-picker-anchor')) closeChartPicker(false); });
document.addEventListener('focusin', (event) => { if (!$('chartAssetPicker').hidden && !event.target.closest('.chart-picker-anchor')) closeChartPicker(false); });
$('chartClosePosition').addEventListener('click', () => { const symbol = focusedSymbol(); if (positionsOf().some((position) => position.symbol === symbol)) control('close_position', symbol); });
$('resetButton').addEventListener('click', () => { $('resetDialog').returnValue = ''; $('resetDialog').showModal(); });
$('resetDialog').addEventListener('cancel', () => { $('resetDialog').returnValue = 'cancel'; });
$('resetDialog').addEventListener('close', () => { if ($('resetDialog').returnValue === 'confirm') control('reset'); });
$('backtestForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (backtestBusy || !state) return;
  backtestBusy = true; updateButtons();
  text('backtestButton', 'Geçmiş test çalışıyor…'); $('backtestEmpty').hidden = true;
  try {
    const result = await api('/api/backtest', { method: 'POST', body: JSON.stringify({ symbol: focusedSymbol(), interval: state.config.interval, limit: 500, strategy: $('backtestStrategy').value }) });
    if (result.results) renderBacktest(result);
    else if (result.backtest) renderBacktest(result.backtest);
    await fetchState();
    toast('Geçmiş test tamamlandı. Sonuçlar komisyon ve kayma içerir.');
  } catch (error) { toast(error.message, true); $('backtestEmpty').hidden = Boolean(state.backtest?.results?.length); }
  finally { backtestBusy = false; text('backtestButton', 'Geçmiş testi çalıştır'); updateButtons(); }
});
document.querySelectorAll('[data-positions-window]').forEach((link) => link.addEventListener('click', (event) => {
  if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  const popup = window.open('', 'spot-positions', 'popup=yes,width=1440,height=960');
  if (popup) {
    popup.opener = null;
    popup.location.href = link.href;
    event.preventDefault();
  }
}));
document.querySelectorAll('nav a').forEach((link) => link.addEventListener('click', () => { document.querySelectorAll('[data-positions-window]').forEach((link) => link.addEventListener('click', (event) => {
  if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  const popup = window.open('', 'spot-positions', 'popup=yes,width=1440,height=960');
  if (popup) {
    popup.opener = null;
    popup.location.href = link.href;
    event.preventDefault();
  }
}));
document.querySelectorAll('nav a').forEach((item) => item.classList.remove('active')); link.classList.add('active'); }));
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') fetchState(); });
updateButtons();
fetchState();
setInterval(fetchState, 3000);
setInterval(() => { if (state) renderLivePnl(); }, 1000);
