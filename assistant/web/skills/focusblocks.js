/* Focus blocks: "2 hours tomorrow for the Q3 deck" → Vesper finds the time and asks before it goes on the calendar;
   "Put today's plan on my calendar" after a Re-plan; and the 🔒 blocks coming up. */
(() => {
  const { h, fill, empty, meta, panels, body, toast } = Vesper.ui;
  const tm = iso => new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const dayOf = iso => {
    const d = new Date(iso), t = new Date(); const tom = new Date(); tom.setDate(t.getDate() + 1);
    return d.toDateString() === t.toDateString() ? 'Today' : d.toDateString() === tom.toDateString() ? 'Tomorrow'
      : d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
  };

  async function ask(action, payload, btn) {
    if (btn) btn.disabled = true;
    const r = await Vesper.act('focusblocks', action, payload);
    if (btn) btn.disabled = false;
    if (r.ok === false) return toast(r.error || 'That didn’t work.', { level: 'warn', source: 'focusblocks' });
    toast(r.pending ? `${r.reply.replace(/ Say yes or cancel\.$/, '')} Click Confirm or say yes.` : r.reply);
    return r;
  }

  function render(d) {
    for (const p of panels('focusblocks')) {
      const b = body(p);
      if (!d) { fill(b, empty('Loading…')); continue; }
      const made = d.made || [];
      meta(p, made.length ? `${made.length} coming up` : '');
      if (d.ok === false) { fill(b, empty(d.error || 'Connect Google to block time.')); b._form = null; continue; }
      if (!b._form) {  // built once: what J is typing survives updates
        const input = h('input', { placeholder: '2 hours tomorrow for the Q3 deck', 'aria-label': 'What time to protect', maxlength: 160 });
        const btn = h('button', { class: 'btn small primary', type: 'submit' }, 'Find time');
        b._form = h('form', { class: 'addrow fb-form', onsubmit: async ev => {
          ev.preventDefault(); if (!input.value.trim()) return input.focus();
          if (await ask('propose', { text: input.value }, btn)) input.value = '';
        } }, h('span', { class: 'fb-lock', 'aria-hidden': 'true' }, '🔒'), input, btn);
        b._view = h('div', {});
        fill(b, b._form, b._view);
      }
      fill(b._view,
        d.missing ? h('div', { class: 'fb-need', role: 'status' }, d.missing) : null,
        d.plan_blocks ? h('div', { class: 'fb-offer' },
          h('div', { class: 't2' }, `Today’s plan has ${d.plan_blocks} task block${d.plan_blocks === 1 ? '' : 's'} left.`),
          h('button', { class: 'btn small', onclick: ev => ask('plan', {}, ev.currentTarget) }, 'Put these blocks on my calendar')) : null,
        d.live ? empty('Live: your blocks stay hidden until the stream ends.') :
        made.length ? h('div', { class: 'list fb-list' }, made.map(m => h('a', { class: 'item fb-item', href: m.link || undefined, target: '_blank', rel: 'noopener' },
          h('div', { class: 'main' }, h('div', { class: 't1' }, m.title),
            h('div', { class: 't2' }, `${dayOf(m.start)} · ${tm(m.start)}–${tm(m.end)} · ${m.account}`))))) :
          empty('Say “protect 2 hours tomorrow for the Q3 deck” or “block my afternoon for deep work”. I’ll show the exact time first.'));
    }
  }

  Vesper.skill('focusblocks', { panels: [{ view: 'command', span: 4, title: 'Focus blocks', after: 'planner' }], render });
})();
