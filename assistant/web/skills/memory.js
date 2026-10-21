/* Memory: the core profile (edit it and Vesper leaves it), clashes to confirm, your rhythm, what Vesper learned,
   search, pin, forget. */
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

  // ---- clashes: "Is your manager Sam Lee or Priya Shah?" ------------------------------------------------
  function confirmCard(c) {
    const row = h('div', { class: 'mem-q' },
      h('div', { class: 'mem-q-text' }, c.question, c.found_by === 'model' ? h('span', { class: 'mem-q-by', title: 'Spotted by the AI model' }, 'AI') : null),
      h('div', { class: 'mem-q-opts' },
        h('button', { class: 'btn small', title: c.a.statement, onclick: () => answer(row, c, 'a') }, c.a.label),
        h('button', { class: 'btn small', title: c.b.statement, onclick: () => answer(row, c, 'b') }, c.b.label),
        h('button', { class: 'btn small ghost', title: 'Both are true', onclick: () => answer(row, c, 'both') }, 'Both'),
        h('button', { class: 'btn small ghost', title: 'Neither: stop using both', onclick: () => answer(row, c, 'neither') }, 'Neither'),
        h('button', { class: 'btn small ghost', title: "Don't ask again", onclick: () => answer(row, c, 'skip') }, 'Not sure')));
    return row;
  }
  async function answer(row, c, choice) {
    row.classList.add('mem-q-out');
    const r = await Vesper.act('memory', 'confirm', { id: c.id, choice });
    if (!r.ok) { row.classList.remove('mem-q-out'); return toast(r.error || 'That failed.'); }
    toast(r.kept ? `Got it: ${r.kept}.` : choice === 'both' ? 'Kept both.' : choice === 'neither' ? 'Both put aside.' : 'Okay, I won’t ask again.');
  }

  // ---- your rhythm --------------------------------------------------------------------------------------
  const mins = hm => { const [a, b] = String(hm).split(':').map(Number); return a * 60 + b; };
  const t12 = hm => { const m = mins(hm), hh = Math.floor(m / 60) % 24, mm = m % 60; return `${(hh % 12) || 12}${mm ? `:${String(mm).padStart(2, '0')}` : ''} ${hh < 12 ? 'AM' : 'PM'}`; };
  const pos = hm => (((mins(hm) - 300 + 1440) % 1440) / 1440) * 100;  // the strip runs 5 AM -> 5 AM
  function band(cls, from, to, label) {
    const a = pos(from); let b = pos(to); if (b <= a) b = 100;
    return h('span', { class: `rh-band ${cls}`, style: { left: `${a}%`, width: `${Math.max(1.2, b - a)}%` }, title: `${label}: ${t12(from)}–${t12(to)}` });
  }
  function strip(r) {
    const s = r.suggest || {};
    const bands = [];
    if (s.quiet_hours) { const [q0, q1] = s.quiet_hours.split('-'); bands.push(band('rh-sleep', q0, '05:00', 'Quiet'), band('rh-sleep', '05:00', q1, 'Quiet')); }
    if (r.work) bands.push(band('rh-work', r.work.start, r.work.end, 'Work'));
    if (r.lunch) { const end = mins(r.lunch.start) + r.lunch.minutes; bands.push(band('rh-lunch', r.lunch.start, `${Math.floor(end / 60) % 24}:${String(end % 60).padStart(2, '0')}`, 'Lunch')); }
    if (r.stream) bands.push(band('rh-stream', r.stream.start, r.stream.end, 'Stream'));
    const ticks = ['05:00', '09:00', '13:00', '17:00', '21:00', '01:00'].map(t => h('span', { class: 'rh-tick', style: { left: `${pos(t)}%` } }, t12(t).replace(':00', '')));
    return h('div', { class: 'rh-strip', role: 'img', 'aria-label': 'Your usual day, 5 AM to 5 AM' }, h('div', { class: 'rh-track' }, bands), h('div', { class: 'rh-ticks' }, ticks));
  }
  function rhythmCard(r) {
    if (!r) return null;
    const head = h('div', { class: 'sub-h' }, 'Your rhythm',
      h('button', { class: 'btn small ghost rh-relearn', onclick: () => act('rhythm', {}, 'Re-learned from your activity.') }, 'Re-learn'));
    if (!r.ok) return h('div', { class: 'mem-rhythm' }, head, h('div', { class: 't3 muted' }, r.error || 'Not enough activity yet.'));
    const rows = [
      r.work && ['Work', `${t12(r.work.start)} – ${t12(r.work.end)}`, `${r.work.days} days`],
      r.lunch && ['Lunch', `~${t12(r.lunch.start)}, ${r.lunch.minutes} min`, `${r.lunch.days} days`],
      r.stream && ['Streams', `${r.stream.days.join(' · ')}, ${t12(r.stream.start)} – ${t12(r.stream.end)}`, `${r.stream.nights} nights`],
      r.bedtime && ['Off the PC', `~${t12(r.bedtime.time)}`, `${r.bedtime.days} days`],
      r.stretch_minutes && ['Before a break', `about ${r.stretch_minutes} min`, 'usually'],
    ].filter(Boolean);
    const s = r.suggest || {};
    const plan = [s.quiet_hours && `quiet hours ${s.quiet_hours.split('-').map(t12).join('–')}`,
      s.break_after_minutes && `a break nudge after ${s.break_after_minutes} min`,
      s.work_hours && `work hours ${t12(s.work_hours.start)}–${t12(s.work_hours.end)} for the day plan`].filter(Boolean);
    const applied = r.applied && r.applied.at;
    return h('div', { class: 'mem-rhythm' }, head, strip(r),
      h('dl', { class: 'rh-rows' }, rows.map(([k, v, n]) => [h('dt', {}, k), h('dd', {}, v, h('span', { class: 'muted' }, ` · ${n}`))])),
      h('div', { class: 't3 muted' }, `Learned from ${r.days} days of activity${r.since ? ` since ${new Date(`${r.since}T12:00`).toLocaleDateString([], { month: 'short', day: 'numeric' })}` : ''}.`),
      plan.length ? h('div', { class: 'rh-use' },
        h('div', { class: 't2' }, applied ? `In use since ${ago(r.applied.at)}: ` : 'Use these for ', plan.join(', '), '.'),
        applied ? h('button', { class: 'btn small ghost', onclick: () => act('undo_rhythm', {}, 'Back to your previous settings.') }, 'Undo')
          : h('button', { class: 'btn small primary', onclick: () => act('use_rhythm', {}, 'Done: quiet hours, breaks and work hours now follow your rhythm.') }, 'Use these')) : null);
  }

  Vesper.skill('memory', {
    panels: [{ view: 'setup', span: 12, title: 'Memory · what Vesper knows about you', after: 'google', buttons: [
      { label: 'Tidy now', onclick: async () => { const r = await Vesper.act('memory', 'cleanup', {}); toast(r.ok ? `Tidied: ${r.merged} merged, ${r.open} to confirm, ${r.expired} retired.` : (r.error || 'That failed.')); } }] }],
    render(d) {
      for (const p of panels('memory')) {
        const b = body(p);
        if (!d) { fill(b, empty('Loading…')); continue; }
        const e = d.embeddings || {};
        const tidy = d.cleanup && d.cleanup.at ? ` · tidied ${ago(d.cleanup.at)}` : '';
        meta(p, `${d.facts} things known · ${e.on ? `meaning search on (${e.model})` : 'word search'}${tidy}`);
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
        const qs = d.confirm || [];
        fill(b,
          qs.length ? h('div', { class: 'mem-confirm' }, h('div', { class: 'sub-h' }, `Confirm · ${qs.length} thing${qs.length > 1 ? 's' : ''} I'm not sure about`),
            qs.slice(0, 5).map(confirmCard)) : null,
          h('div', { class: 'mem-grid' }, blocks),
          rhythmCard(d.rhythm),
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
