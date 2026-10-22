const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {createSite, plain, root} = require('./harness.cjs');

for (const reducedMotion of [false, true]) {
  test(`the app recording stays a still image until requested (reduced motion ${reducedMotion})`, () => {
    const site = createSite({reducedMotion}), image = site.get('walkthrough-image');
    const source = image.getAttribute('src');
    assert.match(source, /app-order\.png$/);
    assert.match(image.dataset.walkthroughSrc, /app-walkthrough\.gif$/);
    assert.equal(site.get('walkthrough-play').getAttribute('aria-pressed'), 'false');
    assert.equal(site.get('walkthrough-play').getAttribute('aria-controls'), 'walkthrough-image');
    assert.equal(site.get('walkthrough-status').getAttribute('aria-live'), 'polite');
    site.flushTimers();
    assert.equal(image.getAttribute('src'), source);
    assert.equal(site.network.length, 0);
    assert.match(site.get('walkthrough-status').textContent, /still image|only when/i);
  });
}

test('Play starts the recording, Stop restores the poster, and replay starts a fresh recording', async () => {
  const site = createSite(), image = site.get('walkthrough-image'), button = site.get('walkthrough-play');
  const poster = image.getAttribute('src'), before = plain(site.api.getState());
  await site.click('walkthrough-play');
  const firstPlayback = image.getAttribute('src');
  assert.match(firstPlayback, /app-walkthrough\.gif\?play=\d+$/);
  assert.equal(button.getAttribute('aria-pressed'), 'true');
  assert.match(button.textContent, /stop/i);
  assert.match(site.get('walkthrough-status').textContent, /playing.*fictional/i);
  await site.click('walkthrough-play');
  assert.equal(image.getAttribute('src'), poster);
  assert.equal(button.getAttribute('aria-pressed'), 'false');
  assert.match(button.textContent, /play/i);
  await site.click('walkthrough-play');
  assert.notEqual(image.getAttribute('src'), firstPlayback);
  assert.deepEqual(plain(site.api.getState()), before); // Playback never edits the sample order.
  assert.equal(site.storage.length, 0);
});

test('enabling reduced motion stops an active recording and never restarts it automatically', async () => {
  const site = createSite(), image = site.get('walkthrough-image'), poster = image.getAttribute('src');
  await site.click('walkthrough-play'); await site.setReducedMotion(true);
  assert.equal(image.getAttribute('src'), poster);
  assert.equal(site.get('walkthrough-play').getAttribute('aria-pressed'), 'false');
  assert.match(site.get('walkthrough-status').textContent, /motion preference/i);
  await site.setReducedMotion(false);
  assert.equal(image.getAttribute('src'), poster);
  await site.setReducedMotion(true); await site.click('walkthrough-play');
  assert.equal(site.get('walkthrough-play').getAttribute('aria-pressed'), 'true'); // Explicit user choice may play.
});

test('a recording load failure returns to the screenshot and leaves a usable retry', async () => {
  const site = createSite(), image = site.get('walkthrough-image'), poster = image.getAttribute('src');
  await site.click('walkthrough-play'); await image.fire('error');
  assert.equal(image.getAttribute('src'), poster);
  assert.equal(site.get('walkthrough-play').getAttribute('aria-pressed'), 'false');
  assert.equal(site.get('walkthrough-play').disabled, false);
  assert.match(site.get('walkthrough-status').textContent, /could not be loaded.*try Play again/i);
  await site.click('walkthrough-play');
  assert.match(image.getAttribute('src'), /app-walkthrough\.gif\?play=\d+$/);
});

test('the real-app proof includes valid PNG screenshot files and a GIF recording file', () => {
  for (const name of ['app-order.png', 'app-stock.png']) {
    const bytes = fs.readFileSync(path.join(root, 'assets', name));
    assert.deepEqual([...bytes.subarray(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10]);
    assert.ok(bytes.length > 100);
  }
  const gif = fs.readFileSync(path.join(root, 'assets/app-walkthrough.gif'));
  assert.match(gif.subarray(0, 6).toString('ascii'), /^GIF8[79]a$/);
  assert.ok(gif.length > 100);
});
