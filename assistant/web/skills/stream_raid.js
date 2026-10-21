/* Raid planner: who's live in your category, ranked (size, language, friends, past raids), with a Raid button that
   needs a second tap (a raid is tier 2: that tap is your yes). Ten minutes before the stream block ends, a card. */
(() => {
  const { h, fill, empty, meta, panels, body, toast, ago } = Vesper.ui;
  async function act(action, payload, ok) {
    const r = await Vesper.act('stream_raid', action, payload);
    if (r.ok === false) toast(r.error || 'That didn’t work.', { level: 'warn', source: 'stream' }); else if (ok) toast(typeof ok === 'function' ? ok(r) : ok, { level: 'good', source: 'stream' });
    return r;
  }
  // First tap arms it ("Confirm raid"), a second tap within 5 s sends the raid.
  function raidBtn(c) {
    const b = h('button', { class: 'btn small', 'aria-label': `Raid ${c.name}` }, 'Raid');
    b.addEventListener('click', async () => {
      if (!b.classList.contains('armed')) {
        b.classList.add('armed', 'primary'); b.textContent = 'Confirm raid';
        b._t = setTimeout(() => { b.classList.remove('armed', 'primary'); b.textContent = 'Raid'; }, 5000);
        return;
      }
      clearTimeout(b._t); b.disabled = true;
      await act('raid', { login: c.login }, `Raiding ${c.name}: Twitch counts down 90 seconds.`);
      b.disabled = false; b.classList.remove('armed', 'primary'); b.textContent = 'Raid';
    });
    return b;
  }
  function row(c, compact) {
    return h('div', { class: `item sr-item${c.recent ? ' recent' : ''}` },
      compact ? null : h('button', { class: `sr-star${c.friend ? ' on' : ''}`, title: c.friend ? 'Friend (click to remove)' : 'Mark as a friend',
        'aria-pressed': String(!!c.friend), 'aria-label': `Friend: ${c.name}`, onclick: () => act('friend', { login: c.login, on: !c.friend }) }, c.friend ? '★' : '☆'),
      h('div', { class: 'main' },
        h('div', { class: 't1' }, c.name, h('span', { class: 'num sr-v' }, ` ${c.viewers}`), h('span', { class: 't3 muted' }, ' watching')),
        h('div', { class: 't3 muted sr-why' }, [c.why.join(' · '), compact ? '' : c.title].filter(Boolean).join(' — '))),
      raidBtn(c));
  }
  const cardList = c => h('div', { class: 'list sr-card-list' }, (c.data.channels || []).map(x => row(x, true)));
  Vesper.skill('stream_raid', {
    panels: [{ view: 'stream', span: 4, title: 'Raid planner', after: 'stream_moments' }],
    render(d) {
      Vesper.streamCards?.set('stream_raid', d?.cards || [], cardList);
      for (const p of panels('stream_raid')) {
        const b = body(p);
        if (!d) { fill(b, empty('Loading…')); continue; }
        const list = d.channels || [];
        meta(p, d.updated ? `${d.game || ''} · ${ago(d.updated)}` : '');
        fill(b,
          h('div', { class: 'sc-row' }, h('span', { class: 't3 muted' }, list.length ? 'Best first: your size, your language, friends, not raided this week.' : ''),
            h('span', { class: 'spacer' }), h('button', { class: 'btn small', onclick: () => act('refresh', {}) }, list.length ? 'Refresh' : 'Who should I raid?')),
          d.error ? h('div', { class: 't3 sr-err' }, d.error) : null,
          list.length ? h('div', { class: 'list' }, list.map(c => row(c)))
            : empty('Say “who should I raid”, or wait: ten minutes before your stream block ends, picks show up here.'),
          (d.history || []).length ? h('div', { class: 't3 muted sr-hist' }, 'Recent: ', d.history.map(x => `${x.direction === 'out' ? '→' : '←'} ${x.name} (${ago(x.ts)})`).join(' · ')) : null);
      }
    },
  });
})();
