(function () {
  'use strict';

  const PROVIDERS = { steam: 'Steam', sonkwo: '杉果', steampy: 'SteamPY', steamcici: 'SteamCici' };
  const ROW_PROVIDERS = { sonkwo: '杉果', steampy: 'PY', steamcici: 'CICI' };
  const CDK_PROVIDERS = ['sonkwo', 'steampy', 'steamcici'];
  const FAVORITES_KEY = 'game-price-radar.favorites.v1';
  const THEME_KEY = 'game-price-radar.theme.v1';
  const PAGE_SIZE = 30;
  const icons = {
    external: ['M14 4h6v6', 'm20 4-9 9', 'M10 4H4v16h16v-6'],
    star: ['m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-2.9-5.6 2.9 1.1-6.2L3 9.6l6.2-.9Z'],
    store: ['M4 8h16v12H4Z', 'M3 8 5 3h14l2 5', 'M9 20v-7h6v7'],
    tag: ['M4 4h9l7 7-9 9-7-7Z', 'M8 8h.01'],
    chart: ['M4 4v16h16', 'm7 14 4-4 4 2 5-6'],
    clock: ['M12 7v5l3 2'],
    search: ['m16 16 4 4'],
    info: ['M12 8v5', 'M12 16h.01'],
    check: ['m5 12 4 4 10-10'],
    arrow: ['M5 12h14', 'm14 7 5 5-5 5'],
    alert: ['M12 8v5', 'M12 16h.01'],
    expand: ['M9 4H4v5', 'M15 4h5v5', 'M4 15v5h5', 'M20 15v5h-5'],
    close: ['m6 6 12 12', 'M18 6 6 18'],
    sun: ['M12 3v2', 'M12 19v2', 'M3 12h2', 'M19 12h2', 'm5.6 5.6 1.4 1.4', 'm17 17 1.4 1.4', 'm5.6 18.4 1.4-1.4', 'm17 7 1.4-1.4'],
    moon: ['M20 14a8.5 8.5 0 0 1-10-10 8.5 8.5 0 1 0 10 10Z']
  };
  const state = { data: null, catalog: null, catalogEntries: [], catalogStatus: 'loading', query: '', provider: 'all', sort: 'recommended', favorites: readFavorites(), favoritesOnly: false, selected: null, loading: false, catalogLoading: false, visibleLimit: PAGE_SIZE, searchTimer: null, toastTimer: null, dialogReturn: null, expirySignature: '', historyRanges: new Map(), historyReturn: null, contentSelections: new Map(), contentListOpen: new Map() };
  const $ = (id) => document.getElementById(id);

  function node(tag, className, value) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (value !== undefined && value !== null) result.textContent = String(value);
    return result;
  }
  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    if (['clock', 'info', 'alert'].includes(name)) {
      const circle = document.createElementNS(svg.namespaceURI, 'circle');
      circle.setAttribute('cx', '12'); circle.setAttribute('cy', '12'); circle.setAttribute('r', '9'); svg.append(circle);
    }
    if (name === 'search') {
      const circle = document.createElementNS(svg.namespaceURI, 'circle');
      circle.setAttribute('cx', '10.5'); circle.setAttribute('cy', '10.5'); circle.setAttribute('r', '6.5'); svg.append(circle);
    }
    if (name === 'sun') {
      const circle = document.createElementNS(svg.namespaceURI, 'circle'); circle.setAttribute('cx', '12'); circle.setAttribute('cy', '12'); circle.setAttribute('r', '4'); svg.append(circle);
    }
    (icons[name] || icons.external).forEach((d) => {
      const path = document.createElementNS(svg.namespaceURI, 'path'); path.setAttribute('d', d); svg.append(path);
    });
    return svg;
  }
  function safeUrl(value) {
    if (typeof value !== 'string' || !value.trim()) return null;
    try { const parsed = new URL(value, window.location.href); return ['https:', 'http:'].includes(parsed.protocol) ? parsed.href : null; } catch (_) { return null; }
  }
  function externalLink(label, href, className) {
    const url = safeUrl(href);
    const a = node(url ? 'a' : 'span', className || '', label);
    if (url) { a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer'; }
    return a;
  }
  function isPrice(value) { return typeof value === 'number' && Number.isFinite(value) && value >= 0; }
  function isQuoteExpired(quote, now, refreshMinutes) {
    if (!quote || window.location.protocol === 'file:') return false;
    const checked = dateValue(quote.checkedAt);
    const threshold = Math.max(60, (Number(refreshMinutes) || Number(state.data && state.data.refreshMinutes) || 15) * 4) * 60000;
    return !checked || (typeof now === 'number' ? now : Date.now()) - checked.getTime() > threshold;
  }
  function effectiveStatus(quote) { return quote && ['ok', 'unknown'].includes(quote.status) && isQuoteExpired(quote) ? 'stale' : quote && quote.status; }
  function isUsableQuote(quote) { return Boolean(quote && isPrice(quote.price) && ['ok', 'unknown'].includes(effectiveStatus(quote)) && quote.stock !== 'out_of_stock'); }
  function isPurchasable(quote) { return isUsableQuote(quote) && quote.status === 'ok' && quote.stock === 'in_stock' && quote.eligible === true; }
  function getSteamQuote(game) { return (game.quotes || []).find((quote) => quote.source === 'steam' && quote.kind === 'direct' && isUsableQuote(quote)) || null; }
  function getCdkQuote(game, provider, eligibleOnly) {
    return (game.quotes || []).filter((quote) => quote.kind === 'cdk' && CDK_PROVIDERS.includes(quote.source) && (provider === undefined || provider === 'all' || quote.source === provider) && isUsableQuote(quote) && (!eligibleOnly || isPurchasable(quote))).sort((a, b) => a.price - b.price || Number(isPurchasable(b)) - Number(isPurchasable(a)))[0] || null;
  }
  function normalizeQuery(value) {
    const text = String(value || '').trim();
    const match = text.match(/(?:store\.steampowered\.com|steamdb\.info)\/app\/(\d+)/i);
    return (match ? match[1] : text).toLocaleLowerCase('zh-CN');
  }
  function filterGames(games, query, options) {
    const term = normalizeQuery(query);
    const opts = options || {};
    const favorites = opts.favorites instanceof Set ? opts.favorites : new Set(opts.favorites || []);
    return games.filter((game) => {
      if (opts.favoritesOnly && !favorites.has(Number(game.appid))) return false;
      if (!term) return true;
      if (/^\d+$/.test(term) && String(game.appid) === term) return true;
      return [game.name, game.nameEn, ...(Array.isArray(game.aliases) ? game.aliases : [])].filter((item) => typeof item === 'string').some((item) => item.toLocaleLowerCase('zh-CN').includes(term));
    });
  }
  function computeSavings(game, provider) {
    const direct = getSteamQuote(game); const cdk = getCdkQuote(game, provider);
    return direct && cdk ? direct.price - cdk.price : null;
  }
  function sortGames(games, sort, provider) {
    const result = games.slice();
    const priceSort = (a, b, kind) => {
      const aq = kind === 'direct' ? getSteamQuote(a) : getCdkQuote(a, provider);
      const bq = kind === 'direct' ? getSteamQuote(b) : getCdkQuote(b, provider);
      return (aq ? aq.price : Infinity) - (bq ? bq.price : Infinity) || String(a.name || '').localeCompare(String(b.name || ''), 'zh-CN');
    };
    if (sort === 'direct' || sort === 'cdk') result.sort((a, b) => priceSort(a, b, sort));
    else if (sort === 'saving') result.sort((a, b) => (computeSavings(b, provider) ?? -Infinity) - (computeSavings(a, provider) ?? -Infinity));
    else if (sort === 'name') result.sort((a, b) => String(a.name || '').localeCompare(String(b.name || ''), 'zh-CN'));
    return result;
  }
  function currency(value) { return isPrice(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: 2, minimumFractionDigits: Number.isInteger(value) ? 0 : 2 }) : '—'; }
  function moneyNode(value, className) {
    const result = node('span', className || '');
    if (!isPrice(value)) { result.textContent = '暂无报价'; return result; }
    result.append(node('span', 'price-symbol', '¥'), document.createTextNode(currency(value)));
    return result;
  }
  function dateValue(value) { const date = new Date(value); return Number.isNaN(date.getTime()) ? null : date; }
  function formatDate(value, short) {
    const date = dateValue(value);
    if (!date) return '尚未更新';
    return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: short ? undefined : '2-digit', minute: short ? undefined : '2-digit', hour12: false }).format(date);
  }
  function timeAgo(value) {
    const date = dateValue(value);
    if (!date) return '时间未知';
    const minutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60000));
    if (minutes < 1) return '刚刚更新';
    if (minutes < 60) return `${minutes} 分钟前`;
    if (minutes < 1440) return `${Math.floor(minutes / 60)} 小时前`;
    return formatDate(value);
  }
  function validAppId(value) { if (typeof value !== 'number' && (typeof value !== 'string' || !/^\d+$/.test(value))) return false; const id = Number(value); return Number.isSafeInteger(id) && id > 0 && id <= 2147483647; }
  function readFavorites() {
    try { const parsed = JSON.parse(localStorage.getItem(FAVORITES_KEY) || '[]'); return new Set(Array.isArray(parsed) ? parsed.filter(validAppId).map(Number) : []); } catch (_) { return new Set(); }
  }
  function saveFavorites() {
    try { localStorage.setItem(FAVORITES_KEY, JSON.stringify([...state.favorites])); } catch (_) { toast('浏览器未允许保存关注；可导出清单保留。', true); }
  }
  function toast(message, error) {
    clearTimeout(state.toastTimer);
    const item = node('div', error ? 'toast error' : 'toast', message);
    $('toast-region').replaceChildren(item);
    state.toastTimer = setTimeout(() => $('toast-region').replaceChildren(), 4200);
  }
  function imageElement(url, alt, className, fallbackClass) {
    const source = safeUrl(url);
    const fallback = () => node('span', fallbackClass || 'thumbnail-fallback', 'GAME');
    if (!source) return fallback();
    const img = node('img', className); img.src = source; img.alt = alt; img.loading = 'lazy'; img.decoding = 'async';
    img.addEventListener('error', () => img.replaceWith(fallback()), { once: true });
    return img;
  }
  function currentGames() {
    const priced = state.data ? state.data.games : [];
    const ids = new Set(priced.map((game) => Number(game.appid)));
    const options = { favorites: state.favorites, favoritesOnly: state.favoritesOnly };
    const games = filterGames(priced, state.query, options);
    // The landing page stays focused on collected prices; search and favorites
    // also include the independently synchronized Steam directory.
    if (normalizeQuery(state.query) || state.favoritesOnly || !priced.length) {
      const term = normalizeQuery(state.query);
      state.catalogEntries.forEach((game) => {
        if (ids.has(game.appid) || (state.favoritesOnly && !state.favorites.has(game.appid))) return;
        if (!term || String(game.appid) === term || game.searchText.includes(term)) games.push(game);
      });
    }
    return sortGames(games, state.sort, state.provider);
  }
  function validateCatalog(data) {
    if (!data || data.schemaVersion !== 1 || !Array.isArray(data.games)) throw new Error('游戏目录格式不正确');
    const seen = new Set();
    return { ...data, games: data.games.filter((game) => {
      if (!game || !validAppId(game.appid) || seen.has(Number(game.appid)) || (game.type && game.type !== 'game')) return false;
      seen.add(Number(game.appid)); return typeof game.name === 'string' && game.name.trim();
    }) };
  }
  async function loadCatalog(manual) {
    if (state.catalogLoading) return;
    state.catalogLoading = true;
    try {
      let data;
      if (window.location.protocol === 'file:' && window.__CATALOG_DATA__) data = window.__CATALOG_DATA__;
      else {
        if (window.location.protocol === 'file:') throw new Error('本地预览尚未包含目录');
        const url = new URL('./data/catalog-index.json', window.location.href); if (manual) url.searchParams.set('t', String(Date.now()));
        const response = await fetch(url, { cache: 'no-cache', credentials: 'omit' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        data = await response.json();
      }
      state.catalog = validateCatalog(data);
      state.catalogEntries = state.catalog.games.map((game) => ({ ...game, appid: Number(game.appid), catalogOnly: true, quotes: [], history: [], searchText: [game.name, game.nameEn, ...(Array.isArray(game.aliases) ? game.aliases : [])].filter((text) => typeof text === 'string').join('\n').toLocaleLowerCase('zh-CN') }));
      state.catalogStatus = ['error', 'stale', 'unavailable'].includes(data.status) ? 'stale' : 'ok';
    } catch (_) { state.catalogStatus = state.catalog ? 'stale' : 'unavailable'; }
    finally {
      state.catalogLoading = false;
      if (state.data) { renderOverview(); renderGameList(); }
      else renderCatalogStatus();
    }
  }
  function renderCatalogStatus() {
    const count = state.catalogEntries.length;
    const priced = state.data ? state.data.games.length : 0;
    const status = $('catalog-status');
    status.textContent = count ? `${count.toLocaleString('zh-CN')} 款目录游戏 · ${priced} 款已采价${state.catalogStatus === 'stale' ? ' · 目录缓存' : state.catalog && state.catalog.complete === false ? ' · 目录正在补全' : ''}。输入名称或 AppID 搜索已同步目录。` : state.catalogStatus === 'loading' ? '正在载入 Steam 游戏目录…' : `目前可搜索 ${priced} 款已采价游戏，同步目录暂不可用。`;
    if (state.catalog && state.catalog.generatedAt) status.title = `目录同步于 ${formatDate(state.catalog.generatedAt)}（北京时间）。目录收录不代表已经取得价格。`;
  }
  function renderNav() {
    $('favorites-count').textContent = state.favorites.size;
    $('all-tab').classList.toggle('active', !state.favoritesOnly);
    $('favorites-tab').classList.toggle('active', state.favoritesOnly);
    $('all-tab').setAttribute('aria-pressed', String(!state.favoritesOnly));
    $('favorites-tab').setAttribute('aria-pressed', String(state.favoritesOnly));
    $('catalog-title').firstChild.textContent = state.favoritesOnly ? '我的游戏关注' : '发现下一款好游戏';
  }
  function renderGameList() {
    renderNav();
    const games = currentGames();
    const visible = games.slice(0, state.visibleLimit);
    $('visible-count').textContent = `${games.length.toLocaleString('zh-CN')} 款`;
    const more = $('catalog-more'); more.hidden = games.length <= visible.length;
    more.textContent = `再显示 ${Math.min(PAGE_SIZE, games.length - visible.length)} 款（已显示 ${visible.length} 款）`;
    renderCatalogStatus();
    const container = $('game-list'); container.replaceChildren(); container.setAttribute('aria-busy', 'false');
    if (!games.length) { renderEmpty(container); renderDetail(null); return; }
    if (!visible.some((game) => Number(game.appid) === state.selected)) state.selected = Number(visible[0].appid);
    visible.forEach((game) => {
      const selected = Number(game.appid) === state.selected;
      const row = node('button', `game-row${selected ? ' selected' : ''}`);
      row.type = 'button'; row.setAttribute('aria-pressed', String(selected));
      row.setAttribute('aria-label', `查看 ${game.name || game.nameEn || game.appid} 的价格详情`);
      row.dataset.appid = game.appid;
      const info = node('span', 'game-info');
      info.append(imageElement(game.image, '', 'game-thumbnail'));
      const copy = node('span', 'game-copy');
      const name = node('span', 'game-name', game.name || game.nameEn || `App ${game.appid}`);
      if (state.favorites.has(Number(game.appid))) { const marker = node('span', 'favorite-marker', '★'); marker.setAttribute('aria-label', '已关注'); name.append(marker); }
      copy.append(name, node('span', 'game-subtitle', game.nameEn || `Steam AppID ${game.appid}`), node('span', 'game-version', game.catalogOnly ? '目录收录 · 待采价' : game.edition || '标准版'));
      info.append(copy); row.append(info);
      const steam = getSteamQuote(game); const cdk = getCdkQuote(game, state.provider);
      const directPrice = node('span', `row-price${steam ? '' : ' is-missing'}`);
      directPrice.append(steam ? moneyNode(steam.price) : document.createTextNode('—'));
      const oldSteam = (game.quotes || []).some((quote) => quote.source === 'steam' && quote.kind === 'direct' && effectiveStatus(quote) === 'stale');
      directPrice.append(node('small', '', steam ? steam.dataProvider === 'heybox' ? '小黑盒 · 参考' : steam.price === 0 ? '免费游戏' : 'Steam 国区' : oldSteam ? '快照已过期' : game.catalogOnly ? '尚未采价' : '暂不可用'));
      const cdkPrice = node('span', `row-price cdk-price${cdk ? '' : ' is-missing'}`);
      cdkPrice.append(cdk ? moneyNode(cdk.price) : document.createTextNode('—'));
      const oldCdk = (game.quotes || []).some((quote) => quote.kind === 'cdk' && (state.provider === 'all' || quote.source === state.provider) && effectiveStatus(quote) === 'stale');
      cdkPrice.append(node('small', '', cdk ? `${ROW_PROVIDERS[cdk.source] || PROVIDERS[cdk.source]}${isPurchasable(cdk) ? ' · 可购' : ' · 参考'}` : oldCdk ? '快照已过期' : game.catalogOnly ? '尚未匹配' : '暂无有效报价'));
      row.append(directPrice, cdkPrice);
      row.addEventListener('click', () => {
        state.selected = Number(game.appid);
        container.querySelectorAll('.game-row').forEach((item) => { const active = Number(item.dataset.appid) === state.selected; item.classList.toggle('selected', active); item.setAttribute('aria-pressed', String(active)); });
        renderDetail(game);
        if (window.matchMedia('(max-width: 720px)').matches) $('game-detail').scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
      });
      container.append(row);
    });
    renderDetail(visible.find((game) => Number(game.appid) === state.selected));
  }
  function renderEmpty(container) {
    const empty = node('div', 'empty-state'); const emptyIcon = node('span', 'empty-icon'); emptyIcon.append(icon(state.favoritesOnly ? 'star' : 'search'));
    const hasQuery = Boolean(state.query.trim());
    empty.append(emptyIcon, node('h3', '', hasQuery ? '这款游戏暂未收录' : state.favoritesOnly ? '还没有可显示的关注' : '还没有价格快照'));
    empty.append(node('p', '', hasQuery ? `没有找到“${state.query.trim()}”。可尝试英文名或 AppID；目录同步和价格采集会持续补充。` : state.favoritesOnly ? '在游戏详情中点亮星标即可关注。已导入但尚未收录的 AppID，会在目录同步后显示。' : '价格采集完成后，真实报价会显示在这里。'));
    const links = node('div', 'empty-links');
    if (hasQuery) {
      const term = normalizeQuery(state.query);
      const steam = /^\d+$/.test(term) ? `https://store.steampowered.com/app/${term}/?cc=cn&l=schinese` : `https://store.steampowered.com/search/?term=${encodeURIComponent(state.query.trim())}&cc=cn&l=schinese`;
      const steamdb = /^\d+$/.test(term) ? `https://steamdb.info/app/${term}/` : `https://steamdb.info/search/?a=app&q=${encodeURIComponent(state.query.trim())}`;
      links.append(externalLink('去 Steam 查找 ↗', steam, 'button button-secondary'), externalLink('去 SteamDB 查找 ↗', steamdb, 'button button-quiet'));
    } else if (state.favoritesOnly) {
      const button = node('button', 'button button-secondary', '浏览全部游戏'); button.type = 'button'; button.addEventListener('click', () => { state.favoritesOnly = false; renderGameList(); }); links.append(button);
    }
    empty.append(links); container.append(empty);
  }
  function priceCard(label, quote, kind) {
    const isCdk = kind === 'cdk'; const card = node('div', `price-card${isCdk ? ' cdk-card' : ''}`);
    const heading = node('div', 'price-card-label'); heading.append(icon(isCdk ? 'tag' : 'store'), document.createTextNode(label)); card.append(heading);
    if (quote) card.append(moneyNode(quote.price, 'price-number'));
    else card.append(node('div', 'price-number no-price', '暂无报价'));
    const available = isPurchasable(quote);
    let note = isCdk ? quote ? available ? '已确认在售 · 可购报价' : '参考报价 · 库存未确认' : '三家平台暂无有效报价' : quote ? quote.price === 0 ? '免费游戏 · Steam 国区' : '官方商店 · 当前售价' : '暂无有效报价 · 请打开商店核对';
    if (quote && window.location.protocol === 'file:') note = isCdk ? '采集时参考 · 请核对库存/地区' : '采集时快照 · 不保证当前价';
    if (!isCdk && quote && quote.dataProvider === 'heybox') note = window.location.protocol === 'file:' ? '采集时小黑盒参考 · 非当前价' : '小黑盒国区参考 · 未核验当前价';
    if (isCdk && state.provider !== 'all' && !quote) note = `${PROVIDERS[state.provider]}暂无有效报价`;
    card.append(node('p', `price-card-note${isCdk && quote ? available ? ' verified' : ' warning' : ''}`, note));
    const foot = node('div', 'price-card-footer');
    if (quote) {
      if (!isCdk && isPrice(quote.originalPrice) && quote.originalPrice > quote.price) {
        foot.append(node('span', 'discount-badge', `-${Math.round((1 - quote.price / quote.originalPrice) * 100)}%`), node('span', 'original-price', `¥${currency(quote.originalPrice)}`));
      } else foot.append(node('span', '', isCdk ? PROVIDERS[quote.source] || quote.source : quote.dataProvider === 'heybox' ? '来源：小黑盒' : '人民币 · CNY'));
      const time = node('span', '', `· ${timeAgo(quote.checkedAt)}`); time.title = `${formatDate(quote.checkedAt)}（北京时间）`; foot.append(time);
    } else foot.append(node('span', '', '不使用缺货、异常或过期价格'));
    card.append(foot); return card;
  }
  function quoteStatusText(quote) {
    if (!quote) return '尚未取得报价';
    if (quote.stock === 'out_of_stock') return '当前缺货';
    if (quote.status === 'suspect') return '报价异常 · 不参与比价';
    if (effectiveStatus(quote) === 'stale') return '快照已过期 · 不参与比价';
    if (quote.status === 'error') return '接口暂不可用';
    if (quote.status === 'unavailable') return '暂无有效报价';
    if (!isPrice(quote.price)) return '暂无有效报价';
    return isPurchasable(quote) ? '库存已确认 · 可购' : '参考 · 库存未确认';
  }
  function renderQuoteRow(game, source, lowest) {
    const row = node('div', 'quote-row');
    const quotes = (game.quotes || []).filter((quote) => quote.source === source && quote.kind === 'cdk');
    const quote = getCdkQuote(game, source) || quotes[0] || null;
    const info = node('div', 'quote-provider');
    const logo = node('span', `provider-logo ${source}`, source === 'sonkwo' ? '杉' : source === 'steampy' ? 'PY' : 'C');
    const text = node('div'); const heading = node('strong', '', PROVIDERS[source]);
    if (lowest && quote === lowest) { const badge = node('span', '', ' · 最低'); badge.style.color = 'var(--mint)'; badge.style.fontSize = '8px'; heading.append(badge); }
    text.append(heading);
    const noteText = quote && quote.note ? quote.note : source === 'sonkwo' ? 'Steam 激活码 / 激活入库' : source === 'steampy' ? 'CDKey 报价，不含余额购与代购' : 'CDKey 参考报价，以商品页为准';
    const note = node('span', 'quote-note', noteText); note.title = noteText; text.append(note);
    if (quote && quote.checkedAt) { const updated = node('span', 'quote-note quote-updated', `检查于 ${formatDate(quote.checkedAt)}`); updated.style.color = '#536e80'; text.append(updated); }
    info.append(logo, text);
    const usable = isUsableQuote(quote);
    const price = node('div', `quote-price${usable ? '' : ' missing'}`);
    if (usable) price.append(moneyNode(quote.price));
    else if (quote && effectiveStatus(quote) === 'stale' && isPrice(quote.price)) price.append(document.createTextNode(`旧价 ¥${currency(quote.price)}`));
    else price.append(document.createTextNode('暂无报价'));
    const stock = node('small', usable ? isPurchasable(quote) ? 'available' : 'reference' : '', quoteStatusText(quote)); price.append(stock);
    if (quote && quote.checkedAt) price.title = `报价检查：${formatDate(quote.checkedAt)}（北京时间）`;
    const link = externalLink('', quote && quote.url, `external-button${safeUrl(quote && quote.url) ? '' : ' disabled'}`);
    link.append(icon('external'));
    if (link.tagName === 'A') {
      const label = source === 'steamcici' ? `打开 SteamCici 首页，请搜索 ${game.name || game.nameEn}` : `在 ${PROVIDERS[source]} 查看 ${game.name || game.nameEn}，新窗口打开`;
      link.setAttribute('aria-label', label); link.title = label;
    }
    else link.setAttribute('aria-label', `${PROVIDERS[source]} 暂无商品链接`);
    row.append(info, price, link); return row;
  }
  function normalizeHistoryPoints(history) {
    if (!history || history.country !== 'CN' || history.currency !== 'CNY' || !Array.isArray(history.points)) return [];
    const valid = history.points.filter((point) => point && point.date !== null && point.date !== undefined && isPrice(point.price)).map((point) => {
      const numeric = typeof point.date === 'number' ? point.date : /^\d{10,13}$/.test(String(point.date)) ? Number(point.date) : null;
      const parsed = dateValue(numeric === null ? point.date : numeric < 1e12 ? numeric * 1000 : numeric);
      return parsed ? { date: parsed.toISOString(), price: point.price, timestamp: parsed.getTime() } : null;
    }).filter(Boolean).sort((a, b) => a.timestamp - b.timestamp);
    return [...new Map(valid.map((point) => [point.timestamp, point])).values()];
  }
  function selectHistoryRange(history, range) {
    const all = normalizeHistoryPoints(history);
    if (!all.length) return { all, points: [], start: null, end: null, lowest: null, recordedLowest: null, carried: false };
    const last = all[all.length - 1].timestamp;
    const fetched = dateValue(history.fetchedAt);
    const end = Math.max(last, fetched ? fetched.getTime() : last);
    const days = ['30', '90', '180'].includes(String(range)) ? Number(range) : null;
    const start = days ? Math.max(all[0].timestamp, end - days * 86400000) : all[0].timestamp;
    const points = all.filter((point) => point.timestamp >= start && point.timestamp <= end);
    // Carry the last genuine change before the range into its starting edge.
    // Do not extend a series with no record in the requested range to "today".
    const predecessor = days && points.length && points[0].timestamp > start ? all.filter((point) => point.timestamp < start).at(-1) : null;
    if (predecessor) points.unshift(predecessor);
    return { all, points, start, end, lowest: points.length ? Math.min(...points.map((point) => point.price)) : null, recordedLowest: Math.min(...all.map((point) => point.price)), carried: Boolean(predecessor) };
  }
  function historyDate(value, withYear) {
    const date = dateValue(value);
    return date ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: withYear ? 'numeric' : undefined, month: '2-digit', day: '2-digit' }).format(date) : '未知日期';
  }
  function svgNode(tag, attributes, text) {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attributes || {}).forEach(([key, value]) => element.setAttribute(key, String(value)));
    if (text !== undefined) element.textContent = String(text);
    return element;
  }
  function stepPath(coordinates) {
    return coordinates.length ? coordinates.reduce((path, point, index) => index ? `${path} H${point.x} V${point.y}` : `M${point.x} ${point.y}`, '') : '';
  }
  function makeSteamChart(selection, expanded, label) {
    const seriesLabel = label || 'Steam 国区';
    const mobile = window.matchMedia('(max-width: 720px)').matches;
    const width = expanded && !mobile ? 850 : mobile ? Math.max(250, window.innerWidth - 106) : window.innerWidth <= 1150 ? Math.max(280, Math.round(window.innerWidth / 2 - 76)) : 520;
    const height = expanded && !mobile ? 320 : 235;
    const margin = { left: mobile ? 42 : 49, right: 13, top: 23, bottom: 35 };
    const plotWidth = width - margin.left - margin.right; const plotHeight = height - margin.top - margin.bottom;
    const points = selection.points; const maxPrice = Math.max(...points.map((point) => point.price), 1);
    const unit = maxPrice > 100 ? 100 : maxPrice > 20 ? 20 : maxPrice > 5 ? 5 : 1;
    const axisMax = Math.max(unit, Math.ceil(maxPrice * 1.08 / unit) * unit);
    const start = selection.start; const end = Math.max(selection.end, start + 86400000);
    const x = (timestamp) => margin.left + (timestamp - start) / (end - start) * plotWidth;
    const y = (price) => margin.top + plotHeight - price / axisMax * plotHeight;
    const coordinates = points.map((point) => ({ ...point, carried: point.timestamp < start, x: x(Math.max(start, point.timestamp)), y: y(point.price) }));
    const chart = node('div', 'steam-history-chart');
    const svg = svgNode('svg', { viewBox: `0 0 ${width} ${height}`, role: 'group', tabindex: '0', 'aria-label': `${seriesLabel}价格走势图，${points.length} 条真实记录。使用左右方向键查看记录，Home 和 End 到首尾记录。`, class: 'steam-history-svg' });
    svg.append(svgNode('title', {}, `${seriesLabel}人民币价格走势；阶梯线仅连接来源提供的真实价格记录，截止末条记录。`));
    for (let index = 0; index <= 4; index += 1) {
      const value = axisMax * index / 4; const lineY = y(value);
      svg.append(svgNode('line', { x1: margin.left, y1: lineY, x2: width - margin.right, y2: lineY, class: 'history-grid' }));
      svg.append(svgNode('text', { x: margin.left - 8, y: lineY + 4, 'text-anchor': 'end', class: 'history-axis-label' }, `¥${currency(value)}`));
    }
    const ticks = mobile ? 2 : 3;
    for (let index = 0; index <= ticks; index += 1) {
      const timestamp = start + (end - start) * index / ticks; const tickX = x(timestamp);
      svg.append(svgNode('text', { x: tickX, y: height - 11, 'text-anchor': index === 0 ? 'start' : index === ticks ? 'end' : 'middle', class: 'history-axis-label' }, historyDate(timestamp, end - start > 365 * 86400000)));
    }
    const path = stepPath(coordinates); const baseline = y(0);
    svg.append(svgNode('path', { d: `${path} L${coordinates[coordinates.length - 1].x} ${baseline} L${coordinates[0].x} ${baseline} Z`, class: 'history-price-area' }));
    svg.append(svgNode('path', { d: path, class: 'history-price-line', 'vector-effect': 'non-scaling-stroke' }));
    coordinates.filter((point) => !point.carried).forEach((point) => svg.append(svgNode('circle', { cx: point.x, cy: point.y, r: 2.2, class: 'history-record-point' })));
    const cursor = svgNode('line', { x1: coordinates[0].x, y1: margin.top, x2: coordinates[0].x, y2: baseline, class: 'history-cursor', visibility: 'hidden' });
    const dot = svgNode('circle', { cx: coordinates[0].x, cy: coordinates[0].y, r: 4, class: 'history-active-point', visibility: 'hidden' }); svg.append(cursor, dot);
    const tooltip = node('div', 'history-tooltip'); tooltip.hidden = true; tooltip.setAttribute('role', 'status');
    const tooltipDate = node('span', 'history-tooltip-date'); const tooltipPrice = node('strong'); tooltip.append(tooltipDate, tooltipPrice);
    const live = node('span', 'sr-only'); live.setAttribute('aria-live', 'polite');
    let activeIndex = coordinates.length - 1;
    function show(index, announce) {
      activeIndex = Math.max(0, Math.min(coordinates.length - 1, index)); const point = coordinates[activeIndex];
      cursor.setAttribute('x1', point.x); cursor.setAttribute('x2', point.x); cursor.setAttribute('visibility', 'visible');
      dot.setAttribute('cx', point.x); dot.setAttribute('cy', point.y); dot.setAttribute('visibility', 'visible');
      tooltipDate.textContent = `${historyDate(point.date, true)}${point.carried ? ' · 起点前' : ''}`; tooltipPrice.textContent = `¥${currency(point.price)}`;
      tooltip.hidden = false;
      const chartWidth = chart.clientWidth || width; const halfTooltip = Math.min(tooltip.offsetWidth / 2 + 6, chartWidth / 2);
      tooltip.style.left = `${Math.min(chartWidth - halfTooltip, Math.max(halfTooltip, point.x / width * chartWidth))}px`;
      chart.dataset.activeDate = point.date; chart.dataset.activePrice = String(point.price);
      if (announce) live.textContent = `${historyDate(point.date, true)}，${seriesLabel}价格 ${currency(point.price)} 元${point.carried ? '，区间起点前最近一次价格记录' : ''}`;
    }
    function pointer(event) {
      const bounds = svg.getBoundingClientRect(); const pointerX = (event.clientX - bounds.left) / bounds.width * width;
      let nearest = 0; coordinates.forEach((point, index) => { if (Math.abs(point.x - pointerX) < Math.abs(coordinates[nearest].x - pointerX)) nearest = index; });
      show(nearest, event.type === 'pointerdown');
    }
    svg.addEventListener('pointermove', pointer); svg.addEventListener('pointerdown', pointer);
    svg.addEventListener('pointerleave', () => { if (document.activeElement !== svg) { tooltip.hidden = true; cursor.setAttribute('visibility', 'hidden'); dot.setAttribute('visibility', 'hidden'); } });
    svg.addEventListener('focus', () => show(activeIndex, true));
    svg.addEventListener('blur', () => { tooltip.hidden = true; cursor.setAttribute('visibility', 'hidden'); dot.setAttribute('visibility', 'hidden'); });
    svg.addEventListener('keydown', (event) => {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) { event.preventDefault(); show(event.key === 'Home' ? 0 : event.key === 'End' ? coordinates.length - 1 : activeIndex + (event.key === 'ArrowLeft' ? -1 : 1), true); }
      if (event.key === 'Escape') { tooltip.hidden = true; cursor.setAttribute('visibility', 'hidden'); dot.setAttribute('visibility', 'hidden'); }
    });
    chart.append(svg, tooltip, live); return chart;
  }
  function createSteamHistory(game, expanded) {
    const history = game.steamHistory || null;
    const sourceName = history && history.sourceName ? String(history.sourceName) : '未取得历史来源';
    const section = node('section', `steam-history-section${expanded ? ' expanded' : ''}`);
    section.dataset.historySource = history && history.source || 'unavailable';
    section.dataset.historyStatus = history && history.status || 'unavailable';
    const heading = node('div', 'steam-history-heading'); const title = node('div'); title.append(node('h3', '', 'Steam 国区价格走势'), node('span', 'history-subtitle', '直售历史 · 人民币 / CNY')); heading.append(title);
    if (!expanded) { const expand = node('button', 'history-expand text-button'); expand.type = 'button'; expand.dataset.historyExpand = game.appid; expand.append(icon('expand'), document.createTextNode('放大')); expand.setAttribute('aria-label', `放大查看 ${game.name || game.nameEn} 的 Steam 历史价格`); expand.addEventListener('click', () => openHistory(game)); heading.append(expand); }
    section.append(heading);
    const rangeGroup = node('div', 'history-range-group'); rangeGroup.setAttribute('role', 'group'); rangeGroup.setAttribute('aria-label', '选择历史价格时间区间');
    const body = node('div', 'steam-history-body');
    const defaultRange = state.historyRanges.get(Number(game.appid)) || 'all'; let selectedRange = defaultRange;
    const tabs = new Map();
    [['30', '近 30 天'], ['90', '近 90 天'], ['180', '近 180 天'], ['all', '全部记录']].forEach(([range, label]) => {
      const button = node('button', 'history-range', label); button.type = 'button'; button.dataset.historyRange = range; button.addEventListener('click', () => { selectedRange = range; state.historyRanges.set(Number(game.appid), range); update(); }); rangeGroup.append(button); tabs.set(range, button);
    });
    section.append(rangeGroup, body);
    function update() {
      tabs.forEach((button, range) => { button.classList.toggle('active', range === selectedRange); button.setAttribute('aria-pressed', String(range === selectedRange)); });
      body.replaceChildren(); const selection = selectHistoryRange(history, selectedRange); const steam = getSteamQuote(game);
      const metrics = node('div', 'history-metrics');
      [[steam && steam.dataProvider === 'heybox' ? '小黑盒参考价' : window.location.protocol === 'file:' ? '采集时直售' : '当前直售', steam ? steam.price : null], ['所选区间最低', selection.lowest], ['收录区间最低', selection.recordedLowest]].forEach(([label, price], index) => { const metric = node('div', `history-metric${index === 2 ? ' is-low' : ''}`); metric.append(node('span', '', label), isPrice(price) ? moneyNode(price, 'history-metric-price') : node('strong', 'history-metric-price missing', '—')); metrics.append(metric); });
      body.append(metrics);
      if (selection.points.length >= 2) {
        body.append(makeSteamChart(selection, expanded));
        const legend = node('div', 'history-chart-footer'); const key = node('span', 'history-chart-key'); key.append(node('i'), document.createTextNode('Steam 国区直售')); legend.append(key, node('span', '', `${selection.points.length} 条真实价格记录${selection.carried ? ' · 含起点前一条' : ''}`)); body.append(legend);
        body.append(node('p', 'history-interaction-hint', '移入 / 轻触查看日期与价格；键盘聚焦后按 ← → 查看记录。'));
      } else {
        const empty = node('div', 'steam-history-unavailable'); const symbol = node('span', 'empty-icon'); symbol.append(icon('chart'));
        const hasAll = selection.all.length >= 2;
        empty.append(symbol, node('strong', '', hasAll ? '这个区间的价格记录不足' : selection.all.length === 1 ? '目前只有一条历史记录' : '历史价格暂不可用'));
        const unavailableReason = history && (history.country !== 'CN' || history.currency !== 'CNY') ? '来源未返回中国大陆人民币历史，未用其他地区或换算价格代替。' : history && history.note ? history.note : '尚未取得可验证的 Steam 国区历史序列；当前售价和第三方历史分开显示。';
        empty.append(node('p', '', hasAll ? '该区间内变价记录不足，请切换更长区间。未将最后价格延伸到今天。' : selection.all.length === 1 ? '来源仅返回一条真实记录，至少两条记录后展示走势。当前售价与该条记录仍可核对。' : unavailableReason));
        if (hasAll && selectedRange !== 'all') { const all = node('button', 'text-button', '查看全部真实记录 →'); all.type = 'button'; all.addEventListener('click', () => { selectedRange = 'all'; state.historyRanges.set(Number(game.appid), 'all'); update(); }); empty.append(all); }
        body.append(empty);
      }
      const provenance = node('div', 'history-provenance');
      const source = externalLink(`历史来源：${sourceName}${safeUrl(history && history.sourceUrl) ? ' ↗' : ''}`, history && history.sourceUrl, 'history-source-link'); provenance.append(source);
      if (selection.all.length) provenance.append(node('span', '', `收录 ${historyDate(selection.all[0].date, true)} — ${historyDate(selection.all[selection.all.length - 1].date, true)}`));
      if (history && history.fetchedAt) provenance.append(node('span', '', `获取于 ${formatDate(history.fetchedAt)}（北京时间）${history.status && history.status !== 'ok' ? ' · 历史缓存' : ''}`));
      provenance.append(node('p', '', `最低价仅指来源返回的记录区间，不代表全历史最低价。曲线截止末条真实记录，不混入 CDK 报价。${selection.carried ? '区间起点承接起点前最近一次变价，提示保留原记录日期。' : ''}`));
      if (history && history.note && selection.points.length >= 2) provenance.append(node('p', 'history-source-note', history.note));
      provenance.append(externalLink('SteamDB 历史对照 ↗', game.steamdbUrl || `https://steamdb.info/app/${game.appid}/`, 'history-compare-link'));
      body.append(provenance);
    }
    update(); return section;
  }
  function openHistory(game) {
    state.historyReturn = document.activeElement;
    $('history-dialog-title').textContent = `${game.name || game.nameEn} · Steam 价格走势`;
    $('history-dialog-content').replaceChildren(createSteamHistory(game, true));
    if (!$('history-dialog').open) $('history-dialog').showModal();
  }
  function renderHistory(game, container) {
    const section = node('section', 'history-section cdk-local-history'); const heading = node('div', 'section-heading'); heading.append(node('h3', '', '本站 CDK 可购记录'), node('span', '', '仅本站开始采集以来')); section.append(heading);
    const points = (Array.isArray(game.history) ? game.history : []).filter((point) => point && point.date && dateValue(point.date) && isPrice(point.cdk)).sort((a, b) => dateValue(a.date) - dateValue(b.date));
    const unique = [...new Map(points.map((point) => [dateValue(point.date).toISOString(), point])).values()];
    if (unique.length < 2) { const empty = node('div', 'history-empty'); const copy = node('div'); copy.append(node('p', '', '尚无足够的可购 CDK 日记录'), node('small', '', '库存未知参考价不写入历史 · 至少两个采集日期后展示')); empty.append(icon('chart'), copy); section.append(empty); }
    else {
      const history = { country: 'CN', currency: 'CNY', fetchedAt: unique[unique.length - 1].date, points: unique.map((point) => ({ date: point.date, price: point.cdk })) };
      const chart = makeSteamChart(selectHistoryRange(history, 'all'), false, '本站可购 CDK'); chart.classList.add('local-cdk-chart'); chart.querySelector('svg').setAttribute('aria-label', '本站每日已核验可购 CDK 最低价记录，使用方向键查看日期与价格。'); section.append(chart);
      section.append(node('p', 'history-interaction-hint', '仅已核验可购报价的每日最低记录，不代表平台全历史。'));
    }
    container.append(section);
  }
  function contentOptions(game) {
    const standard = { id: 'base', name: game.edition || '标准版', nameZh: game.edition || '标准版', isStandard: true, kind: 'base', quotes: game.quotes || [], steamUrl: game.steamUrl, appid: game.appid };
    const options = [standard]; const ids = new Set(['base']);
    (Array.isArray(game.editions) ? game.editions : []).forEach((edition) => {
      if (!edition || edition.isStandard || !Array.isArray(edition.quotes)) return;
      const id = String(edition.id || (edition.subid ? `sub-${edition.subid}` : ''));
      if (!id || ids.has(id)) return; ids.add(id);
      options.push({ ...edition, id, kind: edition.kind || 'package', name: edition.nameZh || edition.name || '其他版本', quotes: edition.quotes });
    });
    (Array.isArray(game.dlc) ? game.dlc : []).forEach((dlc) => {
      if (!dlc || !validAppId(dlc.appid)) return;
      const id = `app-${Number(dlc.appid)}`; if (ids.has(id)) return; ids.add(id);
      options.push({ ...dlc, id, kind: 'dlc', name: dlc.nameZh || dlc.name || `DLC ${dlc.appid}`, quotes: Array.isArray(dlc.quotes) ? dlc.quotes : [] });
    });
    return options;
  }
  function selectedContent(game) {
    const options = contentOptions(game);
    return options.find((item) => item.id === state.contentSelections.get(Number(game.appid))) || options[0];
  }
  function createContentSelector(game, current) {
    const options = contentOptions(game); const section = node('section', 'content-selector');
    const heading = node('div', 'content-selector-heading');
    const label = node('label', '', '查看版本 / 附加内容'); label.htmlFor = 'content-select';
    heading.append(label, node('span', '', `${options.filter((item) => item.kind !== 'dlc').length} 个版本 · ${options.filter((item) => item.kind === 'dlc').length} 个 DLC`));
    const select = node('select', 'content-select'); select.id = 'content-select';
    [['游戏版本', options.filter((item) => item.kind !== 'dlc')], ['附加内容 / DLC', options.filter((item) => item.kind === 'dlc')]].forEach(([name, entries]) => {
      if (!entries.length) return;
      const group = node('optgroup'); group.label = name;
      entries.forEach((item) => { const option = node('option', '', item.nameZh || item.name); option.value = item.id; option.selected = item.id === current.id; group.append(option); }); select.append(group);
    });
    select.addEventListener('change', () => { state.contentSelections.set(Number(game.appid), select.value); renderDetail(game); $('content-select').focus({ preventScroll: true }); });
    section.append(heading, select);
    if (current.kind === 'dlc') section.append(node('p', 'selected-content-note', current.contentType === 'music' ? '以下只比较这一项原声带；使用条件以 Steam 商店说明为准。' : current.metadataVerified === false ? '这项附加内容的资料正在同步，核验完成后显示对应价格。' : 'DLC 需要拥有游戏本体；以下只比较这一项附加内容。'));
    else if (!current.isStandard) section.append(node('p', 'selected-content-note', current.note || '以下报价对应所选版本，请核对所含游戏与附加内容。'));
    const details = node('details', 'edition-prices'); details.open = state.contentListOpen.get(Number(game.appid)) === true;
    const summary = node('summary', '', '展开各版本与 DLC 价格'); details.append(summary);
    const labels = node('div', 'edition-price-labels'); labels.append(node('span', '', '内容'), node('span', '', 'Steam 直售'), node('span', '', 'CDK 报价')); details.append(labels);
    const list = node('div', 'edition-price-list');
    options.forEach((item) => {
      const button = node('button', `edition-price-row${item.id === current.id ? ' selected' : ''}`); button.type = 'button'; button.dataset.contentId = item.id;
      button.setAttribute('aria-pressed', String(item.id === current.id)); button.setAttribute('aria-label', `查看 ${item.nameZh || item.name} 的价格`);
      const name = node('span', 'edition-content-name'); name.append(node('span', '', item.nameZh || item.name), node('small', '', item.kind === 'dlc' ? item.contentType === 'music' ? '原声带' : item.metadataVerified === false ? '附加内容 · 待核验' : 'DLC · 需本体' : item.kind === 'bundle' ? '捆绑包' : item.isStandard ? '本体标准版' : '游戏版本'));
      const direct = getSteamQuote(item); const cdk = getCdkQuote(item, state.provider);
      button.append(name, moneyNode(direct && direct.price, 'edition-direct-price'), moneyNode(cdk && cdk.price, 'edition-cdk-price'));
      button.addEventListener('click', () => { state.contentSelections.set(Number(game.appid), item.id); state.contentListOpen.set(Number(game.appid), true); renderDetail(game); $('content-select').focus({ preventScroll: true }); }); list.append(button);
    });
    details.append(list); details.addEventListener('toggle', () => state.contentListOpen.set(Number(game.appid), details.open)); section.append(details);
    const contentStatus = game.contentStatus;
    if (contentStatus && (typeof contentStatus === 'string' ? contentStatus !== 'ok' : [contentStatus.editions, contentStatus.dlc].some((status) => status && status !== 'ok'))) section.append(node('p', 'content-coverage-note', typeof contentStatus === 'object' && contentStatus.note ? contentStatus.note : '版本或 DLC 信息尚未完整取得，已获取的内容列在上方。'));
    return section;
  }
  function renderDetail(game) {
    const container = $('game-detail'); container.replaceChildren();
    if (!game) {
      const placeholder = node('div', 'detail-placeholder'); const radar = node('span', 'placeholder-radar'); radar.setAttribute('aria-hidden', 'true'); radar.append(node('span'));
      placeholder.append(radar, node('h2', '', state.query ? '发现更多游戏' : '下一份好价，值得关注'), node('p', '', state.query ? '搜索已收录游戏，查看对应版本的直售和 CDK 报价。' : '选择一款游戏，查看各平台价格与库存状态。')); container.append(placeholder); return;
    }
    const current = selectedContent(game);
    const steam = getSteamQuote(current); const cdk = getCdkQuote(current, state.provider);
    const steamUrl = safeUrl(current.steamUrl || game.steamUrl) || `https://store.steampowered.com/app/${current.appid || game.appid}/?cc=cn&l=schinese`;
    const cover = node('div', 'detail-cover'); cover.append(imageElement(game.image, '', '', 'cover-fallback'));
    const badges = node('div', 'detail-cover-badges'); badges.append(node('span', 'tag', game.catalogOnly ? '目录收录' : current.kind === 'dlc' ? 'DLC' : current.isStandard ? game.edition || '标准版' : '其他版本'), node('span', 'tag', 'Steam 国区'));
    (Array.isArray(game.genres) ? game.genres : []).slice(0, 1).forEach((genre) => badges.append(node('span', 'tag', genre)));
    cover.append(badges);
    const favorite = node('button', `icon-button detail-favorite${state.favorites.has(Number(game.appid)) ? ' active' : ''}`); favorite.type = 'button'; favorite.setAttribute('aria-label', state.favorites.has(Number(game.appid)) ? '取消关注此游戏' : '关注此游戏'); favorite.setAttribute('aria-pressed', String(state.favorites.has(Number(game.appid)))); favorite.append(icon('star'));
    favorite.addEventListener('click', () => { const id = Number(game.appid); const wasFavorite = state.favorites.has(id); if (wasFavorite) state.favorites.delete(id); else state.favorites.add(id); saveFavorites(); renderGameList(); toast(wasFavorite ? '已取消关注' : '已关注，保存在当前浏览器'); }); cover.append(favorite); container.append(cover);
    const heading = node('div', 'detail-heading'); heading.append(node('h2', '', game.name || game.nameEn || `App ${game.appid}`));
    if (game.nameEn) heading.append(node('p', 'detail-name-en', game.nameEn));
    const meta = node('div', 'detail-meta'); meta.append(node('span', '', `AppID ${current.appid || game.appid}`), node('span', 'meta-separator'), node('span', '', game.catalogOnly ? '尚未采集价格' : current.isStandard ? game.edition || '标准版' : current.kind === 'dlc' ? '附加内容 / DLC' : current.kind === 'bundle' ? '捆绑包' : '游戏版本'), node('span', 'meta-separator')); const storeLink = externalLink('商店页面 ', steamUrl); storeLink.append(icon('external')); meta.append(storeLink); heading.append(meta); container.append(heading);
    const content = node('div', 'detail-content');
    if (game.catalogOnly) {
      const empty = node('div', 'catalog-price-pending'); empty.append(icon('clock'), node('h3', '', '已收录，尚未采集价格'), node('p', '', '这款游戏已进入可搜索目录。当前还没有可核对的现价、版本、DLC 或历史记录。'), externalLink('前往 Steam 查看当前价格 ', steamUrl, 'button button-secondary'));
      content.append(empty); container.append(content); return;
    }
    content.append(createContentSelector(game, current));
    const context = node('div', 'selected-content-heading'); context.append(node('span', '', current.kind === 'dlc' ? '当前附加内容' : '当前版本'), node('h3', '', current.nameZh || current.name)); content.append(context);
    const cards = node('div', 'price-cards'); cards.append(priceCard('Steam 国区直售价', steam, 'direct'), priceCard(state.provider === 'all' ? '最低 CDK 报价' : `${PROVIDERS[state.provider]} CDK 报价`, cdk, 'cdk')); content.append(cards);
    if (current.isStandard) content.append(createSteamHistory(game, false));
    else {
      const scope = node('div', 'content-history-note'); scope.append(icon('chart'), node('span', '', '所选内容暂未收录历史走势。'));
      const base = node('button', 'text-button', '查看标准版历史 →'); base.type = 'button'; base.addEventListener('click', () => { state.contentSelections.set(Number(game.appid), 'base'); renderDetail(game); }); scope.append(base); content.append(scope);
    }
    const section = node('section', 'quote-section'); const sectionHeading = node('div', 'section-heading'); sectionHeading.append(node('h3', '', 'CDK 平台报价'), node('span', '', '同版本 · 人民币')); section.append(sectionHeading);
    const list = node('div', 'quote-list'); CDK_PROVIDERS.forEach((source) => list.append(renderQuoteRow({ ...current, name: current.nameZh || current.name }, source, cdk))); section.append(list);
    const sourceTime = node('div', 'quote-source-time'); sourceTime.append(icon('clock'), node('span', '', `采集快照 ${formatDate(state.data.generatedAt)}（北京时间）· 各报价均标注检查时间`)); section.append(sourceTime); content.append(section);
    if (current.isStandard) renderHistory(game, content);
    const actions = node('div', 'detail-actions'); const direct = externalLink('前往 Steam 商店 ', steamUrl, 'button button-secondary'); direct.append(icon('external')); actions.append(direct);
    if (cdk && safeUrl(cdk.url)) { const buy = externalLink(isPurchasable(cdk) ? '查看最低 CDK 商品 ' : '核对 CDK 参考报价 ', cdk.url, 'button button-primary'); buy.append(icon('external')); actions.append(buy); }
    else { const help = node('button', 'button button-quiet', '了解报价说明'); help.type = 'button'; help.addEventListener('click', openHelp); actions.append(help); }
    content.append(actions, node('p', 'disclaimer', '跳转至对应商家 · 购买前请核对激活地区、版本及最终价格')); container.append(content);
  }
  function renderOverview() {
    const data = state.data;
    const catalogIds = new Set(state.catalogEntries.map((game) => game.appid));
    $('game-count').textContent = (state.catalogEntries.length + data.games.filter((game) => !catalogIds.has(Number(game.appid))).length).toLocaleString('zh-CN');
    $('catalog-coverage').textContent = `${data.games.length} 款已采价 · 搜索已同步目录`;
    renderCatalogStatus();
    const generated = dateValue(data.generatedAt); const minutes = generated ? Math.max(0, (Date.now() - generated.getTime()) / 60000) : Infinity;
    const old = minutes > Math.max(60, (Number(data.refreshMinutes) || 15) * 4);
    $('snapshot-dot').className = `status-dot${old ? ' warning' : ''}`;
    $('snapshot-label').textContent = window.location.protocol === 'file:' ? '采集时快照 · 仅供本地预览' : old ? '快照已过期 · 已排除最低价' : '价格快照已同步';
    $('snapshot-time').textContent = `${formatDate(data.generatedAt)} · ${window.location.protocol === 'file:' ? '本地预览快照' : `计划每 ${Number(data.refreshMinutes) || 15} 分钟更新`}`;
    $('snapshot-time').title = '北京时间（UTC+8）。定时采集可能因调度或商家接口延迟。';
    const statuses = $('source-statuses'); statuses.replaceChildren();
    ['steam', ...CDK_PROVIDERS].forEach((source) => {
      const info = (data.sources || []).find((item) => item.id === source);
      const sourceExpired = info && isQuoteExpired({ checkedAt: info.checkedAt });
      const healthy = info && ['ok', 'partial'].includes(info.status) && !sourceExpired;
      const item = node('span', 'source-item'); const dot = node('span', `status-dot${healthy ? info.status === 'partial' ? ' warning' : '' : info && info.status === 'error' ? ' error' : ' warning'}`);
      item.append(dot, document.createTextNode(PROVIDERS[source]));
      const message = info && info.message ? info.message : healthy ? '已检查数据源' : '暂未获取有效数据';
      item.title = `${PROVIDERS[source]}：${sourceExpired ? '快照已过期 · ' : ''}${message}${info && info.checkedAt ? `（${formatDate(info.checkedAt)}）` : ''}`;
      item.setAttribute('aria-label', item.title); statuses.append(item);
    });
  }
  function validateData(data) {
    if (!data || data.schemaVersion !== 1 || !Array.isArray(data.games)) throw new Error('价格快照格式不正确');
    data.games = data.games.filter((game) => game && validAppId(game.appid)).map((game) => ({ ...game, appid: Number(game.appid), quotes: Array.isArray(game.quotes) ? game.quotes : [], history: Array.isArray(game.history) ? game.history : [] }));
    return data;
  }
  function applyTheme(theme, persist) {
    const value = theme === 'light' ? 'light' : 'dark';
    document.documentElement.dataset.theme = value;
    const button = $('theme-toggle'); button.replaceChildren(icon(value === 'light' ? 'moon' : 'sun'));
    button.setAttribute('aria-label', value === 'light' ? '切换到黑夜主题' : '切换到白天主题');
    button.setAttribute('title', value === 'light' ? '切换到黑夜主题' : '切换到白天主题');
    button.setAttribute('aria-pressed', String(value === 'light'));
    const meta = document.querySelector('meta[name="theme-color"]'); if (meta) meta.content = value === 'light' ? '#f3f6f8' : '#0c121b';
    if (persist) { try { localStorage.setItem(THEME_KEY, value); } catch (_) { /* Theme still applies when storage is unavailable. */ } }
  }
  async function loadData(manual) {
    if (state.loading) return;
    state.loading = true; const button = $('refresh-button'); button.classList.add('is-loading'); button.disabled = true;
    try {
      if (window.location.protocol === 'file:' && window.PRICE_RADAR_BOOTSTRAP) {
        state.data = validateData(window.PRICE_RADAR_BOOTSTRAP);
        if (manual) toast('这是本地采集快照；更新价格请运行采集脚本，再生成预览。');
      } else {
        const url = new URL('./data/prices.json', window.location.href); if (manual) url.searchParams.set('t', String(Date.now()));
        const response = await fetch(url, { cache: 'no-store', credentials: 'omit' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        state.data = validateData(await response.json());
        if (manual) toast('已重新读取发布的快照，未即时查询商家接口。');
      }
      state.expirySignature = getExpirySignature(); renderOverview(); renderGameList();
    } catch (error) {
      if (state.data) toast('快照读取失败，正在显示上一次成功读取的数据。', true);
      else {
        $('snapshot-label').textContent = '价格快照读取失败'; $('snapshot-time').textContent = window.location.protocol === 'file:' ? '请打开“打开预览.html”或通过本地服务访问' : '检查网络或价格数据文件后重试'; $('snapshot-dot').className = 'status-dot error';
        const empty = node('div', 'empty-state'); const symbol = node('span', 'empty-icon'); symbol.append(icon('alert'));
        empty.append(symbol, node('h3', '', '暂时无法载入价格'), node('p', '', window.location.protocol === 'file:' ? '浏览器限制直接读取本地 JSON。请打开自包含预览文件，或通过本地 HTTP 服务访问网站。' : '未能读取已发布的价格快照。没有用模拟报价代替真实数据。'));
        const links = node('div', 'empty-links'); const retry = node('button', 'button button-secondary', '重新读取'); retry.type = 'button'; retry.addEventListener('click', () => loadData(true)); links.append(retry); empty.append(links); $('game-list').replaceChildren(empty); $('game-list').setAttribute('aria-busy', 'false'); $('source-statuses').replaceChildren(node('span', '', '价格快照不可用')); renderDetail(null);
      }
      if (manual) toast(`读取失败：${error.message}`, true);
    } finally { state.loading = false; button.classList.remove('is-loading'); button.disabled = false; }
  }
  function openHelp() { state.dialogReturn = document.activeElement; const dialog = $('help-dialog'); if (!dialog.open) dialog.showModal(); }
  function closeHelp() { $('help-dialog').close(); }
  function exportFavorites() {
    const data = { schemaVersion: 1, type: 'game-price-radar-favorites', exportedAt: new Date().toISOString(), appids: [...state.favorites].sort((a, b) => a - b) };
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
    const a = node('a'); a.href = url; a.download = '游戏价签-关注清单.json'; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); toast(`已导出 ${state.favorites.size} 个关注 AppID`);
  }
  async function importFavorites(file) {
    if (!file) return;
    try {
      if (file.size > 1024 * 1024) throw new Error('文件超过 1 MB，请选择关注清单 JSON');
      const parsed = JSON.parse(await file.text()); const ids = Array.isArray(parsed) ? parsed : parsed && Array.isArray(parsed.appids) ? parsed.appids : null;
      if (!ids || ids.length > 10000 || !ids.every(validAppId)) throw new Error('清单需包含有效的 Steam AppID 数组');
      const previousSize = state.favorites.size; ids.forEach((id) => state.favorites.add(Number(id))); saveFavorites();
      if (state.data) renderGameList(); else renderNav();
      toast(`已添加 ${state.favorites.size - previousSize} 个本地关注；不会自动采集新游戏。`);
    } catch (error) { toast(`导入失败：${error.message}`, true); }
    finally { $('import-file').value = ''; }
  }
  function getExpirySignature() { return state.data ? state.data.games.map((game) => (game.quotes || []).map((quote) => effectiveStatus(quote)).join(',')).join('|') : ''; }
  function checkExpiry() { if (state.data) { const signature = getExpirySignature(); if (signature !== state.expirySignature) { state.expirySignature = signature; renderOverview(); renderGameList(); } } }
  function initialize() {
    let theme = document.documentElement.dataset.theme;
    try { theme = localStorage.getItem(THEME_KEY) || theme; } catch (_) { /* Use the system preference. */ }
    applyTheme(theme || (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'), false);
    $('theme-toggle').addEventListener('click', () => applyTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light', true));
    $('search-form').addEventListener('submit', (event) => event.preventDefault());
    $('search-input').addEventListener('input', (event) => { state.query = event.target.value; state.visibleLimit = PAGE_SIZE; clearTimeout(state.searchTimer); state.searchTimer = setTimeout(() => { if (state.data) renderGameList(); }, 100); });
    $('provider-select').addEventListener('change', (event) => { state.provider = event.target.value; state.visibleLimit = PAGE_SIZE; if (state.data) renderGameList(); });
    $('sort-select').addEventListener('change', (event) => { state.sort = event.target.value; state.visibleLimit = PAGE_SIZE; if (state.data) renderGameList(); });
    $('all-tab').addEventListener('click', () => { state.favoritesOnly = false; state.visibleLimit = PAGE_SIZE; if (state.data) renderGameList(); else renderNav(); });
    $('favorites-tab').addEventListener('click', () => { state.favoritesOnly = true; state.visibleLimit = PAGE_SIZE; if (state.data) renderGameList(); else renderNav(); });
    $('catalog-more').addEventListener('click', () => { state.visibleLimit += PAGE_SIZE; if (state.data) renderGameList(); });
    $('refresh-button').addEventListener('click', () => { loadData(true); loadCatalog(true); });
    ['help-button', 'method-button', 'privacy-button'].forEach((id) => $(id).addEventListener('click', openHelp));
    ['close-dialog', 'understood-button'].forEach((id) => $(id).addEventListener('click', closeHelp));
    $('help-dialog').addEventListener('click', (event) => { if (event.target === $('help-dialog')) { const box = $('help-dialog').getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) closeHelp(); } });
    $('help-dialog').addEventListener('close', () => { if (state.dialogReturn && state.dialogReturn.isConnected) state.dialogReturn.focus(); });
    $('close-history-dialog').addEventListener('click', () => $('history-dialog').close());
    $('history-dialog').addEventListener('close', () => { if (state.historyReturn && state.historyReturn.isConnected) state.historyReturn.focus(); });
    $('history-dialog').addEventListener('click', (event) => { if (event.target === $('history-dialog')) { const bounds = $('history-dialog').getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) $('history-dialog').close(); } });
    $('export-button').addEventListener('click', exportFavorites);
    $('import-button').addEventListener('click', () => $('import-file').click());
    $('import-file').addEventListener('change', (event) => importFavorites(event.target.files[0]));
    document.addEventListener('keydown', (event) => {
      if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey && !$('help-dialog').open && !$('history-dialog').open && !['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName) && !document.activeElement.isContentEditable) { event.preventDefault(); $('search-input').focus(); }
      if (event.key === 'Escape' && document.activeElement === $('search-input') && state.query) { clearTimeout(state.searchTimer); $('search-input').value = ''; state.query = ''; state.visibleLimit = PAGE_SIZE; if (state.data) renderGameList(); }
    });
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') checkExpiry(); });
    setInterval(checkExpiry, 60000);
    renderNav(); loadData(false); loadCatalog(false);
  }
  window.PriceRadar = Object.freeze({ isPrice, isUsableQuote, isPurchasable, isQuoteExpired, effectiveStatus, getSteamQuote, getCdkQuote, normalizeQuery, filterGames, sortGames, computeSavings, safeUrl, validateData, validateCatalog, validAppId, normalizeHistoryPoints, selectHistoryRange, stepPath, contentOptions, selectedContent });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, { once: true }); else initialize();
})();
