// A deliberately small DOM simulation, not a browser or a layout/accessibility test.
// It loads the real HTML and executes its unmodified inline script with fake I/O.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../../index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const {parseDocument} = require('../dom.cjs');

function response(body, status = 200) { return {ok: status >= 200 && status < 300, status, json: async () => body}; }
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; }
const clone = value => JSON.parse(JSON.stringify(value));

function createApp() {
  const document = parseDocument(html), calls = [], timers = new Map();
  let now = Date.parse('2026-10-22T10:00:00Z'), timerId = 0, nextUuid = 0;
  let transport = () => { throw new Error('Unexpected request: install a transport before making requests'); };
  class ClockDate extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  const context = vm.createContext({
    document, location: {protocol: 'file:'}, Date: ClockDate, AbortController,
    TypeError, console,
    window: {crypto: {randomUUID: () => `00000000-0000-4000-8000-${String(++nextUuid).padStart(12, '0')}`}},
    fetch: async (url, options) => {
      const call = {url, method: options.method, body: options.body ? JSON.parse(options.body) : null, options};
      calls.push(call); return transport(call);
    },
    setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, {callback, at: now + delay}); return id; },
    clearTimeout: id => timers.delete(id),
    setInterval: (callback, delay) => { const id = ++timerId; timers.set(id, {callback, at: now + delay, interval: delay}); return id; },
    clearInterval: id => timers.delete(id),
  });
  // The app's file: startup exits before network I/O. Tests may call start() explicitly.
  vm.runInContext(script + `\n;globalThis.testApi={state,refreshSource,refreshInventory,renderConnection,renderInventory,renderMenu,renderKitchen,stockCountBlockReason,transition,inventoryMutation,request,start};`, context, {filename: 'index.html:inline-script'});
  return {
    document, calls, app: context.testApi,
    get: id => { const element = document.getElementById(id); if (!element) throw new Error('Missing actual HTML element: ' + id); return element; },
    route: callback => { transport = callback; },
    now: () => now,
    setNow: value => { now = typeof value === 'string' ? Date.parse(value) : value; },
    async advance(milliseconds) {
      const end = now + milliseconds;
      for (;;) {
        const next = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        const [id, timer] = next; now = timer.at;
        if (timer.interval) timer.at += timer.interval; else timers.delete(id);
        timer.callback(); await flush();
      }
      now = end; await flush();
    },
    run: code => vm.runInContext(code, context),
  };
}
// One host event-loop turn drains promise chains across the VM boundary without
// waiting on an unresolved fetch or relying on an arbitrary count of microtasks.
async function flush() { await new Promise(resolve => setImmediate(resolve)); }

module.exports = {createApp, response, deferred, clone, flush};
