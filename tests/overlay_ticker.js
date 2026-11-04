// Runs web/overlay.js against a tiny stand-in DOM with a fake clock, and feeds its socket follower events: the
// "Latest" ticker must stay at four rows through a burst (a raid, a follow-bot wave). Used by test_surfaces.py.
// node overlay_ticker.js <overlay.js> <motion: full|off>
'use strict';
const fs = require('fs');

let now = 0;
const timers = [];
global.setTimeout = (fn, ms) => { timers.push({ at: now + (ms || 0), fn }); return timers.length; };
global.setInterval = () => 0;
global.requestAnimationFrame = () => 0;
function advance(ms) {
  const end = now + ms;
  for (;;) {
    timers.sort((a, b) => a.at - b.at);
    const t = timers[0];
    if (!t || t.at > end) break;
    timers.shift();
    now = t.at;
    t.fn();
  }
  now = end;
}

class Node {
  constructor(tag) {
    this.tagName = tag; this.childNodes = []; this.parent = null; this.attrs = {}; this.dataset = {}; this.style = {};
    this._cls = new Set();
    const cls = this._cls;
    this.classList = { add: c => cls.add(c), remove: c => cls.delete(c), contains: c => cls.has(c),
      toggle: (c, on) => ((on ?? !cls.has(c)) ? cls.add(c) : cls.delete(c)) };
    this.style.setProperty = () => {};
  }
  set className(v) { this._cls.clear(); for (const c of String(v).split(/\s+/)) if (c) this._cls.add(c); }
  get className() { return [...this._cls].join(' '); }
  get children() { return this.childNodes.filter(c => c instanceof Node); }
  get lastElementChild() { const k = this.children; return k[k.length - 1] || null; }
  get firstChild() { return this.childNodes[0] || null; }
  get isConnected() { return true; }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
  toggleAttribute(k, on) { if (on) this.attrs[k] = ''; else delete this.attrs[k]; }
  _adopt(k) { if (k instanceof Node) { if (k.parent) k.remove(); k.parent = this; } return k; }
  append(...ks) { for (const k of ks) this.childNodes.push(this._adopt(k)); }
  prepend(...ks) { this.childNodes.unshift(...ks.map(k => this._adopt(k))); }
  replaceChildren(...ks) { for (const k of this.childNodes) if (k instanceof Node) k.parent = null; this.childNodes = []; this.append(...ks); }
  remove() { if (this.parent) { this.parent.childNodes = this.parent.childNodes.filter(c => c !== this); this.parent = null; } }
  querySelector() { return new Node('div'); }
}
global.Node = Node;
const made = [];
const box = new Node('main');
const root = new Node('html');
root.dataset.motion = process.argv[3] || 'full';
global.document = {
  documentElement: root,
  getElementById: () => box,
  createElement: tag => { const n = new Node(tag); made.push(n); return n; },
  createElementNS: (ns, tag) => new Node(tag),
};
global.location = { search: '?panels=followers&key=k', host: 'overlay.test', protocol: 'http:' };
global.fetch = () => Promise.resolve({ ok: false });
let sock = null;
global.WebSocket = class { constructor() { sock = this; } };

eval(fs.readFileSync(process.argv[2], 'utf8'));  // the page script, as the browser runs it

const ticker = made.find(n => n.tagName === 'ol' && n.classList.contains('ticker'));
const rows = () => ticker.children.length;
let n = 0;
const follow = () => sock.onmessage({ data: JSON.stringify({ type: 'follower', data: { kind: 'follow', user: `viewer_${n++}` } }) });
const out = {};
for (let i = 0; i < 4; i++) { follow(); advance(500); }
out.spaced = rows();
for (let i = 0; i < 20; i++) { follow(); advance(20); }  // a burst: 20 follows in 0.4 s
advance(1500);
out.burst = rows();
for (let i = 0; i < 5; i++) { follow(); advance(600); }  // later, ordinary follows again
advance(1500);
out.after = rows();
for (let i = 0; i < 30; i++) { follow(); advance(100 + (i * 397) % 1100); }  // a raid: 30 follows over ~20 s
advance(1500);
out.raid = rows();
console.log(JSON.stringify(out));
