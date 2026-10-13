/* Spotify: a Now Playing card (art, a progress bar that runs on its own between updates, controls, volume), the
   stream-safe check while live, and the one-time connect steps. */
(() => {
  const { h, fill, empty, meta, panels, body, toast } = Vesper.ui;
  const NS = 'http://www.w3.org/2000/svg';
  const ICON = {
    prev: 'M6 5v14M18 6l-9 6 9 6z', next: 'M18 5v14M6 6l9 6-9 6z',
    play: 'M8 5.5v13l11-6.5z', pause: 'M8 5h3v14H8zM13 5h3v14h-3z',
    shuffle: 'M3 7h4l10 10h4M3 17h4l3-3M14 10l3-3h4M18 4l3 3-3 3M18 14l3 3-3 3',
  };
  function icon(name) {
    const s = document.createElementNS(NS, 'svg'); s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('aria-hidden', 'true');
    const p = document.createElementNS(NS, 'path'); p.setAttribute('d', ICON[name]); s.append(p); return s;
  }
  const mmss = ms => { const s = Math.max(0, Math.round(ms / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
  const act = async (action, payload) => { const r = await Vesper.act('spotify', action, payload); if (!r.ok) toast(r.error); return r; };
  let clocks = [], lastId = null;

  function setup(d) {
    const input = h('input', { placeholder: 'Client ID (32 letters and digits)', 'aria-label': 'Spotify Client ID', autocomplete: 'off' });
    const copy = h('button', { class: 'btn small', type: 'button', onclick: () => navigator.clipboard?.writeText(d.redirect).then(() => toast('Copied.')) }, 'Copy');
    return h('div', { class: 'sp-setup' },
      h('div', { class: 't2' }, d.next_step || ''),
      h('div', { class: 'sp-redirect' }, h('span', { class: 't3 muted' }, 'Redirect URI'), h('code', {}, d.redirect), copy),
      d.configured ? null : h('form', { class: 'addrow', onsubmit: async e => {
        e.preventDefault(); const r = await act('client', { client_id: input.value.trim() }); if (r.ok) toast('Saved. Now click Connect.');
      } }, input, h('button', { class: 'btn small', type: 'submit' }, 'Save')),
      d.configured ? h('div', { class: 'controls' }, h('button', { class: 'btn primary', onclick: async () => {
        const r = await act('connect'); if (r.ok) toast('Approve on the Spotify page that just opened.');
      } }, 'Connect Spotify')) : null,
      d.error ? h('div', { class: 't3 sp-warn' }, d.error) : null);
  }

  function card(d, fresh) {
    const n = d.now;
    if (!n || !n.track) return h('div', {}, empty(d.error || 'Nothing playing. Start Spotify on any device, or say “play lofi beats”.'));
    const frac = () => n.duration_ms ? Math.min(1, (n.progress_ms + (n.playing ? Date.now() - n.at * 1000 : 0)) / n.duration_ms) : 0;
    const bar = h('i', { style: { transform: `scaleX(${frac()})` } });
    const elapsed = h('span', { class: 'num' }, mmss(frac() * n.duration_ms));
    if (n.playing && n.duration_ms) {
      const calm = matchMedia('(prefers-reduced-motion: reduce)').matches;  // then: a step a second, no glide
      if (!calm) requestAnimationFrame(() => requestAnimationFrame(() => {  // one long linear run to the song's end
        bar.style.transition = `transform ${Math.max(0, (1 - frac()) * n.duration_ms)}ms linear`; bar.style.transform = 'scaleX(1)';
      }));
      clocks.push(setInterval(() => {
        if (document.hidden) return;
        elapsed.textContent = mmss(frac() * n.duration_ms);
        if (calm) bar.style.transform = `scaleX(${frac()})`;
      }, 1000));
    }
    const vol = h('input', { type: 'range', min: 0, max: 100, value: n.volume ?? 50, 'aria-label': 'Spotify volume',
      onchange: e => act('volume', { level: Number(e.target.value) }) });
    const risky = d.live && n.playing && !d.safe;
    return h('div', { class: `sp-card ${n.playing ? 'is-playing' : 'is-paused'} ${fresh ? 'sp-new' : ''}` },
      h('div', { class: 'sp-art' }, n.art ? h('img', { src: n.art, alt: n.album || '', loading: 'lazy', referrerpolicy: 'no-referrer' }) : null,
        h('div', { class: 'sp-viz', 'aria-hidden': 'true' }, [0, 1, 2, 3, 4].map(i => h('span', { style: { animationDelay: `${-i * 0.17}s` } })))),
      h('div', { class: 'sp-info' },
        h('div', { class: 'sp-track t1', title: n.track }, n.track),
        h('div', { class: 't2 ink2 sp-artist' }, n.artist),
        h('div', { class: 'sp-bar', role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': Math.round(frac() * 100) }, bar),
        h('div', { class: 'sp-times t3 muted' }, elapsed, h('span', { class: 'num' }, mmss(n.duration_ms))),
        h('div', { class: 'sp-controls' },
          h('button', { class: 'btn small icon', 'aria-label': 'Previous', onclick: () => act('control', { action: 'previous' }) }, icon('prev')),
          h('button', { class: 'btn small icon sp-main', 'aria-label': n.playing ? 'Pause' : 'Play', onclick: () => act('control', { action: n.playing ? 'pause' : 'play' }) }, icon(n.playing ? 'pause' : 'play')),
          h('button', { class: 'btn small icon', 'aria-label': 'Next', onclick: () => act('control', { action: 'next' }) }, icon('next')),
          h('button', { class: `btn small icon ${n.shuffle ? 'on' : ''}`, 'aria-label': 'Shuffle', 'aria-pressed': String(!!n.shuffle), onclick: () => act('shuffle', { on: !n.shuffle }) }, icon('shuffle')),
          h('label', { class: 'range-row sp-vol' }, vol))),
      risky ? h('div', { class: 'sp-dmca t3' }, 'Live: this may not be stream-safe. ',
        n.context_type === 'playlist' ? h('button', { class: 'btn small', onclick: () => act('mark_safe', { on: true }).then(r => r.ok && toast('Marked this playlist stream-safe.')) }, 'Mark playlist stream-safe') : null) : null);
  }

  Vesper.skill('spotify', {
    panels: [{ view: 'life', span: 4, title: 'Now playing', after: 'journal' },
      { view: 'stream', span: 4, title: 'Music', after: 'twitch' }],
    render(d) {
      clocks.forEach(clearInterval); clocks = [];
      const fresh = d?.now?.id !== lastId; lastId = d?.now?.id;  // slide in for a new song, not every refresh
      for (const p of panels('spotify')) {
        const b = body(p);
        if (!d) { fill(b, empty('Loading…')); continue; }
        if (!d.connected) { meta(p, 'not connected'); fill(b, setup(d)); continue; }
        const n = d.now || {};
        meta(p, [n.device, d.live ? (d.safe ? 'stream-safe' : 'live') : ''].filter(Boolean).join(' · '));
        fill(b, card(d, fresh));
      }
    },
  });
})();
