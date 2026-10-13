/* Phone: a Telegram bot to talk to Vesper from anywhere, and urgent things pushed to the phone while you're away. */
(() => {
  const { h, fill, panels, body, meta, toast, status, ago } = Vesper.ui;
  let changing = false;
  const act = async (action, payload, ok) => {
    const r = await Vesper.act('phone', action, payload);
    if (r.ok === false) toast(r.error || 'That failed.'); else if (ok) toast(typeof ok === 'function' ? ok(r) : ok);
    return r;
  };
  const step = (n, title, done, ...kids) => h('div', { class: `ph-step ${done ? 'done' : ''}` },
    h('div', { class: 'ph-num', 'aria-hidden': 'true' }, done ? '✓' : String(n)),
    h('div', { class: 'ph-main' }, h('div', { class: 't1' }, title), ...kids));

  function tokenStep(d) {
    if (d.token && !changing) {
      return step(1, d.bot ? `Bot: @${d.bot}` : 'Bot token saved', true,
        h('button', { class: 'btn small ghost', onclick: () => { changing = true; paint(Vesper.data.phone); } }, 'Change token'));
    }
    const input = h('input', { type: 'password', placeholder: '123456789:AA…', 'aria-label': 'Telegram bot token', autocomplete: 'off' });
    return step(1, 'Make your bot', false,
      h('ol', { class: 't2 ph-how' },
        h('li', {}, 'In Telegram, search for ', h('b', {}, '@BotFather'), ' (blue tick) and press Start.'),
        h('li', {}, 'Send ', h('code', {}, '/newbot'), ', give it a name, then a username ending in “bot”.'),
        h('li', {}, 'Copy the token it sends (looks like 123456789:AA…) and paste it here.')),
      h('form', { class: 'addrow', onsubmit: async e => {
        e.preventDefault();
        const r = await act('token', { token: input.value }, r2 => `Saved. Your bot is @${r2.bot}.`);
        if (r.ok) { input.value = ''; changing = false; }
      } }, input, h('button', { class: 'btn primary small', type: 'submit' }, 'Save'),
        changing ? h('button', { class: 'btn small ghost', type: 'button', onclick: () => { changing = false; paint(Vesper.data.phone); } }, 'Cancel') : null));
  }

  function pairStep(d) {
    if (d.paired) {
      return step(2, `Paired with ${d.chat || 'your phone'}`, true,
        h('div', { class: 't2' }, d.last_message ? `Last message ${ago(d.last_message)}.` : 'Send the bot a message to try it: “/status”.'),
        h('button', { class: 'btn small ghost', onclick: () => { if (confirm('Unpair this phone? It will stop reaching Vesper.')) act('unpair', {}, 'Unpaired.'); } }, 'Unpair'));
    }
    return step(2, 'Pair your phone', false,
      d.token ? [
        h('div', { class: 'ph-code', 'aria-label': `Pairing code ${d.code}` }, String(d.code || '').split('').map(c => h('span', {}, c))),
        h('div', { class: 't2' }, 'Send your bot ', h('code', {}, `/start ${d.code}`), d.bot ? ` (open @${d.bot} in Telegram)` : '', '. The first chat with this code is paired; everyone else is ignored.'),
        d.pair_link ? h('a', { class: 'btn small', href: d.pair_link, target: '_blank', rel: 'noopener' }, 'Open in Telegram') : null,
        d.locked ? h('div', { class: 't2 warn' }, 'Too many wrong codes: this is a new one, and pairing waits 10 minutes.') : null,
        h('button', { class: 'btn small ghost', onclick: () => act('new_code') }, 'New code'),
      ] : h('div', { class: 't2' }, 'Save the bot token first.'));
  }

  function pushStep(d) {
    const topic = h('input', { placeholder: d.ntfy ? `Topic set (${d.ntfy_hint})` : 'ntfy topic (optional)', 'aria-label': 'ntfy topic', class: 'ph-mono' });
    return step(3, 'When you’re away', d.push_away && (d.paired || d.ntfy),
      h('label', { class: 'ph-toggle' },
        h('input', { type: 'checkbox', role: 'switch', checked: d.push_away, onchange: e => act('push_away', { on: e.target.checked }) }),
        h('span', { class: 'ph-switch', 'aria-hidden': 'true' }),
        'Send urgent things and reminders to the phone after 5 minutes away from the PC'),
      h('div', { class: 't2' }, 'Also, or instead: the free ', h('b', {}, 'ntfy'), ' app. Install it, subscribe to a long random topic, and save the same topic here. Anyone who knows the topic can read it.'),
      h('form', { class: 'addrow', onsubmit: async e => { e.preventDefault(); const r = await act('ntfy', { topic: topic.value }, topic.value ? 'ntfy topic saved.' : 'ntfy off.'); if (r.ok) topic.value = ''; } },
        topic,
        h('button', { class: 'btn small ghost', type: 'button', onclick: () => { topic.value = d.ntfy_suggest; topic.select(); } }, 'Random topic'),
        h('button', { class: 'btn small', type: 'submit' }, 'Save')));
  }

  function paint(d) {
    if (!d) return;
    for (const p of panels('phone')) {
      const live = d.token && (d.polling || d.last_poll);
      meta(p, d.error ? status('critical', 'Problem') : d.paired ? status('good', 'Paired') : live ? status('warning', 'Waiting to pair') : status('idle', 'Off'));
      const typing = document.activeElement;
      if (typing && typing.tagName === 'INPUT' && typing.value && body(p).contains(typing)) continue;  // don't wipe what's being typed
      fill(body(p),
        h('div', { class: 'ph-steps' }, tokenStep(d), pairStep(d), pushStep(d)),
        d.error ? h('div', { class: 't2 warn ph-err' }, d.error) : null,
        h('div', { class: 'controls' },
          h('button', { class: 'btn small', disabled: !(d.paired || d.ntfy), onclick: () => act('test', {}, r => `Sent by ${r.sent.join(' and ')}.`) }, 'Send test'),
          h('span', { class: 't2' }, d.away ? 'You look away from the PC right now.' : 'Pushes start after 5 minutes idle.')));
    }
  }

  Vesper.skill('phone', { panels: [{ view: 'setup', span: 4, title: 'Phone', after: 'mcp' }], render: paint });
})();
