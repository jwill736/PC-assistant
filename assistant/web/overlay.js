'use strict';
/* OBS overlay. Read-only: its key opens /overlay/state and /overlay/ws, which send only the whitelisted pieces
   (assistant/overlay.py). Every string goes in through textContent. Panels: ?panels=nowplaying,goals,mood,countdown,
   followers,timers (copilot = mood). Empty panels fold away so nothing sits on stream with a dash in it. */
(() => {
  const q = new URLSearchParams(location.search);
  const KEY = q.get('key') || '';
  const ALIAS = { music: 'nowplaying', spotify: 'nowplaying', copilot: 'mood', chat: 'mood', subgoal: 'goals', goal: 'goals',
    ticker: 'followers', follows: 'followers', events: 'followers', alerts: 'followers', timer: 'timers' };
  const ALL = ['nowplaying', 'goals', 'mood', 'countdown', 'followers', 'timers'];
  const wanted = [...new Set((q.get('panels') || 'nowplaying,goals,mood').toLowerCase().split(/[,\s]+/).map(p => ALIAS[p] || p).filter(p => ALL.includes(p)))];
  const showEmpty = q.get('show') === 'all';
  const D = {};
  const root = document.documentElement;
  const still = () => root.dataset.motion === 'off';

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v; else if (k === 'style') Object.assign(el.style, v); else el.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
    return el;
  }
  function svg(tag, attrs, ...kids) {
    const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    for (const kid of kids) el.append(kid);
    return el;
  }
  const clock = s => {
    s = Math.max(0, Math.round(s));
    const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
    return hh ? `${hh}:${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}` : `${mm}:${String(ss).padStart(2, '0')}`;
  };
  /* Swap a text in place: the old line lifts out, the new one rises in (opacity/transform only). */
  function swap(el, text) {
    text = text == null ? '' : String(text);
    if (el.dataset.text === text) return;
    el.dataset.text = text;
    if (still() || !el.isConnected || !el.textContent) { el.textContent = text; return; }
    el.animate([{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(-6px)' }], { duration: 160, easing: 'cubic-bezier(.55,0,1,.45)' })
      .onfinish = () => {
        el.textContent = text;
        el.animate([{ opacity: 0, transform: 'translateY(8px)' }, { opacity: 1, transform: 'none' }], { duration: 320, easing: 'cubic-bezier(.23,1,.32,1)' });
      };
  }

  // ---- panels: built once, then updated in place
  const P = {};
  const box = document.getElementById('ov');
  function card(name, kicker, ...kids) {
    const el = h('section', { class: `card c-${name}`, 'data-panel': name, 'data-empty': '' },
      h('div', { class: 'kicker' }, h('span', { class: 'k-dot', 'aria-hidden': 'true' }), h('span', { class: 'k-txt' }, kicker)), ...kids);
    box.append(el);
    return el;
  }
  function setEmpty(name, empty) {
    const el = P[name]?.el;
    if (!el) return;
    el.toggleAttribute('data-empty', empty && !showEmpty);
  }

  const build = {
    nowplaying() {
      const art = h('div', { class: 'art' }), track = h('div', { class: 'big track' }), artist = h('div', { class: 'sub artist' });
      const bar = h('div', { class: 'bar' }, h('i'));
      const eq = h('span', { class: 'eq', 'aria-hidden': 'true' }, h('i'), h('i'), h('i'));
      const el = card('nowplaying', 'Now playing', h('div', { class: 'np' }, art, h('div', { class: 'np-txt' }, track, artist, bar)));
      el.querySelector('.kicker').append(eq);
      return { el, art, track, artist, fill: bar.firstChild };
    },
    goals() {
      const list = h('div', { class: 'goals' });
      return { el: card('goals', 'Goals', list), list, rows: {} };
    },
    mood() {
      const marker = h('span', { class: 'marker' });
      const word = h('div', { class: 'big mood-word' }), rate = h('div', { class: 'sub' });
      const el = card('mood', 'Chat mood', h('div', { class: 'mood' }, word, h('div', { class: 'track' }, h('span', { class: 'mid' }), marker),
        h('div', { class: 'ends', 'aria-hidden': 'true' }, h('span', {}, 'cold'), h('span', {}, 'hype')), rate));
      return { el, marker, word, rate };
    },
    countdown() {
      const time = h('div', { class: 'huge num' }), label = h('div', { class: 'sub' });
      const el = card('countdown', 'Countdown', time, label);
      return { el, time, label, kick: el.querySelector('.k-txt') };
    },
    followers() {
      const list = h('ol', { class: 'ticker' });
      return { el: card('followers', 'Latest', list), list };
    },
    timers() {
      const list = h('div', { class: 'timers' });
      return { el: card('timers', 'Timers', list), list };
    },
  };
  for (const name of wanted) P[name] = build[name]();

  // ---- painters
  const paint = {
    nowplaying(np) {
      const p = P.nowplaying;
      if (!p) return;
      const on = !!(np && np.track);
      setEmpty('nowplaying', !on);
      p.el.classList.toggle('paused', on && !np.playing);
      if (!on) return;
      swap(p.track, np.track); swap(p.artist, np.artist || np.album || '');
      const src = np.art || '';
      if (p.art.dataset.src !== src) {
        p.art.dataset.src = src;
        const img = src ? h('img', { src, alt: '', referrerpolicy: 'no-referrer' }) : h('span', { class: 'art-none' });
        p.art.replaceChildren(img);
      }
    },
    goals(g) {
      const p = P.goals;
      if (!p) return;
      const items = (g && g.items) || [];
      setEmpty('goals', !items.length);
      const seen = new Set();
      for (const it of items) {
        seen.add(it.kind);
        let row = p.rows[it.kind];
        if (!row) {
          const label = h('span', { class: 'g-lbl' }), count = h('span', { class: 'g-num num' }), fill = h('i');
          row = p.rows[it.kind] = { el: h('div', { class: 'goal' }, h('div', { class: 'g-top' }, label, count), h('div', { class: 'bar' }, fill)), label, count, fill, cur: null };
          p.list.append(row.el);
        }
        row.label.textContent = it.label;
        const pct = Math.max(0, Math.min(1, it.target ? it.current / it.target : 0));
        row.fill.style.transform = `scaleX(${pct})`;
        const done = it.current >= it.target;
        if (row.cur != null && it.current > row.cur && !still()) {
          row.el.animate([{ boxShadow: '0 0 0 0 var(--glow)' }, { boxShadow: '0 0 0 10px transparent' }], { duration: 700, easing: 'ease-out' });
        }
        row.cur = it.current;
        row.el.classList.toggle('done', done);
        swap(row.count, `${it.current.toLocaleString()} / ${it.target.toLocaleString()}`);
      }
      for (const [k, row] of Object.entries(p.rows)) if (!seen.has(k)) { row.el.remove(); delete p.rows[k]; }
    },
    mood(m) {
      const p = P.mood;
      if (!p) return;
      const on = !!(m && m.live);
      setEmpty('mood', !on);
      if (!on) return;
      const pos = (Math.max(-1, Math.min(1, m.score || 0)) + 1) / 2;
      p.marker.style.left = `${(pos * 100).toFixed(1)}%`;
      p.el.dataset.tone = m.score > 0.25 ? 'hot' : m.score < -0.25 ? 'cold' : 'even';
      swap(p.word, m.label || (m.score > 0.25 ? 'hyped' : m.score < -0.25 ? 'quiet' : 'steady'));
      p.rate.textContent = `${m.rate || 0} messages a minute`;
    },
    countdown() { tickClock(); },
    followers(list) {
      const p = P.followers;
      if (!p) return;
      setEmpty('followers', !list.length);
      p.list.replaceChildren(...list.slice(0, 4).map(tickerRow));
    },
    timers() { tickClock(); },
  };

  const VERB = {
    follow: () => 'followed', sub: () => 'subscribed', resub: e => `resubscribed${e.amount ? ` · ${e.amount} months` : ''}`,
    gift: e => `gifted ${e.amount || 1} sub${e.amount === 1 ? '' : 's'}`, cheer: e => `cheered ${e.amount || ''} bits`,
    raid: e => `raided with ${e.amount || 0}`, hype_train: e => `hype train · level ${e.amount || 1}`,
  };
  const GLYPH = { follow: '♥', sub: '★', resub: '★', gift: '✦', cheer: '◆', raid: '➜', hype_train: '▲' };
  function tickerRow(e) {
    return h('li', { class: `tk tk-${e.kind}` }, h('span', { class: 'tk-g', 'aria-hidden': 'true' }, GLYPH[e.kind] || '•'),
      h('b', {}, e.user || (e.kind === 'hype_train' ? 'Chat' : 'Someone')), ' ', h('span', { class: 'tk-v' }, (VERB[e.kind] || (() => e.kind))(e)));
  }
  function addFollower(e) {
    D.followers = [e, ...(D.followers || [])].slice(0, 6);
    const p = P.followers;
    if (!p) return;
    setEmpty('followers', false);
    const row = tickerRow(e);
    row.classList.add('new');
    p.list.prepend(row);
    while (p.list.children.length > 4) {
      const last = p.list.lastElementChild;
      if (still()) last.remove();
      else { last.classList.add('gone'); setTimeout(() => last.remove(), 300); break; }
    }
    setTimeout(() => row.classList.remove('new'), 2400);
  }

  /* Countdown, timers and the song's progress move once a second from the times the server sent. */
  function tickClock() {
    const now = Date.now() / 1000;
    if (P.countdown) {
      const c = D.countdown || {};
      const left = c.at ? c.at - now : -1;
      setEmpty('countdown', !(left > -30));
      if (left > -30) {
        P.countdown.time.textContent = left > 0 ? clock(left) : '0:00';
        swap(P.countdown.kick, c.label || 'Countdown');
        P.countdown.label.textContent = left > 0 ? '' : 'any second now';
        P.countdown.el.classList.toggle('zero', left <= 0);
      }
    }
    if (P.timers) {
      const items = ((D.timers && D.timers.items) || []).filter(t => t.at - now > -5);
      setEmpty('timers', !items.length);
      const rows = items.map((t, i) => {
        const left = Math.max(0, t.at - now), frac = t.total_s ? Math.max(0, Math.min(1, left / t.total_s)) : 0;
        const ring = svg('svg', { viewBox: '0 0 40 40', class: 'ring', 'aria-hidden': 'true' },
          svg('circle', { cx: 20, cy: 20, r: 16, class: 'r-track', pathLength: 100 }),
          svg('circle', { cx: 20, cy: 20, r: 16, class: 'r-val', pathLength: 100, 'stroke-dasharray': `${(frac * 100).toFixed(2)} 100` }));
        return h('div', { class: `timer${left <= 0 ? ' done' : ''}` }, ring, h('span', { class: 'big num' }, left > 0 ? clock(left) : 'Done'),
          items.length > 1 ? h('span', { class: 'sub' }, `#${i + 1}`) : null);
      });
      P.timers.list.replaceChildren(...rows);
    }
    const np = D.nowplaying;
    if (P.nowplaying && np && np.track && np.duration_ms) {
      const ms = np.playing ? np.progress_ms + (now - np.at) * 1000 : np.progress_ms;
      P.nowplaying.fill.style.transform = `scaleX(${Math.max(0, Math.min(1, ms / np.duration_ms)).toFixed(4)})`;
    }
  }
  setInterval(tickClock, 1000);

  function apply(type, data) {
    if (type === 'snapshot') {
      for (const k of Object.keys(data || {})) { D[k] = data[k]; if (paint[k]) paint[k](data[k] || (k === 'followers' ? [] : null)); }
      return;
    }
    if (type === 'follower') { addFollower(data); return; }
    if (!(type in paint)) return;
    D[type] = data;
    if (type === 'nowplaying' && data) data.at = data.at || Date.now() / 1000;
    paint[type](data);
  }

  async function load() {
    try {
      const r = await fetch(`/overlay/state?key=${encodeURIComponent(KEY)}`, { cache: 'no-store' });
      if (r.ok) apply('snapshot', await r.json());
    } catch { /* the socket retries */ }
  }
  let retry = 0;
  function connect() {
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/overlay/ws?key=${encodeURIComponent(KEY)}`);
    ws.onopen = () => { retry = 0; root.removeAttribute('data-offline'); };
    ws.onmessage = m => { try { const ev = JSON.parse(m.data); apply(ev.type, ev.data); } catch (err) { console.error(err); } };
    ws.onclose = ev => {
      root.dataset.offline = '';
      if (ev.code === 4401) { setTimeout(() => location.reload(), 15000); return; }  // the link was replaced
      setTimeout(connect, Math.min(10000, 800 * 2 ** retry++));
    };
  }
  for (const name of wanted) setEmpty(name, true);
  load().then(() => { tickClock(); requestAnimationFrame(() => root.classList.add('ready')); });
  connect();
})();
