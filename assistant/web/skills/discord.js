/* Discord: webhook status, the go-live switch, a preview of the next go-live post, test / manual posts (each asks
   first: they're public), and what went out recently. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast } = Vesper.ui;
  const KIND = { golive: 'go-live', manual: 'announcement', clip: 'clip', test: 'test' };
  const act = async (action, payload, ok) => { const r = await Vesper.act('discord', action, payload); toast(r.ok ? ok : r.error); return r; };
  const ask = (q, fn) => () => { if (window.confirm(q)) fn(); };  // posts are public: a click still gets a yes

  function rich(text) {  // **bold** the way Discord shows it; everything else stays plain text
    return text.split('\n').map((line, i) => [i ? h('br') : null,
      line.split(/(\*\*[^*]+\*\*)/).map(part => part.startsWith('**') && part.endsWith('**') ? h('strong', {}, part.slice(2, -2)) : part)]);
  }

  function setup(d) {
    const url = h('input', { type: 'password', autocomplete: 'off', placeholder: 'https://discord.com/api/webhooks/…', 'aria-label': 'Discord webhook URL' });
    return h('form', { class: 'addrow', onsubmit: async e => {
      e.preventDefault(); const r = await act('webhook', { url: url.value }, 'Webhook saved.'); if (r.ok) url.value = '';
    } }, url, h('button', { class: 'btn small', type: 'submit' }, d.configured ? 'Replace' : 'Save'));
  }

  Vesper.skill('discord', {
    panels: [{ view: 'stream', span: 4, title: 'Discord', after: 'youtube' }],
    render(d) {
      for (const p of panels('discord')) {
        const b = body(p);
        if (!d) { fill(b, empty('Loading…')); continue; }
        if (b.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') continue;
        if (!d.configured) { meta(p, 'not set up'); fill(b, h('div', { class: 't2' }, d.next_step), setup(d)); continue; }
        meta(p, d.hook ? `webhook “${d.hook.name}”` : 'webhook saved');
        const auto = h('input', { type: 'checkbox', checked: d.auto_announce || null, onchange: e => act('auto', { on: e.target.checked }, e.target.checked ? 'Go-live posts on.' : 'Go-live posts off.') });
        const text = h('input', { placeholder: 'Announce something…', 'aria-label': 'Announcement', maxlength: 2000 });
        const mention = h('input', { placeholder: '@everyone, @here or <@&role id>', value: d.mention || '', 'aria-label': 'Go-live mention' });
        const tpl = h('input', { placeholder: '**{name} is live!** {title}{game}\\n{link}', 'aria-label': 'Go-live template' });
        fill(b,
          h('label', { class: 'dc-switch t2' }, auto, h('span', { class: 'dc-track', 'aria-hidden': 'true' }, h('i')), 'Post when I go live'),
          h('div', { class: 'sub-h' }, 'Next go-live post'),
          h('div', { class: 'dc-preview' }, h('span', { class: 'dc-avatar', 'aria-hidden': 'true' }),
            h('div', { class: 'dc-msg' }, h('div', { class: 't3 muted' }, d.hook?.name || 'Webhook', h('span', { class: 'dc-tag' }, 'APP')),
              h('div', { class: 'dc-text' }, d.mention ? h('span', { class: 'dc-ping' }, d.mention) : null, d.mention ? ' ' : null, rich(d.preview || '')))),
          h('div', { class: 'controls dc-actions' },
            h('button', { class: 'btn small', onclick: ask('Post a test message to your Discord channel?', () => act('test', {}, 'Test posted.')) }, 'Test post'),
            h('button', { class: 'btn small', onclick: ask('Post the go-live message to Discord now?', () => act('golive', {}, 'Go-live posted.')) }, 'Post go-live now')),
          h('form', { class: 'addrow', onsubmit: e => { e.preventDefault(); if (!text.value.trim()) return;
            ask(`Post this to Discord?\n\n${text.value}`, async () => { const r = await act('post', { text: text.value }, 'Posted.'); if (r.ok) text.value = ''; })(); } },
            text, h('button', { class: 'btn small', type: 'submit' }, 'Post')),
          d.error ? h('div', { class: 't3 dc-warn' }, d.error) : null,
          (d.posts || []).length ? h('div', { class: 'list dc-posts' }, d.posts.slice(0, 5).map(x => h('div', { class: 'item' },
            h('div', { class: 'main' }, h('div', { class: 't3 muted' }, `${KIND[x.kind] || x.kind} · ${ago(x.at)}`), h('div', { class: 't2 dc-line' }, rich(x.text.replace(/\n/g, ' '))))))) : null,
          h('details', { class: 'dc-more' }, h('summary', { class: 't3 muted' }, 'Mention, message, webhook'),
            h('form', { class: 'addrow', onsubmit: e => { e.preventDefault(); act('mention', { mention: mention.value }, 'Saved.'); } }, mention, h('button', { class: 'btn small', type: 'submit' }, 'Save')),
            h('form', { class: 'addrow', onsubmit: e => { e.preventDefault(); act('template', { template: tpl.value.replace(/\\n/g, '\n') }, tpl.value ? 'Saved.' : 'Back to the default.'); } }, tpl, h('button', { class: 'btn small', type: 'submit' }, 'Save')),
            setup(d)));
      }
    },
  });
})();
