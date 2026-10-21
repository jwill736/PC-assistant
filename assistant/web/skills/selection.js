/* Selected text: what Vesper did with the text you'd highlighted ("rewrite this", "explain this", "reply to this"),
   with Replace (for rewrites that waited for a yes) and Copy. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast, status } = Vesper.ui;
  const NAME = { rewrite: 'Rewritten', shorter: 'Shorter', fix: 'Fixed', formal: 'More professional', casual: 'More casual',
    translate: 'Translated', explain: 'Explained', summarize: 'Summary', reply: 'Reply draft' };
  const BUSY = { rewrite: 'Rewriting', shorter: 'Shortening', fix: 'Fixing', formal: 'Polishing', casual: 'Loosening up',
    translate: 'Translating', explain: 'Reading', summarize: 'Summarising', reply: 'Drafting a reply' };
  const SAY = ['rewrite this', 'make this shorter', 'fix this', 'translate this to Spanish', 'explain this', 'summarise this', 'reply to this'];
  let shown = null;

  async function act(action, payload, ok) {
    const r = await Vesper.act('selection', action, payload || {});
    toast(r.ok ? ok : r.error, { source: 'selection', level: r.ok ? 'good' : 'warn' });
  }

  function paint(d) {
    for (const p of panels('selection')) {
      const b = body(p);
      if (!d) { fill(b, empty('Loading…')); continue; }
      const r = d.result;
      meta(p, d.busy ? 'working' : r ? `${NAME[r.action] || r.action} · ${ago(r.at)}` : 'any app');
      if (!d.available) { fill(b, empty('Vesper can’t reach the keyboard or clipboard on this PC (Windows only).')); continue; }
      const fresh = r && r.at !== shown;
      if (r) shown = r.at;
      const state = !r ? null : r.replaced ? status('good', 'In place of your selection')
        : r.waiting ? status('warning', 'Waiting for your yes') : r.mode === 'replace' ? status('warning', 'Not put in') : null;
      fill(b,
        d.busy ? h('div', { class: 'sel-card sel-busy' }, h('span', { class: 'sel-shimmer', 'aria-hidden': 'true' }),
          h('span', { class: 't3 muted' }, `${BUSY[d.busy] || 'Working on'} your selection…`)) : null,
        r ? h('div', { class: `sel-card ${fresh ? 'sel-new' : ''}` },
          h('div', { class: 'sel-head' },
            h('span', { class: 't3 muted' }, `${NAME[r.action] || r.action}${r.language ? ` · ${r.language}` : ''}${r.app ? ` · from ${r.app}` : ''}`),
            state),
          h('details', { class: 'sel-src' }, h('summary', { class: 't3 muted' }, 'What you selected'), h('div', { class: 'sel-src-text' }, r.source)),
          h('div', { class: 'sel-text' }, r.text),
          h('div', { class: 'sel-actions' },
            r.mode === 'replace' && !r.replaced ? h('button', { class: 'btn small primary', onclick: () => act('replace', { id: r.id }, 'Replaced your selection.') }, 'Replace selection') : null,
            h('button', { class: `btn small ${r.mode === 'show' ? 'primary' : ''}`, onclick: () => act('copy', {}, 'Copied.') }, 'Copy'),
            h('button', { class: 'btn small', onclick: () => Vesper.act('selection', 'dismiss') }, 'Dismiss'))) : null,
        !r && !d.busy ? h('div', { class: 'sel-empty' },
          h('div', { class: 't2' }, 'Highlight text in any app, then say:'),
          h('div', { class: 'sel-say' }, SAY.map(s => h('span', { class: 'pill' }, `Vesper, ${s}`))),
          h('div', { class: 't3 muted' }, 'Rewrites go straight in place of your selection; long ones are read out and wait for your yes. Never in password fields.')) : null);
    }
  }
  Vesper.skill('selection', { panels: [{ view: 'pc', span: 6, title: 'Selected text', after: 'dictation' }], render: paint });
})();
