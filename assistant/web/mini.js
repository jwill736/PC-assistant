'use strict';
/* Mini HUD (/mini, ~360×520): a corner window that keeps the essentials in view while a game or OBS has the
   screen. Same token, socket and events as the big HUD; every string goes in as text. */
(() => {
  const TOKEN = document.querySelector('meta[name="token"]').content;
  const HUD_VERSION = (document.querySelector('meta[name="hud-version"]')?.content || '').replace(/^\{\{.*/, '');
  const $ = s => document.querySelector(s);
  const D = { calendar: null, spotify: null, automations: null, update: null, name: 'Vesper' };
  const orb = { voice: 'off', thinking: false, speaking: false };

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'style') for (const [p, x] of Object.entries(v)) { if (p.startsWith('--')) el.style.setProperty(p, x); else el.style[p] = x; }
      else el.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
    return el;
  }
  async function api(path, opts = {}) {
    const res = await fetch(path, { ...opts, headers: { 'Content-Type': 'application/json', 'X-Assistant-Token': TOKEN },
      body: opts.body ? JSON.stringify(opts.body) : undefined });
    if (!res.ok) throw new Error(`${res.status} ${path}`);
    return res.json();
  }
  const post = (path, body) => api(path, { method: 'POST', body: body || {} });
  const clock = s => {
    s = Math.max(0, Math.round(s));
    const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
    return hh ? `${hh}:${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}` : `${mm}:${String(ss).padStart(2, '0')}`;
  };
  const ago = ts => {
    const d = Date.now() / 1000 - ts;
    return d < 60 ? 'just now' : d < 3600 ? `${Math.round(d / 60)}m ago` : d < 86400 ? `${Math.round(d / 3600)}h ago` : `${Math.round(d / 86400)}d ago`;
  };
  const say = text => { const r = $('#mini-live'); r.textContent = ''; setTimeout(() => { r.textContent = text; }, 30); };

  // ---- orb + state
  FX.orb.attach($('#mini-orb'));
  const ORB = { armed: 'listen', hearing: 'listen', transcribing: 'listen', listening: 'standby', muted: 'muted', off: 'off',
    disabled: 'off', unavailable: 'off', loading: 'loading', error: 'error' };
  const LABEL = { listen: 'Listening', think: 'Thinking', speak: 'Speaking', standby: 'Standby', muted: 'Mic muted', off: 'Voice off',
    loading: 'Loading', error: 'Voice error' };
  function paintOrb() {
    const st = orb.speaking ? 'speak' : orb.thinking ? 'think' : ORB[orb.voice] || 'off';
    FX.orb.setState(st);
    const wrap = $('#mini-orb-wrap'), lbl = $('#mini-state .lbl');
    if (wrap.dataset.state !== st) { wrap.dataset.state = st; $('#mini-state').dataset.state = st; FX.reveal(lbl, LABEL[st], { mode: 'decode' }); }
    wrap.classList.toggle('idle', !['listen', 'think', 'speak', 'loading'].includes(st));
  }

  // ---- what was said
  function line(which, text, { ignored = false } = {}) {
    const el = $(which === 'you' ? '#mini-you' : '#mini-ves');
    el.parentElement.toggleAttribute('data-empty', !text);
    el.classList.toggle('ignored', ignored);
    el.title = text || '';
    FX.reveal(el, text || '', { mode: which === 'you' && (text || '').length <= 48 && !ignored ? 'decode' : 'type' });
    if (text && !ignored) say(`${which === 'you' ? 'You' : D.name}: ${text}`);
  }

  // ---- next event
  function nextEvent() {
    const now = new Date();
    return ((D.calendar && D.calendar.events) || []).filter(e => !e.all_day && new Date(e.start) > now)
      .sort((a, b) => new Date(a.start) - new Date(b.start))[0];
  }
  function paintNext() {
    const e = nextEvent();
    const card = $('#mini-next');
    if (!e) {
      $('#mini-cd').textContent = '—'; $('#mini-next-title').textContent = D.calendar ? 'Nothing else today' : 'Loading calendars…';
      $('#mini-next-when').textContent = ''; card.dataset.soon = ''; return;
    }
    const start = new Date(e.start), left = (start - Date.now()) / 1000;
    $('#mini-cd').textContent = left < 3600 ? clock(left) : left < 86400 ? `${Math.floor(left / 3600)}h ${String(Math.floor(left % 3600 / 60)).padStart(2, '0')}m` : start.toLocaleDateString([], { weekday: 'short' });
    if ($('#mini-next-title').textContent !== e.title) $('#mini-next-title').textContent = e.title;
    $('#mini-next-when').textContent = [start.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }), e.calendar].filter(Boolean).join(' · ');
    card.dataset.soon = left < 300 ? 'now' : left < 900 ? 'soon' : '';
  }

  // ---- latest update
  function update(source, text, ts, level) {
    if (!text) return;
    if (D.update && D.update.ts > ts) return;
    D.update = { source, text, ts, level };
    paintUpdate(true);
  }
  function paintUpdate(fresh) {
    const u = D.update;
    if (!u) return;
    $('#mini-update-txt').textContent = u.text;
    $('#mini-update-src').textContent = (u.source || 'vesper').replace(/_/g, ' ');
    $('#mini-update-ago').textContent = ago(u.ts);
    $('#mini-update').dataset.level = u.level || '';
    if (fresh && FX.motion.full) $('#mini-update').animate([{ boxShadow: '0 0 0 1px var(--ember)' }, { boxShadow: '0 0 0 1px transparent' }], { duration: 900, easing: 'ease-out' });
  }

  // ---- music
  function paintMusic() {
    const now = (D.spotify && D.spotify.now) || {};
    const card = $('#mini-music');
    card.hidden = !now.track;
    if (!now.track) return;
    if ($('#mini-track').textContent !== now.track) {
      $('#mini-track').textContent = now.track;
      const art = $('#mini-art');
      art.replaceChildren(now.art ? h('img', { src: now.art, alt: '', referrerpolicy: 'no-referrer' }) : h('span', { class: 'mini-art-none' }));
    }
    $('#mini-artist').textContent = now.artist || now.album || '';
    card.classList.toggle('paused', !now.playing);
    tickMusic();
  }
  function tickMusic() {
    const now = (D.spotify && D.spotify.now) || {};
    if (!now.track || !now.duration_ms) return;
    const ms = now.playing ? now.progress_ms + (Date.now() / 1000 - (now.at || Date.now() / 1000)) * 1000 : now.progress_ms;
    $('#mini-prog').style.transform = `scaleX(${Math.max(0, Math.min(1, ms / now.duration_ms)).toFixed(4)})`;
  }

  // ---- timers
  function paintTimers() {
    const items = ((D.automations && D.automations.items) || []).filter(i => i.kind === 'timer' && i.at && i.at > Date.now() / 1000 - 5);
    $('#mini-timers').hidden = !items.length;
    $('#mini-timer-list').replaceChildren(...items.slice(0, 3).map(t => {
      const left = Math.max(0, t.at - Date.now() / 1000), frac = t.total_s ? Math.max(0, Math.min(1, left / t.total_s)) : 0;
      return h('div', { class: `mini-timer${left <= 0 ? ' done' : ''}`, title: t.label || 'Timer' },
        h('span', { class: 'mini-ring', style: { '--p': `${(frac * 360).toFixed(1)}deg` }, 'aria-hidden': 'true' }),
        h('span', { class: 'num' }, left > 0 ? clock(left) : 'Done'), h('span', { class: 'mini-timer-lbl mini-private' }, t.label || ''));
    }));
  }

  setInterval(() => { if (document.hidden) return; paintNext(); paintTimers(); tickMusic(); if (D.update) $('#mini-update-ago').textContent = ago(D.update.ts); }, 1000);

  // ---- on air: what was said, the next event and the latest update blur while the stream is live (hover shows them)
  const air = { obs: false, copilot: false };
  function paintAir() {
    const live = air.obs || air.copilot;
    document.body.classList.toggle('live-private', live);
    $('#mini-onair').hidden = !live;
  }

  // ---- recording (skills/recorder.py): a REC badge with the time, amber while paused
  function paintRec() {
    const r = D.recorder && D.recorder.rec, el = $('#mini-rec');
    el.hidden = !r;
    if (!r) return;
    el.classList.toggle('off', !!r.paused);
    const t = r.paused ? r.elapsed : r.elapsed + Math.max(0, Date.now() / 1000 - (D.recorder.at || Date.now() / 1000));
    el.textContent = `${r.paused ? 'PAUSED' : 'REC'} ${clock(t)}`;
  }
  setInterval(() => { if (!document.hidden && D.recorder && D.recorder.rec) paintRec(); }, 1000);

  // ---- buttons
  $('#mini-mic').addEventListener('click', () => post('/api/voice/arm').catch(() => {}));
  $('#mini-mute').addEventListener('click', e => {
    const muted = e.currentTarget.getAttribute('aria-pressed') !== 'true';
    e.currentTarget.setAttribute('aria-pressed', String(muted));
    post('/api/voice/mute', { muted }).catch(() => {});
  });

  // ---- live updates
  function onEvent({ type, data, ts }) {
    switch (type) {
      case 'level': FX.orb.setLevel(data?.rms); break;
      case 'voice_state': orb.voice = data.state; $('#mini-mute').setAttribute('aria-pressed', String(data.state === 'muted')); paintOrb(); break;
      case 'thinking': orb.thinking = !!data.active; paintOrb(); break;
      case 'speaking': orb.speaking = !!data.active; paintOrb(); break;
      case 'wake': orb.voice = 'armed'; paintOrb(); FX.flare($('#mini-orb')); break;
      case 'user_said': line('you', data.text); break;
      case 'assistant_said': if (data.text) line('ves', data.text); break;
      case 'heard': if (!data.wake && !data.armed) line('you', `(ignored) “${data.text}”`, { ignored: true }); break;
      case 'calendar': D.calendar = data; paintNext(); break;
      case 'spotify': D.spotify = data; paintMusic(); break;
      case 'obs': air.obs = !!(data && data.streaming && data.streaming.active); paintAir(); break;
      case 'stream_copilot': air.copilot = !!(data && data.live); paintAir(); break;
      case 'automations': D.automations = data; paintTimers(); break;
      case 'recorder': D.recorder = data; paintRec(); break;
      case 'notice': update(data.source, data.text, data.ts || ts, data.level); break;
      case 'announce': update('vesper', data.text, ts, 'vesper'); break;
      case 'stream_alert': update('stream', data.text, ts, data.level); break;
      default: break;
    }
  }

  async function load() {
    try {
      const st = await api('/api/state');
      if (st.hud_version && HUD_VERSION && st.hud_version !== HUD_VERSION) return location.reload();
      D.name = st.assistant?.name || D.name;
      orb.voice = st.voice?.state || 'off';
      $('#mini-mute').setAttribute('aria-pressed', String(!!st.voice?.muted));
      D.calendar = st.calendar || D.calendar;
      const sk = st.skills?.data || {};
      D.spotify = sk.spotify || null; D.automations = sk.automations || null; D.recorder = sk.recorder || null;
      const feed = (sk.nudges && sk.nudges.feed) || [];
      if (feed[0]) update(feed[0].source, feed[0].text, feed[0].delivered || 0, feed[0].priority);
      const log = st.log || [];
      const you = [...log].reverse().find(m => m.role === 'user'), ves = [...log].reverse().find(m => m.role === 'assistant');
      if (you) { $('#mini-you').textContent = you.text; $('#mini-you').parentElement.removeAttribute('data-empty'); }
      if (ves) { $('#mini-ves').textContent = ves.text; $('#mini-ves').parentElement.removeAttribute('data-empty'); }
      air.obs = !!st.obs?.streaming?.active; air.copilot = !!sk.stream_copilot?.live;
      paintOrb(); paintNext(); paintMusic(); paintTimers(); paintUpdate(false); paintAir(); paintRec();
    } catch (err) { console.error(err); }
  }
  let retry = 0;
  function connect() {
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`);
    ws.onopen = () => { retry = 0; document.body.classList.remove('stale'); load(); };
    ws.onmessage = m => { try { onEvent(JSON.parse(m.data)); } catch (err) { console.error(err); } };
    ws.onclose = () => { document.body.classList.add('stale'); setTimeout(connect, Math.min(4000, 500 * 2 ** retry++)); };
  }
  paintOrb();
  connect();
})();
