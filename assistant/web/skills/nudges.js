/* Updates: what Vesper brought up on its own, why (tap "why"), why it's holding the rest, and 👍 / ✕ so it learns. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast } = Vesper.ui;
  const SRC = { calendar: 'Calendar', break: 'Break', tasks: 'Tasks', welcome: 'Welcome back', weather: 'Weather', inbox_zero: 'Email', planner: 'Plan', habits: 'Habits', journal: 'Journal', bills: 'Money', travel: 'Travel', stream_copilot: 'Stream', spotify: 'Music', home: 'Home', phone: 'Phone', memory: 'Memory', screen: 'Screen', clipboard: 'Clipboard', rewind: 'Rewind', discord: 'Discord', youtube: 'YouTube',
    automations: 'Automation', focus: 'Focus', people: 'People', meetings: 'Meetings', janitor: 'PC', pulse: 'Pulse', ambient: 'Look-around' };
  const open = new Set();  // which "why" boxes are unfolded (kept across the 15 s repaints)
  async function fb(action, key) {
    const r = await Vesper.act('nudges', action, { key });
    if (!r.ok) return toast(r.error);
    if (r.quiet) toast(`OK: ${SRC[r.source] || r.source} updates will stay on the HUD, no speaking.`);
  }
  const whyText = w => (w ? [w.rule, ...(w.evidence || []), w.note].filter(Boolean).join('\n') : '');
  function whyBox(w) {
    return h('div', { class: 'nudge-why' },
      h('div', { class: 'why-rule' }, w.rule),
      (w.evidence || []).length ? h('ul', { class: 'why-ev' }, w.evidence.map(e => h('li', {}, e))) : null,
      w.note ? h('div', { class: 'why-note' }, w.note) : null);
  }
  function item(n) {
    const w = n.why;
    const box = w ? whyBox(w) : null;
    if (box) box.hidden = !open.has(n.key);
    const toggle = ev => {  // in place: no repaint, so nothing else moves
      const btn = ev.currentTarget;
      box.hidden = !box.hidden;
      if (box.hidden) open.delete(n.key); else open.add(n.key);
      btn.classList.toggle('on', !box.hidden);
      btn.setAttribute('aria-expanded', String(!box.hidden));
    };
    return h('div', { class: `item nudge nudge-${n.priority}` },
      h('div', { class: 'main' },
        h('div', { class: 't3 muted' }, `${SRC[n.source] || n.source} · ${ago(n.delivered)}${n.spoken ? ' · said aloud' : ''}`),
        h('div', { class: 't1', title: whyText(w) }, n.text),
        box),
      h('div', { class: 'nudge-fb' },
        w ? h('button', { class: `btn small why-btn${open.has(n.key) ? ' on' : ''}`, title: 'Why did Vesper say this?',
          'aria-expanded': String(open.has(n.key)), onclick: toggle }, 'why') : null,
        h('button', { class: 'btn small icon', title: 'Useful', 'aria-label': 'Useful', onclick: () => fb('useful', n.key) }, '👍'),
        h('button', { class: 'btn small icon', title: 'Not useful: show less like this', 'aria-label': 'Not useful', onclick: () => fb('dismiss', n.key) }, '✕')));
  }
  function ambientLine(a) {
    if (!a) return null;
    const last = a.last || {};
    const said = (last.kept || []).length;
    const what = last.error ? 'hit a snag' : last.note && !said ? last.note.replace(/\.$/, '').toLowerCase() : said ? `${said} worth saying` : null;
    return h('div', { class: 'nudge-ambient t3' },
      h('span', { class: 'amb-dot', 'aria-hidden': 'true' }),
      h('span', { class: 'muted' }, a.enabled === false ? 'Look-around is off' :
        `Look-around every ${a.every_minutes} min${a.last_run ? ` · ran ${ago(a.last_run)}${what ? `: ${what}` : ''}` : ' · not yet'}${a.waiting && a.waiting !== 'resting' ? ` · ${a.waiting}` : ''}`),
      a.enabled === false ? null : h('button', { class: 'btn small ghost', onclick: async () => {
        const r = await Vesper.act('ambient', 'run', {});
        toast(r.ok === false ? r.error : (r.kept || []).length ? `Look-around: ${r.kept.length} update${r.kept.length > 1 ? 's' : ''}.` : 'Look-around: nothing worth saying.');
      } }, 'Look now'));
  }
  Vesper.skill('nudges', {
    panels: [{ view: 'command', span: 4, title: 'Updates', after: 'agenda', buttons: [
      { label: 'Quiet 1h', onclick: async () => { const r = await Vesper.act('nudges', 'mute', { minutes: 60 }); toast(r.ok ? 'Quiet for an hour.' : r.error); } }] }],
    render(d) {
      for (const p of panels('nudges')) {
        const b = body(p);
        if (!d) { fill(b, empty('Loading…')); continue; }
        const g = d.gate || {};
        meta(p, g.open ? `listening for moments · ${g.spoken_last_hour}/${g.budget} spoken this hour` : `holding: ${g.why}`);
        fill(b,
          (d.queue || []).length ? h('div', { class: 'nudge-held t3' }, h('span', { class: 'nudge-pulse' }),
            `${d.queue.length} waiting for a better moment${g.why ? ` (${g.why})` : ''}`) : null,
          (d.feed || []).length ? h('div', { class: 'list nudge-feed' }, d.feed.slice(0, 8).map(item)) :
            empty('Nothing yet. Meetings, emails that need you, breaks and reminders show up here, at moments that suit you.'),
          ambientLine(d.ambient));
      }
    },
  });
})();
