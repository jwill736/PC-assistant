/* Replies: who J owes a reply, who owes him one, and what he promised (Add as task / Dismiss). */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast } = Vesper.ui;
  const TABS = [['owed', 'Owed'], ['waiting', 'Waiting on'], ['promises', 'Promised']];
  let tab = 'owed';
  try { tab = localStorage.getItem('iz-tab') || 'owed'; } catch { /* private window */ }

  const age = ts => ago(ts).replace(' ago', '');
  const acct = a => h('span', { class: 'pill iz-acct' }, a === 'personal' ? 'personal' : 'work');
  const due = iso => {
    if (!iso) return null;
    const d = new Date(`${iso}T23:59:59`);
    const late = d < new Date();
    return h('span', { class: `pill iz-due ${late ? 'late' : ''}` },
      `${late ? 'overdue' : 'due'} ${d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })}`);
  };

  function owedItem(x) {
    return h('a', { class: 'item mail iz-item', href: x.link, target: '_blank', rel: 'noopener' },
      h('div', { class: 'main' },
        h('div', { class: 't1' }, x.important ? h('span', { class: 'mail-flag', title: 'Gmail marked it important' }, '◆ ') : null, x.name, ' ', acct(x.account)),
        h('div', { class: 't2' }, x.subject),
        x.snippet ? h('div', { class: 't3 muted' }, x.snippet) : null),
      h('span', { class: 'muted mail-age', title: 'waiting on you' }, age(x.ts)));
  }
  function waitingItem(x) {
    return h('a', { class: 'item mail iz-item', href: x.link, target: '_blank', rel: 'noopener' },
      h('div', { class: 'main' },
        h('div', { class: 't1' }, x.name, x.others ? h('span', { class: 'muted' }, ` +${x.others}`) : null, ' ', acct(x.account)),
        h('div', { class: 't2' }, x.subject)),
      h('span', { class: 'iz-days', title: 'since you wrote' }, `${x.days}d`));
  }
  function promiseItem(p) {
    if (p.confirm) return keptItem(p);
    const row = h('div', { class: 'item iz-item' },
      h('div', { class: 'main' },
        h('div', { class: 't1' }, p.task, ' ', due(p.due)),
        h('div', { class: 't2' }, `to ${p.person || 'someone'} · `, h('a', { href: p.link, target: '_blank', rel: 'noopener', class: 'iz-link' }, p.subject), ` · ${ago(p.ts)}`),
        p.quote ? h('div', { class: 't3 muted iz-quote' }, `“${p.quote}”`) : null),
      h('div', { class: 'iz-btns' },
        h('button', { class: 'btn small', onclick: () => decide(row, 'add_task', p.id, 'Added to your tasks.') }, 'Add as task'),
        h('button', { class: 'btn small ghost', 'aria-label': 'Dismiss', onclick: () => decide(row, 'dismiss', p.id) }, 'Dismiss')));
    return row;
  }
  /* J's later email looks like he kept the promise: ask, never tick it off alone. */
  function keptItem(p) {
    const row = h('div', { class: 'item iz-item iz-kept' },
      h('div', { class: 'main' },
        h('div', { class: 'iz-kept-tag' }, 'looks done'),
        h('div', { class: 't1' }, p.question || p.task),
        h('div', { class: 't2' }, `to ${p.person || 'someone'} · `, h('a', { href: p.link, target: '_blank', rel: 'noopener', class: 'iz-link' }, p.subject),
          p.state === 'added' ? ' · on your tasks' : ''),
        p.confirm_quote ? h('div', { class: 't3 muted iz-quote' }, `You wrote: “${p.confirm_quote}”`) : null),
      h('div', { class: 'iz-btns' },
        h('button', { class: 'btn small iz-yes', 'aria-label': 'Yes, it is done', onclick: () => decide(row, 'kept_yes', p.id, p.state === 'added' ? 'Done, and ticked off your tasks.' : 'Marked done.') }, '✓ Done'),
        h('button', { class: 'btn small ghost', 'aria-label': 'Not yet', onclick: () => decide(row, 'kept_no', p.id, 'Okay, still open.') }, '✕ Not yet')));
    return row;
  }
  function closedList(d, kind) {
    const items = (d.closed || []).filter(c => c.kind === kind);
    if (!items.length) return null;
    return h('div', { class: 'iz-closed' }, h('div', { class: 'sub-h' }, 'Closed by themselves'),
      items.slice(0, 4).map(c => h('div', { class: 'iz-closed-row t3' }, h('span', { class: 'iz-tick', 'aria-hidden': 'true' }, '✓'),
        h('span', {}, c.text), h('span', { class: 'muted' }, ` · ${ago(c.ts)}`))));
  }
  async function decide(row, action, id, ok) {
    row.classList.add('iz-leaving');
    const r = await Vesper.act('inbox_zero', action, { id });
    if (!r.ok) { row.classList.remove('iz-leaving'); return toast(r.error); }
    if (ok) toast(ok);
  }

  function render(d) {
    for (const p of panels('inbox_zero')) {
      const b = body(p);
      if (!d) { fill(b, empty('Loading…')); continue; }
      if (d.ok === false) { meta(p, ''); fill(b, empty(d.error || 'Gmail is not connected.')); continue; }
      const lists = { owed: d.owed || [], waiting: d.waiting || [], promises: d.promises || [] };
      meta(p, d.loading ? 'reading your mail…' : `${lists.owed.length} owed · ${lists.waiting.length} waiting · ${ago(d.at)}`);
      const seg = h('span', { class: 'seg iz-tabs', role: 'tablist' }, TABS.map(([k, label]) => h('button', {
        role: 'tab', 'aria-pressed': String(tab === k), 'aria-selected': String(tab === k),
        onclick: () => { tab = k; try { localStorage.setItem('iz-tab', k); } catch { /* fine */ } render(Vesper.data.inbox_zero); },
      }, label, h('span', { class: 'iz-n' }, String(lists[k].length)))));
      const items = lists[tab];
      const none = { owed: 'Nobody is waiting on a reply from you.', waiting: 'Nobody owes you a reply.',
        promises: 'No open promises. When you write “I’ll send it by Friday”, it shows up here.' }[tab];
      const make = { owed: owedItem, waiting: waitingItem, promises: promiseItem }[tab];
      fill(b, seg,
        items.length ? h('div', { class: 'list iz-list' }, items.slice(0, 12).map(make)) : empty(d.loading ? 'Reading your mail…' : none),
        tab === 'promises' ? null : closedList(d, tab === 'waiting' ? 'replied' : 'answered'),
        (d.missing || []).length ? h('div', { class: 't3 muted', style: { marginTop: '8px' } },
          `Gmail isn't allowed yet for ${d.missing.join(', ')}: Setup → Google accounts → Reconnect, and tick Gmail.`) : null);
    }
  }

  Vesper.skill('inbox_zero', {
    panels: [{ view: 'work', span: 7, title: 'Replies · owed, waiting, promised', after: 'inbox', buttons: [
      { label: 'Refresh', onclick: async () => { const r = await Vesper.act('inbox_zero', 'refresh', {}); if (r.ok === false) toast(r.error); } }] }],
    render,
  });
})();
