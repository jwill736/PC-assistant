/* Updates: what Vesper brought up on its own, why it's holding the rest, and 👍 / ✕ so it learns. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast } = Vesper.ui;
  const SRC = { calendar: 'Calendar', break: 'Break', tasks: 'Tasks', welcome: 'Welcome back', weather: 'Weather', inbox_zero: 'Email', planner: 'Plan', habits: 'Habits', journal: 'Journal', bills: 'Money', travel: 'Travel', stream_copilot: 'Stream', spotify: 'Music', home: 'Home', phone: 'Phone', memory: 'Memory', screen: 'Screen', clipboard: 'Clipboard', rewind: 'Rewind', discord: 'Discord', youtube: 'YouTube',
    automations: 'Automation', focus: 'Focus', people: 'People', meetings: 'Meetings', janitor: 'PC', pulse: 'Pulse' };
  async function fb(action, key) {
    const r = await Vesper.act('nudges', action, { key });
    if (!r.ok) return toast(r.error);
    if (r.quiet) toast(`OK: ${SRC[r.source] || r.source} updates will stay on the HUD, no speaking.`);
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
          (d.feed || []).length ? h('div', { class: 'list nudge-feed' }, d.feed.slice(0, 8).map(n => h('div', { class: `item nudge nudge-${n.priority}` },
            h('div', { class: 'main' },
              h('div', { class: 't3 muted' }, `${SRC[n.source] || n.source} · ${ago(n.delivered)}${n.spoken ? ' · said aloud' : ''}`),
              h('div', { class: 't1' }, n.text)),
            h('div', { class: 'nudge-fb' },
              h('button', { class: 'btn small icon', title: 'Useful', 'aria-label': 'Useful', onclick: () => fb('useful', n.key) }, '👍'),
              h('button', { class: 'btn small icon', title: 'Not useful: show less like this', 'aria-label': 'Not useful', onclick: () => fb('dismiss', n.key) }, '✕'))))) :
            empty('Nothing yet. Meetings, emails that need you, breaks and reminders show up here, at moments that suit you.'));
      }
    },
  });
})();
