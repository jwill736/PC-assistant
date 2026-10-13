/* Journal: this week's mood as a sparkline, a box to write in, and entries hidden until asked (and while live). */
(() => {
  const { h, fill, empty, meta, panels, body, toast } = Vesper.ui;
  const NS = 'http://www.w3.org/2000/svg';
  const sv = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); return e; };
  const MOOD = { '-2': 'hard', '-1': 'rough', 0: 'okay', 1: 'good', 2: 'great' };
  const tone = m => (m == null ? 'none' : m >= 0.5 ? 'up' : m <= -0.5 ? 'down' : 'flat');
  let open = false;

  function spark(week) {
    const W = 280, H = 64, pad = 8, step = (W - pad * 2) / Math.max(1, week.length - 1);
    const y = m => pad + (2 - m) / 4 * (H - pad * 2);
    const svg = sv('svg', { viewBox: `0 0 ${W} ${H + 14}`, class: 'jn-spark', role: 'img', 'aria-label': 'Mood over the last 7 days' });
    svg.append(sv('line', { x1: pad, x2: W - pad, y1: y(0), y2: y(0), class: 'jn-zero' }));
    let d = '', run = false;
    week.forEach((p, i) => {
      if (p.mood == null) { run = false; return; }
      d += `${run ? 'L' : 'M'}${(pad + i * step).toFixed(1)} ${y(p.mood).toFixed(1)} `; run = true;
    });
    if (d) svg.append(sv('path', { d, class: 'jn-line', pathLength: 1 }));
    week.forEach((p, i) => {
      const x = pad + i * step;
      if (p.mood != null) {
        const c = sv('circle', { cx: x, cy: y(p.mood), r: 3.5, class: `jn-pt ${tone(p.mood)}` });
        const t = sv('title', {}); t.textContent = `${p.day}: ${MOOD[Math.round(p.mood)]} (${p.n})`; c.append(t);
        svg.append(c);
      }
      const lab = sv('text', { x, y: H + 12, class: 'jn-day', 'text-anchor': 'middle' });
      lab.textContent = new Date(`${p.day}T12:00:00`).toLocaleDateString([], { weekday: 'narrow' });
      svg.append(lab);
    });
    return svg;
  }

  function entry(e) {
    const day = new Date(`${e.day}T12:00:00`).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
    return h('div', { class: 'item jn-entry' },
      h('span', { class: `jn-dot ${tone(e.mood)}`, title: MOOD[e.mood] }),
      h('div', { class: 'main' },
        h('div', { class: 't2' }, day, ' · ', MOOD[e.mood], (e.tags || []).length ? ` · ${e.tags.join(', ')}` : ''),
        h('div', { class: 'jn-text' }, e.text)),
      h('button', { class: 'btn small icon', 'aria-label': 'Delete entry', title: 'Delete', onclick: () => { if (confirm('Delete this entry?')) Vesper.act('journal', 'delete', { id: e.id }); } }, '✕'));
  }

  function render(d) {
    for (const p of panels('journal')) {
      const b = body(p);
      if (!d) { fill(b, empty('Loading…')); continue; }
      meta(p, `${d.count_week} this week · private`);
      if (!b._form) {
        const input = h('textarea', { rows: 2, placeholder: 'How did today go?', 'aria-label': 'New journal entry', maxlength: 4000 });
        b._form = h('form', { class: 'jn-form', onsubmit: async ev => {
          ev.preventDefault(); if (!input.value.trim()) return;
          const r = await Vesper.act('journal', 'add', { text: input.value }); toast(r.ok ? 'Saved.' : r.error); if (r.ok) input.value = '';
        } }, input, h('button', { class: 'btn small', type: 'submit' }, 'Save'));
        b._view = h('div', {});
        fill(b, b._view, b._form);
      }
      const avg = d.average_week;
      const entries = d.entries || [];
      fill(b._view,
        h('div', { class: `jn-top${b._drawn ? '' : ' jn-anim'}` }, spark(d.week || []),
          h('div', { class: 'jn-avg' }, h('div', { class: `v ${tone(avg)}` }, avg == null ? '—' : MOOD[Math.round(avg)]),
            h('div', { class: 'k' }, (d.tags_week || []).join(' · ') || 'this week'))),
        d.awaiting ? h('div', { class: 'jn-listen' }, h('span', { class: 'jn-pulse' }), 'Listening for your entry…') : null,
        d.live ? h('div', { class: 'jn-lock' }, 'Live: entries stay hidden until the stream ends.')
          : entries.length ? h('div', {},
            h('button', { class: 'btn small', 'aria-expanded': String(open), onclick: () => { open = !open; render(Vesper.data.journal); } },
              open ? 'Hide entries' : `Show entries (${entries.length})`),
            open ? h('div', { class: 'list jn-list' }, entries.map(entry)) : null)
            : empty('Say “journal: …”, “dear diary …”, or “end of day journal” and I’ll ask how it went.'));
      b._drawn = true;
    }
  }

  Vesper.skill('journal', { panels: [{ view: 'command', span: 4, title: 'Journal', after: 'habits' }], render });
})();
