/* Focus: a big progress ring counting down the session, what it's for, and today's numbers. */
(() => {
  const { h, fill, meta, panels, body, toast } = Vesper.ui;
  const NS = 'http://www.w3.org/2000/svg';
  const sv = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); return e; };
  const clock = s => { s = Math.max(0, Math.round(s)); const m = Math.floor(s / 60); return `${m}:${String(s % 60).padStart(2, '0')}`; };
  const dur = m => (m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m` : `${m}m`);
  let ticking = null;

  function build(b) {
    const svg = sv('svg', { viewBox: '0 0 120 120', class: 'fx-ring', role: 'img' });
    svg.append(sv('circle', { cx: 60, cy: 60, r: 52, class: 'fx-track' }));
    const arc = sv('circle', { cx: 60, cy: 60, r: 52, class: 'fx-arc', pathLength: 1 });
    svg.append(arc);
    const left = h('div', { class: 'fx-left num' }, '—');
    const label = h('div', { class: 'fx-label' });
    const drift = h('div', { class: 'fx-drift' });
    const input = h('input', { placeholder: 'What for? (optional)', 'aria-label': 'What this focus session is for', maxlength: 80 });
    const go = m => async () => {
      const r = await Vesper.act('focus', 'start', { minutes: m, label: input.value.trim() });
      if (!r.ok) toast(r.error); else input.value = '';
    };
    const start = h('div', { class: 'fx-start' }, h('div', { class: 'addrow' }, input),
      h('div', { class: 'fx-btns' }, [25, 50, 90].map(m => h('button', { class: 'btn small', onclick: go(m) }, `${m} min`))));
    const stop = h('button', { class: 'btn small', onclick: async () => { const r = await Vesper.act('focus', 'stop', {}); if (!r.ok) toast(r.error); } }, 'Stop');
    const stats = h('div', { class: 'fx-stats' });
    const last = h('div', { class: 'fx-last' });
    const wrap = h('div', { class: 'fx' }, h('div', { class: 'fx-dial' }, svg, h('div', { class: 'fx-center' }, left, label, drift)),
      h('div', { class: 'fx-side' }, stats, start, stop, last));
    fill(b, wrap);
    return { svg, arc, left, label, drift, start, stop, stats, last, wrap };
  }

  function stat(v, k) { return h('div', { class: 'fx-stat' }, h('div', { class: 'v num' }, v), h('div', { class: 'k' }, k)); }

  function tick(ui, d) {
    const a = d?.active;
    const now = Date.now() / 1000;
    if (a) {
      const total = a.end - a.start, left = Math.max(0, a.end - now);
      ui.arc.style.strokeDashoffset = String(1 - left / total);
      ui.left.textContent = clock(left);
      ui.svg.setAttribute('aria-label', `${clock(left)} left`);
    } else {
      ui.arc.style.strokeDashoffset = '0';
      ui.left.textContent = clock((d?.default_minutes || 50) * 60);
      ui.svg.setAttribute('aria-label', 'No focus session');
    }
  }

  function render(d) {
    for (const p of panels('focus')) {
      const b = body(p);
      const ui = b._ui || (b._ui = build(b));
      const a = d?.active, td = d?.today || {};
      ui.wrap.classList.toggle('on', !!a);
      ui.wrap.classList.toggle('drifting', !!a?.drifting);
      meta(p, a ? `${a.distractions} distraction${a.distractions === 1 ? '' : 's'}` : (d?.streak ? `${d.streak}-day streak` : ''));
      ui.label.textContent = a ? (a.label || 'Focus') : 'ready';
      ui.drift.textContent = a?.drifting ? `on ${a.drifting}` : '';
      ui.start.hidden = !!a; ui.stop.hidden = !a;
      fill(ui.stats, stat(dur(td.minutes || 0), 'today'), stat(String(td.sessions || 0), 'sessions'),
        stat(String(d?.streak || 0), 'streak'));
      const l = d?.last;
      fill(ui.last, l && !a && Date.now() / 1000 - l.at < 7200
        ? `Last: ${dur(l.minutes)} · ${l.distractions ? `${l.distractions} distraction${l.distractions === 1 ? '' : 's'} · ${l.on_task_pct}% on task` : 'no distractions'} · take ${l.break_min}` : null);
      tick(ui, d);
    }
    clearInterval(ticking);
    if (d?.active) ticking = setInterval(() => { if (!document.hidden) for (const p of panels('focus')) if (body(p)._ui) tick(body(p)._ui, Vesper.data.focus); }, 1000);
  }

  Vesper.skill('focus', { panels: [{ view: 'command', span: 4, title: 'Focus', after: 'planner' }], render });
})();
