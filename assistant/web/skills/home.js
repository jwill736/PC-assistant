/* Lights & home: a tile per light (toggle, brightness, warmth), scene buttons, the ON AIR light, and setup. */
(() => {
  const { h, fill, empty, meta, panels, body, toast } = Vesper.ui;
  let setupOpen = false;  // stays open across the 10-second refreshes
  const act = async (action, payload) => { const r = await Vesper.act('home', action, payload); if (!r.ok) toast(r.error); return r; };

  function onAir(d) {
    const oa = d.on_air || {};
    const set = oa.entity || oa.key_light;
    return h('span', { class: `hm-onair ${oa.on ? 'on' : ''}`, title: set ? 'Turns on when you go live' : 'Not set up: see Set up below' },
      h('span', { class: 'hm-onair-dot' }), oa.on ? 'ON AIR' : 'off air');
  }

  function tile(x) {
    const range = (label, min, max, value, step, action, key) => h('input', { type: 'range', min, max, step, value, 'aria-label': `${x.name} ${label}`,
      onchange: e => act(action, { id: x.id, [key]: Number(e.target.value) }) });
    return h('div', { class: `hm-tile ${x.on ? 'on' : ''} ${x.ok ? '' : 'down'}` },
      h('button', { class: 'hm-toggle', 'aria-pressed': String(x.on), title: x.ok ? '' : 'Not answering',
        onclick: () => act('toggle', { id: x.id, on: !x.on }) },
        h('span', { class: 'hm-glow', 'aria-hidden': 'true' }),
        h('span', { class: 't1 hm-name' }, x.name),
        h('span', { class: 't3 muted' }, !x.ok ? 'not answering' : x.on ? (x.brightness != null ? `${x.brightness}%` : 'on') + (x.kelvin ? ` · ${x.kelvin} K` : '') : 'off')),
      x.dimmable ? h('div', { class: 'hm-sliders' },
        range('brightness', 1, 100, x.brightness ?? 50, 1, 'brightness', 'level'),
        x.kelvin != null || x.kind === 'elgato' ? range('warmth', 2900, 7000, x.kelvin ?? 4500, 100, 'kelvin', 'kelvin') : null) : null);
  }

  function setup(d) {
    const ip = h('input', { placeholder: 'Key Light IP, e.g. 192.168.1.40', 'aria-label': 'Key Light IP address' });
    const nm = h('input', { placeholder: 'Name (optional)', 'aria-label': 'Key Light name' });
    const url = h('input', { placeholder: 'http://homeassistant.local:8123', 'aria-label': 'Home Assistant address', value: d.ha?.url || '' });
    const tok = h('input', { type: 'password', placeholder: d.ha?.configured ? 'Token saved: paste to replace' : 'Long-lived access token', 'aria-label': 'Home Assistant token', autocomplete: 'off' });
    const oa = d.on_air || {};
    const ent = h('input', { placeholder: 'HA entity, e.g. light.on_air', value: oa.entity || '', 'aria-label': 'On air entity' });
    const br = h('input', { type: 'number', min: 1, max: 100, placeholder: 'Brightness %', value: oa.key_light?.brightness || '', 'aria-label': 'On air key light brightness' });
    const kv = h('input', { type: 'number', min: 2900, max: 7000, step: 100, placeholder: 'Kelvin', value: oa.key_light?.kelvin || '', 'aria-label': 'On air key light kelvin' });
    return h('details', { class: 'hm-setup', open: setupOpen || d.next_step ? true : null, ontoggle: e => { setupOpen = e.target.open; } },
      h('summary', { class: 't2' }, 'Set up'),
      h('div', { class: 'sub-h' }, 'Elgato Key Light'),
      h('form', { class: 'addrow', onsubmit: async e => { e.preventDefault(); const r = await act('add_light', { ip: ip.value, name: nm.value }); if (r.ok) toast(r.reachable ? 'Key Light added.' : 'Saved, but it isn’t answering yet.'); } },
        ip, nm, h('button', { class: 'btn small', type: 'submit' }, 'Add'),
        d.can_find ? h('button', { class: 'btn small', type: 'button', onclick: async () => { toast('Looking for Key Lights…'); const r = await act('discover'); if (r.ok) toast(`Found ${r.found.length}.`); } }, 'Find') : null),
      (d.key_lights || []).map(k => h('div', { class: 'item' }, h('div', { class: 'main t2' }, `${k.name} · ${k.ip}`),
        h('button', { class: 'btn small icon', 'aria-label': `Remove ${k.name}`, onclick: () => act('remove_light', { ip: k.ip }) }, '✕'))),
      h('div', { class: 'sub-h' }, 'Home Assistant'),
      h('form', { class: 'addrow', onsubmit: async e => { e.preventDefault(); const r = await act('ha', { url: url.value, token: tok.value }); if (r.ok) { tok.value = ''; toast(r.reachable ? 'Home Assistant connected.' : 'Saved, but Home Assistant isn’t answering yet.'); } } },
        url, tok, h('button', { class: 'btn small', type: 'submit' }, 'Save')),
      h('div', { class: 'sub-h' }, 'ON AIR when you go live'),
      h('form', { class: 'addrow', onsubmit: async e => { e.preventDefault(); const r = await act('on_air_setup', { entity: ent.value, brightness: br.value, kelvin: kv.value }); if (r.ok) toast('Saved.'); } },
        ent, br, kv, h('button', { class: 'btn small', type: 'submit' }, 'Save'),
        h('button', { class: 'btn small', type: 'button', onclick: () => act('on_air', { on: !oa.on }) }, oa.on ? 'Test: off' : 'Test: on')));
  }

  Vesper.skill('home', {
    panels: [{ view: 'life', span: 4, title: 'Lights & home', after: 'spotify' },
      { view: 'stream', span: 4, title: 'Lights', after: 'spotify' }],
    render(d) {
      for (const p of panels('home')) {
        const b = body(p);
        if (!d) { fill(b, empty('Loading…')); continue; }
        if (b.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') continue;  // mid-drag or typing
        meta(p, onAir(d));
        const lights = d.lights || [];
        fill(b,
          d.next_step ? h('div', { class: 't2' }, d.next_step) : null,
          lights.length ? h('div', { class: 'hm-tiles' }, lights.map(tile)) : null,
          (d.scenes || []).length ? h('div', { class: 'hm-scenes' }, d.scenes.map(s => h('button', { class: 'btn small', onclick: async () => { const r = await act('scene', { id: s.id }); if (r.ok) toast(`${s.name} on.`); } }, s.name))) : null,
          [d.ha?.error, ...(d.errors || [])].filter(Boolean).map(e => h('div', { class: 't3 hm-err' }, e)),
          setup(d));
      }
    },
  });
})();
