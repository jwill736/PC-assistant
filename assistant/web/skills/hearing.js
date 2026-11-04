/* Hearing (Setup): the voice test (your commands and your background, replayed through the live chain, with
   suggested settings that change nothing until Apply) and the words Vesper has learned to hear right
   (skills/hearing.py, voice/bench.py, voice/vocab.py). */
(() => {
  const { h, fill, empty, panels, body, meta, toast, status, ago } = Vesper.ui;
  const NS = 'http://www.w3.org/2000/svg';
  const svg = (tag, attrs, ...kids) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null) e.setAttribute(k, v);
    for (const k of kids) if (k) e.append(k);
    return e;
  };
  const ui = { prep: null, live: null, busy: false, ticked: null };
  const STEP = { commands: 'Your commands', background: 'Background only', analysing: 'Working it out' };
  const act = (action, payload) => Vesper.act('hearing', action, payload);

  Vesper.skill('hearing', { panels: [{ view: 'setup', span: 12, title: 'Voice test · words I hear', after: 'speech' }], render: paint });

  Vesper.on('voice_bench', d => {
    const prev = ui.live;
    ui.live = { ...(d.level != null && prev ? prev : {}), ...d };
    const bar = document.querySelector('#hr-meter i');
    if (d.level != null && bar && prev && prev.prompt === d.prompt) bar.style.width = `${Math.min(100, d.level / 30)}%`;
    else if (Vesper.data.hearing) paint(Vesper.data.hearing);
  });
  Vesper.on('hearing_suggestion', d => {
    for (const s of d.items || []) {
      toast(`You typed “${s.meant}” where I heard “${s.heard}”. Always write it that way?`, {
        source: 'voice', life: 20000, actions: [
          { label: 'Remember', primary: true, run: () => add(s.heard, s.meant) },
          { label: 'No', run: () => act('dismiss', { id: s.id }) }] });
    }
  });

  async function add(heard, meant) {
    const r = await act('add', { heard, meant });
    toast(r.ok ? `Got it: “${r.heard}” → “${r.meant}”.` : r.error, { level: r.ok ? 'good' : 'warn', source: 'voice' });
  }
  async function prepare() {
    const r = await act('phrases');
    if (r.ok === false) { toast(r.error); return; }
    ui.prep = { phrases: (r.phrases || []).join('\n'), minutes: 3 };
    paint(Vesper.data.hearing);
  }
  async function start() {
    const lines = ui.prep.phrases.split('\n').map(x => x.trim()).filter(Boolean);
    const r = await act('start', { phrases: lines, background_s: ui.prep.minutes * 60 });
    if (r.ok === false) { toast(r.error, { level: 'warn' }); return; }
    ui.prep = null; ui.live = { step: 'commands', prompt: 'Starting…' };
  }
  async function simple(action, done) {
    ui.busy = true; paint(Vesper.data.hearing);
    const r = await act(action, action === 'apply' ? { keys: [...ui.ticked] } : {});
    ui.busy = false;
    if (r.ok === false) toast(r.error, { level: 'warn' });
    else if (done) toast(done(r), { level: 'good', source: 'voice' });
    if (Vesper.data.hearing) paint(Vesper.data.hearing);
  }

  function paint(d) {
    for (const p of panels('hearing')) {
      const b = body(p);
      if (!d) { fill(b, empty('Loading…')); continue; }
      const st = d.bench || {};
      meta(p, { recording: 'recording', analysing: 'working it out', done: 'done' }[st.state] || (d.report ? `last run ${ago(d.report.when)}` : ''));
      fill(b, h('div', { class: 'hr-grid' }, h('section', { class: 'hr-test' }, test(d, st)), h('section', { class: 'hr-words' }, words(d))));
    }
  }

  // ---- the voice test --------------------------------------------------------------------------------------------
  function test(d, st) {
    if (st.state === 'recording' || st.state === 'analysing') {
      const l = ui.live || {};
      const step = st.state === 'analysing' ? 'analysing' : l.step;
      return [h('div', { class: 'sub-h' }, 'Voice test'),
        h('div', { class: 'cal-step' }, `${STEP[step] || step || ''}${l.total > 1 && step !== 'analysing' ? ` · ${l.index}/${l.total}` : ''}`),
        h('div', { class: 'cal-prompt' }, l.recording ? h('span', { class: 'rec-dot' }) : null,
          step === 'analysing' ? (st.detail || l.prompt || 'Working it out…') : (l.prompt || '…')),
        step === 'analysing' ? h('div', { class: 't2' }, "I'm not listening for commands while I work this out (about a minute).")
          : h('div', { class: 'meter', id: 'hr-meter' }, h('i', { style: { width: `${Math.min(100, (l.level || 0) / 30)}%` } })),
        step === 'analysing' ? null : h('div', { class: 'controls' }, h('button', { class: 'btn danger', onclick: () => act('cancel') }, 'Cancel'))];
    }
    if (ui.prep) return prepView();
    const r = d.report;
    const intro = h('div', { class: 't2 hr-intro' }, 'Read about 36 of your own commands, then leave a few minutes of only background ' +
      '(game, Discord, music; I talk a bit too). I replay it all through the same chain the mic uses and show what I got wrong, ' +
      'then suggest settings. Nothing changes until you click Apply. The recordings stay in data/bench on this PC.');
    const buttons = h('div', { class: 'controls hr-actions' },
      h('button', { class: `btn ${r ? '' : 'primary'}`, onclick: prepare }, r ? 'Record again' : 'Start the voice test'),
      d.recordings ? h('button', { class: 'btn', disabled: ui.busy, onclick: () => simple('rerun') }, 'Run again on these recordings') : null,
      d.recordings ? h('button', { class: 'btn ghost', onclick: () => { if (confirm('Delete the voice test recordings? The results stay.')) simple('delete', () => 'Recordings deleted.'); } }, 'Delete recordings') : null);
    const ending = h('div', { class: 'hr-ending' }, h('span', { class: 't2' }, 'End of a sentence'),
      h('span', { class: 'seg' }, [['silence', 'After a silence', 'Ends after a fixed silence (endpoint_ms)'],
        ['smart', 'Natural', 'Smart Turn listens at each pause for whether you finished (9 MB model, downloaded once)']]
        .map(([mode, label, tip]) => h('button', { 'aria-pressed': String((d.end_of_turn || 'silence') === mode), title: tip,
          onclick: async () => { const x = await act('end_of_turn', { mode }); if (x.ok === false) toast(x.error, { level: 'warn' }); } }, label))));
    return [h('div', { class: 'sub-h' }, 'Voice test'),
      st.state === 'error' ? h('div', { class: 'empty' }, status('critical', `The test failed: ${st.error}`)) : null,
      r ? report(r) : intro, buttons, ending];
  }

  function prepView() {
    const n = ui.prep.phrases.split('\n').filter(x => x.trim()).length;
    const area = h('textarea', { class: 'field hr-lines', rows: '10', spellcheck: 'false', 'aria-label': 'Commands to read, one per line',
      oninput: e => { ui.prep.phrases = e.target.value; } });
    area.value = ui.prep.phrases;
    const mins = h('select', { class: 'field', 'aria-label': 'Minutes of background', onchange: e => { ui.prep.minutes = Number(e.target.value); } },
      [1, 3, 5].map(m => h('option', { value: String(m), selected: m === ui.prep.minutes }, `${m} min of background`)));
    return [h('div', { class: 'sub-h' }, 'Voice test · what you\'ll read'),
      h('div', { class: 't2' }, 'Your own recent commands first. Change or remove any line. Where a line has “…”, pause there for a second, as if thinking.'),
      area,
      h('div', { class: 't3' }, `${n} commands, about ${Math.round(n * 6 / 60 + ui.prep.minutes)} minutes. Then start the game, Discord or music for the background part, and don't talk to me.`),
      h('div', { class: 'controls' }, h('button', { class: 'btn primary', onclick: start }, 'Start recording'), mins,
        h('button', { class: 'btn ghost', onclick: () => { ui.prep = null; paint(Vesper.data.hearing); } }, 'Back'))];
  }

  const tile = (label, value, sub, level) => h('div', { class: `hr-tile ${level || ''}` },
    h('div', { class: 'hr-n' }, value == null ? '—' : String(value)), h('div', { class: 'hr-l' }, label), sub ? h('div', { class: 't3' }, sub) : null);

  function report(r) {
    const lat = r.latency_ms || {};
    const recs = r.recommendations || [];
    if (!ui.ticked || ui.ticked._when !== r.when) { ui.ticked = new Set(recs.map(x => x.key)); ui.ticked._when = r.when; }
    const wrong = (r.rows || []).filter(x => norm(x.said) !== norm(x.heard) || x.cut);
    return [
      h('div', { class: 'hr-tiles' },
        tile('word errors', r.wer != null ? `${r.wer}%` : null, r.wer_without_names != null ? `${r.wer_without_names}% without expecting names` : null, r.wer > 15 ? 'warn' : ''),
        tile('missed wakes', `${r.missed_wakes}/${r.named}`, 'commands with the name', r.missed_wakes ? 'warn' : ''),
        tile('cut off', `${r.cut_off}/${r.commands}`, 'ended before you finished', r.cut_off ? 'warn' : ''),
        tile('false wakes / hour', r.false_wakes_per_hour, r.background_s ? `${r.would_run_per_hour}/h got past the voice check · ${Math.round(r.background_s / 60)} min of background` : 'no background recorded', r.would_run ? 'warn' : ''),
        tile('speech → text', lat.p50 != null ? `${lat.p50} ms` : null, lat.p50 != null ? `9 in 10 under ${lat.p90} ms · waiting ${lat.endpoint_p50} + recognising ${lat.stt_p50}` : null)),
      r.false_heard && r.false_heard.length ? h('div', { class: 't3 hr-false' }, `Woke on: ${r.false_heard.map(t => `“${t}”`).join(' · ')}`) : null,
      scores(r),
      recs.length ? h('div', { class: 'hr-recs' }, h('div', { class: 'sub-h' }, 'Suggested from your numbers'),
        recs.map(x => {
          const on = ui.ticked.has(x.key);
          const applied = (r.applied || []).includes(x.key);
          const box = h('input', { type: 'checkbox', checked: on, disabled: applied, 'aria-label': x.label, onchange: e => {
            for (const y of recs) if (y.key === x.key || (x.group && y.group === x.group)) e.target.checked ? ui.ticked.add(y.key) : ui.ticked.delete(y.key);
            paint(Vesper.data.hearing);
          } });
          return h('label', { class: `hr-rec${applied ? ' applied' : ''}` }, box,
            h('div', {}, h('div', { class: 't1' }, `${x.label}: `, h('span', { class: 'muted' }, `${x.current}${x.unit ? ` ${x.unit}` : ''}`), ' → ',
              h('b', {}, `${x.value}${x.unit ? ` ${x.unit}` : ''}`), applied ? h('span', { class: 'muted' }, ' · applied') : null),
            h('div', { class: 't2' }, x.why)));
        }),
        h('div', { class: 'controls' }, h('button', { class: 'btn primary', disabled: ui.busy || ![...ui.ticked].some(k => !(r.applied || []).includes(k)),
          onclick: () => simple('apply', a => `Applied ${a.applied.length} setting${a.applied.length === 1 ? '' : 's'}. Listening again with them.`) }, 'Apply ticked')))
        : h('div', { class: 't2 hr-none' }, status('good', 'Nothing to change'), ' Your settings already fit these recordings.'),
      wrong.length ? h('details', { class: 'hr-rows' }, h('summary', {}, `What I got wrong (${wrong.length} of ${r.commands})`),
        h('table', { class: 't' }, h('thead', {}, h('tr', {}, h('th', {}, 'You said'), h('th', {}, 'I heard'))),
          h('tbody', {}, wrong.map(x => h('tr', {}, h('td', {}, x.said), h('td', {}, x.heard || '(nothing)', x.cut ? h('span', { class: 'muted' }, ' · cut off') : null)))))) : null,
      (r.notes || []).map(n => h('div', { class: 't3' }, n)),
    ];
  }

  const norm = t => (t || '').toLowerCase().replace(/[^a-z' ]+/g, ' ').split(/\s+/).filter(Boolean).join(' ');

  /* Voice match: your commands' scores on one lane, every other voice in the background on the other, the
     threshold across both. Position carries who is who; colour repeats it. */
  function scores(r) {
    const mine = r.owner_scores || [], others = r.other_scores || [];
    if (!mine.length && !others.length) return r.threshold == null ? h('div', { class: 't3' }, 'Voice match not checked: calibrate your voice first (Your voice panel).') : null;
    const W = 600, L = 92, R = 12, x = v => L + Math.max(0, Math.min(1, v)) * (W - L - R);
    const lane = (vals, y, cls, who) => vals.map(v => svg('circle', { cx: x(v).toFixed(1), cy: y, r: 4, class: cls }, svg('title', {}, document.createTextNode(`${who}: ${v}`))));
    const plot = svg('svg', { viewBox: `0 0 ${W} 74`, class: 'hr-strip', role: 'img',
      'aria-label': `Voice match scores: yours ${mine.length}, other voices ${others.length}, threshold ${r.threshold}` },
      svg('line', { x1: L, x2: W - R, y1: 62, y2: 62, class: 'hr-axis' }),
      ...[0, 0.25, 0.5, 0.75, 1].map(t => svg('text', { x: x(t), y: 73, class: 'hr-tick', 'text-anchor': 'middle' }, document.createTextNode(String(t)))),
      svg('text', { x: 0, y: 20, class: 'hr-lab' }, document.createTextNode('You')),
      svg('text', { x: 0, y: 46, class: 'hr-lab' }, document.createTextNode('Other voices')),
      ...lane(mine, 16, 'hr-me', 'You'), ...lane(others, 42, 'hr-them', 'Other voice'),
      r.threshold != null ? svg('line', { x1: x(r.threshold), x2: x(r.threshold), y1: 4, y2: 56, class: 'hr-thr' }, svg('title', {}, document.createTextNode(`Threshold now: ${r.threshold}`))) : null);
    const below = r.threshold != null ? mine.filter(v => v < r.threshold).length : null;
    const above = r.threshold != null ? others.filter(v => v >= r.threshold).length : null;
    return h('div', { class: 'hr-scores' }, h('div', { class: 't2' }, 'Voice match', r.threshold != null ? ` · threshold ${r.threshold}: ${below} of yours turned away, ${above} of ${others.length} others let in` : ''), plot);
  }

  // ---- words ------------------------------------------------------------------------------------------------------
  function words(d) {
    const heard = h('input', { class: 'field', placeholder: 'I keep hearing… (belatro)', 'aria-label': 'Word I hear' });
    const meant = h('input', { class: 'field', placeholder: 'You mean… (Balatro)', 'aria-label': 'Word you mean' });
    const list = d.words || [];
    return [h('div', { class: 'sub-h' }, 'Words I hear right now'),
      (d.suggestions || []).map(s => h('div', { class: 'hr-sugg' },
        h('div', { class: 't1' }, `“${s.heard}” → “${s.meant}”?`), h('div', { class: 't3' }, `You typed it after I heard “${s.said}”.`),
        h('div', { class: 'controls' }, h('button', { class: 'btn small primary', onclick: () => add(s.heard, s.meant) }, 'Remember'),
          h('button', { class: 'btn small ghost', onclick: () => act('dismiss', { id: s.id }) }, 'No')))),
      list.length ? h('table', { class: 't' }, h('tbody', {}, list.map(w => h('tr', {},
        h('td', {}, h('span', { class: 'muted' }, w.heard), ' → ', h('b', {}, w.meant)),
        h('td', { class: 'r act' }, w.learned ? h('button', { class: 'btn small', onclick: () => act('remove', { heard: w.heard }) }, 'Remove')
          : h('span', { class: 't3', title: 'Set in config.yaml (voice.corrections): change it there' }, 'config.yaml'))))))
        : empty('None yet. Say “you misheard Balatro as Belatro”, or retype what you said with the word fixed.'),
      h('form', { class: 'hr-add', onsubmit: e => { e.preventDefault(); if (heard.value.trim() && meant.value.trim()) add(heard.value.trim(), meant.value.trim()); } },
        heard, meant, h('button', { class: 'btn small', type: 'submit' }, 'Add')),
      h('div', { class: 't3' }, 'Names I expect (the game, chatters, people you email, scenes) are listened for automatically.')];
  }
})();
