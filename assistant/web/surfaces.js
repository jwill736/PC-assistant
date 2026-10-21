'use strict';
/* Setup → Mini HUD, Phone, OBS overlay: the HUD's other surfaces (assistant/surfaces.py), plus their Ctrl+K entries.
   Uses app.js's helpers; every string goes in as text. */
(() => {
  const PHONE = document.documentElement.dataset.remote === 'phone';  // this page is open on J's phone
  const OV_PANELS = [['nowplaying', 'Now playing'], ['goals', 'Stream goals'], ['mood', 'Chat mood'], ['countdown', 'Countdown'],
    ['followers', 'Follower ticker'], ['timers', 'Timers']];
  const OV_THEMES = [['transparent', 'Glass', 'Dark glass plates on a see-through page (best over gameplay)'],
    ['bare', 'Bare', 'No plates: outlined text only'], ['dark', 'Solid', 'A solid dark page (for previewing)']];
  const GOAL_KINDS = [['subs', 'Sub goal'], ['follows', 'Follower goal'], ['bits', 'Bits goal']];
  const U = { remote: null, overlay: null, at: { remote: 0, overlay: 0 }, ov: null };
  try { U.ov = JSON.parse(localStorage.getItem('hud.overlayPick') || 'null'); } catch { /* none */ }
  U.ov = U.ov || { panels: ['nowplaying', 'goals', 'mood'], theme: 'transparent', align: 'left' };

  Vesper.panel({ name: 'mini_hud', view: 'setup', span: 4, title: 'Mini HUD · always in view', after: 'display' });
  Vesper.panel({ name: 'phone_hud', view: 'setup', span: 4, title: 'Phone · the HUD over Wi-Fi', after: 'mini_hud' });
  Vesper.panel({ name: 'obs_overlay', view: 'setup', span: 4, title: 'OBS overlay · on stream', after: 'phone_hud' });

  const busy = {};
  async function refresh(kind, force) {
    if (PHONE || busy[kind]) return;
    if (!force && U[kind] && Date.now() - U.at[kind] < 30000) return;
    busy[kind] = true;
    U.at[kind] = Date.now();
    try {
      U[kind] = await api(kind === 'remote' ? '/api/hud/remote' : '/api/hud/overlay');
    } catch (err) { U[kind] = { ok: false, error: err.message }; } finally { busy[kind] = false; }
    render[kind === 'remote' ? 'phone_hud' : 'obs_overlay']();
  }
  const kv = (k, v) => [h('span', { class: 'k' }, k), h('span', {}, v)];
  async function copy(text, done) {
    try { await navigator.clipboard.writeText(text); toast(done, { level: 'good' }); return; } catch { /* not allowed: fall back */ }
    const ta = h('textarea', { style: { position: 'fixed', opacity: 0 } }, text);
    document.body.append(ta); ta.select();
    const ok = document.execCommand && document.execCommand('copy');
    ta.remove();
    toast(ok ? done : 'Couldn’t copy: select the link and press Ctrl+C.', { level: ok ? 'good' : 'warn' });
  }

  // ---- mini HUD
  async function openMini() {
    if (PHONE) { location.href = '/mini'; return; }
    const r = await post('/api/hud/mini/open').catch(e => ({ ok: false, error: e.message }));
    if (!r.ok) toast(r.error || 'Couldn’t open it.', 'error');
    else toast(r.window ? 'Mini HUD opened in the top-right corner. Win+Ctrl+T (PowerToys) keeps it on top.' : 'Opened the mini HUD in your browser (Chrome gives it its own small window).');
  }
  render.mini_hud = function miniHud() {
    for (const p of panels('mini_hud')) {
      fill(body(p),
        h('div', { class: 'surf-lede' }, 'A small window for a corner of the screen: the orb, what was just said, your next event, the latest update, music, timers and the mic.'),
        h('div', { class: 'surf-mini-art', 'aria-hidden': 'true' }, h('span', { class: 'sm-orb' }), h('span', { class: 'sm-l' }), h('span', { class: 'sm-l s' }),
          h('span', { class: 'sm-c' }), h('span', { class: 'sm-c' })),
        h('div', { class: 'controls' },
          h('button', { class: 'btn primary', type: 'button', onclick: openMini }, 'Open mini HUD'),
          h('a', { class: 'btn', href: '/mini', target: '_blank', rel: 'noopener' }, 'Open in a tab')),
        h('div', { class: 't2' }, 'Keep it above a game or OBS with Microsoft PowerToys → Always On Top: click the mini window, then press ',
          h('kbd', {}, 'Win'), ' ', h('kbd', {}, 'Ctrl'), ' ', h('kbd', {}, 'T'), '. Also in the tray menu and Ctrl+K.'));
    }
  };

  // ---- phone
  async function setRemote(body, okText) {
    const r = await post('/api/hud/remote', body).catch(e => ({ ok: false, error: e.message }));
    if (r.ok === false) { toast(r.error || 'Couldn’t change it.', 'error'); return; }
    U.remote = r; U.at.remote = Date.now();
    render.phone_hud();
    if (okText) toast(r.error ? r.error : okText, { level: r.error ? 'warn' : 'good' });
  }
  function pinDigits(pin) {
    return h('span', { class: 'surf-pin num', 'aria-label': `PIN ${pin.split('').join(' ')}` },
      pin.slice(0, 3).split('').map(d => h('i', { 'aria-hidden': 'true' }, d)), h('span', { class: 'gap', 'aria-hidden': 'true' }),
      pin.slice(3).split('').map(d => h('i', { 'aria-hidden': 'true' }, d)));
  }
  render.phone_hud = function phoneHud() {
    for (const p of panels('phone_hud')) {
      const b = body(p);
      if (PHONE) {
        meta(p, 'this phone');
        fill(b, h('div', { class: 'surf-lede' }, 'You’re using the HUD on your phone. The PIN and phone access are changed on the PC (Setup → Phone).'),
          h('div', { class: 'controls' }, h('button', { class: 'btn', type: 'button', onclick: async () => {
            await fetch('/pin/logout', { method: 'POST' }).catch(() => {});
            location.replace('/pin');
          } }, 'Sign out this phone')));
        continue;
      }
      const r = U.remote;
      if (!r) { fill(b, empty('Loading…')); refresh('remote'); continue; }
      const on = !!r.enabled;
      meta(p, on ? (r.running ? 'on' : 'starting') : 'off');
      const toggle = h('span', { class: 'seg', role: 'group', 'aria-label': 'Use the HUD from my phone' }, [[false, 'Off'], [true, 'On']].map(([v, label]) =>
        h('button', { type: 'button', 'aria-pressed': String(on === v), onclick: () => on !== v && setRemote({ enabled: v },
          v ? 'Phone access is on. Scan the code with your phone.' : 'Phone access is off.') }, label)));
      const parts = [h('div', { class: 'set-row' }, h('span', { class: 'k' }, 'Use from my phone'), toggle)];
      if (!on) {
        parts.push(h('div', { class: 'surf-lede' }, 'Open this HUD in your phone’s browser over your home Wi-Fi. It asks for a 6-digit PIN once; the phone then stays signed in for 30 days. Nothing is reachable from outside your home network.'));
      } else {
        const url = (r.urls || [])[0];
        parts.push(r.error ? h('div', { class: 'empty' }, status('critical', r.error))
          : r.running ? h('div', { class: 't2' }, status('good', 'Listening on your home network'), ` · port ${r.port}`)
          : h('div', { class: 't2' }, status('idle', 'Starting…')));
        if (url) {
          let code = null;
          try { code = QR.svg(url, { ecl: 'M', label: `QR code for ${url}` }); } catch (err) { console.error(err); }
          parts.push(h('div', { class: 'surf-phone' },
            code ? h('div', { class: 'surf-qr' }, code) : null,
            h('div', { class: 'surf-phone-txt' },
              h('div', { class: 'sub-h' }, 'Scan, or type in the phone’s browser'),
              h('div', { class: 'surf-url mono' }, url),
              (r.urls || []).length > 1 ? h('div', { class: 't2' }, `Also: ${r.urls.slice(1).join(' · ')}`) : null,
              h('div', { class: 'sub-h' }, 'PIN'), pinDigits(r.pin || '------'),
              h('div', { class: 'controls' },
                h('button', { class: 'btn small', type: 'button', onclick: () => copy(url, 'Phone link copied.') }, 'Copy link'),
                h('button', { class: 'btn small', type: 'button', onclick: () => { if (confirm('Make a new PIN? Every phone has to type the new one.')) setRemote({ new_pin: true }, 'New PIN made. Phones sign in again.'); } }, 'New PIN'),
                h('button', { class: 'btn small ghost', type: 'button', onclick: () => { if (confirm('Sign out every phone?')) setRemote({ sign_out_all: true }, 'Every phone is signed out.'); } }, 'Sign out all phones')))));
        } else if (!r.error) {
          parts.push(empty('No home-network address found. Is the PC on Wi-Fi or a network cable?'));
        }
        parts.push(h('div', { class: 't2' }, 'The first time, Windows asks whether Python may use networks: tick ', h('b', {}, 'Private networks'),
          ', then ', h('b', {}, 'Allow access'), `. ${r.max_fails || 5} wrong PINs lock a phone out for ${r.lock_minutes || 15} minutes.`));
      }
      fill(b, ...parts);
    }
  };

  // ---- OBS overlay
  function ovLink(r, pick = U.ov, extra = '') {
    if (!r || !r.base) return '';
    return `${r.base}?panels=${pick.panels.join(',')}&theme=${pick.theme}${pick.align !== 'left' ? `&align=${pick.align}` : ''}${extra}&key=${r.key}`;
  }
  function pickSave() { try { localStorage.setItem('hud.overlayPick', JSON.stringify(U.ov)); } catch { /* storage unavailable */ } render.obs_overlay(); }
  async function setOverlay(path, body, okText) {
    const r = await post(path, body).catch(e => ({ ok: false, error: e.message }));
    if (r.ok === false) { toast(r.error || 'Couldn’t save.', 'error'); return; }
    U.overlay = r; U.at.overlay = Date.now();
    render.obs_overlay();
    if (okText) toast(okText, { level: 'good' });
  }
  function copyOverlay() {
    if (!U.overlay) { refresh('overlay', true).then(() => U.overlay?.key && copy(ovLink(U.overlay), 'OBS link copied: paste it as a Browser source’s URL.')); return; }
    copy(ovLink(U.overlay), 'OBS link copied: paste it as a Browser source’s URL.');
  }
  render.obs_overlay = function obsOverlay() {
    for (const p of panels('obs_overlay')) {
      const b = body(p);
      if (PHONE) { fill(b, empty('Made on the PC: Setup → OBS overlay.')); continue; }
      const r = U.overlay;
      if (!r) { fill(b, empty('Loading…')); refresh('overlay'); continue; }
      if (r.ok === false) { fill(b, empty(r.error || 'Not available.')); continue; }
      const conf = r.settings || {};
      meta(p, `${U.ov.panels.length} panel${U.ov.panels.length === 1 ? '' : 's'}`);
      const chips = h('div', { class: 'surf-chips', role: 'group', 'aria-label': 'Panels on the overlay' }, OV_PANELS.map(([id, label]) => h('button', {
        type: 'button', class: 'surf-chip', 'aria-pressed': String(U.ov.panels.includes(id)),
        onclick: () => { U.ov.panels = U.ov.panels.includes(id) ? U.ov.panels.filter(x => x !== id) : OV_PANELS.map(x => x[0]).filter(x => x === id || U.ov.panels.includes(x)); if (!U.ov.panels.length) U.ov.panels = [id]; pickSave(); } }, label)));
      const seg = (opts, cur, fn, label) => h('span', { class: 'seg', role: 'group', 'aria-label': label }, opts.map(([id, l, note]) => h('button', {
        type: 'button', 'aria-pressed': String(cur === id), title: note || '', onclick: () => fn(id) }, l)));
      const link = ovLink(r);
      const masked = link.replace(/key=.*$/, 'key=••••••');
      const goals = {};
      for (const g of conf.goals || []) goals[g.kind] = g;
      const inputs = GOAL_KINDS.map(([kind, label]) => {
        const inp = h('input', { type: 'number', min: '0', step: '1', inputmode: 'numeric', placeholder: 'off', 'aria-label': `${label} target`, value: goals[kind]?.target || '' });
        return { kind, label, inp, row: h('label', { class: 'surf-goal' }, h('span', {}, label), inp) };
      });
      const cd = conf.countdown || {};
      const cdLeft = cd.at ? Math.round(cd.at - Date.now() / 1000) : 0;
      fill(b,
        h('div', { class: 'surf-lede' }, 'A link for an OBS Browser source. It shows only what’s already on stream: music, goals, chat mood, a countdown, new followers and timers. Never your calendar, email or chat messages.'),
        h('div', { class: 'sub-h' }, 'Show'), chips,
        h('div', { class: 'kv surf-kv' },
          ...kv('Style', seg(OV_THEMES, U.ov.theme, id => { U.ov.theme = id; pickSave(); }, 'Overlay style')),
          ...kv('Side', seg([['left', 'Left'], ['center', 'Middle'], ['right', 'Right']], U.ov.align, id => { U.ov.align = id; pickSave(); }, 'Overlay side'))),
        h('div', { class: 'surf-link mono', title: 'The key in the link only opens this overlay' }, masked),
        h('div', { class: 'controls' },
          h('button', { class: 'btn primary', type: 'button', onclick: copyOverlay }, 'Copy OBS link'),
          h('a', { class: 'btn', href: ovLink(r, { ...U.ov, theme: 'dark' }, '&show=all'), target: '_blank', rel: 'noopener noreferrer' }, 'Preview'),
          h('button', { class: 'btn ghost', type: 'button', onclick: () => {
            if (confirm('Make a new overlay link? Every OBS source using the old one goes blank until you paste the new link.')) setOverlay('/api/hud/overlay/rotate', {}, 'New overlay link made. Paste it into OBS.');
          } }, 'New link')),
        h('div', { class: 'sub-h' }, 'Goals · this stream'),
        h('div', { class: 'surf-goals' }, inputs.map(x => x.row),
          h('button', { class: 'btn small', type: 'button', onclick: () => setOverlay('/api/hud/overlay', {
            goals: inputs.filter(x => Number(x.inp.value) > 0).map(x => ({ kind: x.kind, target: Math.round(Number(x.inp.value)) })) }, 'Goals saved: the overlay shows them now.') }, 'Save goals')),
        h('div', { class: 'sub-h' }, 'Countdown'),
        h('div', { class: 'controls', style: { marginTop: 0 } },
          [5, 10, 15].map(m => h('button', { class: 'btn small', type: 'button', onclick: () => setOverlay('/api/hud/overlay', { countdown_min: m, countdown_label: 'Starting soon' }, `“Starting soon” countdown: ${m} minutes.`) }, `${m} min`)),
          cdLeft > 0 ? h('button', { class: 'btn small ghost', type: 'button', onclick: () => setOverlay('/api/hud/overlay', { countdown_min: 0 }, 'Countdown cleared.') }, 'Clear') : null,
          h('span', { class: 'muted', style: { alignSelf: 'center' } }, cdLeft > 0 ? `running · ${Math.ceil(cdLeft / 60)} min left` : 'else the next stream on your calendar')),
        h('div', { class: 't2' }, 'In OBS: Sources → + → Browser → paste the link into URL, Width 600, Height 900. Leave “Shutdown source when not visible” off.'));
    }
  };

  // ---- refresh while Setup is open; Ctrl+K
  Vesper.on('hud:view', view => { if (view === 'setup') { refresh('remote'); refresh('overlay'); } });
  Vesper.on('hud_remote', data => { if (U.remote && data) { U.remote = { ...U.remote, ...data }; render.phone_hud(); } });
  Vesper.on('hud_overlay', data => { if (U.overlay && data) { U.overlay.settings = { ...(U.overlay.settings || {}), ...data }; render.obs_overlay(); } });
  window.addEventListener('DOMContentLoaded', () => { if (S.view === 'setup') { refresh('remote'); refresh('overlay'); } });
  if (!PHONE) {
    PAL_ACTIONS.push(['Open mini HUD', openMini, 'a small always-in-view window'],
      ['Copy OBS overlay link', copyOverlay, 'for a Browser source'],
      ['Use the HUD from my phone', () => { const p = $('.panel[data-panel="phone_hud"]'); if (p) jumpTo(p); }, 'phone, PIN, QR code']);
  }
})();
