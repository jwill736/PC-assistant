/* Conversation: talking with Vesper. J's bubbles in ion, Vesper's in ember, a box that continues the same
   conversation, Talk / End. While it's on, the body carries data-conversation so the orb says "In conversation".
   Vesper 2.2: the mode chips (ember: Vesper's persona), the thread it's on (iris: what Vesper remembers), an offer to
   pick up an earlier talk, the plan card (amber: needs J's yes), "what we covered" when it ends, and a drawer with the
   Threads list (resume / follow-ups / forget) and the style profile. */
(() => {
  const { h, fill, panels, body, meta, toast, post, ago } = Vesper.ui;
  const ENDED = { said: 'you wrapped it up', timeout: 'quiet for a minute', live: 'the stream went live', hud: 'ended here',
    stopped: 'stopped', 'no model': 'the model went away' };
  const COVERED = { saving: 'Saving to threads…', saved: 'Saved to threads', raw: 'Kept word for word (no model to summarise)',
    off_record: 'Off the record: nothing kept', on_stream: 'On air: not kept', short: 'Too short to keep',
    forgotten: 'Forgotten' };
  const STYLE_NAMES = { length: 'Length', humour: 'Humour', questions: 'Questions', formality: 'Tone' };
  const parts = new WeakMap();  // panel -> its nodes, built once so typing survives every update
  const armed = new Map();      // thread id -> forget armed until (two clicks to forget)
  let drawer = '';              // '' | 'threads' | 'style'
  try { drawer = localStorage.getItem('convo.drawer') || ''; } catch { /* private window */ }

  async function act(action, body, fail) {
    const r = await Vesper.act('conversation', action, body || {});
    if (r && r.ok === false) toast(r.error || fail || 'That didn’t work.', { source: 'vesper', level: 'warn' });
    return r || {};
  }
  const start = (onStream, mode) => act('start', { ...(onStream ? { on_stream: true } : {}), ...(mode ? { mode } : {}) },
    'Couldn’t start a conversation.');
  const end = () => act('end');
  async function send(input) {
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    try { await post('/api/command', { text }); } catch (err) { toast(`Couldn’t reach Vesper (${err.message}).`, { level: 'error' }); }
  }
  function setDrawer(which) {
    drawer = drawer === which ? '' : which;
    try { localStorage.setItem('convo.drawer', drawer); } catch { /* fine */ }
    Vesper.repaint('conversation');
  }

  function build(p) {
    const log = h('div', { class: 'convo-log', role: 'log', 'aria-live': 'polite', 'aria-label': 'Conversation' });
    const input = h('input', { class: 'convo-input', placeholder: 'Say something…', 'aria-label': 'Say something to Vesper' });
    const form = h('form', { class: 'convo-form', autocomplete: 'off', onsubmit: e => { e.preventDefault(); send(input); } },
      input, h('button', { class: 'btn primary', type: 'submit' }, 'Send'));
    const chip = h('span', { class: 'convo-chip' });
    const tags = h('div', { class: 'convo-tags' });
    const lede = h('div', { class: 'convo-lede' });
    const actions = h('div', { class: 'convo-actions' });
    const head = h('div', { class: 'convo-head' }, chip, tags, lede, actions);
    const modes = h('div', { class: 'convo-modes', role: 'group', 'aria-label': 'Conversation mode' });
    const offer = h('div', { class: 'convo-offer', role: 'status' });
    const plan = h('div', { class: 'convo-plan' });
    const covered = h('div', { class: 'convo-covered' });
    const foot = h('div', { class: 'convo-foot' });
    const drawerBox = h('div', { class: 'convo-drawer' });
    fill(body(p), head, modes, offer, log, plan, form, covered, foot, drawerBox);
    const n = { log, input, form, chip, tags, lede, actions, modes, offer, plan, covered, foot, drawerBox, key: '' };
    parts.set(p, n);
    return n;
  }

  function bubble(m, name) {
    const you = m.role === 'user';
    return h('div', { class: `msg ${you ? 'user' : 'assistant'} convo-msg` },
      m.text, h('span', { class: 'meta' }, `${you ? 'You' : name}${you && m.source === 'text' ? ' · typed' : ''}`));
  }

  // ---- the head: state, mode, thread, off the record --------------------------------------------------------------
  function tagsFor(d, on) {
    const out = [];
    if (on && d.mode && d.mode !== 'chat') out.push(h('span', { class: 'convo-tag mode' }, h('i', { 'aria-hidden': 'true' }), d.mode_label));
    if (d.thread && d.thread.title) out.push(h('span', { class: 'convo-tag thread', title: d.thread.resumed ? 'Continuing an earlier talk' : 'Kept as a thread' },
      h('i', { 'aria-hidden': 'true' }), d.thread.title, d.thread.resumed ? h('small', {}, 'resumed') : null));
    if (on && d.off_record) out.push(h('span', { class: 'convo-tag off' }, 'Off the record'));
    if (on && d.held) out.push(h('span', { class: 'convo-tag held' }, 'Thinking time'));
    return out;
  }

  function modeChips(d, on) {
    const list = (d.modes || []).filter(m => on || m.id !== 'chat');
    return [h('span', { class: 'convo-modes-k' }, on ? 'Mode' : 'Or start as'),
      ...list.map(m => h('button', {
        type: 'button', class: `convo-mode${on && d.mode === m.id ? ' on' : ''}`, 'aria-pressed': String(on && d.mode === m.id),
        disabled: !d.model, onclick: () => act('mode', { mode: m.id }),
      }, m.label))];
  }

  // ---- cards: the link offer, the plan, what we covered -----------------------------------------------------------
  function offerCard(d) {
    const o = d.link_offer;
    if (!o || !d.active) return [];
    return [h('span', { class: 'convo-offer-ic', 'aria-hidden': 'true' }, '↺'),
      h('span', { class: 'convo-offer-t' }, 'Sounds like ', h('b', {}, o.title), ` (${o.when}).`),
      h('button', { class: 'btn small convo-offer-yes', onclick: () => act('link', { id: o.id }) }, 'Pick it up'),
      h('button', { class: 'btn small ghost', onclick: () => act('unlink') }, 'Keep it new')];
  }

  function planCard(d) {
    const items = d.proposals || [];
    if (!items.length) return [];
    const boxes = items.map((t, i) => h('label', { class: 'convo-task' },
      h('input', { type: 'checkbox', checked: t.keep !== false, 'data-i': i }), h('span', {}, t.title)));
    const add = () => {
      const titles = boxes.filter(b => b.querySelector('input').checked).map(b => b.querySelector('span').textContent);
      if (!titles.length) return toast('Tick at least one task.', { source: 'vesper', level: 'warn' });
      act('add_tasks', { titles }).then(r => r.ok && toast(`Added ${titles.length} task${titles.length === 1 ? '' : 's'}.`,
        { source: 'vesper', level: 'good' }));
    };
    return [h('div', { class: 'convo-card-k' }, `Plan · ${items.length} task${items.length === 1 ? '' : 's'} to add?`),
      h('div', { class: 'convo-tasks' }, boxes),
      h('div', { class: 'convo-card-row' },
        h('button', { class: 'btn small primary', onclick: add }, 'Add tasks'),
        h('button', { class: 'btn small ghost', onclick: () => act('dismiss_tasks') }, 'Not now'),
        h('span', { class: 'convo-hint' }, 'Nothing is added until you say so. “Add them” works too.'))];
  }

  function list(label, items, cls) {
    if (!items || !items.length) return null;
    return h('div', { class: `convo-cov-col ${cls || ''}` }, h('div', { class: 'convo-cov-k' }, label),
      h('ul', {}, items.map(x => h('li', {}, typeof x === 'string' ? x : x.text))));
  }

  function coveredCard(d) {
    const c = d.covered;
    if (d.active || !c) return [];
    const kept = c.state === 'saved' || c.state === 'raw' || c.state === 'saving';
    return [h('div', { class: 'convo-card-row convo-cov-head' },
      h('span', { class: 'convo-card-k' }, 'What we covered'),
      c.title ? h('b', { class: 'convo-cov-title' }, c.title) : null,
      h('span', { class: `convo-cov-state s-${c.state}` }, COVERED[c.state] || c.state),
      h('span', { class: 'grow' }),
      kept && c.thread_id ? h('button', { class: 'btn small ghost', onclick: () => forget(c.thread_id, c.title) }, 'Forget it') : null,
      h('button', { class: 'icon-btn convo-x', 'aria-label': 'Dismiss', onclick: () => act('dismiss_covered') }, '✕')),
    c.summary ? h('p', { class: 'convo-cov-sum' }, c.summary) : null,
    (c.points || c.stances || c.open || c.promises) ? h('div', { class: 'convo-cov-grid' },
      list('Key points', c.points), list('Your views', c.stances), list('Still open', c.open, 'open'),
      list('I’ll come back to', c.promises)) : null,
    c.learned && c.learned.length ? h('div', { class: 'convo-hint' },
      `Remembered ${c.learned.length} thing${c.learned.length === 1 ? '' : 's'}: ${c.learned[0]}`) : null];
  }

  // ---- the drawer: threads and style ------------------------------------------------------------------------------
  async function forget(id, title) {
    const until = armed.get(id) || 0;
    if (Date.now() > until) {
      armed.set(id, Date.now() + 4000);
      toast(`Click Forget again to delete “${title}” and what it taught me.`, { source: 'vesper', level: 'warn' });
      setTimeout(() => Vesper.repaint('conversation'), 4100);
      return Vesper.repaint('conversation');
    }
    armed.delete(id);
    const r = await act('forget', { id });
    if (r.ok) toast(`Forgot “${r.title}”${r.facts && r.facts.length ? ` and ${r.facts.length} thing${r.facts.length === 1 ? '' : 's'} it taught me` : ''}.`,
      { source: 'vesper', level: 'good' });
  }

  function threadRow(t, d) {
    const isArmed = (armed.get(t.id) || 0) > Date.now();
    const current = d.thread && d.thread.id === t.id && d.active;
    const metaBits = [ago(t.updated), `${t.talks} talk${t.talks === 1 ? '' : 's'}`, t.mode && t.mode !== 'chat'
      ? ((d.modes || []).find(m => m.id === t.mode) || {}).label : null].filter(Boolean);
    return h('li', { class: `convo-thread${current ? ' current' : ''}` },
      h('div', { class: 'main' },
        h('div', { class: 't1' }, t.title, t.open ? h('span', { class: 'convo-open', title: 'Open questions' }, `${t.open} open`) : null,
          t.raw ? h('span', { class: 'convo-raw', title: 'Kept word for word: no summary' }, 'raw') : null),
        h('div', { class: 't2' }, metaBits.join(' · ')),
        t.summary ? h('div', { class: 't3' }, t.summary) : null),
      h('div', { class: 'convo-thread-acts' },
        h('button', { class: 'btn small', disabled: current || !d.model || d.live, onclick: () => act('resume', { id: t.id }) },
          current ? 'Talking' : 'Resume'),
        h('button', { class: `btn small ghost convo-bell${t.followups ? ' on' : ''}`, 'aria-pressed': String(!!t.followups),
          title: t.followups ? 'Follow-ups on: I may check in once on what’s open. Click to stop for good.'
            : 'Follow-ups off for this talk. Click to turn them back on.',
          onclick: () => act('followups', { id: t.id, on: !t.followups }) }, 'Follow-ups'),
        h('button', { class: `btn small ${isArmed ? 'danger' : 'ghost'}`, onclick: () => forget(t.id, t.title) },
          isArmed ? 'Sure?' : 'Forget')));
  }

  function threadsPane(d) {
    if (d.live) return h('div', { class: 'empty' }, `${d.thread_count || 0} threads · hidden while you’re live.`);
    const rows = d.threads || [];
    if (!rows.length) return h('div', { class: 'empty' }, 'No threads yet. Every conversation is kept here (unless it’s off the record), and “let’s continue our talk about…” picks one back up.');
    return h('ul', { class: 'convo-threads' }, rows.map(t => threadRow(t, d)));
  }

  function stylePane(d) {
    const st = d.style || {};
    const rows = Object.keys(STYLE_NAMES).map(k => h('div', { class: 'set-row convo-style-row' },
      h('span', { class: 'k' }, STYLE_NAMES[k]),
      h('div', { class: 'seg convo-seg', role: 'group', 'aria-label': STYLE_NAMES[k] }, (st.labels?.[k] || []).map((lab, i) =>
        h('button', { type: 'button', 'aria-pressed': String(st[k] === i), class: st.default?.[k] === i ? 'dflt' : null,
          onclick: () => act('style', { dim: k, value: i }) }, lab)))));
    const last = (st.changed || []).slice(-1)[0];
    return h('div', { class: 'convo-style' }, rows,
      h('div', { class: 'convo-card-row' },
        h('span', { class: 'convo-hint' }, last ? `Last change: “${last.said || 'HUD'}” · ${ago(last.at)}`
          : 'Learns from what you say in a conversation: “too long”, “more detail”, “be funnier”, “stop asking me questions”.'),
        h('span', { class: 'grow' }),
        h('button', { class: 'btn small ghost', onclick: () => act('style_reset') }, 'Reset')));
  }

  function footer(d) {
    const n = d.thread_count || 0;
    const open = (d.threads || []).reduce((a, t) => a + (t.open || 0), 0);
    const tab = (id, label, sub) => h('button', { type: 'button', class: `convo-tab${drawer === id ? ' on' : ''}`,
      'aria-expanded': String(drawer === id), onclick: () => setDrawer(id) }, h('b', {}, label), h('span', {}, sub));
    return [tab('threads', 'Threads', `${n}${open && !d.live ? ` · ${open} open` : ''}`), tab('style', 'Style', d.style?.words || '')];
  }

  // ---- render -----------------------------------------------------------------------------------------------------
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
      fill(n.tags, tagsFor(d, on));
      fill(n.lede, on
        ? (d.held ? 'Take your time: the quiet minute is on hold.'
          : `No need to say “${name}” — just talk. Say “that’s all” to finish${d.voice ? ', or stay quiet for a minute' : ''}.`)
        : d.available === false ? 'Conversation mode isn’t available.'
        : !d.model ? 'Needs a model to talk with: set one up in Setup → Brain.'
        : d.live ? `You’re live. “${name}, let’s talk on stream” keeps it public.`
        : `Say “${name}, let’s talk” — or “let’s continue our talk about…” to pick one back up.`);
      const key = `${on}|${!!d.live}|${!!d.model}`;
      if (n.key !== key) {
        n.key = key;
        fill(n.actions, on
          ? h('button', { class: 'btn small convo-end', onclick: end }, 'End conversation')
          : d.live
            ? h('button', { class: 'btn small convo-air-btn', disabled: !d.model, onclick: () => start(true) }, 'Talk on stream')
            : h('button', { class: 'btn small convo-start', disabled: !d.model, onclick: () => start(false) }, 'Talk'));
      }
      fill(n.modes, d.model && d.available !== false && !(d.live && !on) ? modeChips(d, on) : []);
      n.modes.hidden = !n.modes.childNodes.length;
      fill(n.offer, offerCard(d));
      n.offer.hidden = !n.offer.childNodes.length;
      fill(n.plan, planCard(d));
      n.plan.hidden = !n.plan.childNodes.length;
      fill(n.covered, coveredCard(d));
      n.covered.hidden = !n.covered.childNodes.length;
      n.covered.dataset.state = d.covered?.state || '';
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
      // the drawer: threads and style
      const avail = d.available !== false;
      fill(n.foot, avail ? footer(d) : []);
      n.foot.hidden = !avail;
      fill(n.drawerBox, !avail ? [] : drawer === 'threads' ? threadsPane(d) : drawer === 'style' ? stylePane(d) : []);
      n.drawerBox.hidden = !n.drawerBox.childNodes.length;
    }
  }

  Vesper.skill('conversation', {
    panels: [{ view: 'command', span: 12, title: 'Conversation mode', first: true }],
    render,
  });
})();
