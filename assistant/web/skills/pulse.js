/* Pulse: the cards made overnight. Swipe (or ← / →) through them; 👍 / 👎 steers tomorrow's. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast } = Vesper.ui;
  const KIND = { idea: 'Idea', follow_up: 'Follow up', news: 'News', goal: 'Goal', task: 'Task', project: 'Project', note: 'Note', research: 'Research' };
  const st = { idx: 0, day: undefined, dir: 0, first: true };

  function go(step, to = null) {
    const cards = Vesper.data.pulse?.cards || [];
    if (!cards.length) return;
    st.idx = to != null ? to : (st.idx + step + cards.length) % cards.length;
    st.dir = step;
    paint(Vesper.data.pulse);
  }

  async function vote(c, v) {
    const r = await Vesper.act('pulse', 'vote', { id: c.id, vote: c.vote === v ? 0 : v });
    if (!r.ok) toast(r.error);
    else if (v < 0 && c.vote !== v) toast('Noted: fewer like that tomorrow.');
  }

  function swipe(el) {
    let x0 = null, dx = 0;
    el.addEventListener('pointerdown', e => {
      if (e.button || e.target.closest('a, button')) return;
      x0 = e.clientX; dx = 0; el.setPointerCapture(e.pointerId); el.classList.add('pl-drag');
    });
    el.addEventListener('pointermove', e => {
      if (x0 == null) return;
      dx = e.clientX - x0;
      el.style.transform = `translateX(${dx}px) rotate(${dx / 50}deg)`;
      el.style.opacity = String(1 - Math.min(Math.abs(dx) / 500, 0.4));
    });
    const end = () => {
      if (x0 == null) return;
      x0 = null; el.classList.remove('pl-drag'); el.style.transform = ''; el.style.opacity = '';
      if (Math.abs(dx) > 60) go(dx < 0 ? 1 : -1);
    };
    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);
  }

  function card(c, i, n) {
    const enter = st.dir > 0 ? 'pl-from-right' : st.dir < 0 ? 'pl-from-left' : st.first ? 'pl-rise' : '';
    const el = h('article', { class: `pl-card pl-k-${c.kind} ${enter}`, 'aria-roledescription': 'card', 'aria-label': `Card ${i + 1} of ${n}` },
      h('div', { class: 'pl-top' }, h('span', { class: 'pl-kind' }, KIND[c.kind] || c.kind),
        h('span', { class: 'pl-n num' }, `${String(i + 1).padStart(2, '0')} / ${String(n).padStart(2, '0')}`)),
      h('h3', { class: 'pl-title' }, c.title),
      c.body ? h('p', { class: 'pl-body' }, c.body) : null,
      c.why ? h('div', { class: 'pl-why' }, c.why) : null,
      h('div', { class: 'pl-foot' },
        c.link ? h('a', { class: 'btn small', href: c.link, target: '_blank', rel: 'noopener' }, 'Open') : null,
        h('span', { class: 'spacer' }),
        h('button', { class: 'btn small icon pl-vote', 'aria-pressed': String(c.vote === 1), title: 'More like this', 'aria-label': 'More like this', onclick: () => vote(c, 1) }, '👍'),
        h('button', { class: 'btn small icon pl-vote', 'aria-pressed': String(c.vote === -1), title: 'Less like this', 'aria-label': 'Less like this', onclick: () => vote(c, -1) }, '👎')));
    swipe(el);
    return el;
  }

  function research(d) {
    if (!d.can_research) return null;
    const box = h('input', { type: 'checkbox', onchange: async e => {
      const r = await Vesper.act('pulse', 'research', { on: e.target.checked });
      toast(r.ok ? (r.research ? 'Tomorrow’s Pulse gets a research card.' : 'No research card.') : r.error);
    } });
    box.checked = !!d.research;
    return h('label', { class: 'pl-research t2' }, box, ' Add a web research card each morning (uses your Claude key)');
  }

  function paint(d) {
    for (const p of panels('pulse')) {
      const b = body(p);
      if (!d) { fill(b, empty('Loading…')); continue; }
      const cards = d.cards || [];
      if (d.day !== st.day) { st.day = d.day; st.idx = 0; }
      st.idx = Math.min(st.idx, Math.max(0, cards.length - 1));
      const today = d.day && d.day === new Date().toLocaleDateString('en-CA');
      meta(p, cards.length ? `${today ? 'today' : d.day} · ${d.by === 'model' ? 'written by your model' : 'built-in'} · ${ago(d.built_at)}` : `next at ${d.time}`);
      if (!cards.length) {
        fill(b, empty(`No cards yet. They're made at ${d.time} each morning, or say “run pulse now”.`), research(d));
        continue;
      }
      const focused = document.activeElement?.classList?.contains('pl-stage') && b.contains(document.activeElement);
      const stage = h('div', { class: 'pl-stage', tabindex: '0', 'aria-label': 'Pulse cards. Left and right arrows move between them.',
        onkeydown: e => { if (e.key === 'ArrowRight') { e.preventDefault(); go(1); } else if (e.key === 'ArrowLeft') { e.preventDefault(); go(-1); } } },
        cards.length > 1 ? h('div', { class: 'pl-under', 'aria-hidden': 'true' }) : null, card(cards[st.idx], st.idx, cards.length));
      fill(b, stage,
        h('div', { class: 'pl-nav' },
          h('button', { class: 'btn small icon', 'aria-label': 'Previous card', onclick: () => go(-1) }, '‹'),
          h('div', { class: 'pl-dots' }, cards.map((x, i) => h('button', {
            class: `pl-dot ${i === st.idx ? 'on' : ''} ${x.vote > 0 ? 'up' : x.vote < 0 ? 'down' : ''}`,
            'aria-label': `Card ${i + 1}: ${x.title}`, 'aria-current': i === st.idx ? 'true' : null,
            onclick: () => go(i > st.idx ? 1 : -1, i) }))),
          h('button', { class: 'btn small icon', 'aria-label': 'Next card', onclick: () => go(1) }, '›')),
        research(d));
      if (focused) stage.focus({ preventScroll: true });
    }
    st.dir = 0; st.first = false;
  }

  Vesper.skill('pulse', {
    panels: [{ view: 'command', span: 6, title: 'Pulse', after: 'weather', buttons: [
      { label: 'Run now', onclick: async () => { const r = await Vesper.act('pulse', 'run', {}); toast(r.ok ? `${(r.cards || []).length} cards ready.` : r.error); } }] }],
    render: paint,
  });
})();
