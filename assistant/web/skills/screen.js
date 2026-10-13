/* Screen: the last "look at this" with its answer. The thumbnail lives in memory only and is dropped after two minutes. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast } = Vesper.ui;
  let expiry = null;
  async function look(question) {
    toast('Looking at your screen…');
    const r = await Vesper.act('screen', 'look', { question, whole_screen: true });
    if (!r.ok) toast(r.error);
  }
  function paint(d) {
    const last = d?.last;
    for (const p of panels('screen')) {
      const b = body(p);
      if (!b._view) {
        const input = h('input', { placeholder: 'Ask about your screen, e.g. what does this error mean?', 'aria-label': 'Question about your screen' });
        b._view = h('div', { class: 'scr-view' });
        fill(b, b._view, h('form', { class: 'addrow scr-ask', onsubmit: e => { e.preventDefault(); look(input.value.trim()); input.value = ''; } },
          input, h('button', { class: 'btn small', type: 'submit' }, 'Look')));
      }
      meta(p, last ? `${last.via === 'ocr' ? 'read the text' : 'looked'} · ${ago(last.at)}${d.live ? ' · live: asks only' : ''}` : (d?.live ? 'live: only when you ask' : 'nothing kept'));
      if (!last) { fill(b._view, empty('Say “Vesper, look at this”, “read this error” or “summarize this page”. Nothing is saved; password managers and banking are never captured.')); continue; }
      const key = `${last.at}`;
      const fresh = b._view.dataset.key !== key;
      b._view.dataset.key = key;
      fill(b._view,
        last.thumb ? h('figure', { class: `scr-shot ${fresh ? 'scr-new' : ''}` },
          h('img', { src: last.thumb, alt: `What Vesper looked at${last.app ? ` (${last.app})` : ''}`, class: 'scr-img' }),
          h('span', { class: 'scr-scan', 'aria-hidden': 'true' }),
          h('figcaption', { class: 't3 muted' }, `${last.whole_screen ? 'Screen' : last.app || 'Window'} · ${last.size?.[0] || '?'}×${last.size?.[1] || '?'} · gone in 2 min`)) : null,
        h('div', { class: 'scr-q t3 muted' }, `“${last.question}”`),
        h('div', { class: `scr-a ${fresh ? 'scr-new' : ''}` }, last.answer),
        h('div', { class: 'scr-foot' }, h('span', { class: 't3 muted' }, last.via === 'ocr' ? 'From the text on screen' : 'Seen by the local model'),
          h('button', { class: 'btn small', onclick: async () => { await Vesper.act('screen', 'forget'); } }, 'Forget')));
    }
    clearTimeout(expiry);
    if (last?.thumb) {
      const ms = Math.max(0, (last.at + (d.ttl || 120)) * 1000 - Date.now());
      expiry = setTimeout(() => { const x = Vesper.data.screen; if (x?.last?.thumb) { delete x.last.thumb; paint(x); } }, ms + 50);
    }
  }
  Vesper.skill('screen', { panels: [{ view: 'command', span: 4, title: 'Screen', after: 'automations' }], render: paint });
})();
