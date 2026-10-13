'use strict';
/* HUD dashboard. All dynamic text goes in via textContent (h() below) — window
   titles, calendar events and headlines are untrusted strings. */

const TOKEN = document.querySelector('meta[name="token"]').content;
// '' when the page wasn't served by Vesper (placeholder left in), so it never reloads in a loop
const HUD_VERSION = (document.querySelector('meta[name="hud-version"]')?.content || '').replace(/^\{\{.*/, '');
const S = { data: {}, log: [], view: 'command', procSort: 'cpu', optimizer: null, openJobs: new Set(), newsTopic: null };

// ---------------------------------------------------------------- helpers
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  setAttrs(el, attrs);
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}
function s(tag, attrs, ...kids) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  setAttrs(el, attrs);
  for (const kid of kids.flat(Infinity)) if (kid) el.append(kid);
  return el;
}
function setAttrs(el, attrs) {
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.setAttribute('class', v);
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
}
/* replaceChildren() would print null/false as text; skip them. */
function fill(el, ...kids) { el.replaceChildren(...kids.flat(Infinity).filter(k => k != null && k !== false)); }
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { 'Content-Type': 'application/json', 'X-Assistant-Token': TOKEN, ...(opts.headers || {}) },
    body: opts.body && typeof opts.body !== 'string' ? JSON.stringify(opts.body) : opts.body,
  });
  if (!res.ok) throw new Error(`${res.status} ${path}`);
  return res.json();
}
const post = (path, body) => api(path, { method: 'POST', body: body || {} });

function fmtClock(d) { return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); }
function fmtTime(iso) { return fmtClock(new Date(iso)); }
/* "9:30–9:45 AM" when both ends share AM/PM, else "11:30 AM–12:15 PM". */
function fmtRange(a, b) {
  const x = fmtTime(a), y = fmtTime(b);
  const [xt, xm] = x.split(' '), [, ym] = y.split(' ');
  return xm && xm === ym ? `${xt}–${y}` : `${x}–${y}`;
}
function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h_ = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  if (h_) return `${h_}h ${String(m).padStart(2, '0')}m`;
  if (m) return `${m}m`;
  return `${sec}s`;
}
function fmtHours(sec) { return `${((sec || 0) / 3600).toFixed(1)}h`; }
function fmtRate(bps) {
  const u = ['B/s', 'KB/s', 'MB/s', 'GB/s']; let i = 0; let v = bps || 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v < 10 && i ? v.toFixed(1) : Math.round(v)} ${u[i]}`;
}
function ago(ts) {
  if (!ts) return '';
  const t = typeof ts === 'number' ? ts * 1000 : Date.parse(ts);
  const d = (Date.now() - t) / 1000;
  if (d < 60) return 'just now';
  if (d < 3600) return `${Math.round(d / 60)}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  return `${Math.round(d / 86400)}d ago`;
}
function compact(n) {
  if (n == null) return '—';
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (Math.abs(n) >= 1e4) return `${(n / 1e3).toFixed(1)}K`;
  return n.toLocaleString();
}
function sameDay(a, b) { return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate(); }
function empty(text, code) { return h('div', { class: 'empty' }, text, code ? h('code', {}, code) : null); }
function status(level, label) { return h('span', { class: `status ${level}` }, h('span', { class: 'dot' }), label); }

/* Source icons (16px, stroke) shared by toasts, the notification center and the palette. */
const ICON = {
  vesper: 'M8 1.5 13.6 4.75v6.5L8 14.5 2.4 11.25v-6.5zM5.5 6 8 10.5 10.5 6',
  calendar: 'M2.5 4h11v9.5h-11zM2.5 7h11M5.5 2.5v3M10.5 2.5v3',
  weather: 'M5 12.5h6.5a3 3 0 0 0 0-6 4.2 4.2 0 0 0-8 1.4A2.4 2.4 0 0 0 5 12.5z',
  mail: 'M2 4h12v8.5H2zM2 4.5l6 4.5 6-4.5',
  tasks: 'M2.5 3.5h11v9h-11zM5 8l2 2 4-4',
  timer: 'M8 4a5 5 0 1 0 0 10A5 5 0 0 0 8 4zM8 6.5V9l1.8 1.2M6.5 1.5h3',
  stream: 'M2 4h12v8H2zM6.5 6.2v3.6L9.6 8z',
  system: 'M2 3h12v8H2zM5.5 14h5M8 11v3',
  focus: 'M8 1.5v2.5M8 12v2.5M1.5 8H4M12 8h2.5M8 5.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z',
  people: 'M5.5 7a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM1.5 13a4 4 0 0 1 8 0M11 7.5a1.6 1.6 0 1 0 0-3.2M11.5 9.5a3.5 3.5 0 0 1 3 3.5',
  warn: 'M8 2 14.5 13.5h-13zM8 6.5v3.2M8 11.6v.2',
  ok: 'M3 8.5l3 3 7-7',
  view: 'M2 2.5h5v5H2zM9 2.5h5v5H9zM2 9.5h5v4H2zM9 9.5h5v4H9z',
  panel: 'M2.5 2.5h11v11h-11zM2.5 6h11',
  cmd: 'M3 5l3 3-3 3M8 11.5h5',
  recent: 'M8 2.5a5.5 5.5 0 1 0 5.5 5.5M8 5v3.2l2 1.3M13.5 2.5v3h-3',
  ask: 'M8 1.5 13.6 4.75v6.5L8 14.5 2.4 11.25v-6.5zM5.5 6 8 10.5 10.5 6',
  bolt: 'M9 1.5 3.5 9H8l-1 5.5L12.5 7H8z',
  gear: 'M8 5.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zM8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4',
};
const SOURCE_ICON = { calendar: 'calendar', meetings: 'people', people: 'people', weather: 'weather', inbox_zero: 'mail', gmail: 'mail',
  email: 'mail', tasks: 'tasks', planner: 'tasks', habits: 'tasks', automations: 'timer', break: 'focus', focus: 'focus', welcome: 'vesper',
  janitor: 'system', pulse: 'system', system: 'system', stream: 'stream', obs: 'stream', twitch: 'stream', highlight: 'stream', vesper: 'vesper' };
function icon(name) {
  return s('svg', { viewBox: '0 0 16 16', 'aria-hidden': 'true' }, s('path', { d: ICON[name] || ICON[SOURCE_ICON[name]] || ICON.vesper }));
}
const LEVEL_TONE = { urgent: 'error', critical: 'error', error: 'error', warning: 'warn', warn: 'warn', good: 'good', ok: 'good',
  normal: 'info', info: 'info', low: 'info', quiet: 'info', vesper: 'vesper' };

/* Toasts stack bottom-right (newest nearest the corner, at most 3), slide in, and leave on their own; hovering one
   holds it. toast(text) still works; toast(text, 'error') or toast(text, { level, source, life }) add a tone. */
function toast(text, opt) {
  const o = typeof opt === 'string' ? { level: opt } : (opt || {});
  const tone = LEVEL_TONE[o.level] || 'info';
  const box = $('#toasts');
  if (!box || !text) return;
  const life = o.life || (tone === 'error' ? 9000 : 6000);
  const close = h('button', { class: 't-x', 'aria-label': 'Dismiss' }, '✕');
  const el = h('div', { class: 'toast', 'data-level': tone, role: tone === 'error' ? 'alert' : 'status' },
    h('span', { class: 't-ic' }, icon(o.source || (tone === 'error' || tone === 'warn' ? 'warn' : tone === 'good' ? 'ok' : 'vesper'))),
    h('div', {}, o.source ? h('div', { class: 't-src' }, SOURCE_LABEL_N[o.source] || o.source.replace(/_/g, ' ')) : null, h('div', { class: 't-txt' }, text)),
    close, h('span', { class: 't-bar', 'aria-hidden': 'true' }));
  el.style.setProperty('--life', `${life}ms`);
  const dismiss = () => { clearTimeout(el._t); el.classList.add('out'); setTimeout(() => el.remove(), 200); };
  let left = life, started = Date.now();
  const arm = () => { started = Date.now(); el._t = setTimeout(dismiss, left); };
  el.addEventListener('mouseenter', () => { clearTimeout(el._t); left -= Date.now() - started; });
  el.addEventListener('mouseleave', arm);
  close.addEventListener('click', dismiss);
  box.prepend(el);
  const live = [...box.children].filter(t => !t.classList.contains('out'));
  live.forEach((t, i) => t.classList.toggle('older', i > 0));
  for (const old of live.slice(3)) { clearTimeout(old._t); old.classList.add('out'); setTimeout(() => old.remove(), 200); }
  arm();
}
const SOURCE_LABEL_N = { inbox_zero: 'Email', automations: 'Automation', nudges: 'Updates', calendar: 'Calendar', weather: 'Weather',
  break: 'Break', tasks: 'Tasks', welcome: 'Welcome back', focus: 'Focus', people: 'People', meetings: 'Meetings', janitor: 'PC',
  pulse: 'Pulse', vesper: 'Vesper', stream: 'Stream', obs: 'OBS', twitch: 'Twitch', highlight: 'Highlight', system: 'System' };

// Category -> colour: profiles take categorical slots in config order, then "other"; idle is neutral.
function categories() { return [...Object.keys(S.data.profiles || { work: 1, stream: 1 }), 'other']; }
function catColor(cat) {
  if (cat === 'idle') return 'var(--idle)';
  const i = categories().indexOf(cat);
  return `var(--series-${Math.min((i < 0 ? categories().length : i) + 1, 8)})`;
}
function catLabel(cat) { return (S.data.profiles?.[cat]?.label) || cat[0].toUpperCase() + cat.slice(1); }

// ---------------------------------------------------------------- tooltip
const tip = $('#tooltip');
function showTip(evt, rows, foot) {
  fill(tip,
    ...rows.map(r => h('div', { class: 'row' },
      r.color ? h('span', { class: 'ln', style: { background: r.color } }) : null,
      h('span', { class: 'v' }, r.value), r.label ? h('span', { class: 'l' }, r.label) : null)),
    foot ? h('div', { class: 'foot' }, foot) : null);
  tip.hidden = false;
  const pad = 14, w = tip.offsetWidth, hh = tip.offsetHeight;
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if (x + w > innerWidth - 8) x = evt.clientX - w - pad;
  if (y + hh > innerHeight - 8) y = evt.clientY - hh - pad;
  tip.style.left = `${Math.max(8, x)}px`; tip.style.top = `${Math.max(8, y)}px`;
}
function hideTip() { tip.hidden = true; }

// ---------------------------------------------------------------- charts
/* Single-series sparkline: 2px line, 10% wash, end dot with surface ring, crosshair hover. */
const DRAWN = new Set();  // sparklines that have already drawn in once (refreshes don't replay it)
function sparkline(container, points, { max, color = 'var(--series-1)', fmt = v => v, label = '', key = '' }) {
  const w = Math.max(container.clientWidth || 220, 60), ht = 40;
  const vals = points.map(p => p.v);
  const valid = vals.filter(v => v != null);
  const first = !!key && valid.length >= 2 && !DRAWN.has(key);
  if (first) DRAWN.add(key);
  const svgEl = s('svg', { class: `spark${first ? ' draw' : ''}`, viewBox: `0 0 ${w} ${ht}`, role: 'img', 'aria-label': `${label} trend` });
  container.replaceChildren(svgEl);
  if (valid.length < 2) return;
  const mx = max ?? Math.max(...valid, 1) * 1.1;
  const n = points.length;
  const x = i => 1 + (i / (n - 1)) * (w - 8);
  const y = v => ht - 3 - (Math.min(v ?? 0, mx) / mx) * (ht - 10);
  const d = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join('');
  svgEl.append(
    s('line', { x1: 0, x2: w, y1: ht - 3, y2: ht - 3, stroke: 'var(--baseline)', 'stroke-width': 1 }),
    s('path', { class: 's-area', d: `${d}L${x(n - 1)},${ht - 3}L${x(0)},${ht - 3}Z`, fill: color, opacity: 0.1 }),
    s('path', { class: 's-line', d, pathLength: 1, fill: 'none', stroke: color, 'stroke-width': 1.5, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }),
    s('circle', { class: 's-dot', cx: x(n - 1), cy: y(vals[n - 1]), r: 3.5, fill: color, stroke: 'var(--panel)', 'stroke-width': 2 }));
  const cross = s('line', { y1: 2, y2: ht - 3, stroke: 'var(--muted)', 'stroke-width': 1, visibility: 'hidden' });
  const hit = s('rect', { x: 0, y: 0, width: w, height: ht, fill: 'transparent' });
  svgEl.append(cross, hit);
  hit.addEventListener('pointermove', e => {
    const r = svgEl.getBoundingClientRect();
    const i = Math.round(((e.clientX - r.left) / r.width * w - 1) / (w - 8) * (n - 1));
    const p = points[Math.max(0, Math.min(n - 1, i))];
    cross.setAttribute('x1', x(points.indexOf(p))); cross.setAttribute('x2', x(points.indexOf(p)));
    cross.setAttribute('visibility', 'visible');
    showTip(e, [{ value: p.v == null ? '—' : fmt(p.v), label, color }], p.ts ? fmtClock(new Date(p.ts * 1000)) : '');
  });
  hit.addEventListener('pointerleave', () => { cross.setAttribute('visibility', 'hidden'); hideTip(); });
}

/* Day strip: one row, blocks per category, 2px surface gaps, per-block hover. */
function dayStrip(timeline, dayStart, width) {
  const w = Math.max(width || 600, 200), ht = 46, top = 6, bh = 22;
  const startH = 6, endH = 24;
  const t0 = dayStart + startH * 3600, t1 = dayStart + endH * 3600;
  const X = t => ((Math.min(Math.max(t, t0), t1) - t0) / (t1 - t0)) * w;
  const svgEl = s('svg', { class: 'strip', viewBox: `0 0 ${w} ${ht}`, role: 'img', 'aria-label': 'Activity timeline' });
  svgEl.append(s('rect', { x: 0, y: top, width: w, height: bh, fill: 'var(--grid)' }));
  for (const b of timeline || []) {
    const x0 = X(b.start), x1 = X(b.end);
    if (x1 - x0 < 0.5) continue;
    const r = s('rect', { x: x0 + 1, y: top, width: Math.max(x1 - x0 - 2, 1), height: bh, fill: catColor(b.category), tabindex: 0 });
    const tipFn = e => showTip(e, [{ value: fmtDur(b.end - b.start), label: catLabel(b.category), color: catColor(b.category) }],
      `${fmtClock(new Date(b.start * 1000))} – ${fmtClock(new Date(b.end * 1000))}`);
    r.addEventListener('pointermove', tipFn); r.addEventListener('pointerleave', hideTip);
    r.addEventListener('focus', e => { const bb = r.getBoundingClientRect(); tipFn({ clientX: bb.left, clientY: bb.bottom }); });
    r.addEventListener('blur', hideTip);
    svgEl.append(r);
  }
  for (let hr = startH; hr <= endH; hr += 3) {
    const x = X(dayStart + hr * 3600);
    svgEl.append(s('line', { x1: x, x2: x, y1: top + bh + 2, y2: top + bh + 5, stroke: 'var(--baseline)', 'stroke-width': 1 }));
    const anchor = hr === startH ? 'start' : hr === endH ? 'end' : 'middle';
    const lbl = s('text', { x, y: ht - 2, 'text-anchor': anchor, fill: 'var(--muted)', 'font-size': 10 });
    lbl.textContent = hr === 24 ? '12a' : hr === 12 ? '12p' : hr > 12 ? `${hr - 12}p` : `${hr}a`;
    svgEl.append(lbl);
  }
  const now = Date.now() / 1000;
  if (now > t0 && now < t1) svgEl.append(s('line', { x1: X(now), x2: X(now), y1: 2, y2: top + bh + 4, stroke: 'var(--ink)', 'stroke-width': 1.5 }));
  return svgEl;
}

/* A 270-degree instrument gauge: track, value arc, 28 ticks, a pointer, and the number in the middle.
   Built once per panel and updated in place, so the arc and pointer glide between samples. */
function gauge(label, key) {
  const ticks = [];
  for (let i = 0; i < 28; i++) {
    const a = (135 + (i / 27) * 270) * Math.PI / 180, major = i % 9 === 0 || i === 27;
    const r0 = major ? 52 : 53.5, r1 = 57;
    ticks.push(s('line', { class: `g-tick${major ? ' major' : ''}`, x1: 60 + Math.cos(a) * r0, y1: 60 + Math.sin(a) * r0, x2: 60 + Math.cos(a) * r1, y2: 60 + Math.sin(a) * r1 }));
  }
  const val = s('circle', { class: 'g-val', cx: 60, cy: 60, r: 46, pathLength: 100 });
  const needle = s('line', { class: 'g-needle', x1: 96, y1: 60, x2: 104, y2: 60 });
  const read = h('div', { class: 'g-read' });
  const sub = h('div', { class: 'g-sub' });
  const dial = h('div', { class: 'g-dial', role: 'img' },
    s('svg', { viewBox: '0 0 120 120', 'aria-hidden': 'true' }, s('circle', { class: 'g-track', cx: 60, cy: 60, r: 46, pathLength: 100 }), val, ...ticks, needle), read);
  const el = h('div', { class: 'gauge' }, dial, h('div', { class: 'g-lbl' }, label), sub);
  let said = 0;
  return {
    el,
    set(pct, subText) {
      const v = Math.max(0, Math.min(100, Number(pct) || 0));
      val.style.strokeDasharray = `${(v * 0.75).toFixed(2)} 100`;
      needle.style.transform = `rotate(${(v * 2.7).toFixed(1)}deg)`;
      el.dataset.level = v >= 92 ? 'crit' : v >= 80 ? 'warn' : '';
      FX.tick(read, `${Math.round(v)}%`, key);
      sub.textContent = subText || '';
      if (Date.now() - said > 5000) { dial.setAttribute('aria-label', `${label} ${Math.round(v)} percent`); said = Date.now(); }
    },
  };
}

function meter(pct, warnAt = 80, critAt = 92) {
  const cls = pct >= critAt ? 'crit' : pct >= warnAt ? 'warn' : '';
  return h('div', { class: `meter ${cls}`, role: 'meter', 'aria-valuenow': pct, 'aria-valuemin': 0, 'aria-valuemax': 100 },
    h('i', { style: { width: `${Math.min(100, pct)}%` } }));
}

// ---------------------------------------------------------------- panels
function panels(name) { return $$(`.panel[data-panel="${name}"]`); }
function body(p) { return $('.body', p); }
function meta(p, text) { const m = $('[data-slot="meta"]', p); if (m) m.replaceChildren(text instanceof Node ? text : document.createTextNode(text || '')); }

const HEALTH_CLASSES = [['network', 'Network drops', 'lower the bitrate 200–500 kbps'],
  ['encoder', 'Encoder skips', 'NVENC, a faster preset, or a lower resolution'],
  ['render', 'Render lag', 'cap the game’s FPS; the GPU is maxed']];
const HEALTH_LABEL = { ok: 'Clean', notice: 'Minor', warning: 'Visible', critical: 'Bad', unknown: '—' };
const HEALTH_STATUS = { ok: 'good', notice: 'good', warning: 'warning', critical: 'critical', unknown: 'idle' };

function streamHealth(health, live) {
  const out = [h('div', { class: 'sub-h' }, `Health · last ${Math.round((health.window_s || 60) / 60)} min`)];
  if (!live) {
    out.push(h('div', { class: 't2' }, 'Measured while you’re live: dropped frames split by cause, reconnects, and whether your mic reaches the stream.'));
  } else {
    out.push(h('table', { class: 't' }, h('tbody', {}, HEALTH_CLASSES.map(([key, label, fix]) => {
      const c = (health.classes || {})[key] || {};
      const bad = c.level === 'warning' || c.level === 'critical';
      return h('tr', {}, h('td', { class: 'fit' }, label), h('td', { class: 'r fit num' }, c.pct != null ? `${c.pct}%` : '—'),
        h('td', { class: 'fit' }, status(HEALTH_STATUS[c.level] || 'idle', HEALTH_LABEL[c.level] || '—')),
        h('td', { class: 'muted' }, bad ? fix : ''));
    }))));
  }
  const mic = health.mic || {};
  if (mic.source) {
    out.push(h('div', { class: 'kv' }, h('span', { class: 'k' }, 'Mic in OBS'),
      h('span', {}, mic.source, mic.peak_db != null ? h('span', { class: 'muted' }, ` · loudest ${mic.peak_db} dB in the last minute`) : null,
        mic.clipping ? h('span', {}, ' · ', status('warning', 'clipping')) : null)));
  }
  const alerts = (health.alerts || []).slice(0, 4);
  if (alerts.length) {
    out.push(h('div', { class: 'list' }, alerts.map(a => h('div', { class: 'item' },
      h('div', { class: 'fit' }, status({ critical: 'critical', warning: 'warning', good: 'good' }[a.level] || 'idle', ago(a.ts))),
      h('div', { class: 'main t3' }, a.text)))));
  }
  const pre = S.data.prestream;
  out.push(h('div', { class: 'controls' }, h('button', { class: 'btn', onclick: () => tool('prestream_check').then(r => { if (r && r.items) { S.data.prestream = r; render.obs(); } }) }, 'Run pre-stream check'),
    pre ? h('span', { class: 'muted', style: { alignSelf: 'center' } }, `last run ${ago(pre.ts)}`) : null));
  if (pre && pre.items) {
    out.push(h('table', { class: 't' }, h('tbody', {}, pre.items.map(i => h('tr', {},
      h('td', { class: 'fit' }, status({ good: 'good', warning: 'warning', critical: 'critical' }[i.level] || 'idle', i.name)),
      h('td', { class: 't2' }, i.detail))))));
  }
  return out;
}

const render = {
  stats() {
    const act = S.data.activity || {};
    const cats = act.by_category || {};
    const obs = S.data.obs || {};
    const proj = S.data.projects || {};
    const tasks = S.data.tasks || [];
    const events = (S.data.calendar?.events || []).filter(e => !e.all_day);
    const now = new Date();
    const next = events.find(e => new Date(e.start) > now);
    const commits = (proj.repos || []).reduce((a, r) => a + (r.commits_today || 0), 0);
    const focus = act.focus_sessions || [];
    let mode = 'all';
    const tile = (label, value, sub, hero) => h('div', { class: `stat${hero ? ' hero' : ''}${value === 'LIVE' ? ' live' : ''}` },
      h('div', { class: 'label' }, label), FX.tick(h('div', { class: 'value' }), value, `stat:${mode}:${label}`), sub ? h('div', { class: 'sub' }, sub) : null);
    for (const p of panels('stats')) {
      mode = p.dataset.mode;
      let tiles;
      if (mode === 'work') {
        tiles = [
          tile('Work today', fmtHours(cats.work), `${fmtHours(act.active_seconds)} active overall`, true),
          tile('Deep work', `${focus.reduce((a, f) => a + f.minutes, 0)}m`, focus.length ? `${focus.length} session(s), longest ${Math.max(...focus.map(f => f.minutes))}m` : 'no 25-min block yet'),
          tile('Switches / hour', act.switches_per_hour ?? '—', `${act.context_switches || 0} app switches`),
          tile('Commits today', commits, `${(proj.repos || []).filter(r => r.dirty_files).length} repos with uncommitted work`),
          tile('Open work tasks', tasks.filter(t => t.profile === 'work').length, tasks.find(t => t.profile === 'work')?.title || 'nothing queued'),
          tile('Next event', next ? fmtTime(next.start) : '—', next ? next.title : 'calendar clear'),
        ];
      } else if (mode === 'stream') {
        const st = obs.streaming || {};
        const tw = S.data.twitch || {};
        tiles = [
          tile('Status', st.active ? 'LIVE' : obs.connected ? 'Offline' : 'OBS off', st.active ? `for ${fmtDur((st.duration_ms || 0) / 1000)}` : obs.current_scene || '', true),
          tile('Bitrate', st.kbps ? `${compact(st.kbps)} kbps` : '—', st.active ? `congestion ${((st.congestion || 0) * 100).toFixed(0)}%` : 'not streaming'),
          tile('Dropped frames', st.total_frames ? `${st.dropped_pct}%` : '—', st.total_frames ? `${compact(st.dropped_frames)} of ${compact(st.total_frames)}` : ''),
          tile('OBS FPS', obs.stats ? obs.stats.fps : '—', obs.stats ? `${obs.stats.render_ms} ms render · CPU ${obs.stats.cpu}%` : ''),
          tile('Viewers', tw.live ? compact(tw.viewers) : '—', tw.enabled ? (tw.live ? tw.game || '' : 'offline') : 'Twitch not configured'),
          tile('Stream time today', fmtHours(cats.stream), 'tracked from active windows'),
        ];
      } else {
        tiles = [
          tile('Active today', fmtHours(act.active_seconds), act.first_active ? `since ${fmtClock(new Date(act.first_active * 1000))}` : 'no activity yet'),
          ...categories().filter(c => c !== 'other').map(c => tile(catLabel(c), fmtHours(cats[c]), c === 'work' && focus.length ? `${focus.length} deep-work session(s)` : '')),
          tile('Commits today', commits, `${(proj.claude || []).length} Claude projects active`),
          tile('Open tasks', tasks.length, tasks[0]?.title || 'list is clear'),
          tile('Next event', next ? fmtTime(next.start) : '—', next ? next.title : 'calendar clear'),
          tile('Stream', obs.streaming?.active ? 'LIVE' : obs.connected ? 'Offline' : 'OBS off', obs.current_scene || ''),
        ];
      }
      body(p).replaceChildren(h('div', { class: 'stats' }, tiles));
    }
  },

  vitals() {
    const sys = S.data.system;
    for (const p of panels('vitals')) {
      const b = body(p);
      if (!sys) { b._vit = null; fill(b, empty('Waiting for first sample…')); continue; }
      const hist = sys.history || [];
      const gpu = (sys.gpus || [])[0];
      const vmode = p.dataset.mode || 'all';
      if (!b._vit || !!b._vit.gpu !== !!gpu) {  // the gauges are built once, then glide between samples
        const g = { cpu: gauge('CPU', `g:${vmode}:cpu`), mem: gauge('RAM', `g:${vmode}:mem`) };
        if (gpu) { g.gpu = gauge('GPU', `g:${vmode}:gpu`); g.vram = gauge('VRAM', `g:${vmode}:vram`); }
        b._vit = { ...g, rest: h('div', {}) };
        fill(b, h('div', { class: 'gauges' }, Object.values(g).map(x => x.el)), b._vit.rest);
      }
      b._vit.cpu.set(sys.cpu.percent, sys.cpu.freq_mhz ? `${(sys.cpu.freq_mhz / 1000).toFixed(1)} GHz` : `${sys.cpu.threads}T`);
      b._vit.mem.set(sys.memory.percent, `${sys.memory.used_gb}/${sys.memory.total_gb} GB`);
      if (gpu) {
        b._vit.gpu.set(gpu.util ?? 0, gpu.temp_c != null ? `${Math.round(gpu.temp_c)}°C` : '');
        b._vit.vram.set(gpu.mem_total_mb ? (gpu.mem_used_mb / gpu.mem_total_mb) * 100 : 0, gpu.mem_total_mb ? `${(gpu.mem_used_mb / 1024).toFixed(1)}/${Math.round(gpu.mem_total_mb / 1024)} GB` : '');
      }
      const rows = [
        ['CPU', `${Math.round(sys.cpu.percent)}%`, `${sys.cpu.cores || '?'}C/${sys.cpu.threads}T${sys.cpu.freq_mhz ? ` · ${(sys.cpu.freq_mhz / 1000).toFixed(1)} GHz` : ''}`, hist.map(x => ({ v: x.cpu, ts: x.ts })), 100, v => `${Math.round(v)}%`],
        ['Memory', `${Math.round(sys.memory.percent)}%`, `${sys.memory.used_gb} / ${sys.memory.total_gb} GB`, hist.map(x => ({ v: x.mem, ts: x.ts })), 100, v => `${Math.round(v)}%`],
      ];
      if (gpu) rows.push(['GPU', `${Math.round(gpu.util ?? 0)}%`, `${gpu.name.replace('NVIDIA GeForce ', '')}${gpu.temp_c != null ? ` · ${Math.round(gpu.temp_c)}°C` : ''}${gpu.encoder_util != null ? ` · NVENC ${gpu.encoder_util}%` : ''}`, hist.map(x => ({ v: x.gpu, ts: x.ts })), 100, v => `${Math.round(v)}%`]);
      rows.push(['Network', fmtRate(sys.net.down_bps), `↑ ${fmtRate(sys.net.up_bps)}`, hist.map(x => ({ v: x.down, ts: x.ts })), undefined, fmtRate]);
      const frag = [];
      for (const [k, v, sub, pts, max, fmt] of rows) {
        const wrap = h('div', { class: 'spark-wrap' });
        frag.push(h('div', { class: 'vital' }, h('div', {}, h('div', { class: 'k' }, k), FX.tick(h('div', { class: 'v num' }), v, `v:${vmode}:${k}`), h('div', { class: 's' }, sub)), wrap));
        requestAnimationFrame(() => sparkline(wrap, pts, { max, fmt, label: k, key: `${vmode}:${k}` }));
      }
      const cores = h('div', { class: 'cores', role: 'img', 'aria-label': 'Per-core CPU load' },
        (sys.cpu.per_core || []).map((c, i) => {
          const col = h('span', { class: 'track' }, h('span', { style: { position: 'absolute', bottom: 0, left: 0, right: 0, height: `${Math.max(c, 2)}%` } }));
          col.addEventListener('pointermove', e => showTip(e, [{ value: `${Math.round(c)}%`, label: `core ${i}`, color: 'var(--series-1)' }]));
          col.addEventListener('pointerleave', hideTip);
          return col;
        }));
      const disks = (sys.disks || []).map(d => h('div', { class: 'disk' }, h('span', { class: 'ink2' }, d.mount), meter(d.percent, 85, 92),
        h('span', { class: 'muted num' }, `${Math.round(d.total_gb - d.used_gb)} GB free`)));
      const procs = sys.processes ? sys.processes[S.procSort === 'cpu' ? 'by_cpu' : 'by_mem'] : [];
      const seg = h('span', { class: 'seg' }, ['cpu', 'mem'].map(k => h('button', {
        'aria-pressed': String(S.procSort === k), onclick: () => { S.procSort = k; render.vitals(); },
      }, k.toUpperCase())));
      const table = h('table', { class: 't' }, h('thead', {}, h('tr', {}, h('th', {}, 'App'), h('th', { class: 'r' }, 'CPU'), h('th', { class: 'r' }, 'RAM'))),
        h('tbody', {}, procs.slice(0, 6).map(r => h('tr', {}, h('td', { title: r.name }, r.count > 1 ? `${r.name} ×${r.count}` : r.name),
          h('td', { class: 'r' }, `${r.cpu}%`), h('td', { class: 'r' }, r.mem_mb >= 1024 ? `${(r.mem_mb / 1024).toFixed(1)} GB` : `${r.mem_mb} MB`)))));
      fill(b._vit.rest, ...frag, cores,
        disks.length ? h('div', { class: 'sub-h' }, 'Drives') : null, ...disks,
        h('div', { class: 'sub-h', style: { display: 'flex', alignItems: 'center', gap: '8px' } }, 'Heaviest apps', seg), table,
        S.optimizer ? renderFindings(S.optimizer) : null);
    }
  },

  activity() {
    const act = S.data.activity;
    for (const p of panels('activity')) {
      const b = body(p);
      if (!act || !act.timeline) { fill(b, empty('Tracking starts when the app runs. Check back in a few minutes.')); continue; }
      const mode = p.dataset.mode;
      const dayStart = new Date(); dayStart.setHours(0, 0, 0, 0);
      const cats = act.by_category || {};
      const total = Object.values(cats).reduce((a, v) => a + v, 0) || 1;
      const order = [...categories(), 'idle'].filter(c => cats[c]);
      meta(p, `${fmtHours(act.active_seconds)} active`);
      const maxV = Math.max(...order.map(c => cats[c]), 1);
      const bars = order.map(c => h('div', { class: 'catrow' }, h('span', { class: 'ink2' }, catLabel(c)),
        h('div', { class: 'catbar' }, h('i', { style: { width: `${(cats[c] / maxV) * 78}%`, background: catColor(c) } }),
          h('span', {}, `${fmtDur(cats[c])} · ${Math.round(cats[c] / total * 100)}%`))));
      const legend = h('div', { class: 'legend' }, [...categories(), 'idle'].map(c => h('span', { class: 'key' },
        h('span', { class: 'sw', style: { background: catColor(c) } }), catLabel(c))));
      const apps = (act.top_apps || []).filter(a => mode !== 'work' || a.category === 'work').slice(0, 6);
      const seen = {}; for (const a of apps) seen[a.app] = (seen[a.app] || 0) + 1;
      const appTable = h('table', { class: 't' }, h('tbody', {}, apps.map(a => h('tr', {},
        h('td', {}, h('span', { class: 'sw', style: { display: 'inline-block', width: '8px', height: '8px', borderRadius: '2px', background: catColor(a.category), marginRight: '6px' } }), a.app, seen[a.app] > 1 ? h('span', { class: 'muted' }, ` · ${catLabel(a.category).toLowerCase()}`) : null),
        h('td', { class: 'r' }, fmtDur(a.seconds))))));
      const extra = [];
      if (mode === 'work') {
        const fs = act.focus_sessions || [];
        extra.push(h('div', { class: 'sub-h' }, 'Deep-work sessions (25m+)'),
          fs.length ? h('div', { class: 'list' }, fs.map(f => h('div', { class: 'item' }, h('div', { class: 'main' },
            h('div', { class: 't1 num' }, `${fmtClock(new Date(f.start * 1000))} – ${fmtClock(new Date(f.end * 1000))}`),
            h('div', { class: 't2' }, `${f.minutes} minutes unbroken`)))))
            : empty('No unbroken 25-minute work block yet today.'),
          h('div', { class: 'sub-h' }, 'Context switching'),
          h('div', { class: 'ink2' }, act.switches_per_hour != null ? `${act.switches_per_hour} app switches per active hour (${act.context_switches} total). Under 20/hr is focused; 40+ is scattered.` : 'Not enough data yet.'));
      }
      fill(b, dayStrip(act.timeline, dayStart.getTime() / 1000, b.clientWidth), legend,
        h('div', { class: 'sub-h' }, 'By category'), ...bars,
        h('div', { class: 'sub-h' }, mode === 'work' ? 'Top work apps' : 'Top apps'), apps.length ? appTable : empty('No app time yet.'), ...extra);
    }
  },

  agenda() {
    const cal = S.data.calendar;
    for (const p of panels('agenda')) {
      const b = body(p);
      const days = Number(p.dataset.days || 1), profile = p.dataset.profile;
      if (!cal) { fill(b, empty('Loading calendars…')); continue; }
      if (!cal.calendars?.length) { fill(b, empty('No calendars connected. Add iCal links under ', 'calendars:'), empty('in config.yaml (see README → Calendars).')); continue; }
      const now = new Date();
      const limit = new Date(now); limit.setHours(0, 0, 0, 0); limit.setDate(limit.getDate() + days);
      let events = cal.events.filter(e => new Date(e.start) < limit && new Date(e.end) > new Date(now.getFullYear(), now.getMonth(), now.getDate()));
      if (profile) events = events.filter(e => e.profile === profile);
      const next = events.find(e => !e.all_day && new Date(e.start) > now);
      const items = [];
      if (next) {
        const mins = Math.round((new Date(next.start) - now) / 60000);
        const when = mins < 90 ? `in ${mins} min` : mins < 12 * 60 ? `in ${fmtDur(mins * 60)}`
          : `${sameDay(new Date(next.start), now) ? 'at' : new Date(next.start).toLocaleDateString([], { weekday: 'short' })} ${fmtTime(next.start)}`;
        items.push(h('div', { class: 'countdown' }, 'Next: ', h('b', {}, next.title), ` ${when}`));
      }
      let lastDay = '';
      for (const e of events) {
        const st = new Date(e.start), en = new Date(e.end);
        const dayKey = st.toDateString();
        if (days > 1 && dayKey !== lastDay) {
          lastDay = dayKey;
          items.push(h('div', { class: 'dayhdr' }, sameDay(st, now) ? 'Today' : st.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' })));
        }
        const cls = e.all_day ? '' : en < now ? 'past' : st <= now ? 'now' : '';
        items.push(h('div', { class: `ev ${cls}` },
          h('div', { class: 'when' }, e.all_day ? 'All day' : fmtRange(e.start, e.end)),
          h('span', { class: 'cd', style: { background: e.color } }),
          h('div', {}, h('div', { class: 'title' }, e.title), h('div', { class: 'sub' }, [e.calendar, e.location].filter(Boolean).join(' · ')))));
      }
      if (!events.length) items.push(empty(profile ? `Nothing on ${catLabel(profile).toLowerCase()} calendars.` : 'Nothing scheduled.'));
      const cals = cal.calendars.filter(c => !profile || c.profile === profile);
      meta(p, `${events.filter(e => !e.all_day).length} events`);
      const legend = cals.length > 1 ? h('div', { class: 'legend' }, cals.map(c => h('span', { class: 'key' },
        h('span', { class: 'sw', style: { background: c.color, borderRadius: '50%' } }), c.name,
        c.error ? status('critical', 'error') : c.missing ? status('idle', 'not set up') : null))) : null;
      const errs = cals.filter(c => c.error).map(c => h('div', { class: 'empty' }, status('critical', `${c.name}: ${c.error}`)));
      const missing = cals.filter(c => c.missing && !c.error);
      const hint = missing.length ? empty(`Not connected yet: paste each calendar's private iCal link into .env as `,
        missing.map(c => c.missing).join(', ')) : null;
      fill(b, h('div', { class: 'agenda' }, items), legend, ...errs, hint);
    }
  },

  obs() {
    const obs = S.data.obs;
    for (const p of panels('obs')) {
      const b = body(p);
      const full = p.dataset.size === 'full';
      if (!obs) { fill(b, empty('Checking OBS…')); continue; }
      if (!obs.connected) {
        meta(p, h('span', { class: 'badge-live off' }, 'OFFLINE'));
        fill(b, empty(obs.enabled === false ? 'OBS control is disabled in config.' : (obs.error || 'OBS not reachable.')),
          empty('In OBS: Tools → WebSocket Server Settings → Enable, then put the password in .env as ', 'OBS_PASSWORD'),
          h('div', { class: 'controls' }, h('button', { class: 'btn', onclick: () => command('open obs') }, 'Launch OBS')));
        continue;
      }
      const st = obs.streaming, rec = obs.recording;
      meta(p, h('span', { class: `badge-live ${st.active ? '' : 'off'}` }, st.active ? 'LIVE' : 'OFFLINE'));
      // Judge health on the last minute (what's happening now), not the whole stream's totals.
      const health = obs.health || {};
      const levels = Object.values(health.classes || {}).map(c => c.level);
      const recent = levels.includes('critical') ? 'critical' : levels.includes('warning') ? 'warning' : levels.some(l => l === 'ok' || l === 'notice') ? 'good' : null;
      const worst = health.reconnecting ? 'critical' : recent
        || (st.dropped_pct >= 2 ? 'critical' : st.dropped_pct >= 0.5 ? 'warning' : 'good');
      const dropLevel = [worst, health.reconnecting ? 'Reconnecting' : { critical: 'Dropping', warning: 'Watch', good: 'Healthy' }[worst]];
      const top = h('div', { class: 'obs-top' },
        h('div', {}, h('div', { class: 'muted', style: { fontSize: '12px' } }, 'Program scene'), h('div', { style: { fontSize: '18px', fontWeight: 600 } }, obs.current_scene || '—')),
        st.active ? h('div', { class: 'tc' }, (st.timecode || '').split('.')[0]) : null,
        st.active ? status(...dropLevel) : null,
        rec.active ? h('span', { class: 'pill' }, rec.paused ? '⏸ REC paused' : `● REC ${(rec.timecode || '').split('.')[0]}`) : null);
      const scenes = h('div', { class: `scenes ${full ? '' : 'mini'}` }, obs.scenes.map(sc => h('button', {
        class: `scene ${sc === obs.current_scene ? 'on' : ''}`, onclick: () => tool('obs_switch_scene', { scene: sc }),
      }, sc)));
      const parts = [top, scenes];
      if (full) {
        const ctl = (label, action, danger, ask) => h('button', {
          class: `btn ${danger ? 'danger' : ''}`,
          onclick: () => { if (!ask || confirm(ask)) tool('obs_control', { action }); },
        }, label);
        parts.push(h('div', { class: 'controls' },
          st.active ? ctl('■ End stream', 'stop_stream', true, 'End the stream?') : ctl('● Go live', 'start_stream', true, 'Go live now?'),
          rec.active ? ctl('Stop recording', 'stop_recording', false, 'Stop recording?') : ctl('Start recording', 'start_recording'),
          rec.active ? ctl(rec.paused ? 'Resume' : 'Pause', rec.paused ? 'resume_recording' : 'pause_recording') : null,
          ctl('Clip replay', 'save_replay'), ctl('Start replay buffer', 'start_replay_buffer'), ctl('Virtual cam', 'start_virtualcam')));
        if (obs.audio?.length) {
          parts.push(h('div', { class: 'sub-h' }, 'Audio'), h('div', { class: 'audio' }, obs.audio.map(a => h('button', {
            class: a.muted ? 'muted' : '', onclick: () => tool('obs_set_mute', { source: a.name }), title: a.muted ? 'Muted — click to unmute' : 'Live — click to mute',
          }, a.name))));
        }
        parts.push(...streamHealth(health, st.active));
        const stt = obs.stats || {};
        parts.push(h('div', { class: 'sub-h' }, 'Encoder & render'), h('table', { class: 't' }, h('tbody', {},
          [['Bitrate', st.kbps ? `${compact(st.kbps)} kbps` : '—'], ['Dropped (network)', `${compact(st.dropped_frames)} / ${compact(st.total_frames)} (${st.dropped_pct}%)`],
            ['Skipped (encoder)', `${compact(stt.encoder_skipped)} / ${compact(stt.encoder_total)}`], ['Missed (render)', `${compact(stt.render_skipped)} / ${compact(stt.render_total)}`],
            ['FPS / render time', `${stt.fps} / ${stt.render_ms} ms`], ['OBS CPU / RAM', `${stt.cpu}% / ${compact(stt.memory_mb)} MB`],
            ['Congestion', st.active ? `${((st.congestion || 0) * 100).toFixed(0)}%` : '—']].map(([k, v]) => h('tr', {}, h('td', { class: 'ink2' }, k), h('td', { class: 'r' }, v))))));
      }
      fill(b, ...parts);
    }
  },

  twitch() {
    const tw = S.data.twitch;
    for (const p of panels('twitch')) {
      const b = body(p);
      if (!tw || !tw.enabled) {
        fill(b, empty('Twitch isn’t set up. Register an app at dev.twitch.tv/console (Client Type: Public) and put its Client ID in .env as', 'TWITCH_CLIENT_ID'));
        continue;
      }
      const auth = tw.auth || {};
      const parts = [];
      if (auth.pending) parts.push(twitchLogin(auth.pending));
      else if (!auth.connected) {
        parts.push(h('div', { class: 'twitch-login' },
          h('div', { class: 't1' }, 'Connect Twitch'),
          h('div', { class: 't2' }, 'Clips, markers, title changes, shoutouts, polls, chat and live follows/subs/raids need a login. Or say “connect Twitch”.'),
          auth.error ? h('div', { class: 't2 warn' }, auth.error) : null,
          h('div', { class: 'controls' }, h('button', { class: 'btn primary', onclick: twitchConnect }, 'Connect Twitch'))));
      }
      if (tw.error) parts.push(empty(`Twitch error: ${tw.error}`));
      else if (!tw.needs_login) {
        parts.push(h('div', { class: 'obs-top' }, h('span', { class: `badge-live ${tw.live ? '' : 'off'}` }, tw.live ? 'LIVE' : 'OFFLINE'), h('span', { class: 'ink2' }, tw.channel)));
        if (tw.live) parts.push(h('div', { class: 'stats' },
          h('div', { class: 'stat' }, h('div', { class: 'label' }, 'Viewers'), h('div', { class: 'value' }, compact(tw.viewers))),
          h('div', { class: 'stat' }, h('div', { class: 'label' }, 'Uptime'), h('div', { class: 'value' }, fmtDur(tw.uptime_s)))));
        if (tw.title) parts.push(h('div', { class: 'item' }, h('div', { class: 'main' }, h('div', { class: 't1' }, tw.title), h('div', { class: 't2' }, tw.game || ''))));
      }
      if (auth.connected) {
        const ev = tw.events || {};
        const evState = { connected: status('good', 'Events live'), connecting: status('idle', 'Connecting…'),
          reconnecting: status('warning', 'Reconnecting…'), stopped: status('idle', 'Events off') }[ev.state] || null;
        parts.push(h('div', { class: 'twitch-bar' },
          status('good', `Connected as ${auth.login || '…'}`), evState,
          h('span', { class: 'grow' }),
          h('button', { class: 'btn small', title: 'Drop a stream marker here', onclick: () => tool('twitch_marker', {}).then(r => toast(r.ok ? 'Marker added.' : r.error)) }, 'Marker'),
          h('button', { class: 'btn small', title: 'Make a Twitch clip of the last 30 seconds', onclick: () => tool('twitch_clip', {}).then(r => toast(r.ok ? 'Clipped on Twitch.' : r.error)) }, 'Clip'),
          h('button', { class: 'btn small ghost', onclick: twitchLogout }, 'Disconnect')));
        const events = (tw.feed && tw.feed.events) || [];
        parts.push(h('div', { class: 'sub-h' }, 'Recent'));
        parts.push(events.length ? h('div', { class: 'list' }, events.slice(0, 8).map(twitchEvent)) : empty('No follows, subs or raids yet.'));
        if (ev.failed && Object.keys(ev.failed).length) {
          parts.push(h('div', { class: 't2 warn' }, `Not receiving: ${Object.keys(ev.failed).map(k => k.replace(/^channel\./, '').replace(/_/g, ' ')).join(', ')} (${Object.values(ev.failed)[0]})`));
        }
      }
      const hs = tw.highlights || [];
      if (hs.length) {
        parts.push(h('div', { class: 'sub-h' }, `Highlights · today`));
        parts.push(h('div', { class: 'list' }, hs.slice(0, 8).map(x => h('div', { class: 'item' },
          h('div', { class: 'main' },
            h('div', { class: 't1' }, x.uptime_s != null ? h('span', { class: 'stamp' }, stamp(x.uptime_s)) : null, x.reason),
            h('div', { class: 't2' }, `${ago(x.ts)}${x.marker ? ' · marker on the VOD' : ''}`))))));
      }
      fill(b, ...parts);
    }
  },

  projects() {
    const pr = S.data.projects;
    for (const p of panels('projects')) {
      const b = body(p);
      if (!pr) { fill(b, empty('Scanning projects…')); continue; }
      const claude = pr.claude || [], repos = pr.repos || [], gh = pr.github || {};
      meta(p, `${claude.length} Claude · ${repos.length} repos`);
      const parts = [h('div', { class: 'sub-h' }, 'Claude Code sessions')];
      parts.push(claude.length ? h('div', { class: 'list' }, claude.slice(0, 6).map(c => h('div', { class: 'item' },
        h('div', { class: 'main' },
          h('div', { class: 't1' }, c.project, ' ', c.branch ? h('span', { class: 'pill' }, c.branch) : null),
          h('div', { class: 't3 quote' }, c.latest_ask),
          h('div', { class: 't2' }, `${ago(c.last_activity)} · ${c.sessions} session${c.sessions === 1 ? '' : 's'} · ${c.prompts} prompts`)),
        h('button', { class: 'btn small', title: 'Hand Claude Code a task in this repo', onclick: () => delegate(c.project) }, 'Delegate')))) :
        empty('No Claude Code sessions in the last 30 days (reads ~/.claude/projects).'));
      parts.push(h('div', { class: 'sub-h' }, 'Local repos'));
      parts.push(repos.length ? h('table', { class: 't' },
        h('thead', {}, h('tr', {}, h('th', {}, 'Repo'), h('th', {}, 'Last commit'), h('th', { class: 'r' }, 'Today'), h('th', { class: 'r' }, 'Dirty'))),
        h('tbody', {}, repos.slice(0, 8).map(r => h('tr', {},
          h('td', { title: r.path }, r.name),
          h('td', { title: r.last_commit.message }, `${ago(r.last_commit.ts)} · ${r.last_commit.message}`),
          h('td', { class: 'r' }, r.commits_today || '·'),
          h('td', { class: 'r' }, r.dirty_files ? status('warning', String(r.dirty_files)) : '·'))))) :
        empty('Add folders under projects.scan_dirs in config.yaml to track repos.'));
      if (gh.enabled) {
        parts.push(h('div', { class: 'sub-h' }, 'GitHub · open PRs'));
        parts.push(gh.error ? empty(`GitHub: ${gh.error}`) : (gh.open_prs || []).length ? h('div', { class: 'list' }, gh.open_prs.slice(0, 5).map(x => h('div', { class: 'item' },
          h('div', { class: 'main' }, h('a', { class: 't1', href: x.url, target: '_blank', rel: 'noopener' }, x.title), h('div', { class: 't2' }, `${x.repo}${x.draft ? ' · draft' : ''} · ${ago(x.updated_at)}`))))) : empty('No open PRs.'));
      }
      fill(b, ...parts);
    }
  },

  tasks() {
    const tasks = S.data.tasks || [];
    const goals = S.data.goals || {};
    const profiles = Object.keys(S.data.profiles || {});
    for (const p of panels('tasks')) {
      const b = body(p);
      const profile = p.dataset.profile;
      const list = profile ? tasks.filter(t => t.profile === profile) : tasks;
      const parts = [];
      if (!profile) parts.push(...goalsBlock(goals), h('div', { class: 'sub-h' }, 'Tasks'));
      const input = h('input', { placeholder: 'Add a task…', 'aria-label': 'New task' });
      const sel = h('select', { 'aria-label': 'Priority' }, [1, 2, 3].map(n => h('option', { value: n, selected: n === 2 }, `P${n}`)));
      const profSel = profile ? null : h('select', { 'aria-label': 'Profile' }, [...profiles, 'personal'].map(x => h('option', { value: x }, catLabel(x))));
      const form = h('form', { class: 'addrow', onsubmit: async e => {
        e.preventDefault();
        if (!input.value.trim()) return;
        await post('/api/tasks', { title: input.value.trim(), priority: Number(sel.value), profile: profile || profSel.value });
        input.value = '';
        refreshTasks();
      } }, input, profSel, sel);
      parts.push(form);
      parts.push(list.length ? h('div', { class: 'list' }, list.slice(0, 10).map(t => h('label', { class: 'item task' },
        h('input', { type: 'checkbox', onchange: async () => { await api(`/api/tasks/${t.id}`, { method: 'PATCH', body: { status: 'done' } }); refreshTasks(); } }),
        h('div', { class: 'main' }, h('div', { class: 't1' }, t.title),
          h('div', { class: 't2' }, [`P${t.priority}`, profile ? null : catLabel(t.profile), t.due ? `due ${t.due}` : null].filter(Boolean).join(' · ')))))) :
        empty('Nothing open. Say “add task …” or type above.'));
      fill(b, ...parts);
    }
  },

  news() {
    const items = S.data.news?.headlines || [];
    for (const p of panels('news')) {
      const b = body(p);
      if (!items.length) { fill(b, empty('No headlines yet (feeds refresh every 15 min).')); continue; }
      const topics = [...new Set(items.map(i => i.topic))];
      const shown = S.newsTopic ? items.filter(i => i.topic === S.newsTopic) : items;
      const seg = topics.length > 1 ? h('span', { class: 'seg', style: { marginBottom: '6px' } }, [null, ...topics].map(tp => h('button', {
        'aria-pressed': String(S.newsTopic === tp), onclick: () => { S.newsTopic = tp; render.news(); },
      }, tp || 'all'))) : null;
      fill(b, seg, h('div', { class: 'list' }, shown.slice(0, 9).map(n => h('div', { class: 'item' }, h('div', { class: 'main' },
        h('a', { class: 't1', href: n.link, target: '_blank', rel: 'noopener' }, n.title),
        h('div', { class: 't2' }, `${n.source} · ${n.published ? ago(n.published) : ''}`))))));
    }
  },

  log() {
    for (const p of panels('log')) {
      const b = body(p);
      const box = h('div', { class: 'log' }, S.log.slice(-60).map(m => h('div', { class: `msg ${m.role}` }, m.text,
        h('span', { class: 'meta' }, [m.source === 'voice' ? 'voice' : m.source === 'heard' ? 'heard (no wake word)' : null,
          m.ms != null ? `${m.ms} ms` : null, m.ts ? fmtClock(new Date(m.ts * 1000)) : null].filter(Boolean).join(' · ')))));
      if (!S.log.length) box.append(empty(`Try: “${S.data.assistant?.name || 'Vesper'}, good morning” · “open discord” · “switch to BRB” · “where am I”`));
      fill(b, box);
      box.scrollTop = box.scrollHeight;
    }
  },

  jobs() {
    const jobs = S.data.jobs || [];
    for (const p of panels('jobs')) {
      const b = body(p);
      if (!jobs.length) { fill(b, empty('Nothing running. Say “have Claude write tests in <repo>” or “research …”.')); continue; }
      const level = { running: 'warning', queued: 'idle', done: 'good', failed: 'critical', cancelled: 'idle', interrupted: 'serious' };
      fill(b, h('div', { class: 'list' }, jobs.slice(0, 8).map(j => {
        const open = S.openJobs.has(j.id);
        return h('div', { class: 'item job' }, h('div', { class: 'main' },
          h('div', { class: 't1', style: { cursor: j.output ? 'pointer' : 'default' }, onclick: () => { open ? S.openJobs.delete(j.id) : S.openJobs.add(j.id); render.jobs(); } }, j.title),
          h('div', { class: 't2' }, status(level[j.status] || 'idle', j.status), ` · ${j.kind === 'claude_code' ? 'Claude Code' : 'research'}`,
            j.cwd ? ` · ${j.cwd.split(/[\\/]/).pop()}` : '', ` · ${ago(j.created)}`),
          open && j.output ? h('pre', {}, j.output) : null),
          j.status === 'running' ? h('button', { class: 'btn small danger', onclick: () => post(`/api/jobs/${j.id}/cancel`) }, 'Stop') : null);
      })));
    }
  },

  briefing() {
    const br = S.data.briefing;
    for (const p of panels('briefing')) {
      const b = body(p);
      if (!br) { fill(b, empty(`Say “${S.data.assistant?.name || 'Vesper'}, good morning” or click Good morning. It pulls calendars, yesterday's activity, projects, tasks and news.`)); continue; }
      fill(b, 
        h('div', { class: 'brief-head' }, br.headline || ''),
        h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '-6px', marginBottom: '10px' } },
          `${br.kind === 'recap' ? 'End-of-day recap' : 'Morning briefing'} · ${ago(br.created)} · ${br.generated_by === 'claude' ? 'planned by Claude' : br.generated_by === 'local' ? `planned by ${br.model || 'your local model'}` : 'built-in summary (connect an AI model for a real plan)'}`),
        br.top_moves?.length ? h('div', { class: 'moves' }, br.top_moves.map(m => h('div', { class: 'move' }, h('div', {},
          h('div', { style: { fontWeight: 600 } }, m.move), h('div', { class: 'w' }, [m.when, m.why].filter(Boolean).join(' · ')))))) : null,
        h('div', { class: 'sections' }, (br.sections || []).map(sec => h('div', {}, h('div', { class: 'sub-h' }, sec.title),
          h('ul', {}, sec.bullets.map(x => h('li', {}, x)))))),
        br.risks?.length ? h('div', { class: 'risks' }, h('div', { class: 'sub-h' }, 'Risks'),
          br.risks.map(r => h('div', { class: 'risk' }, status('warning', '!'), r))) : null);
    }
  },
};

const CONN_STATUS = { action: ['warning', 'Needs you'], missing: ['idle', 'Not found'], found: ['idle', 'Found'], connected: ['good', 'Connected'] };
const CHECK_STATUS = { fail: ['critical', 'Fail'], warn: ['warning', 'Warn'], pass: ['good', 'Pass'], skip: ['idle', 'Skip'] };

render.connections = function connections() {
  const d = S.data.discovery;
  paintSetupBadge();
  for (const p of panels('connections')) {
    const b = body(p);
    if (!d) { fill(b, empty('No scan yet. Click Rescan PC — it looks for apps, games, OBS, bookmarks, repos, mics and GPU.')); continue; }
    meta(p, `scanned ${ago(d.scanned_at)} · ${d.duration_s}s`);
    const counts = d.summary || {};
    const tiles = h('div', { class: 'stats', style: { marginBottom: '8px' } },
      ['action', 'connected', 'found', 'missing'].map(k => h('div', { class: 'stat' },
        h('div', { class: 'label' }, CONN_STATUS[k][1]), h('div', { class: 'value' }, counts[k] || 0))));
    const byArea = {};
    for (const f of d.findings) (byArea[f.area] = byArea[f.area] || []).push(f);
    const areas = Object.keys(byArea).sort((a, b) => {
      const rank = x => Math.min(...byArea[x].map(f => ['action', 'missing', 'found', 'connected'].indexOf(f.status)));
      return rank(a) - rank(b) || a.localeCompare(b);
    });
    fill(b, tiles, areas.map(area => h('div', { class: 'conn-area' }, h('div', { class: 'sub-h' }, area),
      byArea[area].map(f => h('div', { class: 'conn' }, status(...(CONN_STATUS[f.status] || CONN_STATUS.found)),
        h('div', {}, h('div', { class: 'n' }, f.name), f.detail ? h('div', { class: 'd' }, f.detail) : null,
          f.fix ? h('div', { class: 'f' }, f.fix) : null))))));
  }
};

const PART_STATUS = { ok: ['good', 'OK'], error: ['critical', 'Error'], stalled: ['warning', 'Stalled'], restarting: ['warning', 'Restarting'],
  starting: ['idle', 'Starting'], disabled: ['idle', 'Off'], stopped: ['idle', 'Stopped'] };

function unhealthyParts() { return (S.data.health || []).filter(p => ['error', 'stalled', 'restarting'].includes(p.state)); }

render.health = function health() {
  const parts = S.data.health || [];
  for (const p of panels('health')) {
    const b = body(p);
    if (!parts.length) { fill(b, empty('The watchdog starts with the app.')); continue; }
    const bad = unhealthyParts().length;
    meta(p, bad ? `${bad} need attention` : 'all healthy');
    fill(b, h('table', { class: 't' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Part'), h('th', {}, 'State'), h('th', {}, 'Last OK'), h('th', { class: 'r wide-only' }, 'Runs'),
        h('th', { class: 'r wide-only' }, 'Restarts'), h('th', {}, 'Last problem'))),
      h('tbody', {}, parts.map(x => h('tr', {},
        h('td', { class: 'fit' }, x.name), h('td', {}, status(...(PART_STATUS[x.state] || PART_STATUS.starting))),
        h('td', {}, x.last_ok ? ago(x.last_ok) : '—'), h('td', { class: 'r wide-only' }, x.kind === 'poller' ? x.runs : '·'),
        h('td', { class: 'r wide-only' }, x.restarts || '·'), h('td', { class: 'muted', title: x.last_error || '' }, x.last_error || '—'))))));
  }
  paintSetupBadge();
};

function paintSetupBadge() {
  const badge = $('#setup-badge');
  if (!badge) return;
  const todo = (S.data.discovery ? S.data.discovery.findings.filter(f => f.status === 'action').length : 0) + unhealthyParts().length;
  badge.textContent = todo; badge.classList.toggle('hidden', !todo);
}

const CAL_STEP = { prepare: 'Getting ready', noise: 'Room noise', wake: 'Wake word', speech: 'Your voice', enroll: 'Voice profile' };

render.voice = function voice() {
  const v = S.data.voice_profile;
  const cal = S.data.calibration;
  for (const p of panels('voice')) {
    const b = body(p);
    if (!v) { fill(b, empty('Loading…')); continue; }
    if (!v.enabled) { fill(b, empty('Voice is off (voice.enabled: false). Turn it on to calibrate.')); continue; }
    const running = v.calibrating || (cal && !['done', 'error', 'cancelled'].includes(cal.step));
    if (running && cal) {
      meta(p, 'calibrating');
      fill(b, h('div', { class: 'cal-step' }, `${CAL_STEP[cal.step] || cal.step}${cal.total > 1 ? ` · ${cal.index}/${cal.total}` : ''}`),
        h('div', { class: 'cal-prompt' }, cal.recording ? h('span', { class: 'rec-dot' }) : null, cal.prompt || '…'),
        h('div', { class: 'meter', id: 'cal-meter' }, h('i', { style: { width: `${Math.min(100, (cal.level || 0) / 30)}%` } })),
        h('div', { class: 'controls' }, h('button', { class: 'btn danger', onclick: () => post('/api/voice/calibrate/cancel') }, 'Cancel')));
      continue;
    }
    const prof = v.profile || {};
    meta(p, prof.enrolled ? 'enrolled' : 'not calibrated');
    const modes = h('span', { class: 'seg' }, ['off', 'log', 'strict'].map(m => h('button', {
      'aria-pressed': String(prof.mode === m), title: { off: 'Answer anyone', log: 'Score voices but never block', strict: 'Ignore voices that are not yours' }[m],
      onclick: () => post('/api/voice/speaker_check', { mode: m }).then(() => refreshVoice()),
    }, m)));
    const last = v.last_calibration;
    fill(b,
      prof.enrolled
        ? h('div', {}, status('good', 'Voice enrolled'), h('span', { class: 'muted' }, ` · ${prof.clips} samples · ${ago(prof.created)}`))
        : h('div', {}, status('warning', 'Not calibrated'), h('span', { class: 'muted' }, ' · anyone near the mic can give commands')),
      h('div', { class: 'kv' },
        h('span', { class: 'k' }, 'Only answer me'), h('span', {}, modes),
        h('span', { class: 'k' }, 'Match threshold'), h('span', {}, prof.threshold != null ? `${prof.threshold}${prof.last_score != null ? ` · last voice ${prof.last_score}` : ''}` : '—'),
        h('span', { class: 'k' }, 'Speech engine'), h('span', {}, v.pipeline?.engine
          ? `${v.pipeline.engine} · ${v.pipeline.vad === 'silero' ? 'Silero' : 'energy'} voice detection${v.pipeline.latency_ms != null ? ` · ${v.pipeline.latency_ms} ms median` : ''}`
          : 'loads with the listener'),
        v.pipeline?.vad === 'silero' ? null : h('span', { class: 'k' }, 'Speech threshold'),
        v.pipeline?.vad === 'silero' ? null : h('span', {}, `min_rms ${v.min_rms}`)),
      last && last.step === 'done' && last.summary ? h('div', { class: 't2', style: { marginBottom: '8px' } },
        `Last run heard the name as: ${(last.summary.wake_heard || []).join(' · ') || '—'}`) : null,
      last && last.step === 'error' ? h('div', { class: 'empty' }, status('critical', `Calibration failed: ${last.error}`)) : null,
      prof.error ? h('div', { class: 'empty' }, status('warning', `Speaker check unavailable: ${prof.error}`)) : null,
      h('div', { class: 'controls' },
        h('button', { class: 'btn primary', onclick: startCalibration }, prof.enrolled ? 'Re-calibrate' : 'Calibrate my voice'),
        prof.enrolled ? h('button', { class: 'btn', onclick: () => { if (confirm('Delete your voice profile?')) api('/api/voice/profile', { method: 'DELETE' }).then(refreshVoice); } }, 'Delete voice profile') : null),
      h('div', { class: 't2' }, 'About a minute: stay quiet 5 s, say the name 5 times, read 3 lines. Your voice profile stays on this PC.'));
  }
};

render.triggers = function triggers() {
  const v = S.data.voice_profile;
  for (const p of panels('triggers')) {
    const b = body(p);
    if (!v) { fill(b, empty('Loading…')); continue; }
    const models = v.wake_models || [];
    const mode = v.pipeline?.wake_mode;
    const peaks = { ...(v.pipeline?.wake?.peak || {}), ...(S.wakeScores || {}) };
    const KIND = { name: 'wakes it', stop: 'stops a reply', command: 'runs instantly' };
    const upload = h('input', { type: 'file', accept: '.onnx', hidden: true, onchange: async e => {
      const f = e.target.files[0];
      if (!f) return;
      const name = f.name.replace(/\.onnx$/i, '');
      const t = prompt('Detection threshold (the notebook prints one; blank = 0.5):', '');
      const q = t && !Number.isNaN(Number(t)) ? `&threshold=${Number(t)}` : '';
      const res = await fetch(`/api/wakewords?name=${encodeURIComponent(name)}${q}`, {
        method: 'POST', headers: { 'X-Assistant-Token': TOKEN, 'Content-Type': 'application/octet-stream' }, body: f });
      const r = await res.json();
      toast(r.ok ? `${r.name}: ${KIND[r.kind] || r.kind}${r.kind === 'name' ? ` — ${r.mode} wake mode` : ''}` : r.error);
      refreshVoice();
    } });
    fill(b, h('div', { class: 'sub-h' }, 'Wake words'),
      h('div', { class: 'chips' }, (v.wake_words || []).map(w => h('span', { class: 'pill' }, w))),
      h('div', { class: 't2', style: { marginBottom: '6px' } }, mode === 'acoustic'
        ? 'Acoustic: a trained model listens for the name; speech-to-text runs only after it fires at the start of what you say.'
        : 'Matched on the transcript. Train the name on Colab (training/wake_words.ipynb) for fewer false wakes.'),
      h('div', { class: 'sub-h' }, 'Trained models'),
      models.length ? h('table', { class: 't' }, h('tbody', {}, models.map(m => h('tr', {},
        h('td', {}, h('b', {}, m.name.replace(/_/g, ' ')), h('span', { class: 'muted' }, ` · ${KIND[m.kind] || ''}`)),
        h('td', { class: 'r' }, `${m.threshold != null ? `≥ ${m.threshold}` : '≥ 0.5'}${peaks[m.name] != null ? ` · peak ${Number(peaks[m.name]).toFixed(2)}` : ''}`),
        h('td', { class: 'r act' }, h('button', { class: 'btn small', onclick: () => {
          if (confirm(`Remove the ${m.name} model?`)) api(`/api/wakewords/${encodeURIComponent(m.name)}`, { method: 'DELETE' }).then(refreshVoice);
        } }, 'Remove'))))))
        : empty('None yet. Name the file after the phrase: vesper.onnx wakes it, stop.onnx stops a reply, clip_that.onnx runs “clip that”.'),
      h('div', { class: 'controls' }, h('button', { class: 'btn', onclick: () => upload.click() }, 'Add trained model (.onnx)'), upload),
      h('div', { class: 'sub-h' }, 'Macros — one phrase, many actions'),
      (v.macros || []).length ? h('table', { class: 't' },
        h('thead', {}, h('tr', {}, h('th', {}, 'Say'), h('th', { class: 'r' }, 'Steps'), h('th', { class: 'act' }, ''))),
        h('tbody', {}, v.macros.map(m => h('tr', {},
          h('td', { title: m.triggers.join(', ') }, `“${m.triggers[0] || m.name}”`, m.triggers.length > 1 ? h('span', { class: 'muted' }, ` +${m.triggers.length - 1}`) : null),
          h('td', { class: 'r' }, m.steps),
          h('td', { class: 'r act' }, h('button', { class: 'btn small', onclick: () => {
            if (!m.needs_yes || confirm(`Run ${m.name}? It includes actions that normally need a yes.`)) post(`/api/macros/${encodeURIComponent(m.name)}/run`);
          } }, 'Run'))))))
        : empty('No macros yet. Add them under macros: in config.yaml (see config.example.yaml).'));
  }
};

const TTS_ENGINES = [
  ['supertonic', 'Supertonic', 'fast · 85 MB'],
  ['kokoro', 'Kokoro', 'British voices · 320 MB · slower'],
  ['pyttsx3', 'Windows', 'built-in, robotic'],
  ['none', 'Off', 'replies on screen only'],
];

render.speech = function speech() {
  const t = S.data.tts;
  for (const p of panels('speech')) {
    const b = body(p);
    if (!t) { fill(b, empty('Loading…')); continue; }
    const engine = t.engine;
    meta(p, t.state === 'loading' ? `loading${t.progress != null && t.progress < 1 ? ` · ${Math.round(t.progress * 100)}%` : ''}` : (t.labels?.[engine] || engine));
    const set = body => post('/api/tts', body).then(r => { if (r && r.ok === false) toast(r.error); else { S.data.tts = r; render.speech(); } });
    const engines = h('span', { class: 'seg' }, TTS_ENGINES
      .filter(([id]) => t.neural_available || !['supertonic', 'kokoro'].includes(id))
      .map(([id, label, note]) => h('button', { 'aria-pressed': String(engine === id), title: note, onclick: () => set({ engine: id }) }, label)));
    const voices = (t.voices || {})[engine];
    const picker = voices ? h('select', { class: 'field', 'aria-label': 'Voice', onchange: e => set({ voice: e.target.value }) },
      voices.map(v => h('option', { value: v.id, selected: v.id === t.voice }, v.label))) : null;
    const speedOut = h('span', { class: 'muted' }, `${Number(t.speed || 1).toFixed(2)}×`);
    const speed = voices ? h('input', { type: 'range', min: '0.8', max: '1.3', step: '0.05', value: String(t.speed || 1), 'aria-label': 'Speed',
      oninput: e => { speedOut.textContent = `${Number(e.target.value).toFixed(2)}×`; },
      onchange: e => set({ speed: Number(e.target.value) }) }) : null;
    fill(b,
      h('div', { class: 'kv' },
        h('span', { class: 'k' }, 'Engine'), h('span', {}, engines),
        voices ? h('span', { class: 'k' }, 'Voice') : null, picker,
        voices ? h('span', { class: 'k' }, 'Speed') : null, voices ? h('span', { class: 'range-row' }, speed, speedOut) : null,
        h('span', { class: 'k' }, 'First words after'),
        h('span', {}, t.first_audio_ms != null ? `${t.first_audio_ms} ms` : '—', h('span', { class: 'muted' }, t.first_audio_ms != null ? ' · median of recent replies' : ' · measured on the next reply'))),
      t.error ? h('div', { class: 'empty' }, status('warning', t.error)) : null,
      !t.neural_available ? h('div', { class: 'empty' }, status('warning', 'Natural voices need the voice packages: run setup.bat again.')) : null,
      h('div', { class: 'controls' },
        h('button', { class: 'btn primary', disabled: engine === 'none' || t.state === 'loading', onclick: () => post('/api/tts/preview') }, 'Preview'),
        t.state === 'loading' ? h('span', { class: 'muted' }, 'First use downloads the voice…') : null),
      h('div', { class: 't2' }, 'Long replies start speaking after the first sentence. Say “stop” or press the talk hotkey to cut it off.'));
  }
};

render.brain = function brain() {
  const br = S.data.brain || (S.data.assistant || {}).brain;
  for (const p of panels('brain')) {
    const b = body(p);
    if (!br) { fill(b, empty('Loading…')); continue; }
    const local = br.local || {}, claude = br.claude || {};
    meta(p, br.active === 'claude' ? 'Claude' : br.active === 'local' ? local.model : 'no model');
    const set = body => post('/api/brain', body).then(r => { if (r && r.ok === false) toast(r.error); else { S.data.brain = r; render.brain(); paintOrb(); } });
    const modes = h('span', { class: 'seg' }, [['auto', 'Auto', 'Claude when there is a key, else your local model'],
      ['claude', 'Claude', 'Always Claude (needs ANTHROPIC_API_KEY)'], ['local', 'Local only', 'Never send anything to the cloud']]
      .map(([id, label, note]) => h('button', { 'aria-pressed': String(br.provider === id), title: note, onclick: () => set({ provider: id }) }, label)));
    const choices = local.choices || [];
    const tag = c => c.result === 'pass' ? ` · passed, first word ${c.first_word_s} s`
      : c.result === 'fail' ? ' · failed the test' : c.result === 'error' ? ' · error in the test'
      : c.result === 'no_actions' || c.tools === false ? " · can't take actions"
      : c.result === 'too_big' || c.fits === false ? ' · too big for your GPU' : '';
    const best = (local.bench || {}).best;
    const picker = choices.length ? h('select', { class: 'field', 'aria-label': 'Local model', onchange: e => set({ model: e.target.value }) },
      [h('option', { value: '', selected: !local.pinned }, best ? `Auto: strongest that passed (${best})` : 'Auto: strongest that fits your GPU'),
        ...choices.map(c => h('option', { value: c.name, selected: local.pinned === c.name }, `${c.name}${c.size_gb ? ` (${c.size_gb} GB)` : ''}${tag(c)}`))]) : null;
    const answering = br.active === 'claude' ? status('good', `Claude ${claude.model}`)
      : br.active === 'local' ? (local.can_act === false ? status('warning', `${local.model} on ${local.server} · can't take actions`)
        : status('good', `${local.model} on ${local.server}`))
      : status('warning', 'No model: built-in commands only');
    const bench = S.data.brainBench;
    const benchLine = bench && bench.running
      ? (bench.testing ? `Testing ${bench.testing} (${bench.done + 1} of ${bench.total})… each one loads, so this takes a minute or two.` : 'Testing your models…')
      : (bench && bench.summary) || ((local.bench || {}).at ? `Models last tested ${new Date(local.bench.at).toLocaleString()}.`
        : 'Test my models loads each one and checks it takes the right actions and answers quickly; the strongest that passes is used.');
    const light = (local.bench || {}).light;
    const onStream = local.streaming ? status('good', `Live: answering with ${local.model}`)
      : local.stream_model === 'same' ? h('span', { class: 'muted' }, 'Same model')
      : h('span', { class: 'muted' }, `${light || 'The smallest model that can act'}, and the big one is unloaded`);
    const result = h('span', { class: 'muted' });
    fill(b,
      h('div', { class: 'kv' },
        h('span', { class: 'k' }, 'Answering with'), h('span', {}, answering),
        h('span', { class: 'k' }, 'Use'), h('span', {}, modes),
        h('span', { class: 'k' }, 'Local model'), picker || h('span', { class: 'muted' },
          local.server ? `${local.server} is running with no models.`
          : local.configured_url ? `Can't reach ${local.configured_url}. On that PC: setx OLLAMA_HOST 0.0.0.0, then restart Ollama `
            + '(LM Studio: Serve on Local Network). Setup guide, step 19.'
          : 'None found. Install Ollama (ollama.com), then run: ollama pull llama3.1:8b'),
        h('span', { class: 'k' }, 'While live'), onStream,
        h('span', { class: 'k' }, 'Claude'), h('span', {}, claude.ready ? status('good', 'API key set') : status('idle', 'no key (optional)'))),
      h('div', { class: 'controls' },
        h('button', { class: 'btn primary', disabled: !br.active, onclick: async () => {
          result.textContent = 'Asking…';
          const r = await post('/api/brain/test').catch(e => ({ ok: false, error: e.message }));
          result.textContent = !r.ok ? r.error : `Answered “${r.reply}” in ${(r.ms / 1000).toFixed(1)} s`
            + (r.acts === undefined ? '' : r.acts ? ' · took the right action' : " · didn't take the action");
        } }, 'Test'),
        h('button', { class: 'btn', disabled: !!(bench && bench.running) || !choices.length, onclick: async () => {
          const r = await post('/api/brain/bench').catch(e => ({ ok: false, error: e.message }));
          if (r && r.ok) { S.data.brainBench = { running: true, done: 0, total: choices.length }; render.brain(); } else toast(r.error);
        } }, 'Test my models'),
        result),
      h('div', { class: 't2' }, benchLine),
      h('div', { class: 't2' }, 'Models run on your GPU. While you’re live, Vesper answers with the light model and unloads the big '
        + 'one, so the game and OBS’s encoder keep the video memory.'));
  }
};

// Goals, one per area ("Streaming: …", "Work: …"), and this week's focus: shown on the Command tab (Goals & tasks), edited in place.
const goalLines = g => (Array.isArray(g.north_star) ? g.north_star : [g.north_star]).filter(x => x && String(x).trim());
function goalsBlock(goals) {
  const lines = goalLines(goals), week = goals.this_week || [];
  if (S.editGoals) {
    const goalBox = h('textarea', { class: 'field', id: 'goals-edit', rows: 4, 'aria-label': 'Goals, one per line',
      placeholder: 'Streaming: $5k/month from streaming by June 2027\nWork: …\nLife: …' }, lines.join('\n'));
    const weekBox = h('textarea', { class: 'field', id: 'week-edit', rows: 3, 'aria-label': 'This week, one per line',
      placeholder: 'Stream 4 nights\nFinish the overlay' }, week.join('\n'));
    const save = async () => {
      const split = el => el.value.split('\n').map(x => x.trim()).filter(Boolean);
      const r = await post('/api/goals', { goals: split(goalBox), this_week: split(weekBox) }).catch(e => ({ ok: false, error: e.message }));
      if (!r.ok) return toast(r.error || 'Could not save the goals.');
      S.data.goals = r; S.editGoals = false; render.tasks(); toast('Goals saved. Plans rank against them from now on.');
    };
    return [h('div', { class: 'sub-h' }, 'Goals · one per line, each with a number and a date'), goalBox,
      h('div', { class: 'sub-h' }, 'This week · one per line'), weekBox,
      h('div', { class: 'controls' }, h('button', { class: 'btn primary', onclick: save }, 'Save'),
        h('button', { class: 'btn', onclick: () => { S.editGoals = false; render.tasks(); } }, 'Cancel'))];
  }
  const edit = h('button', { class: 'btn', onclick: () => { S.editGoals = true; render.tasks(); } }, lines.length ? 'Edit goals' : 'Set goals');
  return [h('div', { class: 'sub-h' }, 'Goals'),
    lines.length ? h('ul', { class: 'north', style: { margin: 0, paddingLeft: '18px' } }, lines.map(g => h('li', {}, g)))
      : empty('Not set. Priorities are guesses without them: one per area, each with a number and a date.'),
    ...(week.length ? [h('div', { class: 'sub-h' }, 'This week'), h('ul', { class: 'ink2', style: { margin: 0, paddingLeft: '18px' } }, week.map(g => h('li', {}, g)))] : []),
    h('div', { class: 'controls' }, edit)];
}

const TIER_LABEL = ['read-only', 'reversible', 'needs a yes', "needs a yes · can't be undone"];
const SOURCE_LABEL = { voice: 'voice', text: 'typed', hud: 'HUD', hotkey: 'hotkey', tray: 'tray' };
let controlTimer = null;
function refreshControl() {  // tool events arrive in bursts: fetch the log once they settle
  clearTimeout(controlTimer);
  controlTimer = setTimeout(async () => { S.data.pc_control = await api('/api/control'); paintHalt(); render.control(); }, 400);
}

render.control = function control() {
  const c = S.data.pc_control;
  for (const p of panels('control')) {
    const b = body(p);
    if (!c) { fill(b, empty('Loading…')); continue; }
    meta(p, c.hands_off ? 'paused' : `${c.max_steps} actions per request · stops after ${c.max_failures} failures`);
    const rows = (c.recent || []).filter(e => e.tool);
    fill(b,
      h('div', { class: 'controls', style: { marginTop: 0 } },
        c.hands_off
          ? h('button', { class: 'btn primary', onclick: () => post('/api/control/resume') }, 'Resume control')
          : h('button', { class: 'btn danger', onclick: () => post('/api/control/stop') }, 'Stop everything'),
        h('span', { class: 'muted', style: { alignSelf: 'center' } },
          c.hands_off ? `Paused ${ago(c.since)} from the ${c.reason}.` : 'Or press Ctrl+Alt+K, or say “stop everything”.')),
      h('div', { class: 'tier-key' }, [0, 1, 2, 3].map(t => h('span', {}, h('span', { class: `tier t${t}` }, `T${t}`), ` ${TIER_LABEL[t]}`))),
      rows.length ? h('table', { class: 't' },
        h('thead', {}, h('tr', {}, h('th', {}, 'When'), h('th', {}, 'Tier'), h('th', {}, 'Action'), h('th', { class: 'wide-only' }, 'Asked'),
          h('th', { class: 'wide-only' }, 'Yes from'), h('th', {}, 'Result'))),
        h('tbody', {}, rows.slice(0, 25).map(e => h('tr', {},
          h('td', { class: 'fit' }, ago(e.ts)),
          h('td', { class: 'fit' }, h('span', { class: `tier t${e.tier || 0}` }, `T${e.tier || 0}`)),
          h('td', { title: JSON.stringify(e.args || {}) }, (e.tool || '').replace(/_/g, ' '),
            e.args && Object.keys(e.args).length ? h('span', { class: 'muted' }, ` ${Object.values(e.args).slice(0, 2).join(' · ')}`) : null),
          h('td', { class: 'wide-only', title: e.utterance || '' }, SOURCE_LABEL[e.source] || e.source || '—',
            e.utterance ? h('span', { class: 'muted' }, ` “${e.utterance}”`) : null),
          h('td', { class: 'wide-only' }, e.confirmed_by && e.confirmed_by !== 'auto' ? e.confirmed_by : '—'),
          h('td', { title: e.error || '' }, e.outcome === 'ok' || e.outcome === 'resumed' ? status('good', e.outcome === 'ok' ? 'done' : 'resumed')
            : e.outcome === 'stopped' ? status('critical', 'kill switch')
            : e.outcome === 'blocked' ? status('warning', 'refused') : status('critical', 'failed'))))))
        : empty('Nothing yet. Every action it takes will be listed here, and in data/logs/actions-*.jsonl.'));
  }
};

async function refreshVoice() { S.data.voice_profile = await api('/api/voice'); render.voice(); render.triggers(); }
async function startCalibration() {
  const r = await post('/api/voice/calibrate');
  if (r.ok === false) { toast(r.error); return; }
  S.data.calibration = { step: 'noise', prompt: 'Starting…' };
  S.data.voice_profile = { ...S.data.voice_profile, calibrating: true };
  render.voice();
}

render.doctor = function doctor() {
  const r = S.data.doctor;
  for (const p of panels('doctor')) {
    const b = body(p);
    if (!r) { fill(b, empty('Runs live checks: the AI model (Claude or your local one), OBS, each calendar, news feeds, Chrome, Claude Code CLI, voice.')); continue; }
    const counts = {};
    for (const c of r.checks) counts[c.status] = (counts[c.status] || 0) + 1;
    fill(b, h('div', { class: 'muted', style: { fontSize: '12px', marginBottom: '6px' } },
      `${counts.pass || 0} pass · ${counts.warn || 0} warn · ${counts.fail || 0} fail · ${ago(r.ran_at)}`),
      h('div', { class: 'list' }, r.checks.map(c => h('div', { class: 'item' }, h('div', { class: 'main' },
        h('div', { class: 't1' }, status(...(CHECK_STATUS[c.status] || CHECK_STATUS.skip)), ' ', c.name),
        c.detail ? h('div', { class: 't2' }, c.detail) : null,
        c.fix ? h('div', { class: 't3' }, `→ ${c.fix}`) : null)))));
  }
};

function renderFindings(res) {
  const sev = { high: ['critical', 'High'], medium: ['warning', 'Medium'], info: ['idle', 'Info'], ok: ['good', 'OK'] };
  return h('div', {}, h('div', { class: 'sub-h' }, `Optimizer · power plan ${res.power_plan || 'n/a'} · temp ${res.temp_gb} GB`),
    h('div', { class: 'list' }, res.findings.map(f => h('div', { class: 'item' }, h('div', { class: 'main' },
      h('div', { class: 't1' }, status(...(sev[f.severity] || sev.info)), ' ', f.title), h('div', { class: 't2' }, f.detail)),
      f.action ? h('button', { class: 'btn small', onclick: () => {
        const label = f.action.tool === 'close_app' ? `Close ${f.action.args.name}?` : f.action.tool === 'clean_temp' ? 'Delete temp files older than a day?' : null;
        if (!label || confirm(label)) tool(f.action.tool, f.action.args).then(r => toast(r.ok === false ? r.error : 'Done.'));
      } }, 'Fix') : null))));
}

// ---------------------------------------------------------------- library (the second brain's documents)
render.library = function library() {
  const st = S.data.library;
  for (const p of panels('library')) {
    const b = body(p);
    if (!st || st.enabled === false) {
      b._lib = null;
      fill(b, empty(st ? 'The library is turned off (library: enabled in config.yaml).' : 'Loading…'));
      continue;
    }
    if (!b._lib) {  // built once, so typing in the search box survives the progress updates
      const q = h('input', { placeholder: 'Search your documents, e.g. vendor support hours', 'aria-label': 'Search your documents' });
      const results = h('div', { class: 'lib-results' });
      const form = h('form', { class: 'addrow', onsubmit: e => { e.preventDefault(); libSearch(q.value, results); } },
        q, h('button', { class: 'btn small', type: 'submit' }, 'Search'));
      b._lib = { stat: h('div', { class: 'lib-status muted' }), folders: h('div', {}), suggested: h('div', { style: { marginTop: '12px' } }), results };
      fill(b, b._lib.stat, form, results, h('div', { class: 'sub-h' }, 'Folders it reads'), b._lib.folders, b._lib.suggested);
      libSuggest(b._lib.suggested);
    }
    fill(b._lib.stat, libStatusLine(st));
    fill(b._lib.folders, libFolders(st));
  }
};

function libStatusLine(st) {
  const pr = st.progress || {};
  if (st.running) {
    const read = (pr.added || 0) + (pr.updated || 0);
    return `Reading your documents… ${(pr.seen || 0).toLocaleString()} looked at, ${read.toLocaleString()} read${pr.current ? ` · ${pr.current}` : ''}`;
  }
  const last = st.last || {};
  if (!st.files && !last.at) return 'Not read yet. The first read starts a minute after Vesper starts, or press Read new files.';
  const by = st.by_status || {};
  const notes = [by.online && `${by.online} online only (names only)`, by.link && `${by.link} Google docs (names only: Setup → Google accounts)`,
    by.empty && `${by.empty} with no text (scans)`, by.error && `${by.error} couldn't be read`, by.too_big && `${by.too_big} too big`].filter(Boolean);
  return [`${(st.files || 0).toLocaleString()} documents`, last.at ? `read ${ago(last.at)}` : null,
    last.capped ? 'stopped at the file limit (library.max_files)' : null, ...notes].filter(Boolean).join(' · ');
}

function libFolders(st) {
  const folders = st.folders || [];
  const save = saveLibFolders;
  const input = h('input', { placeholder: 'Add a folder: paste its path, e.g. C:\\Users\\you\\Work', 'aria-label': 'Folder to add' });
  const add = h('form', { class: 'addrow', onsubmit: e => {
    e.preventDefault();
    const path = input.value.trim().replace(/^"|"$/g, '');
    if (path) save([...folders.map(f => f.path), path]);
  } }, input, h('button', { class: 'btn small', type: 'submit' }, 'Add'));
  const whole = !!st.whole_pc;
  const scope = h('div', { style: { margin: '2px 0 8px' } }, h('span', { class: 'seg' }, [['Your folders', false], ['This whole PC', true]].map(([label, v]) =>
    h('button', { 'aria-pressed': String(whole === v), onclick: () => setLibScope(v, whole) }, label))));
  const drives = whole ? [h('div', { class: 'sub-h' }, 'Every drive · ranked after your folders'),
    h('div', { class: 'list' }, (st.drives || []).map(d => h('div', { class: 'item' }, h('div', { class: 'main' },
      h('div', { class: 't1' }, d.path), h('div', { class: 't2' }, `${(d.files || 0).toLocaleString()} documents`))))),
    h('div', { class: 't3 muted' }, 'Leaves out Windows, programs, games and code, and plain text files outside your folders. Read once a day.')] : [];
  return [
    scope,
    h('div', { class: 't3 muted', style: { marginBottom: '4px' } }, whole ? 'Your folders (searched first):' : 'Reads:'),
    folders.length ? h('div', { class: 'list' }, folders.map(f => h('div', { class: 'item' },
      h('div', { class: 'main' }, h('div', { class: 't1' }, f.path), h('div', { class: 't2' }, `${(f.files || 0).toLocaleString()} documents`)),
      h('button', { class: 'btn small', 'aria-label': `Stop reading ${f.path}`, onclick: () => {
        if (confirm(`Stop reading ${f.path}? Its files leave the library (the files themselves aren't touched).`)) save(folders.filter(x => x.path !== f.path).map(x => x.path));
      } }, 'Remove')))) : empty('No folders found. Add one below.'),
    add,
    st.custom ? h('button', { class: 'btn small', onclick: () => save([]) }, 'Go back to the usual places') :
      h('div', { class: 't3 muted' }, 'These are the usual places on this PC. Add or remove folders to choose your own.'),
    ...drives,
  ];
}

async function setLibScope(whole, was) {
  if (whole === was) return;
  if (whole && !confirm('Read every drive in this PC too?\n\nYour own folders still come first in answers. Windows, programs, games and code are left out. The first read can take a while; Vesper works normally meanwhile.')) return;
  const r = await post('/api/library/scope', { whole_pc: whole }).catch(e => ({ ok: false, error: e.message }));
  if (!r.ok) return toast(r.error || 'Could not change it.');
  S.data.library = r; render.library(); toast(whole ? 'Reading the whole PC now.' : 'Back to your folders only.');
}

async function saveLibFolders(list) {
  const r = await post('/api/library/folders', { folders: list }).catch(e => ({ ok: false, error: e.message }));
  if (!r.ok) return toast(r.error || 'Could not save the folders.');
  S.data.library = r; render.library(); toast('Saved. Reading the folders now.');
  for (const p of panels('library')) if (body(p)._lib) libSuggest(body(p)._lib.suggested);
}

/* Folders worth adding: the ones pinned in File Explorer, Downloads, Google shared drives. */
async function libSuggest(box) {
  const r = await api('/api/library/suggestions').catch(() => ({ suggested: [] }));
  const list = r.suggested || [];
  if (!list.length) return fill(box);
  const current = (S.data.library?.folders || []).map(f => f.path);
  fill(box, h('div', { class: 'sub-h' }, 'Suggested · add the ones with your work in them'),
    h('div', { class: 'list' }, list.map(sg => h('div', { class: 'item' },
      h('div', { class: 'main' }, h('div', { class: 't1' }, sg.path), h('div', { class: 't2' }, sg.why)),
      h('button', { class: 'btn small', onclick: () => saveLibFolders([...current, sg.path]) }, 'Add')))));
}

async function libSearch(query, box) {
  if (!query.trim()) return fill(box, h('div', { class: 'muted' }, 'Type a few words you know are in the file: a client, a project, a phrase.'));
  fill(box, h('div', { class: 'muted' }, 'Searching…'));
  const r = await post('/api/library/search', { query, limit: 8 }).catch(e => ({ ok: false, error: e.message }));
  if (r.ok === false) return fill(box, empty(r.error || 'Search failed.'));
  if (!r.hits.length) return fill(box, empty(query.trim() ? `Nothing matches “${query}”. Try other words.` : 'No documents yet.'));
  fill(box, h('div', { class: 'list' }, r.hits.map(hit => h('div', { class: 'item' }, h('div', { class: 'main' },
    h('div', { class: 't1' }, hit.title),
    h('div', { class: 't2' }, [hit.file, hit.where, `modified ${hit.modified}`, hit.folder, hit.note].filter(Boolean).join(' · ')),
    hit.passage ? h('div', { class: 't3' }, marked(hit.passage)) : null),
    h('button', { class: 'btn small', onclick: async () => {
      const o = await post('/api/library/open', { path: hit.path }).catch(e => ({ ok: false, error: e.message }));
      toast(o.ok ? `Opening ${o.opened}.` : o.error);
    } }, 'Open')))));
}

/* "Acme [support] [hours] are…" -> the bracketed words highlighted (still text nodes, never HTML). */
function marked(text) { return text.split(/(\[[^\]]*\])/).map(part => part.startsWith('[') && part.endsWith(']') ? h('mark', {}, part.slice(1, -1)) : part); }

// ---------------------------------------------------------------- Inbox (Gmail, work + personal)
render.inbox = function inbox() {
  const st = S.data.gmail;
  for (const p of panels('inbox')) {
    const b = body(p);
    const accts = st?.accounts || [];
    const waiting = accts.reduce((n, a) => n + (a.waiting || 0), 0);
    meta(p, st?.at ? `${waiting} waiting · ${ago(st.at)}` : '');
    if (!st || (!accts.length && !(st.missing || []).length)) {
      fill(b, empty(st?.error || 'Connect Google (Setup → Google accounts) to see who is waiting on a reply.'));
      continue;
    }
    fill(b, h('div', { class: 'inbox-cols' }, accts.map(a => h('div', { class: 'inbox-col' },
      h('div', { class: 'sub-h' }, a.email.endsWith('@gmail.com') ? 'Personal' : 'Work', h('span', { class: 'muted' }, ` · ${a.email}`)),
      a.error ? h('div', { class: 't3' }, a.error) : null,
      (a.people || []).length ? h('div', { class: 'list' }, a.people.map(m => h('a', { class: 'item mail', href: m.link, target: '_blank', rel: 'noopener' },
        h('div', { class: 'main' },
          h('div', { class: 't1' }, m.important ? h('span', { class: 'mail-flag', title: 'Gmail marked it important' }, '◆ ') : null, m.from),
          h('div', { class: 't2' }, m.subject),
          m.snippet ? h('div', { class: 't3 muted' }, m.snippet.slice(0, 140)) : null),
        h('span', { class: 'muted mail-age' }, ago(m.ts))))) : empty('Nobody waiting.'),
      a.bulk ? h('div', { class: 't3 muted', style: { marginTop: '6px' } }, `+ ${a.bulk} newsletters and notifications`) : null))),
      (st.missing || []).length ? h('div', { class: 't3', style: { marginTop: '8px' } },
        `Gmail isn't allowed yet for ${st.missing.join(', ')}: Setup → Google accounts → Reconnect, and tick Gmail.`) : null);
  }
};

// ---------------------------------------------------------------- Google accounts (work + personal)
render.google = function google() {
  const st = S.data.google;
  for (const p of panels('google')) {
    const b = body(p);
    meta(p, st?.accounts?.length ? `${st.accounts.length} connected` : '');
    if (!st) { fill(b, empty('Loading…')); continue; }
    if (!b._g) {  // built once: the calendar list you're ticking survives updates
      b._g = { top: h('div', {}), cals: h('div', { style: { marginTop: '10px' } }) };
      fill(b, b._g.top, b._g.cals);
    }
    if (!st.configured || b._g.editKey) { fill(b._g.top, googleKeyForm(b._g)); fill(b._g.cals); continue; }
    const docs = st.docs || {};
    const broken = (st.ical || []).filter(c => !c.ok);
    fill(b._g.top,
      h('div', { class: 'lib-status muted' }, [
        `${(docs.read || 0).toLocaleString()} Google docs read`,
        docs.names_only ? `${docs.names_only.toLocaleString()} still names only` : null,
        `${(st.calendars || []).length} Google calendars on the agenda`,
        broken.length ? `${broken.length} old iCal link${broken.length === 1 ? '' : 's'} failing` : null,
      ].filter(Boolean).join(' · ')),
      (st.accounts || []).length ? h('div', { class: 'list' }, st.accounts.map(a => h('div', { class: 'item' },
        h('div', { class: 'main' }, h('div', { class: 't1' }, status(...(a.error ? ['critical', 'Signed out'] : a.missing?.length ? ['warning', 'Partly'] : ['good', 'Connected'])), ' ', a.email),
          h('div', { class: 't2' }, a.error || (a.missing?.length ? `You didn't allow ${a.missing.join(' or ')}: click Reconnect and tick every box on Google's page.` :
            `connected ${ago(a.connected_at)} · reads Docs, Sheets, Slides and calendars (never changes them)`))),
        h('button', { class: 'btn small', onclick: () => googleConnect(a.email) }, 'Reconnect'),
        h('button', { class: 'btn small', onclick: () => googleRemove(a.email) }, 'Disconnect')))) :
        empty('No account yet. Connect your work account, then your personal one.'),
      h('div', { class: 'controls', style: { marginTop: '8px' } },
        h('button', { class: 'btn primary', onclick: () => googleConnect('') }, 'Connect a Google account'),
        (st.accounts || []).length ? h('button', { class: 'btn', onclick: () => googleCalendars(b._g.cals) }, 'Choose calendars') : null,
        h('button', { class: 'btn small', onclick: () => { b._g.editKey = true; render.google(); } }, 'Change sign-in key')),
      h('div', { class: 't3 muted', style: { marginTop: '6px' } }, 'Each Connect opens Google in your browser: pick the account, allow, then close that tab.'));
  }
};

function googleKeyForm(g) {
  const id = h('input', { placeholder: 'Client ID: ….apps.googleusercontent.com', 'aria-label': 'Google client ID', autocomplete: 'off' });
  const secret = h('input', { placeholder: 'Client secret: GOCSPX-…', 'aria-label': 'Google client secret', type: 'password', autocomplete: 'off' });
  return [
    h('div', { class: 't2', style: { marginBottom: '6px' } }, 'Sign in with Google so Vesper reads your Google Docs, Sheets and Slides and your Google calendars. First paste the sign-in key you made in Google Cloud (docs/SETUP.md, Part 7).'),
    h('form', { class: 'addrow', onsubmit: async e => {
      e.preventDefault();
      const r = await post('/api/google/client', { client_id: id.value, client_secret: secret.value }).catch(err => ({ ok: false, error: err.message }));
      if (!r.ok) return toast(r.error || 'Could not save it.');
      g.editKey = false; S.data.google = r; render.google(); toast('Saved. Now connect your accounts.');
    } }, id, secret, h('button', { class: 'btn small', type: 'submit' }, 'Save')),
    g.editKey ? h('button', { class: 'btn small', onclick: () => { g.editKey = false; render.google(); } }, 'Cancel') : null,
  ];
}

async function googleConnect(hint) {
  const r = await post('/api/google/connect', { login_hint: hint || '' }).catch(e => ({ ok: false, error: e.message }));
  toast(r.ok ? 'Google sign-in opened in your browser.' : r.error);
}

async function googleRemove(email) {
  if (!confirm(`Disconnect ${email}? Vesper stops reading its Google Docs and calendars (nothing in your Google account changes).`)) return;
  const r = await post('/api/google/remove', { email }).catch(e => ({ ok: false, error: e.message }));
  if (r.configured !== undefined) { S.data.google = r; render.google(); }
  toast(r.ok ? `Disconnected ${email}.` : (r.error || 'It was already disconnected.'));
}

/* Every calendar the accounts can see: tick the ones for the agenda and say whose area each is. */
async function googleCalendars(box) {
  fill(box, h('div', { class: 'muted' }, 'Asking Google for your calendars…'));
  const r = await api('/api/google/calendars').catch(e => ({ ok: false, error: e.message }));
  if (r.ok === false) return fill(box, empty(r.error || 'Could not list them.'));
  const areas = [...new Set([...Object.keys(S.data.profiles || {}), 'personal'])];
  const rows = (r.calendars || []).map(c => {
    const on = h('input', { type: 'checkbox', 'aria-label': `Show ${c.name}` }); on.checked = c.on;
    const area = h('select', { 'aria-label': `Area for ${c.name}` }, areas.map(x => h('option', { value: x }, catLabel(x)))); area.value = areas.includes(c.profile) ? c.profile : 'personal';
    return { c, on, area, el: h('label', { class: 'item', style: { cursor: 'pointer' } }, on,
      h('div', { class: 'main' }, h('div', { class: 't1' }, c.name, c.primary ? h('span', { class: 'muted' }, ' · main') : null), h('div', { class: 't2' }, c.account)), area) };
  });
  const failing = (S.data.google?.ical || []);
  const drop = h('input', { type: 'checkbox' }); drop.checked = failing.some(c => !c.ok);
  fill(box, h('div', { class: 'sub-h' }, 'Calendars · tick the ones for your agenda'),
    Object.entries(r.errors || {}).map(([email, err]) => h('div', { class: 't3' }, `${email}: ${err}`)),
    rows.length ? h('div', { class: 'list' }, rows.map(x => x.el)) : empty('No calendars came back.'),
    failing.length ? h('label', { class: 't2', style: { display: 'block', margin: '8px 0' } }, drop,
      ` Turn off the old calendar links (${failing.map(c => c.name).join(', ')}): the Google ones replace them`) : null,
    h('div', { class: 'controls' }, h('button', { class: 'btn primary', onclick: async () => {
      const chosen = rows.filter(x => x.on.checked).map(x => ({ account: x.c.account, id: x.c.id, name: x.c.name, profile: x.area.value }));
      const s = await post('/api/google/calendars', { calendars: chosen, drop_ical: failing.length ? drop.checked : false }).catch(e => ({ ok: false, error: e.message }));
      if (!s.ok) return toast(s.error || 'Could not save.');
      S.data.google = s; render.google(); fill(box); toast(`Saved: ${chosen.length} calendar${chosen.length === 1 ? '' : 's'} on the agenda.`);
    } }, 'Save calendars'), h('button', { class: 'btn', onclick: () => fill(box) }, 'Cancel')));
}

// ---------------------------------------------------------------- skills (assistant/skills + web/skills/<name>.js)
const SKILL_RENDER = {};
const PENDING_PLACE = [];
/* After every skill script ran: put panels whose "after" neighbour came from a later skill in their place. */
window.addEventListener('DOMContentLoaded', () => {
  for (let round = 0; round < 4; round++) {  // chains (a after b after c) settle within a few rounds
    for (const item of PENDING_PLACE) {
      const ref = $(`.panel[data-panel="${item.after}"]`, item.section);
      if (ref && ref !== item.art && ref.nextElementSibling !== item.art) ref.after(item.art);
    }
  }
});
function paintSkill(name) { try { SKILL_RENDER[name](S.data[name]); } catch (err) { console.error(name, err); } }
const NOTICES = [];
function notice(n) {
  if (NOTICES.some(x => x.key && x.key === n.key)) return;
  NOTICES.unshift(n); NOTICES.length = Math.min(NOTICES.length, 50);
  if (n.level !== 'quiet') toast(n.text, { source: n.source, level: n.level });
  feed({ source: n.source, text: n.text, level: n.level, ts: n.ts, key: n.key });
  for (const fn of Vesper._noticeListeners) fn(n, NOTICES);
}
/* The HUD side of a skill: Vesper.skill('weather', { panels: [...], render(data) {...} }) */
const Vesper = window.Vesper = {
  _noticeListeners: [],
  ui: { h, fill, empty, status, ago, toast, api, post, panels, body, meta, $, $$, meter },
  get data() { return S.data; },
  notices: NOTICES,
  onNotice(fn) { this._noticeListeners.push(fn); },
  repaint(name) { if (SKILL_RENDER[name] && S.data[name] !== undefined) paintSkill(name); },
  act(name, action, payload) { return post(`/api/skill/${name}/${action}`, payload || {}).catch(e => ({ ok: false, error: e.message })); },
  panel({ name, view = 'command', span = 4, title = '', after = null, first = false, buttons = [] }) {
    const section = $(`.view[data-view="${view}"]`);
    if (!section || $(`.panel[data-panel="${name}"]`, section)) return;
    const art = h('article', { class: `panel span-${span}`, 'data-panel': name },
      h('div', { class: 'panel-head' }, h('h2', {}, title), h('span', { class: 'spacer' }), h('span', { class: 'meta', 'data-slot': 'meta' }),
        buttons.map(b => h('button', { class: 'btn small', onclick: b.onclick }, b.label))),
      h('div', { class: 'body' }, empty('Loading…')));
    const ref = after && $(`.panel[data-panel="${after}"]`, section);
    if (ref) ref.after(art); else if (first) section.prepend(art); else section.append(art);
    if (after) PENDING_PLACE.push({ art, after, section });  // re-placed once every skill has loaded
    stagger(section);
    if (typeof render.systems === 'function') queueSystems();
  },
  skill(name, { panels: list = [], render: fn }) {
    for (const p of list) this.panel({ name, ...p });
    if (fn) { SKILL_RENDER[name] = fn; render[`skill_${name}`] = () => { if (S.data[name] !== undefined) paintSkill(name); }; }
  },
};

function renderAll() { for (const fn of Object.values(render)) { try { fn(); } catch (err) { console.error(err); } } }

// ---------------------------------------------------------------- actions
const TWITCH_EVENT = {
  follow: () => 'followed', sub: e => `subscribed${e.detail && e.detail.tier ? ` · ${e.detail.tier}` : ''}`,
  resub: e => `resubscribed · ${e.amount} months`, gift: e => `gifted ${e.amount} sub${e.amount === 1 ? '' : 's'}`,
  cheer: e => `cheered ${e.amount} bits`, raid: e => `raided with ${e.amount}`,
  redemption: e => `redeemed ${(e.detail && e.detail.reward) || 'a reward'}`, hype_train: e => `hype train · level ${e.amount || 1}`,
};
function twitchEvent(e) {
  const what = (TWITCH_EVENT[e.kind] || (() => e.kind))(e);
  return h('div', { class: 'item' }, h('div', { class: 'main' },
    h('div', { class: 't1' }, e.user || (e.kind === 'hype_train' ? 'Chat' : 'Anonymous'), ' ', h('span', { class: 'ink2' }, what)),
    h('div', { class: 't2' }, ago(e.ts))));
}
function stamp(sec) {
  sec = Math.max(0, Math.round(sec));
  return `${Math.floor(sec / 3600)}:${String(Math.floor(sec % 3600 / 60)).padStart(2, '0')}:${String(sec % 60).padStart(2, '0')}`;
}
function twitchLogin(pending) {
  const code = pending.user_code || '';
  return h('div', { class: 'twitch-login' },
    h('div', { class: 't2' }, 'Enter this code at twitch.tv/activate, or open the link and approve:'),
    h('div', { class: 'code' }, code),
    h('div', { class: 'controls' },
      h('a', { class: 'btn primary', href: pending.verification_uri, target: '_blank', rel: 'noopener' }, 'Open Twitch'),
      h('button', { class: 'btn', onclick: () => post('/api/twitch/cancel').catch(() => {}) }, 'Cancel')),
    h('div', { class: 't2' }, 'Waiting for you to approve… this updates on its own.'));
}
async function twitchConnect() {
  try {
    const r = await post('/api/twitch/connect');
    if (!r.ok) toast(r.error);
  } catch (err) { toast(`Failed: ${err.message}`); }
}
async function twitchLogout() {
  try { await post('/api/twitch/logout'); toast('Twitch disconnected.'); } catch (err) { toast(`Failed: ${err.message}`); }
}
function twitchPatch(fn) {
  S.data.twitch = S.data.twitch || { enabled: true };
  fn(S.data.twitch);
  render.twitch();
}

async function tool(name, args) {
  try { return await post(`/api/tool/${name}`, args || {}); } catch (err) { toast(`Failed: ${err.message}`); return { ok: false }; }
}
async function command(text) {
  if (!text.trim()) return;
  strip.pending();
  try {
    const res = await post('/api/command', { text });
    if (res.data && (res.kind === 'briefing' || res.kind === 'recap')) { S.data.briefing = res.data; render.briefing(); setActive(panelFor('briefing')); }
  } catch (err) { strip.reply(`Couldn't reach the assistant (${err.message}).`); }
}
async function refreshTasks() { S.data.tasks = await api('/api/tasks'); render.tasks(); render.stats(); }
async function delegate(project) {
  const prompt = window.prompt(`What should Claude Code do in ${project}? It runs in the background (accept-edits mode).`);
  if (!prompt) return;
  const res = await post('/api/jobs', { kind: 'claude_code', prompt, project });
  toast(res.ok ? `Job ${res.job_id} started in ${project}.` : res.error);
}

document.addEventListener('click', async e => {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  const action = btn.dataset.action;
  setActive(btn.closest('.panel'), { ms: 1800 });
  if (action === 'briefing') {
    btn.disabled = true; const label = btn.textContent; btn.textContent = 'Building…';
    try { S.data.briefing = await post(`/api/briefing/${btn.dataset.kind}`); render.briefing(); } finally { btn.disabled = false; btn.textContent = label; }
  } else if (action === 'speak-briefing') {
    post('/api/briefing/morning/speak');
  } else if (action === 'optimize') {
    btn.disabled = true;
    try { S.optimizer = await tool('optimize_pc', { streaming: !!btn.dataset.streaming }); render.vitals(); } finally { btn.disabled = false; }
  } else if (action === 'rescan' || action === 'doctor') {
    btn.disabled = true; const label = btn.textContent; btn.textContent = action === 'rescan' ? 'Scanning…' : 'Checking…';
    try {
      const res = await post(action === 'rescan' ? '/api/discovery/run' : '/api/doctor');
      S.data[action === 'rescan' ? 'discovery' : 'doctor'] = res;
      render[action === 'rescan' ? 'connections' : 'doctor']();
    } catch (err) { toast(`Failed: ${err.message}`); } finally { btn.disabled = false; btn.textContent = label; }
  } else if (action === 'inbox-refresh') {
    btn.disabled = true;
    try { S.data.gmail = await api('/api/gmail?fresh=true'); render.inbox(); } catch (err) { toast(`Failed: ${err.message}`); } finally { btn.disabled = false; }
  } else if (action === 'library-index') {
    const r = await post('/api/library/index').catch(e => ({ ok: false, error: e.message }));
    toast(r.ok === false ? r.error : r.running ? 'Already reading.' : 'Reading new and changed files…');
  } else if (action === 'new-research') {
    const prompt = window.prompt('Research topic — Claude will search the web and write a brief in the background:');
    if (prompt) { const r = await post('/api/jobs', { kind: 'research', prompt }); toast(r.ok ? `Research job ${r.job_id} started.` : r.error); }
  }
});

$('#cmd-form').addEventListener('submit', e => { e.preventDefault(); const i = $('#cmd'); command(i.value); i.value = ''; });
document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 'k') { e.preventDefault(); togglePalette(); return; }
  const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName) || document.activeElement.isContentEditable;
  if (e.key === '/' && !typing && !document.querySelector('dialog[open]')) { e.preventDefault(); $('#cmd').focus(); }
});
$('#confirm-btn').addEventListener('click', () => post('/api/confirm', { id: S.pending?.id }));
$('#cancel-btn').addEventListener('click', () => post('/api/cancel', { id: S.pending?.id }));
$('#resume-btn').addEventListener('click', () => post('/api/control/resume'));
$('#mic-btn').addEventListener('click', () => post('/api/voice/arm'));
$('#mute-btn').addEventListener('click', e => {
  const muted = e.currentTarget.getAttribute('aria-pressed') !== 'true';
  e.currentTarget.setAttribute('aria-pressed', String(muted));
  post('/api/voice/mute', { muted });
});
for (const t of $$('.tabs button')) t.addEventListener('click', () => setView(t.dataset.view));
const VIEWS = ['command', 'work', 'life', 'stream', 'pc', 'setup'];
function setView(view) {
  const prev = S.view;
  S.view = view;
  try { localStorage.setItem('hud.view', view); } catch { /* storage unavailable */ }
  for (const t of $$('.tabs button')) t.setAttribute('aria-selected', String(t.dataset.view === view));
  for (const v of $$('.view')) {
    v.hidden = v.dataset.view !== view;
    if (!v.hidden && prev !== view && FX.motion.full) {
      v.classList.remove('enter', 'back'); void v.offsetWidth;
      v.classList.add('enter'); if (VIEWS.indexOf(view) < VIEWS.indexOf(prev)) v.classList.add('back');
    }
  }
  moveInk();
  renderAll();
}
function moveInk(instant) {
  const ink = $('.tab-ink'), btn = $('.tabs button[aria-selected="true"]');
  if (!ink || !btn || !btn.offsetWidth) return;
  const pad = parseFloat(getComputedStyle(btn).paddingLeft) || 0;
  if (instant) ink.style.transition = 'none';
  ink.style.transform = `translateX(${btn.offsetLeft + pad}px) scaleX(${Math.max(1, btn.offsetWidth - pad * 2)})`;
  if (instant) { void ink.offsetWidth; ink.style.transition = ''; }
}
addEventListener('resize', () => moveInk(true));

// ---------------------------------------------------------------- voice / status chrome
FX.orb.attach($('#orb'));
FX.orb.attach($('#orb-mini'), { mini: true });
const orbState = { voice: 'off', thinking: false, speaking: false };
const ORB_STATE = { armed: 'listen', hearing: 'listen', transcribing: 'listen', listening: 'standby', muted: 'muted', off: 'off',
  disabled: 'off', unavailable: 'off', loading: 'loading', error: 'error' };
const ORB_LABEL = { listen: 'Listening', think: 'Thinking', speak: 'Speaking', standby: 'Standby', muted: 'Mic muted', off: 'Voice off',
  loading: 'Loading', error: 'Voice error' };
function paintOrb() {
  const st = orbState.speaking ? 'speak' : orbState.thinking ? 'think' : ORB_STATE[orbState.voice] || 'off';
  FX.orb.setState(st);
  const wrap = $('#orb-wrap');
  if (wrap._st !== st) {
    wrap._st = st; wrap.dataset.state = st;
    FX.reveal($('.lbl', wrap), ORB_LABEL[st], { mode: 'decode' });
  }
  wrap.classList.toggle('idle', !['listen', 'think', 'speak', 'loading'].includes(st));
  const a = S.data.assistant || {};
  const labels = {
    listening: `Listening for “${(a.wake_words || ['vesper'])[0]}”`, hearing: 'Hearing you…', transcribing: 'Transcribing…',
    armed: 'Go ahead — listening', muted: 'Mic muted', loading: 'Loading speech model…', disabled: 'Voice disabled',
    unavailable: 'Voice unavailable', error: 'Voice error', off: 'Voice off',
  };
  let txt = orbState.speaking ? 'Speaking…' : orbState.thinking ? 'Thinking…' : labels[orbState.voice] || orbState.voice;
  if (orbState.voice === 'loading' && S.data.voice?.detail) txt = S.data.voice.detail;  // first-run model download
  if ((orbState.voice === 'error' || orbState.voice === 'unavailable') && S.data.voice?.error) txt = S.data.voice.error;
  const brain = a.brain || {};
  txt += brain.active === 'claude' ? ` · Claude ${a.model}`
    : brain.active === 'local' ? ` · ${brain.local.model} on ${brain.local.server}`
    : ' · no AI model (built-in commands only)';
  $('#voice-status').textContent = txt;
  $('#voice-status').title = txt;
}
function paintPending(p) {
  S.pending = p || null;
  $('#pending').classList.toggle('hidden', !p);
  $('#pending-text').textContent = p ? `${p.text[0].toUpperCase()}${p.text.slice(1)}?` : '';
  $('#pending-tier').textContent = p && p.tier >= 3 ? "Confirm · can't be undone" : 'Confirm';
}
function paintHalt() {
  $('#halt').classList.toggle('hidden', !S.data.pc_control?.hands_off);
}
function paintProfile() {
  const chip = $('#profile-chip');
  const prof = S.data.active_profile;
  chip.classList.toggle('hidden', !prof);
  chip.replaceChildren(prof ? h('span', { class: 'sw', style: { width: '8px', height: '8px', borderRadius: '50%', background: catColor(prof), display: 'inline-block' } }) : '',
    prof ? `${catLabel(prof)} mode` : '');
}
function tick() {
  const now = new Date();
  $('#clock-t').textContent = fmtClock(now);
  $('#clock-d').textContent = now.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
}
setInterval(tick, 1000); tick();

// Theme: follows Windows light/dark until you pick one here.
$('#theme-btn').addEventListener('click', () => {
  const current = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  const next = current === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('hud.theme', next); } catch { /* storage unavailable */ }
  FX.orb.refresh();
  renderAll();  // charts read colours at draw time
});

// Browser speech (tts.engine: browser)
function pickVoice(hint) {
  const voices = speechSynthesis.getVoices();
  const en = voices.filter(v => v.lang?.startsWith('en'));
  return (hint && voices.find(v => v.name.toLowerCase().includes(hint.toLowerCase())))
    || en.find(v => /natural/i.test(v.name) && /(ryan|guy|davis|andrew|brian|christopher)/i.test(v.name))
    || en.find(v => /natural/i.test(v.name)) || en.find(v => /google uk english male/i.test(v.name)) || en[0];
}
function speak({ text, voice_hint, rate }) {
  if (!('speechSynthesis' in window)) { post('/api/voice/speak_done'); return; }
  const u = new SpeechSynthesisUtterance(text);
  const v = pickVoice(voice_hint); if (v) u.voice = v;
  u.rate = rate || 1;
  u.onend = u.onerror = () => post('/api/voice/speak_done');
  speechSynthesis.speak(u);
}

// ---------------------------------------------------------------- HUD 2.0 chrome: panels that assemble, the live strip,
// the active-panel glow, notification center, command palette, service constellation, display settings, boot.

/* Panels reveal once (top-down scan, brackets drawing out), staggered 38 ms, capped so a view assembles in < 600 ms.
   data-shown marks a panel as done, so data refreshes and tab switches never replay it. */
function stagger(section) {
  $$('.panel', section).forEach((p, i) => p.style.setProperty('--i', Math.min(i, 10)));
}
document.addEventListener('animationend', e => {
  if (e.animationName === 'brackets-in') e.target.closest?.('.panel')?.setAttribute('data-shown', '');
});

/* The panel Vesper is working on gets one light running round its edge (iris for tools, ember otherwise). One at a time. */
let activeP = null, activeTimer = 0;
function setActive(p, { ms = 2600, color = '' } = {}) {
  if (!p || p.closest('.view')?.hidden) return;
  if (activeP && activeP !== p) clearActive();
  activeP = p;
  p.classList.add('active');
  let edge = $(':scope > .edge', p);
  if (!edge) { edge = h('span', { class: 'edge', 'aria-hidden': 'true' }); p.append(edge); }
  edge.style.setProperty('--s', `${Math.ceil(Math.hypot(p.offsetWidth, p.offsetHeight)) + 24}px`);
  if (color) p.style.setProperty('--edge-c', color); else p.style.removeProperty('--edge-c');
  requestAnimationFrame(() => edge.classList.add('on'));
  clearTimeout(activeTimer);
  activeTimer = setTimeout(clearActive, ms);
}
function clearActive() {
  const p = activeP;
  activeP = null;
  if (!p) return;
  p.classList.remove('active');
  const edge = $(':scope > .edge', p);
  if (edge) { edge.classList.remove('on'); setTimeout(() => { if (!p.classList.contains('active')) edge.remove(); }, 300); }
}
function panelFor(name) { return name ? panels(name).find(p => !p.closest('.view')?.hidden) : null; }
const TOOL_PANEL = [[/calendar|agenda|schedule|event/, 'agenda'], [/gmail|email|mail|inbox/, 'inbox'], [/^obs_|prestream|scene/, 'obs'],
  [/^twitch_|clip|marker/, 'twitch'], [/library|document|docs/, 'library'], [/task/, 'tasks'], [/weather/, 'weather'],
  [/brief|plan|recap/, 'briefing'], [/job|research|claude_code|delegate/, 'jobs'], [/optimi|system|close_app|clean_temp|process/, 'vitals'],
  [/remind|timer|routine|automation/, 'automations'], [/news|headline/, 'news'], [/project|repo|github/, 'projects'],
  [/nudge|quiet/, 'nudges'], [/activity|focus|time_/, 'activity'], [/goal/, 'tasks']];
function toolPanel(name) { const hit = TOOL_PANEL.find(([re]) => re.test(name || '')); return hit ? hit[1] : null; }

// ---------------------------------------------------------------- live strip: J (ion) -> Vesper (ember), tools (iris)
function announce(text) { const r = $('#sr-live'); if (r) { r.textContent = ''; setTimeout(() => { r.textContent = text; }, 30); } }
const strip = {
  hint() {
    const name = S.data.assistant?.name || 'Vesper';
    const you = $('#heard');
    you.classList.add('hint'); you.textContent = `Say “${name}, …”, type below, or press Ctrl K`;
  },
  you(text, { ignored = false } = {}) {
    const el = $('#heard');
    $('#strip-you').removeAttribute('data-empty');
    el.classList.remove('hint'); el.classList.toggle('ignored', ignored);
    el.title = text;
    FX.reveal(el, text, { mode: text.length <= 64 && !ignored ? 'decode' : 'type' });
    if (!ignored) announce(`You: ${text}`);
  },
  restore() {  // after a (re)load: the last exchange, shown still
    const lastUser = [...S.log].reverse().find(m => m.role === 'user'), lastVes = [...S.log].reverse().find(m => m.role === 'assistant');
    if (lastUser && $('#strip-you').hasAttribute('data-empty')) {
      $('#strip-you').removeAttribute('data-empty'); const el = $('#heard'); el.classList.remove('hint'); el.textContent = lastUser.text; el.title = lastUser.text;
    }
    if (lastVes && $('#strip-ves').hasAttribute('data-empty')) {
      $('#strip-ves').removeAttribute('data-empty'); $('#reply').textContent = lastVes.text; $('#reply').title = lastVes.text;
    }
  },
  pending() {
    const el = $('#reply');
    $('#strip-ves').removeAttribute('data-empty');
    el._fx = (el._fx || 0) + 1;
    el.replaceChildren(h('span', { class: 'muted' }, 'on it'), h('span', { class: 'caret' }));
  },
  reply(text) {
    const el = $('#reply');
    $('#strip-ves').toggleAttribute('data-empty', !text);
    el.title = text;
    FX.reveal(el, text, { mode: 'type' });
    if (text) announce(`${S.data.assistant?.name || 'Vesper'}: ${text}`);
  },
};
const chips = {
  thinking: null, fadeTimer: 0,
  box() { return $('#tool-chips'); },
  think(on) {
    if (on) {
      clearTimeout(this.fadeTimer);
      for (const c of $$('.tchip', this.box())) c.remove();
      if (!this.thinking) {
        this.thinking = h('span', { class: 'tchip thinking', role: 'status' }, h('span', { class: 'dots', 'aria-hidden': 'true' }, h('i'), h('i'), h('i')), 'thinking');
        this.box().append(this.thinking);
      }
    } else {
      this.out(this.thinking); this.thinking = null;
      this.fadeSoon(2600);
    }
  },
  tool(name, result) {
    const failed = result && typeof result === 'object' && result.ok === false;
    const chip = h('span', { class: 'tchip run', title: (name || '').replace(/_/g, ' ') }, (name || 'tool').replace(/_/g, '.'));
    if (this.thinking) this.thinking.before(chip); else this.box().append(chip);
    setTimeout(() => chip.classList.replace('run', failed ? 'fail' : 'done'), 450);
    const all = $$('.tchip:not(.thinking):not(.out)', this.box());
    for (const old of all.slice(0, Math.max(0, all.length - 3))) this.out(old);
    if (!this.thinking) this.fadeSoon(3200);
  },
  out(chip) { if (!chip) return; chip.classList.add('out'); setTimeout(() => chip.remove(), 260); },
  fadeSoon(ms) {
    clearTimeout(this.fadeTimer);
    this.fadeTimer = setTimeout(() => { for (const c of $$('.tchip:not(.thinking)', this.box())) this.out(c); }, ms);
  },
};
function wakeFlare() {
  FX.flare($('#orb').getBoundingClientRect().width ? $('#orb') : $('#orb-mini'));
  const bar = $('#topbar');
  bar.classList.remove('flare'); void bar.offsetWidth; bar.classList.add('flare');
}

// ---------------------------------------------------------------- notification center (bell + drawer)
const FEED = [];
let feedSeen = 0;
try { feedSeen = Number(localStorage.getItem('hud.feedSeen')) || 0; } catch { /* storage unavailable */ }
function feed(item) {
  if (!item || !item.text) return;
  if (item.key && FEED.some(x => x.key === item.key)) return;
  FEED.unshift({ ...item, ts: item.ts || Date.now() / 1000 });
  FEED.sort((a, b) => b.ts - a.ts);
  FEED.length = Math.min(FEED.length, 100);
  paintBell(true);
  if ($('#drawer').open) renderDrawer();
}
/* After a reload: the updates the nudges skill already delivered, so the drawer isn't empty. */
function hydrateFeed() {
  for (const n of (S.data.nudges?.feed || []).slice(0, 30)) {
    feed({ source: n.source, text: n.text, level: n.priority, ts: n.delivered, key: n.key });
  }
}
function unread() { return FEED.filter(n => n.ts > feedSeen).length; }
function paintBell(bump) {
  const n = unread(), badge = $('#bell-count'), btn = $('#bell-btn');
  const was = badge.textContent;
  badge.textContent = n > 99 ? '99+' : String(n);
  badge.classList.toggle('hidden', !n);
  btn.setAttribute('aria-label', n ? `Notifications, ${n} unread` : 'Notifications');
  if (bump && n && was !== badge.textContent && FX.motion.full) { badge.classList.remove('bump'); void badge.offsetWidth; badge.classList.add('bump'); }
}
function markRead() {
  feedSeen = Date.now() / 1000;
  try { localStorage.setItem('hud.feedSeen', String(feedSeen)); } catch { /* storage unavailable */ }
  paintBell();
}
let drawerSeenAt = 0;
function openDrawer() {
  const d = $('#drawer');
  if (d.open) return;
  drawerSeenAt = feedSeen;
  renderDrawer();
  d.showModal();
  setTimeout(markRead, 900);
}
function renderDrawer() {
  const box = $('#drawer-body');
  $('#drawer-meta').textContent = FEED.length ? `${FEED.length} · ${unread()} new` : '';
  if (!FEED.length) {
    return fill(box, h('div', { class: 'n-empty' },
      s('svg', { viewBox: '0 0 60 60', 'aria-hidden': 'true' }, s('path', { d: 'M30 4 52.5 17v26L30 56 7.5 43V17z' }), s('path', { d: 'M21 24l9 15 9-15' })),
      h('div', {}, 'All quiet.'), h('div', {}, 'Meetings, emails that need you, reminders and stream alerts land here.')));
  }
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const t0 = today.getTime() / 1000, now = Date.now() / 1000;
  const groupOf = ts => (now - ts < 600 ? 'Just now' : ts >= t0 ? 'Earlier today' : ts >= t0 - 86400 ? 'Yesterday' : 'Older');
  const out = [];
  let last = '';
  for (const n of FEED) {
    const g = groupOf(n.ts);
    if (g !== last) { out.push(h('div', { class: 'n-group' }, g)); last = g; }
    const lv = n.level || 'info';
    out.push(h('div', { class: `n-item${n.ts > drawerSeenAt ? ' unread' : ''}`, 'data-level': lv },
      h('span', { class: 'n-ic' }, icon(n.source)),
      h('div', {}, h('div', { class: 'n-meta' }, h('span', {}, SOURCE_LABEL_N[n.source] || (n.source || 'Vesper').replace(/_/g, ' ')),
        ['urgent', 'error', 'critical', 'warning', 'warn'].includes(lv) ? h('span', { class: 'lv' }, lv === 'urgent' ? 'urgent' : lv) : null,
        h('span', { class: 'time', title: new Date(n.ts * 1000).toLocaleString() }, ago(n.ts))),
      h('div', { class: 'n-txt' }, n.text))));
  }
  fill(box, out);
}

// ---------------------------------------------------------------- services: constellation, readouts, boot lines
function services() {
  const d = S.data, a = d.assistant || {};
  const br = d.brain || a.brain || {};
  const out = [];
  out.push({ id: 'brain', label: 'Brain', status: br.active ? (br.active === 'local' && br.local?.can_act === false ? 'warn' : 'ok') : 'warn',
    detail: br.active === 'claude' ? `Claude ${a.model || ''}` : br.active === 'local' ? `${br.local?.model} on ${br.local?.server}` : 'no AI model: built-in commands only' });
  const v = d.voice || {};
  const vs = v.state || 'off';
  out.push({ id: 'voice', label: 'Voice', status: ['listening', 'armed', 'hearing', 'transcribing'].includes(vs) ? 'ok' : ['muted', 'loading'].includes(vs) ? 'warn'
    : ['error', 'unavailable'].includes(vs) ? 'down' : 'off', detail: v.error || vs });
  const obs = d.obs;
  out.push({ id: 'obs', label: 'OBS', status: obs?.connected ? 'ok' : obs && obs.enabled !== false && obs.error ? 'down' : 'off',
    detail: obs?.connected ? `scene ${obs.current_scene || '—'}${obs.streaming?.active ? ' · LIVE' : ''}` : obs?.error || 'not connected' });
  const tw = d.twitch;
  out.push({ id: 'twitch', label: 'Twitch', status: !tw?.enabled ? 'off' : tw.error ? 'down' : tw.auth?.connected || !tw.needs_login ? 'ok' : 'warn',
    detail: !tw?.enabled ? 'not set up' : tw.error || (tw.live ? `live · ${tw.viewers} watching` : tw.channel || 'connected') });
  const g = d.google;
  const accts = g?.accounts || [];
  out.push({ id: 'google', label: 'Google', status: !g?.configured ? 'off' : accts.some(x => x.error) ? 'down' : !accts.length || accts.some(x => x.missing?.length) ? 'warn' : 'ok',
    detail: !g?.configured ? 'not set up' : `${accts.length} account${accts.length === 1 ? '' : 's'}` });
  const gm = d.gmail;
  out.push({ id: 'gmail', label: 'Gmail', status: gm?.error ? 'down' : gm?.accounts?.length ? (gm.accounts.some(x => x.error) ? 'warn' : 'ok') : gm?.missing?.length ? 'warn' : 'off',
    detail: gm?.accounts?.length ? `${gm.accounts.reduce((n, x) => n + (x.waiting || 0), 0)} waiting` : 'not connected' });
  const cal = d.calendar;
  const cals = cal?.calendars || [];
  out.push({ id: 'calendar', label: 'Calendar', status: !cals.length ? 'off' : cals.some(c => c.error) ? (cals.every(c => c.error) ? 'down' : 'warn') : 'ok',
    detail: cals.length ? `${cals.length} calendar${cals.length === 1 ? '' : 's'}` : 'none connected' });
  const lib = d.library;
  out.push({ id: 'library', label: 'Library', status: !lib || lib.enabled === false ? 'off' : lib.files ? 'ok' : 'warn',
    detail: !lib || lib.enabled === false ? 'off' : `${(lib.files || 0).toLocaleString()} documents${lib.running ? ' · reading' : ''}` });
  const sk = d.skills || {};
  for (const x of sk.loaded || []) {
    const data = d[x.name];
    out.push({ id: `skill:${x.name}`, label: x.title || x.name, skill: x.name,
      status: sk.errors?.[x.name] ? 'down' : data && data.ok === false ? 'warn' : 'ok', detail: sk.errors?.[x.name] || (data && data.error) || 'skill' });
  }
  return out;
}
const SYSTEM_EVENTS = new Set(['obs', 'twitch', 'calendar']);
let systemsQueued = false;
function queueSystems() {
  if (systemsQueued) return;
  systemsQueued = true;
  requestAnimationFrame(() => { systemsQueued = false; render.systems(); });
}
const STATUS_WORD = { ok: 'connected', warn: 'needs you', down: 'down', off: 'off' };
const hexPts = (r, cx = 0, cy = 0) => Array.from({ length: 6 }, (_, i) => {
  const a = (i / 6) * Math.PI * 2 - Math.PI / 2;
  return `${(cx + Math.cos(a) * r).toFixed(2)},${(cy + Math.sin(a) * r).toFixed(2)}`;
}).join(' ');
render.systems = function systems() {
  const list = services();
  paintReadouts(list);
  for (const p of panels('systems')) {
    const b = body(p);
    const sig = list.map(n => n.id).join(',');
    if (!b._c || b._c.sig !== sig) b._c = buildConstellation(b, list, sig);
    const ok = list.filter(n => n.status === 'ok').length;
    meta(p, `${ok}/${list.length} online`);
    for (const n of list) {
      const node = b._c.nodes[n.id];
      if (!node) continue;
      node.g.setAttribute('class', `node-g n-${n.status}`);
      node.edge.setAttribute('class', `edge-l ${n.status}`);
      node.title.textContent = `${n.label}: ${STATUS_WORD[n.status]} · ${n.detail}`;
    }
  }
};
function buildConstellation(b, list, sig) {
  const svgEl = s('svg', { class: 'constellation', viewBox: '-175 -104 350 212', role: 'img', 'aria-label': 'Vesper and the services it talks to' });
  const RX = 140, RY = 74;
  svgEl.append(s('ellipse', { class: 'c-ring', cx: 0, cy: 0, rx: RX, ry: RY }), s('circle', { class: 'c-ring', cx: 0, cy: 0, r: 36 }));
  const nodes = {};
  const edges = s('g', {}), dots = s('g', {}), ng = s('g', {});
  list.forEach((n, i) => {
    const a = (i / list.length) * Math.PI * 2 - Math.PI / 2;
    const x = Math.cos(a) * RX, y = Math.sin(a) * RY;
    const len = Math.hypot(x, y), ux = x / len, uy = y / len;
    const edge = s('line', { class: 'edge-l', x1: (ux * 24).toFixed(2), y1: (uy * 24).toFixed(2), x2: (x - ux * 11).toFixed(2), y2: (y - uy * 11).toFixed(2) });
    edges.append(edge);
    const title = s('title', {});
    const below = y > -10;
    const label = s('text', { class: 'lbl', x: x.toFixed(2), y: (below ? y + 19 : y - 14).toFixed(2), 'text-anchor': 'middle' });
    label.textContent = n.label.length > 12 ? `${n.label.slice(0, 11)}…` : n.label;
    const bang = s('text', { class: 'bang', x: x.toFixed(2), y: (y + 3).toFixed(2), 'text-anchor': 'middle' });
    bang.textContent = '!';
    const g = s('g', { class: 'node-g', tabindex: 0 }, title, s('polygon', { class: 'hx', points: hexPts(9, x, y) }),
      s('line', { class: 'x', x1: x - 4.5, y1: y - 4.5, x2: x + 4.5, y2: y + 4.5 }), s('line', { class: 'x', x1: x - 4.5, y1: y + 4.5, x2: x + 4.5, y2: y - 4.5 }),
      bang, label);
    ng.append(g);
    nodes[n.id] = { g, edge, title, x, y };
  });
  const core = s('g', { class: 'core' },
    s('circle', { class: 'core-ring', cx: 0, cy: 0, r: 25 }), s('polygon', { class: 'core-hx', points: hexPts(18) }),
    s('path', { class: 'core-v', d: 'M-7.5 -5.5 0 7 7.5 -5.5' }));
  svgEl.append(edges, dots, ng, core);
  const legend = h('div', { class: 'c-legend', 'aria-hidden': 'true' },
    [['ok', 'connected'], ['warn', 'needs you'], ['down', 'down'], ['off', 'off']].map(([k, label]) => h('span', {},
      s('svg', { viewBox: '-10 -10 20 20', class: 'constellation' }, s('g', { class: `n-${k}` }, s('polygon', { class: 'hx', points: hexPts(8) }),
        s('line', { class: 'x', x1: -4, y1: -4, x2: 4, y2: 4 }), s('line', { class: 'x', x1: -4, y1: 4, x2: 4, y2: -4 }))), label)),
    h('span', {}, s('svg', { viewBox: '-10 -10 20 20', class: 'constellation' }, s('circle', { class: 'packet', r: 3 })), 'traffic'));
  fill(b, svgEl, legend);
  return { sig, nodes, dots };
}
/* A dot travels the edge when a service actually talks (inbound ion, outbound iris). Event-driven, never looped. */
const EVENT_NODE = { brain: 'brain', brain_bench: 'brain', assistant_said: 'brain', voice_state: 'voice', wake: 'voice', heard: 'voice',
  user_said: 'voice', speaking: 'voice', tts: 'voice', obs: 'obs', prestream: 'obs', stream_alert: 'obs', twitch: 'twitch', twitch_auth: 'twitch',
  twitch_event: 'twitch', twitch_events_state: 'twitch', highlight: 'twitch', google: 'google', gmail: 'gmail', calendar: 'calendar', library: 'library' };
const lastPulse = {};
function pulse(type, data) {
  if (S.view !== 'setup' || document.hidden) return;
  let id = EVENT_NODE[type], out = false;
  if (type === 'thinking' && data?.active) { id = 'brain'; out = true; }
  if (type === 'tool') {
    const hit = { agenda: 'calendar', inbox: 'gmail', obs: 'obs', twitch: 'twitch', library: 'library' }[toolPanel(data?.name)];
    id = hit || (SKILL_RENDER[toolPanel(data?.name)] ? `skill:${toolPanel(data?.name)}` : 'brain'); out = true;
  }
  if (type === 'notice' && data?.source) id = `skill:${data.source}`;
  if (!id && SKILL_RENDER[type]) id = `skill:${type}`;
  if (!id) return;
  const now = performance.now();
  if (now - (lastPulse[id] || 0) < 450) return;
  lastPulse[id] = now;
  for (const p of panels('systems')) {
    const c = body(p)._c, node = c?.nodes[id];
    if (!node) continue;
    node.g.classList.add('ping');
    setTimeout(() => node.g.classList.remove('ping'), 450);
    if (!FX.motion.full) continue;
    const dot = s('circle', { class: `packet${out ? ' out' : ''}`, r: 2.4, cx: 0, cy: 0 });
    c.dots.append(dot);
    const far = `translate(${node.x.toFixed(1)}px, ${node.y.toFixed(1)}px)`, near = 'translate(0px, 0px)';
    const anim = dot.animate([{ transform: out ? near : far, opacity: 0 }, { opacity: 1, offset: 0.15 }, { opacity: 1, offset: 0.85 }, { transform: out ? far : near, opacity: 0 }],
      { duration: 720, easing: 'cubic-bezier(.65,0,.35,1)', fill: 'forwards' });
    anim.onfinish = () => dot.remove();
  }
}
function paintReadouts(list = services()) {
  const a = S.data.assistant || {}, br = S.data.brain || a.brain || {};
  const set = (id, text, cls) => { const el = $(`#${id}`); if (!el) return; el.textContent = text; el.title = text; el.className = cls || ''; };
  set('ro-brain', br.active === 'claude' ? `Claude · ${a.model || ''}` : br.active === 'local' ? (br.local?.model || 'local') : 'built-in only', br.active ? 'iris' : 'warn');
  const ok = list.filter(n => n.status === 'ok').length;
  set('ro-links', `${ok}/${list.length} online`, list.some(n => n.status === 'down') ? 'warn' : 'ok');
  const sk = S.data.skills?.loaded || [];
  set('ro-skills', sk.length ? `${sk.length} · ${sk.map(x => x.name).join(' ')}` : 'none', '');
}

// ---------------------------------------------------------------- display settings (Setup)
render.display = function display() {
  const stored = FX.motion.stored();
  const theme = document.documentElement.dataset.theme || 'auto';
  for (const p of panels('display')) {
    const seg = (opts, cur, fn) => h('span', { class: 'seg' }, opts.map(([id, label, note]) => h('button', { 'aria-pressed': String(cur === id), title: note || '', onclick: () => fn(id) }, label)));
    fill(body(p),
      h('div', { class: 'set-row' }, h('span', { class: 'k' }, 'Motion'),
        seg([['auto', 'Auto', 'Follow Windows (Animation effects)'], ['full', 'Full'], ['reduced', 'Reduced', 'States change, nothing travels or loops'], ['off', 'Off', 'Nothing moves']],
          stored || 'auto', id => { FX.motion.set(id === 'auto' ? null : id); render.display(); toast(`Motion: ${id === 'auto' ? `auto (${FX.motion.mode})` : id}.`); })),
      h('div', { class: 'set-row' }, h('span', { class: 'k' }, 'Theme'),
        seg([['auto', 'Auto', 'Follow Windows light/dark'], ['dark', 'Dark'], ['light', 'Light']], theme, id => setTheme(id))),
      h('div', { class: 'set-row' }, h('span', { class: 'k' }, 'Boot'),
        h('span', {}, h('button', { class: 'btn small', onclick: () => boot(true) }, 'Replay start-up'))),
      h('div', { class: 't2', style: { marginTop: '8px' } },
        `Now: ${FX.motion.mode}. Auto follows Windows → Settings → Accessibility → Visual effects → Animation effects. `
        + 'Reduced keeps every state but drops travel and loops; Off stops all animation. Everything pauses while this window is hidden.'));
  }
};
function setTheme(id) {
  const root = document.documentElement;
  if (id === 'auto') delete root.dataset.theme; else root.dataset.theme = id;
  try { if (id === 'auto') localStorage.removeItem('hud.theme'); else localStorage.setItem('hud.theme', id); } catch { /* storage unavailable */ }
  FX.orb.refresh();
  renderAll();
}

// ---------------------------------------------------------------- command palette (Ctrl+K)
const PAL_COMMANDS = [
  ['Good morning', 'good morning', 'briefing: calendars, tasks, news'],
  ['Plan my day', 'plan my day', 'ranked against your goals'],
  ['Check my email', 'check my email', "who's waiting on a reply"],
  ["What's the weather", "what's the weather", 'now and the next hours'],
  ["What's on my calendar", "what's on my calendar today", 'today, every calendar'],
  ['Start a 25 minute focus session', 'start a 25 minute focus session', 'timer + do not disturb'],
  ['Quiet mode', 'quiet mode', 'no speaking for a while'],
  ['Recap my day', 'recap my day', 'end-of-day summary'],
  ['Where did my time go today', 'where did my time go today', 'activity'],
  ['Remind me in 20 minutes to …', 'remind me in 20 minutes to ', 'a reminder', true],
  ['Clip that', 'clip that', 'Twitch clip of the last 30 s'],
  ['Stop everything', 'stop everything', 'kill switch: pause PC control'],
];
const PAL_ACTIONS = [
  ['Push to talk', () => post('/api/voice/arm'), 'listen without the wake word'],
  ['Mute or unmute the microphone', () => $('#mute-btn').click(), 'mic'],
  ['Show notifications', () => openDrawer(), `${FEED.length} in the center`],
  ['Switch light / dark theme', () => $('#theme-btn').click(), 'display'],
  ['Motion: full', () => { FX.motion.set('full'); render.display(); }, 'display'],
  ['Motion: reduced', () => { FX.motion.set('reduced'); render.display(); }, 'display'],
  ['Motion: off', () => { FX.motion.set('off'); render.display(); }, 'display'],
  ['Run a health check', () => { setView('setup'); $('.view[data-view="setup"] [data-action="doctor"]')?.click(); }, 'setup'],
  ['Rescan this PC', () => { setView('setup'); $('.view[data-view="setup"] [data-action="rescan"]')?.click(); }, 'setup'],
  ['Replay the start-up sequence', () => boot(true), 'display'],
];
const pal = { items: [], sel: 0 };
function togglePalette() { const d = $('#palette'); if (d.open) d.close(); else openPalette(); }
function openPalette(q = '') {
  const d = $('#palette');
  if ($('#drawer').open) $('#drawer').close();
  const input = $('#pal-input');
  input.value = q;
  d.showModal();
  renderPalette();
  input.focus();
}
function fuzzy(q, text) {
  const t = text.toLowerCase(), qq = q.toLowerCase().trim();
  if (!qq) return { score: 0, idx: [] };
  const sub = t.indexOf(qq);
  if (sub >= 0) return { score: 100 + (sub === 0 ? 30 : /[\s·&/(-]/.test(t[sub - 1]) ? 15 : 0) - t.length * 0.05, idx: [...Array(qq.length).keys()].map(i => sub + i) };
  let ti = 0, score = 0, prev = -2;
  const idx = [];
  for (const ch of qq) {
    if (ch === ' ') continue;
    const f = t.indexOf(ch, ti);
    if (f < 0) return null;
    score += 1 + (f === prev + 1 ? 3 : 0) + (f === 0 || /[\s·&/(-]/.test(t[f - 1]) ? 4 : 0) - Math.min(3, (f - ti) * 0.15);
    idx.push(f); prev = f; ti = f + 1;
  }
  if (score < idx.length * 3) return null;  // scattered letters across words: not a match
  return { score: score - t.length * 0.05, idx };
}
function marks(text, idx) {
  if (!idx?.length) return [text];
  const set = new Set(idx), out = [];
  let run = '', on = false;
  for (let i = 0; i < text.length; i++) {
    const m = set.has(i);
    if (m !== on && run) { out.push(on ? h('mark', {}, run) : run); run = ''; }
    on = m; run += text[i];
  }
  if (run) out.push(on ? h('mark', {}, run) : run);
  return out;
}
function paletteSources() {
  const items = [];
  for (const [label, text, sub, partial] of PAL_COMMANDS) items.push({ kind: 'cmd', group: 'Commands', label, sub, weight: 6, complete: text, run: () => partial ? openPalette(text) : command(text) });
  const seen = new Set();
  for (const m of [...S.log].reverse()) {
    if (m.role !== 'user' || !m.text || seen.has(m.text.toLowerCase())) continue;
    seen.add(m.text.toLowerCase());
    items.push({ kind: 'recent', group: 'Recent', label: m.text, sub: m.ts ? ago(m.ts) : 'recent', weight: 3, complete: m.text, run: () => command(m.text) });
    if (seen.size >= 6) break;
  }
  for (const b of $$('.tabs button')) {
    const view = b.dataset.view, label = b.firstChild.textContent.trim();
    items.push({ kind: 'view', group: 'Go to', label: `${label} view`, sub: 'tab', weight: 8, run: () => setView(view) });
  }
  for (const p of $$('.panel[data-panel]')) {
    const view = p.closest('.view')?.dataset.view;
    const title = $('h2', p)?.textContent.trim() || (p.dataset.panel === 'stats' ? `Headline numbers` : p.dataset.panel);
    items.push({ kind: 'panel', group: 'Panels', label: title, sub: view, weight: view === S.view ? 2 : 0, run: () => jumpTo(p) });
  }
  for (const [label, fn, sub] of PAL_ACTIONS) items.push({ kind: 'act', group: 'Actions', label, sub, weight: 1, run: fn });
  return items;
}
function renderPalette() {
  const input = $('#pal-input'), list = $('#pal-list'), q = input.value;
  $('#palette').classList.toggle('typing', !!q.trim());
  const name = S.data.assistant?.name || 'Vesper';
  const ask = q.trim() ? { kind: 'ask', group: '', label: `Ask ${name}: “${q.trim()}”`, sub: 'send as a command', run: () => command(q.trim()) } : null;
  let items;
  if (!q.trim()) {
    const all = paletteSources();
    const by = g => all.filter(x => x.group === g);
    items = [...by('Commands').slice(0, 6), ...by('Recent').slice(0, 4), ...by('Go to'), ...by('Panels').filter(x => x.sub === S.view), ...by('Actions').slice(0, 3)];
  } else {
    items = paletteSources().map(x => ({ ...x, m: fuzzy(q, x.label) })).filter(x => x.m)
      .sort((a, b) => (b.m.score + b.weight) - (a.m.score + a.weight)).slice(0, 14);
    const strong = items.length && items[0].m.score >= 100;
    if (ask) { if (strong) items.push({ ...ask, group: 'Ask' }); else items.unshift({ ...ask, group: '' }); }
    items = items.map(x => ({ ...x, group: q.trim() ? (x.kind === 'ask' ? '' : 'Results') : x.group }));
  }
  pal.items = items;
  pal.sel = 0;
  const rows = [];
  let last = null;
  items.forEach((x, i) => {
    if (x.group && x.group !== last) rows.push(h('li', { class: 'pal-group', role: 'presentation' }, x.group));
    last = x.group;
    rows.push(h('li', { class: 'pal-item', id: `pal-opt-${i}`, role: 'option', 'aria-selected': 'false', 'data-kind': x.kind,
      onclick: () => runPalette(i), onpointermove: () => { if (pal.sel !== i) selectPalette(i); } },
      h('span', { class: 'p-ic' }, icon({ cmd: 'cmd', recent: 'recent', view: 'view', panel: 'panel', act: 'gear', ask: 'ask' }[x.kind])),
      h('span', { class: 'p-txt' }, marks(x.label, x.m?.idx)), x.sub ? h('span', { class: 'p-sub' }, x.sub) : null, h('span', { class: 'p-go', 'aria-hidden': 'true' }, '↵')));
  });
  if (!items.length) rows.push(h('li', { class: 'pal-empty', role: 'presentation' }, 'Type a command, or a panel to jump to.'));
  fill(list, rows);
  selectPalette(0);
}
function selectPalette(i) {
  const list = $('#pal-list');
  if (!pal.items.length) { $('.pal-bar').style.opacity = '0'; $('#pal-input').removeAttribute('aria-activedescendant'); return; }
  pal.sel = (i + pal.items.length) % pal.items.length;
  for (const el of $$('.pal-item', list)) el.setAttribute('aria-selected', 'false');
  const row = $(`#pal-opt-${pal.sel}`);
  if (!row) return;
  row.setAttribute('aria-selected', 'true');
  $('#pal-input').setAttribute('aria-activedescendant', row.id);
  const bar = $('.pal-bar');
  bar.style.opacity = '1';
  bar.style.height = `${row.offsetHeight}px`;
  bar.style.transform = `translateY(${list.offsetTop + row.offsetTop}px)`;
  row.scrollIntoView({ block: 'nearest' });
}
function runPalette(i = pal.sel) {
  const x = pal.items[i];
  if (!x) return;
  $('#palette').close();
  x.run();
}
function jumpTo(p) {
  const view = p.closest('.view')?.dataset.view;
  if (view && view !== S.view) setView(view);
  requestAnimationFrame(() => {
    p.scrollIntoView({ behavior: FX.motion.full ? 'smooth' : 'auto', block: 'center' });
    setActive(p, { ms: 2200 });
    const f = p.querySelector('input, button, a, select, [tabindex]');
    if (!f) { p.setAttribute('tabindex', '-1'); p.focus({ preventScroll: true }); }
  });
}

// ---------------------------------------------------------------- boot: once a session, ~1.2 s, from real state
let resolveState;
const STATE_READY = new Promise(r => { resolveState = v => { r(v); resolveState = () => {}; }; });
const wait = ms => new Promise(r => setTimeout(r, ms));
function bootLines(st) {
  if (!st) return [['LINK', 'waiting for Vesper…', 'warn']];
  const a = st.assistant || {}, br = a.brain || {};
  const v = st.voice || {};
  const list = services();
  const ok = list.filter(n => n.status === 'ok').length;
  const sk = st.skills?.loaded || [];
  return [
    ['HUD', `2.0 · build ${(HUD_VERSION || 'dev').slice(0, 7)}`, 'ion'],
    ['BRAIN', br.active === 'claude' ? `claude · ${a.model}` : br.active === 'local' ? `${br.local?.model} @ ${br.local?.server}` : 'built-in commands', br.active ? '' : 'warn'],
    ['VOICE', v.state || 'off', ['listening', 'armed', 'hearing'].includes(v.state) ? '' : 'warn'],
    ['LINKS', `${ok}/${list.length} online`, list.some(n => n.status === 'down') ? 'warn' : ''],
    ['SKILLS', sk.length ? `${sk.length} loaded · ${sk.map(x => x.name).join(', ')}` : 'none', ''],
    ['READY', a.user ? `hello, ${a.user}` : 'online', 'ion'],
  ];
}
async function boot(replay = false) {
  const root = document.documentElement;
  if (replay) {
    if (!FX.motion.full) { toast('The start-up sequence plays with motion set to Full.'); return; }
    const mark = $('.boot-mark');
    mark.replaceWith(mark.cloneNode(true));  // restart its draw-in
    $('.boot-name').replaceWith($('.boot-name').cloneNode(true));
    root.dataset.boot = 'on';
  }
  if (root.dataset.boot !== 'on') return;
  try { sessionStorage.setItem('hud.booted', '1'); } catch { /* storage unavailable */ }
  const started = performance.now();
  const lines = $('#boot-lines');
  fill(lines);
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    removeEventListener('keydown', finish, true); removeEventListener('pointerdown', finish, true);
    root.dataset.boot = 'out';
    setTimeout(() => { if (root.dataset.boot === 'out') delete root.dataset.boot; moveInk(true); }, 440);
    announce(`${S.data.assistant?.name || 'Vesper'} ready`);
  };
  addEventListener('keydown', finish, true); addEventListener('pointerdown', finish, true);
  const st = await Promise.race([STATE_READY, wait(700)]);
  await wait(Math.max(0, 420 - (performance.now() - started)));
  for (const [k, v, cls] of bootLines(st)) {
    if (done) return;
    const val = h('span', { class: `v ${cls}` });
    lines.append(h('li', {}, h('span', { class: 'k' }, k), h('span', { class: 'dots' }), val));
    FX.reveal(val, v, { mode: 'decode', ms: 220 });
    await wait(78);
  }
  await wait(300);
  finish();
}

// ---------------------------------------------------------------- wiring
function initChrome() {
  for (const v of $$('.view')) stagger(v);
  FX.spotlight($('main'));
  strip.hint();
  fill($('#quick'), PAL_COMMANDS.slice(0, 5).map(([label, text]) => h('button', { class: 'chip-btn', type: 'button', onclick: () => command(text) }, label)));
  $('#palette-btn').addEventListener('click', () => openPalette());
  $('#bell-btn').addEventListener('click', openDrawer);
  $('#drawer-close').addEventListener('click', () => $('#drawer').close());
  $('#drawer-read').addEventListener('click', () => { markRead(); drawerSeenAt = feedSeen; renderDrawer(); });
  $('#drawer-clear').addEventListener('click', () => { FEED.length = 0; markRead(); renderDrawer(); });
  for (const d of [$('#drawer'), $('#palette')]) d.addEventListener('click', e => { if (e.target === d) d.close(); });  // the backdrop
  const input = $('#pal-input');
  input.addEventListener('input', renderPalette);
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') { e.preventDefault(); selectPalette(pal.sel + 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); selectPalette(pal.sel - 1); }
    else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); if (input.value.trim()) { $('#palette').close(); command(input.value.trim()); } }
    else if (e.key === 'Enter') { e.preventDefault(); runPalette(); }
    else if (e.key === 'Tab' && !e.shiftKey) {
      const x = pal.items[pal.sel];
      if (x && x.complete) { e.preventDefault(); input.value = x.complete; renderPalette(); }
    }
  });
  $('#palette').addEventListener('close', () => { $('#palette').classList.remove('typing'); });
  FX.motion.on(() => { render.display(); });
  paintBell();
  moveInk(true);
  document.fonts?.ready.then(() => moveInk(true));
}

// ---------------------------------------------------------------- live updates
const sticky = new Set(['system', 'obs', 'twitch', 'activity', 'projects', 'calendar', 'news', 'briefing', 'discovery', 'doctor', 'health', 'voice_profile']);
function onEvent(ev) {
  const { type, data } = ev;
  if (type === 'level') { FX.orb.setLevel(data?.rms); return; }  // up to 20/s while listening or speaking: nothing else
  pulse(type, data);
  if (sticky.has(type)) {
    S.data[type] = data;
    const fns = { system: ['vitals', 'stats'], obs: ['obs', 'stats'], twitch: ['twitch', 'stats'], activity: ['activity', 'stats'],
      projects: ['projects', 'stats'], calendar: ['agenda', 'stats'], news: ['news'], briefing: ['briefing'],
      discovery: ['connections'], doctor: ['doctor'], health: ['health'], voice_profile: ['voice', 'triggers'] }[type];
    for (const f of fns) if (!(f === 'vitals' && document.hidden)) render[f]();
    if (SYSTEM_EVENTS.has(type)) queueSystems();
    return;
  }
  switch (type) {
    case 'user_said': S.log.push({ role: 'user', text: data.text, source: data.source, ts: ev.ts }); strip.you(data.text); render.log(); break;
    case 'assistant_said':
      if (data.text) { S.log.push({ role: 'assistant', text: data.text, source: data.source, ms: data.ms, ts: ev.ts }); render.log(); }
      strip.reply(data.text || '');
      if ('pending' in data) paintPending(data.pending);
      break;
    case 'heard': if (!data.wake && !data.armed) strip.you(`(ignored) “${data.text}”`, { ignored: true }); break;
    case 'thinking': orbState.thinking = data.active; paintOrb(); chips.think(data.active); break;
    case 'speaking': orbState.speaking = data.active; paintOrb(); break;
    case 'voice_state': orbState.voice = data.state; S.data.voice = data; paintOrb(); queueSystems(); break;
    case 'wake': orbState.voice = 'armed'; paintOrb(); wakeFlare(); break;
    case 'pending': paintPending(data); break;
    case 'pc_control': S.data.pc_control = data; paintHalt(); render.control(); break;
    case 'stream_alert': toast(data.text, { source: 'stream', level: data.level }); feed({ source: 'stream', text: data.text, level: data.level, ts: ev.ts }); break;
    case 'twitch_auth': twitchPatch(t => { t.auth = data; if (data.connected) t.needs_login = false; }); break;
    case 'twitch_events_state': twitchPatch(t => { t.events = data; }); break;
    case 'twitch_event': twitchPatch(t => { t.feed = t.feed || { events: [] }; t.feed.events = [{ ...data, ts: data.ts || ev.ts }, ...(t.feed.events || [])].slice(0, 25); }); break;
    case 'highlight': {
      twitchPatch(t => { t.highlights = [data, ...(t.highlights || [])].slice(0, 20); });
      const text = `Highlight: ${data.reason}${data.marker ? ' (marker added)' : ''}`;
      toast(text, { source: 'highlight', level: 'good' }); feed({ source: 'highlight', text, level: 'good', ts: ev.ts });
      break;
    }
    case 'prestream': S.data.prestream = data; render.obs(); break;
    case 'tool':
      if (S.data.pc_control) refreshControl();
      if (['add_task', 'complete_task'].includes(data.name)) refreshTasks();
      chips.tool(data.name, data.result);
      setActive(panelFor(toolPanel(data.name)), { color: 'var(--iris)' });
      break;
    case 'speak': speak(data); break;
    case 'speak_stop': if ('speechSynthesis' in window) speechSynthesis.cancel(); break;
    case 'tts': S.data.tts = data; render.speech(); break;
    case 'brain': S.data.brain = data; render.brain(); paintOrb(); queueSystems(); break;
    case 'brain_bench': S.data.brainBench = data; render.brain(); break;
    case 'goals': S.data.goals = data; if (!S.editGoals) render.tasks(); break;
    case 'library': S.data.library = data; render.library(); queueSystems(); break;
    case 'google': S.data.google = data; render.google(); queueSystems(); break;
    case 'gmail': S.data.gmail = data; render.inbox(); queueSystems(); break;
    case 'wake_word': (S.wakeScores = S.wakeScores || {})[data.name] = data.score; if (S.view === 'setup') render.triggers(); break;
    case 'calibration': {
      const prev = S.data.calibration;
      S.data.calibration = { ...(data.level != null && prev ? prev : {}), ...data };
      const meterEl = $('#cal-meter i');
      if (data.level != null && meterEl && prev && prev.prompt === data.prompt) meterEl.style.width = `${Math.min(100, data.level / 30)}%`;
      else render.voice();
      if (['done', 'error', 'cancelled'].includes(data.step)) {
        refreshVoice();
        if (data.step === 'done') toast('Voice calibrated.');
      }
      break;
    }
    case 'announce': toast(data.text, { source: 'vesper', level: 'vesper' }); feed({ source: 'vesper', text: data.text, level: 'info', ts: ev.ts }); break;
    case 'profile': S.data.active_profile = data?.active; paintProfile(); render.stats(); break;
    case 'tasks': S.data.tasks = data; render.tasks(); render.stats(); break;
    case 'job': {
      const jobs = S.data.jobs || [];
      const i = jobs.findIndex(j => j.id === data.id);
      if (i >= 0) jobs[i] = data; else jobs.unshift(data);
      S.data.jobs = jobs; render.jobs();
      break;
    }
    case 'notice': notice(data); break;
    default:
      if (SKILL_RENDER[type]) { S.data[type] = data; paintSkill(type); queueSystems(); }
  }
}

let socket, retry = 0;
function connect() {
  socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`);
  socket.onopen = () => { retry = 0; document.body.classList.remove('stale'); load(); };
  socket.onmessage = m => { try { onEvent(JSON.parse(m.data)); } catch (err) { console.error(err); } };
  socket.onclose = () => {
    document.body.classList.add('stale');
    $('#voice-status').textContent = 'Reconnecting to the assistant…';
    setTimeout(connect, Math.min(2000, 500 * 2 ** retry++));  // back within seconds after an update restarts Vesper
  };
}

async function load() {
  try {
    const st = await api('/api/state');
    if (st.hud_version && HUD_VERSION && st.hud_version !== HUD_VERSION) return location.reload();  // Vesper was updated
    S.data = { ...S.data, ...st };
    for (const [name, data] of Object.entries(st.skills?.data || {})) if (data != null) S.data[name] = data;
    S.log = (st.log || []).map(l => ({ role: l.role, text: l.text, source: l.source, ts: l.ts }));
    orbState.voice = st.voice?.state || 'off';
    $('#mute-btn').setAttribute('aria-pressed', String(!!st.voice?.muted));
    paintOrb(); paintPending(st.pending); paintProfile(); paintHalt();
    renderAll();
    hydrateFeed();
    strip.restore();
    resolveState(st);
  } catch (err) { console.error(err); resolveState(null); }
}

try { const v = localStorage.getItem('hud.view'); if (v && $(`.view[data-view="${v}"]`)) setView(v); } catch { /* storage unavailable */ }
function viewFromHash() { const v = location.hash.slice(1); if (v && $(`.view[data-view="${v}"]`)) setView(v); }
window.addEventListener('hashchange', viewFromHash);
viewFromHash();  // the tray's "Setup & health" opens #setup
if ('speechSynthesis' in window) speechSynthesis.onvoiceschanged = () => {};
initChrome();
connect();
boot();
setInterval(() => { if (!document.hidden) { render.agenda(); render.projects(); } }, 60000);
