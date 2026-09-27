'use strict';
/* HUD dashboard. All dynamic text goes in via textContent (h() below) — window
   titles, calendar events and headlines are untrusted strings. */

const TOKEN = document.querySelector('meta[name="token"]').content;
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
function toast(text) {
  const t = h('div', { class: 'toast' }, text);
  document.body.append(t);
  setTimeout(() => t.remove(), 6000);
}

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
function sparkline(container, points, { max, color = 'var(--series-1)', fmt = v => v, label = '' }) {
  const w = Math.max(container.clientWidth || 220, 60), ht = 44;
  const vals = points.map(p => p.v);
  const valid = vals.filter(v => v != null);
  const svgEl = s('svg', { class: 'spark', viewBox: `0 0 ${w} ${ht}`, role: 'img', 'aria-label': `${label} trend` });
  container.replaceChildren(svgEl);
  if (valid.length < 2) return;
  const mx = max ?? Math.max(...valid, 1) * 1.1;
  const n = points.length;
  const x = i => 1 + (i / (n - 1)) * (w - 8);
  const y = v => ht - 3 - (Math.min(v ?? 0, mx) / mx) * (ht - 10);
  const d = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join('');
  svgEl.append(
    s('line', { x1: 0, x2: w, y1: ht - 3, y2: ht - 3, stroke: 'var(--baseline)', 'stroke-width': 1 }),
    s('path', { d: `${d}L${x(n - 1)},${ht - 3}L${x(0)},${ht - 3}Z`, fill: color, opacity: 0.1 }),
    s('path', { d, fill: 'none', stroke: color, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }),
    s('circle', { cx: x(n - 1), cy: y(vals[n - 1]), r: 4, fill: color, stroke: 'var(--panel)', 'stroke-width': 2 }));
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
  svgEl.append(s('rect', { x: 0, y: top, width: w, height: bh, fill: 'rgba(255,255,255,0.03)', rx: 4 }));
  for (const b of timeline || []) {
    const x0 = X(b.start), x1 = X(b.end);
    if (x1 - x0 < 0.5) continue;
    const r = s('rect', { x: x0 + 1, y: top, width: Math.max(x1 - x0 - 2, 1), height: bh, fill: catColor(b.category), rx: 2, tabindex: 0 });
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

function meter(pct, warnAt = 80, critAt = 92) {
  const cls = pct >= critAt ? 'crit' : pct >= warnAt ? 'warn' : '';
  return h('div', { class: `meter ${cls}`, role: 'meter', 'aria-valuenow': pct, 'aria-valuemin': 0, 'aria-valuemax': 100 },
    h('i', { style: { width: `${Math.min(100, pct)}%` } }));
}

// ---------------------------------------------------------------- panels
function panels(name) { return $$(`.panel[data-panel="${name}"]`); }
function body(p) { return $('.body', p); }
function meta(p, text) { const m = $('[data-slot="meta"]', p); if (m) m.replaceChildren(text instanceof Node ? text : document.createTextNode(text || '')); }

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
    const tile = (label, value, sub, hero) => h('div', { class: `stat${hero ? ' hero' : ''}` },
      h('div', { class: 'label' }, label), h('div', { class: 'value' }, value), sub ? h('div', { class: 'sub' }, sub) : null);
    for (const p of panels('stats')) {
      const mode = p.dataset.mode;
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
      if (!sys) { fill(b, empty('Waiting for first sample…')); continue; }
      const hist = sys.history || [];
      const gpu = (sys.gpus || [])[0];
      const rows = [
        ['CPU', `${Math.round(sys.cpu.percent)}%`, `${sys.cpu.cores || '?'}C/${sys.cpu.threads}T${sys.cpu.freq_mhz ? ` · ${(sys.cpu.freq_mhz / 1000).toFixed(1)} GHz` : ''}`, hist.map(x => ({ v: x.cpu, ts: x.ts })), 100, v => `${Math.round(v)}%`],
        ['Memory', `${Math.round(sys.memory.percent)}%`, `${sys.memory.used_gb} / ${sys.memory.total_gb} GB`, hist.map(x => ({ v: x.mem, ts: x.ts })), 100, v => `${Math.round(v)}%`],
      ];
      if (gpu) rows.push(['GPU', `${Math.round(gpu.util ?? 0)}%`, `${gpu.name.replace('NVIDIA GeForce ', '')}${gpu.temp_c != null ? ` · ${Math.round(gpu.temp_c)}°C` : ''}${gpu.encoder_util != null ? ` · NVENC ${gpu.encoder_util}%` : ''}`, hist.map(x => ({ v: x.gpu, ts: x.ts })), 100, v => `${Math.round(v)}%`]);
      rows.push(['Network', fmtRate(sys.net.down_bps), `↑ ${fmtRate(sys.net.up_bps)}`, hist.map(x => ({ v: x.down, ts: x.ts })), undefined, fmtRate]);
      const frag = [];
      for (const [k, v, sub, pts, max, fmt] of rows) {
        const wrap = h('div', { class: 'spark-wrap' });
        frag.push(h('div', { class: 'vital' }, h('div', {}, h('div', { class: 'k' }, k), h('div', { class: 'v num' }, v), h('div', { class: 's' }, sub)), wrap));
        requestAnimationFrame(() => sparkline(wrap, pts, { max, fmt, label: k }));
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
      fill(b, ...frag, cores,
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
        items.push(h('div', { class: 'countdown' }, 'Next: ', h('b', {}, next.title), ` in ${mins < 90 ? `${mins} min` : fmtDur(mins * 60)}`));
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
          h('div', { class: 'when' }, e.all_day ? 'All day' : `${fmtTime(e.start)}–${fmtTime(e.end)}`),
          h('span', { class: 'cd', style: { background: e.color } }),
          h('div', {}, h('div', { class: 'title' }, e.title), h('div', { class: 'sub' }, [e.calendar, e.location].filter(Boolean).join(' · ')))));
      }
      if (!events.length) items.push(empty(profile ? `Nothing on ${catLabel(profile).toLowerCase()} calendars.` : 'Nothing scheduled.'));
      const cals = cal.calendars.filter(c => !profile || c.profile === profile);
      meta(p, `${events.filter(e => !e.all_day).length} events`);
      const legend = cals.length > 1 ? h('div', { class: 'legend' }, cals.map(c => h('span', { class: 'key' },
        h('span', { class: 'sw', style: { background: c.color, borderRadius: '50%' } }), c.name, c.error ? status('critical', 'error') : null))) : null;
      const errs = cals.filter(c => c.error).map(c => h('div', { class: 'empty' }, status('critical', `${c.name}: ${c.error}`)));
      fill(b, h('div', { class: 'agenda' }, items), legend, ...errs);
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
      const dropLevel = st.dropped_pct >= 2 ? ['critical', 'Dropping'] : st.dropped_pct >= 0.5 ? ['warning', 'Watch'] : ['good', 'Healthy'];
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
        fill(b, empty('Twitch stats are off. Set twitch.enabled, channel, and TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET (see README).'));
        continue;
      }
      if (tw.error) { fill(b, empty(`Twitch error: ${tw.error}`)); continue; }
      fill(b, 
        h('div', { class: 'obs-top' }, h('span', { class: `badge-live ${tw.live ? '' : 'off'}` }, tw.live ? 'LIVE' : 'OFFLINE'), h('span', { class: 'ink2' }, tw.channel)),
        tw.live ? h('div', { class: 'stats' },
          h('div', { class: 'stat' }, h('div', { class: 'label' }, 'Viewers'), h('div', { class: 'value' }, compact(tw.viewers))),
          h('div', { class: 'stat' }, h('div', { class: 'label' }, 'Uptime'), h('div', { class: 'value' }, fmtDur(tw.uptime_s)))) : null,
        tw.title ? h('div', { class: 'item' }, h('div', { class: 'main' }, h('div', { class: 't1' }, tw.title), h('div', { class: 't2' }, tw.game || ''))) : null);
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
      if (!profile) {
        parts.push(h('div', { class: 'sub-h' }, 'North star'),
          goals.north_star ? h('div', { class: 'north' }, goals.north_star) : empty('Not set — add goals.north_star in config.yaml. Priorities are guesses without it.'));
        if (goals.this_week?.length) parts.push(h('div', { class: 'sub-h' }, 'This week'), h('ul', { class: 'ink2', style: { margin: 0, paddingLeft: '18px' } }, goals.this_week.map(g => h('li', {}, g))));
        parts.push(h('div', { class: 'sub-h' }, 'Tasks'));
      }
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
        h('span', { class: 'meta' }, [m.source === 'voice' ? '🎙 voice' : m.source === 'heard' ? 'heard (no wake word)' : null,
          m.ms != null ? `${m.ms} ms` : null, m.ts ? fmtClock(new Date(m.ts * 1000)) : null].filter(Boolean).join(' · ')))));
      if (!S.log.length) box.append(empty(`Try: “${S.data.assistant?.name || 'Jarvis'}, good morning” · “open discord” · “switch to BRB” · “where am I”`));
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
      if (!br) { fill(b, empty(`Say “${S.data.assistant?.name || 'Jarvis'}, good morning” or click Good morning. It pulls calendars, yesterday's activity, projects, tasks and news.`)); continue; }
      fill(b, 
        h('div', { class: 'brief-head' }, br.headline || ''),
        h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '-6px', marginBottom: '10px' } },
          `${br.kind === 'recap' ? 'End-of-day recap' : 'Morning briefing'} · ${ago(br.created)} · ${br.generated_by === 'claude' ? 'planned by Claude' : 'local summary (add an API key for a real plan)'}`),
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
  const badge = $('#setup-badge');
  const todo = d ? d.findings.filter(f => f.status === 'action').length : 0;
  badge.textContent = todo; badge.classList.toggle('hidden', !todo);
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

render.doctor = function doctor() {
  const r = S.data.doctor;
  for (const p of panels('doctor')) {
    const b = body(p);
    if (!r) { fill(b, empty('Runs live checks: Claude key, OBS connection, each calendar, news feeds, Chrome, Claude Code CLI, voice.')); continue; }
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

function renderAll() { for (const fn of Object.values(render)) { try { fn(); } catch (err) { console.error(err); } } }

// ---------------------------------------------------------------- actions
async function tool(name, args) {
  try { return await post(`/api/tool/${name}`, args || {}); } catch (err) { toast(`Failed: ${err.message}`); return { ok: false }; }
}
async function command(text) {
  if (!text.trim()) return;
  $('#reply').textContent = '…';
  try {
    const res = await post('/api/command', { text });
    if (res.data && (res.kind === 'briefing' || res.kind === 'recap')) { S.data.briefing = res.data; render.briefing(); }
  } catch (err) { $('#reply').textContent = `Couldn't reach the assistant (${err.message}).`; }
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
  } else if (action === 'new-research') {
    const prompt = window.prompt('Research topic — Claude will search the web and write a brief in the background:');
    if (prompt) { const r = await post('/api/jobs', { kind: 'research', prompt }); toast(r.ok ? `Research job ${r.job_id} started.` : r.error); }
  }
});

$('#cmd-form').addEventListener('submit', e => { e.preventDefault(); const i = $('#cmd'); command(i.value); i.value = ''; });
document.addEventListener('keydown', e => {
  if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') { e.preventDefault(); $('#cmd').focus(); }
});
$('#confirm-btn').addEventListener('click', () => post('/api/confirm'));
$('#cancel-btn').addEventListener('click', () => post('/api/cancel'));
$('#mic-btn').addEventListener('click', () => post('/api/voice/arm'));
$('#mute-btn').addEventListener('click', e => {
  const muted = e.currentTarget.getAttribute('aria-pressed') !== 'true';
  e.currentTarget.setAttribute('aria-pressed', String(muted));
  post('/api/voice/mute', { muted });
});
for (const t of $$('.tabs button')) t.addEventListener('click', () => setView(t.dataset.view));
function setView(view) {
  S.view = view;
  try { localStorage.setItem('hud.view', view); } catch { /* storage unavailable */ }
  for (const t of $$('.tabs button')) t.setAttribute('aria-selected', String(t.dataset.view === view));
  for (const v of $$('.view')) v.hidden = v.dataset.view !== view;
  renderAll();
}

// ---------------------------------------------------------------- voice / status chrome
const orb = $('#orb');
const chrome = { voice: 'off', thinking: false, speaking: false };
function paintOrb() {
  orb.className = `orb ${chrome.speaking ? 'speaking' : chrome.thinking ? 'thinking' : chrome.voice}`;
  const a = S.data.assistant || {};
  const labels = {
    listening: `Listening for “${(a.wake_words || ['jarvis'])[0]}”`, hearing: 'Hearing you…', transcribing: 'Transcribing…',
    armed: 'Go ahead — listening', muted: 'Mic muted', loading: 'Loading speech model…', disabled: 'Voice disabled',
    unavailable: 'Voice unavailable', error: 'Voice error', off: 'Voice off',
  };
  let txt = chrome.speaking ? 'Speaking…' : chrome.thinking ? 'Thinking…' : labels[chrome.voice] || chrome.voice;
  if ((chrome.voice === 'error' || chrome.voice === 'unavailable') && S.data.voice?.error) txt = S.data.voice.error;
  txt += a.claude ? ` · Claude ${a.model}` : ' · local mode (no API key)';
  $('#voice-status').textContent = txt;
  $('#voice-status').title = txt;
}
function paintPending(p) {
  $('#pending').classList.toggle('hidden', !p);
  $('#pending-text').textContent = p ? `${p.text[0].toUpperCase()}${p.text.slice(1)}?` : '';
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

// ---------------------------------------------------------------- live updates
const sticky = new Set(['system', 'obs', 'twitch', 'activity', 'projects', 'calendar', 'news', 'briefing', 'discovery', 'doctor']);
function onEvent(ev) {
  const { type, data } = ev;
  if (sticky.has(type)) {
    S.data[type] = data;
    const fns = { system: ['vitals', 'stats'], obs: ['obs', 'stats'], twitch: ['twitch', 'stats'], activity: ['activity', 'stats'],
      projects: ['projects', 'stats'], calendar: ['agenda', 'stats'], news: ['news'], briefing: ['briefing'],
      discovery: ['connections'], doctor: ['doctor'] }[type];
    for (const f of fns) if (!(f === 'vitals' && document.hidden)) render[f]();
    return;
  }
  switch (type) {
    case 'user_said': S.log.push({ role: 'user', text: data.text, source: data.source, ts: ev.ts }); $('#heard').textContent = `“${data.text}”`; render.log(); break;
    case 'assistant_said':
      if (data.text) { S.log.push({ role: 'assistant', text: data.text, source: data.source, ms: data.ms, ts: ev.ts }); render.log(); }
      $('#reply').replaceChildren(h('b', {}, `${S.data.assistant?.name || 'Assistant'}: `), data.text || '');
      if ('pending' in data) paintPending(data.pending);
      break;
    case 'heard': if (!data.wake && !data.armed) { $('#heard').textContent = `(ignored) “${data.text}”`; } break;
    case 'thinking': chrome.thinking = data.active; paintOrb(); break;
    case 'speaking': chrome.speaking = data.active; paintOrb(); break;
    case 'voice_state': chrome.voice = data.state; S.data.voice = data; paintOrb(); break;
    case 'wake': chrome.voice = 'armed'; paintOrb(); break;
    case 'pending': paintPending(data); break;
    case 'speak': speak(data); break;
    case 'announce': toast(data.text); break;
    case 'profile': S.data.active_profile = data?.active; paintProfile(); render.stats(); break;
    case 'tasks': S.data.tasks = data; render.tasks(); render.stats(); break;
    case 'job': {
      const jobs = S.data.jobs || [];
      const i = jobs.findIndex(j => j.id === data.id);
      if (i >= 0) jobs[i] = data; else jobs.unshift(data);
      S.data.jobs = jobs; render.jobs();
      break;
    }
    case 'tool':
      if (['add_task', 'complete_task'].includes(data.name)) refreshTasks();
      break;
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
    setTimeout(connect, Math.min(10000, 500 * 2 ** retry++));
  };
}

async function load() {
  try {
    const st = await api('/api/state');
    S.data = { ...S.data, ...st };
    S.log = (st.log || []).map(l => ({ role: l.role, text: l.text, source: l.source, ts: l.ts }));
    chrome.voice = st.voice?.state || 'off';
    $('#mute-btn').setAttribute('aria-pressed', String(!!st.voice?.muted));
    paintOrb(); paintPending(st.pending); paintProfile();
    renderAll();
  } catch (err) { console.error(err); }
}

try { const v = localStorage.getItem('hud.view'); if (v && $(`.view[data-view="${v}"]`)) setView(v); } catch { /* storage unavailable */ }
if ('speechSynthesis' in window) speechSynthesis.onvoiceschanged = () => {};
connect();
setInterval(() => { if (!document.hidden) { render.agenda(); render.projects(); } }, 60000);
