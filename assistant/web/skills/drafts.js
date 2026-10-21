/* Reply drafts: a "Draft reply" button on each owed reply (Replies panel), and this panel with the drafts Vesper
   saved in Gmail (never sent): who, subject, the text, Open in Gmail. Plus how J writes, per account. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast } = Vesper.ui;
  let open = null; // the draft whose full text is showing

  async function draft(x, btn) {
    btn.disabled = true; const label = btn.textContent; btn.textContent = 'Drafting…';
    btn.classList.add('dr-busy');
    const r = await Vesper.act('drafts', 'draft', { thread_id: x.thread_id, account: x.account, name: x.name });
    btn.disabled = false; btn.textContent = label; btn.classList.remove('dr-busy');
    if (r.ok === false) return toast(r.error || 'Couldn’t save a draft.', { level: 'warn', source: 'drafts' });
    open = null;
    toast(`Draft saved in your ${r.account} Gmail · not sent`, { level: 'good', source: 'drafts' });
    const p = document.querySelector('.panel[data-panel="drafts"]');
    if (p) p.scrollIntoView({ block: 'nearest', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  }

  // The hook skills/inbox_zero.js calls for each owed reply.
  Vesper.hooks = Vesper.hooks || {};
  (Vesper.hooks.owedItem = Vesper.hooks.owedItem || []).push(x => {
    const st = Vesper.data.drafts;
    if (!st || st.ok === false || !x.thread_id) return null;
    const btn = h('button', { class: 'btn small dr-btn', title: 'Write a reply the way you write, and save it in Gmail Drafts (never sent)' }, 'Draft reply');
    btn.onclick = ev => { ev.preventDefault(); ev.stopPropagation(); draft(x, btn); };
    return btn;
  });
  if (Vesper.data.inbox_zero !== undefined) Vesper.repaint('inbox_zero');

  function card(d) {
    const full = open === d.id;
    const lines = (d.body || '').split('\n');
    const short = lines.slice(0, 5).join('\n') + (lines.length > 5 ? '\n…' : '');
    return h('div', { class: 'dr-card' },
      h('div', { class: 'dr-head' },
        h('div', { class: 'main' },
          h('div', { class: 't1' }, d.to || d.to_email, ' ', h('span', { class: 'pill dr-acct' }, d.account),
            d.by === 'model' ? h('span', { class: 'pill dr-by', title: 'Written by the model in your style' }, 'your voice') :
              h('span', { class: 'pill', title: 'No model was free: your greeting and sign-off around a holding line' }, 'starter')),
          h('div', { class: 't2' }, d.subject, d.cc ? h('span', { class: 'muted' }, ` · cc ${d.cc}`) : null)),
        h('span', { class: 'muted mail-age' }, ago(d.ts))),
      d.flagged ? h('div', { class: 'dr-flag', role: 'note' }, d.flagged) : null,
      h('pre', { class: `dr-body${full ? ' open' : ''}` }, full ? d.body : short),
      (d.removed || []).length ? h('div', { class: 't3 muted' }, `Taken out: ${d.removed.join(', ')}`) : null,
      h('div', { class: 'controls dr-actions' },
        h('a', { class: 'btn small primary', href: d.link, target: '_blank', rel: 'noopener' }, 'Open in Gmail'),
        lines.length > 5 ? h('button', { class: 'btn small', 'aria-expanded': String(full), onclick: () => { open = full ? null : d.id; render(Vesper.data.drafts); } }, full ? 'Less' : 'Whole draft') : null,
        h('button', { class: 'btn small ghost', onclick: async () => { const r = await Vesper.act('drafts', 'hide', { id: d.id }); if (r.ok === false) toast(r.error); } }, 'Hide')));
  }

  let usable = null;
  function render(d) {
    const now = !!d && d.ok !== false;
    if (now !== usable) { usable = now; if (Vesper.data.inbox_zero !== undefined) Vesper.repaint('inbox_zero'); }  // show/hide the buttons
    for (const p of panels('drafts')) {
      const b = body(p);
      if (!d) { fill(b, empty('Loading…')); continue; }
      if (d.ok === false) { meta(p, ''); fill(b, empty(d.error || 'Gmail is not connected.')); continue; }
      const drafts = d.drafts || [];
      meta(p, drafts.length ? `${drafts.length} in Gmail · never sent` : 'never sent');
      fill(b,
        d.missing ? h('div', { class: 'dr-need', role: 'status' }, d.missing) : null,
        d.live ? empty('Live: drafts stay hidden until the stream ends.') :
          drafts.length ? h('div', { class: 'dr-list' }, drafts.map(card)) :
            empty('Click “Draft reply” on someone in Replies, or say “draft a reply to Dana saying Thursday works”.'),
        (d.accounts || []).some(a => a.style) ? h('div', { class: 'dr-style' },
          h('div', { class: 'sub-h' }, 'How you write'),
          d.accounts.filter(a => a.style).map(a => h('div', { class: 't3' }, h('b', {}, a.kind), ` · ${a.style}`, a.emails ? h('span', { class: 'muted' }, ` (from ${a.emails} sent emails)`) : null)),
          h('button', { class: 'btn small ghost', onclick: async () => { const r = await Vesper.act('drafts', 'restyle', {}); toast(r.ok === false ? r.error : 'Re-read your sent mail.'); } }, 'Re-learn')) : null);
    }
  }

  Vesper.skill('drafts', { panels: [{ view: 'work', span: 5, title: 'Reply drafts', after: 'inbox_zero' }], render });
})();
