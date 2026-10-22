const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {createSite, plain, html, root} = require('./harness.cjs');

const grams = value => value.toLocaleString('en-GB', {maximumFractionDigits: 0});
const assertQuantity = (site, id, value) => assert.equal(
  site.get(id).textContent.replace(/\s*g\s*$/, '').trim(), grams(value),
  `${id} should display ${value} g; got ${site.get(id).textContent}`);

test('the public demo starts with fictional stock and no restaurant order history', () => {
  const site = createSite(), state = site.api.getState();
  assert.equal(state.orderStatus, 'empty');
  assert.deepEqual(plain(state.inventory), {onHand: 5000, reserved: 0, used: 0});
  assert.equal(state.events.length, 0);
  assert.equal(state.paused, false);
  assertQuantity(site, 'demo-on-hand', 5000);
  assert.equal(site.get('demo-place').disabled, false);
  assert.equal(site.get('demo-prepare').disabled, true);
  assert.equal(site.get('demo-ready').disabled, true);
});

test('the visible Table 7 flow reserves 400 g, records usage on preparation, and never deducts again on ready', async () => {
  const site = createSite();
  await site.click('demo-place');
  assert.equal(site.api.getState().orderStatus, 'queued');
  assertQuantity(site, 'demo-on-hand', 5000);
  assertQuantity(site, 'demo-reserved', 400);
  assertQuantity(site, 'demo-available', 4600);
  await site.click('demo-prepare');
  assert.equal(site.api.getState().orderStatus, 'preparing');
  assertQuantity(site, 'demo-on-hand', 4600);
  assertQuantity(site, 'demo-reserved', 0);
  assertQuantity(site, 'demo-used', 400);
  const before = plain(site.api.getState().inventory);
  await site.click('demo-ready');
  assert.equal(site.api.getState().orderStatus, 'ready');
  assert.deepEqual(plain(site.api.getState().inventory), before);
  assert.equal(site.api.getState().appliedEventIds.length, site.api.getState().events.length);
});

test('pausing first keeps the displayed stock frozen while the kitchen completes its order', async () => {
  const site = createSite();
  await site.click('demo-sync');
  assert.equal(site.api.getState().paused, true);
  await site.click('demo-place'); await site.click('demo-prepare'); await site.click('demo-ready');
  const paused = site.api.getState();
  assert.equal(paused.orderStatus, 'ready');
  assert.deepEqual(plain(paused.inventory), {onHand: 5000, reserved: 0, used: 0});
  assert.equal(paused.appliedEventIds.length, 0);
  assert.equal(paused.events.length, 3);
  assert.match(site.get('demo-sync-status').textContent, /paused|cached|not current/i);
  assert.match(site.get('demo-sync-note').textContent, /paused|cached|not current|wait/i);
  assert.ok(site.get('demo-pending').textContent.includes('3'));
  await site.click('demo-sync');
  const caughtUp = site.api.getState();
  assert.equal(caughtUp.paused, false);
  assert.deepEqual(plain(caughtUp.inventory), {onHand: 4600, reserved: 0, used: 400});
  assert.equal(caughtUp.appliedEventIds.length, 3);
  assertQuantity(site, 'demo-on-hand', 4600);
});

test('pausing an already reserved order retains the reservation until catch-up applies preparation', async () => {
  const site = createSite(); await site.click('demo-place'); await site.click('demo-sync');
  await site.click('demo-prepare'); await site.click('demo-ready');
  assert.deepEqual(plain(site.api.getState().inventory), {onHand: 5000, reserved: 400, used: 0});
  assert.equal(site.api.getState().appliedEventIds.length, 1);
  await site.click('demo-sync');
  assert.deepEqual(plain(site.api.getState().inventory), {onHand: 4600, reserved: 0, used: 400});
});

test('cancelling a queued order releases its reservation without creating usage', async () => {
  const site = createSite(); await site.click('demo-place'); await site.click('demo-cancel');
  const state = site.api.getState();
  assert.equal(state.orderStatus, 'cancelled');
  assert.deepEqual(plain(state.inventory), {onHand: 5000, reserved: 0, used: 0});
  assert.equal(site.get('demo-prepare').disabled, true);
  assertQuantity(site, 'demo-used', 0);
});

test('queued cancellation while paused is replayed once together with its reservation', async () => {
  const site = createSite(); await site.click('demo-sync');
  await site.click('demo-place'); await site.click('demo-cancel'); await site.click('demo-sync');
  const state = site.api.getState();
  assert.equal(state.orderStatus, 'cancelled');
  assert.deepEqual(plain(state.inventory), {onHand: 5000, reserved: 0, used: 0});
  assert.equal(state.events.length, 2);
  assert.equal(state.appliedEventIds.length, 2);
});

test('replayed ready, preparation, and resume actions cannot consume ingredients twice', () => {
  const {api} = createSite();
  let state = api.initialState();
  for (const type of ['pause', 'place', 'prepare', 'ready', 'resume', 'resume', 'prepare', 'ready', 'resume'])
    state = api.reduce(state, {type});
  assert.deepEqual(plain(state.inventory), {onHand: 4600, reserved: 0, used: 400});
  assert.equal(state.orderStatus, 'ready');
  assert.equal(state.events.length, 3);
  assert.equal(new Set(state.appliedEventIds).size, state.appliedEventIds.length);
});

test('invalid kitchen actions cannot skip preparation or restore stock already used', () => {
  const {api} = createSite();
  let state = api.initialState();
  state = api.reduce(state, {type: 'ready'}); state = api.reduce(state, {type: 'prepare'});
  assert.equal(state.orderStatus, 'empty'); assert.equal(state.events.length, 0);
  state = api.reduce(state, {type: 'place'}); state = api.reduce(state, {type: 'ready'});
  assert.equal(state.orderStatus, 'queued');
  state = api.reduce(state, {type: 'prepare'}); state = api.reduce(state, {type: 'cancel'});
  assert.equal(state.orderStatus, 'preparing');
  assert.deepEqual(plain(state.inventory), {onHand: 4600, reserved: 0, used: 400});
});

test('reset discards every fictional event, pending change, and stock adjustment', async () => {
  const site = createSite(); await site.click('demo-place'); await site.click('demo-sync');
  await site.click('demo-prepare'); await site.click('demo-ready'); await site.click('demo-reset');
  assert.deepEqual(plain(site.api.getState()), plain(site.api.initialState()));
  assertQuantity(site, 'demo-on-hand', 5000);
  assertQuantity(site, 'demo-reserved', 0);
  assertQuantity(site, 'demo-used', 0);
  assert.equal(site.get('demo-place').disabled, false);
});

test('initial states and exposed snapshots cannot be mutated to change the live demo', () => {
  const {api} = createSite(), first = api.initialState(), second = api.initialState();
  first.inventory.onHand = 1; first.events.push({id: 99, type: 'fake'});
  assert.equal(second.inventory.onHand, 5000); assert.equal(second.events.length, 0);
  const exposed = api.getState(); exposed.inventory.onHand = 0; exposed.events.push({id: 99});
  assert.equal(api.getState().inventory.onHand, 5000); assert.equal(api.getState().events.length, 0);
});

test('arbitrary action sequences preserve ingredient conservation and unique event application', () => {
  const {api} = createSite(), actions = ['place', 'prepare', 'ready', 'cancel', 'pause', 'resume', 'reset'];
  let seed = 173, state = api.initialState();
  for (let i = 0; i < 800; i++) {
    seed = (seed * 48271) % 2147483647;
    state = api.reduce(state, {type: actions[seed % actions.length]});
    const {onHand, reserved, used} = state.inventory;
    assert.equal(onHand + used, 5000);
    assert.ok(onHand >= 0 && reserved >= 0 && reserved <= onHand);
    assert.ok(used === 0 || used === 400);
    assert.equal(new Set(state.events.map(event => event.id)).size, state.events.length);
    assert.equal(new Set(state.appliedEventIds).size, state.appliedEventIds.length);
    assert.ok(state.appliedEventIds.every(id => state.events.some(event => event.id === id)));
  }
});

test('the public demo never calls a backend or browser storage throughout its supported flows', async () => {
  const site = createSite();
  await site.click('demo-place'); await site.click('demo-prepare'); await site.click('demo-ready');
  await site.click('demo-reset'); await site.click('demo-sync'); await site.click('demo-place');
  await site.click('demo-cancel'); await site.click('demo-sync'); await site.click('demo-reset');
  site.flushTimers();
  assert.deepEqual(site.network, []); assert.deepEqual(site.storage, []);
  const reopened = createSite();
  assert.equal(reopened.api.getState().orderStatus, 'empty');
  assert.equal(reopened.api.getState().events.length, 0);
});

test('mobile navigation exposes its expanded state and opens and closes using its button', async () => {
  const site = createSite(), toggle = site.get('menu-toggle');
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(toggle.getAttribute('aria-controls'), 'site-navigation');
  await site.click('menu-toggle');
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  await site.click('menu-toggle');
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
});

test('every local website stylesheet, script, image, and page link resolves within the published folder', () => {
  const refs = [...html.matchAll(/(?:href|src)\s*=\s*["']([^"']+)["']/gi)].map(match => match[1]);
  for (const ref of refs) {
    if (/^(?:https?:|mailto:|tel:|data:|#)/i.test(ref)) continue;
    assert.equal(ref.startsWith('/'), false, `Use a repository-relative Pages URL: ${ref}`);
    const target = path.resolve(root, ref.split(/[?#]/)[0]);
    assert.ok(target.startsWith(path.resolve(root) + path.sep), `Asset escapes public folder: ${ref}`);
    assert.ok(fs.existsSync(target), `Missing public asset: ${ref}`);
  }
});
