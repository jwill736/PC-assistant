/* Stream co-pilot: live timer, viewers, chat mood, questions to answer, who to greet, and the post-stream recap. HUD only. */
(() => {
  const { h, fill, empty, meta, panels, body, toast } = Vesper.ui;
  const NS = 'http://www.w3.org/2000/svg';
  const svg = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v); return e; };
  const clock = s => { s = Math.max(0, Math.floor(s)); return `${Math.floor(s / 3600)}:${String(Math.floor(s % 3600 / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`; };
  const age = s => (s < 60 ? `${s}s` : `${Math.floor(s / 60)}m`);
  const MOOD = { hyped: 'good', happy: 'good', steady: 'idle', quiet: 'idle', restless: 'warning', salty: 'critical' };
  let timer = null; // ticks the live clock every second
  async function act(action, payload, ok) {
    const r = await Vesper.act('stream_copilot', action, payload);
    if (r.ok === false) toast(r.error || 'That didn’t work.'); else if (ok) toast(typeof ok === 'function' ? ok(r) : ok);
    return r;
  }
  const copy = (text, what) => navigator.clipboard.writeText(text).then(() => toast(`${what} copied.`), () => toast('Couldn’t copy: select it and copy by hand.'));
  const copyBtn = (text, what) => h('button', { class: 'btn small', onclick: () => copy(text, what) }, 'Copy');

  // Mood gauge: three arcs (salty · steady · hyped) and a needle that swings with transform only.
  function gauge() {
    const s = svg('svg', { viewBox: '0 0 120 70', class: 'sc-gauge', role: 'img', 'aria-label': 'Chat mood' });
    s.append(svg('path', { d: 'M10 62 A50 50 0 0 1 35 18.7', class: 'sc-arc neg' }), svg('path', { d: 'M35 18.7 A50 50 0 0 1 85 18.7', class: 'sc-arc mid' }),
      svg('path', { d: 'M85 18.7 A50 50 0 0 1 110 62', class: 'sc-arc pos' }));
    const needle = svg('g', { class: 'sc-needle' });
    needle.append(svg('line', { x1: 60, y1: 62, x2: 60, y2: 20 }), svg('circle', { cx: 60, cy: 62, r: 4 }));
    s.append(needle); s._needle = needle;
    return s;
  }
  function spark(values) {
    const w = 220, ht = 40, s = svg('svg', { viewBox: `0 0 ${w} ${ht}`, class: 'sc-spark', role: 'img', 'aria-label': 'Viewers over time' });
    if (values.length < 2) return s;
    const mx = Math.max(...values, 1) * 1.1, x = i => (i / (values.length - 1)) * (w - 6) + 1, y = v => ht - 3 - (v / mx) * (ht - 8);
    const d = values.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('');
    s.append(svg('path', { d: `${d}L${x(values.length - 1)},${ht - 3}L1,${ht - 3}Z`, class: 'sc-wash' }), svg('path', { d, class: 'sc-line', pathLength: 1 }),
      svg('circle', { cx: x(values.length - 1), cy: y(values[values.length - 1]), r: 3, class: 'sc-dot' }));
    return s;
  }
  const stat = (label, value) => h('div', { class: 'sc-stat' }, h('div', { class: 'num sc-v' }, value ?? '—'), h('div', { class: 't3 muted' }, label));

  function questions(d) {
    const qs = d.questions || [];
    return h('section', { class: 'sc-col' }, h('h3', {}, 'Questions in chat', h('span', { class: 'muted' }, qs.length ? ` · ${qs.length}` : '')),
      qs.length ? h('div', { class: 'list' }, qs.map(q => h('div', { class: `item sc-q ${q.late ? 'late' : ''}` },
        h('div', { class: 'main' }, h('div', { class: 't2' }, `${q.user} · ${age(q.age_s)} ago${q.late ? ' · waiting' : ''}`), h('div', { class: 't1' }, q.text)),
        h('button', { class: 'btn small icon', title: 'Answered / not for me', 'aria-label': 'Dismiss question', onclick: () => act('dismiss', { id: q.id }) }, '✕'))))
        : empty('No open questions. Say the asker’s name or the topic out loud and they clear themselves.'));
  }
  function greet(d) {
    const g = d.greet || [];
    return h('section', { class: 'sc-col' }, h('h3', {}, 'Say hi to'),
      g.length ? h('div', { class: 'list' }, g.map(x => h('div', { class: 'item' },
        h('div', { class: 'main' }, h('div', { class: 't1' }, x.user), h('div', { class: 't2' }, x.kind === 'new' ? 'first time in chat' : `regular · ${x.sessions} streams`)),
        h('button', { class: 'btn small', onclick: () => act('greeted', { user: x.user }) }, 'Done'))))
        : empty('First-time chatters and regulars show up here while you’re live.'));
  }
  function side(d) {
    const out = [];
    if (d.hint) out.push(h('div', { class: 'sc-card sc-hint' }, h('div', { class: 't1' }, `Playing ${d.hint.game}, category is ${d.hint.category}`),
      h('button', { class: 'btn small primary', onclick: () => act('switch_category', { game: d.hint.game }, r => r.reply || 'Asked Twitch to switch.') }, `Switch to ${d.hint.game}`)));
    if (d.plan) out.push(h('div', { class: 'sc-card' }, h('div', { class: 't2' }, 'Up next'), h('div', { class: 't1' }, d.plan.event),
      h('div', { class: 't3' }, [d.plan.game, d.plan.title].filter(Boolean).join(' · ') || 'No game in the event title'),
      h('div', { class: 'sc-row' }, h('button', { class: 'btn small', onclick: () => act('prestream', {}, r => (r.spoken || 'Checked.')) }, 'Pre-stream check'),
        h('button', { class: 'btn small', onclick: () => act('suggest_title', {}) }, 'Suggest titles'))));
    if ((d.titles || []).length) out.push(h('div', { class: 'sc-card' }, h('div', { class: 't2' }, 'Title ideas'),
      d.titles.map(t => h('div', { class: 'sc-copyrow' }, h('span', { class: 't3' }, t), copyBtn(t, 'Title')))));
    return h('section', { class: 'sc-col' }, h('h3', {}, 'Heads-up'), out.length ? out : empty('Category hints and the go-live plan land here.'));
  }
  function recap(r) {
    if (!r) return empty('After you go offline, the recap shows up here: chapters for the VOD and posts ready to copy.');
    const posts = r.posts || {};
    return h('div', { class: 'sc-recap' },
      h('div', { class: 'sc-stats' }, stat('length', r.duration), stat('peak viewers', r.peak_viewers), stat('avg viewers', r.avg_viewers),
        stat('new follows', r.follows), stat('subs', r.subs), stat('bits', r.bits), stat('raids', (r.raids || []).length), stat('msgs / min', r.avg_rate)),
      h('div', { class: `t3 sc-health ${r.health?.clean ? 'ok' : 'bad'}` }, r.health?.text || ''),
      h('div', { class: 'sc-recap-grid' },
        h('div', { class: 'sc-card' }, h('div', { class: 'sc-row' }, h('span', { class: 't2' }, 'VOD chapters'), h('span', { class: 'spacer' }), copyBtn((r.chapters || []).join('\n'), 'Chapters')),
          h('pre', { class: 'sc-pre' }, (r.chapters || []).join('\n'))),
        ['x', 'discord'].map(k => h('div', { class: 'sc-card' }, h('div', { class: 'sc-row' }, h('span', { class: 't2' }, k === 'x' ? 'Post for X' : 'Post for Discord'), h('span', { class: 'spacer' }),
          copyBtn(posts[k] || '', 'Post')), h('pre', { class: 'sc-pre' }, posts[k] || '')))));
  }

  function paint(d) {
    for (const p of panels('stream_copilot')) {
      const b = body(p);
      if (!d) { fill(b, empty('Loading…')); continue; }
      if (!b._top) {
        b._timer = h('div', { class: 'num sc-timer' }); b._gauge = gauge(); b._mood = h('div', { class: 't3' }); b._spark = h('div', { class: 'sc-sparkbox' });
        b._viewers = h('div', { class: 't3' }); b._note = h('div', { class: 't3 muted sc-note' }); b._cols = h('div', { class: 'sc-cols' }); b._recap = h('div', {});
        b._top = h('div', { class: 'sc-top' }, h('div', { class: 'sc-live' }, b._timer, b._viewers, b._spark), h('div', { class: 'sc-moodbox' }, b._gauge, b._mood));
        fill(b, b._note, b._top, b._cols, h('h3', { class: 'sc-h' }, 'Last stream'), b._recap);
      }
      const n = d.now || {}, m = d.mood || { score: 0, label: 'quiet', rate: 0 };
      p.classList.toggle('sc-on', !!d.live);
      meta(p, d.live ? h('span', { class: 'sc-badge' }, h('span', { class: 'sc-pulse' }), 'LIVE') : 'offline');
      b._note.textContent = [d.error, d.note].filter(Boolean).join(' ');
      b._timer.dataset.start = d.start || '';
      b._timer.textContent = d.live && d.start ? clock(Date.now() / 1000 - d.start) : '0:00:00';
      b._viewers.textContent = d.live ? `${n.viewers ?? '—'} watching · peak ${n.peak_viewers ?? '—'} · ${n.follows || 0} follows · ${n.subs || 0} subs` : 'Not live';
      b._spark.replaceChildren(spark(d.viewers || []));
      b._gauge._needle.style.transform = `rotate(${Math.max(-1, Math.min(1, m.score)) * 90}deg)`;
      b._gauge.setAttribute('aria-label', `Chat mood ${m.label}`);
      fill(b._mood, h('span', { class: `status ${MOOD[m.label] || 'idle'}` }, h('span', { class: 'dot' }), m.label), ` · ${m.rate}/min`);
      fill(b._cols, questions(d), greet(d), side(d));
      fill(b._recap, recap(d.recap));
    }
    clearInterval(timer);
    if (d?.live && d.start) timer = setInterval(() => {
      if (document.hidden) return;
      for (const p of panels('stream_copilot')) { const t = body(p)._timer; if (t) t.textContent = clock(Date.now() / 1000 - d.start); }
    }, 1000);
  }
  Vesper.skill('stream_copilot', { panels: [{ view: 'stream', span: 12, title: 'Co-pilot', after: 'stats' }], render: paint });
})();
