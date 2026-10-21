/* Recorder (Work tab, next to meeting prep and debrief: it's for calls and meetings): a big REC state with a timer
   and a live level meter, the transcript as it grows, bookmarks, Pause / Mark / Stop; then the notes, an
   "Add 4 tasks?" card, and past recordings with search. While recording, the orb gets a red ring and a REC pill.
   Colours: alert = recording (it's "on air" for privacy), ion = J (his lines, his bookmarks and notes), iris = Vesper
   working (transcribing, writing notes), amber = waiting for J's yes (the task card). Nothing private shows live. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast, status } = Vesper.ui;
  const parts = new WeakMap();
  const S = { d: null, sel: '', view: null, viewKey: '', search: '', results: null, confirmDel: '', level: 0 };
  const two = n => String(n).padStart(2, '0');
  const clock = s => { s = Math.max(0, Math.floor(s)); return `${s >= 3600 ? `${Math.floor(s / 3600)}:` : ''}${two(Math.floor(s % 3600 / 60))}:${two(s % 60)}`; };
  const mins = s => (s < 60 ? `${Math.max(0, Math.round(s))} s` : s < 3600 ? `${Math.round(s / 60)} min` : `${Math.floor(s / 3600)} h ${Math.round(s % 3600 / 60)} min`);
  const when = ts => { const d = new Date(ts * 1000); return `${d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })}, ${d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`; };
  const day = iso => new Date(`${iso}T12:00:00`).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
  const keys = combo => (combo || '').split('+').filter(Boolean).map(k => h('kbd', {}, k.length === 1 ? k.toUpperCase() : k[0].toUpperCase() + k.slice(1)));
  const SVG = 'http://www.w3.org/2000/svg';
  function icon(d, cls = '') {
    const s = document.createElementNS(SVG, 'svg'); s.setAttribute('viewBox', '0 0 16 16'); s.setAttribute('aria-hidden', 'true');
    if (cls) s.setAttribute('class', cls);
    const p = document.createElementNS(SVG, 'path'); p.setAttribute('d', d); s.append(p); return s;
  }
  const I = { pause: 'M5.5 3.5v9M10.5 3.5v9', play: 'M5 3.2v9.6L12.5 8z', stop: 'M4 4h8v8H4z', star: 'M8 2.2l1.75 3.6 3.95.55-2.86 2.8.68 3.95L8 11.2l-3.52 1.9.68-3.95L2.3 6.35l3.95-.55z',
    copy: 'M5.5 5.5h7v8h-7zM3.5 10.5v-8h7', folder: 'M2 4.5h4l1.5 1.5H14v6.5H2z', trash: 'M3.5 4.5h9M6.5 4.5V3h3v1.5M4.5 4.5l.7 9h5.6l.7-9', retry: 'M12.5 8a4.5 4.5 0 1 1-1.3-3.2M12.5 2.5v2.8H9.7',
    mic: 'M5.5 1.5h5v8h-5zM3 7.5a5 5 0 0 0 10 0M8 12.5v2.5', screen: 'M2 3h12v8H2zM6 14h4M8 11v3', search: 'M7 12a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM10.6 10.6 14 14' };

  async function act(action, payload, okText) {
    const r = await Vesper.act('recorder', action, payload || {});
    if (r.ok === false) toast(r.error || 'That didn’t work.', { source: 'recorder', level: 'warn' });
    else if (okText) toast(typeof okText === 'function' ? okText(r) : okText, { source: 'recorder', level: 'good' });
    return r;
  }

  // ---- the orb: a red ring and a REC pill while recording (on every tab) ----
  let pill = null;
  function paintOrb(d) {
    const st = d && d.state !== 'idle' ? d.state : '';
    document.body.dataset.recording = st === 'recording' ? 'on' : st === 'paused' ? 'paused' : '';
    const wrap = document.getElementById('orb-wrap');
    if (!wrap) return;
    if (!pill) { pill = h('button', { class: 'rec-pill', type: 'button', title: 'Recording: open the Recorder', onclick: () => { document.querySelector('[role="tab"][data-view="work"]')?.click(); setTimeout(() => panels('recorder')[0]?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 60); } }, h('span', { class: 'rec-dot', 'aria-hidden': 'true' }), h('span', { class: 'rec-pill-t' }, 'REC')); wrap.append(pill); }
    pill.hidden = !st;
    pill.classList.toggle('paused', st === 'paused');
  }

  // ---- the level meter: its own throttled event (not the orb's) ----
  const BARS = 28;
  Vesper.on('recorder_level', data => {
    S.level = Math.max(0, Math.min(1, data?.rms || 0));
    for (const p of panels('recorder')) {
      const n = parts.get(p); if (!n) continue;
      const lit = Math.round(S.level * BARS);
      n.bars.forEach((b, i) => b.classList.toggle('on', i < lit));
    }
  });

  // ---- the timer ticks on its own between updates ----
  setInterval(() => {
    const d = S.d; if (!d || !d.rec || document.hidden) return;
    const t = d.rec.paused ? d.rec.elapsed : d.rec.elapsed + Math.max(0, Date.now() / 1000 - d.at);
    for (const p of panels('recorder')) { const n = parts.get(p); if (n) n.timer.textContent = clock(t); }
    if (pill && !pill.hidden) pill.lastChild.textContent = `REC ${clock(t)}`;
  }, 1000);

  function build(p) {
    const bars = Array.from({ length: BARS }, () => h('i'));
    const timer = h('div', { class: 'rec-timer num', 'aria-live': 'off' }, '00:00');
    const stateLbl = h('div', { class: 'rec-state' });
    const chips = h('div', { class: 'rec-chips' });
    const ctrls = h('div', { class: 'rec-ctrls' });
    const noteIn = h('input', { class: 'rec-note-in', maxlength: 500, placeholder: 'Type a note… (or say “Vesper, note: …”)', 'aria-label': 'Add a note to the recording' });
    const noteForm = h('form', { class: 'rec-note', autocomplete: 'off', onsubmit: async e => {
      e.preventDefault(); const v = noteIn.value.trim(); if (!v) return;
      const r = await act('mark', { note: v }); if (r.ok !== false) { noteIn.value = ''; toast(`Note added at ${r.at}.`, { source: 'recorder', level: 'good' }); }
    } }, noteIn, h('button', { class: 'btn small', type: 'submit' }, 'Add note'));
    const hero = h('div', { class: 'rec-hero' },
      h('div', { class: 'rec-big' }),
      h('div', { class: 'rec-read' }, stateLbl, timer, h('div', { class: 'rec-meter', 'aria-hidden': 'true' }, bars)),
      ctrls);
    const log = h('div', { class: 'rec-log', role: 'log', 'aria-live': 'polite', 'aria-label': 'Live transcript' });
    const jobs = h('div', { class: 'rec-jobs' });
    const left = h('div', { class: 'rec-left' }, hero, chips, noteForm, log, jobs);
    const card = h('div', { class: 'rec-card-slot' });
    const notes = h('div', { class: 'rec-notes' });
    const q = h('input', { class: 'rec-q', type: 'search', placeholder: 'Search recordings…', 'aria-label': 'Search your recordings' });
    let timerQ = null;
    q.addEventListener('input', () => { clearTimeout(timerQ); timerQ = setTimeout(() => search(q.value), 250); });
    const list = h('div', { class: 'rec-list' });
    const lib = h('div', { class: 'rec-lib' }, h('div', { class: 'rec-lib-head' }, h('span', { class: 'rec-k' }, 'Past recordings'), h('span', { class: 'rec-search' }, icon(I.search), q)), list);
    const right = h('div', { class: 'rec-right' }, card, notes, lib);
    const foot = h('div', { class: 'rec-foot' });
    fill(body(p), h('div', { class: 'rec-grid' }, left, right), foot);
    const n = { bars, timer, stateLbl, chips, ctrls, noteForm, noteIn, hero, log, jobs, card, notes, list, q, ctrlKey: '', big: hero.firstChild, left, right, lib, foot };
    parts.set(p, n);
    return n;
  }

  async function search(text) {
    S.search = text.trim();
    if (!S.search) { S.results = null; return paint(S.d); }
    const r = await Vesper.act('recorder', 'search', { q: S.search });
    S.results = r.ok === false ? [] : (r.recordings || []);
    paint(S.d);
  }

  async function select(id, force) {
    S.sel = id; S.confirmDel = '';
    const row = (S.d?.recent || []).find(r => r.id === id);
    const key = `${id}|${row?.state}|${row?.by}|${row?.tasks_state}`;
    if (!force && S.viewKey === key && S.view) return paint(S.d);
    S.viewKey = key;
    const r = await Vesper.act('recorder', 'notes', { id });
    S.view = r.ok === false ? { error: r.error, id } : r;
    paint(S.d);
  }

  // ---- left: the recorder itself ----
  function controls(d, n) {
    const rec = d.rec;
    const key = `${d.state}|${d.live}|${d.system_mode}|${!!rec?.on_stream}`;
    if (n.ctrlKey === key) return;
    n.ctrlKey = key;
    if (!rec) {
      const start = onStream => act('start', onStream ? { on_stream: true } : {});
      fill(n.big, h('button', { class: 'rec-go', type: 'button', title: 'Start recording', 'aria-label': 'Start recording', onclick: () => start(d.live) }, h('span', { class: 'rec-go-dot' })));
      const seg = h('div', { class: 'seg rec-seg', role: 'group', 'aria-label': 'Call audio' },
        ['meetings', 'always', 'never'].map(m => h('button', { type: 'button', 'aria-pressed': String(d.system_mode === m), title: { meetings: 'Record the PC’s sound too during calendar meetings or when you say “meeting” or “call”', always: 'Always record the PC’s sound too', never: 'Only your mic' }[m],
          onclick: () => act('set', { system_audio: m }) }, m === 'meetings' ? 'In meetings' : m === 'always' ? 'Always' : 'Mic only')));
      fill(n.ctrls, h('div', { class: 'rec-ctrl-col' },
        d.live ? h('button', { class: 'btn small danger', onclick: () => start(true) }, 'Record on stream')
          : h('button', { class: 'btn primary rec-start', onclick: () => start(false) }, 'Start recording'),
        h('div', { class: 'rec-sys' }, h('span', { class: 'rec-k' }, 'Call audio'), seg)));
      return;
    }
    fill(n.big, h('div', { class: `rec-orb ${rec.paused ? 'paused' : ''}`, 'aria-hidden': 'true' }, h('span')));
    fill(n.ctrls, h('div', { class: 'rec-btns' },
      h('button', { class: 'btn small', onclick: () => act('pause', { resume: rec.paused }) }, icon(rec.paused ? I.play : I.pause), rec.paused ? 'Resume' : 'Pause'),
      h('button', { class: 'btn small rec-mark', disabled: rec.paused, onclick: async () => { const r = await act('mark', {}); if (r.ok !== false) toast(`Bookmark at ${r.at}.`, { source: 'recorder', level: 'good' }); } }, icon(I.star), 'Mark'),
      h('button', { class: 'btn small rec-stop', onclick: () => act('stop', {}, 'Stopped. Notes are on their way.') }, icon(I.stop), 'Stop')));
  }

  function line(l, fresh) {
    const who = l.vesper ? 'vesper' : l.src === 'system' ? 'others' : 'me';
    return h('div', { class: `rec-line ${who}${fresh ? ' fresh' : ''}` },
      h('span', { class: 'rec-t num' }, clock(l.t)), h('span', { class: 'rec-who' }, l.vesper ? 'Vesper' : l.who === 'J' ? 'You' : l.who),
      h('span', { class: 'rec-txt' }, l.text));
  }
  function markRow(m, fresh) {
    return h('div', { class: `rec-line mark ${m.note ? 'note' : ''}${fresh ? ' fresh' : ''}` },
      h('span', { class: 'rec-t num' }, clock(m.t)), h('span', { class: 'rec-who' }, icon(I.star, 'rec-star')),
      h('span', { class: 'rec-txt' }, m.note || (m.label ? `Bookmark · ${m.label}` : 'Bookmark')));
  }

  function left(d, n, name) {
    const rec = d.rec;
    controls(d, n);
    n.hero.classList.toggle('on', !!rec);
    n.hero.classList.toggle('paused', !!rec?.paused);
    if (!rec) {
      n.stateLbl.replaceChildren(h('span', { class: 'rec-k' }, 'Ready'));
      n.timer.textContent = '00:00';
      n.bars.forEach(b => b.classList.remove('on'));
      fill(n.chips, h('span', { class: 'rec-hint' }, `Say “${name}, start recording” or “take notes of this meeting”`,
        d.hotkey ? h('span', { class: 'muted' }, ' · ', keys(d.hotkey)) : null));
      n.noteForm.hidden = true;
      fill(n.log, h('div', { class: 'rec-empty' },
        h('div', { class: 'rec-empty-t' }, 'Record a call, a meeting or a thought.'),
        h('div', { class: 't3 muted' }, 'The transcript grows here as you talk. When you stop, the model writes notes: summary, decisions, action items with owners and dates. “Mark that” bookmarks a moment; “note: …” adds a typed note.'),
        d.live ? h('div', { class: 'rec-warn' }, status('critical', 'You’re live: recording only starts if you say “record on stream”.')) : null));
    } else {
      n.stateLbl.replaceChildren(h('span', { class: 'rec-k rec-k-on' }, rec.paused ? 'Paused' : 'Recording'),
        h('span', { class: 'rec-title' }, rec.meeting ? rec.meeting.title : rec.title));
      n.timer.textContent = clock(rec.paused ? rec.elapsed : rec.elapsed + Math.max(0, Date.now() / 1000 - d.at));
      fill(n.chips,
        h('span', { class: 'chip rec-chip me' }, icon(I.mic), rec.muted ? 'Mic muted: not recording' : rec.via === 'own' ? 'Mic' : 'Mic · shared with voice'),
        rec.system ? h('span', { class: 'chip rec-chip others' }, icon(I.screen), 'Call audio')
          : rec.system_why ? h('span', { class: 'chip rec-chip off', title: rec.system_why }, icon(I.screen), 'No call audio') : null,
        rec.meeting && rec.meeting.with?.length ? h('span', { class: 'chip rec-chip' }, `with ${rec.meeting.with.join(', ')}`) : null,
        rec.on_stream ? h('span', { class: 'badge-live' }, 'On stream') : null,
        rec.backlog > 1 ? h('span', { class: 'chip rec-chip ai' }, `transcribing · ${rec.backlog} behind`) : null);
      n.noteForm.hidden = false;
      if (rec.hidden || (d.live && !rec.lines.length)) {
        fill(n.log, h('div', { class: 'rec-empty' }, status('critical', 'Transcript hidden while you’re live.')));
      } else {
        const atBottom = n.log.scrollHeight - n.log.scrollTop - n.log.clientHeight < 48;
        const seen = n.seen || new Set();
        const rows = [...rec.lines.map(l => ({ t: l.t, k: 1, id: `l${l.n}`, el: f => line(l, f) })),
          ...rec.marks.map(m => ({ t: m.t, k: 0, id: `m${m.id}`, el: f => markRow(m, f) }))]
          .sort((a, b) => a.t - b.t || a.k - b.k).slice(-60);
        const first = !n.seen || n.seenRec !== rec.id;  // a fresh panel: nothing slides in
        fill(n.log, rows.length ? rows.map(r => r.el(!first && !seen.has(r.id))) : h('div', { class: 'rec-empty t3 muted' }, rec.paused ? 'Paused.' : 'Listening… the first words show up a moment after they’re said.'));
        n.seen = new Set(rows.map(r => r.id)); n.seenRec = rec.id;
        if (atBottom) n.log.scrollTop = n.log.scrollHeight;
      }
    }
    const jobs = (d.recent || []).filter(r => r.job);
    fill(n.jobs, jobs.map(r => h('div', { class: 'rec-job' }, h('span', { class: 'rec-spin', 'aria-hidden': 'true' }), h('span', {}, r.job), h('span', { class: 'muted' }, ` · ${r.title}`))));
  }

  // ---- right: the task card, the notes, past recordings ----
  function taskCard(d, n) {
    const c = d.tasks_card;
    if (!c) { n.card._id = ''; return fill(n.card); }
    if (c.hidden) return fill(n.card, h('div', { class: 'rec-card' }, h('div', { class: 'rec-card-q' }, `Add ${c.count} tasks?`), h('div', { class: 't3 muted' }, 'Hidden while you’re live.')));
    const key = `${c.id}|${c.items.length}`;
    if (n.card._id === key) return;
    n.card._id = key;
    const boxes = c.items.map((a, i) => h('input', { type: 'checkbox', checked: true, 'aria-label': a.task, onchange: () => sync() }));
    const addBtn = h('button', { class: 'btn small primary' });
    const sync = () => { const k = boxes.filter(b => b.checked).length; addBtn.textContent = k === c.items.length ? `Add ${k} task${k === 1 ? '' : 's'}` : `Add ${k}`; addBtn.disabled = !k; };
    addBtn.onclick = async () => {
      const pick = boxes.map((b, i) => (b.checked ? i : -1)).filter(i => i >= 0);
      addBtn.disabled = true;
      const r = await act('add_tasks', { id: c.id, pick }, x => `Added ${x.added} task${x.added === 1 ? '' : 's'}.`);
      if (r.ok === false) addBtn.disabled = false;
    };
    sync();
    fill(n.card, h('div', { class: 'rec-card', role: 'group', 'aria-label': 'Action items from the recording' },
      h('div', { class: 'rec-card-head' }, h('span', { class: 'rec-card-q' }, `Add ${c.items.length} task${c.items.length === 1 ? '' : 's'}?`), h('span', { class: 't3 muted' }, `from ${c.title}`)),
      h('div', { class: 'rec-card-list' }, c.items.map((a, i) => h('label', { class: 'rec-ai' }, boxes[i],
        h('span', { class: 'rec-ai-t' }, a.task), a.owner ? h('span', { class: `pill ${a.owner === 'J' ? 'me' : ''}` }, a.owner === 'J' ? 'You' : a.owner) : null,
        a.due ? h('span', { class: 'pill rec-due' }, day(a.due)) : null))),
      h('div', { class: 'rec-card-btns' }, addBtn, h('button', { class: 'btn small ghost', onclick: () => act('dismiss_tasks', { id: c.id }) }, 'Not now'))));
  }

  function section(title, items, render) {
    if (!items || !items.length) return null;
    return h('div', { class: 'rec-sec' }, h('div', { class: 'rec-k' }, title), h('ul', {}, items.map(x => h('li', {}, render ? render(x) : x))));
  }

  function notesView(d, n) {
    const v = S.view;
    if (d.live) return fill(n.notes, h('div', { class: 'rec-notes-box hidden' }, status('critical', 'Notes are hidden while you’re live.')));
    if (!v || !S.sel) return fill(n.notes);
    if (v.error) return fill(n.notes, h('div', { class: 'rec-notes-box' }, h('div', { class: 't3 muted' }, v.error)));
    const nt = v.notes;
    const row = (d.recent || []).find(r => r.id === v.id) || {};
    const busy = row.job;
    const copy = async () => { try { await navigator.clipboard.writeText(v.markdown || ''); toast('Notes copied (Markdown).', { source: 'recorder', level: 'good' }); } catch { toast('The browser didn’t allow copying.', { level: 'warn' }); } };
    const delBtn = S.confirmDel === v.id
      ? h('button', { class: 'btn small danger', onclick: async () => { const r = await act('delete', { id: v.id }, 'Recording deleted.'); if (r.ok !== false) { S.sel = ''; S.view = null; paint(S.d); } } }, icon(I.trash), 'Delete audio, transcript and notes')
      : h('button', { class: 'btn small ghost', title: 'Delete this recording', onclick: () => { S.confirmDel = v.id; paint(S.d); } }, icon(I.trash));
    fill(n.notes, h('article', { class: 'rec-notes-box' },
      h('div', { class: 'rec-notes-head' },
        h('div', { class: 'rec-notes-title' }, v.title),
        h('div', { class: 't3 muted' }, [when(v.started), mins(v.seconds || 0), v.marks?.length ? `${v.marks.length} bookmark${v.marks.length === 1 ? '' : 's'}` : null,
          v.by === 'model' ? 'notes by the model' : v.state === 'done' ? 'no model: transcript only' : null].filter(Boolean).join(' · '))),
      busy ? h('div', { class: 'rec-job' }, h('span', { class: 'rec-spin', 'aria-hidden': 'true' }), busy) : null,
      v.error ? h('div', { class: 'rec-warn' }, status('warning', v.error)) : null,
      nt ? [
        h('p', { class: 'rec-summary' }, nt.summary),
        section('Decisions', nt.decisions),
        section('Action items', nt.action_items, a => [a.task, a.owner ? h('span', { class: `pill ${a.owner === 'J' ? 'me' : ''}` }, a.owner === 'J' ? 'You' : a.owner) : null, a.due ? h('span', { class: 'pill rec-due' }, day(a.due)) : null]),
        section('Key points', nt.key_points),
        section('Open questions', nt.open_questions),
        nt.people?.length ? h('div', { class: 't3 muted rec-people' }, `People: ${nt.people.join(', ')}`) : null,
      ] : v.state === 'done' ? h('div', { class: 'rec-nomodel' }, h('b', {}, 'No model: transcript only. '), 'Start the local model (Setup → Brain), then Retry for full notes.') : null,
      v.marks?.length ? section('Bookmarks', v.marks, m => [h('span', { class: 'rec-t num' }, clock(m.t)), ' ', m.note || (m.label || '★ bookmark')]) : null,
      !nt && v.lines?.length ? h('details', { class: 'rec-tx' }, h('summary', {}, `Transcript · ${v.lines.length} lines`),
        h('div', { class: 'rec-log small' }, v.lines.slice(0, 200).map(l => line(l)))) : null,
      h('div', { class: 'rec-notes-btns' },
        h('button', { class: 'btn small', disabled: !v.markdown, onclick: copy }, icon(I.copy), 'Copy'),
        h('button', { class: 'btn small', onclick: () => act('open', { id: v.id, which: 'notes' }) }, icon(I.folder), v.path ? 'Open notes' : 'Open folder'),
        h('button', { class: 'btn small ghost', disabled: !!busy, title: 'Write the notes again', onclick: async () => { const r = await act('retry', { id: v.id }); if (r.ok !== false) { S.viewKey = ''; } } }, icon(I.retry), 'Retry'),
        h('span', { class: 'spacer' }), delBtn),
      v.path ? h('div', { class: 't3 muted rec-path', title: v.path }, v.path) : null));
  }

  function pastList(d, n) {
    const items = S.results ?? (d.recent || []);
    if (S.results && !items.length) return fill(n.list, empty(`Nothing about “${S.search}”.`));
    if (!items.length) return fill(n.list, empty('No recordings yet.'));
    fill(n.list, items.map(r => {
      const row = (d.recent || []).find(x => x.id === r.id) || r;
      const lines = r.lines || [];
      return h('button', { type: 'button', class: `rec-item ${S.sel === r.id ? 'sel' : ''}`, onclick: () => select(r.id) },
        h('div', { class: 'rec-item-top' }, h('span', { class: 'rec-item-t' }, r.title),
          h('span', { class: 't3 muted num' }, row.started ? `${when(row.started)} · ${mins(row.seconds || 0)}` : r.when)),
        !d.live && (lines[0] || row.summary) ? h('div', { class: 'rec-item-s' }, lines[0] ? [h('span', { class: 'rec-t num' }, lines[0].at), ' ', lines[0].text] : row.summary) : null,
        h('div', { class: 'rec-item-b' },
          row.job ? h('span', { class: 'pill ai' }, row.job) : row.state === 'error' ? h('span', { class: 'pill warn' }, 'needs a look') : null,
          row.tasks ? h('span', { class: `pill ${row.tasks_state === 'ask' ? 'warn' : ''}` }, `${row.tasks} action item${row.tasks === 1 ? '' : 's'}`) : null,
          row.marks ? h('span', { class: 'pill' }, `★ ${row.marks}`) : null,
          row.file ? h('span', { class: 'pill' }, 'from a file') : row.audio === false ? h('span', { class: 'pill muted' }, 'audio deleted') : null,
          row.by === 'none' && row.state === 'done' ? h('span', { class: 'pill muted' }, 'transcript only') : null));
    }));
  }

  async function changeFolder(cur) {
    const v = window.prompt('Folder for your notes (paste the full path, e.g. K:\\My Drive\\Vesper Notes). Empty = Vesper’s own data\\notes.', cur || '');
    if (v === null) return;
    await act('set', { notes_dir: v.trim() }, r => `Notes go to ${r.notes_dir}`);
  }
  function foot(d, n) {
    const key = `${d.notes_dir}|${d.ffmpeg}|${d.keep_audio_days}|${d.hotkey}|${d.hotkey_ok}`;
    if (n.foot._key === key) return;
    n.foot._key = key;
    fill(n.foot,
      h('span', { class: 'rec-foot-i' }, h('span', { class: 'rec-k' }, 'Notes'), h('span', { class: 'mono rec-dir', title: d.notes_dir }, d.notes_dir),
        h('button', { class: 'btn small ghost', onclick: () => changeFolder(d.notes_dir) }, 'Change')),
      h('span', { class: 'rec-foot-i' }, h('span', { class: 'rec-k' }, 'Audio'),
        d.ffmpeg ? `Opus, kept ${d.keep_audio_days} days` : h('span', { title: 'Install ffmpeg for files about 10× smaller and notes from .m4a/.mp3 files' }, `WAV (no ffmpeg), kept ${d.keep_audio_days} days`)),
      d.hotkey ? h('span', { class: 'rec-foot-i' }, h('span', { class: 'rec-k' }, 'Hotkey'), keys(d.hotkey), d.hotkey_ok ? null : h('span', { class: 'muted' }, ' (not active)')) : null);
  }

  function paint(d) {
    S.d = d;
    paintOrb(d);
    const name = Vesper.data.assistant?.name || 'Vesper';
    for (const p of panels('recorder')) {
      if (!d) { fill(body(p), empty('Loading…')); continue; }
      const n = parts.get(p) || build(p);
      p.classList.toggle('rec-on', d.state === 'recording');
      p.classList.toggle('rec-paused', d.state === 'paused');
      meta(p, d.rec ? (d.rec.paused ? 'paused' : 'recording') : d.total ? `${d.total} recording${d.total === 1 ? '' : 's'} · audio kept ${d.keep_audio_days} days` : 'notes from your audio');
      left(d, n, name);
      // idle: past recordings sit under the REC button; recording: the live transcript has the left side to itself
      const home = d.rec ? n.right : n.left;
      if (n.lib.parentNode !== home) home.append(n.lib);
      taskCard(d, n);
      // the latest finished recording opens by itself (and again when its notes arrive)
      const latest = (d.recent || []).find(r => r.state !== 'recording');
      if (!S.sel && latest && !d.live) select(latest.id);
      else if (S.sel) {
        const row = (d.recent || []).find(r => r.id === S.sel);
        const key = row ? `${row.id}|${row.state}|${row.by}|${row.tasks_state}` : '';
        if (row && key !== S.viewKey) select(S.sel, true);
        if (!row && !S.results) { S.sel = ''; S.view = null; }
      }
      notesView(d, n);
      pastList(d, n);
      foot(d, n);
    }
  }

  Vesper.skill('recorder', { panels: [{ view: 'work', span: 12, title: 'Recorder · notes from your audio', after: 'debrief' }], render: paint });
})();
