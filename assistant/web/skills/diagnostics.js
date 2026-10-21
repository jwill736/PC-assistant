/* Diagnostics: one report to send (Setup → Diagnostics), and Setup → Skills: every skill with an on/off switch. */
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

  function paint(d) {
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
      { name: 'skill-switches', view: 'setup', span: 8, title: 'Skills', after: 'display' }],
    render: paint,
  });
})();
