/* Google Tasks sync (Setup tab): which "Vesper · …" list each area goes to, when it last synced, what moved. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast, status } = Vesper.ui;

  function line(c) {
    if (!c) return 'Not synced yet.';
    const bits = [c.sent && `${c.sent} sent`, c.brought_in && `${c.brought_in} brought in`,
      (c.updated_here + c.updated_there) && `${c.updated_here + c.updated_there} updated`,
      c.conflicts && `${c.conflicts} changed on both sides (newest kept)`,
      c.unlinked && `${c.unlinked} deleted in Google (kept here)`].filter(Boolean);
    return bits.length ? `Last sync: ${bits.join(' · ')}` : 'Last sync: nothing new.';
  }

  function render(d) {
    for (const p of panels('gtasks')) {
      const b = body(p);
      if (!d) { fill(b, empty('Loading…')); continue; }
      meta(p, d.at ? `synced ${ago(d.at)}` : '');
      if (d.ok === false) {
        fill(b, empty(d.error || 'Google Tasks isn’t set up.'));
        continue;
      }
      fill(b,
        h('div', { class: 'list' }, (d.lists || []).map(l => h('div', { class: 'item' },
          h('div', { class: 'main' }, h('div', { class: 't1' }, status('good', 'on'), ' ', l.title),
            h('div', { class: 't2' }, `${l.profile} tasks ↔ ${l.account}`)),
          h('span', { class: 'muted gt-n' }, `${l.tasks}`)))),
        h('div', { class: 't3 muted gt-line' }, line(d.counts)),
        (d.errors || []).length ? h('div', { class: 'gt-err', role: 'status' }, d.errors.join(' ')) :
          d.missing ? h('div', { class: 'gt-err', role: 'status' }, d.missing) : null,
        h('div', { class: 'controls', style: { marginTop: '8px' } },
          h('button', { class: 'btn small', onclick: async ev => {
            const btn = ev.currentTarget; btn.disabled = true;
            const r = await Vesper.act('gtasks', 'sync', {}); btn.disabled = false;
            toast(r.ok === false ? (r.errors || [r.error]).join(' ') : 'Google Tasks is in step.');
          } }, 'Sync now'),
          h('span', { class: 't3 muted' }, 'Every 5 minutes and right after a change. Newest change wins.')));
    }
  }

  Vesper.skill('gtasks', { panels: [{ view: 'setup', span: 4, title: 'Google Tasks', after: 'google' }], render });
})();
