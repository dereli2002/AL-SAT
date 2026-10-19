'use strict';

// This view reads the same paper account as the main panel; viewing never trades.
const $ = (id) => document.getElementById(id);
const numberFormat = new Intl.NumberFormat('tr-TR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const priceFormat = new Intl.NumberFormat('tr-TR', { minimumFractionDigits: 2, maximumFractionDigits: 10 });
const quantityFormat = new Intl.NumberFormat('tr-TR', { maximumFractionDigits: 10 });
const timeFormat = new Intl.DateTimeFormat('tr-TR', { timeZone: 'Europe/Istanbul', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
let state = null;
let lastStateAt = 0;
let networkHealthy = false;
let polling = false;
let requestBusy = false;
let emergencyBusy = false;
let emergencyUncertain = false;
let mutationVersion = 0;
let toastTimer;
let catalogue = [];
let catalogueSource = '';
let catalogueBusy = false;
let catalogueRetryAt = 0;
let catalogueError = '';
const cards = new Map();
const buyPicker = new SpotAssetPicker({
  trigger: $('windowBuyPickerButton'),
  anchor: $('windowBuyPickerAnchor'),
  title: 'İşlem yapılacak varlık',
  onSelect: (symbol) => { $('windowBuySymbol').value = symbol; renderBuy(); },
  onRefresh: () => loadSymbols(state?.config?.data_source),
});

function numeric(value) {
  if (value === '' || value === null || value === undefined) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}
function instant(value) {
  if (!value) return null;
  const result = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value).getTime();
  return Number.isFinite(result) ? result : null;
}
function dateTime(value) { const stamp = instant(value); return stamp ? timeFormat.format(stamp) : '—'; }
function money(value, signed = false) { const n = numeric(value); return n === null ? '—' : `${signed && n > 0 ? '+' : ''}${numberFormat.format(n)}`; }
function price(value) { const n = numeric(value); return n === null ? '—' : priceFormat.format(n); }
function percent(value) { const n = numeric(value); return n === null ? '—' : `${n > 0 ? '+' : ''}%${numberFormat.format(n)}`; }
function text(element, value) { const next = String(value ?? '—'); if (element.textContent !== next) element.textContent = next; }
function paint(element, value, suffix = '') {
  const n = numeric(value);
  text(element, `${money(n, true)}${n === null ? '' : suffix}`);
  element.classList.toggle('positive', n > 0);
  element.classList.toggle('negative', n < 0);
}
function positions() { return Array.isArray(state?.positions) ? state.positions : []; }
function markets() { return Array.isArray(state?.markets) ? state.markets : []; }
function marketFor(symbol) { return markets().find((market) => market.symbol === symbol) || {}; }
function freshQuote(symbol) {
  const market = marketFor(symbol);
  const stamp = instant(market.updated_at);
  return Boolean(networkHealthy && state?.status?.fresh && lastStateAt && Date.now() - lastStateAt < 30000 && stamp && Date.now() - stamp < 30000 && numeric(market.price) > 0);
}
function freshPosition(position) { return !position.quote_missing && freshQuote(position.symbol) && numeric(position.unrealized_pnl) !== null; }
function locked() { return Boolean(state?.emergency?.active || emergencyBusy || emergencyUncertain); }
function recentState() { return Boolean(networkHealthy && lastStateAt && Date.now() - lastStateAt < 30000); }
function currentFeed() {
  const stamp = instant(state?.market?.updated_at || markets().find((market) => market.updated_at)?.updated_at);
  return Boolean(recentState() && state?.status?.fresh && stamp && Date.now() - stamp < 30000);
}
function toast(message, error = false) {
  clearTimeout(toastTimer);
  text($('windowToast'), message);
  $('windowToast').classList.toggle('error', error);
  $('windowToast').hidden = false;
  toastTimer = setTimeout(() => { $('windowToast').hidden = true; }, error ? 10000 : 6000);
}
async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30000);
  try {
    const response = await fetch(path, { ...options, cache: 'no-store', headers: { 'Content-Type': 'application/json', ...options.headers }, signal: controller.signal });
    let data;
    try { data = await response.json(); } catch { throw new Error('Sunucudan geçerli yanıt alınamadı.'); }
    if (!response.ok) throw new Error(data.error || `İstek başarısız (${response.status}).`);
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('İstek zaman aşımına uğradı; güncel işlem durumunu kontrol edin.');
    throw error;
  } finally { clearTimeout(timer); }
}

function createCard(symbol) {
  const element = document.createElement('article');
  element.className = 'position-graph-card';
  element.dataset.positionSymbol = symbol;
  // Only fixed markup is parsed. Exchange symbols and all API values use textContent.
  element.innerHTML = '<header class="position-graph-heading"><div><h2 data-field="symbol"></h2><small data-field="quantity"></small></div><div class="position-graph-quote"><strong data-field="price">—</strong><small data-field="quote">Fiyat bekleniyor</small></div></header><div class="position-market-chart"></div><div class="position-graph-details"><div><span>Alış fiyatı · USDT</span><strong data-field="entry">—</strong></div><div><span>Anlık net kâr / zarar</span><strong data-field="pnl">—</strong><small data-field="pnlPct">—</small></div><div><span>Alış altı stop · USDT</span><strong data-field="fixed">—</strong></div><div><span>Kâr takip stopu · USDT</span><strong data-field="trailing">—</strong><small data-field="trailingStatus"></small></div><div><span>Alış maliyeti · USDT</span><strong data-field="cost">—</strong></div><div><span>Açılış zamanı</span><strong data-field="opened">—</strong></div></div><div class="position-graph-actions"><p>İzlemek işlemi kapatmaz. Otomasyon açıksa strateji ve stop kuralları işlemeye devam eder.</p><button data-close-position type="button" class="button button-danger">SAT / POZİSYONU KAPAT</button></div>';
  const fields = Object.fromEntries([...element.querySelectorAll('[data-field]')].map((node) => [node.dataset.field, node]));
  const close = element.querySelector('[data-close-position]');
  close.dataset.closePosition = symbol;
  close.setAttribute('aria-label', `${symbol} deneme pozisyonunu sat ve kapat`);
  close.addEventListener('click', () => control('close_position', symbol));
  text(fields.symbol, `${symbol.replace(/USDT$/, '')} / USDT`);
  const chart = new SpotMarketChart(element.querySelector('.position-market-chart'), { interval: '5m', height: 230 });
  const card = { element, fields, close, chart };
  cards.set(symbol, card);
  $('positionsGrid').append(element);
  return card;
}

function renderCard(position) {
  const card = cards.get(position.symbol) || createCard(position.symbol);
  const fields = card.fields;
  const fresh = freshPosition(position);
  text(fields.quantity, `${quantityFormat.format(numeric(position.quantity) || 0)} ${position.symbol.replace(/USDT$/, '')}`);
  text(fields.price, fresh ? price(marketFor(position.symbol).price) : '—');
  text(fields.quote, fresh ? `${dateTime(marketFor(position.symbol).updated_at)} · USDT` : position.quote_missing ? 'Güncel fiyat eksik' : 'Güncel fiyat bekleniyor');
  text(fields.entry, price(position.entry_price));
  paint(fields.pnl, fresh ? position.unrealized_pnl : null, ' USDT');
  text(fields.pnlPct, fresh ? percent(position.unrealized_pnl_pct) : 'Güncel fiyatla hesaplanamıyor');
  fields.pnlPct.classList.toggle('positive', fresh && numeric(position.unrealized_pnl_pct) > 0);
  fields.pnlPct.classList.toggle('negative', fresh && numeric(position.unrealized_pnl_pct) < 0);
  text(fields.fixed, numeric(position.fixed_stop_price) === null ? 'Kapalı' : price(position.fixed_stop_price));
  text(fields.trailing, position.trailing_active ? price(position.trailing_stop_price) : 'Eşik bekleniyor');
  text(fields.trailingStatus, position.trailing_active ? state.config.trailing_mode === 'net_profit' ? 'Net kâr takibi etkin' : 'Fiyat takibi etkin' : `Etkinleşme eşiği: ${percent(state.config.trailing_activation_pct)}`);
  text(fields.cost, money(position.entry_cost));
  text(fields.opened, dateTime(position.opened_at || position.entry_time));
  card.element.classList.toggle('is-stale', !fresh);
  card.close.disabled = requestBusy || emergencyBusy || !fresh;
  card.close.title = fresh ? 'Bu paritenin tamamını satar; diğer pozisyonlar açık kalır.' : 'Güncel fiyat gelmeden satış yapılamaz. Acil durdurma kilidi her zaman uygulanabilir.';
  card.chart.update({
    symbol: position.symbol,
    source: state.config.data_source,
    market: marketFor(position.symbol),
    position,
    networkHealthy,
    statusFresh: state.status?.fresh,
  });
}

function renderBuy() {
  buyPicker.update({
    catalogue,
    source: state?.config?.data_source,
    markets: markets(),
    status: state?.status,
    loading: catalogueBusy,
    error: catalogueError,
    selectedSymbol: $('windowBuySymbol').value,
    networkHealthy,
  });
  const symbol = $('windowBuySymbol').value.trim().toUpperCase();
  const amount = numeric($('windowBuyAmount').value);
  const alreadyOpen = positions().some((position) => position.symbol === symbol);
  const valid = catalogueSource === state?.config?.data_source && catalogue.some((entry) => entry.symbol === symbol);
  const fresh = valid && freshQuote(symbol);
  const cash = numeric(state?.account?.cash) || 0;
  const dailyHalted = state?.account?.risk?.daily_halted;
  const atLimit = positions().length >= (numeric(state?.config?.max_open_positions) || 0);
  text($('windowBuyQuote'), !symbol ? 'Varlık seçin' : !valid ? 'Aktif USDT Spot listesinden bir varlık seçin' : fresh ? `Son fiyat: ${price(marketFor(symbol).price)} USDT` : 'Güncel fiyat bekleniyor');
  $('windowBuy').disabled = requestBusy || catalogueBusy || Boolean(catalogueError) || locked() || !fresh || alreadyOpen || !(amount > 0 && amount <= cash) || dailyHalted || atLimit;
  text($('windowBuyStatus'), locked() ? 'Acil kilit açık; yeni alışlar engellendi.' : catalogueBusy ? 'Varlık listesi doğrulanıyor; yeni alış bekliyor.' : catalogueError ? 'Varlık listesi doğrulanamadı. Listeyi yenileyerek yeniden deneyin.' : dailyHalted ? 'Günlük zarar sınırı nedeniyle yeni alışlar kilitli.' : atLimit ? 'Açık pozisyon sınırına ulaşıldı.' : alreadyOpen ? 'Bu paritede zaten açık pozisyon var; kısmi satış veya ek alım yapılmaz.' : amount > cash ? 'Alış tutarı kullanılabilir USDT bakiyesini aşıyor.' : 'Güncel fiyat, bakiye ve risk sınırları sunucuda tekrar kontrol edilir.');
}

function render() {
  if (!state) return;
  const open = positions();
  const allFresh = recentState() && open.every(freshPosition) && !state.account?.risk?.valuation_stale;
  const pnl = allFresh ? open.length ? state.account.unrealized_pnl : 0 : null;
  const pnlPct = allFresh ? open.length ? state.account.unrealized_pnl_pct : 0 : null;
  const active = locked();
  const pending = state.emergency?.pending_symbols || [];
  paint($('openPnl'), pnl, ' USDT');
  text($('openPnlPct'), percent(pnlPct));
  text($('openPnlStatus'), !allFresh ? 'Güncel fiyat eksik veya bağlantı kesildi; anlık sonuç gösterilmiyor' : !open.length ? 'Açık işlem yok' : 'Açık alış maliyeti üzerinden · tahmini satış giderleri sonrası');
  $('openPnlStatus').classList.toggle('negative', !allFresh);
  text($('positionsCount'), open.length);
  text($('windowLimit'), `Pozisyon sınırı: ${state.config.max_open_positions}`);
  text($('windowCash'), `${money(state.account.cash)} USDT`);
  text($('windowAutomation'), active ? 'Acil kilit etkin · otomasyon durduruldu' : state.status.running ? 'Otomasyon çalışıyor' : 'Otomasyon duraklatıldı');
  text($('windowStatus'), currentFeed() ? 'Güncel fiyat akışı' : 'Güncel fiyat bekleniyor');
  $('windowStatus').classList.toggle('negative', !currentFeed());
  text($('windowUpdated'), `Son panel güncellemesi: ${dateTime(lastStateAt)} · 3 sn`);
  $('windowDemo').hidden = state.config.data_source !== 'demo';
  $('windowPause').disabled = requestBusy || emergencyBusy || !state.status.running;
  $('windowCloseAll').disabled = requestBusy || emergencyBusy || !open.length;
  $('windowEmergency').disabled = emergencyBusy;
  $('windowEmergencyBanner').hidden = !active;
  text($('windowEmergencyDetails'), emergencyBusy ? 'Önce yeni alışlar kilitleniyor; ardından pozisyonların kapatılması isteniyor…' : emergencyUncertain ? 'Acil isteğin sonucu doğrulanamadı. Yeniden ACİL DURDUR seçin; yeni alışlar bu ekranda engelleniyor.' : pending.length ? `${pending.join(', ')} güncel fiyatla kapatılmayı bekliyor. Fiyat gelince kapatma otomatik sürer.${state.emergency?.last_error ? ` ${state.emergency.last_error}` : ''}` : 'Tüm pozisyonlar kapalı. Kilidi açıkça kaldırmadıkça yeni alış yapılmaz.');
  $('windowUnlock').disabled = requestBusy || emergencyBusy || !active || open.length > 0 || pending.length > 0;
  $('positionsEmpty').hidden = open.length > 0;
  const symbols = new Set(open.map((position) => position.symbol));
  for (const [symbol, card] of cards) if (!symbols.has(symbol)) { card.chart.dispose(); card.element.remove(); cards.delete(symbol); }
  open.forEach(renderCard);
  renderBuy();
  const source = state.config.data_source;
  if ((catalogueSource !== source || catalogueError) && !catalogueBusy && Date.now() >= catalogueRetryAt) loadSymbols(source);
}

async function loadSymbols(source) {
  if (catalogueBusy || !source) return;
  catalogueBusy = true;
  catalogueError = '';
  if (catalogueSource !== source) {
    catalogue = [];
  }
  text($('windowCatalogStatus'), 'Varlık listesi yükleniyor…');
  $('windowRefreshSymbols').disabled = true;
  renderBuy();
  try {
    const data = await api('/api/symbols');
    if (state?.config?.data_source !== source || Boolean(data.is_synthetic) !== (source === 'demo')) return;
    catalogue = Array.isArray(data.symbols) ? data.symbols : [];
    catalogueSource = source;
    catalogueError = data.error || '';
    catalogueRetryAt = data.error ? Date.now() + 60000 : 0;
    text($('windowCatalogStatus'), data.error ? `Liste doğrulanamadı: ${data.error}` : `${catalogue.length} aktif USDT Spot paritesi`);
    $('windowCatalogStatus').classList.toggle('negative', Boolean(data.error));
  } catch (error) {
    catalogueError = error.message;
    catalogueRetryAt = Date.now() + 60000;
    text($('windowCatalogStatus'), `Liste yüklenemedi: ${error.message}`);
    $('windowCatalogStatus').classList.add('negative');
  } finally {
    catalogueBusy = false;
    $('windowRefreshSymbols').disabled = false;
    renderBuy();
  }
}

function acceptState(current) {
  state = current;
  lastStateAt = Date.now();
  networkHealthy = true;
  $('windowError').hidden = !current.status?.error;
  text($('windowError'), current.status?.error || '');
  render();
}
async function fetchState() {
  if (polling || emergencyBusy) return;
  polling = true;
  const version = mutationVersion;
  try { const current = await api('/api/state'); if (version === mutationVersion) acceptState(current); }
  catch (error) {
    if (version === mutationVersion) {
      networkHealthy = false;
      text($('windowError'), `Panel bağlantısı: ${error.message} Güncel olmayan fiyatla işlem yapılmaz.`);
      $('windowError').hidden = false;
      if (state) render(); else text($('windowStatus'), 'Sunucuya bağlanılamadı');
    }
  } finally { polling = false; }
}

async function control(action, symbol, amount) {
  if (requestBusy || emergencyBusy || !state) return;
  if (action === 'close_position' && !confirm(`${symbol} deneme pozisyonunun tamamı güncel fiyatla satılsın mı? Diğer pozisyonlar açık kalır.`)) return;
  if (action === 'close_all' && !confirm('Otomasyon duraklatılsın ve tüm açık deneme pozisyonları güncel fiyatlarla kapatılsın mı?')) return;
  if (action === 'clear_emergency' && !confirm('Acil kilit kaldırılsın mı? Bot duraklatılmış kalır; buradan otomasyon başlatılmaz.')) return;
  requestBusy = true;
  const version = ++mutationVersion;
  render();
  try {
    const current = await api('/api/control', { method: 'POST', body: JSON.stringify({ action, ...(symbol ? { symbol } : {}), ...(amount ? { amount } : {}), ...(action === 'clear_emergency' ? { expected_emergency_revision: state.emergency?.revision } : {}) }) });
    if (version === mutationVersion) {
      if (action === 'clear_emergency') emergencyUncertain = false;
      acceptState(current);
      toast(action === 'pause' ? 'Otomasyon duraklatıldı. Açık pozisyonların stop takibi sürer.' : action === 'close_all' ? 'Otomasyon duraklatıldı; tüm deneme pozisyonları kapatıldı.' : action === 'clear_emergency' ? 'Acil kilit kaldırıldı. Otomasyon duraklatılmış durumda.' : `${symbol} deneme ${action === 'buy' ? 'alışı uygulandı' : 'pozisyonu kapatıldı'}.`);
    }
  } catch (error) { if (version === mutationVersion) toast(error.message, true); }
  finally { requestBusy = false; if (state) render(); fetchState(); }
}

async function emergencyClose() {
  if (emergencyBusy || !confirm('ACİL DURDUR: Otomasyon durdurulacak, yeni alışlar kilitlenecek ve tüm açık deneme pozisyonları kapatılacak. Fiyatı eksik pozisyonlar güncel fiyat gelince kapatılmayı bekler. Uygulansın mı?')) return;
  emergencyBusy = true;
  emergencyUncertain = true;
  mutationVersion += 1;
  $('windowEmergency').disabled = true;
  text($('windowEmergency'), 'ACİL DURDURMA UYGULANIYOR…');
  if (state) render();
  try {
    const current = await api('/api/control', { method: 'POST', body: JSON.stringify({ action: 'emergency_close' }) });
    mutationVersion += 1;
    emergencyUncertain = false;
    acceptState(current);
    toast(current.emergency?.pending_symbols?.length ? 'Acil kilit etkin. Fiyatı eksik pozisyonların kapatılması bekleniyor.' : 'Acil kilit etkin. Tüm deneme pozisyonları kapatıldı.');
  } catch (error) { mutationVersion += 1; toast(`Acil istek doğrulanamadı: ${error.message}`, true); }
  finally {
    emergencyBusy = false;
    text($('windowEmergency'), 'ACİL DURDUR / TÜMÜNÜ KAPAT');
    $('windowEmergency').disabled = false;
    if (state) render();
    fetchState();
  }
}

$('windowPause').addEventListener('click', () => control('pause'));
$('windowCloseAll').addEventListener('click', () => control('close_all'));
$('windowEmergency').addEventListener('click', emergencyClose);
$('windowUnlock').addEventListener('click', () => control('clear_emergency'));
$('windowBuySymbol').addEventListener('input', renderBuy);
$('windowBuyAmount').addEventListener('input', renderBuy);
$('windowRefreshSymbols').addEventListener('click', () => loadSymbols(state?.config?.data_source));
$('windowBuyForm').addEventListener('submit', (event) => {
  event.preventDefault();
  renderBuy();
  if (!$('windowBuy').disabled) control('buy', $('windowBuySymbol').value.trim().toUpperCase(), $('windowBuyAmount').value);
});
fetchState();
setInterval(fetchState, 3000);
// Re-evaluate quote ages even while a state request hangs. Stable card nodes and
// untouched forms preserve typing, keyboard focus and the window's scroll.
setInterval(() => { if (state) { if (!recentState()) networkHealthy = false; render(); } }, 1000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) fetchState(); });
