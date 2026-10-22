// A deliberately small DOM simulation, not a browser or a layout/accessibility test.
// It loads the real HTML and executes its unmodified inline script with fake I/O.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../../index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const decode = text => text.replace(/&(amp|lt|gt|quot|#39);/g,
  (_, key) => ({amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'"})[key]);

class Element {
  constructor(tag, document) {
    this.tagName = tag.toUpperCase();
    this.ownerDocument = document;
    this.children = [];
    this.parentElement = null;
    this.attributes = {};
    this.dataset = {};
    this.listeners = new Map();
    this.className = '';
    this.hidden = this.disabled = this.checked = false;
    this._value = undefined;
    this._text = '';
    this.valid = true;
    this.classList = {
      contains: value => this.className.split(/\s+/).includes(value),
      add: (...values) => values.forEach(value => this.classList.toggle(value, true)),
      remove: (...values) => values.forEach(value => this.classList.toggle(value, false)),
      toggle: (value, force) => {
        const values = new Set(this.className.split(/\s+/).filter(Boolean));
        const present = force === undefined ? !values.has(value) : Boolean(force);
        if (present) values.add(value); else values.delete(value);
        this.className = [...values].join(' ');
        return present;
      },
    };
  }
  get id() { return this.attributes.id || ''; }
  set id(value) { this.attributes.id = String(value); }
  get value() {
    if (this._value !== undefined) return this._value;
    if (this.tagName === 'SELECT') return this.children.find(x => x.tagName === 'OPTION')?.value || '';
    return this.tagName === 'OPTION' ? this.textContent : '';
  }
  set value(value) { this._value = String(value); }
  get textContent() { return this._text + this.children.map(x => x.textContent).join(''); }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  append(...children) {
    for (const child of children) {
      if (typeof child === 'string') {
        const node = new Element('#text', this.ownerDocument); node._text = child; this.append(node);
      } else { child.parentElement = this; this.children.push(child); }
    }
  }
  replaceChildren(...children) {
    this.children.forEach(child => { child.parentElement = null; });
    this.children = []; this._text = ''; this._value = this.tagName === 'SELECT' ? undefined : this._value;
    this.append(...children);
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === 'class') this.className = String(value);
    if (name === 'value') this.value = value;
    if (['hidden', 'disabled', 'checked'].includes(name)) this[name] = true;
    if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, x) => x.toUpperCase())] = String(value);
  }
  getAttribute(name) { return name === 'class' ? this.className : this.attributes[name] ?? null; }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  async fire(type) {
    const event = {type, target: this, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; }};
    await Promise.all((this.listeners.get(type) || []).map(listener => listener(event)));
    return event;
  }
  reportValidity() { return this.valid; }
  querySelectorAll(selector) { return select(this, selector); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function matches(element, selector) {
  if (!selector) return false;
  let remainder = selector;
  const tag = remainder.match(/^[a-z][\w-]*/i);
  if (tag && element.tagName !== tag[0].toUpperCase()) return false;
  if (tag) remainder = remainder.slice(tag[0].length);
  for (const token of remainder.matchAll(/([.#])([\w-]+)|\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]|:(checked)/g)) {
    if (token[1] === '#' && element.id !== token[2]) return false;
    if (token[1] === '.' && !element.classList.contains(token[2])) return false;
    if (token[3]) {
      const value = element.getAttribute(token[3]) ?? element[token[3]];
      if (token[4] === undefined ? value == null : String(value) !== token[4]) return false;
    }
    if (token[5] && !element.checked) return false;
  }
  return true;
}
function select(root, selector) {
  const groups = selector.split(',').map(group => group.trim().split(/\s+/));
  const output = [];
  function visit(element) {
    for (const parts of groups) {
      if (!matches(element, parts.at(-1))) continue;
      let parent = element.parentElement, index = parts.length - 2;
      while (index >= 0 && parent) {
        if (matches(parent, parts[index])) index--;
        parent = parent.parentElement;
      }
      if (index < 0) { output.push(element); break; }
    }
    element.children.forEach(visit);
  }
  root.children.forEach(visit);
  return output;
}

function parseDocument() {
  const document = new Element('document', null); document.ownerDocument = document; document.hidden = false;
  document.createElement = tag => new Element(tag, document);
  document.getElementById = id => document.querySelector('#' + id);
  const stack = [document];
  const markup = html.split('<script>')[0].replace(/<style>[\s\S]*?<\/style>/g, '');
  for (const token of markup.matchAll(/<!--[\s\S]*?-->|<![^>]*>|<\/[\w-]+\s*>|<[\w-]+\b[^>]*>|[^<]+/g)) {
    const value = token[0];
    if (value.startsWith('<!')) continue;
    if (value.startsWith('</')) {
      const tag = value.slice(2, -1).trim().toUpperCase();
      const index = stack.findLastIndex(element => element.tagName === tag);
      if (index > 0) stack.length = index;
    } else if (value.startsWith('<')) {
      const tag = value.match(/^<([\w-]+)/)[1], element = document.createElement(tag);
      const attributes = value.slice(tag.length + 1, -1);
      for (const attr of attributes.matchAll(/([\w:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g))
        element.setAttribute(attr[1], decode(attr[2] ?? attr[3] ?? attr[4] ?? ''));
      stack.at(-1).append(element);
      if (!/^(area|base|br|col|embed|hr|img|input|link|meta|param|source|track|wbr)$/i.test(tag)) stack.push(element);
    } else { stack.at(-1).append(decode(value)); }
  }
  return document;
}

function response(body, status = 200) { return {ok: status >= 200 && status < 300, status, json: async () => body}; }
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; }
const clone = value => JSON.parse(JSON.stringify(value));

function createApp() {
  const document = parseDocument(), calls = [], timers = new Map();
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
