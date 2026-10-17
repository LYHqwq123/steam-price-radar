/** Validate the published data contract and local asset paths; no dependencies. */
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const dist = resolve(root, 'dist');
const file = resolve(dist, 'data/prices.json');
const sourceIds = new Set(['steam', 'sonkwo', 'steampy', 'steamcici']);
const quoteStatuses = new Set(['ok', 'unknown', 'stale', 'error', 'unavailable', 'suspect']);
const data = JSON.parse(readFileSync(file, 'utf8').replace(/^\uFEFF/, ''));

function nonempty(value, label) {
  assert.equal(typeof value, 'string', `${label} must be a string`);
  assert.ok(value.trim(), `${label} must not be empty`);
}

function date(value, label) {
  nonempty(value, label);
  assert.ok(Number.isFinite(Date.parse(value)), `${label} must be a valid date`);
}

function money(value, label) {
  assert.ok(value === null || (Number.isFinite(value) && value >= 0), `${label} must be null or a nonnegative number`);
}

function webUrl(value, label) {
  nonempty(value, label);
  const url = new URL(value);
  assert.equal(url.protocol, 'https:', `${label} must use HTTPS`);
}

function contentQuotes(quotes, label, subid) {
  assert.ok(Array.isArray(quotes), `${label}: missing quotes`);
  const seen = new Set();
  for (const q of quotes) {
    assert.ok(sourceIds.has(q.source) && !seen.has(q.source), `${label}: invalid or duplicate source`);
    seen.add(q.source);
    assert.equal(q.kind, q.source === 'steam' ? 'direct' : 'cdk', `${label}: incorrect purchase category`);
    assert.equal(q.currency, 'CNY', `${label}: wrong region currency`);
    assert.ok(quoteStatuses.has(q.status), `${label}: invalid quote status`);
    money(q.price, `${label}.price`);
    money(q.originalPrice, `${label}.originalPrice`);
    webUrl(q.url, `${label}.url`);
    date(q.checkedAt, `${label}.checkedAt`);
    if (q.eligible) {
      assert.equal(q.status, 'ok', `${label}: unverified quote cannot be purchasable`);
      assert.equal(q.stock, 'in_stock', `${label}: missing stock verification`);
      assert.notEqual(q.price, null, `${label}: missing purchasable price`);
    }
    if (q.source === 'steampy' && q.editionSubId != null && subid != null) {
      assert.equal(q.editionSubId, subid, `${label}: SteamPY belongs to another edition`);
    }
    if (q.source === 'steampy' && q.status === 'suspect') assert.equal(q.price, null, `${label}: anomalous PY price must be hidden`);
    if (q.source === 'steampy' && q.priceKind === 'reference') assert.equal(q.eligible, false, `${label}: a reference is not a verified order`);
  }
}

assert.equal(data.schemaVersion, 1, 'unsupported data schema');
date(data.generatedAt, 'generatedAt');
assert.equal(data.currency, 'CNY');
assert.equal(data.country, 'CN');
assert.ok(Number.isInteger(data.refreshMinutes) && data.refreshMinutes > 0);
assert.ok(Array.isArray(data.sources) && data.sources.length, 'sources must not be empty');
const seenSources = new Set();
for (const source of data.sources) {
  assert.ok(sourceIds.has(source.id), `unknown source ${source.id}`);
  assert.ok(!seenSources.has(source.id), `duplicate source ${source.id}`);
  seenSources.add(source.id);
  nonempty(source.name, `${source.id}.name`);
  nonempty(source.status, `${source.id}.status`);
  date(source.checkedAt, `${source.id}.checkedAt`);
  assert.equal(typeof source.message, 'string');
}
assert.equal(seenSources.size, sourceIds.size, 'all four sources must declare a state, including failures');

assert.ok(Array.isArray(data.games) && data.games.length, 'games must not be empty');
const appids = new Set();
for (const game of data.games) {
  const label = `game ${game.appid}`;
  assert.ok(Number.isInteger(game.appid) && game.appid > 0, `${label}: invalid AppID`);
  assert.ok(!appids.has(game.appid), `${label}: duplicate AppID`);
  appids.add(game.appid);
  nonempty(game.name, `${label}.name`);
  nonempty(game.edition, `${label}.edition`);
  assert.ok(Array.isArray(game.aliases), `${label}.aliases must be an array`);
  assert.ok(Array.isArray(game.genres), `${label}.genres must be an array`);
  webUrl(game.steamUrl, `${label}.steamUrl`);
  webUrl(game.steamdbUrl, `${label}.steamdbUrl`);
  assert.ok(Array.isArray(game.quotes), `${label}.quotes must be an array`);
  const quoteSources = new Set();
  for (const quote of game.quotes) {
    const quoteLabel = `${label}/${quote.source}`;
    assert.ok(seenSources.has(quote.source), `${quoteLabel}: source is not declared`);
    assert.ok(!quoteSources.has(quote.source), `${quoteLabel}: duplicate quote source`);
    quoteSources.add(quote.source);
    assert.equal(quote.kind, quote.source === 'steam' ? 'direct' : 'cdk', `${quoteLabel}: wrong purchase category`);
    assert.equal(quote.currency, 'CNY', `${quoteLabel}: wrong currency`);
    assert.ok(quoteStatuses.has(quote.status), `${quoteLabel}: unknown quote status`);
    money(quote.price, `${quoteLabel}.price`);
    money(quote.originalPrice, `${quoteLabel}.originalPrice`);
    assert.ok(quote.discount === null || (Number.isFinite(quote.discount) && quote.discount >= 0 && quote.discount <= 100), `${quoteLabel}: invalid discount`);
    assert.equal(typeof quote.eligible, 'boolean', `${quoteLabel}.eligible must be boolean`);
    assert.ok(quote.stock === null || typeof quote.stock === 'string', `${quoteLabel}.stock must be a string or null`);
    if (quote.eligible) {
      assert.equal(quote.status, 'ok', `${quoteLabel}: an unconfirmed quote cannot be marked purchasable`);
      assert.equal(quote.stock, 'in_stock', `${quoteLabel}: purchasable quotes require stock confirmation`);
      assert.notEqual(quote.price, null, `${quoteLabel}: purchasable quotes require a price`);
    }
    webUrl(quote.url, `${quoteLabel}.url`);
    date(quote.checkedAt, `${quoteLabel}.checkedAt`);
  }
  assert.equal(quoteSources.size, sourceIds.size, `${label}: each source must return a quote or a missing/error state`);
  assert.ok(Array.isArray(game.history), `${label}.history must be an array`);
  for (const point of game.history) {
    date(point.date, `${label}.history.date`);
    money(point.steam, `${label}.history.steam`);
    money(point.cdk, `${label}.history.cdk`);
  }
  const history = game.steamHistory;
  assert.ok(history && typeof history === 'object', `${label}: Steam history must declare data or an explicit failure state`);
  assert.equal(history.country, 'CN', `${label}: historical country must match the direct price region`);
  assert.equal(history.currency, 'CNY', `${label}: foreign currency conversions are not CN Steam history`);
  nonempty(history.source, `${label}.steamHistory.source`);
  nonempty(history.sourceName, `${label}.steamHistory.sourceName`);
  assert.ok(['ok', 'unavailable', 'error', 'stale'].includes(history.status), `${label}: unsupported history status`);
  assert.ok(['recorded', 'all'].includes(history.scope), `${label}: history must describe its actual coverage`);
  date(history.fetchedAt, `${label}.steamHistory.fetchedAt`);
  webUrl(history.sourceUrl, `${label}.steamHistory.sourceUrl`);
  assert.ok(Array.isArray(history.points), `${label}: history points must be an array`);
  let previousDate = '';
  for (const point of history.points) {
    assert.match(point.date, /^\d{4}-\d{2}-\d{2}$/, `${label}: history dates must use the source's Beijing calendar date`);
    date(point.date, `${label}.steamHistory.points.date`);
    assert.equal(new Date(point.date).toISOString().slice(0, 10), point.date, `${label}: invalid historical calendar date`);
    assert.ok(point.date > previousDate, `${label}: history must be sorted and deduplicated`);
    assert.ok(Number.isFinite(point.price) && point.price >= 0, `${label}: history price must be a real CNY amount`);
    previousDate = point.date;
  }
  if (history.status === 'ok') assert.ok(history.points.length > 0, `${label}: an empty result cannot be marked as successful history`);
  if (history.points.length) {
    if (history.coverageStart) assert.equal(history.coverageStart, history.points[0].date, `${label}: coverage starts outside its real records`);
    if (history.coverageEnd) assert.equal(history.coverageEnd, history.points.at(-1).date, `${label}: coverage ends outside its real records`);
  }
  assert.ok(Array.isArray(game.editions), `${label}: editions must declare results or failure`);
  assert.ok(Array.isArray(game.dlc), `${label}: DLC must declare results or failure`);
  assert.ok(game.contentStatus && typeof game.contentStatus.note === 'string', `${label}: content coverage is missing`);
  const contentIds = new Set();
  for (const edition of game.editions) {
    nonempty(edition.id, `${label}.edition.id`);
    assert.ok(!contentIds.has(edition.id), `${label}: duplicate edition`);
    contentIds.add(edition.id);
    assert.ok(['base', 'package', 'bundle'].includes(edition.kind), `${label}: invalid edition kind`);
    assert.equal(edition.appid, game.appid, `${label}: edition belongs to a different game`);
    nonempty(edition.name, `${label}.edition.name`);
    if (edition.kind === 'package') assert.ok(Number.isInteger(edition.subid) && edition.subid > 0, `${label}: invalid package`);
    if (edition.includedAppids && edition.includedAppids.length) assert.ok(edition.includedAppids.includes(game.appid), `${label}: edition does not include the base game`);
    contentQuotes(edition.quotes, `${label}/${edition.id}`, edition.subid);
  }
  const dlcIds = new Set();
  for (const dlc of game.dlc) {
    assert.ok(Number.isInteger(dlc.appid) && dlc.appid > 0 && dlc.appid !== game.appid, `${label}: invalid DLC AppID`);
    assert.ok(!dlcIds.has(dlc.appid), `${label}: duplicate DLC`);
    dlcIds.add(dlc.appid);
    assert.equal(dlc.parentAppid, game.appid, `${label}: DLC parent mismatch`);
    nonempty(dlc.name, `${label}.dlc.name`);
    contentQuotes(dlc.quotes, `${label}/DLC-${dlc.appid}`, dlc.subid);
    if (dlc.metadataVerified === false) assert.ok(dlc.quotes.every(q => q.price === null), `${label}: unverified content must not carry a price`);
  }
}

const indexPath = resolve(dist, 'data/catalog-index.json');
assert.ok(existsSync(indexPath), 'search directory is missing');
const index = JSON.parse(readFileSync(indexPath, 'utf8'));
assert.equal(index.schemaVersion, 1, 'unsupported catalog schema');
date(index.generatedAt, 'catalog.generatedAt');
assert.ok(Array.isArray(index.games) && index.games.length >= data.games.length, 'catalog must contain the priced games');
const indexedIds = new Set();
for (const game of index.games) {
  assert.ok(Number.isInteger(game.appid) && game.appid > 0 && !indexedIds.has(game.appid), 'catalog contains an invalid or duplicate AppID');
  indexedIds.add(game.appid);
  nonempty(game.name, `catalog ${game.appid}.name`);
}
for (const appid of appids) assert.ok(indexedIds.has(appid), `priced game ${appid} must also be searchable in the index`);
if (index.complete === false) assert.notEqual(index.status, 'complete', 'a partial directory must not be labelled complete');

const directPath = resolve(dist, 'data/direct-prices.json');
assert.ok(existsSync(directPath), 'batch Steam current prices are missing');
const direct = JSON.parse(readFileSync(directPath, 'utf8'));
assert.equal(direct.schemaVersion, 1, 'unsupported direct price schema');
assert.equal(direct.source, 'steam');
assert.equal(direct.country, 'CN');
assert.equal(direct.currency, 'CNY');
date(direct.generatedAt, 'direct.generatedAt');
assert.ok(['ok', 'partial', 'stale', 'error'].includes(direct.status));
assert.ok(Array.isArray(direct.games), 'batch prices must contain game records');
assert.equal(direct.gameCount, direct.games.length);
const directIds = new Set();
let directPricedCount = 0;
for (const row of direct.games) {
  const label = `Steam batch ${row.appid}`;
  assert.ok(Number.isInteger(row.appid) && indexedIds.has(row.appid) && !directIds.has(row.appid), `${label}: invalid or duplicate AppID`);
  directIds.add(row.appid);
  assert.ok(['ok', 'unavailable', 'stale', 'error'].includes(row.status), `${label}: unsupported status`);
  money(row.price, `${label}.price`);
  money(row.originalPrice, `${label}.originalPrice`);
  assert.ok(row.discount === null || (Number.isFinite(row.discount) && row.discount >= 0 && row.discount <= 100), `${label}: invalid discount`);
  if (row.checkedAt != null) date(row.checkedAt, `${label}.checkedAt`);
  if (row.status === 'ok') {
    assert.notEqual(row.price, null, `${label}: a valid price cannot be missing`);
    date(row.checkedAt, `${label}.checkedAt`);
    directPricedCount += 1;
  }
}
assert.equal(direct.pricedCount, directPricedCount, 'batch price coverage count must reflect actual successful amounts');

function filesBelow(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const path = resolve(directory, entry.name);
    return entry.isDirectory() ? filesBelow(path) : [path];
  });
}

function localReference(reference, from) {
  const path = reference.trim();
  if (!path || /^(?:[a-z][a-z0-9+.-]*:|\/\/|#)/i.test(path)) return;
  assert.ok(!path.startsWith('/'), `${from}: root-relative path breaks project GitHub Pages: ${path}`);
  const clean = decodeURIComponent(path.split(/[?#]/, 1)[0]);
  if (!clean) return;
  const target = resolve(dirname(from), clean);
  assert.ok(target === dist || target.startsWith(dist + sep), `${from}: path escapes dist: ${path}`);
  assert.ok(existsSync(target), `${from}: missing local resource ${path}`);
  if (statSync(target).isDirectory()) {
    assert.ok(existsSync(resolve(target, 'index.html')), `${from}: missing directory index for ${path}`);
  }
}

assert.ok(existsSync(resolve(dist, 'index.html')), 'dist/index.html is missing');
for (const asset of filesBelow(dist)) {
  const extension = extname(asset);
  if (!['.html', '.css', '.js', '.mjs'].includes(extension)) continue;
  const content = readFileSync(asset, 'utf8');
  if (extension === '.html') {
    assert.match(content, /<html\b[^>]*\blang=["']zh(?:-CN)?["']/i, `${asset}: missing Chinese page language`);
    for (const match of content.matchAll(/\b(?:src|href)\s*=\s*["']([^"']+)["']/gi)) localReference(match[1], asset);
  }
  if (extension === '.css') {
    for (const match of content.matchAll(/url\(\s*["']?([^"')\s]+)["']?\s*\)/gi)) localReference(match[1], asset);
  }
  if (['.js', '.mjs'].includes(extension)) {
    for (const match of content.matchAll(/(?:\bfrom\s*|\bimport\s*)["'](\.?\.?\/[^"']+)["']/g)) localReference(match[1], asset);
    // fetch() resolves against the document URL, rather than the script file.
    for (const match of content.matchAll(/\bfetch\(\s*["'](\.?\.?\/[^"']+|data\/[^"']+)["']/g)) localReference(match[1], resolve(dist, 'index.html'));
  }
}

console.log(`Website checks passed: ${data.games.length} games, ${data.sources.length} sources; prices, regional histories, and local resource paths are valid.`);
