/* Money: what your subscriptions cost a month, what's charged in the next two weeks, and free trials about to end.
   Every row opens the email it came from. Amounts blur while you're live. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast } = Vesper.ui;
  const SIGN = { USD: '$', EUR: '€', GBP: '£' };
  const CAD = { monthly: '/mo', yearly: '/yr', weekly: '/wk' };
  const KIND = { renewal: 'renews', due: 'bill due', trial: 'trial ends' };
  const money = (a, c = 'USD') => a == null ? '—' :
    `${SIGN[c] || `${c} `}${Number(a).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  const local = iso => new Date(`${iso}T12:00:00`);
  let showAll = false;

  const amt = (a, c, extra = '') => h('span', { class: `mn-amt num ${extra}` }, money(a, c));
  function tile(iso) {
    const d = local(iso);
    return h('div', { class: 'mn-date', 'aria-hidden': 'true' },
      h('span', { class: 'mn-dow' }, d.toLocaleDateString([], { weekday: 'short' })), h('span', { class: 'mn-dom num' }, d.getDate()));
  }
  const link = (href, cls, kids, i) => h('a', { class: cls, href: href || null, target: '_blank', rel: 'noopener', style: `--i:${i}` }, kids);

  function upcoming(list) {
    if (!list.length) return empty('Nothing charged in the next two weeks.');
    return h('div', { class: 'list mn-list' }, list.map((u, i) => link(u.link, `item mn-row mn-r-${u.kind}`, [
      tile(u.date),
      h('div', { class: 'main' }, h('div', { class: 't1' }, u.merchant),
        h('div', { class: 't2' }, h('span', { class: `pill mn-kind mn-k-${u.kind}` }, KIND[u.kind] || u.kind), ` ${u.when}`)),
      u.amount != null ? amt(u.amount, u.currency) : null], i)));
  }

  function trials(list) {
    if (!list.length) return null;
    return h('div', { class: 'mn-trials' }, list.map((t, i) => link(t.link, 'mn-trial', [
      h('span', { class: 'mn-beacon', 'aria-hidden': 'true' }),
      h('div', { class: 'main' }, h('div', { class: 't1' }, `${t.merchant} trial ends ${t.when}`),
        h('div', { class: 't2' }, t.then != null ? ['then ', amt(t.then, t.currency, 'inline'), ' unless you cancel'] : 'cancel before then if you don’t want it')),
      h('span', { class: 'mn-left num' }, t.days === 0 ? 'today' : `${t.days}d`)], i)));
  }

  function subs(list) {
    if (!list.length) return empty('No subscriptions found in your receipts yet.');
    const shown = showAll ? list : list.slice(0, 6);
    return [h('div', { class: 'list mn-list' }, shown.map((s, i) => h('div', { class: `item mn-row mn-sub ${s.lapsed ? 'mn-lapsed' : ''}`, style: `--i:${i}` },
      h('div', { class: 'main' },
        h('a', { class: 't1', href: s.link || null, target: '_blank', rel: 'noopener' }, s.merchant),
        h('div', { class: 't2' }, s.lapsed ? h('span', { class: 'pill' }, 'no recent receipt') :
          `next ${local(s.next).toLocaleDateString([], { month: 'short', day: 'numeric' })}`, s.count > 1 ? ` · ${s.count} receipts` : '')),
      h('div', { class: 'mn-price' }, amt(s.amount, s.currency), h('span', { class: 'mn-cad' }, CAD[s.cadence] || '')),
      h('button', { class: 'btn small icon mn-hide', title: 'Not a subscription: hide it', 'aria-label': `Hide ${s.merchant}`, onclick: async () => {
        const r = await Vesper.act('bills', 'hide', { merchant: s.merchant }); toast(r.ok ? `${s.merchant} hidden.` : r.error);
      } }, '✕')))),
    list.length > 6 ? h('button', { class: 'btn ghost small mn-more', onclick: () => { showAll = !showAll; paint(Vesper.data.bills); } },
      showAll ? 'Show fewer' : `Show all ${list.length}`) : null];
  }

  function paint(d) {
    for (const p of panels('bills')) {
      const b = body(p);
      if (!d) { fill(b, empty('Loading…')); continue; }
      if (!d.ok) { fill(b, empty(d.error || 'No receipts read yet.')); meta(p, ''); continue; }
      const active = d.subscriptions.filter(s => !s.lapsed);
      const others = Object.entries(d.monthly || {}).filter(([c]) => c !== d.currency);
      meta(p, d.live ? 'amounts hidden while live' : d.scanned_at ? `receipts read ${ago(d.scanned_at)}` : 'reading receipts…');
      b.classList.toggle('mn-private', !!d.live);
      fill(b,
        d.warning ? h('div', { class: 't3 mn-warn' }, d.warning) : null,
        h('div', { class: 'mn-top' },
          h('div', { class: 'stat hero mn-total' }, h('div', { class: 'label' }, 'Subscriptions a month'),
            h('div', { class: 'value' }, amt(d.monthly_total, d.currency)),
            h('div', { class: 'sub' }, `${active.length} active · about `, amt(d.monthly_total * 12, d.currency, 'inline'), ' a year',
              others.map(([c, v]) => [' · + ', amt(v, c, 'inline')]))),
          trials(d.trials)),
        h('div', { class: 'mn-cols' },
          h('section', {}, h('div', { class: 'sub-h' }, `Next ${d.days || 14} days`), upcoming(d.upcoming || [])),
          h('section', {}, h('div', { class: 'sub-h' }, 'Subscriptions'), subs(d.subscriptions || []))));
    }
  }

  Vesper.skill('bills', {
    panels: [{ view: 'work', span: 12, title: 'Money · bills & subscriptions', after: 'jobs', buttons: [
      { label: 'Read receipts', onclick: async () => { const r = await Vesper.act('bills', 'scan', {}); toast(r.ok ? `Read ${r.read || 0} new email${r.read === 1 ? '' : 's'}.` : r.error); } }] }],
    render: paint,
  });
})();
