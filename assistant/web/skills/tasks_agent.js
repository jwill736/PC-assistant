/* Jobs (skills/tasks_agent.py): several multi-step jobs at once, on the Command tab.
   Each job is a card: its state, the goal, a step timeline (running in iris, done in phosphor, failed in alert, waiting
   for J in amber), progress and elapsed time, Pause / Priority / Cancel, the ONE approval card for everything it would
   change, and the result with Copy. Above them: a box for a job in your own words and the Recipes launcher (a tile per
   ready-made job; tiles that need something, like a topic, open an input). Claude Code jobs from the Background jobs
   panel show here too with a kind badge, so "what's running" is one place; that panel keeps working as before. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast, post } = Vesper.ui;
  const STATE = {
    queued: ['Queued', 'muted'], planning: ['Planning', 'iris'], running: ['Running', 'iris'], paused: ['Paused', 'muted'],
    waiting: ['Needs your OK', 'amber'], done: ['Done', 'phosphor'], failed: ['Failed', 'alert'],
    cancelled: ['Cancelled', 'muted'], interrupted: ['Interrupted', 'amber'],
  };
  const STEP = { pending: 'Not started', running: 'Running', done: 'Done', failed: 'Failed', skipped: 'Skipped',
    waiting: 'Waiting for your OK', cancelled: 'Cancelled' };
  const ICON = {  // 16px strokes, like the HUD's own
    sun: 'M8 5a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM8 1.5v1.8M8 12.7v1.8M1.5 8h1.8M12.7 8h1.8M3.4 3.4l1.3 1.3M11.3 11.3l1.3 1.3M3.4 12.6l1.3-1.3M11.3 4.7l1.3-1.3',
    moon: 'M12.5 9.5A5 5 0 0 1 6.5 3.5a5 5 0 1 0 6 6z',
    people: 'M5.5 7a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM1.5 13a4 4 0 0 1 8 0M11 7.5a1.6 1.6 0 1 0 0-3.2M11.5 9.5a3.5 3.5 0 0 1 3 3.5',
    mail: 'M2 4h12v8.5H2zM2 4.5l6 4.5 6-4.5',
    chart: 'M2.5 13.5h11M4 11V8M7 11V5M10 11V7M13 11V3.5',
    stream: 'M2 4h12v8H2zM6.5 6.2v3.6L9.6 8z',
    search: 'M7 2.5a4.5 4.5 0 1 0 0 9 4.5 4.5 0 0 0 0-9zM10.3 10.3l3.2 3.2',
    calendar: 'M2.5 4h11v9.5h-11zM2.5 7h11M5.5 2.5v3M10.5 2.5v3',
    money: 'M2 4.5h12v7H2zM8 6.2a1.8 1.8 0 1 0 0 3.6 1.8 1.8 0 0 0 0-3.6zM4 8h.01M12 8h.01',
    plane: 'M2 9.5l12-5-3.5 9-2.5-3.5zM8 10l-1.5 3.5',
    spark: 'M8 1.5l1.6 4.9 4.9 1.6-4.9 1.6L8 14.5l-1.6-4.9L1.5 8l4.9-1.6z',
    book: 'M2.5 3h4a1.5 1.5 0 0 1 1.5 1.5V13a1.2 1.2 0 0 0-1.2-1.2H2.5zM13.5 3h-4A1.5 1.5 0 0 0 8 4.5V13a1.2 1.2 0 0 1 1.2-1.2h4.3z',
    gear: 'M8 5.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zM8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4',
    job: 'M8 1.5 13.6 4.75v6.5L8 14.5 2.4 11.25v-6.5zM5.5 6 8 10.5 10.5 6',
  };
  const ui = { open: new Set(), full: new Set(), unticked: {}, picked: null, recipesOpen: null };
  const parts = new WeakMap();  // panel -> its fixed nodes (typing survives every update)

  function svg(name) {
    const NS = 'http://www.w3.org/2000/svg';
    const s = document.createElementNS(NS, 'svg');
    s.setAttribute('viewBox', '0 0 16 16'); s.setAttribute('aria-hidden', 'true'); s.setAttribute('class', 'ta-ic');
    const p = document.createElementNS(NS, 'path'); p.setAttribute('d', ICON[name] || ICON.job);
    s.append(p);
    return s;
  }
  function dur(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    const m = Math.floor(sec / 60), s = sec % 60;
    return m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m` : `${m}:${String(s).padStart(2, '0')}`;
  }
  async function act(action, payload, okText) {
    const r = await Vesper.act('tasks_agent', action, payload);
    if (r && r.ok === false) toast(r.error || 'That didn’t work.', { level: 'warn', source: 'tasks_agent' });
    else if (okText) toast(okText, { level: 'good', source: 'tasks_agent' });
    return r;
  }

  // ---- the fixed part: a box for your own job, the recipes ---------------------------------------------------
  function build(p) {
    const input = h('input', { class: 'ta-goal-in', placeholder: 'Give Vesper a job… e.g. research the best 4K capture cards under $300',
      'aria-label': 'A job for Vesper, in your words' });
    const just = h('input', { type: 'checkbox', class: 'ta-just' });
    const form = h('form', { class: 'ta-new', autocomplete: 'off', onsubmit: async e => {
      e.preventDefault();
      const goal = input.value.trim();
      if (!goal) return input.focus();
      const r = await act('start', { goal, preapproved: just.checked });
      if (r && r.ok !== false) { input.value = ''; just.checked = false; }
    } }, input,
      h('label', { class: 'ta-just-l', title: 'Drafts, tasks and reminders for you go ahead without the approval card. Anything public still waits.' },
        just, 'Just do it'),
      h('button', { class: 'btn primary', type: 'submit' }, 'Start'));
    const toggle = h('button', { class: 'btn small ghost ta-rtoggle', type: 'button', 'aria-expanded': 'true',
      onclick: () => { ui.recipesOpen = !isOpen(); render(Vesper.data.tasks_agent); } }, 'Recipes');
    const recipes = h('div', { class: 'ta-recipes', role: 'list', 'aria-label': 'Ready-made jobs' });
    const others = h('div', { class: 'ta-others' });
    const list = h('div', { class: 'ta-list', 'aria-live': 'polite' });
    fill(body(p), h('div', { class: 'ta-top' }, form, toggle), recipes, others, list);
    const n = { input, form, toggle, recipes, others, list, recipeKey: '' };
    parts.set(p, n);
    return n;
  }
  function isOpen() {
    const d = Vesper.data.tasks_agent || {};
    return ui.recipesOpen ?? !(d.jobs || []).length;  // open until there's a job to look at
  }

  function tile(r, n) {
    const picked = ui.picked === r.id;
    const needs = (r.inputs || []).filter(i => !i.optional);
    const go = async extra => {
      const r2 = await act('start', { recipe: r.id, inputs: extra || {} });
      if (r2 && r2.ok !== false) { ui.picked = null; n.recipeKey = ''; render(Vesper.data.tasks_agent); }
    };
    const head = h('button', { class: 'ta-tile-b', type: 'button', 'aria-expanded': needs.length ? String(picked) : null,
      onclick: () => {
        if (!(r.inputs || []).length) return go();
        ui.picked = picked ? null : r.id; n.recipeKey = ''; render(Vesper.data.tasks_agent);
        if (ui.picked) setTimeout(() => n.recipes.querySelector('.ta-tile.picked input')?.focus(), 0);
      } },
      h('span', { class: 'ta-tile-h' }, svg(r.icon), h('b', {}, r.title),
        r.needs_model ? h('span', { class: 'pill ta-ai', title: 'Writes it up with the model' }, 'AI') : null,
        r.custom ? h('span', { class: 'pill', title: 'Yours, from data/recipes.yaml' }, 'yours') : null),
      h('span', { class: 'ta-tile-t' }, r.blurb));
    let form = null;
    if (picked) {
      const ins = (r.inputs || []).map(i => h('input', { name: i.name, placeholder: i.placeholder || i.label, 'aria-label': i.label,
        required: i.optional ? null : true }));
      form = h('form', { class: 'ta-tile-f', autocomplete: 'off', onsubmit: e => {
        e.preventDefault();
        const vals = Object.fromEntries(ins.map(x => [x.name, x.value.trim()]));
        go(vals);
      } }, ins, h('button', { class: 'btn small primary', type: 'submit' }, 'Start'));
    }
    return h('div', { class: `ta-tile${picked ? ' picked' : ''}`, role: 'listitem' }, head, form);
  }

  // ---- one job ------------------------------------------------------------------------------------------------
  function stepRow(s) {
    const extra = s.state === 'failed' || s.state === 'skipped' ? s.error : s.count > 1 ? `×${s.count}` : s.ms != null && s.state === 'done' ? `${(s.ms / 1000).toFixed(1)} s` : '';
    return h('li', { class: `ta-step st-${s.state}` },
      h('span', { class: 'ta-dot', role: 'img', 'aria-label': STEP[s.state] || s.state }),
      h('span', { class: 'ta-step-t' }, s.title),
      extra ? h('span', { class: 'ta-step-m', title: extra }, extra) : null);
  }

  function approvalCard(j) {
    const waiting = (j.approvals || []).filter(a => a.state === 'waiting');
    if (!waiting.length) return null;
    const off = ui.unticked[j.id] || (ui.unticked[j.id] = new Set());
    const count = () => waiting.filter(a => !off.has(a.id)).length;
    const btn = h('button', { class: 'btn small primary ta-approve' }, `Approve ${count()}`);
    btn.onclick = async () => {
      const items = waiting.filter(a => !off.has(a.id)).map(a => a.id);
      if (!items.length) return toast('Tick at least one, or Discard.', { level: 'warn', source: 'tasks_agent' });
      btn.disabled = true;
      const r = await act('approve', { id: j.id, items });
      if (r && r.ok !== false) toast(`On it: ${r.doing}.`, { level: 'good', source: 'tasks_agent' });
      btn.disabled = false;
    };
    return h('div', { class: 'ta-approval', role: 'group', 'aria-label': `${j.title}: waiting for your OK` },
      h('div', { class: 'ta-approval-h' }, h('b', {}, 'Ready'), ` · ${j.batch}. Nothing has gone anywhere yet.`),
      h('ul', { class: 'ta-approval-l' }, waiting.map(a => {
        const box = h('input', { type: 'checkbox', checked: !off.has(a.id) || null, 'aria-label': a.text });
        box.onchange = () => { box.checked ? off.delete(a.id) : off.add(a.id); btn.textContent = `Approve ${count()}`; };
        return h('li', {}, h('label', {}, box, h('span', {}, a.text)),
          a.tier >= 2 ? h('span', { class: 'pill ta-public', title: 'Public: others will see this' }, 'public') : null);
      })),
      h('div', { class: 'controls ta-approval-b' }, btn,
        h('button', { class: 'btn small ghost', onclick: () => act('reject', { id: j.id }, 'Discarded. Nothing was sent or saved.') }, 'Discard')));
  }

  function result(j) {
    const r = j.result;
    if (!r) return j.error ? h('div', { class: 'ta-error', role: 'alert' }, j.error) : null;
    const full = ui.full.has(j.id);
    return h('div', { class: 'ta-result' },
      r.summary ? h('p', { class: 'ta-summary' }, r.summary) : null,
      full ? h('pre', { class: 'ta-md', tabindex: '0' }, r.markdown) : null,
      h('div', { class: 'controls ta-result-b' },
        r.markdown ? h('button', { class: 'btn small', 'aria-expanded': String(full),
          onclick: () => { full ? ui.full.delete(j.id) : ui.full.add(j.id); render(Vesper.data.tasks_agent); } }, full ? 'Less' : 'Full write-up') : null,
        r.markdown ? h('button', { class: 'btn small', onclick: async () => {
          try { await navigator.clipboard.writeText(r.markdown); toast('Copied the write-up.', { level: 'good', source: 'tasks_agent' }); }
          catch { toast('The browser wouldn’t copy; open the write-up and select it.', { level: 'warn' }); }
        } }, 'Copy') : null,
        r.note ? h('button', { class: 'btn small ghost', title: r.note, onclick: () => act('open_note', { id: j.id }) }, 'Open note') : null),
      (j.notes || []).length ? h('div', { class: 't2 ta-notes' }, j.notes.join(' ')) : null);
  }

  function card(j) {
    const [label, tone] = STATE[j.state] || [j.state, 'muted'];
    const active = ['queued', 'planning', 'running', 'paused', 'waiting'].includes(j.state);
    const open = ui.open.has(j.id) || ['running', 'planning'].includes(j.state);
    const p = j.progress || { done: 0, total: 0 };
    const pct = p.total ? Math.round(100 * p.done / p.total) : 0;
    const now = (j.steps || []).find(s => s.state === 'running');
    const time = h('span', { class: 'ta-time', 'data-base': j.active_s || 0, 'data-since': j.running_since || '' }, dur(j.elapsed_s));
    const buttons = [];
    if (['queued', 'planning', 'running'].includes(j.state) && !j.paused) buttons.push(h('button', { class: 'btn small', onclick: () => act('pause', { id: j.id }) }, 'Pause'));
    if (j.state === 'paused' || j.paused || j.state === 'interrupted') buttons.push(h('button', { class: 'btn small', onclick: () => act('resume', { id: j.id }) }, 'Resume'));
    if (['queued', 'planning', 'running', 'paused'].includes(j.state)) buttons.push(h('button', { class: `btn small${j.priority > 0 ? ' ta-pri-on' : ''}`, 'aria-pressed': String(j.priority > 0),
      title: 'Its model calls go first', onclick: () => act('priority', { id: j.id }) }, j.priority > 0 ? 'Priority ★' : 'Priority'));
    if (active) buttons.push(h('button', { class: 'btn small danger', onclick: () => act('cancel', { id: j.id }) }, 'Cancel'));
    else buttons.push(h('button', { class: 'btn small ghost', onclick: () => act('dismiss', { id: j.id }) }, 'Dismiss'));
    return h('article', { class: `ta-card s-${j.state} t-${tone}`, 'aria-label': `${j.title}: ${label}` },
      h('div', { class: 'ta-card-h' },
        h('span', { class: `ta-chip ${tone}` }, h('span', { class: 'ta-dot' }), label),
        h('div', { class: 'ta-card-t' },
          h('div', { class: 'ta-title' }, j.title, j.priority > 0 ? h('span', { class: 'pill ta-pri' }, 'priority') : null,
            j.preapproved ? h('span', { class: 'pill', title: 'You said “just do it”: drafts and tasks for you went ahead' }, 'pre-approved') : null),
          j.goal && j.goal !== j.title ? h('div', { class: 't2 ta-goal' }, j.goal) : null),
        h('div', { class: 'ta-card-m' }, time, h('span', { class: 't2' }, j.finished ? ago(j.finished) : j.created ? `started ${ago(j.created)}` : ''))),
      h('div', { class: 'ta-progress', role: 'progressbar', 'aria-valuenow': pct, 'aria-valuemin': 0, 'aria-valuemax': 100,
        'aria-label': `${p.done} of ${p.total} steps` },
        h('i', { style: { transform: `scaleX(${pct / 100})` } })),
      h('div', { class: 'ta-sub' },
        h('span', { class: 't2' }, `${p.done}/${p.total} steps`, now ? ` · ${now.title}` : '', j.model_calls ? ` · ${j.model_calls} model call${j.model_calls === 1 ? '' : 's'}` : ''),
        h('button', { class: 'btn small ghost ta-steps-b', 'aria-expanded': String(open),
          onclick: () => { ui.open.has(j.id) ? ui.open.delete(j.id) : ui.open.add(j.id); render(Vesper.data.tasks_agent); } }, open ? 'Hide steps' : 'Steps')),
      open ? h('ol', { class: 'ta-steps' }, (j.steps || []).map(stepRow)) : null,
      approvalCard(j),
      result(j),
      h('div', { class: 'controls ta-card-b' }, buttons));
  }

  function others(n) {
    const jobs = (Vesper.data.jobs || []).filter(j => ['queued', 'running'].includes(j.status));
    fill(n.others, jobs.length ? h('div', { class: 'ta-cc' }, jobs.slice(0, 4).map(j =>
      h('div', { class: 'ta-cc-row' },
        h('span', { class: 'pill ta-kind' }, j.kind === 'claude_code' ? 'Claude Code' : 'Research · Claude'),
        h('span', { class: 'ta-cc-t' }, j.title),
        h('span', { class: 't2' }, j.status),
        h('button', { class: 'btn small ghost', onclick: () => post(`/api/jobs/${j.id}/cancel`).catch(() => {}) }, 'Stop')))) : null);
  }

  function render(d) {
    for (const p of panels('tasks_agent')) {
      const n = parts.get(p) || build(p);
      if (!d) { fill(n.list, empty('Loading…')); continue; }
      const jobs = d.jobs || [];
      const c = d.counts || {};
      const busy = (c.running || 0) + (c.planning || 0);
      meta(p, [busy ? `${busy} running` : '', c.queued ? `${c.queued} queued` : '', c.waiting ? `${c.waiting} need you` : '',
        `${d.max_parallel || 3} at a time`].filter(Boolean).join(' · '));
      // recipes: rebuilt only when they or the picked tile change (an input being typed in survives)
      const openR = isOpen();
      n.toggle.setAttribute('aria-expanded', String(openR));
      n.toggle.textContent = openR ? 'Hide recipes' : 'Recipes';
      const key = `${openR}|${ui.picked}|${(d.recipes || []).map(r => r.id).join(',')}`;
      if (n.recipeKey !== key) {
        n.recipeKey = key;
        fill(n.recipes, openR ? (d.recipes || []).map(r => tile(r, n)) : null);
        n.recipes.hidden = !openR;
      }
      others(n);
      fill(n.list,
        !d.model ? h('div', { class: 'ta-hint' }, 'No model connected: ready-made jobs still run and list what they found; ones marked AI and jobs in your own words need the model (Setup → Brain).') : null,
        (d.problems || []).length ? h('div', { class: 'ta-hint warn' }, `Your recipes: ${d.problems.join('; ')}`) : null,
        jobs.length ? jobs.map(card) : empty('No jobs yet. Pick a recipe, type one above, or say “Vesper, research … and email me a summary”.'));
    }
  }

  // elapsed time on running cards, once a second (text only; nothing moves)
  setInterval(() => {
    const now = Date.now() / 1000;
    for (const el of document.querySelectorAll('.ta-time[data-since]')) {
      const since = parseFloat(el.dataset.since);
      if (since) el.textContent = dur(parseFloat(el.dataset.base || 0) + now - since);
    }
  }, 1000);
  Vesper.on('job', () => { if (Vesper.data.tasks_agent !== undefined) Vesper.repaint('tasks_agent'); });

  Vesper.skill('tasks_agent', { panels: [{ view: 'command', span: 12, title: 'Jobs', after: 'jobs' }], render });
})();
