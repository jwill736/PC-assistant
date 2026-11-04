/* YouTube Live: connect (one more box on the Google sign-in), the live broadcast, its chat going to the co-pilot,
   today's quota, and where stream titles go. Viewers' messages are never shown here (they're in the co-pilot). */
(() => {
  const { h, fill, empty, meta, panels, body, toast } = Vesper.ui;
  async function act(action, payload, ok) {
    const r = await Vesper.act('stream_youtube', action, payload);
    if (r.ok === false) toast(r.error || 'That didn’t work.', { level: 'warn', source: 'stream_youtube' });
    else if (r.reply || ok) toast(r.reply || ok, { level: 'good', source: 'stream_youtube' });
    return r;
  }
  const WHERE = [['both', 'Twitch + YouTube'], ['twitch', 'Just Twitch']];

  Vesper.skill('stream_youtube', {
    panels: [{ view: 'stream', span: 4, title: 'YouTube Live', after: 'youtube' }],
    render(d) {
      for (const p of panels('stream_youtube')) {
        const b = body(p);
        if (!d) { fill(b, empty('Loading…')); continue; }
        if (!d.connected) {
          meta(p, 'not connected');
          fill(b, h('div', { class: 't2' }, 'Stream to YouTube too? Connect it and YouTube chat joins the co-pilot, and “set the title” can change both.'),
            d.google ? h('button', { class: 'btn small primary', onclick: () => act('connect', {}, 'Tick “Manage your YouTube account” on the Google page that just opened.') }, 'Connect YouTube Live')
              : h('div', { class: 't3 muted' }, 'First add the Google sign-in key: Setup → Google accounts.'));
          continue;
        }
        const q = d.quota || {}, pct = q.daily ? Math.min(1, q.spent / q.daily) : 0;
        meta(p, d.broadcast ? h('span', { class: 'yl-badge' }, 'LIVE') : d.account || 'connected');
        const ask = d.asking ? h('div', { class: 'yl-ask' }, h('div', { class: 't1' }, `New title “${d.asking.title}”: Twitch and YouTube, or just Twitch?`),
          h('div', { class: 'yl-row' }, WHERE.map(([w, label]) => h('button', { class: `btn small${w === 'both' ? ' primary' : ''}`, onclick: () => act('where', { where: w }) }, label))),
          h('div', { class: 't3 muted' }, 'Your click changes the title now, and is remembered for next time.')) : null;
        fill(b, ask,
          h('div', { class: 't2' }, d.broadcast ? `On YouTube: ${d.broadcast.title || 'untitled'}` : d.live ? 'Not live on YouTube right now.' : 'Not live.'),
          d.broadcast ? h('div', { class: 't3 muted num' }, `${d.read} chat messages to the co-pilot${d.interval_s ? ` · read every ${Math.round(d.interval_s)} s` : ''}`) : null,
          d.note ? h('div', { class: 't3 yl-warn' }, d.note) : null,
          h('div', { class: 'yl-quota', title: 'YouTube gives 10,000 units a day; a chat read is 5' },
            h('div', { class: 't3 muted num' }, `Quota today ${q.spent?.toLocaleString() ?? 0} / ${q.daily?.toLocaleString() ?? '—'} (resets midnight Pacific)`),
            h('div', { class: 'yl-bar', 'aria-hidden': 'true' }, h('i', { style: `transform: scaleX(${pct.toFixed(3)})` }))),
          h('div', { class: 'yl-row' }, h('span', { class: 't3 muted' }, 'Titles go to'),
            h('span', { class: 'seg', role: 'group', 'aria-label': 'Where stream titles go' }, WHERE.map(([w, label]) => h('button', {
              type: 'button', 'aria-pressed': String(d.title_on === w), onclick: () => act('where', { where: w, run: false }, `Titles go to ${label} from now on.`) }, label)))),
          !d.title_on ? h('div', { class: 't3 muted' }, 'Not chosen yet: the first “set the title” asks.') : null,
          d.error ? h('div', { class: 't3 yl-warn' }, d.error) : null);
      }
    },
  });
})();
