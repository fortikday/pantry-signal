// Test-only DOM simulation. It does not implement browser layout or accessibility.
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

function parseDocument(html) {
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

module.exports = {Element, parseDocument};
