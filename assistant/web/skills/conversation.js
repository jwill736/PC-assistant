/* Conversation: talking with Vesper. J's bubbles in ion, Vesper's in ember, a box that continues the same
   conversation, Talk / End. While it's on, the body carries data-conversation so the orb says "In conversation". */
(() => {
  const { h, fill, panels, body, meta, toast, post, ago } = Vesper.ui;
  const ENDED = { said: 'you wrapped it up', timeout: 'quiet for a minute', live: 'the stream went live', hud: 'ended here',
    stopped: 'stopped', 'no model': 'the model went away' };
  const parts = new WeakMap();  // panel -> its nodes, built once so typing survives every update

  async function start(onStream) {
    const r = await Vesper.act('conversation', 'start', onStream ? { on_stream: true } : {});
    if (r.ok === false) toast(r.error || 'Couldn’t start a conversation.', { source: 'vesper', level: 'warn' });
  }
  async function end() { await Vesper.act('conversation', 'end'); }
  async function send(input) {
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    try { await post('/api/command', { text }); } catch (err) { toast(`Couldn’t reach Vesper (${err.message}).`, { level: 'error' }); }
  }

  function build(p) {
    const log = h('div', { class: 'convo-log', role: 'log', 'aria-live': 'polite', 'aria-label': 'Conversation' });
    const input = h('input', { class: 'convo-input', placeholder: 'Say something…', 'aria-label': 'Say something to Vesper' });
    const form = h('form', { class: 'convo-form', autocomplete: 'off', onsubmit: e => { e.preventDefault(); send(input); } },
      input, h('button', { class: 'btn primary', type: 'submit' }, 'Send'));
    const chip = h('span', { class: 'convo-chip' });
    const lede = h('div', { class: 'convo-lede' });
    const actions = h('div', { class: 'convo-actions' });
    const head = h('div', { class: 'convo-head' }, chip, lede, actions);
    fill(body(p), head, log, form);
    const n = { log, input, form, chip, lede, actions, key: '' };
    parts.set(p, n);
    return n;
  }

  function bubble(m, name) {
    const you = m.role === 'user';
    return h('div', { class: `msg ${you ? 'user' : 'assistant'} convo-msg` },
      m.text, h('span', { class: 'meta' }, `${you ? 'You' : name}${you && m.source === 'text' ? ' · typed' : ''}`));
  }

  function render(d) {
    d = d || {};
    const on = !!d.active;
    document.body.dataset.conversation = on ? (d.on_stream ? 'air' : 'on') : '';
    const name = Vesper.data.assistant?.name || 'Vesper';
    for (const p of panels('conversation')) {
      const n = parts.get(p) || build(p);
      p.classList.toggle('convo-on', on);
      p.classList.toggle('convo-air', on && !!d.on_stream);
      const shown = (d.transcript || []).slice(-30);
      const recent = !on && d.ended && Date.now() / 1000 - d.ended.ts < 600;
      // header: what state it's in, what to say, the buttons
      fill(n.chip, h('span', { class: 'dot' }), on ? (d.on_stream ? 'In conversation · on air' : 'In conversation') : 'Talk');
      n.chip.className = `convo-chip ${on ? (d.on_stream ? 'air' : 'on') : ''}`;
      fill(n.lede, on
        ? `No need to say “${name}” — just talk. Say “that’s all” to finish${d.voice ? ', or stay quiet for a minute' : ''}.`
        : d.available === false ? 'Conversation mode isn’t available.'
        : !d.model ? 'Needs a model to talk with: set one up in Setup → Brain.'
        : d.live ? `You’re live. “${name}, let’s talk on stream” keeps it public.`
        : `Say “${name}, let’s talk” — then no wake word until you say “that’s all”.`);
      const key = `${on}|${!!d.live}|${!!d.model}`;
      if (n.key !== key) {
        n.key = key;
        fill(n.actions, on
          ? h('button', { class: 'btn small convo-end', onclick: end }, 'End conversation')
          : d.live
            ? h('button', { class: 'btn small convo-air-btn', disabled: !d.model, onclick: () => start(true) }, 'Talk on stream')
            : h('button', { class: 'btn small convo-start', disabled: !d.model, onclick: () => start(false) }, 'Talk'));
      }
      meta(p, on ? `${d.turns || 0} turn${d.turns === 1 ? '' : 's'}${d.summarised ? ' · earlier part summarised' : ''}`
        : recent ? `ended ${ago(d.ended.ts)}: ${ENDED[d.ended.reason] || d.ended.reason}` : '');
      // the transcript (kept after it ends for a few minutes, faded)
      const show = on || recent;
      n.log.hidden = !show || (!shown.length && !d.thinking);
      n.form.hidden = !on;
      p.classList.toggle('convo-past', !on && !!recent);
      if (show) {
        const atBottom = n.log.scrollHeight - n.log.scrollTop - n.log.clientHeight < 40;
        fill(n.log, shown.map(m => bubble(m, name)),
          on && d.thinking ? h('div', { class: 'msg assistant convo-typing', 'aria-label': `${name} is thinking` },
            h('i'), h('i'), h('i')) : null);
        if (atBottom || on) n.log.scrollTop = n.log.scrollHeight;
      }
    }
  }

  Vesper.skill('conversation', {
    panels: [{ view: 'command', span: 12, title: 'Conversation mode', first: true }],
    render,
  });
})();
