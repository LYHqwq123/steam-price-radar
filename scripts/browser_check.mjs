// Browser QA using the installed Chromium browser and its native DevTools API.
// No browser download or package installation is required.
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';

const root = resolve(fileURLToPath(new URL('../', import.meta.url)));
const qa = join(root, '.qa');
mkdirSync(qa, { recursive: true });
const executable = process.env.RADAR_BROWSER || [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
].find(existsSync);
if (!executable) throw new Error('No installed Chromium browser found. Set RADAR_BROWSER.');
const url = process.argv[2] || 'http://127.0.0.1:8765/';
const profile = join(qa, `browser-profile-${Date.now()}`);
mkdirSync(profile, { recursive: true });
const child = spawn(executable, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--disable-extensions', '--disable-background-networking', '--hide-scrollbars',
  '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank',
], { windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'] });
let browserErrors = '';
child.stderr.on('data', c => { browserErrors = (browserErrors + c.toString()).slice(-4000); });
let socket;
let counter = 0;
const pending = new Map();
const errors = [];
const interactionChecks = [];
function command(method, params = {}) {
  const id = ++counter;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 15000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const r = await command('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
  return r.result.value;
}
async function waitFor(expression, timeout = 15000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await evaluate(expression)) return;
    await delay(150);
  }
  throw new Error(`Condition not met: ${expression}`);
}
async function screenshot(name, width, height) {
  await command('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: width < 600 });
  await delay(350);
  const { data } = await command('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  writeFileSync(join(qa, name), Buffer.from(data, 'base64'));
}
async function check(name, expression) {
  if (!await evaluate(expression)) throw new Error(`Interaction failed: ${name}`);
  interactionChecks.push(name);
}
async function search(value) {
  await evaluate(`(() => { const input=document.getElementById('search-input'); input.value=${JSON.stringify(value)}; input.dispatchEvent(new Event('input',{bubbles:true})); })()`);
  // The full-directory search deliberately debounces input by 100 ms.
  await delay(180);
}
try {
  const portFile = join(profile, 'DevToolsActivePort');
  for (let i = 0; !existsSync(portFile) && i < 100; i++) await delay(100);
  if (!existsSync(portFile)) throw new Error(`Browser did not launch: ${browserErrors}`);
  const port = readFileSync(portFile, 'utf8').split(/\r?\n/)[0];
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const target = targets.find(t => t.type === 'page');
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { socket.addEventListener('open', res, { once: true }); socket.addEventListener('error', rej, { once: true }); });
  socket.addEventListener('message', ({ data }) => {
    const message = JSON.parse(data);
    if (message.id && pending.has(message.id)) {
      const p = pending.get(message.id); pending.delete(message.id); clearTimeout(p.timer);
      if (message.error) p.reject(new Error(JSON.stringify(message.error))); else p.resolve(message.result);
    } else if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails.text);
  });
  await command('Page.enable');
  await command('Runtime.enable');
  await command('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1050, deviceScaleFactor: 1, mobile: false });
  await command('Page.navigate', { url });
  await waitFor(`document.readyState === 'complete' && window.PriceRadar && document.querySelector('.game-row')`);
  await evaluate(`window.__qaInitialTheme=document.documentElement.dataset.theme;document.getElementById('theme-toggle').click()`);
  const savedTheme = await evaluate(`document.documentElement.dataset.theme`);
  await check('Day/night theme changes and persists the choice', `['light','dark'].includes(document.documentElement.dataset.theme) && document.documentElement.dataset.theme!==window.__qaInitialTheme && localStorage.getItem('game-price-radar.theme.v1')===document.documentElement.dataset.theme`);
  await command('Page.reload', {});
  await waitFor(`document.readyState==='complete' && window.PriceRadar && document.querySelector('.game-row')`);
  await check('Theme remains selected after reload', `document.documentElement.dataset.theme===${JSON.stringify(savedTheme)}`);
  await evaluate(`if(document.documentElement.dataset.theme!=='dark')document.getElementById('theme-toggle').click()`);
  await evaluate(`(async()=>{const values=await Promise.all([window.PRICE_RADAR_BOOTSTRAP || (await fetch('./data/prices.json')).json(),window.__CATALOG_DATA__ || (await fetch('./data/catalog-index.json')).json(),window.__DIRECT_PRICE_DATA__ || (await fetch('./data/direct-prices.json')).json()]);window.__qaData=values[0];window.__qaCatalog=values[1];window.__qaDirect=values[2];return true})()`);
  await waitFor(`!document.getElementById('catalog-status').textContent.includes('正在载入')`);
  await waitFor(`!document.getElementById('direct-price-time').textContent.includes('正在读取')`);
  await check('Synchronized directory separates Steam prices from complete comparisons', `window.__qaCatalog.games.length>window.__qaData.games.length && window.__qaCatalog.gameCount===window.__qaCatalog.games.length && document.getElementById('catalog-status').textContent.includes('款有有效 Steam 现价') && Number(document.getElementById('complete-price-count').textContent.replace(/,/g,''))===window.__qaData.games.length`);
  await check('Batch snapshot provides real CN prices for many directory games', `window.__qaDirect.source==='steam' && window.__qaDirect.country==='CN' && window.__qaDirect.currency==='CNY' && window.__qaDirect.games.filter(g=>g.status==='ok' && typeof g.price==='number' && Number.isFinite(g.price) && g.price>=0 && g.checkedAt).length>window.__qaData.games.length && Number(document.getElementById('direct-price-count').textContent.replace(/,/g,''))>window.__qaData.games.length`);
  await check('Missing check time cannot become a valid current basic quote', `(()=>{if(typeof PriceRadar.validateDirectData!=='function')return false;for(const checkedAt of [null,'','not-a-date']){try{const data=PriceRadar.validateDirectData({...window.__qaDirect,games:[{appid:386940,price:24.75,status:'ok',checkedAt}]});if(data.games.some(g=>g.status==='ok'))return false}catch(_){}}return true})()`);
  await check('Foreign currency basic snapshot is rejected', `(()=>{if(typeof PriceRadar.validateDirectData!=='function')return false;try{PriceRadar.validateDirectData({...window.__qaDirect,currency:'USD'});return false}catch(_){return true}})()`);
  await check('Widget URLs accept only numeric Steam AppIDs on the official origin', `typeof PriceRadar.steamWidgetUrl==='function' && PriceRadar.steamWidgetUrl(386940)==='https://store.steampowered.com/widget/386940/?cc=cn&l=schinese' && [null,true,0,-1,1.5,'386940/../../','https://evil.example','1?cc=us',2147483648].every(id=>PriceRadar.steamWidgetUrl(id)===null)`);
  await search('老头环');
  await check('Chinese alias search', `document.querySelectorAll('.game-row').length===1 && document.querySelector('.game-row').dataset.appid==='1245620'`);
  await search('2077');
  await check('Numeric game alias search', `document.querySelector('.game-row[data-appid="1091500"]')!==null`);
  await search('https://store.steampowered.com/app/2050650/');
  await check('Steam URL search', `document.querySelectorAll('.game-row').length===1 && document.querySelector('.game-row').dataset.appid==='2050650'`);
  await search('不存在的游戏 QA-92381');
  await check('Uncollected game empty state', `!document.querySelector('.game-row') && document.getElementById('game-list').innerText.includes('暂未收录')`);
  await search('');
  await check('Suspicious prices excluded', `window.__qaData.games.every(g=>{const q=PriceRadar.getCdkQuote(g,'all');return !q || !['suspect','stale','error','unavailable'].includes(q.status)})`);
  await check('Abnormal SteamPY amounts are hidden across every content quote', `window.__qaData.games.every(g=>[g,...(g.editions||[]),...(g.dlc||[])].every(item=>(item.quotes||[]).every(q=>q.source!=='steampy'||q.status!=='suspect'||q.price===null)))`);
  await check('Old online quotes expire; offline preview remains a snapshot', `PriceRadar.isQuoteExpired({checkedAt:new Date(Date.now()-61*60000).toISOString()})===(location.protocol!=='file:')`);
  await evaluate(`document.getElementById('provider-select').value='sonkwo';document.getElementById('provider-select').dispatchEvent(new Event('change'))`);
  await check('Provider filter changes the CDK comparison', `document.querySelector('.cdk-card .price-card-label').textContent.includes('杉果')`);
  await evaluate(`document.getElementById('provider-select').value='all';document.getElementById('provider-select').dispatchEvent(new Event('change'))`);
  await evaluate(`document.querySelector('.detail-favorite').click(); document.getElementById('favorites-tab').click()`);
  await check('Favorite filtering', `document.querySelectorAll('.game-row').length===1 && document.getElementById('favorites-count').textContent==='1'`);
  await evaluate(`document.querySelector('.detail-favorite').click(); document.getElementById('all-tab').click()`);
  await check('Favorite removal restores the bounded full candidate list', `document.getElementById('favorites-count').textContent==='0' && document.querySelectorAll('.game-row').length===Math.min(30,Number(document.getElementById('visible-count').textContent.replace(/[^0-9]/g,''))) && Number(document.getElementById('visible-count').textContent.replace(/[^0-9]/g,''))>window.__qaData.games.length`);
  await evaluate(`document.getElementById('sort-select').value='direct';document.getElementById('sort-select').dispatchEvent(new Event('change'))`);
  await check('Visible Steam price sort is ascending with missing prices last', `(()=>{const amounts=[...document.querySelectorAll('.game-row')].map(row=>{const amount=row.querySelector('.row-price:not(.cdk-price)');return amount.classList.contains('is-missing')?Infinity:Number(amount.firstElementChild.textContent.replace(/[^0-9.]/g,''))});return amounts.length===30&&amounts.every((amount,index)=>amount>=0&&(index===0||amount>=amounts[index-1]))})()`);
  const basicAppid = await evaluate(`(()=>{const ids=new Set(window.__qaData.games.map(g=>Number(g.appid))),catalogIds=new Set(window.__qaCatalog.games.map(g=>Number(g.appid)));const game=window.__qaDirect.games.find(g=>!ids.has(Number(g.appid))&&catalogIds.has(Number(g.appid))&&g.status==='ok'&&typeof g.price==='number'&&Number.isFinite(g.price)&&g.price>=0&&g.checkedAt&&!PriceRadar.isQuoteExpired(g));window.__qaBasic=game;return game?.appid})()`);
  if (!basicAppid) throw new Error('QA requires a fresh basic Steam price without full game details');
  await search(String(basicAppid));
  await evaluate(`document.querySelector('.game-row[data-appid="${basicAppid}"]').click()`);
  await check('Basic directory game displays its own exact Steam snapshot', `document.querySelector('#game-detail .detail-meta').textContent.includes('AppID ${basicAppid}') && Number(document.querySelector('#game-detail .basic-price-cards .price-number').textContent.replace(/[^0-9.]/g,''))===window.__qaBasic.price && document.querySelector('#game-detail .basic-price-cards .price-card-label').textContent.includes('Steam 国区本体现价')`);
  await check('Basic Steam price does not invent editions DLC CDK or history', `!document.querySelector('#game-detail .steam-history-section') && !document.querySelector('#game-detail .content-selector') && !document.querySelector('#game-detail .quote-section') && !document.querySelector('#game-detail .cdk-card') && document.querySelector('#game-detail .basic-pending-list').textContent.includes('DLC 资料待采集') && document.querySelector('#game-detail .basic-pending-list').textContent.includes('历史价格待采集')`);
  await evaluate(`document.querySelector('#game-detail .detail-heading').scrollIntoView({block:'start',behavior:'instant'})`);
  await screenshot('bulk-game.png', 1440, 1050);
  await evaluate(`document.getElementById('theme-toggle').click()`);
  await check('Basic Steam price remains correct in light theme', `document.documentElement.dataset.theme==='light' && Number(document.querySelector('#game-detail .basic-price-cards .price-number').textContent.replace(/[^0-9.]/g,''))===window.__qaBasic.price && document.documentElement.scrollWidth<=innerWidth`);
  await screenshot('bulk-game-light.png', 1440, 1050);
  await evaluate(`document.getElementById('theme-toggle').click()`);
  await search('386940');
  await evaluate(`document.querySelector('.game-row[data-appid="386940"]').click()`);
  await check('Ultimate Chicken Horse now has its own collected current Steam price', `(()=>{const game=window.__qaData.games.find(g=>Number(g.appid)===386940),batch=window.__qaDirect.games.find(g=>Number(g.appid)===386940),amount=document.querySelector('#game-detail .price-card:not(.cdk-card) .price-number');if(!game||!amount||amount.classList.contains('no-price'))return false;const quote=PriceRadar.getSteamQuote(game),expected=quote?quote.price:batch&&batch.status==='ok'&&!PriceRadar.isQuoteExpired(batch)?batch.price:null;return expected!==null&&Number(amount.textContent.replace(/[^0-9.]/g,''))===expected&&document.querySelector('#game-detail .detail-meta').textContent.includes('AppID 386940')&&document.querySelector('#game-detail .content-selector')!==null})()`);
  await check('Ultimate Chicken Horse history has its own real source or explicit failure', `(()=>{const game=window.__qaData.games.find(g=>Number(g.appid)===386940),history=game&&game.steamHistory;if(!history||history.source!=='heybox'||history.country!=='CN'||history.currency!=='CNY')return false;return ['unavailable','error'].includes(history.status)||(['ok','stale'].includes(history.status)&&history.points.length>0&&history.points.every(p=>typeof p.date==='string'&&Number.isFinite(p.price)&&p.price>=0))})()`);
  const pendingAppid = await evaluate(`(()=>{const ids=new Set(window.__qaData.games.map(g=>Number(g.appid))),prices=new Map(window.__qaDirect.games.map(g=>[Number(g.appid),g]));return window.__qaCatalog.games.find(g=>{const price=prices.get(Number(g.appid));return !ids.has(Number(g.appid))&&(!price||price.status!=='ok'||typeof price.price!=='number'||!Number.isFinite(price.price)||price.price<0||PriceRadar.isQuoteExpired(price))})?.appid})()`);
  if (!pendingAppid) throw new Error('QA requires a directory entry without a usable basic price');
  await search(String(pendingAppid));
  await evaluate(`document.querySelector('.game-row[data-appid="${pendingAppid}"]').click()`);
  await check('Unpriced directory game uses only its own official Steam widget', `document.querySelectorAll('#game-detail iframe').length===1 && document.querySelector('#game-detail iframe').src==='https://store.steampowered.com/widget/${pendingAppid}/?cc=cn&l=schinese' && document.querySelector('#game-detail iframe').title.includes('Steam 官方当前价格') && document.querySelector('#game-detail .detail-meta').textContent.includes('AppID ${pendingAppid}')`);
  await check('Widget fallback neither fabricates a price nor reuses previous details', `!document.querySelector('#game-detail .price-cards') && !document.querySelector('#game-detail .price-number') && !document.querySelector('#game-detail .steam-history-section') && !document.querySelector('#game-detail .content-selector') && !document.querySelector('#game-detail .quote-section') && document.querySelector('#game-detail .steam-widget-description').textContent.includes('不计入本站最低价或价格排序')`);
  await evaluate(`document.querySelector('#game-detail .steam-widget-section').scrollIntoView({block:'center',behavior:'instant'})`);
  await screenshot('widget.png', 1440, 1050);
  await evaluate(`document.getElementById('theme-toggle').click()`);
  await check('Official widget fallback has no desktop overflow in light theme', `document.documentElement.dataset.theme==='light' && document.documentElement.scrollWidth<=innerWidth && document.querySelector('#game-detail iframe').src==='https://store.steampowered.com/widget/${pendingAppid}/?cc=cn&l=schinese'`);
  await screenshot('widget-light.png', 1440, 1050);
  await command('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await evaluate(`document.querySelector('#game-detail .steam-widget-section').scrollIntoView({block:'start',behavior:'instant'})`);
  await check('390px light widget fallback has no horizontal overflow', `document.documentElement.dataset.theme==='light' && document.documentElement.scrollWidth<=innerWidth && document.querySelector('#game-detail .steam-widget-viewport').getBoundingClientRect().right<=innerWidth`);
  await screenshot('mobile-widget-light.png', 390, 844);
  await evaluate(`document.getElementById('theme-toggle').click()`);
  await check('390px dark widget fallback has no horizontal overflow', `document.documentElement.dataset.theme==='dark' && document.documentElement.scrollWidth<=innerWidth && document.querySelector('#game-detail .steam-widget-viewport').getBoundingClientRect().right<=innerWidth`);
  await screenshot('mobile-widget.png', 390, 844);
  await command('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1050, deviceScaleFactor: 1, mobile: false });
  const broadQuery = await evaluate(`['the','a','2','simulator'].find(term=>window.__qaCatalog.games.filter(g=>[g.name,g.nameEn,...(g.aliases||[])].filter(Boolean).join(' ').toLowerCase().includes(term)).length>60)`);
  if (!broadQuery) throw new Error('QA requires a directory query matching more than 60 games');
  await search(broadQuery);
  await check('Large directory search initially renders only thirty rows', `document.querySelectorAll('.game-row').length===30 && !document.getElementById('catalog-more').hidden`);
  const firstPageIds = await evaluate(`[...document.querySelectorAll('.game-row')].map(e=>e.dataset.appid)`);
  await evaluate(`document.getElementById('catalog-more').click()`);
  await check('Directory results continue in bounded pages', `document.querySelectorAll('.game-row').length===60 && JSON.stringify([...document.querySelectorAll('.game-row')].slice(0,30).map(e=>e.dataset.appid))===${JSON.stringify(JSON.stringify(firstPageIds))}`);
  await search('');
  await evaluate(`document.getElementById('sort-select').value='recommended';document.getElementById('sort-select').dispatchEvent(new Event('change'));document.querySelector('.game-row[data-appid="1245620"]').click()`);
  await evaluate(`document.getElementById('help-button').click()`);
  await check('Help dialog opens', `document.getElementById('help-dialog').open`);
  await command('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  await command('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  await waitFor(`!document.getElementById('help-dialog').open`);
  interactionChecks.push('Escape closes help');
  await evaluate(`(() => { const dt=new DataTransfer();dt.items.add(new File(['{"appids":[true]}'],'invalid.json',{type:'application/json'}));const el=document.getElementById('import-file');el.files=dt.files;el.dispatchEvent(new Event('change')); })()`);
  await waitFor(`document.getElementById('toast-region').innerText.includes('导入失败')`);
  await check('Invalid favorite import rejected', `document.getElementById('favorites-count').textContent==='0'`);
  await evaluate(`document.getElementById('refresh-button').click()`);
  await waitFor(`!document.getElementById('refresh-button').disabled`);
  await check('Snapshot refresh is explicit', `document.getElementById('toast-region').innerText.includes(location.protocol==='file:'?'本地':'快照')`);
  await check('Collected history is real CN data or explicitly unavailable', `window.__qaData.games.every(g=>{const h=g.steamHistory;return h && h.country==='CN' && h.currency==='CNY' && (['unavailable','error'].includes(h.status)||(['ok','stale'].includes(h.status)&&Array.isArray(h.points)&&h.points.every(p=>typeof p.date==='string'&&Number.isFinite(p.price)&&p.price>=0)))})`);
  await check('Elden Ring has the actual historical source records', `window.__qaData.games.find(g=>Number(g.appid)===1245620).steamHistory.points.length>=2`);
  await evaluate(`document.getElementById('sort-select').value='recommended';document.getElementById('sort-select').dispatchEvent(new Event('change'));document.querySelector('.game-row[data-appid="1245620"]').click();document.querySelector('#game-detail [data-history-range="all"]').click()`);
  await check('Full history draws the actual source records', `document.querySelectorAll('#game-detail .history-record-point').length===window.__qaData.games.find(g=>g.appid===1245620).steamHistory.points.length && document.querySelector('#game-detail .history-price-line').getAttribute('d').includes('H')`);
  await check('Recorded low comes from Steam history, not CDK prices', `Number(document.querySelector('#game-detail .history-metric.is-low .history-metric-price').textContent.replace(/[^0-9.]/g,''))===Math.min(...window.__qaData.games.find(g=>g.appid===1245620).steamHistory.points.map(p=>p.price))`);
  await evaluate(`document.querySelector('#game-detail .steam-history-svg').focus()`);
  await command('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Home', code: 'Home', windowsVirtualKeyCode: 36 });
  await command('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Home', code: 'Home', windowsVirtualKeyCode: 36 });
  await check('Keyboard exposes a real historical date and price', `!document.querySelector('#game-detail .history-tooltip').hidden && document.querySelector('#game-detail .steam-history-chart').dataset.activeDate.slice(0,10)===window.__qaData.games.find(g=>g.appid===1245620).steamHistory.points[0].date`);
  await evaluate(`document.querySelector('#game-detail .steam-history-svg').scrollIntoView({block:'center',behavior:'instant'})`);
  const chartBox = await evaluate(`(()=>{const r=document.querySelector('#game-detail .steam-history-svg').getBoundingClientRect();return {x:r.left+r.width*.6,y:r.top+r.height*.5}})()`);
  await command('Input.dispatchMouseEvent', { type: 'mouseMoved', x: chartBox.x, y: chartBox.y });
  await check('Pointer tooltip shows source price', `!document.querySelector('#game-detail .history-tooltip').hidden && window.__qaData.games.find(g=>g.appid===1245620).steamHistory.points.some(p=>p.price===Number(document.querySelector('#game-detail .steam-history-chart').dataset.activePrice))`);
  await evaluate(`document.querySelector('#game-detail [data-history-range="30"]').click()`);
  await check('Thirty-day history range is selectable', `document.querySelector('#game-detail [data-history-range="30"]').getAttribute('aria-pressed')==='true' && document.querySelector('#game-detail .steam-history-svg')!==null`);
  await evaluate(`document.querySelector('#game-detail .history-expand').click()`);
  await check('Enlarged history is interactive', `document.getElementById('history-dialog').open && document.querySelector('#history-dialog .steam-history-svg')!==null`);
  await evaluate(`document.querySelector('#history-dialog [data-history-range="all"]').click()`);
  await screenshot('history-expanded.png', 1440, 1050);
  await evaluate(`document.getElementById('close-history-dialog').click()`);
  await check('Enlarged history closes', `!document.getElementById('history-dialog').open`);
  // Each selected product must drive its own price cards and historical scope.
  await evaluate(`(()=>{const game=window.__qaData.games.find(g=>Number(g.appid)===1245620);window.__qaEdition=(game.editions||[]).find(e=>!e.isStandard&&e.subid&&/豪华|Deluxe|Ultimate|终极/i.test((e.nameZh||'')+' '+(e.name||''))&&PriceRadar.getSteamQuote(e));if(!window.__qaEdition)throw new Error('Missing priced deluxe package');const select=document.getElementById('content-select');select.value=window.__qaEdition.id;select.dispatchEvent(new Event('change'));})()`);
  await check('Deluxe prices follow the exact selected SubID', `document.getElementById('content-select').value===window.__qaEdition.id && Number(document.querySelector('#game-detail .price-card:not(.cdk-card) .price-number').textContent.replace(/[^0-9.]/g,''))===PriceRadar.getSteamQuote(window.__qaEdition).price && document.querySelector('.edition-price-row[data-content-id="'+window.__qaEdition.id+'"]').getAttribute('aria-pressed')==='true' && !document.querySelector('#game-detail .steam-history-section')`);
  await check('Deluxe CDK comparison uses only that package quotes', `(()=>{const q=PriceRadar.getCdkQuote(window.__qaEdition,'all');const amount=document.querySelector('#game-detail .cdk-card .price-number');return q?Number(amount.textContent.replace(/[^0-9.]/g,''))===q.price:amount.classList.contains('no-price')})()`);
  await evaluate(`document.querySelector('#game-detail .content-selector').scrollIntoView({block:'start',behavior:'instant'})`);
  await screenshot('editions.png', 1440, 1050);
  await evaluate(`(()=>{const game=window.__qaData.games.find(g=>Number(g.appid)===1245620);window.__qaDlc=(game.dlc||[]).find(e=>PriceRadar.getSteamQuote(e)&&PriceRadar.getSteamQuote(e).price!==PriceRadar.getSteamQuote(game).price);if(!window.__qaDlc)throw new Error('Missing separately priced DLC');const select=document.getElementById('content-select');select.value='app-'+window.__qaDlc.appid;select.dispatchEvent(new Event('change'));})()`);
  await check('DLC price and AppID follow the selected additional content', `document.getElementById('content-select').value==='app-'+window.__qaDlc.appid && document.querySelector('#game-detail .detail-meta').textContent.includes('AppID '+window.__qaDlc.appid) && Number(document.querySelector('#game-detail .price-card:not(.cdk-card) .price-number').textContent.replace(/[^0-9.]/g,''))===PriceRadar.getSteamQuote(window.__qaDlc).price`);
  await check('DLC does not reuse the base game historical chart', `!document.querySelector('#game-detail .steam-history-section') && document.querySelector('#game-detail .content-history-note').textContent.includes('暂未收录历史')`);
  await evaluate(`document.querySelector('#game-detail .content-selector').scrollIntoView({block:'start',behavior:'instant'})`);
  await screenshot('dlc.png', 1440, 1050);
  await evaluate(`(()=>{const select=document.getElementById('content-select');select.value='base';select.dispatchEvent(new Event('change'));document.querySelector('#game-detail [data-history-range="all"]').click()})()`);
  await check('Returning to the base game restores its historical records', `document.getElementById('content-select').value==='base' && document.querySelectorAll('#game-detail .history-record-point').length===window.__qaData.games.find(g=>Number(g.appid)===1245620).steamHistory.points.length`);
  // Exercise every hidden abnormal PY content entry, including DLC/editions.
  const suspiciousSelections = await evaluate(`window.__qaData.games.flatMap(g=>PriceRadar.contentOptions(g).filter(item=>(item.quotes||[]).some(q=>q.source==='steampy'&&q.status==='suspect')).map(item=>({appid:g.appid,id:item.id})))`);
  for (const item of suspiciousSelections) {
    await search(String(item.appid));
    await evaluate(`(()=>{document.querySelector('.game-row[data-appid="${item.appid}"]').click();const s=document.getElementById('content-select');s.value=${JSON.stringify(item.id)};s.dispatchEvent(new Event('change'))})()`);
    await check(`Abnormal PY quote hidden for ${item.appid}/${item.id}`, `(()=>{const row=[...document.querySelectorAll('#game-detail .quote-row')].find(e=>e.textContent.includes('SteamPY'));return row&&!row.querySelector('.quote-price').textContent.includes('¥')})()`);
  }
  // Restore the ordinary view, including fresh local storage, before screenshots.
  await evaluate(`localStorage.removeItem('game-price-radar.favorites.v1')`);
  await command('Page.reload', {});
  await waitFor(`document.querySelectorAll('.game-row').length>0`);
  await delay(2500);
  await search('1245620');
  await evaluate(`(()=>{document.querySelector('.game-row[data-appid="1245620"]').click();const s=document.getElementById('content-select');s.value='base';s.dispatchEvent(new Event('change'));window.scrollTo(0,0)})()`);
  await search('');
  const snapshot = await evaluate(`({title: document.title, body: document.body.innerText, inputs: [...document.querySelectorAll('input')].map(e=>({id:e.id,type:e.type,placeholder:e.placeholder})), buttons:[...document.querySelectorAll('button')].map(e=>({id:e.id,text:e.innerText,aria:e.getAttribute('aria-label')})), overflow:document.documentElement.scrollWidth>innerWidth, brokenImages:[...document.images].filter(i=>i.complete&&!i.naturalWidth).map(i=>i.src)})`);
  await screenshot('desktop.png', 1440, 1050);
  await evaluate(`if(document.documentElement.dataset.theme!=='light')document.getElementById('theme-toggle').click()`);
  await check('Desktop light theme applies without horizontal overflow', `document.documentElement.dataset.theme==='light' && document.documentElement.scrollWidth<=innerWidth`);
  await screenshot('desktop-light.png', 1440, 1050);
  await evaluate(`document.getElementById('theme-toggle').click()`);
  await check('Desktop dark theme restores without horizontal overflow', `document.documentElement.dataset.theme==='dark' && document.documentElement.scrollWidth<=innerWidth`);
  await evaluate(`document.querySelector('#game-detail [data-history-range="all"]').click();document.querySelector('#game-detail .detail-heading').scrollIntoView({block:'start',behavior:'instant'})`);
  await screenshot('desktop-history.png', 1440, 1050);
  // Reload at the phone width so SVG tick density matches a real mobile visit.
  await command('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await command('Page.reload', {});
  await waitFor(`document.querySelectorAll('.game-row').length>0`);
  await delay(500);
  await search('1245620');
  await evaluate(`(()=>{document.querySelector('.game-row[data-appid="1245620"]').click();const s=document.getElementById('content-select');s.value='base';s.dispatchEvent(new Event('change'))})()`);
  await search('');
  await evaluate('window.scrollTo(0,0)');
  await screenshot('mobile.png', 390, 844);
  const mobileOverflow = await evaluate('document.documentElement.scrollWidth > innerWidth');
  await check('390px dark theme has no horizontal overflow', `document.documentElement.dataset.theme==='dark' && document.documentElement.scrollWidth<=innerWidth`);
  await evaluate(`document.getElementById('theme-toggle').click()`);
  await check('390px light theme has no horizontal overflow', `document.documentElement.dataset.theme==='light' && document.documentElement.scrollWidth<=innerWidth`);
  await screenshot('mobile-light.png', 390, 844);
  await evaluate(`document.getElementById('theme-toggle').click()`);
  await evaluate(`document.getElementById('game-detail').scrollIntoView({block:'start',behavior:'instant'})`);
  await screenshot('mobile-prices.png', 390, 844);
  await evaluate(`document.querySelector('#game-detail .steam-history-section').scrollIntoView({block:'start',behavior:'instant'})`);
  await command('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 });
  const touchBox = await evaluate(`(()=>{const r=document.querySelector('#game-detail .steam-history-svg').getBoundingClientRect();return {x:r.left+r.width*.55,y:r.top+r.height*.5}})()`);
  await command('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [touchBox] });
  await command('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await check('Mobile touch reads the historical price', `!document.querySelector('#game-detail .history-tooltip').hidden && Number.isFinite(Number(document.querySelector('#game-detail .steam-history-chart').dataset.activePrice))`);
  await screenshot('mobile-history.png', 390, 844);
  const report = { url, ...snapshot, mobileOverflow, javascriptErrors: errors, interactionChecks };
  writeFileSync(join(qa, 'browser-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ title: snapshot.title, textLength: snapshot.body.length, desktopOverflow: snapshot.overflow, mobileOverflow, javascriptErrors: errors, interactionChecks, screenshots: ['desktop.png','desktop-light.png','mobile.png','mobile-light.png','editions.png','dlc.png','bulk-game.png','bulk-game-light.png','widget.png','widget-light.png','mobile-widget.png','mobile-widget-light.png'].map(name=>join(qa,name)) }));
  if (errors.length || snapshot.overflow || mobileOverflow) process.exitCode = 1;
} finally {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ id: ++counter, method: 'Browser.close' }));
    await delay(150);
    socket.close();
  }
  child.kill();
}
