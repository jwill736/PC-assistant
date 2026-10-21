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

  /* ---- Travel day: countdown, leave-by rail, check-in, weather there, packing list ---------------------------- */
  const hm = s => { s = Math.max(0, Math.round(s / 60)); const hh = Math.floor(s / 60), mm = s % 60; return hh ? `${hh}h ${String(mm).padStart(2, '0')}m` : `${mm}m`; };
  const clockOf = iso => new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const DRIVE_FROM = { route: 'route estimate', setting: 'your setting', guess: 'a guess: set skills.travel.drive_min', away: 'from where you’re staying' };

  function lead(t) {
    const leave = t.leave_by ? (Date.parse(t.leave_by) - Date.now()) / 1000 : null, dep = secondsTo(t);
    if (leave != null && leave > 0) return { label: 'Leave in', value: hm(leave), cls: leave <= 1800 ? 'td-soon' : '' };
    if (dep > 0) return { label: leave != null ? 'Departs in · you should be on the way' : 'Departs in', value: hm(dep), cls: 'td-go' };
    return { label: 'Departed', value: t.clock || '', cls: '' };
  }

  function rail(t) {
    if (!t.leave_by) return h('div', { class: 't2' }, 'No departure time in the email: check the airline app.');
    const leave = Date.parse(t.leave_by), dep = Date.parse(t.depart), at = leave + t.drive_min * 60000;
    const span = dep - leave, now = Date.now();
    const pos = Math.min(1, Math.max(0, (now - leave) / span));
    return h('div', { class: 'td-rail', role: 'img', 'aria-label': `Leave ${t.leave_clock}, at the airport ${clockOf(at)}, departs ${t.clock}` },
      h('div', { class: 'td-bar' },
        h('i', { class: 'td-drive', style: `flex:${t.drive_min}` }), h('i', { class: 'td-buffer', style: `flex:${t.buffer_min}` }),
        now >= leave && now <= dep ? h('b', { class: 'td-now', style: `--x:${pos}` }) : null),
      h('div', { class: 'td-marks' },
        h('span', {}, h('em', {}, 'Leave'), t.leave_clock),
        h('span', { style: `--x:${t.drive_min / (t.drive_min + t.buffer_min)}` }, h('em', {}, 'Airport'), clockOf(at)),
        h('span', {}, h('em', {}, 'Departs'), t.clock)));
  }

  function weatherBox(w, dest) {
    if (!w) return h('div', { class: 'td-wx' }, h('div', { class: 'sub-h' }, `Weather in ${dest || 'your destination'}`), empty('No forecast yet.'));
    return h('div', { class: 'td-wx' }, h('div', { class: 'sub-h' }, `Weather in ${w.place}`),
      h('div', { class: 'td-now-wx' }, h('span', { class: 'num' }, `${w.now.temp}${w.unit}`), h('span', { class: 't2' }, w.now.text)),
      h('div', { class: 'td-days' }, (w.days || []).map((x, i) => h('div', { class: 'td-day', style: `--i:${i}` },
        h('em', {}, local(x.date).toLocaleDateString([], { weekday: 'short' })),
        h('span', { class: 'num' }, `${x.high}° / ${x.low}°`), h('span', { class: 't3' }, x.rain >= 20 ? `${x.rain}% rain` : x.text)))),
      w.advice ? h('div', { class: 't2 td-advice' }, w.advice) : null);
  }

  function packing(t) {
    const list = t.packing || [], done = list.filter(x => x.done).length;
    const act = async (action, item) => { const r = await Vesper.act('travel', action, { trip: t.key, item }); if (!r.ok) toast(r.error); };
    const input = h('input', { type: 'text', placeholder: 'Add an item…', 'aria-label': 'Add a packing item', maxlength: 60 });
    return h('div', { class: 'td-pack' },
      h('div', { class: 'sub-h' }, `Packing · ${done} of ${list.length}`),
      h('div', { class: 'td-meter' }, h('i', { style: `--p:${list.length ? done / list.length : 0}` })),
      h('ul', { class: 'td-list' }, list.map((x, i) => h('li', { class: x.done ? 'done' : '', style: `--i:${i}` },
        h('label', {}, h('input', { type: 'checkbox', checked: x.done, onchange: () => act('pack_toggle', x.item) }), h('span', {}, x.item)),
        h('button', { class: 'btn ghost small td-x', title: 'Remove', 'aria-label': `Remove ${x.item}`, onclick: () => act('pack_remove', x.item) }, '✕')))),
      h('form', { class: 'td-add', onsubmit: e => { e.preventDefault(); const v = input.value.trim(); if (v) { input.value = ''; act('pack_add', v); } } },
        input, h('button', { class: 'btn small', type: 'submit' }, 'Add')));
  }

  function paintDay(d) {
    const t = d && d.ok ? d.travel_day : null;
    for (const p of panels('travel-day')) {
      p.hidden = !t;
      if (!t) continue;
      const b = body(p), l = lead(t);
      p.querySelector('h2').textContent = t.today ? 'Travel day' : `Travel ${t.when}`;
      const conf = t.confirmation && !d.live;  // never on screen while live
      meta(p, `${t.airline} ${t.flight}${conf ? ' · ' : ''}`);
      if (conf) p.querySelector('[data-slot="meta"]').append(h('span', { class: 'tr-conf' }, t.confirmation));
      b.classList.toggle('tr-private', !!d.live);
      fill(b, h('div', { class: 'td-grid' },
        h('section', { class: 'td-main' },
          h('div', { class: `td-lead ${l.cls}` }, h('div', { class: 'label' }, l.label), h('div', { class: 'td-count num', 'data-td': '' }, l.value)),
          h('div', { class: 'tr-route td-route' }, h('span', {}, t.from || '—'), h('i', { class: 'tr-arrow', 'aria-hidden': 'true' }), h('span', {}, t.dest || t.to || '—')),
          h('div', { class: 't1 td-leave' }, t.leave_by ? `Leave by ${t.leave_clock}` : `Departs ${t.clock}`),
          h('div', { class: 't2' }, `${Math.round(t.buffer_min / 60 * 10) / 10} h at the airport${t.international ? ' (international)' : ''} + ${t.drive_min} min drive · ${DRIVE_FROM[t.drive_from] || ''}`),
          rail(t),
          h('div', { class: 'td-actions' },
            t.checkin_url ? h('a', { class: `btn ${t.checkin_open ? 'primary' : ''}`, href: t.checkin_url, target: '_blank', rel: 'noopener' },
              t.checkin_open ? 'Check in' : `Check-in opens ${local(t.checkin_opens).toLocaleDateString([], { weekday: 'short' })} ${clockOf(t.checkin_opens)}`) : null,
            t.link ? h('a', { class: 'btn ghost', href: t.link, target: '_blank', rel: 'noopener' }, 'Booking email') : null)),
        weatherBox(t.weather, t.dest),
        packing(t)));
    }
  }

  const art = (Vesper.panel({ name: 'travel-day', view: 'life', span: 12, title: 'Travel day', first: true }), panels('travel-day')[0]);
  if (art) art.hidden = true;
  let dayTick = null;
  Vesper.skill('travel', {
    panels: [{ view: 'life', span: 4, title: 'On the way · packages & trips', after: 'home', buttons: [
      { label: 'Check email', onclick: async () => { const r = await Vesper.act('travel', 'scan', {}); toast(r.ok ? 'Up to date.' : r.error); } }] }],
    render(d) {
      paint(d);
      paintDay(d);
      clearInterval(dayTick);
      if (d?.travel_day) dayTick = setInterval(() => {  // just the countdown: a full repaint would eat a half-typed item
        const t = Vesper.data.travel?.travel_day;
        if (document.hidden || !t) return;
        const l = lead(t);
        for (const el of document.querySelectorAll('.panel[data-panel="travel-day"] [data-td]')) {
          el.textContent = l.value;
          el.parentElement.className = `td-lead ${l.cls}`;
          el.previousElementSibling.textContent = l.label;
        }
      }, 30000);
    },
  });
})();
