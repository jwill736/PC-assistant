/* Week in review (Life tab): hours by category, focus, habits, tasks, streams, mood, money, wins and one suggestion for
   next week. "Open card" shows the shareable 1080×1350 card in its own window (screenshot it, or Print → PDF).
   On air the server leaves out moods and money, and this panel says so. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast } = Vesper.ui;
  const DAYS = ['M', 'T', 'W', 'T', 'F', 'S', 'S'];
  const num = x => (Math.round((x || 0) * 10) / 10).toString();

  async function openCard(week = 'this') {
    const w = window.open('', 'vesper-review', 'popup,width=1080,height=1350');
    if (!w) { toast('Allow pop-ups for Vesper to open the card.', { level: 'warn', source: 'review' }); return; }
    try {
      const token = document.querySelector('meta[name="token"]').content;
      const res = await fetch(`/api/skill/review/card?week=${week}`, { headers: { 'X-Assistant-Token': token } });
      if (!res.ok) throw new Error(`${res.status}`);
      const html = await res.text();
      w.location.href = URL.createObjectURL(new Blob([html], { type: 'text/html' }));
    } catch (err) {
      w.close();
      toast(`Couldn't open the card (${err.message}).`, { level: 'error', source: 'review' });
    }
  }

  async function build() {
    const r = await Vesper.act('review', 'build', { week: 'this' });
    toast(r.ok ? 'Week in review updated.' : r.error, { level: r.ok ? 'good' : 'error', source: 'review' });
  }

  const tile = (label, value, sub, i) => h('div', { class: 'stat rv-tile', style: `--i:${i}` },
    h('div', { class: 'label' }, label), h('div', { class: 'value num' }, value), h('div', { class: 'sub' }, sub));

  function catBar(hrs) {
    const by = hrs.by || [];
    if (!by.length) return empty('No activity tracked this week.');
    return [
      h('div', { class: 'rv-bar', role: 'img', 'aria-label': by.map(x => `${x.label} ${x.hours} hours`).join(', ') },
        by.map((x, i) => h('i', { style: `flex:${x.hours};background:var(--series-${(i % 8) + 1});--i:${i}` }))),
      h('div', { class: 'rv-legend' }, by.map((x, i) => h('span', {},
        h('b', { style: `background:var(--series-${(i % 8) + 1})` }), x.label, ' ', h('em', { class: 'num' }, `${num(x.hours)} h`))))];
  }

  function weekCols(hrs) {
    const by = hrs.by || [], days = hrs.days || [];
    if (!by.length) return null;
    const tot = d => by.reduce((s, x) => s + (d.by[x.key] || 0), 0);
    const peak = Math.max(1, ...days.map(tot));
    return h('div', { class: 'rv-cols', 'aria-hidden': 'true' }, days.map((d, j) => h('div', { class: 'rv-col' },
      h('div', { class: 'rv-stack', style: `--i:${j}` }, by.map((x, i) => d.by[x.key] ?
        h('i', { style: `height:${(100 * d.by[x.key] / peak).toFixed(1)}%;background:var(--series-${(i % 8) + 1})` }) : null)),
      h('span', {}, DAYS[j]))));
  }

  function mood(m) {
    if (m.private) return h('div', { class: 't2' }, `${m.entries || 0} journal entr${m.entries === 1 ? 'y' : 'ies'} · moods hidden while live`);
    if (!m.entries) return h('div', { class: 't2' }, 'No journal entries this week.');
    const word = { up: 'trending up', down: 'dipped', steady: 'steady' }[m.trend] || '';
    return [h('div', { class: 'rv-mood', role: 'img', 'aria-label': `Mood ${word || 'by day'}` }, (m.days || []).map((d, j) =>
      h('div', { class: 'rv-md' },
        h('i', { class: d.mood == null ? 'none' : d.mood >= 0 ? 'pos' : 'neg',
          style: `--m:${d.mood == null ? 0 : Math.abs(d.mood) / 2}` }),
        h('span', {}, DAYS[j])))),
    h('div', { class: 't2' }, `${m.entries} entr${m.entries === 1 ? 'y' : 'ies'}${word ? ` · ${word}` : ''}`)];
  }

  function money(mo, priv) {
    if (priv) return h('div', { class: 't2' }, 'Money is hidden while live.');
    if (!mo) return h('div', { class: 't2' }, 'Connect Gmail to see subscriptions and bills.');
    const sign = { USD: '$', EUR: '€', GBP: '£' }[mo.currency] || '';
    const f = a => `${sign}${Number(a || 0).toFixed(2)}`;
    return h('div', { class: 't2' }, `${f(mo.subs_monthly)}/mo across ${mo.subs} subscription${mo.subs === 1 ? '' : 's'}`,
      mo.upcoming?.length ? ` · ${mo.upcoming.length} due this week (${f(mo.upcoming_total)})` : ' · nothing due this week',
      mo.flagged?.length ? h('span', { class: 'rv-save' }, ` · could save ${f(mo.savings)}/mo`) : null);
  }

  function paint(d) {
    for (const p of panels('review')) {
      const b = body(p);
      p.classList.toggle('rv-fresh', !!d?.fresh);
      if (!d) { fill(b, empty('Loading…')); continue; }
      const r = d.review;
      if (!r) {
        meta(p, '');
        fill(b, empty('Your week builds itself on Sunday evening. Say “life review” any time.'));
        continue;
      }
      meta(p, `${r.label}${r.complete ? '' : ' · so far'} · ${ago(r.at)}`);
      const hrs = r.hours || {}, f = r.focus || {}, hb = r.habits || {}, t = r.tasks || {}, s = r.streams || {};
      fill(b, h('div', { class: 'rv-grid' },
        h('section', { class: 'rv-main' },
          h('div', { class: 'rv-tiles' },
            tile('Hours at the PC', num(hrs.total), 'tracked', 0),
            tile('Focus sessions', String(f.sessions || f.deep || 0), `${num((f.minutes || f.deep_minutes || 0) / 60)} h deep`, 1),
            tile('Habits', hb.pct == null ? '—' : `${hb.pct}%`, `${hb.count || 0} tracked`, 2),
            tile('Tasks done', String(t.done || 0), `${t.open || 0} still open`, 3)),
          h('div', { class: 'sub-h' }, 'Where the time went'), catBar(hrs), weekCols(hrs)),
        h('section', { class: 'rv-side' },
          h('div', { class: 'sub-h' }, 'Wins'),
          (r.wins || []).length ? h('ul', { class: 'rv-wins' }, r.wins.map((w, i) => h('li', { style: `--i:${i}` }, w)))
            : empty('A quiet week. That counts too.'),
          h('div', { class: 'rv-rows' },
            h('div', {}, h('div', { class: 'sub-h' }, 'Streams'), h('div', { class: 't2' }, s.count
              ? `${s.count}× · ${num(s.hours)} h${s.peak ? ` · peak ${s.peak}` : ''}${(s.highlights || []).length ? ` · ${s.highlights[0]}` : ''}`
              : s.hours ? `${num(s.hours)} h on stream` : 'No streams this week.')),
            h('div', {}, h('div', { class: 'sub-h' }, 'Mood'), mood(r.mood || {})),
            h('div', {}, h('div', { class: 'sub-h' }, 'Money'), money(r.money, r.private))),
          h('div', { class: 'rv-next' }, h('div', { class: 'rv-next-l' }, 'Next week'),
            h('p', {}, r.suggestion || ''), h('div', { class: 'rv-by' }, r.by === 'model' ? 'from your model' : 'Vesper’s pick')))));
    }
  }

  Vesper.skill('review', {
    panels: [{ view: 'life', span: 12, title: 'Week in review', after: 'bills', buttons: [
      { label: 'Open card', onclick: () => openCard('this') }, { label: 'Refresh', onclick: build }] }],
    render: paint,
  });
})();
