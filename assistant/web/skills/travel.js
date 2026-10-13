/* On the way: package cards with an Ordered → Shipped → Out for delivery → Delivered track, and the next flight with
   a live countdown, then hotel stays. Confirmation codes blur while you're live. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast } = Vesper.ui;
  const STEPS = ['Ordered', 'Shipped', 'Out for delivery', 'Delivered'];
  const local = iso => new Date(iso.length === 10 ? `${iso}T12:00:00` : iso);
  let ticking = null;

  function countdown(s) {
    if (s <= 0) return 'boarding now';
    const d = Math.floor(s / 86400), hr = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return d ? `in ${d}d ${hr}h` : hr ? `in ${hr}h ${String(m).padStart(2, '0')}m` : `in ${m}m`;
  }

  function track(p) {
    return h('div', { class: 'tr-track', role: 'img', 'aria-label': `${p.stage_name} (${p.stage + 1} of 4)` },
      h('div', { class: 'tr-rail' }, h('span', { class: 'tr-line' }, h('i', { class: 'tr-fill', style: `--p:${p.progress}` })),
        STEPS.map((s, i) => h('b', { class: `tr-dot ${i < p.stage ? 'done' : ''} ${i === p.stage ? 'now' : ''}`, style: `--x:${i / 3}` }))),
      h('div', { class: 'tr-steps' }, STEPS.map((s, i) => h('em', { class: i === p.stage ? 'now' : '' }, s))));
  }

  function pkg(p, i) {
    const eta = p.stage === 3 ? 'Delivered' : p.stage === 2 ? 'Out for delivery' : p.when ? `Arrives ${p.when}` : p.stage_name;
    return h('div', { class: `tr-pkg ${p.stage === 3 ? 'tr-done' : ''} ${p.today && p.stage < 3 ? 'tr-today' : ''}`, style: `--i:${i}` },
      h('div', { class: 'tr-head' },
        h('div', { class: 'main' }, h('div', { class: 't1' }, p.item || `Package from ${p.merchant}`),
          h('div', { class: 't2' }, [p.item ? p.merchant : null, p.carrier !== p.merchant || !p.item ? p.carrier : null].filter(Boolean).join(' · '))),
        h('span', { class: 'tr-eta' }, eta)),
      track(p),
      h('div', { class: 'tr-links t2' },
        p.track_url ? h('a', { href: p.track_url, target: '_blank', rel: 'noopener' }, `Track ${p.carrier}`) : null,
        p.link ? h('a', { href: p.link, target: '_blank', rel: 'noopener' }, 'Email') : null,
        h('button', { class: 'btn ghost small tr-forget', 'aria-label': 'Remove this card', title: 'Remove this card', onclick: async () => {
          const r = await Vesper.act('travel', 'forget', { key: p.key }); if (!r.ok) toast(r.error);
        } }, '✕')));
  }

  const secondsTo = f => (Date.parse(f.depart) - Date.now()) / 1000;
  function flight(f) {
    const left = secondsTo(f);
    return h('a', { class: 'tr-flight', href: f.link || null, target: '_blank', rel: 'noopener' },
      h('div', { class: 'tr-fl-top' }, h('span', { class: 'tr-fl-no' }, f.flight), h('span', { class: 't2' }, f.airline),
        h('span', { class: 'tr-count num', 'data-left': '' }, countdown(left))),
      h('div', { class: 'tr-route' }, h('span', {}, f.from || '—'), h('i', { class: 'tr-arrow', 'aria-hidden': 'true' }), h('span', {}, f.to || '—')),
      h('div', { class: 't2' }, `${local(f.depart).toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' })}${f.clock ? ` · ${f.clock}` : ''}`,
        f.confirmation ? [' · ', h('span', { class: 'tr-conf' }, f.confirmation)] : null));
  }

  function paint(d) {
    clearInterval(ticking);
    for (const p of panels('travel')) {
      const b = body(p);
      if (!d) { fill(b, empty('Loading…')); continue; }
      if (!d.ok) { fill(b, empty(d.error || 'Nothing yet.')); meta(p, ''); continue; }
      const pk = d.packages || [], fl = d.flights || [], ho = d.hotels || [];
      meta(p, d.scanned_at ? `email read ${ago(d.scanned_at)}` : '');
      b.classList.toggle('tr-private', !!d.live);
      fill(b,
        d.warning ? h('div', { class: 't3 tr-warn' }, d.warning) : null,
        h('div', { class: 'sub-h' }, `On the way${pk.length ? ` · ${pk.filter(x => x.stage < 3).length}` : ''}`),
        pk.length ? h('div', { class: 'tr-pkgs' }, pk.slice(0, 5).map(pkg)) : empty('No packages on the way.'),
        h('div', { class: 'sub-h' }, 'Trips'),
        fl.length ? flight(fl[0]) : null,
        fl.length > 1 || ho.length ? h('div', { class: 'list' },
          fl.slice(1, 4).map(f => h('a', { class: 'item', href: f.link || null, target: '_blank', rel: 'noopener' },
            h('div', { class: 'main' }, h('div', { class: 't1' }, `${f.flight} · ${f.from || ''}${f.to ? ` → ${f.to}` : ''}`),
              h('div', { class: 't2' }, `${f.when}${f.clock ? ` · ${f.clock}` : ''}`)))),
          ho.slice(0, 3).map(x => h('a', { class: 'item', href: x.link || null, target: '_blank', rel: 'noopener' },
            h('div', { class: 'main' }, h('div', { class: 't1' }, x.hotel),
              h('div', { class: 't2' }, `Check in ${x.when}${x.checkout ? ` · out ${local(x.checkout).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })}` : ''}`,
                x.confirmation ? [' · ', h('span', { class: 'tr-conf' }, x.confirmation)] : null))))) :
          (fl.length ? null : empty('No flights or hotel stays coming up.')));
    }
    if ((d?.flights || []).length) ticking = setInterval(() => {
      if (document.hidden) return;
      const f = Vesper.data.travel?.flights?.[0];
      if (!f) return;
      for (const el of document.querySelectorAll('.panel[data-panel="travel"] [data-left]')) el.textContent = countdown(secondsTo(f));
    }, 30000);
  }

  Vesper.skill('travel', {
    panels: [{ view: 'life', span: 4, title: 'On the way · packages & trips', after: 'home', buttons: [
      { label: 'Check email', onclick: async () => { const r = await Vesper.act('travel', 'scan', {}); toast(r.ok ? 'Up to date.' : r.error); } }] }],
    render: paint,
  });
})();
