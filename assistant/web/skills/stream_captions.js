/* Live captions: the switch, what's on stream now, the OBS link and the optional OBS text source. */
(() => {
  const { h, fill, empty, meta, panels, body, toast } = Vesper.ui;
  async function act(action, payload, ok) {
    const r = await Vesper.act('stream_captions', action, payload);
    if (r.ok === false) toast(r.error || 'That didn’t work.', { level: 'warn', source: 'stream_captions' });
    else if (ok) toast(typeof ok === 'function' ? ok(r) : ok, { level: 'good', source: 'stream_captions' });
    return r;
  }
  async function copyLink() {
    const r = await act('link', {});
    if (r.url) navigator.clipboard.writeText(r.url).then(() => toast('Captions link copied: paste it as an OBS Browser source’s URL.'),
      () => toast('Couldn’t copy. Setup → OBS overlay has the same link (tick Captions).'));
  }

  Vesper.skill('stream_captions', {
    panels: [{ view: 'stream', span: 4, title: 'Live captions', after: 'stream_privacy' }],
    render(d) {
      for (const p of panels('stream_captions')) {
        const b = body(p);
        if (!d) { fill(b, empty('Loading…')); continue; }
        if (b.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') continue;
        meta(p, d.showing ? h('span', { class: 'cc-badge' }, d.live ? 'ON AIR' : `PREVIEW ${d.preview_s}s`) : d.on ? 'waiting for live' : 'off');
        const sw = h('button', { class: `cc-switch${d.on ? ' on' : ''}`, role: 'switch', 'aria-checked': String(!!d.on), 'aria-label': 'Live captions',
          onclick: () => act('toggle', { on: !d.on }) }, h('i', {}));
        const state = !d.on ? 'Off. Your words stay off the stream.'
          : d.off_record ? 'Off the record: nothing is captioned until it ends.'
            : d.showing ? (d.live ? 'Captioning what you say. Never Vesper’s voice or what you say to Vesper.' : 'Preview: showing for a few minutes so you can line it up in OBS.')
              : 'On: captions show when you go live.';
        const src = h('input', { value: d.obs_source || '', placeholder: 'OBS Text source name (optional)', 'aria-label': 'OBS text source name' });
        fill(b,
          h('div', { class: 'cc-row' }, sw, h('div', {}, h('div', { class: 't1' }, d.on ? 'On' : 'Off'), h('div', { class: 't3 muted' }, state))),
          d.voice ? h('div', { class: 't3 cc-warn' }, d.voice) : null,
          (d.lines || []).length ? h('div', { class: 'cc-now', 'aria-live': 'polite' }, d.lines.slice(-2).map(l => h('div', { class: 't2' }, l.text))) : null,
          h('div', { class: 'cc-actions' }, h('button', { class: 'btn small', onclick: copyLink }, 'Copy captions link for OBS')),
          h('form', { class: 'addrow', onsubmit: e => { e.preventDefault(); act('source', { name: src.value }, r => r.obs_source ? `Captions also go to “${r.obs_source}”.` : 'OBS text source cleared.'); } },
            src, h('button', { class: 'btn small', type: 'submit' }, 'Save')),
          d.obs_error ? h('div', { class: 't3 cc-warn' }, d.obs_error) : null);
      }
    },
  });
})();
