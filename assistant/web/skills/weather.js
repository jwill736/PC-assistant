/* Weather: now, the next 12 hours as a temperature line with rain bars, and the week. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast } = Vesper.ui;
  const ICON = {
    sun: 'M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10zM12 1v3M12 20v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M1 12h3M20 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1',
    'cloud-sun': 'M8 3v2M3.5 5.5l1.4 1.4M2 10h2M12.5 6.9A4 4 0 0 0 5.2 10M7 20h10a4 4 0 0 0 0-8 6 6 0 0 0-11.5 1.5A3.5 3.5 0 0 0 7 20z',
    cloud: 'M7 19h10a4.5 4.5 0 0 0 0-9 6.5 6.5 0 0 0-12.4 2A3.5 3.5 0 0 0 7 19z',
    fog: 'M4 9h16M2 13h20M5 17h14M8 21h8',
    drizzle: 'M7 15h10a4 4 0 0 0 0-8 6 6 0 0 0-11.5 1.5A3.5 3.5 0 0 0 7 15zM9 18v2M15 18v2',
    rain: 'M7 14h10a4 4 0 0 0 0-8 6 6 0 0 0-11.5 1.5A3.5 3.5 0 0 0 7 14zM8 17l-1 3M12 17l-1 3M16 17l-1 3',
    snow: 'M7 14h10a4 4 0 0 0 0-8 6 6 0 0 0-11.5 1.5A3.5 3.5 0 0 0 7 14zM8 18h.01M12 20h.01M16 18h.01',
    storm: 'M7 14h10a4 4 0 0 0 0-8 6 6 0 0 0-11.5 1.5A3.5 3.5 0 0 0 7 14zM13 14l-3 5h4l-2 4',
  };
  const NS = 'http://www.w3.org/2000/svg';
  function icon(name, size = 22) {
    const s = document.createElementNS(NS, 'svg');
    for (const [k, v] of Object.entries({ viewBox: '0 0 24 24', width: size, height: size, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.6, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', class: `wx-icon wx-${name}` })) s.setAttribute(k, v);
    const p = document.createElementNS(NS, 'path'); p.setAttribute('d', ICON[name] || ICON.cloud); s.append(p); return s;
  }
  function hoursChart(hours, unit) {
    const W = 360, H = 90, list = hours.slice(0, 12);
    if (!list.length) return null;
    const temps = list.map(x => x.temp), lo = Math.min(...temps) - 2, hi = Math.max(...temps) + 2;
    const X = i => 12 + i * ((W - 24) / Math.max(1, list.length - 1)), Y = t => 12 + (H - 40) * (1 - (t - lo) / Math.max(1, hi - lo));
    const s = document.createElementNS(NS, 'svg'); s.setAttribute('viewBox', `0 0 ${W} ${H}`); s.setAttribute('class', 'wx-chart');
    const el = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); s.append(e); return e; };
    list.forEach((x, i) => { if (x.rain) el('rect', { x: X(i) - 5, y: H - 18 - (x.rain / 100) * 22, width: 10, height: (x.rain / 100) * 22, rx: 2, class: 'wx-rain' }); });
    el('path', { d: list.map((x, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(x.temp).toFixed(1)}`).join(''), class: 'wx-line', pathLength: 1 });
    list.forEach((x, i) => {
      if (i % 3) return;
      el('circle', { cx: X(i), cy: Y(x.temp), r: 2.5, class: 'wx-dot' });
      const t = el('text', { x: X(i), y: Y(x.temp) - 7, class: 'wx-t', 'text-anchor': 'middle' }); t.textContent = `${x.temp}°`;
      const c = el('text', { x: X(i), y: H - 3, class: 'wx-h', 'text-anchor': 'middle' }); c.textContent = new Date(x.time).toLocaleTimeString([], { hour: 'numeric' });
    });
    return s;
  }
  Vesper.skill('weather', {
    panels: [{ view: 'command', span: 4, title: 'Weather', after: 'agenda' }],
    render(d) {
      for (const p of panels('weather')) {
        const b = body(p);
        if (!d) { fill(b, empty('Loading…')); continue; }
        if (!d.ok) {
          const input = h('input', { placeholder: 'Your city, e.g. Atlanta', 'aria-label': 'Your city' });
          fill(b, empty(d.error || 'No weather yet.'), h('form', { class: 'addrow', onsubmit: async e => {
            e.preventDefault(); const r = await Vesper.act('weather', 'place', { name: input.value }); toast(r.ok ? `Weather for ${r.place.name}.` : r.error);
          } }, input, h('button', { class: 'btn small', type: 'submit' }, 'Set')));
          continue;
        }
        meta(p, `${d.place} · ${ago(d.at)}`);
        const n = d.now, t = d.days[0] || {};
        fill(b,
          h('div', { class: 'wx-now' }, icon(n.icon, 44),
            h('div', {}, h('div', { class: 'wx-temp num' }, `${n.temp}${d.unit}`), h('div', { class: 't2' }, `${n.text} · feels ${n.feels}°`)),
            h('div', { class: 'wx-side t3 muted' }, `H ${t.high}° · L ${t.low}°`, h('br'), `Wind ${n.wind} · Hum ${n.humidity}%`,
              d.aqi != null ? [h('br'), `AQI ${d.aqi}`] : null)),
          hoursChart(d.hours, d.unit),
          h('div', { class: 'wx-days' }, d.days.slice(1, 7).map(x => h('div', { class: 'wx-day', title: `${x.text}, ${x.rain}% rain` },
            h('div', { class: 't3 muted' }, x.name.slice(0, 3)), icon(x.icon, 18), h('div', { class: 'num t3' }, `${x.high}°`), h('div', { class: 'num t3 muted' }, `${x.low}°`)))));
      }
    },
  });
})();
