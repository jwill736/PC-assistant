/* Rewind: the last 24 hours as bands by category. Hover or drag along it (or use the arrow keys) to see what was in
   front at that moment; the cursor glides with transform only. Below: where you left off, and today's top things. */
(() => {
  const { h, fill, empty, meta, panels, body } = Vesper.ui;
  const NS = 'http://www.w3.org/2000/svg';
  const reduce = matchMedia('(prefers-reduced-motion: reduce)');
  const st = { data: null, t: null, x: 0, goal: 0, raf: 0, drag: false };
  const svg = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); return e; };
  const cats = () => [...Object.keys(Vesper.data.profiles || { work: 1, stream: 1 }), 'other'];
  const color = c => c === 'idle' ? 'var(--idle)' : `var(--series-${Math.min((cats().indexOf(c) < 0 ? cats().length : cats().indexOf(c)) + 1, 8)})`;
  const clock = t => new Date(t * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const dur = s => { const m = Math.round(s / 60); return m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m` : `${m}m`; };
  function at(blocks, t) {  // binary search: the block under time t
    let lo = 0, hi = blocks.length - 1;
    while (lo <= hi) { const mid = (lo + hi) >> 1, b = blocks[mid]; if (t < b.s) hi = mid - 1; else if (t > b.e) lo = mid + 1; else return b; }
    return null;
  }
  function readout(track) {
    const d = st.data, t = st.t;
    if (!d || t == null) return;
    const b = at(d.blocks, t), cap = track._read;
    fill(cap, h('span', { class: 'rw-time num' }, clock(t)),
      b ? [h('span', { class: 'rw-dot', style: { background: color(b.c) } }), h('b', {}, b.c === 'idle' ? 'Away' : b.a),
        b.t ? h('span', { class: 'rw-title' }, b.n && b.n !== b.a ? `${b.n} · ${b.t}` : b.t) : null] : h('span', { class: 'muted' }, 'Nothing tracked'));
    track.setAttribute('aria-valuenow', Math.round((t - d.from) / 60));
    track.setAttribute('aria-valuetext', `${clock(t)}: ${b ? (b.c === 'idle' ? 'away' : b.a) : 'nothing tracked'}`);
  }
  function glide(track) {
    cancelAnimationFrame(st.raf);
    const step = () => {
      st.x += (st.goal - st.x) * (reduce.matches ? 1 : 0.28);
      if (Math.abs(st.goal - st.x) < 0.3) st.x = st.goal;
      track._cursor.style.transform = `translateX(${st.x}px)`;
      const w = track.clientWidth, rw = track._read.offsetWidth;
      track._read.style.transform = `translateX(${Math.max(0, Math.min(w - rw, st.x - rw / 2))}px)`;
      if (st.x !== st.goal) st.raf = requestAnimationFrame(step);
    };
    st.raf = requestAnimationFrame(step);
  }
  function point(track, t) {
    const d = st.data; if (!d) return;
    st.t = Math.max(d.from, Math.min(d.now, t));
    st.goal = (st.t - d.from) / (d.now - d.from) * track.clientWidth;
    track.classList.add('rw-on');
    readout(track); glide(track);
  }
  function makeTrack() {
    const bands = svg('svg', { viewBox: '0 0 1000 40', preserveAspectRatio: 'none', class: 'rw-bands', 'aria-hidden': 'true' });
    const track = h('div', { class: 'rw-track', tabindex: 0, role: 'slider', 'aria-label': 'The last 24 hours: what was in front, moment by moment',
      'aria-valuemin': 0, 'aria-valuemax': 1440 });
    track._bands = bands; track._cursor = h('div', { class: 'rw-cursor' }); track._read = h('div', { class: 'rw-read t3' });
    track._ticks = h('div', { class: 'rw-ticks t3 muted', 'aria-hidden': 'true' });
    track.append(bands, track._cursor, track._read);
    const tAt = e => { const r = track.getBoundingClientRect(), d = st.data; return d.from + (e.clientX - r.left) / r.width * (d.now - d.from); };
    track.addEventListener('pointermove', e => { if (st.data) point(track, tAt(e)); });
    track.addEventListener('pointerdown', e => { st.drag = true; track.setPointerCapture(e.pointerId); point(track, tAt(e)); });
    track.addEventListener('pointerup', () => { st.drag = false; });
    track.addEventListener('pointerleave', () => { if (!st.drag && document.activeElement !== track) track.classList.remove('rw-on'); });
    track.addEventListener('blur', () => track.classList.remove('rw-on'));
    track.addEventListener('keydown', e => {
      const d = st.data; if (!d) return;
      const step = (e.shiftKey ? 30 : 5) * 60, t = st.t ?? d.now;
      const next = { ArrowLeft: t - step, ArrowRight: t + step, Home: d.from, End: d.now }[e.key];
      if (next != null) { e.preventDefault(); point(track, next); }
    });
    return track;
  }
  function drawBands(track, d) {
    const span = d.now - d.from, X = t => ((t - d.from) / span * 1000);
    const g = d.blocks.map(b => svg('rect', { x: X(b.s).toFixed(2), y: b.c === 'idle' ? 16 : 4, width: Math.max(0.6, X(b.e) - X(b.s)).toFixed(2),
      height: b.c === 'idle' ? 8 : 32, fill: color(b.c), class: 'rw-band' }));
    const marks = [];
    const first = Math.ceil(d.from / 10800) * 10800;
    for (let t = first; t <= d.now; t += 10800) marks.push(h('span', { style: { left: `${(t - d.from) / span * 100}%` } }, new Date(t * 1000).toLocaleTimeString([], { hour: 'numeric' })));
    const now = svg('rect', { x: 998.5, y: 0, width: 1.5, height: 40, class: 'rw-now' });
    track._bands.classList.toggle('rw-first', !track._drawn);  // the bands grow in once, not on every refresh
    track._drawn = true;
    track._bands.replaceChildren(...g, now);
    fill(track._ticks, marks);
  }
  function paint(d) {
    st.data = d && d.blocks ? d : null;
    for (const p of panels('rewind')) {
      const b = body(p);
      if (!d) { fill(b, empty('Loading…')); continue; }
      meta(p, d.today?.active_s ? `${dur(d.today.active_s)} active today${d.live ? ' · live: no titles' : ''}` : '');
      if (!b._track) { b._track = makeTrack(); b._rest = h('div', { class: 'rw-rest' }); b._legend = h('div', { class: 'rw-legend t3 muted' }); }
      if (!d.blocks.length) { fill(b, empty('Nothing tracked in the last 24 hours yet. Activity tracking fills this in as you use the PC.')); b._track = null; continue; }
      drawBands(b._track, d);
      fill(b._legend, [...new Set(d.blocks.map(x => x.c))].map(c => h('span', {}, h('i', { style: { background: color(c) } }), c === 'idle' ? 'away' : c)));
      const w = d.where, things = d.today?.things || [], top = Math.max(1, ...things.map(x => x.seconds));
      fill(b._rest,
        w ? h('div', { class: 'rw-where' }, h('div', { class: 't3 muted' }, `Where you left off · ${clock(w.stopped_at)}`),
          h('div', { class: 't1' }, (w.things || []).map(x => x.kind === 'app' ? x.name : `${x.name} (${x.app})`).join(' · ') || w.apps.map(a => a.app).join(' · '))) : null,
        things.length ? h('div', { class: 'rw-top' }, things.map(x => h('div', { class: 'rw-row' },
          h('span', { class: 'rw-name', title: `${x.name} · ${x.app}` }, x.name), h('span', { class: 'rw-bar' }, h('i', { style: { transform: `scaleX(${x.seconds / top})` } })),
          h('span', { class: 'num t3 muted' }, dur(x.seconds))))) : null);
      if (!b.contains(b._track)) fill(b, b._track, b._track._ticks, b._legend, b._rest);
      if (st.t != null && b._track.classList.contains('rw-on')) point(b._track, st.t);
    }
  }
  addEventListener('resize', () => { for (const p of panels('rewind')) { const t = body(p)._track; if (t && st.t != null && t.classList.contains('rw-on')) point(t, st.t); } });
  Vesper.skill('rewind', { panels: [{ view: 'pc', span: 12, title: 'Rewind · last 24 hours', after: 'screen' }], render: paint });
})();
