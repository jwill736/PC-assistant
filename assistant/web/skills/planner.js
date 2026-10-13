/* Day plan: a vertical timeline of today (meetings, task blocks, stream, open time) with a live "now" line,
   the overbooked warning, shutdown recap with the stream-mode offer, and the weekly review. */
(() => {
  const { h, fill, empty, meta, panels, body, toast } = Vesper.ui;
  const PPM = 0.9; // px per minute
  const KIND = { meeting: 'Meeting', task: 'Task', stream: 'Stream', free: 'Open' };
  const tm = iso => new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const dur = m => (m >= 60 ? `${Math.floor(m / 60)}h${m % 60 ? ` ${m % 60}m` : ''}` : `${m}m`);
  const at = (day, hm) => { const [H, M] = hm.split(':').map(Number); const d = new Date(`${day}T00:00:00`); d.setHours(H, M, 0, 0); return d; };

  async function run(action, ok) {
    const r = await Vesper.act('planner', action, {});
    if (r.ok === false) toast(r.error || 'That didn’t work.'); else if (ok) toast(ok(r));
  }

  function timeline(plan) {
    const blocks = plan.blocks || [];
    const hours = plan.hours || { start: '09:00', end: '18:00' };
    let t0 = at(plan.date, hours.start), t1 = at(plan.date, hours.end);
    for (const b of blocks) { const s = new Date(b.start), e = new Date(b.end); if (s < t0) t0 = s; if (e > t1) t1 = e; }
    t0 = new Date(t0); t0.setMinutes(0, 0, 0);
    const mins = (t1 - t0) / 60000, y = d => ((d - t0) / 60000) * PPM;
    const grid = [];
    for (let d = new Date(t0); d <= t1; d = new Date(d.getTime() + 3600000)) {
      grid.push(h('div', { class: 'dp-hour', style: { top: `${y(d)}px` } },
        h('span', {}, d.toLocaleTimeString([], { hour: 'numeric' }))));
    }
    const els = blocks.map((b, i) => {
      const s = new Date(b.start), e = new Date(b.end);
      const tall = (e - s) / 60000 * PPM;
      return h('div', {
        class: `dp-b dp-${b.kind}${tall < 30 ? ' dp-thin' : ''}`, title: `${KIND[b.kind] || b.kind}: ${b.title} · ${tm(b.start)}–${tm(b.end)}`,
        style: `top:${y(s)}px;height:${Math.max(16, tall - 2)}px;--i:${i}`,
      },
        h('div', { class: 'dp-t' }, b.priority === 1 ? h('span', { class: 'dp-p1' }, 'P1') : null, b.title,
          b.part ? h('span', { class: 'muted' }, ` · ${b.part}`) : null),
        tall >= 30 ? h('div', { class: 'dp-s' }, `${tm(b.start)}–${tm(b.end)} · ${dur(b.minutes)}`) : null);
    });
    const now = h('div', { class: 'dp-now', 'aria-hidden': 'true' }, h('i', {}));
    const wrap = h('div', { class: 'dp-track', style: { height: `${mins * PPM}px` } }, grid, els, now);
    if (!timeline.drawn) { wrap.classList.add('dp-anim'); timeline.drawn = true; }
    const place = () => {
      const n = new Date(), show = plan.date === n.toLocaleDateString('en-CA') && n >= t0 && n <= t1;
      now.style.opacity = show ? '1' : '0';
      now.style.transform = `translateY(${y(n)}px)`;
      return show ? y(n) : null;
    };
    return { wrap, place };
  }

  function review(r) {
    return h('div', { class: 'dp-card' },
      h('div', { class: 't3 muted' }, `Week of ${r.week_of}${r.by === 'model' ? ' · written with the model' : ''}`),
      h('div', { class: 'chips dp-chips' }, Object.entries(r.hours || {}).map(([k, v]) => h('span', { class: 'pill' }, `${k} ${v}h`)),
        h('span', { class: 'pill' }, `${r.tasks_done} done`), h('span', { class: 'pill' }, `${r.meetings} meetings`),
        h('span', { class: 'pill' }, `${r.streams} streams`)),
      r.wins.length ? h('div', { class: 'dp-list good' }, h('b', {}, 'Wins'), r.wins.map(w => h('div', {}, w))) : null,
      r.leaks.length ? h('div', { class: 'dp-list warn' }, h('b', {}, 'Leaks'), r.leaks.map(w => h('div', {}, w))) : null,
      r.next_week.length ? h('div', { class: 'dp-list' }, h('b', {}, 'Next week'), r.next_week.map((w, i) => h('div', {}, `${i + 1}. ${w}`))) : null);
  }

  function shutdown(s) {
    return h('div', { class: 'dp-card' },
      h('div', { class: 't1' }, `Shut down · ${s.done.length} done · ${dur(s.work_min)} work${s.deep_min ? ` · ${dur(s.deep_min)} deep` : ''}`),
      s.carried.length ? h('div', { class: 't3 muted' }, `Rolled to ${new Date(`${s.carried_to}T12:00:00`).toLocaleDateString([], { weekday: 'long' })}: ${s.carried.join(', ')}`) : null,
      s.offer ? h('button', { class: 'btn primary small dp-offer', onclick: () => run('stream_mode', r => r.reply || 'Stream mode.') },
        `${s.offer.label} · ${tm(s.stream.start)}`) : null);
  }

  function render(d) {
    for (const p of panels('planner')) {
      const b = body(p);
      clearInterval(p._tick);
      const plan = d?.plan;
      if (!plan) { fill(b, empty('Loading…')); continue; }
      if (plan.ok === false) { fill(b, empty(plan.error || 'No plan.')); continue; }
      const ld = plan.load || {};
      meta(p, `${dur(ld.meetings_min || 0)} meetings · ${dur((plan.blocks || []).filter(x => x.kind === 'task').reduce((a, x) => a + x.minutes, 0))} tasks`);
      const tl = timeline(plan);
      const scroller = h('div', { class: 'dp-scroll' }, tl.wrap);
      fill(b,
        ld.overbooked ? h('div', { class: 'dp-warn', role: 'status' }, `Overbooked by ${dur(ld.over_min)}`,
          plan.push?.length ? h('span', {}, ` · push ${plan.push.join(', ')} to tomorrow?`) : null) : null,
        plan.headline ? h('div', { class: 'dp-head' }, plan.headline) : null,
        (plan.notes || []).length ? h('div', { class: 't3 muted dp-notes' }, plan.notes.join(' ')) : null,
        plan.all_day?.length ? h('div', { class: 'chips' }, plan.all_day.map(x => h('span', { class: 'pill' }, x))) : null,
        (plan.blocks || []).length ? scroller : empty('Nothing on the calendar and no open tasks. Add a task and say “plan my day”.'),
        plan.unplanned?.length ? h('div', { class: 't3 muted dp-left' }, `Didn’t fit: ${plan.unplanned.map(x => `${x.title} (${dur(x.minutes)})`).join(', ')}`) : null,
        d.shutdown ? shutdown(d.shutdown) : null,
        d.review ? review(d.review) : null,
        h('div', { class: 'controls dp-actions' },
          h('button', { class: 'btn small', onclick: () => run('shutdown', r => `Shut down: ${r.done.length} done, ${r.carried.length} rolled over.`) }, 'Shut down day'),
          h('button', { class: 'btn small', onclick: () => run('review', () => 'Weekly review ready.') }, 'Weekly review')));
      const y = tl.place();
      if (!p._scrolled && y != null) { scroller.scrollTop = Math.max(0, y - 80); p._scrolled = true; }
      p._tick = setInterval(() => { if (!document.hidden) tl.place(); }, 30000);
    }
  }

  Vesper.skill('planner', {
    panels: [{ view: 'command', span: 4, title: 'Day plan', after: 'automations', buttons: [
      { label: 'Re-plan', onclick: () => run('replan', r => (r.by === 'model' ? 'Re-planned with the model.' : 'Re-planned.')) }] }],
    render,
  });
})();
