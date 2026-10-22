const test = require('node:test');
const assert = require('node:assert/strict');
const {createApp, response, deferred, clone, flush} = require('./harness.cjs');

const inventoryUrl = 'http://127.0.0.1:8771';
const sourceId = '10000000-0000-4000-8000-000000000001';
const fiveKilograms = (5000).toLocaleString(undefined, {maximumFractionDigits: 0}) + ' g';
function source(version = 1, grams = 400) {
  return {stream_id: sourceId, latest_event_id: 1, orders: [], menu: [{
    id: 'caesar', name: 'Caesar salad', version,
    ingredients: [{ingredient_id: 'lettuce', grams: 100}],
    modifiers: [{id: 'chicken', name: 'Chicken', ingredients: [{ingredient_id: 'chicken', grams}]},
      {id: 'shrimp', name: 'Shrimp', ingredients: [{ingredient_id: 'shrimp', grams: 120}]}],
  }]};
}
function stock() {
  return {today: '2026-10-22', ingredients: [{id: 'chicken', name: 'Chicken', on_hand_g: 5000,
    reserved_g: 0, available_g: 5000, unallocated_g: 0}],
  batches: [{id: 1, ingredient_id: 'chicken', quantity_g: 5000, remaining_g: 5000,
    label: 'Fictional delivery', received_on: '2026-10-22', expires_on: '2026-10-24'}], counts: [],
  sync: {stream_id: sourceId, last_event_id: 1, paused: false, error: null,
    last_sync: '2026-10-22T10:00:00+00:00'}};
}
function ready() {
  const h = createApp();
  Object.assign(h.app.state, {config: {inventory_url: inventoryUrl, today: '2026-10-22'},
    source: source(), stock: stock(), sourceOnline: true, inventoryOnline: true});
  h.app.renderMenu(); h.app.renderInventory(); h.app.renderKitchen(true); h.app.renderConnection();
  return h;
}
function sourceResponse(h) { return response(clone(h.app.state.source)); }
function stockResponse(h) { return response(clone(h.app.state.stock)); }

test('HTML script boots without a service when opened as a saved file', () => {
  const h = createApp();
  assert.equal(h.calls.length, 0);
  assert.match(h.get('connection-error').textContent, /Start Pantry Signal/);
  assert.equal(h.get('connection-error').hidden, false);
});

test('current stock exposes a successful sync timestamp beside the numbers', () => {
  const h = ready();
  assert.equal(h.run('inventoryFreshness().code'), 'current');
  assert.equal(h.get('stock-tiles').classList.contains('inventory-stale'), false);
  assert.equal(h.get('save-count').disabled, false);
  assert.ok(h.get('stock-tiles').textContent.includes(fiveKilograms));
  assert.match(h.get('stock-sync-time').textContent, /22 Oct/);
  assert.match(h.get('inventory-sync-time').textContent, /22 Oct/);
});

const staleCases = [
  ['inventory_offline', h => { h.app.state.inventoryOnline = false; }],
  ['source_offline', h => { h.app.state.sourceOnline = false; }],
  ['stream_mismatch', h => { h.app.state.stock.sync.stream_id = '20000000-0000-4000-8000-000000000002'; }],
  ['stream_unconfirmed', h => { delete h.app.state.stock.sync.stream_id; }],
  ['paused', h => { h.app.state.stock.sync.paused = true; }],
  ['sync_error', h => { h.app.state.stock.sync.error = 'Source log unavailable'; }],
  ['cursor_invalid', h => { h.app.state.stock.sync.last_event_id = -1; }],
  ['cursor_invalid', h => { h.app.state.source.latest_event_id = 'not a cursor'; }],
  ['cursor_invalid', h => { delete h.app.state.source.latest_event_id; }],
  ['cursor_invalid', h => { h.app.state.stock.sync.last_event_id = null; }],
  ['cursor_invalid', h => { h.app.state.stock.sync.last_event_id = 1.5; }],
  ['catching_up', h => { h.app.state.source.latest_event_id = 2; }],
  ['source_behind', h => { h.app.state.stock.sync.last_event_id = 2; }],
  ['sync_missing', h => { h.app.state.stock.sync.last_sync = null; }],
  ['sync_missing', h => { h.app.state.stock.sync.last_sync = 'invalid'; }],
  ['sync_future', h => { h.app.state.stock.sync.last_sync = '2026-10-22T10:01:00Z'; }],
  ['sync_aged', h => { h.setNow('2026-10-22T10:00:16Z'); }],
];
for (const [index, [code, mutate]] of staleCases.entries()) {
  test(`stale stock (${code}, case ${index + 1}) labels cached quantities and prevents a count submission`, async () => {
    const h = ready(); mutate(h); h.app.renderConnection();
    assert.equal(h.run('inventoryFreshness().code'), code);
    assert.equal(h.get('stock-tiles').classList.contains('inventory-stale'), true);
    assert.equal(h.get('batches-body').classList.contains('inventory-stale'), true);
    assert.equal(h.get('stock-sync-warning').hidden, false);
    assert.equal(h.get('save-count').disabled, true);
    assert.match(h.get('stock-freshness-label').textContent, /cached|not current|waiting/i);
    assert.match(h.get('stock-tiles').textContent, /cached|not current|waiting/i);
    assert.ok(h.get('stock-tiles').textContent.includes(fiveKilograms)); // Cached values stay available for reference.
    await h.get('count-form').fire('submit');
    assert.equal(h.calls.length, 0); // An Enter-key/form submission must also be guarded.
    assert.equal(h.get('count-result').hidden, false);
  });
}

test('missing inventory has a waiting state and cannot record a stock comparison', () => {
  const h = ready(); h.app.state.stock = null; h.app.renderConnection();
  assert.equal(h.run('inventoryFreshness().code'), 'loading');
  assert.equal(h.get('save-count').disabled, true);
});

test('fetching the same stale ledger does not turn its old sync timestamp into a current one', async () => {
  const h = ready(); h.setNow('2026-10-22T10:00:16Z');
  h.route(() => stockResponse(h)); await h.app.refreshInventory();
  assert.equal(h.app.state.inventoryOnline, true);
  assert.equal(h.run('inventoryFreshness().code'), 'sync_aged');
  assert.equal(h.get('save-count').disabled, true);
  h.app.state.stock.sync.last_sync = '2026-10-22T10:00:16Z';
  await h.app.refreshInventory();
  assert.equal(h.run('inventoryFreshness().code'), 'current');
  assert.equal(h.get('stock-tiles').classList.contains('inventory-stale'), false);
  assert.equal(h.get('save-count').disabled, false);
});

test('small clock skew is tolerated but a far-future sync time is rejected', () => {
  const h = ready(); h.app.state.stock.sync.last_sync = '2026-10-22T10:00:05Z';
  assert.equal(h.run('inventoryFreshness().code'), 'current');
  h.app.state.stock.sync.last_sync = '2026-10-22T10:00:06Z';
  assert.equal(h.run('inventoryFreshness().code'), 'sync_future');
});

test('the screen ages a stuck sync without waiting for a new network response', async () => {
  const h = ready();
  h.run("location.protocol='http:'");
  h.route(call => call.url === '/api/config'
    ? response(h.app.state.config)
    : call.url === '/api/state' ? sourceResponse(h) : stockResponse(h));
  await h.app.start();
  h.route(() => new Promise(() => {})); // Both services can hang after initial loading.
  await h.advance(16001);
  assert.equal(h.get('stock-tiles').classList.contains('inventory-stale'), true);
  assert.equal(h.get('save-count').disabled, true);
});

for (const service of ['Source', 'Inventory']) {
  const sourceSide = service === 'Source', refresh = `refresh${service}`;
  const online = sourceSide ? 'sourceOnline' : 'inventoryOnline';
  const data = sourceSide ? 'source' : 'stock';
  test(`${service} polling ignores an older success arriving after a newer success`, async () => {
    const h = ready(), first = deferred(), second = deferred(); let n = 0;
    h.route(() => (++n === 1 ? first.promise : second.promise));
    const a = h.app[refresh](), b = h.app[refresh]();
    const newer = sourceSide ? source(2, 450) : stock();
    if (!sourceSide) newer.ingredients[0].on_hand_g = 4200;
    second.resolve(response(newer)); await b;
    first.resolve(response(sourceSide ? source() : stock())); await a;
    assert.equal(h.app.state[data][sourceSide ? 'menu' : 'ingredients'][0][sourceSide ? 'version' : 'on_hand_g'], sourceSide ? 2 : 4200);
  });
  test(`${service} polling ignores an older failure arriving after a newer success`, async () => {
    const h = ready(), first = deferred(), second = deferred(); let n = 0;
    h.route(() => (++n === 1 ? first.promise : second.promise));
    const a = h.app[refresh](), b = h.app[refresh]();
    second.resolve(response(sourceSide ? source() : stock())); await b;
    first.reject(new TypeError('lost old request')); await a;
    assert.equal(h.app.state[online], true);
  });
  test(`${service} polling keeps a newer failure authoritative over an older success`, async () => {
    const h = ready(), first = deferred(), second = deferred(); let n = 0;
    h.route(() => (++n === 1 ? first.promise : second.promise));
    const a = h.app[refresh](), b = h.app[refresh]();
    second.reject(new TypeError('newer connection failed')); await b;
    first.resolve(response(sourceSide ? source() : stock())); await a;
    assert.equal(h.app.state[online], false);
    assert.equal(h.get('stock-tiles').classList.contains('inventory-stale'), true);
  });
}

test('an uncertain order retry reuses its reference, while editing the order gets a new reference', async () => {
  const h = ready(); h.route(() => { throw new TypeError('response lost'); });
  await h.get('order-form').fire('submit');
  await h.get('order-form').fire('submit');
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[0].body.request_id, h.calls[1].body.request_id);
  assert.match(h.get('order-result').textContent, /retry will reuse the same order reference/);
  h.get('order-quantity').value = '2'; await h.get('order-quantity').fire('input');
  await h.get('order-form').fire('submit');
  assert.notEqual(h.calls[0].body.request_id, h.calls[2].body.request_id);
  assert.equal(h.calls[2].body.lines[0].quantity, 2);
});

test('simultaneous submit events send only one order, even before a response arrives', async () => {
  const h = ready(), pending = deferred();
  h.route(call => call.url === '/api/orders' ? pending.promise
    : call.url === '/api/state' ? sourceResponse(h) : stockResponse(h));
  const first = h.get('order-form').fire('submit');
  await h.get('order-form').fire('submit');
  assert.equal(h.calls.filter(call => call.url === '/api/orders').length, 1);
  assert.equal(h.get('place-order').disabled, true);
  pending.resolve(response({order: {id: 1}})); await first; await flush();
  assert.equal(h.app.state.orderIdentity, null);
  assert.equal(h.get('place-order').disabled, false);
});

test('orders and kitchen transitions complete while inventory requests never answer', async () => {
  const h = ready(); h.app.state.inventoryOnline = false;
  h.app.state.source.orders = [{id: 1, table_number: 7, status: 'queued', lines: [], created_at: '2026-10-22T10:00:00Z'}];
  h.app.renderKitchen(true); h.app.renderConnection();
  assert.equal(h.get('place-order').disabled, false);
  assert.equal(h.get('kitchen-tickets').querySelector('button').disabled, false);
  h.route(call => {
    if (call.url.startsWith(inventoryUrl)) return new Promise(() => {});
    if (call.method === 'POST' && call.url === '/api/orders') return response({order: {id: 2}});
    if (call.method === 'POST') { h.app.state.source.orders[0].status = call.body.status; return response({order: {id: 1}}); }
    return sourceResponse(h);
  });
  let orderDone = false;
  const order = h.get('order-form').fire('submit').then(() => { orderDone = true; });
  await flush(); assert.equal(orderDone, true); await order;
  let kitchenDone = false;
  const transition = h.app.transition(1, 'preparing').then(() => { kitchenDone = true; });
  await flush(); assert.equal(kitchenDone, true); await transition;
  assert.equal(h.app.state.source.orders[0].status, 'preparing');
  assert.equal(h.app.state.inventoryOnline, false);
});

for (const path of ['/api/receipts', '/api/waste', '/api/counts']) {
  test(`uncertain ${path} retry reuses its reference and successful save clears it`, async () => {
    const h = ready(), payload = {quantity_g: 100}; let save = false;
    h.route(call => {
      if (call.method === 'GET') return stockResponse(h);
      if (!save) throw new TypeError('response lost');
      return response({result: {id: 1}});
    });
    await h.app.inventoryMutation(path, payload, 'receipt-result', 'Saved');
    save = true; await h.app.inventoryMutation(path, payload, 'receipt-result', 'Saved');
    const attempts = h.calls.filter(call => call.method === 'POST');
    assert.equal(attempts[0].body.request_id, attempts[1].body.request_id);
    assert.equal(h.app.state.inventoryIdentities.has(path), false);
    assert.equal(h.app.state.inventoryBusy, 0);
    assert.equal(h.get('receipt-result').textContent, 'Saved');
  });
}

test('polling preserves a dirty recipe and blocks stale-version saves until explicit reload', async () => {
  const h = ready(), chicken = h.get('recipe-fields').querySelectorAll('input')[1];
  chicken.value = '450'; await chicken.fire('input');
  h.route(() => response(source(2, 500))); await h.app.refreshSource();
  assert.equal(h.app.state.recipeDraft.version, 1);
  assert.equal(h.app.state.recipeDraft.modifiers[0].ingredients[0].grams, 450);
  assert.equal(h.get('recipe-fields').querySelectorAll('input')[1], chicken);
  assert.equal(h.get('recipe-stale').hidden, false);
  assert.equal(h.get('save-recipe').disabled, true);
  await h.get('reload-recipe').fire('click');
  assert.equal(h.app.state.recipeDraft.version, 2);
  assert.equal(h.get('recipe-fields').querySelectorAll('input')[1].value, '500');
  assert.equal(h.get('recipe-stale').hidden, true);
  assert.equal(h.get('save-recipe').disabled, false);
});

test('a recipe conflict preserves unsaved quantities while fetching the latest saved version', async () => {
  const h = ready(), chicken = h.get('recipe-fields').querySelectorAll('input')[1];
  chicken.value = '450'; await chicken.fire('input');
  h.route(call => call.method === 'PUT'
    ? response({error: 'Recipe changed. Reload saved quantities.'}, 400)
    : response(source(2, 500)));
  await h.get('recipe-form').fire('submit');
  assert.equal(h.calls[0].body.version, 1);
  assert.equal(h.calls[0].body.modifiers[0].ingredients[0].grams, 450);
  assert.equal(h.app.state.recipeDraft.version, 1);
  assert.equal(h.app.state.recipeDirty, true);
  assert.equal(chicken.value, '450');
  assert.equal(h.get('recipe-stale').hidden, false);
  assert.match(h.get('recipe-result').textContent, /Recipe changed/);
  assert.equal(h.app.state.recipeBusy, false);
});

test('an overnight inventory response refreshes expiry labels even when the batch rows are unchanged', async () => {
  const h = ready(); h.app.state.stock.batches[0].expires_on = '2026-10-22';
  h.app.renderInventory();
  assert.match(h.get('batches-body').textContent, /Recorded date is today/);
  const nextDay = clone(h.app.state.stock); nextDay.today = '2026-10-23';
  h.route(() => response(nextDay)); await h.app.refreshInventory();
  assert.equal(h.app.state.config.today, '2026-10-22'); // Startup config remains the previous date.
  assert.match(h.get('batches-body').textContent, /Past recorded date/);
  assert.doesNotMatch(h.get('batches-body').textContent, /Recorded date is today/);
});

test('request timeout releases its timer and produces a retryable connection message', async () => {
  const h = ready();
  h.route(call => new Promise((resolve, reject) => call.options.signal.addEventListener('abort', () => {
    const error = new Error('aborted'); error.name = 'AbortError'; reject(error);
  })));
  const request = h.app.request('', '/api/state').catch(error => error);
  await h.advance(4001);
  assert.match((await request).message, /connection took too long/);
});

test('supplier labels and menu names remain text rather than creating HTML elements', () => {
  const h = ready(), label = '<img src=x onerror="alert(1)">';
  h.app.state.source.menu[0].name = label;
  h.app.state.stock.batches[0].label = label;
  h.app.renderMenu(); h.app.renderInventory();
  assert.match(h.get('batches-body').textContent, /<img src=x/);
  assert.equal(h.get('batches-body').querySelectorAll('img').length, 0);
  assert.match(h.get('order-dish').textContent, /<img src=x/);
  assert.equal(h.get('order-dish').querySelectorAll('img').length, 0);
});
