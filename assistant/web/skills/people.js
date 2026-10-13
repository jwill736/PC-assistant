/* People: search everyone J emails or meets, what Vesper remembers about them, and who has gone quiet. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast } = Vesper.ui;
  const initials = n => (n || '?').split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join('');
  const hue = s => { let x = 0; for (const c of s || '') x = (x * 31 + c.charCodeAt(0)) >>> 0; return `var(--series-${(x % 8) + 1})`; };
  const S = { q: '', results: null, open: null, timer: null };

  async function search(q) {
    S.q = q.trim();
    if (!S.q) { S.results = null; return render(Vesper.data.people); }
    const r = await Vesper.act('people', 'search', { q: S.q });
    if (r.ok === false) return toast(r.error);
    if (r.q === S.q.toLowerCase()) { S.results = r.people; render(Vesper.data.people); }
  }
  async function remember(p, input) {
    const text = input.value.trim();
    if (!text) return;
    const r = await Vesper.act('people', 'remember', { name: p.email, text });
    if (r.ok === false) return toast(r.error);
    input.value = ''; toast(`Saved for ${p.name.split(' ')[0]}.`);
    if (S.q) search(S.q);
  }
  async function forget(id) {
    const r = await Vesper.act('people', 'forget', { id });
    if (r.ok === false) toast(r.error); else if (S.q) search(S.q);
  }

  function details(p) {
    const input = h('input', { placeholder: `Remember about ${p.name.split(' ')[0]}… (start with “ask about” for next time)`, 'aria-label': 'Add a note' });
    return h('div', { class: 'pp-detail' },
      (p.subjects || []).length ? h('div', { class: 't3' }, h('span', { class: 'muted' }, 'Recent: '), p.subjects.slice(0, 4).join(' · ')) : null,
      p.next_meeting ? h('div', { class: 't3' }, h('span', { class: 'muted' }, 'Next meeting: '), p.next_meeting) : null,
      [...(p.ask || []).map(n => [n, true]), ...(p.notes || []).map(n => [n, false])].map(([n, ask]) =>
        h('div', { class: `pp-note ${ask ? 'is-ask' : ''}` }, h('span', {}, n.text),
          h('button', { class: 'btn small icon ghost', 'aria-label': 'Forget this', title: 'Forget', onclick: e => { e.stopPropagation(); forget(n.id); } }, '✕'))),
      h('form', { class: 'addrow', onsubmit: e => { e.preventDefault(); remember(p, input); }, onclick: e => e.stopPropagation() },
        input, h('button', { class: 'btn small', type: 'submit' }, 'Save')));
  }
  function person(p, i) {
    const open = S.open === p.email;
    const when = p.last_contact ? ago(p.last_contact) : (p.next_meeting ? 'meeting soon' : '');
    const notes = (p.notes || []).length + (p.ask || []).length;
    return h('div', { class: `pp-card ${open ? 'open' : ''}`, style: `--i:${i}`, role: 'button', tabindex: '0', 'aria-expanded': String(open),
      onclick: () => { S.open = open ? null : p.email; render(Vesper.data.people); },
      onkeydown: e => { if (e.key === 'Enter' && e.target === e.currentTarget) e.currentTarget.click(); } },
    h('div', { class: 'pp-top' },
      h('span', { class: 'pp-av', style: { background: hue(p.email) }, 'aria-hidden': 'true' }, initials(p.name)),
      h('div', { class: 'pp-main' },
        h('div', { class: 't1' }, p.name, ' ', h('span', { class: 'pill pp-acct' }, p.account)),
        h('div', { class: 't2' }, [when, p.count ? `${p.count} email${p.count === 1 ? '' : 's'}${p.meetings ? ` · ${p.meetings} meeting${p.meetings === 1 ? '' : 's'}` : ''}` : null,
          notes ? `${notes} note${notes === 1 ? '' : 's'}` : null].filter(Boolean).join(' · '))),
      (p.ask || []).length ? h('span', { class: 'pp-flag', title: 'Something to ask next time' }, '●') : null),
    p.last_subject && !open ? h('div', { class: 't3 muted pp-subj' }, p.last_subject) : null,
    open ? details(p) : null);
  }

  function render(d) {
    for (const p of panels('people')) {
      const b = body(p);
      if (!b._input) {
        b._input = h('input', { type: 'search', placeholder: 'Find someone: name or email', 'aria-label': 'Find a person',
          oninput: e => { clearTimeout(S.timer); S.timer = setTimeout(() => search(e.target.value), 220); } });
        b._list = h('div', {});
        fill(b, h('div', { class: 'addrow' }, b._input), b._list);
      }
      if (!d) { fill(b._list, empty('Loading…')); continue; }
      if (d.ok === false) { meta(p, ''); fill(b._list, empty(d.error)); continue; }
      meta(p, `${d.count} people${d.built ? ` · updated ${ago(d.built)}` : ''}`);
      const list = S.results ?? d.recent ?? [];
      fill(b._list,
        d.warning ? h('div', { class: 't3 muted', style: { marginBottom: '6px' } }, `From your calendar only: ${d.warning}`) : null,
        !S.q && (d.overdue || []).length ? h('div', { class: 'pp-quiet t3' }, h('span', { class: 'muted' }, 'Gone quiet: '),
          d.overdue.map(o => h('span', { class: 'pill' }, `${o.name} · ${o.days_since}d`))) : null,
        list.length ? h('div', { class: 'pp-grid' }, list.map(person))
          : empty(S.q ? `Nobody called “${S.q}” yet.` : 'People show up here from your email and calendar. Say “who is Dana” or “remember that Dana…”.'));
    }
  }

  Vesper.skill('people', { panels: [{ view: 'work', span: 12, title: 'People · who you deal with', after: 'meetings' }], render });
})();
