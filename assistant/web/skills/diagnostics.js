/* Diagnostics: one report to send (Setup → Diagnostics), what Vesper is using (measured 30 s on demand,
   assistant/resources.py), and Setup → Skills: every skill with an on/off switch. */
(() => {
  const { h, fill, empty, panels, body, meta, toast, status, ago } = Vesper.ui;
  const LEVEL = { fail: ['critical', 'Fail'], warn: ['warning', 'Warn'], pass: ['good', 'Pass'], skip: ['idle', 'Not set up'] };
  const STATE = { on: ['good', 'On'], off: ['idle', 'Off'], crashed: ['critical', 'Switched off'], failed: ['critical', "Didn't load"] };
  let running = false;

  async function run() {
    running = true; paint(Vesper.data.diagnostics);
    const r = await Vesper.act('diagnostics', 'run', {});
    running = false;
    if (r.ok === false) toast(r.error || 'Diagnostics failed.', { level: 'error', source: 'diagnostics' });
    else {
      const s = r.summary || {};
      toast(`Diagnostics: ${s.pass || 0} pass · ${s.warn || 0} warn · ${s.fail || 0} fail. Report saved.`,
        { level: s.fail ? 'warn' : 'good', source: 'diagnostics' });
      Vesper.data.diagnostics = { ...(Vesper.data.diagnostics || {}), last: r };
    }
    paint(Vesper.data.diagnostics);
  }
  async function open(what) {
    const r = await Vesper.act('diagnostics', 'open', { what });
    if (r.ok === false) toast(r.error || "Couldn't open it.");
  }
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); toast('Path copied.', { level: 'good' }); }
    catch { toast("Couldn't copy: select the path and press Ctrl+C."); }
  }

  const chip = (n, kind, label) => h('div', { class: `dg-chip ${kind}${n ? '' : ' zero'}` },
    h('span', { class: 'dg-n' }, String(n || 0)), h('span', { class: 'dg-l' }, label));

  function report(d) {
    const last = d && d.last;
    const busy = running || (d && d.running);
    const btn = h('button', { class: 'btn primary small', disabled: busy, onclick: run }, busy ? 'Running every check…' : 'Run diagnostics');
    if (!last) {
      return [empty('Runs every check at once (connections, models, each skill, recent errors) and writes one file you can send. Secrets are taken out.'),
        h('div', { class: 'controls' }, btn)];
    }
    const s = last.summary || {};
    const files = last.files || {};
    const items = last.attention || [];
    return [
      h('div', { class: `dg-sum${busy ? ' busy' : ''}`, 'aria-live': 'polite' },
        chip(s.pass, 'pass', 'pass'), chip(s.warn, 'warn', 'warn'), chip(s.fail, 'fail', 'fail')),
      h('div', { class: 't2 dg-when' }, `${ago(last.made_at)}${last.ran_s ? ` · took ${last.ran_s} s` : ''}${s.skip ? ` · ${s.skip} not set up` : ''}`),
      files.md ? h('div', { class: 'dg-file' },
        h('div', { class: 't3' }, 'The report to send'),
        h('code', { class: 'dg-path', title: files.md }, files.md)) : null,
      h('div', { class: 'controls dg-actions' },
        btn,
        files.md ? h('button', { class: 'btn small', onclick: () => open('file') }, 'Open the file') : null,
        files.md ? h('button', { class: 'btn small ghost', onclick: () => open('folder') }, 'Show in folder') : null,
        files.md ? h('button', { class: 'btn small ghost', onclick: () => copy(files.md) }, 'Copy path') : null),
      items.length ? h('div', { class: 'list dg-list' }, items.map(c => h('div', { class: 'item' }, h('div', { class: 'main' },
        h('div', { class: 't1' }, status(...(LEVEL[c.status] || LEVEL.skip)), ' ', c.name),
        c.detail ? h('div', { class: 't2' }, c.detail) : null,
        c.fix ? h('div', { class: 't3' }, `→ ${c.fix}`) : null)))) : h('div', { class: 't2 dg-ok' }, 'Every check passed.'),
      last.more ? h('div', { class: 't3' }, `…and ${last.more} more in the file.`) : null,
    ];
  }

  async function toggle(row, on, input) {
    input.disabled = true;
    const r = await Vesper.act('diagnostics', 'skill', { name: row.name, on });
    input.disabled = false;
    if (r.ok === false) { toast(r.error || 'That failed.', { level: 'error' }); input.checked = !on; return; }
    if (r.reload) { toast(`${row.title} is on. Reloading the HUD to show it…`, { level: 'good' }); setTimeout(() => location.reload(), 1200); }
    else toast(on ? `${row.title} is back on.` : `${row.title} is off (until you turn it on here).`, { level: on ? 'good' : 'info' });
  }

  function skillRow(row) {
    const on = row.state === 'on';
    const input = h('input', { type: 'checkbox', role: 'switch', checked: on, disabled: row.essential,
      'aria-label': `${row.title}: ${on ? 'on' : 'off'}`, onchange: e => toggle(row, e.target.checked, e.target) });
    return h('div', { class: `dg-skill ${row.state}` },
      h('div', { class: 'dg-main' },
        h('div', { class: 't1' }, row.title, ' ', status(...(STATE[row.state] || STATE.off))),
        row.last_error ? h('div', { class: 't2 dg-err', title: row.last_error },
          `${row.where ? `${row.where}: ` : ''}${row.last_error}${row.at ? ` · ${ago(row.at)}` : ''}`) : null),
      h('label', { class: 'dg-toggle', title: row.essential ? 'Always on' : on ? 'Turn off' : 'Turn on' },
        input, h('span', { class: 'dg-switch', 'aria-hidden': 'true' })));
  }

  // ---- What Vesper is using: nothing is sampled until "Measure 30 s" is clicked
  async function measure(delay = 0) {
    const r = await Vesper.act('diagnostics', 'measure', { delay });
    if (r.ok !== false && delay) toast(`Measuring starts in ${delay} s: switch to your game now. The card shows the result.`, { level: 'good', source: 'diagnostics' });
    if (r.ok === false) toast(r.error || "Couldn't measure.", { level: 'error', source: 'diagnostics' });
  }
  const pct = v => `${(v || 0) < 0.1 ? (v || 0).toFixed(2) : (v || 0).toFixed(1)}%`;
  const mins = s => s == null ? '' : s < 60 ? 'unloads in under a minute' : s < 10800 ? `unloads in ${Math.round(s / 60)} min` : 'stays loaded';
  const row = (k, v, sub) => h('div', { class: 'rs-row' }, h('span', { class: 'rs-k' }, k), h('span', { class: 'rs-v num' }, v),
    sub ? h('span', { class: 't3 rs-sub' }, sub) : null);
  const block = (title, rows) => rows.length ? h('div', { class: 'rs-block' }, h('div', { class: 't3 rs-h' }, title), ...rows) : null;

  function resources(d) {
    const r = d && d.resources, m = d && d.measuring;
    const waiting = m && m.starts_at && m.starts_at * 1000 > Date.now() && !m.progress;
    const btn = h('button', { class: 'btn primary small', disabled: !!m, onclick: () => measure(0) },
      waiting ? `Starts at ${new Date(m.starts_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`
        : m ? `Measuring… ${Math.round((m.progress || 0) * m.seconds)} of ${Math.round(m.seconds)} s` : r ? 'Measure again (30 s)' : 'Measure 30 s');
    const later = h('button', { class: 'btn small ghost', disabled: !!m, onclick: () => measure(60),
      title: 'Starts in a minute: time to switch to your game, so it measures what Vesper uses mid-game' }, 'Measure in a game (starts in 1 min)');
    const bar = m ? h('div', { class: 'rs-bar', role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100,
      'aria-valuenow': Math.round((m.progress || 0) * 100) }, h('span', { style: { width: `${Math.round((m.progress || 0) * 100)}%` } })) : null;
    if (!r) return [empty('Watches Vesper for 30 seconds: CPU per part, memory per speech model, the graphics card memory each Ollama model holds and when it unloads, wakeups, programs launched and model calls. Nothing is measured until you click.'),
      h('div', { class: 'controls dg-actions' }, btn, later), bar];
    const held = (r.ollama || []).filter(x => x.vram_gb);
    const launched = (r.launches || []).slice(0, 6);
    const win = Math.max(1, Math.round((r.launches_window_h || 0) * 60));
    return [
      h('div', { class: 'rs-head', 'aria-live': 'polite' }, r.headline),
      h('div', { class: 't2 dg-when' }, `${ago(r.at)} · over ${Math.round(r.seconds)} s · ${r.wakeups_s ?? '?'} wakeups a second · ${r.threads ?? '?'} threads${r.private_mb ? ` · ${r.private_mb} MB private` : ''}`),
      h('div', { class: 'controls dg-actions' }, btn, later), bar,
      block('CPU by part (% of one core)', (r.by_part || []).slice(0, 8).map(p => row(p.part, pct(p.cpu_pct)))),
      block('Graphics card', [
        ...held.map(x => row(x.name, `${x.vram_gb} GB`, `${mins(x.unloads_in_s)}${r.in_game ? ' · held mid-game' : ''}`)),
        r.gpu ? row(r.gpu.name || 'GPU', `${r.gpu.used_gb ?? '?'} / ${r.gpu.total_gb ?? '?'} GB`, 'used in all') : null,
        row('Vesper itself', '0 GB', 'its speech runs on the CPU')].filter(Boolean)),
      block('Memory per speech model', (r.speech_models || []).map(x => row(x.name, `${x.rss_mb} MB`, `loaded in ${x.load_s} s`))),
      block(`Programs launched (last ${win} min)`, launched.length ? launched.map(x => row(x.program, String(x.count), `${x.per_h} an hour`))
        : [row('none', '0', r.nvml_reads != null ? 'GPU read through NVML' : '')]),
      block('Model calls (last hour)', (r.model_calls || []).slice(0, 8).map(c => row(c.who, String(c.calls),
        `${Object.keys(c.models).join(', ')}${c.loads ? ` · loaded it ${c.loads}×` : ''}${c.keep_alive.length ? ` · kept ${c.keep_alive.join('/')}` : ''}`))),
      block('Busiest pollers (run time an hour, since Vesper started)', (r.pollers || []).slice(0, 6).map(p => row(p.name, `${Math.round(p.ms_h)} ms`,
        `every ${p.every_s} s${p.pace !== 'normal' ? ` · ${p.pace}` : ''} · ${p.avg_ms} ms a run`))),
      h('div', { class: 't3' }, 'Run diagnostics to put this table in the report you send.'),
    ];
  }

  function paint(d) {
    for (const p of panels('resources')) {
      const r = d && d.resources;
      meta(p, d && d.measuring ? status('idle', 'Measuring…') : r ? `${pct(r.cpu_pct)} of a core · ${r.rss_mb} MB` : '');
      fill(body(p), ...resources(d));
    }
    for (const p of panels('diagnostics')) {
      const s = d && d.last && d.last.summary;
      meta(p, running || (d && d.running) ? status('idle', 'Running…') : !s ? '' :
        s.fail ? status('critical', `${s.fail} failed`) : s.warn ? status('warning', `${s.warn} to look at`) : status('good', 'All clear'));
      fill(body(p), ...report(d));
    }
    const rows = (d && d.skills) || (Vesper.data.skills && Vesper.data.skills.health) || [];
    for (const p of panels('skill-switches')) {
      const counts = {};
      for (const r of rows) counts[r.state] = (counts[r.state] || 0) + 1;
      meta(p, rows.length ? `${counts.on || 0} on${counts.off ? ` · ${counts.off} off` : ''}${counts.crashed ? ` · ${counts.crashed} switched off` : ''}${counts.failed ? ` · ${counts.failed} failed` : ''}` : '');
      if (!rows.length) { fill(body(p), empty('Loading…')); continue; }
      const sorted = [...rows].sort((a, b) => (a.state === 'on') - (b.state === 'on'));  // problems first
      fill(body(p),
        h('div', { class: 't2 dg-intro' }, 'A skill that keeps failing is switched off for the session; turn it back on here. Off here stays off after a restart.'),
        h('div', { class: 'dg-skills' }, sorted.map(skillRow)));
    }
  }

  Vesper.skill('diagnostics', {
    panels: [{ name: 'diagnostics', view: 'setup', span: 4, title: 'Diagnostics · report to send', after: 'display' },
      { name: 'resources', view: 'setup', span: 4, title: 'What Vesper is using', after: 'diagnostics' },
      { name: 'skill-switches', view: 'setup', span: 8, title: 'Skills', after: 'display' }],
    render: paint,
  });
})();
