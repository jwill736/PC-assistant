/* Tool servers (MCP): connected servers with their tools, how much each may do on its own, and an add form with presets. */
(() => {
  const { h, fill, empty, meta, panels, body, toast, status } = Vesper.ui;
  const TIERS = [[2, 'Ask me first'], [1, 'Just do it'], [3, 'Ask every time, read back']];
  const STATE = { connected: ['good', 'Connected'], connecting: ['warning', 'Connecting…'], error: ['critical', 'Not connected'], off: ['idle', 'Off'] };
  const open = new Set();
  const act = async (action, payload, ok) => {
    const r = await Vesper.act('mcp', action, payload);
    if (r.ok === false || r.error) toast(r.error || 'That failed.'); else if (ok) toast(ok);
    return r;
  };
  const tierSelect = (value, onchange, withDefault) => h('select', { class: 'field mcp-tier', 'aria-label': 'What it may do on its own', onchange: e => onchange(e.target.value) },
    withDefault ? h('option', { value: 'default', selected: withDefault === 'default' }, 'Server default') : null,
    TIERS.map(([v, label]) => h('option', { value: v, selected: withDefault !== 'default' && Number(value) === v }, label)));

  function form(b, presets) {
    const name = h('input', { class: 'mcp-name', placeholder: 'Short name (e.g. files)', 'aria-label': 'Server name', maxlength: 24 });
    const target = h('input', { class: 'mcp-mono mcp-target-in', placeholder: 'Command line (npx -y …) or URL (https://…/mcp)', 'aria-label': 'Command line or URL' });
    const token = h('input', { type: 'password', placeholder: 'Token (optional, saved to .env)', 'aria-label': 'Token', autocomplete: 'off' });
    const tokenVar = h('input', { class: 'mcp-mono', placeholder: 'Token setting, e.g. NOTION_TOKEN', 'aria-label': 'Which setting the server reads the token from' });
    const tier = tierSelect(2, () => {});
    const hint = h('div', { class: 't2 mcp-hint' }, 'Pick a preset or fill in your own. Presets only fill the form.');
    const varRow = () => { tokenVar.hidden = /^https?:\/\//i.test(target.value.trim()) || !token.value; };
    target.addEventListener('input', varRow); token.addEventListener('input', varRow); varRow();
    const chips = h('div', { class: 'chips' }, presets.map(p => h('button', { class: 'chip mcp-preset', type: 'button', title: p.about, onclick: () => {
      name.value = p.name; target.value = p.target; tokenVar.value = p.token_var || ''; token.value = '';
      token.placeholder = p.token_label ? `${p.token_label}` : 'Token (optional, saved to .env)';
      fill(hint, h('b', {}, p.label), ' · ', p.about); varRow(); target.focus();
    } }, p.label)));
    const submit = h('button', { class: 'btn primary small', type: 'submit' }, 'Add & connect');
    return h('form', { class: 'mcp-add', onsubmit: async e => {
      e.preventDefault();
      submit.disabled = true; submit.textContent = 'Connecting…';
      const r = await act('add', { name: name.value, target: target.value, token: token.value, token_var: tokenVar.value, tier: Number(tier.value) });
      submit.disabled = false; submit.textContent = 'Add & connect';
      if (r.ok) { toast(r.state === 'connected' ? `${r.name} connected.` : `${r.name} added: ${r.error || r.state}`); name.value = target.value = token.value = ''; varRow(); }
    } }, h('div', { class: 'sub-h' }, 'Add a tool server'), chips, hint,
      h('div', { class: 'addrow' }, name, target), h('div', { class: 'addrow' }, token, tokenVar, tier, submit));
  }

  function toolRow(s, t) {
    return h('div', { class: `mcp-tool ${t.offered ? '' : 'off'}` },
      h('label', { class: 'mcp-check' }, h('input', { type: 'checkbox', checked: t.offered, onchange: e => act('offer', { name: s.name, tool: t.name, on: e.target.checked }) }),
        h('span', { class: 'mcp-mono' }, t.name)),
      h('span', { class: 't2 mcp-desc', title: t.description }, t.read_only ? h('span', { class: 'pill' }, 'read-only') : null, ' ', t.description),
      tierSelect(t.tier, v => act('tier', { name: s.name, tool: t.name, tier: v }), t.own_tier ? t.tier : 'default'));
  }

  function server(s) {
    const [lvl, label] = STATE[s.state] || STATE.off;
    const isOpen = open.has(s.name);
    return h('div', { class: `item mcp-server mcp-${s.state}` },
      h('div', { class: 'main' },
        h('div', { class: 't1 mcp-title' }, status(lvl, ''), h('span', {}, s.name), h('span', { class: 'pill' }, s.transport === 'http' ? 'URL' : 'on this PC'),
          s.state === 'connected' ? h('span', { class: 't2' }, `${s.offered} of ${s.total} tools offered`) : h('span', { class: 't2' }, label)),
        h('div', { class: 't2 mcp-mono mcp-target', title: s.target }, s.target),
        s.error ? h('div', { class: 't2 warn' }, s.error) : null,
        s.missing.length ? h('div', { class: 't2 warn' }, `Missing from .env: ${s.missing.join(', ')}. Add the server again with its token.`) : null,
        h('div', { class: 'mcp-controls' },
          tierSelect(s.tier, v => act('tier', { name: s.name, tier: v }, 'Saved.')),
          s.enabled && s.state !== 'error'
            ? h('button', { class: 'btn small', onclick: () => act('disconnect', { name: s.name }) }, 'Disconnect')
            : h('button', { class: 'btn small', onclick: () => act('connect', { name: s.name }) }, s.state === 'error' ? 'Retry' : 'Connect'),
          s.state === 'connected' ? h('button', { class: 'btn small', onclick: () => act('refresh', { name: s.name }, 'Tools refreshed.') }, 'Refresh') : null,
          s.total ? h('button', { class: 'btn small ghost', 'aria-expanded': String(isOpen), onclick: () => { isOpen ? open.delete(s.name) : open.add(s.name); paint(Vesper.data.mcp); } }, isOpen ? 'Hide tools' : 'Tools') : null,
          h('span', { class: 'spacer' }),
          h('button', { class: 'btn small danger', onclick: () => { if (confirm(`Remove the ${s.name} tool server?`)) act('remove', { name: s.name }); } }, 'Remove')),
        isOpen ? h('div', { class: 'mcp-tools' }, s.tools.map(t => toolRow(s, t))) : null));
  }

  function paint(d) {
    if (!d) return;
    for (const p of panels('mcp')) {
      const b = body(p);
      meta(p, d.connected ? `${d.connected} connected · ${d.tools} tools` : '');
      if (!b._form) { b._top = h('div', {}); b._list = h('div', { class: 'list' }); b._form = form(b, d.presets || []); fill(b, b._top, b._list, b._form); }
      fill(b._top,
        d.sdk ? h('div', { class: 'mcp-sdk' }, h('b', {}, 'The MCP package isn’t installed. '), 'Run ', h('b', {}, 'Update Vesper'), ' from the Start menu, or in a terminal: ', h('code', {}, d.sdk)) : null,
        h('div', { class: 't2' }, 'Tool servers give Vesper new abilities (Model Context Protocol). Each tool asks for your yes unless you lower it; what a server sends back is treated as information, never instructions.'));
      fill(b._list, d.servers.length ? d.servers.map(server) : empty('No tool servers yet. Start with a preset below.'));
    }
  }

  Vesper.skill('mcp', { panels: [{ view: 'setup', span: 8, title: 'Tool servers (MCP)', after: 'google' }], render: paint });
})();
