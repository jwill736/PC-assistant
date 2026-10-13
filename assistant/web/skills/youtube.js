/* YouTube: subscribers / views / videos as counting tickers, a sparkline of subscribers, and the latest uploads. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast } = Vesper.ui;
  const NS = 'http://www.w3.org/2000/svg';
  const shown = {};  // what each ticker shows now, so the next update counts from there
  const calm = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
  const fmt = n => n == null ? '—' : Math.round(n).toLocaleString();

  function ticker(key, value) {
    const el = h('span', { class: 'num' }, fmt(shown[key] ?? value));
    const from = shown[key] ?? value;
    shown[key] = value;
    if (value == null || from == null || from === value || calm()) { el.textContent = fmt(value); return el; }
    const t0 = performance.now(), dur = 900;
    const step = now => {
      const k = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - k, 3);
      el.textContent = fmt(from + (value - from) * e);
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
    return el;
  }
  const since = day => (Date.now() - Date.parse(day)) / 864e5 >= 6.5 ? 'this week'
    : `since ${new Date(`${day}T12:00`).toLocaleDateString([], { month: 'short', day: 'numeric' })}`;
  const delta = n => n == null ? null : h('span', { class: `yt-delta ${n > 0 ? 'up' : n < 0 ? 'down' : ''}` }, `${n > 0 ? '+' : ''}${n.toLocaleString()}`);

  function sparkline(rows) {
    const pts = rows.filter(r => r.subs != null);
    if (pts.length < 2) return h('div', { class: 't3 muted yt-spark-empty' }, 'The growth line starts tomorrow: one point a day.');
    const W = 300, H = 56, lo = Math.min(...pts.map(r => r.subs)), hi = Math.max(...pts.map(r => r.subs));
    const X = i => 2 + i * ((W - 4) / (pts.length - 1)), Y = v => H - 4 - (H - 10) * ((v - lo) / Math.max(1, hi - lo));
    const line = pts.map((r, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(r.subs).toFixed(1)}`).join('');
    const s = document.createElementNS(NS, 'svg'); s.setAttribute('viewBox', `0 0 ${W} ${H}`); s.setAttribute('class', 'yt-spark');
    s.setAttribute('preserveAspectRatio', 'none');  // fills the panel's width
    s.setAttribute('role', 'img'); s.setAttribute('aria-label', `Subscribers from ${fmt(pts[0].subs)} to ${fmt(pts.at(-1).subs)}`);
    const el = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); s.append(e); return e; };
    el('path', { d: `${line}L${X(pts.length - 1)},${H}L${X(0)},${H}Z`, class: 'yt-area' });
    el('path', { d: line, class: 'yt-line', pathLength: 1 });
    return s;
  }

  function setup(d) {
    const key = h('input', { type: 'password', autocomplete: 'off', placeholder: d.has_key ? 'API key saved: paste to replace' : 'API key (AIza…)', 'aria-label': 'YouTube API key' });
    const ch = h('input', { placeholder: '@yourhandle or channel link', value: d.channel_setting || '', 'aria-label': 'YouTube channel' });
    return h('form', { class: 'addrow yt-setup', onsubmit: async e => {
      e.preventDefault(); const r = await Vesper.act('youtube', 'setup', { key: key.value, channel: ch.value });
      toast(r.ok ? `Connected: ${r.channel}.` : r.error);
    } }, key, ch, h('button', { class: 'btn small', type: 'submit' }, 'Save'));
  }

  Vesper.skill('youtube', {
    panels: [{ view: 'stream', span: 4, title: 'YouTube', after: 'home' }],
    render(d) {
      for (const p of panels('youtube')) {
        const b = body(p);
        if (!d) { fill(b, empty('Loading…')); continue; }
        if (b.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') continue;
        if (!d.ok && !d.channel) { meta(p, ''); fill(b, h('div', { class: 't2' }, d.next_step || d.error || ''), setup(d)); continue; }
        meta(p, `${d.handle || d.channel} · ${d.stale ? 'last good ' : ''}${ago(d.at)}`);
        const wk = d.week || {};
        fill(b,
          h('div', { class: 'yt-stats' },
            h('div', { class: 'stat' }, h('div', { class: 'label' }, 'Subscribers'), h('div', { class: 'value' }, d.subs == null ? 'hidden' : ticker('subs', d.subs)),
              h('div', { class: 'sub' }, delta(wk.subs), wk.subs != null ? ` ${since(wk.since)}` : '')),
            h('div', { class: 'stat' }, h('div', { class: 'label' }, 'Views'), h('div', { class: 'value' }, ticker('views', d.views)),
              h('div', { class: 'sub' }, delta(wk.views))),
            h('div', { class: 'stat' }, h('div', { class: 'label' }, 'Videos'), h('div', { class: 'value' }, ticker('videos', d.videos)))),
          sparkline(d.history || []),
          (d.latest || []).length ? h('div', { class: 'list yt-videos' }, d.latest.slice(0, 4).map(v => h('a', { class: 'item yt-video', href: v.url, target: '_blank', rel: 'noopener' },
            v.thumb ? h('img', { src: v.thumb, alt: '', loading: 'lazy', referrerpolicy: 'no-referrer' }) : null,
            h('div', { class: 'main' }, h('div', { class: 't1 yt-title' }, v.title),
              h('div', { class: 't3 muted num' }, `${fmt(v.views)} views · ${fmt(v.likes)} likes · ${fmt(v.comments)} comments · ${ago(v.published)}`))))) : null,
          d.error ? h('div', { class: 't3 yt-warn' }, d.error) : null,
          h('details', { class: 'yt-more' }, h('summary', { class: 't3 muted' }, 'Change key or channel'), setup(d)));
      }
    },
  });
})();
