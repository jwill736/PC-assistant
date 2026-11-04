/* Undo: what Vesper changed this session, newest first, with Undo on each one that can be put back. An undo that
   needs a yes (a focus block, another app's switch) asks for it like any action. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast, status } = Vesper.ui;
  const STATE = { undone: ['good', 'undone'], changed: ['warning', 'you changed it since'] };

  async function undo(id) {
    const r = await Vesper.act('undo', 'undo', { id });
    toast(r.pending ? r.reply : r.ok ? r.reply || 'Undone.' : r.error || r.reply, { source: 'undo', level: r.ok ? 'good' : 'warn' });
  }

  function row(a) {
    const [lvl, text] = STATE[a.state] || [];
    return h('li', { class: `undo-row undo-${a.state}` },
      h('span', { class: `tier t${a.tier || 0}`, title: `Tier ${a.tier || 0}` }, `T${a.tier || 0}`),
      h('span', { class: 'undo-what' }, h('span', { class: 'undo-label' }, a.label),
        h('span', { class: 't3 muted' }, ` · ${ago(a.at)}${a.can_undo && a.undo ? ` · undo: ${a.undo}` : ''}`)),
      text ? status(lvl, text)
        : a.can_undo ? h('button', { class: 'btn small', onclick: () => undo(a.id), 'aria-label': `Undo ${a.label}` }, 'Undo')
          : h('span', { class: 't3 muted undo-cant' }, 'can’t undo'));
  }

  function paint(d) {
    for (const p of panels('undo')) {
      const b = body(p);
      if (!d) { fill(b, empty('Loading…')); continue; }
      const acts = d.actions || [];
      const can = acts.filter(a => a.can_undo).length;
      meta(p, acts.length ? `${can} of ${acts.length} can be undone · this session` : 'this session');
      fill(b, acts.length ? h('ul', { class: 'undo-list' }, acts.map(row))
        : empty('Nothing changed yet. Say “Vesper, undo that” after it changes something (volume, a scene, a window, a task…), or “undo the last 3”.'));
    }
  }
  Vesper.skill('undo', { panels: [{ view: 'pc', span: 6, title: 'Undo', after: 'janitor' }], render: paint });
})();
