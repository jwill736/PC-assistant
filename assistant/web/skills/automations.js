/* Timers (live rings), reminders and routines, plus a one-line box: type it the way you'd say it. */
(() => {
  const { h, fill, empty, meta, panels, body, toast } = Vesper.ui;
  const NS = 'http://www.w3.org/2000/svg';
  let ticking = null;
  function ring(frac, size = 44) {
    const s = document.createElementNS(NS, 'svg'); s.setAttribute('viewBox', '0 0 44 44'); s.setAttribute('width', size); s.setAttribute('height', size);
    s.setAttribute('class', 'tm-ring');
    const c = (cls, r) => { const e = document.createElementNS(NS, 'circle'); e.setAttribute('cx', 22); e.setAttribute('cy', 22); e.setAttribute('r', r); e.setAttribute('class', cls); s.append(e); return e; };
    c('tm-track', 18);
    const arc = c('tm-arc', 18); arc.setAttribute('pathLength', 1); arc.style.strokeDashoffset = String(1 - Math.max(0, Math.min(1, frac)));
    return s;
  }
  const clock = s => { s = Math.max(0, Math.round(s)); const m = Math.floor(s / 60), r = s % 60; return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}:${String(r).padStart(2, '0')}` : `${m}:${String(r).padStart(2, '0')}`; };
  function paint(d) {
    const now = Date.now() / 1000;
    for (const p of panels('automations')) {
      const b = body(p);
      const items = d?.items || [];
      meta(p, items.length ? `${items.length} set` : '');
      if (!b._form) {
        const input = h('input', { placeholder: 'e.g. remind me at 3 pm to send the invoice · 25 minute timer · every weekday at 9 am check my email', 'aria-label': 'New reminder, timer or routine' });
        b._form = h('form', { class: 'addrow', onsubmit: async e => {
          e.preventDefault(); if (!input.value.trim()) return;
          const r = await Vesper.act('automations', 'add', { text: input.value }); toast(r.ok ? 'Set.' : r.error); if (r.ok) input.value = '';
        } }, input, h('button', { class: 'btn small', type: 'submit' }, 'Add'));
        b._list = h('div', {});
        fill(b, b._list, b._form);
      }
      const timers = items.filter(i => i.kind === 'timer'), rest = items.filter(i => i.kind !== 'timer');
      fill(b._list,
        timers.length ? h('div', { class: 'tm-timers' }, timers.map(t => {
          const left = t.at - now;
          return h('div', { class: `tm ${left <= 0 ? 'done' : ''}` }, ring(t.total_s ? left / t.total_s : 0),
            h('div', {}, h('div', { class: 'num tm-left' }, clock(left)), h('div', { class: 't3 muted' }, t.label || 'Timer')),
            h('button', { class: 'btn small icon', 'aria-label': 'Cancel timer', onclick: () => Vesper.act('automations', 'cancel', { id: t.id }) }, '✕'));
        })) : null,
        rest.length ? h('div', { class: 'list' }, rest.map(i => h('div', { class: 'item' },
          h('div', { class: 'main' }, h('div', { class: 't1' }, i.kind === 'routine' ? `↻ ${i.command}` : i.text),
            h('div', { class: 't3 muted' }, i.every || i.moment || i.when)),
          h('button', { class: 'btn small icon', 'aria-label': 'Cancel', onclick: () => Vesper.act('automations', 'cancel', { id: i.id }) }, '✕')))) :
          (timers.length ? null : empty('Say “Vesper, remind me in 20 minutes to …”, “10 minute timer”, or “every weekday at 9 am check my email”.')));
    }
    clearInterval(ticking);
    if (items_have_timers(d)) ticking = setInterval(() => { if (!document.hidden) paint(Vesper.data.automations); }, 1000);
  }
  const items_have_timers = d => (d?.items || []).some(i => i.kind === 'timer');
  Vesper.skill('automations', { panels: [{ view: 'command', span: 4, title: 'Reminders & routines', after: 'nudges' }], render: paint });
})();
