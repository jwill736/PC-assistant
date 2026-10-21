/* Privacy while live: the Stream tab's switch, and "click to peek" on the panels that blur on air. The blur itself is
   the live-private class on <body> (app.js sets it from the live state) and the shared rule in styles.css. */
(() => {
  const { h, fill, empty, meta, panels, body, toast } = Vesper.ui;
  const NAMES = { inbox: 'Inbox', inbox_zero: 'Replies', people: 'People', bills: 'Money', journal: 'Journal', memory: 'Memory', clipboard: 'Clipboard', rewind: 'Rewind' };
  let PRIVATE = Object.keys(NAMES);
  const PEEK_MS = 10000;
  const isPrivate = p => PRIVATE.includes(p?.dataset.panel);
  const on = () => document.body.classList.contains('live-private');

  function peek(el, label) {
    if (el.classList.contains('peek')) return;
    el.classList.add('peek');
    const barEl = el.matches('.panel') ? h('span', { class: 'sp-peekbar', 'aria-hidden': 'true' }) : null;
    if (barEl) el.append(barEl);
    if (label) el.setAttribute('aria-description', `${label} shown for 10 seconds`);
    setTimeout(() => { el.classList.remove('peek'); el.removeAttribute('aria-description'); barEl?.remove(); }, PEEK_MS);
  }
  // A click (or Enter/Space) on a covered panel or a blurred notification peeks instead of acting.
  function onPeek(e) {
    if (!on()) return;
    const panel = e.target.closest?.('.panel');
    if (panel && isPrivate(panel) && !panel.classList.contains('peek') && e.target.closest('.panel > .body')) {
      e.preventDefault(); e.stopPropagation(); peek(panel, NAMES[panel.dataset.panel]); return;
    }
    const txt = e.target.closest?.('.toast .t-txt, .n-txt, .nudge-feed .t1');
    if (txt && !txt.classList.contains('peek')) { e.preventDefault(); e.stopPropagation(); peek(txt); }
  }
  document.addEventListener('click', onPeek, true);
  document.addEventListener('keydown', e => { if ((e.key === 'Enter' || e.key === ' ') && e.target.matches?.('.panel > .body[data-covered]')) onPeek(e); }, true);
  // Covered bodies are focusable buttons for the keyboard and screen readers; uncovered, they're plain again.
  function label() {
    for (const p of document.querySelectorAll('.panel[data-panel]')) {
      const b = p.querySelector(':scope > .body');
      if (!b) continue;
      const cover = on() && isPrivate(p);
      if (cover && !b.dataset.covered) { b.dataset.covered = '1'; b.tabIndex = 0; b.setAttribute('role', 'button'); b.setAttribute('aria-label', `${NAMES[p.dataset.panel]}: hidden while live. Press to peek for 10 seconds.`); }
      else if (!cover && b.dataset.covered) { delete b.dataset.covered; b.removeAttribute('tabindex'); b.removeAttribute('role'); b.removeAttribute('aria-label'); }
    }
  }
  let was = null;
  new MutationObserver(() => { if (on() !== was) { was = on(); label(); Vesper.repaint('stream_privacy'); } })
    .observe(document.body, { attributes: true, attributeFilter: ['class'] });

  Vesper.skill('stream_privacy', {
    panels: [{ view: 'stream', span: 3, title: 'Privacy while live', after: 'stream_raid' }],
    render(d) {
      if (d?.panels?.length) PRIVATE = d.panels;
      for (const p of panels('stream_privacy')) {
        const b = body(p);
        if (!d) { fill(b, empty('Loading…')); continue; }
        const covering = on();
        meta(p, covering ? h('span', { class: 'sp-badge' }, 'HIDING') : d.on ? 'armed' : 'off');
        const sw = h('button', { class: `sp-switch${d.on ? ' on' : ''}`, role: 'switch', 'aria-checked': String(!!d.on), 'aria-label': 'Privacy while live',
          onclick: async () => { const r = await Vesper.act('stream_privacy', 'toggle', { on: !d.on }); if (r.ok === false) toast(r.error); } }, h('i', {}));
        const shows = (d.captures || []).filter(c => c.shows_hud);
        fill(b,
          h('div', { class: 'sp-row' }, sw, h('div', {}, h('div', { class: 't1' }, d.on ? 'On' : 'Off'),
            h('div', { class: 't3 muted' }, d.on ? 'Private panels blur while you’re live.' : 'Panels stay visible on stream.'))),
          h('div', { class: `t3 sp-state${covering ? ' live' : ''}` }, covering
            ? `Hiding ${PRIVATE.map(n => NAMES[n] || n).join(', ')} and notification text${d.why === 'recording your screen' ? ' (OBS is recording your screen)' : ''}. Click one to peek for 10 s.`
            : d.on ? 'Not live: everything is visible.' : ''),
          shows.length ? h('div', { class: 't3 muted' }, `OBS shows your screen: ${shows.map(c => c.name).join(', ')}.`) : null,
          h('div', { class: 't3 muted' }, d.on ? 'Vesper’s Windows pop-ups wait while hidden; the HUD and voice still ask.' : null));
      }
      label();
    },
  });
})();
