/* Click by name: the last "what can I click here" (the controls Vesper can see in that window) and the last thing it
   pressed. Windows only. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, status } = Vesper.ui;
  function paint(d) {
    for (const p of panels('click')) {
      const b = body(p);
      if (!d) { fill(b, empty('Loading…')); continue; }
      if (!d.available) { meta(p, 'Windows only'); fill(b, empty('Clicking by name uses Windows UI Automation, so it works on your Windows PC only.')); continue; }
      const f = d.found, last = d.last;
      meta(p, f ? `${f.app} · ${ago(f.at)}` : 'any app');
      fill(b,
        last ? h('div', { class: 'clk-last' }, last.ok ? status('good', 'Done') : status('warning', 'Didn’t work'),
          h('span', { class: 't3' }, ` ${last.text}`), h('span', { class: 't3 muted' }, ` · ${ago(last.at)}${last.error ? ` · ${last.error}` : ''}`)) : null,
        d.choices?.length ? h('div', { class: 'clk-choices t3' }, 'Which one? Say “the first one”, “the second one”…',
          h('ol', {}, d.choices.map(c => h('li', {}, c)))) : null,
        f ? h('div', { class: 'clk-found' },
          h('div', { class: 't3 muted' }, f.hidden ? `${f.hidden} things to click in ${f.app}: hidden while you're live`
            : `${f.controls.length} things to click in ${f.app}${f.cut ? ' (a big window: the first part)' : ''}`),
          h('div', { class: 'clk-pills' }, f.controls.map(c => h('span', { class: 'pill', title: c.kind }, c.name))))
          : empty('Say “Vesper, what can I click here?”, “click Export in Premiere” or “turn on the Dark mode toggle in Settings”. It asks before pressing anything in another app.'));
    }
  }
  Vesper.skill('click', { panels: [{ view: 'pc', span: 6, title: 'Click by name', after: 'undo' }], render: paint });
})();
