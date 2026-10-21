/* Clip-worthy moments: the live score (chat · your voice · Twitch events), the moments found, and after the stream the
   highlights list with "Copy for promo video". "Clip that?" cards go to the shared stream card stack. */
(() => {
  const { h, fill, empty, meta, panels, body, toast } = Vesper.ui;
  const pct = v => `${Math.round(Math.max(0, Math.min(1, v || 0)) * 100)}%`;
  async function act(action, payload, ok) {
    const r = await Vesper.act('stream_moments', action, payload);
    if (r.ok === false) toast(r.error || 'That didn’t work.', { level: 'warn', source: 'stream' }); else if (ok) toast(ok, { level: 'good', source: 'stream' });
    return r;
  }
  const copy = (text, what) => navigator.clipboard.writeText(text).then(() => toast(`${what} copied.`, { level: 'good' }),
    () => toast('Couldn’t copy: select it and copy by hand.', { level: 'warn' }));

  function bar(label, v, cls) {
    return h('div', { class: `sm-sig ${cls}` }, h('span', { class: 't3 muted' }, label),
      h('span', { class: 'sm-track' }, h('i', { style: `transform: scaleX(${Math.max(0, Math.min(1, v || 0)).toFixed(3)})` })),
      h('span', { class: 'num t3' }, pct(v)));
  }
  function meter(d) {
    const m = d.meter || {}, t = d.threshold || 0.6;
    return h('div', { class: 'sm-meter' },
      h('div', { class: 'sm-score' }, h('span', { class: 'num sm-big' }, pct(m.score)), h('span', { class: 't3 muted' }, 'clip-worthy now'),
        h('span', { class: 'sm-track wide', role: 'meter', 'aria-label': 'Clip-worthy score', 'aria-valuenow': Math.round((m.score || 0) * 100), 'aria-valuemin': 0, 'aria-valuemax': 100 },
          h('i', { class: (m.score || 0) >= t ? 'hot' : '', style: `transform: scaleX(${Math.max(0, Math.min(1, m.score || 0)).toFixed(3)})` }),
          h('b', { class: 'sm-thr', style: `left: ${pct(t)}`, title: `Threshold ${pct(t)}` }))),
      bar('Chat', m.chat, 's1'), bar('Your voice', m.voice, 's2'), bar('Events', m.event, 's3'),
      d.voice_source ? null : h('div', { class: 't3 muted' }, 'Voice needs OBS connected (it reads your mic’s meter).'));
  }
  function moment(m) {
    const fresh = m.state === 'asked' && Date.now() / 1000 - m.ts < 55;
    return h('div', { class: 'item sm-item' },
      h('span', { class: 'num sm-at' }, m.at),
      h('div', { class: 'main' }, h('div', { class: 't1' }, m.reason.replace(/^Clip-worthy: /, '')),
        h('div', { class: 't3 muted' }, `score ${pct(m.score)}${m.marker ? ' · marker on the VOD' : ''}${m.state === 'skipped' ? ' · skipped' : ''}`)),
      m.clip_url ? h('a', { class: 'btn small', href: m.clip_url, target: '_blank', rel: 'noopener' }, 'Clip ↗')
        : fresh ? h('button', { class: 'btn small primary', onclick: () => act('clip', { id: m.id }, 'Clipped.') }, 'Clip') : null);
  }
  function lastList(last, promo) {
    if (!last) return empty('After the stream, the highlights list lands here, ready for a promo video.');
    const hl = last.highlights || [], s = last.stream || {};
    return h('div', { class: 'sm-last' },
      h('div', { class: 'sc-row' }, h('span', { class: 't2' }, `${s.date || 'Last stream'}${s.game ? ` · ${s.game}` : ''} · ${hl.length} highlight${hl.length === 1 ? '' : 's'}`),
        h('span', { class: 'spacer' }),
        h('button', { class: 'btn small primary', disabled: !hl.length, onclick: () => copy(promo || '', 'Highlights for the promo video') }, 'Copy for promo video'),
        h('button', { class: 'btn small', onclick: () => copy(JSON.stringify({ ...last, path: undefined }, null, 2), 'Highlights JSON') }, 'Copy JSON')),
      last.path ? h('div', { class: 't3 muted sm-path', title: last.path }, last.path) : null,
      hl.length ? h('ol', { class: 'list sm-hl' }, hl.slice(0, 12).map(x => h('li', { class: 'item' },
        h('span', { class: 'num sm-at' }, x.at),
        h('div', { class: 'main' }, h('div', { class: 't1' }, x.title), h('div', { class: 't3 muted' }, `${x.reason} · ${pct(x.score)}${x.marker ? ' · marker' : ''}`)),
        x.clip_url ? h('a', { class: 'btn small', href: x.clip_url, target: '_blank', rel: 'noopener' }, 'Clip ↗') : null)))
        : empty('Nothing stood out that stream.'));
  }
  Vesper.skill('stream_moments', {
    panels: [{ view: 'stream', span: 5, title: 'Clip-worthy moments', after: 'stream_copilot' }],
    render(d) {
      Vesper.streamCards?.set('stream_moments', d?.cards || []);
      for (const p of panels('stream_moments')) {
        const b = body(p);
        if (!d) { fill(b, empty('Loading…')); continue; }
        const ms = d.moments || [];
        meta(p, d.live ? `${ms.length} this stream${d.skipped ? ` · ${d.skipped} over the limit` : ''}` : 'offline');
        fill(b, d.error ? h('div', { class: 't3 muted' }, d.error) : null,
          d.live ? [meter(d), ms.length ? h('div', { class: 'list' }, ms.slice().reverse().map(moment))
            : empty('Watching chat, your mic and raids/sub bombs. Big moments get a marker and a “Clip that?” card.')]
            : lastList(d.last, d.promo));
      }
    },
  });
})();
