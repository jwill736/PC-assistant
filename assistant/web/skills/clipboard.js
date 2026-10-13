/* Clipboard: what you copied (secrets never kept), one-click AI actions, and the result ready to copy back. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast } = Vesper.ui;
  const ACTS = [['summarize', 'Summarize'], ['fix grammar', 'Fix'], ['rewrite', 'Rewrite'], ['bullets', 'Bullets'], ['explain', 'Explain']];
  const NAME = { summarize: 'Summary', 'fix grammar': 'Fixed', rewrite: 'Rewritten', bullets: 'Bullets', explain: 'Explained', translate: 'Translated' };
  let busy = null, translating = null, shown = null;
  async function run(id, action, language) {
    busy = id; paint(Vesper.data.clipboard);
    const r = await Vesper.act('clipboard', 'act', { id, action, language });
    busy = null; translating = null; paint(Vesper.data.clipboard);
    if (!r.ok) toast(r.error);
  }
  async function copy(payload, what) {
    const r = await Vesper.act('clipboard', 'copy', payload);
    toast(r.ok ? `${what} copied.` : r.error);
  }
  function chips(item) {
    if (translating === item.id) {
      const lang = h('input', { placeholder: 'Language, e.g. Spanish', 'aria-label': 'Translate to', value: 'Spanish' });
      setTimeout(() => lang.select(), 0);
      return h('form', { class: 'cb-chips', onsubmit: e => { e.preventDefault(); run(item.id, 'translate', lang.value.trim() || 'Spanish'); } },
        lang, h('button', { class: 'btn small', type: 'submit' }, 'Translate'),
        h('button', { class: 'btn small', type: 'button', onclick: () => { translating = null; paint(Vesper.data.clipboard); } }, 'Cancel'));
    }
    return h('div', { class: 'cb-chips' },
      h('button', { class: 'btn small', onclick: () => copy({ id: item.id }, 'Text') }, 'Copy'),
      ACTS.map(([a, label]) => h('button', { class: 'btn small', disabled: !!busy, onclick: () => run(item.id, a) }, label)),
      h('button', { class: 'btn small', disabled: !!busy, onclick: () => { translating = item.id; paint(Vesper.data.clipboard); } }, 'Translate…'),
      h('button', { class: 'btn small icon', title: 'Forget this one', 'aria-label': 'Forget this one', onclick: () => Vesper.act('clipboard', 'forget', { id: item.id }) }, '✕'));
  }
  function paint(d) {
    for (const p of panels('clipboard')) {
      const b = body(p);
      if (!d) { fill(b, empty('Loading…')); continue; }
      const items = d.items || [];
      meta(p, `${items.length} kept${d.skipped ? ` · ${d.skipped} secret${d.skipped === 1 ? '' : 's'} skipped` : ''}${d.persist ? ' · saved' : ' · memory only'}`);
      if (!d.available) { fill(b, empty('Vesper can’t reach the clipboard on this PC.')); continue; }
      const res = d.result, fresh = res && res.at !== shown;
      if (res) shown = res.at;
      fill(b,
        busy ? h('div', { class: 'cb-result cb-busy' }, h('span', { class: 'cb-shimmer' }), h('span', { class: 't3 muted' }, 'Working on it…')) :
          res ? h('div', { class: `cb-result ${fresh ? 'cb-new' : ''}` },
            h('div', { class: 'cb-head' }, h('span', { class: 't3 muted' }, `${NAME[res.action] || res.action}${res.language ? ` · ${res.language}` : ''} · ${ago(res.at)}`),
              h('button', { class: 'btn small primary', onclick: () => copy({ text: res.text }, 'Result') }, 'Copy result')),
            h('div', { class: 'cb-text' }, res.text)) : null,
        items.length ? h('div', { class: 'list cb-list' }, items.slice(0, 20).map(i => h('div', { class: `item cb-item ${busy === i.id ? 'cb-working' : ''}`, tabindex: 0 },
          h('div', { class: 'main' },
            h('div', { class: 'cb-clip' }, i.text),
            h('div', { class: 't2' }, `${ago(i.at)} · ${i.chars.toLocaleString()} chars${i.from === 'vesper' ? ' · from Vesper' : ''}`),
            chips(i))))) :
          empty('Copy some text and it shows up here. Then say “Vesper, summarize what I copied” or “translate my clipboard to Spanish”.'),
        items.length ? h('div', { class: 'cb-foot' }, h('button', { class: 'btn small', onclick: async () => { const r = await Vesper.act('clipboard', 'clear'); toast(r.ok ? 'Clipboard history cleared.' : r.error); } }, 'Clear history')) : null);
    }
  }
  Vesper.skill('clipboard', { panels: [{ view: 'pc', span: 12, title: 'Clipboard', after: 'rewind' }], render: paint });
})();
