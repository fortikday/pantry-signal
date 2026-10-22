// Executes the actual public website script against a simulated DOM.
// This is a behavior test, not a real browser or a responsive-layout test.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {parseDocument} = require('../dom.cjs');

const root = path.join(__dirname, '../../docs');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'site.js'), 'utf8');

function createSite(options = {}) {
  const document = parseDocument(html), network = [], storage = [], timers = [];
  document.documentElement = document.querySelector('html');
  document.body = document.querySelector('body');
  const denyNetwork = (...args) => { network.push(args); throw new Error('The public demo must not send restaurant data to a service'); };
  const fakeStorage = {
    getItem(key) { storage.push(['read', key]); return null; },
    setItem(key, value) { storage.push(['write', key, value]); },
    removeItem(key) { storage.push(['remove', key]); },
    clear() { storage.push(['clear']); },
  };
  const windowEvents = new Map();
  const motionListeners = [];
  const motionPreference = {
    matches: Boolean(options.reducedMotion),
    addEventListener(type, callback) { if (type === 'change') motionListeners.push(callback); },
  };
  const context = vm.createContext({
    document, console, Date, structuredClone,
    location: {protocol: 'https:', hostname: 'fortikday.github.io', pathname: '/pantry-signal/'},
    fetch: denyNetwork,
    XMLHttpRequest: function () { denyNetwork('XMLHttpRequest'); },
    WebSocket: function () { denyNetwork('WebSocket'); },
    localStorage: fakeStorage, sessionStorage: fakeStorage,
    navigator: {sendBeacon: denyNetwork},
    matchMedia: query => query === '(prefers-reduced-motion: reduce)'
      ? motionPreference : {matches: false, addEventListener() {}},
    addEventListener(type, callback) {
      if (!windowEvents.has(type)) windowEvents.set(type, []);
      windowEvents.get(type).push(callback);
    },
    setTimeout(callback) { timers.push(callback); return timers.length; },
    clearTimeout() {},
    requestAnimationFrame(callback) { callback(0); return 1; },
  });
  context.window = context;
  vm.runInContext(script, context, {filename: 'docs/site.js'});
  return {
    api: context.PantryDemo, document, network, storage,
    get(id) {
      const element = document.getElementById(id);
      if (!element) throw new Error('Missing actual website element: ' + id);
      return element;
    },
    async click(id) {
      const element = this.get(id);
      if (element.disabled) throw new Error('Cannot click disabled control: ' + id);
      await element.fire('click');
    },
    run: code => vm.runInContext(code, context),
    async fireWindow(type, detail = {}) {
      for (const callback of windowEvents.get(type) || []) await callback({type, ...detail});
    },
    async setReducedMotion(matches) {
      motionPreference.matches = Boolean(matches);
      for (const callback of motionListeners) await callback({matches: motionPreference.matches});
    },
    flushTimers() { while (timers.length) timers.shift()(); },
  };
}
const plain = value => JSON.parse(JSON.stringify(value));
module.exports = {createSite, plain, html, root};
