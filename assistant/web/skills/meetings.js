/* Meeting prep: the next meeting with other people, who's coming, the last emails with each, related docs and notes. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast } = Vesper.ui;
  const RESP = { accepted: 'going', declined: 'declined', tentative: 'maybe', needsAction: 'no answer' };
  const time = iso => new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const initials = n => (n || '?').split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join('');
  const hue = s => { let x = 0; for (const c of s || '') x = (x * 31 + c.charCodeAt(0)) >>> 0; return `var(--series-${(x % 8) + 1})`; };
  let ticking = null;

  function until(iso) {
    const m = Math.round((new Date(iso) - Date.now()) / 60000);
    if (m <= 0) return 'now';
    return m < 60 ? `in ${m} min` : `in ${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
  }
  function chip(a, i) {
    return h('span', { class: `chip mt-chip mt-${a.response || 'none'}`, title: `${a.email} · ${RESP[a.response] || 'invited'}`, style: `--i:${i}` },
      h('span', { class: 'mt-av', style: { background: hue(a.email) }, 'aria-hidden': 'true' }, initials(a.name)),
      a.name, a.organizer ? h('span', { class: 'muted' }, ' · organizer') : null);
  }
  function section(title, kids) { return kids.length ? [h('div', { class: 'sub-h' }, title), h('div', { class: 'list' }, kids)] : []; }

  function card(c) {
    const mails = c.attendees.flatMap(a => a.emails.map(m => ({ ...m, who: a.name }))).sort((x, y) => y.ts - x.ts).slice(0, 5);
    const asks = c.attendees.flatMap(a => [...a.ask.map(t => ({ who: a.name, t, ask: true })), ...a.notes.map(t => ({ who: a.name, t }))]);
    return h('div', { class: 'mt-card' },
      h('div', { class: 'mt-head' },
        h('div', { class: 'mt-title' }, c.title),
        h('div', { class: 't2' }, `${time(c.start)}–${time(c.end)} · `, h('span', { class: 'mt-until', 'data-start': c.start }, until(c.start)),
          c.location && !c.link ? ` · ${c.location}` : ''),
        c.link ? h('a', { class: 'btn small primary mt-join', href: c.link, target: '_blank', rel: 'noopener' }, 'Join') : null),
      h('div', { class: 'chips mt-chips' }, c.attendees.map(chip)),
      asks.length ? h('div', { class: 'mt-asks' }, asks.map(x => h('div', { class: `mt-ask ${x.ask ? 'is-ask' : ''}` },
        h('b', {}, x.who.split(' ')[0]), ` · ${x.t}`))) : null,
      section('Last emails', mails.map(m => h('a', { class: 'item mail', href: m.link, target: '_blank', rel: 'noopener' },
        h('div', { class: 'main' }, h('div', { class: 't1' }, m.subject),
          h('div', { class: 't2' }, m.from_me ? `you → ${m.who}` : `${m.who} → you`)),
        h('span', { class: 'muted mail-age' }, ago(m.ts))))),
      section('Documents', c.docs.map(d => h('div', { class: 'item' }, h('div', { class: 'main' },
        h('div', { class: 't1' }, d.title), h('div', { class: 't2' }, `${d.folder} · ${d.file}`))))),
      section('Notes & tasks', [...c.notes.map(n => h('div', { class: 'item' }, h('div', { class: 'main t3' }, n))),
        ...c.tasks.map(t => h('div', { class: 'item' }, h('div', { class: 'main t3' }, `☐ ${t}`)))]),
      c.gmail_error ? h('div', { class: 't3 muted', style: { marginTop: '8px' } }, `No emails here: ${c.gmail_error}`) : null);
  }

  function render(d) {
    for (const p of panels('meetings')) {
      const b = body(p);
      if (!d) { fill(b, empty('Loading…')); continue; }
      const later = (d.upcoming || []).filter(u => !d.next || u.start !== d.next.start);
      meta(p, d.next ? `prepared ${ago(d.next.at)}` : '');
      fill(b,
        d.next ? card(d.next) : empty(d.hint || d.warning || 'No meetings with other people in the next day.'),
        later.length ? [h('div', { class: 'sub-h' }, 'Later'), h('div', { class: 'list' }, later.map(u => h('div', { class: 'item' },
          h('div', { class: 'main' }, h('div', { class: 't1' }, u.title), h('div', { class: 't2' }, u.with.join(', '))),
          h('span', { class: 'muted mail-age' }, time(u.start)))))] : null);
    }
    clearInterval(ticking);
    if (d?.next) ticking = setInterval(() => {
      if (document.hidden) return;
      for (const el of document.querySelectorAll('.mt-until')) el.textContent = until(el.dataset.start);
    }, 30000);
  }

  Vesper.skill('meetings', {
    panels: [{ view: 'work', span: 5, title: 'Next meeting · prep', after: 'inbox_zero', buttons: [
      { label: 'Refresh', onclick: async () => { const r = await Vesper.act('meetings', 'refresh', {}); if (r.ok === false) toast(r.error); } }] }],
    render,
  });
})();
