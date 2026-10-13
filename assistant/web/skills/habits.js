/* Habits: one row each, tap to log; a 7-day dot strip and a streak flame. */
(() => {
  const { h, fill, empty, meta, panels, body, toast } = Vesper.ui;
  const NS = 'http://www.w3.org/2000/svg';
  const DOW = ['S', 'M', 'T', 'W', 'T', 'F', 'S'];
  let popped = null; // the habit just logged: its row pops once on the next paint

  function flame(n) {
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 16 20'); svg.setAttribute('class', 'hb-flame'); svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS(NS, 'path');
    path.setAttribute('d', 'M8 1c1 4 6 6 6 11a6 6 0 0 1-12 0c0-3 2-4 3-6 0 2 1 3 2 3 0-3 0-5 1-8z');
    svg.append(path);
    return h('span', { class: `hb-streak ${n ? '' : 'zero'}`, title: `${n} in a row` }, svg, h('span', { class: 'num' }, String(n)));
  }

  function dots(v, days) {
    return h('div', { class: 'hb-dots', 'aria-label': 'Last 7 days' }, v.days.map((d, i) => {
      const dt = new Date(`${d.day}T12:00:00`);
      return h('span', { class: `hb-dot${d.frac >= 1 ? ' full' : ''}${i === days.length - 1 ? ' today' : ''}`, title: `${dt.toLocaleDateString([], { weekday: 'short' })}: ${d.n}`,
        style: `--f:${Math.max(0, Math.min(1, d.frac))}` }, h('i', {}), h('b', {}, DOW[dt.getDay()]));
    }));
  }

  async function log(v) {
    popped = v.id;
    const r = await Vesper.act('habits', 'log', { id: v.id });
    if (!r.ok) { popped = null; toast(r.error); }
  }

  function render(d) {
    for (const p of panels('habits')) {
      const b = body(p);
      const hs = d?.habits || [];
      const daily = hs.filter(v => v.period === 'day');
      meta(p, daily.length ? `${daily.filter(v => v.done).length}/${daily.length} today` : '');
      if (!b._form) {
        const input = h('input', { placeholder: 'track a habit: read daily · drink water 8 times a day · gym 3 times a week', 'aria-label': 'New habit' });
        b._form = h('form', { class: 'addrow', onsubmit: async e => {
          e.preventDefault(); if (!input.value.trim()) return;
          const r = await Vesper.act('habits', 'add', { text: input.value }); toast(r.ok ? `Tracking ${r.habit.name}.` : r.error); if (r.ok) input.value = '';
        } }, input, h('button', { class: 'btn small', type: 'submit' }, 'Add'));
        b._list = h('div', { class: 'hb-list' });
        fill(b, b._list, b._form);
      }
      fill(b._list, hs.length ? hs.map(v => {
        const row = h('div', { class: `hb-row${v.done ? ' done' : ''}${popped === v.id ? ' pop' : ''}` });
        const goal = v.target === 1 && v.period === 'day' ? (v.done ? 'done today' : 'not yet today') : `${v.count}/${v.target} ${v.period === 'day' ? 'today' : 'this week'}`;
        fill(row,
          h('button', { class: 'hb-hit', 'aria-label': `Log ${v.name}`, onclick: () => log(v) },
            h('span', { class: 'hb-plus', 'aria-hidden': 'true' }, v.done ? '✓' : '+'),
            h('span', { class: 'hb-name' }, h('span', { class: 't1' }, v.name), h('span', { class: 'hb-goal' }, goal))),
          dots(v, d.days || []), flame(v.streak),
          h('div', { class: 'hb-more' },
            h('button', { class: 'btn small icon', title: 'Undo today’s last log', 'aria-label': `Undo ${v.name}`, onclick: () => Vesper.act('habits', 'undo', { id: v.id }) }, '−'),
            h('button', { class: 'btn small icon', title: 'Stop tracking', 'aria-label': `Stop tracking ${v.name}`, onclick: () => { if (confirm(`Stop tracking ${v.name}?`)) Vesper.act('habits', 'remove', { id: v.id }); } }, '✕')));
        return row;
      }) : empty('Say “Vesper, track a habit: drink water 8 times a day”, then “I drank water”.'));
      if (hs.some(v => v.id === popped)) popped = null;
    }
  }

  Vesper.skill('habits', { panels: [{ view: 'life', span: 4, title: 'Habits', after: 'focus' }], render });
})();
