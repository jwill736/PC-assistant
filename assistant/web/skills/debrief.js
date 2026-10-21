/* Meeting debrief: after a meeting with people, "Anything to note from …?" with a box to type it (or just say it),
   and the last debriefs: the note, the tasks it made (with due dates) and who got a People note. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast } = Vesper.ui;
  const time = iso => new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const day = iso => new Date(`${iso}T12:00:00`).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });

  async function save(key, input, btn) {
    if (!input.value.trim()) return input.focus();
    btn.disabled = true;
    const r = await Vesper.act('debrief', 'save', { key, text: input.value });
    btn.disabled = false;
    if (r.ok === false) return toast(r.error);
    input.value = '';
    toast(`Noted${r.tasks.length ? ` · ${r.tasks.length} task${r.tasks.length === 1 ? '' : 's'}` : ''}${r.people.length ? ` · notes on ${r.people.join(', ')}` : ''}`,
      { level: 'good', source: 'debrief' });
  }

  function ask(a, b) {
    if (!b._box || b._box.key !== a.key) {  // built once per meeting: what J is typing survives updates
      const input = h('textarea', { rows: 3, maxlength: 4000, 'aria-label': `Notes from ${a.title}`,
        placeholder: 'e.g. I’ll send Dana the revised deck by Friday. We need to book the venue next week.' });
      const btn = h('button', { class: 'btn small primary', type: 'submit' }, 'Save');
      b._box = { key: a.key, el: h('form', { class: 'db-form', onsubmit: ev => { ev.preventDefault(); save(a.key, input, btn); } },
        input, h('div', { class: 'db-btns' }, btn,
          h('button', { class: 'btn small ghost', type: 'button', onclick: () => Vesper.act('debrief', 'skip', { key: a.key }) }, 'Nothing'))) };
    }
    return h('div', { class: 'db-ask' },
      h('div', { class: 'db-q' }, `Anything to note from `, h('b', {}, a.title), '?'),
      h('div', { class: 't3 muted' }, `ended ${time(a.end)}${a.with.length ? ` · with ${a.with.join(', ')}` : ''}`),
      a.listening ? h('div', { class: 'db-listen' }, h('span', { class: 'db-pulse', 'aria-hidden': 'true' }), 'Listening: just say it') : null,
      b._box.el);
  }

  function past(r) {
    return h('div', { class: 'item db-item' }, h('div', { class: 'main' },
      h('div', { class: 't1' }, r.title, h('span', { class: 'muted' }, ` · ${ago(r.done)}`)),
      h('div', { class: 't3 db-text' }, r.text),
      r.tasks.length ? h('div', { class: 'db-tasks' }, r.tasks.map(t => h('span', { class: 'pill db-task' }, `☐ ${t.title}`,
        t.due ? h('span', { class: 'db-due' }, ` · ${day(t.due)}`) : null))) : null,
      r.people.length ? h('div', { class: 't3 muted' }, `People notes: ${r.people.join(', ')}`) : null));
  }

  function render(d) {
    for (const p of panels('debrief')) {
      const b = body(p);
      if (!d) { fill(b, empty('Loading…')); continue; }
      meta(p, d.asking ? 'waiting for your notes' : d.recent?.length ? `last ${ago(d.recent[0].done)}` : '');
      if (!d.asking) b._box = null;
      fill(b,
        d.warning ? h('div', { class: 't3 muted' }, d.warning) : null,
        d.asking ? ask(d.asking, b) : d.last && !d.last.done ? h('div', { class: 'db-offer' },
          h('div', { class: 't2' }, `${d.last.title} ended ${time(d.last.end)}.`),
          h('button', { class: 'btn small', onclick: async () => { const r = await Vesper.act('debrief', 'start', {}); if (r.ok === false) toast(r.error); } }, 'Debrief it')) : null,
        d.live ? empty('Live: debriefs stay hidden until the stream ends.') :
          d.recent?.length ? h('div', { class: 'list db-list' }, d.recent.map(past)) :
            !d.asking ? empty('After a meeting with other people I’ll ask what to note. Or say “debrief my last meeting”.') : null);
    }
  }

  Vesper.skill('debrief', { panels: [{ view: 'work', span: 4, title: 'Meeting debrief', after: 'meetings' }], render });
})();
