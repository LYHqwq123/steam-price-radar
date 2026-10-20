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
const notes = [];
function command(method, params = {}) {
  const id = ++counter;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 30000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const r = await command('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
  return r.result.value;
}
async function waitFor(expression, timeout = 20000) {
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
  await delay(400);
}
async function clickRow(appid) {
  const selector = `.game-row[data-appid="${Number(appid)}"]`;
  await waitFor(`document.querySelector(${JSON.stringify(selector)})!==null`);
  await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
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
  await waitFor(`document.readyState === 'complete' && window.PriceRadar && document.querySelector('.game-row')`, 30000);
  await waitFor(`!document.getElementById('direct-price-time').textContent.includes('正在') && !document.getElementById('catalog-status').textContent.includes('正在')`, 30000);

  await evaluate(`window.__qaInitialTheme=document.documentElement.dataset.theme;document.getElementById('theme-toggle').click()`);
  const savedTheme = await evaluate(`document.documentElement.dataset.theme`);
  await check('Day/night theme changes and persists the choice', `['light','dark'].includes(document.documentElement.dataset.theme) && document.documentElement.dataset.theme!==window.__qaInitialTheme && localStorage.getItem('game-price-radar.theme.v1')===document.documentElement.dataset.theme`);
  await command('Page.reload', {});
  await waitFor(`document.readyState==='complete' && window.PriceRadar && document.querySelector('.game-row')`, 30000);
  await waitFor(`!document.getElementById('direct-price-time').textContent.includes('正在')`, 30000);
  await check('Theme remains selected after reload', `document.documentElement.dataset.theme===${JSON.stringify(savedTheme)}`);
  await evaluate(`if(document.documentElement.dataset.theme!=='dark')document.getElementById('theme-toggle').click()`);

  await evaluate(`(async()=>{
    window.__qaData = window.PRICE_RADAR_BOOTSTRAP || await (await fetch('./data/prices.json')).json();
    window.__qaCatalog = window.__CATALOG_DATA__ || await (await fetch('./data/catalog-index.json')).json();
    window.__qaManifest = window.__PRICE_MANIFEST__ || await (await fetch('./data/price-manifest.json')).json();
    window.__qaShards = {};
    if (window.__PRICE_SHARDS__) Object.assign(window.__qaShards, window.__PRICE_SHARDS__);
    else {
      const entries = window.__qaManifest.shards.filter(entry => entry.available);
      await Promise.all(entries.map(async entry => {
        window.__qaShards[entry.file] = await (await fetch('./data/' + entry.file)).json();
      }));
    }
    window.__qaPrices = new Map();
    Object.values(window.__qaShards).forEach(shard => {
      PriceRadar.validatePriceShard(shard, shard.shard, shard.shardCount).forEach(row => window.__qaPrices.set(row[0], row));
    });
    window.__qaDetailIds = new Set(window.__qaData.games.map(game => Number(game.appid)));
    return true;
  })()`);
  await check('Whole-library directory uses compact v2 rows', `window.__qaCatalog.schemaVersion===2 && Array.isArray(window.__qaCatalog.games) && window.__qaCatalog.games.length===window.__qaCatalog.gameCount && window.__qaCatalog.games.every(row=>Array.isArray(row)&&row.length>=2&&Number.isInteger(row[0])) && window.__qaCatalog.games.length>window.__qaData.games.length`);
  await check('Sharded price manifest matches the aggregate shard data', `(()=>{const m=window.__qaManifest,shards=window.__qaShards;if(m.schemaVersion!==2||m.source!=='steam'||m.country!=='CN'||m.currency!=='CNY'||!Array.isArray(m.shards)||m.shards.length!==m.shardCount)return false;const seen=new Set();let count=0,priced=0,available=0;for(const entry of m.shards){if(!entry.available)continue;available+=1;const shard=shards[entry.file];if(!shard||shard.schemaVersion!==2||shard.shard!==entry.index||shard.shardCount!==m.shardCount||!Array.isArray(shard.games))return false;for(const row of shard.games){if(!Array.isArray(row)||row.length<5||row[0]%m.shardCount!==entry.index||seen.has(row[0]))return false;seen.add(row[0]);if(row[4]===0){if(!Number.isInteger(row[1])||row[1]<0)return false;priced+=1;}count+=1;}}return count===m.gameCount&&priced===m.pricedCount&&available===m.availableShardCount})()`);
  await check('Overview counts come from the directory, shards and detail snapshot', `Number(document.getElementById('game-count').textContent.replace(/,/g,''))===window.__qaCatalog.games.length && Number(document.getElementById('direct-price-count').textContent.replace(/,/g,''))===window.__qaManifest.pricedCount && Number(document.getElementById('complete-price-count').textContent.replace(/,/g,''))===window.__qaData.games.length`);
  await check('Compact catalog and shard validation reject malformed data', `(()=>{let catalogOk=false,manifestCurrency=false,manifestPath=false,shardOk=false;try{const value=PriceRadar.validateCatalog({schemaVersion:2,games:[[1,'A'],[2,'B','Bee',['bee']],[1,'duplicate'],[0,'bad']]});catalogOk=value.rows.length===2&&value.rows[1][3][0]==='bee'}catch(_){}try{PriceRadar.validatePriceManifest({...window.__qaManifest,currency:'USD'})}catch(_){manifestCurrency=true}const badShards=window.__qaManifest.shards.map((entry,index)=>index===0?{...entry,file:'../outside.json'}:entry);try{PriceRadar.validatePriceManifest({...window.__qaManifest,shards:badShards})}catch(_){manifestPath=true}try{const stamp=new Date().toISOString();const rows=PriceRadar.validatePriceShard({schemaVersion:2,shard:0,shardCount:window.__qaManifest.shardCount,checkedAt:stamp,games:[[64,1234,2000,38,0],[1,999,null,null,0]]},0,window.__qaManifest.shardCount);shardOk=rows.length===1&&rows[0][0]===64&&PriceRadar.directQuote(rows[0],64).price===12.34}catch(_){}return catalogOk&&manifestCurrency&&manifestPath&&shardOk})()`);
  await check('Basic prices and details expire according to their own snapshot intervals', `(()=>{if(location.protocol==='file:')return true;const now=Date.now(),ttl=minutes=>Math.min(Math.max(60,minutes*4),48*60)*60000;return [[true,Number(window.__qaManifest.refreshMinutes)||120],[false,Number(window.__qaData.refreshMinutes)||15]].every(([basicPrice,minutes])=>PriceRadar.isQuoteExpired({checkedAt:new Date(now-ttl(minutes)+1000).toISOString(),status:'ok',basicPrice},now)===false&&PriceRadar.isQuoteExpired({checkedAt:new Date(now-ttl(minutes)-1000).toISOString(),status:'ok',basicPrice},now)===true)})()`);
  await check('Unknown row timestamps never become valid prices, including offline previews', `(()=>{const stamp=new Date().toISOString(),count=window.__qaManifest.shardCount;return [null,'','not-a-date'].every(checkedAt=>{const rows=PriceRadar.validatePriceShard({schemaVersion:2,shard:0,shardCount:count,checkedAt:stamp,games:[[count,1234,2000,38,0,checkedAt]]},0,count);return rows.every(row=>!PriceRadar.isUsableQuote(PriceRadar.directQuote(row,count)))})})()`);

  const basicAppid = await evaluate(`(()=>{const row=window.__qaCatalog.games.find(row=>{if(window.__qaDetailIds.has(row[0]))return false;const record=window.__qaPrices.get(row[0]);return record&&PriceRadar.isUsableQuote(PriceRadar.directQuote(record,row[0]))});return row?row[0]:null})()`);
  if (basicAppid) {
    await search(String(basicAppid));
    await clickRow(basicAppid);
    await check('Catalog game search resolves an exact AppID with its shard price', `document.querySelectorAll('.game-row').length===1 && document.querySelector('#game-detail .detail-meta').textContent.includes('AppID ${basicAppid}') && Number(document.querySelector('#game-detail .basic-price-cards .price-number').textContent.replace(/[^0-9.]/g,''))===window.__qaPrices.get(${basicAppid})[1]/100`);
    await check('Basic detail does not fabricate CDK, editions or history', `!document.querySelector('#game-detail .content-selector') && !document.querySelector('#game-detail .quote-section') && !document.querySelector('#game-detail .steam-history-section') && !document.querySelector('#game-detail .cdk-card') && document.querySelector('#game-detail .basic-pending-list').textContent.includes('历史价格待采集')`);
    await evaluate(`document.querySelector('#game-detail .detail-heading').scrollIntoView({block:'start',behavior:'instant'})`);
    await screenshot('basic-game.png', 1440, 1050);
    await evaluate(`document.querySelector('.detail-favorite').click();document.getElementById('favorites-tab').click()`);
    await check('Favorite catalog game is searchable in the favorites view', `document.getElementById('favorites-count').textContent==='1' && document.querySelector('.game-row[data-appid="${basicAppid}"]')!==null`);
    await evaluate(`document.querySelector('.game-row[data-appid="${basicAppid}"]').click();document.querySelector('.detail-favorite').click();document.getElementById('all-tab').click()`);
    await search('');
    await check('Removing a favorite restores the library view', `document.getElementById('favorites-count').textContent==='0' && document.querySelectorAll('.game-row').length===30`);
  } else {
    notes.push('No catalog-only game with a successful price; basic detail checks skipped.');
  }

  const unpricedAppid = await evaluate(`(()=>{const row=window.__qaCatalog.games.find(row=>{if(window.__qaDetailIds.has(row[0]))return false;const record=window.__qaPrices.get(row[0]);return !record||record[4]!==0});return row?row[0]:null})()`);
  if (unpricedAppid) {
    await search(String(unpricedAppid));
    await clickRow(unpricedAppid);
    await check('Unpriced catalog game uses only its official Steam widget', `document.querySelectorAll('#game-detail iframe').length===1 && document.querySelector('#game-detail iframe').src==='https://store.steampowered.com/widget/${unpricedAppid}/?cc=cn&l=schinese' && !document.querySelector('#game-detail .price-cards') && document.querySelector('#game-detail .detail-meta').textContent.includes('AppID ${unpricedAppid}')`);
    await screenshot('widget.png', 1440, 1050);
  } else {
    notes.push('Every catalog game has a successful price; widget fallback skipped.');
  }

  const alias = await evaluate(`(()=>{for(const game of window.__qaData.games){const value=(game.aliases||[]).find(alias=>/[\\u3400-\\u9fff]/.test(alias));if(value)return {appid:Number(game.appid),value};}const game=window.__qaData.games.find(game=>/[\\u3400-\\u9fff]/.test(game.name||''));return game?{appid:Number(game.appid),value:game.name}:null})()`);
  if (alias) {
    await search(alias.value);
    await waitFor(`document.querySelector('.game-row[data-appid="${alias.appid}"]')!==null`);
    interactionChecks.push('Chinese alias search resolves the curated game');
  } else notes.push('No Chinese alias in the detail snapshot; alias search skipped.');

  const numericAlias = await evaluate(`(()=>{const cyberpunk=window.__qaCatalog.games.find(row=>row[0]===1091500&&[row[1],row[2],...(row[3]||[])].some(value=>typeof value==='string'&&value.includes('2077')));if(cyberpunk)return {appid:1091500,value:'2077'};for(const row of window.__qaCatalog.games){const value=(row[3]||[]).find(alias=>typeof alias==='string'&&/^\\d+$/.test(alias));if(value&&Number(value)!==row[0])return {appid:row[0],value};}return null})()`);
  if (numericAlias) {
    await search(numericAlias.value);
    await waitFor(`document.querySelector('.game-row[data-appid="${numericAlias.appid}"]')!==null`);
    await check('Numeric names and aliases remain searchable alongside exact AppIDs', `(()=>{const results=PriceRadar.searchCatalog(${JSON.stringify(numericAlias.value)}).rows;if(!results.some(row=>row[0]===${numericAlias.appid}))return false;const exact=window.__qaCatalog.games.find(row=>row[0]===Number(${JSON.stringify(numericAlias.value)}));return !exact||results[0][0]===exact[0]})()`);
  } else notes.push('No numeric title or alias in this dataset; numeric alias search skipped.');

  const detailGame = await evaluate(`(()=>{const game=window.__qaData.games.find(game=>Number(game.appid));return game?Number(game.appid):null})()`);
  if (detailGame) {
    await search(`https://store.steampowered.com/app/${detailGame}/?cc=cn`);
    await waitFor(`document.querySelector('.game-row[data-appid="${detailGame}"]')!==null`);
    await clickRow(detailGame);
    await check('Steam store URL search opens the detailed comparison', `document.querySelectorAll('.game-row').length===1 && document.querySelector('#game-detail .detail-meta').textContent.includes('AppID ${detailGame}') && document.querySelector('#game-detail .content-selector')!==null`);
    await check('Detailed Steam price matches the merged detail/shard quote', `(()=>{const game=window.__qaData.games.find(game=>Number(game.appid)===${detailGame});const merged=PriceRadar.mergeDirectPrice(game,window.__qaPrices.get(${detailGame}));const quote=PriceRadar.getSteamQuote(merged);const card=document.querySelector('#game-detail .price-card:not(.cdk-card) .price-number');return quote?Number(card.textContent.replace(/[^0-9.]/g,''))===quote.price:card.classList.contains('no-price')})()`);
    await evaluate(`document.getElementById('provider-select').value='sonkwo';document.getElementById('provider-select').dispatchEvent(new Event('change'))`);
    await check('Provider selector relabels the CDK comparison', `document.querySelector('.cdk-card .price-card-label').textContent.includes('杉果')`);
    await evaluate(`document.getElementById('provider-select').value='all';document.getElementById('provider-select').dispatchEvent(new Event('change'))`);
  } else notes.push('Detail snapshot has no games; detailed-page checks skipped.');

  await search('不存在的游戏 QA-92381');
  await check('Unknown game keeps an explicit empty state', `!document.querySelector('.game-row') && document.getElementById('game-list').innerText.includes('全库中暂未找到')`);

  const broadQuery = await evaluate(`(()=>{for(const term of ['a','e','2','战','模拟']){let count=0;for(const row of window.__qaCatalog.games){const fields=[row[1],row[2],...(Array.isArray(row[3])?row[3]:[])];if(fields.some(value=>typeof value==='string'&&value.toLowerCase().includes(term))){count+=1;if(count>2000)return term;}}if(window.__qaCatalog.games.length<2000&&count>60)return term;}return null})()`);
  if (broadQuery) {
    await search(broadQuery);
    await waitFor(`document.querySelectorAll('.game-row').length===30`);
    const capped = await evaluate(`document.getElementById('catalog-status').textContent.includes('匹配超过 2,000') || document.getElementById('visible-count').textContent.includes('+')`);
    if (capped) {
      await check('Broad search is capped and explains the truncation', `document.getElementById('catalog-status').textContent.includes('仅显示前 2,000') && document.getElementById('visible-count').textContent.includes('+') && document.getElementById('catalog-more').hidden===false`);
    } else {
      interactionChecks.push('Broad search matched fewer than 2,000 games; cap copy not required');
    }
    const firstPage = await evaluate(`[...document.querySelectorAll('.game-row')].map(row=>row.dataset.appid)`);
    await evaluate(`document.getElementById('catalog-more').click()`);
    await check('Search results continue in bounded pages', `document.querySelectorAll('.game-row').length===60 && JSON.stringify([...document.querySelectorAll('.game-row')].slice(0,30).map(row=>row.dataset.appid))===${JSON.stringify(JSON.stringify(firstPage))}`);
  } else notes.push('Catalog is too small for a >2,000-result search cap check.');

  await search('');
  await evaluate(`document.getElementById('sort-select').value='direct';document.getElementById('sort-select').dispatchEvent(new Event('change'))`);
  await check('Visible Steam prices sort ascending with missing values last', `(()=>{const amounts=[...document.querySelectorAll('.game-row')].map(row=>{const amount=row.querySelector('.row-price:not(.cdk-price)');return amount.classList.contains('is-missing')?Infinity:Number(amount.firstElementChild.textContent.replace(/[^0-9.]/g,''))});return amounts.length===30&&amounts.every((amount,index)=>index===0||amount>=amounts[index-1])})()`);
  await evaluate(`document.getElementById('sort-select').value='recommended';document.getElementById('sort-select').dispatchEvent(new Event('change'))`);

  const historyAppid = await evaluate(`(()=>{const game=window.__qaData.games.find(game=>game.steamHistory&&Array.isArray(game.steamHistory.points)&&game.steamHistory.points.length>=2);return game?Number(game.appid):null})()`);
  if (historyAppid) {
    await search(String(historyAppid));
    await clickRow(historyAppid);
    await waitFor(`document.querySelector('#game-detail .steam-history-svg')!==null`);
    await check('Recorded history draws exactly the source points', `document.querySelectorAll('#game-detail .history-record-point').length===window.__qaData.games.find(game=>Number(game.appid)===${historyAppid}).steamHistory.points.length && document.querySelector('#game-detail .history-price-line').getAttribute('d').includes('H')`);
    await evaluate(`document.querySelector('#game-detail [data-history-range="30"]').click()`);
    await check('History range switching stays interactive', `document.querySelector('#game-detail [data-history-range="30"]').getAttribute('aria-pressed')==='true'`);
    await evaluate(`document.querySelector('#game-detail [data-history-range="all"]').click();document.querySelector('#game-detail .steam-history-section').scrollIntoView({block:'start',behavior:'instant'})`);
    await screenshot('history.png', 1440, 1050);
    if (await evaluate(`document.querySelector('#game-detail .history-expand')!==null`)) {
      await evaluate(`document.querySelector('#game-detail .history-expand').click()`);
      await check('Expanded history dialog opens', `document.getElementById('history-dialog').open && document.querySelector('#history-dialog .steam-history-svg')!==null`);
      await evaluate(`document.getElementById('close-history-dialog').click()`);
      await check('Expanded history dialog closes', `!document.getElementById('history-dialog').open`);
    }
  } else notes.push('No detailed game has two history points; history checks skipped.');

  await evaluate(`document.getElementById('help-button').click()`);
  await check('Help dialog opens', `document.getElementById('help-dialog').open`);
  await command('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  await command('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  await waitFor(`!document.getElementById('help-dialog').open`);
  interactionChecks.push('Escape closes help');

  await evaluate(`(() => { const dt=new DataTransfer();dt.items.add(new File(['{"appids":[true]}'],'invalid.json',{type:'application/json'}));const el=document.getElementById('import-file');el.files=dt.files;el.dispatchEvent(new Event('change')); })()`);
  await waitFor(`document.getElementById('toast-region').innerText.includes('导入失败')`);
  await check('Invalid favorite import is rejected', `document.getElementById('favorites-count').textContent==='0'`);

  await evaluate(`document.getElementById('refresh-button').click()`);
  await waitFor(`!document.getElementById('refresh-button').disabled`, 30000);
  await check('Snapshot refresh re-reads published data explicitly', `document.getElementById('toast-region').innerText.includes(location.protocol==='file:'?'本地':'快照')`);

  await search('');
  await evaluate(`window.scrollTo(0,0)`);
  const snapshot = await evaluate(`({title: document.title, textLength: document.body.innerText.length, overflow:document.documentElement.scrollWidth>innerWidth, inputs:[...document.querySelectorAll('input')].map(e=>({id:e.id,type:e.type})), buttons:[...document.querySelectorAll('button')].map(e=>({id:e.id,text:e.innerText,aria:e.getAttribute('aria-label')}))})`);
  await screenshot('desktop.png', 1440, 1050);
  await evaluate(`if(document.documentElement.dataset.theme!=='light')document.getElementById('theme-toggle').click()`);
  await check('Desktop light theme has no horizontal overflow', `document.documentElement.dataset.theme==='light' && document.documentElement.scrollWidth<=innerWidth`);
  await screenshot('desktop-light.png', 1440, 1050);
  await evaluate(`document.getElementById('theme-toggle').click()`);
  await check('Desktop dark theme has no horizontal overflow', `document.documentElement.dataset.theme==='dark' && document.documentElement.scrollWidth<=innerWidth`);

  await command('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await command('Page.reload', {});
  await waitFor(`document.readyState==='complete' && window.PriceRadar && document.querySelector('.game-row')`, 30000);
  await waitFor(`!document.getElementById('direct-price-time').textContent.includes('正在')`, 30000);
  await evaluate('window.scrollTo(0,0)');
  await screenshot('mobile.png', 390, 844);
  await check('390px dark theme has no horizontal overflow', `document.documentElement.dataset.theme==='dark' && document.documentElement.scrollWidth<=innerWidth`);
  await evaluate(`document.getElementById('theme-toggle').click()`);
  await check('390px light theme has no horizontal overflow', `document.documentElement.dataset.theme==='light' && document.documentElement.scrollWidth<=innerWidth`);
  await screenshot('mobile-light.png', 390, 844);

  const report = { url, ...snapshot, mobileOverflow: await evaluate('document.documentElement.scrollWidth>innerWidth'), javascriptErrors: errors, notes, interactionChecks };
  writeFileSync(join(qa, 'browser-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ title: snapshot.title, textLength: snapshot.textLength, desktopOverflow: snapshot.overflow, mobileOverflow: report.mobileOverflow, javascriptErrors: errors, notes, interactionChecks, screenshots: ['desktop.png','desktop-light.png','mobile.png','mobile-light.png','basic-game.png','widget.png','history.png'].map(name=>join(qa,name)) }));
  if (errors.length || snapshot.overflow || report.mobileOverflow) process.exitCode = 1;
} finally {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ id: ++counter, method: 'Browser.close' }));
    await delay(150);
    socket.close();
  }
  child.kill();
}
