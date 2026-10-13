/* PC Health: radial gauges for the GPU (temperature, power against its limit) and every drive's fill, what's eating
   the space on a drive, and a Downloads tidy that only moves files (with undo). Gauges are kept between refreshes so
   their end markers glide (transform only); a gauge in the red breathes. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast, status } = Vesper.ui;
  const NS = 'http://www.w3.org/2000/svg';
  const svg = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); return e; };
  const size = n => n == null ? '?' : n >= 2 ** 40 ? `${(n / 2 ** 40).toFixed(1)} TB` : n >= 10 * 2 ** 30 ? `${Math.round(n / 2 ** 30)} GB`
    : n >= 2 ** 30 ? `${(n / 2 ** 30).toFixed(1)} GB` : `${Math.round(n / 2 ** 20)} MB`;
  let confirmTidy = false;
  function gauge(cache, key) {
    if (cache[key]) return cache[key];
    const s = svg('svg', { viewBox: '0 0 100 100', class: 'pc-ring', role: 'img' });
    const g = svg('g', { transform: 'rotate(-90 50 50)' });
    const track = svg('circle', { cx: 50, cy: 50, r: 40, class: 'pc-track', pathLength: 100 });
    const arc = svg('circle', { cx: 50, cy: 50, r: 40, class: 'pc-arc', pathLength: 100, 'stroke-dasharray': '0 100' });
    const halo = svg('circle', { cx: 50, cy: 50, r: 46, class: 'pc-halo' });
    const tip = svg('g', { class: 'pc-tip' }); tip.append(svg('circle', { cx: 90, cy: 50, r: 4 }));
    g.append(track, arc, tip);
    s.append(halo, g);
    const val = h('div', { class: 'pc-val num' }), sub = h('div', { class: 'pc-sub t3 muted' }), lab = h('div', { class: 'pc-lab t3' });
    const el = h('div', { class: 'pc-gauge', 'data-key': key }, h('div', { class: 'pc-dial' }, s, h('div', { class: 'pc-mid' }, val, sub)), lab);
    return (cache[key] = { el, s, arc, tip, val, sub, lab });
  }
  function setGauge(x, pct, level, value, sub, label, title) {
    pct = Math.max(0, Math.min(100, pct || 0));
    x.arc.setAttribute('stroke-dasharray', `${pct} 100`);
    x.tip.style.transform = `rotate(${pct * 3.6}deg)`;
    x.el.className = `pc-gauge ${level}`;
    x.val.textContent = value; x.sub.textContent = sub; x.lab.textContent = label;
    x.s.setAttribute('aria-label', title); x.el.title = title;
  }
  const lvl = (v, warn, crit) => v >= crit ? 'crit' : v >= warn ? 'warn' : '';
  function gauges(b, d) {
    const cache = b._g || (b._g = {}), out = [];
    (d.gpus || []).forEach((g, i) => {
      if (g.temp_c != null) {
        const x = gauge(cache, `t${i}`);
        setGauge(x, g.temp_c, lvl(g.temp_c, 80, 86), `${Math.round(g.temp_c)}°`, 'C', 'GPU temp', `${g.name}: ${g.temp_c}°C`);
        out.push(x.el);
      }
      if (g.power_w != null) {
        const pct = g.limit_w ? g.power_w / g.limit_w * 100 : 0, x = gauge(cache, `p${i}`);
        setGauge(x, pct, g.power_capped && pct > 96 ? 'warn' : '', `${Math.round(g.power_w)}`, g.limit_w ? `of ${Math.round(g.limit_w)} W` : 'W',
          g.throttle.length ? `GPU power · ${g.throttle[0]}` : 'GPU power', `${g.name}: ${g.power_w} W of ${g.limit_w} W${g.throttle.length ? `, throttling: ${g.throttle.join(', ')}` : ''}`);
        out.push(x.el);
      }
    });
    if (d.cpu_temp != null) {
      const x = gauge(cache, 'cpu');
      setGauge(x, d.cpu_temp, lvl(d.cpu_temp, 85, 95), `${Math.round(d.cpu_temp)}°`, 'C', 'CPU temp', `CPU ${d.cpu_temp}°C`);
      out.push(x.el);
    }
    for (const dr of d.drives || []) {
      const x = gauge(cache, `d${dr.mount}`);
      setGauge(x, dr.used_pct, dr.free_pct < 10 ? 'crit' : dr.free_pct < 20 ? 'warn' : '', `${Math.round(dr.used_pct)}%`, `${size(dr.free)} free`,
        dr.label, `${dr.label} ${dr.used_pct}% full: ${size(dr.free)} free of ${size(dr.total)}`);
      x.el.onclick = () => scan(dr.mount);
      out.push(x.el);
    }
    return out;
  }
  async function scan(drive) { const r = await Vesper.act('janitor', 'scan', { drive }); if (!r.ok) toast(r.error); }
  function report(b, d) {
    const scanning = (d.scanning || [])[0], r = d.report;
    if (!r && !scanning) return null;
    if (!r || (scanning && scanning === r.drive.mount)) {
      return h('div', { class: 'pc-section' }, h('div', { class: 'pc-scan' }, h('span', { class: 'pc-sweep' }), `Looking at what's on ${scanning}…`));
    }
    const top = Math.max(1, ...r.folders.map(f => f.size)), fresh = b._rep !== r.at;
    b._rep = r.at;  // the bars grow in once per new report
    const extra = [['Duplicate files', r.duplicates_bytes, `${r.duplicates.length} sets`], ['Downloads over 60 days', r.old_downloads?.bytes, `${r.old_downloads?.files || 0} files`],
      ['Recycle Bin', r.recycle_bin, 'empty it from the desktop'], ['Temp files', r.temp?.bytes, 'say “clean temp files”']].filter(x => x[1]);
    return h('div', { class: `pc-section ${fresh ? 'pc-fresh' : ''}` },
      h('div', { class: 'pc-head' }, h('b', {}, `What's on ${r.drive.label}`), h('span', { class: 't3 muted' }, `${size(r.scanned)} looked at · ${ago(r.at)}${r.partial ? ' · partial (time box)' : ''}`)),
      h('div', { class: 'pc-folders' }, r.folders.map(f => h('div', { class: 'pc-folder', title: [f.path, ...f.children.map(c => `  ${c.name}: ${size(c.size)}`)].join('\n') },
        h('span', { class: 'pc-name' }, f.name), h('span', { class: 'pc-bar' }, h('i', { style: { transform: `scaleX(${f.size / top})` } })), h('span', { class: 'num t3' }, size(f.size))))),
      extra.length ? h('div', { class: 'pc-extra' }, extra.map(([k, v, note]) => h('div', { class: 'pc-chip' }, h('b', { class: 'num' }, size(v)), ` ${k}`, h('span', { class: 't3 muted' }, ` · ${note}`)))) : null);
  }
  function tidy(d) {
    const p = d.preview;
    const btns = [h('button', { class: 'btn small', onclick: async () => { confirmTidy = false; const r = await Vesper.act('janitor', 'preview'); if (!r.ok) toast(r.error); } }, p ? 'Refresh plan' : 'Preview Downloads tidy')];
    if (p?.count) {
      btns.push(h('button', { class: `btn small ${confirmTidy ? 'primary' : ''}`, onclick: async () => {
        if (!confirmTidy) { confirmTidy = true; return paint(Vesper.data.janitor); }
        confirmTidy = false; const r = await Vesper.act('janitor', 'tidy');
        toast(r.ok ? `Moved ${r.moved} files. Nothing was deleted.` : r.error);
      } }, confirmTidy ? `Sure? Move ${p.count} files` : `Move ${p.count} files`));
    }
    if (d.undo) btns.push(h('button', { class: 'btn small', onclick: async () => { const r = await Vesper.act('janitor', 'undo'); toast(r.ok ? `Put ${r.restored} files back.` : r.error); } }, 'Undo last tidy'));
    return h('div', { class: 'pc-section' },
      h('div', { class: 'pc-head' }, h('b', {}, 'Downloads'), h('span', { class: 't3 muted' }, p ? (p.count ? `${p.count} files · ${size(p.bytes)} · moves only, never deletes` : 'already tidy') : 'sorts into Images, Documents, Installers, Archives, Video')),
      p?.count ? h('div', { class: 'pc-extra' }, Object.entries(p.by_folder).map(([k, n]) => h('div', { class: 'pc-chip' }, h('b', { class: 'num' }, n), ` → ${k}`))) : null,
      h('div', { class: 'pc-btns' }, btns));
  }
  function paint(d) {
    for (const p of panels('janitor')) {
      const b = body(p);
      if (!d) { fill(b, empty('Checking…')); b._wrap = null; continue; }
      meta(p, d.at ? `checked ${ago(d.at)}` : '');
      if (!b._wrap) {  // kept between refreshes, so the gauges glide instead of being rebuilt
        b._top = h('div'); b._wrap = h('div', { class: 'pc-gauges' }); b._rest = h('div');
        fill(b, b._top, b._wrap, b._rest);
      }
      const rings = gauges(b, d), keys = rings.map(r => r.dataset.key).join('|');
      if (b._wrap.dataset.keys !== keys) { fill(b._wrap, rings); b._wrap.dataset.keys = keys; }
      fill(b._top,
        (d.issues || []).length ? h('div', { class: 'pc-issues' }, d.issues.map(i => status('critical', i))) : (d.at ? h('div', { class: 'pc-issues' }, status('good', 'All good')) : null),
        d.error ? h('div', { class: 't3 muted' }, d.error) : null);
      fill(b._rest,
        rings.length ? null : empty('No readings yet. GPU readings need the NVIDIA driver (nvidia-smi).'),
        (d.drives || []).length ? h('div', { class: 't3 muted pc-hint' }, 'Click a drive to see what’s filling it.') : null,
        report(b, d), tidy(d));
    }
  }
  Vesper.skill('janitor', { panels: [{ view: 'pc', span: 8, title: 'PC Health', first: true }], render: paint });
})();
