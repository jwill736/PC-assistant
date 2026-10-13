/* Memory: the core profile (edit it and Vesper leaves it), what Vesper learned, search, pin, forget. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast } = Vesper.ui;
  const LABELS = { profile: 'Who you are', preferences: 'How you like things', now: "What's going on now" };
  let results = null;
  async function act(action, payload, ok) { const r = await Vesper.act('memory', action, payload); toast(r.ok ? ok : (r.error || 'That failed.')); return r; }
  function factRow(f) {
    return h('div', { class: `item mem-fact ${f.pinned ? 'pinned' : ''}` },
      h('div', { class: 'main' }, h('div', { class: 't1' }, f.statement),
        h('div', { class: 't3 muted' }, [f.kind, f.source, f.learned_at ? ago(f.learned_at) : null].filter(Boolean).join(' · '))),
      h('button', { class: 'btn small icon', title: f.pinned ? 'Unpin' : 'Pin: always remember', 'aria-label': 'Pin', onclick: () => act('pin', { id: f.id, pinned: !f.pinned }, f.pinned ? 'Unpinned.' : 'Pinned.') }, f.pinned ? '◆' : '◇'),
      h('button', { class: 'btn small icon', title: 'Forget', 'aria-label': 'Forget', onclick: () => { if (confirm(`Forget “${f.statement}”?`)) act('forget', { id: f.id }, 'Forgotten.'); } }, '✕'));
  }
  Vesper.skill('memory', {
    panels: [{ view: 'setup', span: 12, title: 'Memory · what Vesper knows about you', after: 'google' }],
    render(d) {
      for (const p of panels('memory')) {
        const b = body(p);
        if (!d) { fill(b, empty('Loading…')); continue; }
        const e = d.embeddings || {};
        meta(p, `${d.facts} things known · ${e.on ? `meaning search on (${e.model})` : 'word search'}`);
        if (b._editing) continue;  // don't wipe a block being typed
        const blocks = Object.entries(LABELS).map(([label, title]) => {
          const blk = (d.core || {})[label] || { text: '' };
          const ta = h('textarea', { rows: 3, 'aria-label': title, placeholder: 'Vesper writes this each night from what it learns. Edit it to set it yourself.' });
          ta.value = blk.text || '';
          ta.addEventListener('focus', () => { b._editing = true; });
          ta.addEventListener('blur', () => { setTimeout(() => { b._editing = false; }, 300); });
          return h('div', { class: 'mem-block' },
            h('div', { class: 'sub-h' }, title, blk.edited_by === 'J' ? h('span', { class: 'mem-yours' }, ' · yours') : null),
            ta, h('div', { class: 'controls' },
              h('button', { class: 'btn small', onclick: () => { b._editing = false; act('block', { label, text: ta.value }, 'Saved. Vesper will keep yours.'); } }, 'Save'),
              blk.edited_by === 'J' ? h('button', { class: 'btn small', onclick: () => act('release', { label, text: ta.value }, 'Vesper will keep it up to date again.') }, 'Let Vesper update it') : null));
        });
        const q = h('input', { placeholder: 'Search what Vesper knows, e.g. Dana, overlay colours', 'aria-label': 'Search memory' });
        const add = h('input', { placeholder: 'Teach it something: e.g. My manager is Priya Shah', 'aria-label': 'Add a fact' });
        const list = results || d.recent || [];
        fill(b,
          h('div', { class: 'mem-grid' }, blocks),
          (d.learned || []).length ? h('div', { class: 'mem-learned' }, h('div', { class: 'sub-h' }, 'Learned lately'),
            d.learned.slice(0, 4).map(f => h('div', { class: 't2 mem-new' }, h('span', { class: 'mem-dot' }), f.statement))) : null,
          h('form', { class: 'addrow', onsubmit: async ev => { ev.preventDefault(); const r = await Vesper.act('memory', 'search', { query: q.value }); results = q.value.trim() ? r.facts : null; toast(`${(r.facts || []).length} found.`); render(); } },
            q, h('button', { class: 'btn small', type: 'submit' }, 'Search'), results ? h('button', { class: 'btn small', type: 'button', onclick: () => { results = null; render(); } }, 'Clear') : null),
          list.length ? h('div', { class: 'list mem-list' }, list.slice(0, 30).map(factRow)) : empty('Nothing learned yet. Talk to Vesper, or say “remember that …”.'),
          h('form', { class: 'addrow', style: { marginTop: '8px' }, onsubmit: async ev => { ev.preventDefault(); if (add.value.trim()) { await act('add', { statement: add.value }, 'Remembered.'); add.value = ''; } } },
            add, h('button', { class: 'btn small', type: 'submit' }, 'Remember')),
          e.on ? null : h('div', { class: 't3 muted' }, 'Tip: meaning search finds things even with different words. On the PC with Ollama run: ollama pull embeddinggemma'));
      }
    },
  });
  function render() { Vesper.repaint('memory'); }
})();
