/* Dictation: a small chip while you dictate (hold the hotkey, or "Vesper, dictate"), and the last few dictations
   with a Copy button. Nothing here is logged; it lives in memory until Vesper restarts. */
(() => {
  const { h, fill, empty, meta, panels, body, ago, toast, status } = Vesper.ui;
  const HOW = { typed: ['good', 'Typed'], pasted: ['good', 'Pasted'], kept: ['warning', 'Not typed'] };
  const keys = combo => (combo || '').split('+').filter(Boolean).map(k => h('kbd', {}, k.length === 1 ? k.toUpperCase() : k[0].toUpperCase() + k.slice(1)));

  // ---- the chip: bottom centre, only while something is happening ----
  let chip = null;
  function paintChip(d) {
    if (!chip) {
      chip = h('div', { class: 'dict-chip', role: 'status', 'aria-live': 'polite' });
      document.body.append(chip);
    }
    const st = d && d.state;
    const active = st === 'recording' || st === 'transcribing' || st === 'typing' || (d && d.awaiting);
    chip.classList.toggle('on', !!active);
    chip.dataset.state = d && d.awaiting && st === 'idle' ? 'awaiting' : st || 'idle';
    if (!active) return;
    const text = st === 'recording' ? ['Dictating · let go of ', keys(d.hotkey), ' to type it']
      : st === 'transcribing' ? 'Turning it into text…'
      : st === 'typing' ? 'Typing…'
      : 'Go ahead: the next thing you say is typed';
    fill(chip, h('span', { class: 'dict-dot', 'aria-hidden': 'true' }), h('span', {}, text));
  }

  async function copy(id) {
    const r = await Vesper.act('dictation', 'copy', { id });
    toast(r.ok ? 'Dictation copied.' : r.error, { source: 'dictation', level: r.ok ? 'good' : 'warn' });
  }

  function paint(d) {
    paintChip(d);
    for (const p of panels('dictation')) {
      const b = body(p);
      if (!d) { fill(b, empty('Loading…')); continue; }
      const mode = d.mode === 'hold' ? 'hold to talk' : d.mode === 'toggle' ? 'press to start, again to stop' : 'hotkey off';
      meta(p, d.state === 'idle' ? mode : d.state);
      const recent = d.recent || [];
      fill(b,
        h('div', { class: 'dict-how' },
          d.hotkey && d.mode !== 'off' ? h('span', {}, keys(d.hotkey), h('span', { class: 'muted' }, d.mode === 'hold' ? ' hold, talk, let go' : ' press, talk, press again'))
            : h('span', { class: 'muted' }, 'The dictation hotkey is off (skills.dictation.hotkey).'),
          h('span', { class: 'muted' }, 'or say “Vesper, dictate”')),
        !d.available ? h('div', { class: 'empty' }, status('warning', 'Vesper can’t type on this PC (Windows only).')) : null,
        d.note ? h('div', { class: 'dict-note' }, status('warning', d.note)) : null,
        recent.length ? h('div', { class: 'list dict-list' }, recent.map((r, i) => {
          const [lvl, label] = HOW[r.how] || ['info', r.how];
          return h('div', { class: `item dict-item ${i === 0 ? 'dict-latest' : ''}` },
            h('div', { class: 'main' },
              h('div', { class: 'dict-text' }, r.text),
              h('div', { class: 't2' }, status(lvl, label), ` ${r.app ? `into ${r.app} · ` : ''}${ago(r.at)} · ${r.chars.toLocaleString()} chars`,
                r.polished ? h('span', { class: 'dict-ai', title: 'Tidied by the model' }, ' · tidied') : null)),
            h('button', { class: `btn small ${i === 0 ? 'primary' : ''}`, onclick: () => copy(r.id) }, 'Copy'));
        })) : empty('Click into any app, hold the keys and talk. Say “comma”, “period” or “new line” for punctuation; “um” and “uh” are dropped.'),
        recent.length ? h('div', { class: 'dict-foot' }, h('span', { class: 't3 muted' }, 'Kept in memory only, never logged.'),
          h('button', { class: 'btn small', onclick: () => Vesper.act('dictation', 'clear') }, 'Clear')) : null);
    }
  }
  Vesper.skill('dictation', { panels: [{ view: 'pc', span: 6, title: 'Dictation', after: 'clipboard' }], render: paint });
})();
