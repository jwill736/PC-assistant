'use strict';
/* Customisable layout: "Edit layout" (top bar, or Ctrl+K) lets J drag panels by their grip, move them with arrow
   buttons or the arrow keys, set a width (a third, half, two thirds, full) and hide them; hidden ones come back
   from "+ Add panel". Per tab, saved to data/settings.yaml (hud.layout) through /api/hud/layout, so a cleared
   browser keeps it; a copy in localStorage only avoids a flash on load. Moves animate with FLIP (transform only);
   with motion reduced or off they simply happen. Uses app.js's helpers (h, $, $$, api, post, toast, announce, FX). */
(() => {
  const SPANS = [[4, '⅓', 'A third of the width'], [6, '½', 'Half the width'], [8, '⅔', 'Two thirds of the width'], [12, 'Full', 'The full width']];
  const DIRS = { up: ['M8 12.5V3.5M4 7.5l4-4 4 4', 'up'], down: ['M8 3.5v9M4 8.5l4 4 4-4', 'down'],
    left: ['M12.5 8h-9M7.5 4l-4 4 4 4', 'left'], right: ['M3.5 8h9M8.5 4l4 4-4 4', 'right'] };
  const GRIP = 'M6 3.5h.01M10 3.5h.01M6 8h.01M10 8h.01M6 12.5h.01M10 12.5h.01';
  const EYE_OFF = 'M2 8s2.2-4.5 6-4.5S14 8 14 8s-2.2 4.5-6 4.5S2 8 2 8zM8 6a2 2 0 1 0 0 4M2.5 2.5l11 11';
  const L = { layout: {}, editing: false, me: Math.random().toString(36).slice(2, 10), timers: {}, menuOpen: false };

  const ic = d => s('svg', { viewBox: '0 0 16 16', 'aria-hidden': 'true' }, s('path', { d }));
  const sectionOf = view => $(`.view[data-view="${view}"]`);
  const current = () => sectionOf(S.view);
  const panelsIn = section => [...section.children].filter(el => el.classList.contains('panel'));
  const shown = section => panelsIn(section).filter(p => !p.hasAttribute('data-lay-hidden'));
  const spanOf = p => Number((/\bspan-(\d+)\b/.exec(p.className) || [])[1]) || 12;
  const title = p => $('h2', p)?.textContent.trim() || (p.dataset.panel === 'stats' ? 'Headline numbers' : p.dataset.panel || 'panel');
  const moving = () => FX.motion.full;

  /* Every panel gets a key that survives moves: its data-panel, and "#2" on a second one with the same name. */
  function assignKeys(section) {
    const taken = new Set(panelsIn(section).map(p => p.dataset.layKey).filter(Boolean));
    for (const p of panelsIn(section)) {
      if (p.dataset.layKey) continue;
      const name = (p.dataset.panel || 'panel').toLowerCase().replace(/[^a-z0-9_:-]/g, '');
      let k = name, n = 1;
      while (taken.has(k)) k = `${name}#${++n}`;
      taken.add(k);
      p.dataset.layKey = k;
      if (!p.dataset.laySpan0) p.dataset.laySpan0 = String(spanOf(p));
    }
  }
  function defaults(section) {
    if (!section._layDefault) section._layDefault = panelsIn(section).map(p => p.dataset.layKey);
    return section._layDefault;
  }
  function setSpan(p, n) {
    for (const c of [...p.classList]) if (/^span-\d+$/.test(c)) p.classList.remove(c);
    p.classList.add(`span-${n}`);
  }

  /* First, Last, Invert, Play: measure, change the DOM, then slide every panel from where it was. */
  function flip(section, mutate, skip) {
    if (!moving()) { mutate(); return; }
    const before = new Map(shown(section).map(p => [p, p.getBoundingClientRect()]));
    mutate();
    const after = shown(section);
    for (const p of after) for (const a of p.getAnimations()) if (a.id === 'lay-flip') a.cancel();
    for (const p of after) {
      if (p === skip) continue;
      const a = before.get(p), b = p.getBoundingClientRect();
      if (!b.width) continue;
      if (!a) {
        p.animate([{ opacity: 0, transform: 'scale(.96)' }, { opacity: 1, transform: 'none' }], { duration: 260, easing: 'cubic-bezier(.23,1,.32,1)' }).id = 'lay-flip';
        continue;
      }
      const dx = a.left - b.left, dy = a.top - b.top, sx = a.width / b.width, sy = a.height / b.height;
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1 && Math.abs(sx - 1) < 0.01 && Math.abs(sy - 1) < 0.01) continue;
      p.animate([{ transformOrigin: 'top left', transform: `translate(${dx}px, ${dy}px) scale(${sx}, ${sy})` },
        { transformOrigin: 'top left', transform: 'none' }], { duration: 340, easing: 'cubic-bezier(.23,1,.32,1)' }).id = 'lay-flip';
    }
  }

  // ---- applying a saved layout
  function apply(section, lay, animate) {
    assignKeys(section);
    const ps = panelsIn(section), byKey = new Map(ps.map(p => [p.dataset.layKey, p]));
    const def = defaults(section);
    const base = [...def.filter(k => byKey.has(k)), ...ps.map(p => p.dataset.layKey).filter(k => !def.includes(k))];
    let order = base;
    if (lay && lay.order && lay.order.length) {
      order = lay.order.filter(k => byKey.has(k));
      base.forEach((k, i) => {  // a panel the saved layout doesn't know (a new skill) goes after its usual neighbour
        if (order.includes(k)) return;
        let at = 0;
        for (let j = i - 1; j >= 0; j--) { const x = order.indexOf(base[j]); if (x >= 0) { at = x + 1; break; } }
        order.splice(at, 0, k);
      });
    }
    const hidden = new Set((lay && lay.hidden) || []), spans = (lay && lay.spans) || {};
    const mutate = () => {
      if (ps.map(p => p.dataset.layKey).join() !== order.join()) for (const k of order) section.append(byKey.get(k));
      for (const p of ps) {
        setSpan(p, spans[p.dataset.layKey] || Number(p.dataset.laySpan0) || spanOf(p));
        p.toggleAttribute('data-lay-hidden', hidden.has(p.dataset.layKey));
      }
    };
    if (animate && !section.hidden) flip(section, mutate); else mutate();
    if (typeof stagger === 'function') stagger(section);
  }
  function applyAll(animate) { for (const sec of $$('.view[data-view]')) apply(sec, L.layout[sec.dataset.view], animate); }

  function snapshot(section) {
    assignKeys(section);
    const ps = panelsIn(section), spans = {};
    for (const p of ps) if (spanOf(p) !== Number(p.dataset.laySpan0)) spans[p.dataset.layKey] = spanOf(p);
    return { order: ps.map(p => p.dataset.layKey), spans, hidden: ps.filter(p => p.hasAttribute('data-lay-hidden')).map(p => p.dataset.layKey) };
  }
  function cache() { try { localStorage.setItem('hud.layout', JSON.stringify(L.layout)); } catch { /* storage unavailable */ } }
  function save(section) {
    const view = section.dataset.view;
    L.layout[view] = snapshot(section);
    cache();
    clearTimeout(L.timers[view]);
    L.timers[view] = setTimeout(() => {
      post('/api/hud/layout', { view, layout: L.layout[view], by: L.me })
        .then(r => { if (r && r.ok === false) toast(r.error || 'Couldn’t save the layout.', 'error'); })
        .catch(e => toast(`Layout not saved: ${e.message}`, 'error'));
    }, 350);
    paintBar();
  }

  // ---- moving, resizing, hiding
  function box(el) { return { x: el.offsetLeft, y: el.offsetTop, w: el.offsetWidth, h: el.offsetHeight }; }
  function position(p) { const v = shown(p.parentElement); return `${v.indexOf(p) + 1} of ${v.length}`; }
  function move(p, dir, focusSel) {
    const section = p.parentElement, vis = shown(section), i = vis.indexOf(p);
    let target = null, after = false;
    if (dir === 'left') target = vis[i - 1];
    else if (dir === 'right') { target = vis[i + 1]; after = true; }
    else {
      const r = box(p), cx = r.x + r.w / 2;
      const others = vis.filter(x => x !== p).map(x => ({ x, r: box(x) }));
      const rows = dir === 'up' ? others.filter(o => o.r.y + o.r.h <= r.y + 4) : others.filter(o => o.r.y >= r.y + r.h - 4);
      if (rows.length) {
        const edge = dir === 'up' ? Math.max(...rows.map(o => o.r.y)) : Math.min(...rows.map(o => o.r.y));
        const row = rows.filter(o => Math.abs(o.r.y - edge) < 6).sort((a, b) => Math.abs(a.r.x + a.r.w / 2 - cx) - Math.abs(b.r.x + b.r.w / 2 - cx));
        target = row[0].x; after = dir === 'down';
      }
    }
    if (!target) {
      announce(`${title(p)} is already at the ${{ up: 'top', left: 'start', down: 'bottom', right: 'end' }[dir]}.`);
      if (moving()) p.animate([{ transform: 'none' }, { transform: `translate(${{ left: -4, right: 4 }[dir] || 0}px, ${{ up: -4, down: 4 }[dir] || 0}px)` }, { transform: 'none' }], { duration: 180 });
      return;
    }
    flip(section, () => (after ? target.after(p) : target.before(p)));
    if (focusSel) { $(focusSel, p)?.focus({ preventScroll: true }); p.scrollIntoView({ block: 'nearest', behavior: moving() ? 'smooth' : 'auto' }); }
    announce(`${title(p)} moved to ${position(p)}.`);
    save(section);
  }
  function resize(p, n) {
    if (spanOf(p) === n) return;
    flip(p.parentElement, () => setSpan(p, n));
    for (const b of $$('.lay-span button', p)) b.setAttribute('aria-pressed', String(Number(b.dataset.span) === n));
    announce(`${title(p)}: ${SPANS.find(x => x[0] === n)[2].toLowerCase()}.`);
    save(p.parentElement);
  }
  function hide(p) {
    const section = p.parentElement;
    const done = () => { flip(section, () => p.setAttribute('data-lay-hidden', '')); save(section); };
    if (moving()) p.animate([{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(.96)' }], { duration: 160, easing: 'cubic-bezier(.55,0,1,.45)' }).onfinish = done;
    else done();
    announce(`${title(p)} hidden. Bring it back with Add panel.`);
    $('.lay-bar .lay-add > .btn')?.focus();
  }
  function unhide(p) {
    const section = p.parentElement;
    flip(section, () => p.removeAttribute('data-lay-hidden'));
    decorate(p);
    save(section);
    requestAnimationFrame(() => { p.scrollIntoView({ block: 'nearest', behavior: moving() ? 'smooth' : 'auto' }); $('.lay-grip', p)?.focus({ preventScroll: true }); });
    announce(`${title(p)} is back at ${position(p)}.`);
  }

  /* Drag by the grip: the panel follows the pointer and the others slide aside as it passes their middle. */
  function dragStart(e, p) {
    if (e.button !== 0 || !e.isPrimary) return;
    e.preventDefault();
    const grip = e.currentTarget, section = p.parentElement;
    grip.setPointerCapture(e.pointerId);
    for (const a of p.getAnimations()) a.cancel();
    const r0 = p.getBoundingClientRect();
    const grab = { x: e.clientX - r0.left, y: e.clientY - r0.top };
    let at = { x: e.clientX, y: e.clientY }, lastSwap = 0, moved = false;
    p.classList.add('lay-dragging');
    document.body.classList.add('lay-drag-on');
    getSelection()?.removeAllRanges();
    const slot = h('div', { class: 'lay-slot', 'aria-hidden': 'true' });  // where it lands if let go now
    document.body.append(slot);
    const follow = () => {
      p.style.transform = '';
      const r = p.getBoundingClientRect();
      p.style.transform = `translate(${at.x - grab.x - r.left}px, ${at.y - grab.y - r.top}px)`;
      Object.assign(slot.style, { width: `${r.width}px`, height: `${r.height}px`, transform: `translate(${r.left}px, ${r.top}px)` });
    };
    follow();
    requestAnimationFrame(() => slot.classList.add('on'));
    const onMove = ev => {
      at = { x: ev.clientX, y: ev.clientY };
      if (!moved && Math.hypot(ev.clientX - e.clientX, ev.clientY - e.clientY) < 3) return;
      moved = true;
      const now = performance.now();
      if (now - lastSwap > 110) {
        const hit = hitTest(section, p, ev);
        if (hit) { lastSwap = now; flip(section, () => (hit.after ? hit.el.after(p) : hit.el.before(p)), p); }
      }
      follow();
      if (ev.clientY < 90) scrollBy(0, -14); else if (ev.clientY > innerHeight - 70) scrollBy(0, 14);
    };
    const onUp = () => {
      grip.removeEventListener('pointermove', onMove);
      grip.removeEventListener('pointerup', onUp);
      grip.removeEventListener('pointercancel', onUp);
      const from = p.style.transform;
      p.style.transform = '';
      slot.remove();
      p.classList.remove('lay-dragging');
      document.body.classList.remove('lay-drag-on');
      if (from && moving()) p.animate([{ transform: from }, { transform: 'none' }], { duration: 280, easing: 'cubic-bezier(.23,1,.32,1)' });
      if (moved) { announce(`${title(p)} moved to ${position(p)}.`); save(section); }
    };
    grip.addEventListener('pointermove', onMove);
    grip.addEventListener('pointerup', onUp);
    grip.addEventListener('pointercancel', onUp);
  }
  function hitTest(section, p, ev) {
    const op = p.offsetParent || document.body, o = op.getBoundingClientRect();
    const px = ev.clientX - o.left - op.clientLeft, py = ev.clientY - o.top - op.clientTop;
    const vis = shown(section), full = section.clientWidth - 4;
    for (const el of vis) {
      if (el === p) continue;
      const r = box(el);
      if (px < r.x || px > r.x + r.w || py < r.y || py > r.y + r.h) continue;
      const after = r.w >= full ? py > r.y + r.h / 2 : px > r.x + r.w / 2;
      const order = vis.filter(x => x !== p);
      order.splice(order.indexOf(el) + (after ? 1 : 0), 0, p);
      return order.every((x, i) => x === vis[i]) ? null : { el, after };
    }
    return null;
  }

  // ---- the editing chrome
  function decorate(p) {
    if ($(':scope > .lay-ctl', p)) return;
    const name = title(p);
    const grip = h('button', { class: 'lay-grip', type: 'button', title: 'Drag to move · arrow keys move it too', 'aria-label': `Move ${name}: drag, or use the arrow keys`,
      'aria-describedby': 'lay-help' }, ic(GRIP));
    grip.addEventListener('pointerdown', e => dragStart(e, p));
    grip.addEventListener('mousedown', e => e.preventDefault());  // no text selection while dragging
    grip.addEventListener('keydown', e => {
      const dir = { ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right' }[e.key];
      if (dir) { e.preventDefault(); move(p, dir, '.lay-grip'); }
    });
    const moves = h('span', { class: 'lay-moves' }, ['up', 'left', 'right', 'down'].map(d => h('button', {
      class: 'lay-mv', type: 'button', 'data-dir': d, title: `Move ${DIRS[d][1]}`, 'aria-label': `Move ${name} ${DIRS[d][1]}`,
      onclick: () => move(p, d, `.lay-mv[data-dir="${d}"]`) }, ic(DIRS[d][0]))));
    const span = spanOf(p);
    const widths = h('span', { class: 'seg lay-span', role: 'group', 'aria-label': `Width of ${name}` }, SPANS.map(([n, label, note]) => h('button', {
      type: 'button', 'data-span': n, 'aria-pressed': String(span === n), title: note, onclick: () => resize(p, n) }, label)));
    const hideBtn = h('button', { class: 'lay-hide', type: 'button', title: 'Hide this panel', 'aria-label': `Hide ${name}`, onclick: () => hide(p) }, ic(EYE_OFF));
    p.append(h('div', { class: 'lay-ctl', role: 'group', 'aria-label': `Arrange ${name}` }, grip, h('span', { class: 'lay-name' }, name), moves, widths, hideBtn));
  }
  function undecorate(root = document) { for (const c of $$('.lay-ctl', root)) c.remove(); }

  function paintBar() {
    const bar = $('#lay-bar');
    if (!bar) return;
    const section = current();
    const hidden = section ? panelsIn(section).filter(p => p.hasAttribute('data-lay-hidden')) : [];
    const tab = $(`.tabs button[data-view="${S.view}"]`)?.firstChild?.textContent.trim() || S.view;
    $('.lay-tab', bar).textContent = tab;
    const count = $('.lay-add .count', bar);
    count.textContent = hidden.length ? String(hidden.length) : '';
    $('.lay-add > .btn', bar).disabled = !hidden.length;
    const menu = $('.lay-menu', bar);
    fill(menu, hidden.length ? hidden.map(p => h('button', { type: 'button', role: 'menuitem', class: 'lay-mi', onclick: () => { closeMenu(); unhide(p); } },
      ic('M8 3.5v9M3.5 8h9'), title(p))) : h('div', { class: 'muted lay-none' }, 'Every panel on this tab is showing.'));
    if (!hidden.length) closeMenu();
  }
  function openMenu() {
    const btn = $('#lay-bar .lay-add > .btn'), menu = $('#lay-bar .lay-menu');
    menu.hidden = false; btn.setAttribute('aria-expanded', 'true'); L.menuOpen = true;
    $('.lay-mi', menu)?.focus();
  }
  function closeMenu() {
    const btn = $('#lay-bar .lay-add > .btn'), menu = $('#lay-bar .lay-menu');
    if (!menu) return;
    menu.hidden = true; btn?.setAttribute('aria-expanded', 'false'); L.menuOpen = false;
  }
  function buildBar() {
    if ($('#lay-bar')) return;
    const addBtn = h('button', { class: 'btn small', type: 'button', 'aria-haspopup': 'menu', 'aria-expanded': 'false',
      onclick: () => (L.menuOpen ? closeMenu() : openMenu()) }, '+ Add panel', h('span', { class: 'count' }));
    const menu = h('div', { class: 'lay-menu', role: 'menu', hidden: true, 'aria-label': 'Hidden panels' });
    menu.addEventListener('keydown', e => {
      const items = $$('.lay-mi', menu), i = items.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length]?.focus(); }
      if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length]?.focus(); }
    });
    const bar = h('div', { class: 'lay-bar', id: 'lay-bar', role: 'region', 'aria-label': 'Layout editor' },
      h('span', { class: 'lay-dot', 'aria-hidden': 'true' }),
      h('div', { class: 'lay-txt' }, h('div', { class: 'lay-h' }, 'Editing ', h('b', { class: 'lay-tab' }), ' layout'),
        h('div', { class: 'lay-sub', id: 'lay-help' }, 'Drag a grip, or use the arrows. Esc when done.')),
      h('div', { class: 'lay-add' }, addBtn, menu),
      h('button', { class: 'btn small', type: 'button', onclick: resetTab }, 'Reset tab'),
      h('button', { class: 'btn small ghost', type: 'button', onclick: resetAll }, 'Reset all'),
      h('button', { class: 'btn small primary', type: 'button', onclick: () => setEditing(false) }, 'Done'));
    document.body.append(bar);
    paintBar();
  }

  function resetTab() {
    const section = current();
    if (!section) return;
    delete L.layout[section.dataset.view];
    cache();
    apply(section, null, true);
    undecorate(section); for (const p of shown(section)) decorate(p);
    post('/api/hud/layout', { view: section.dataset.view, reset: true, by: L.me }).catch(e => toast(`Not saved: ${e.message}`, 'error'));
    paintBar();
    toast('This tab is back to the standard layout.');
  }
  function resetAll() {
    if (!confirm('Put every tab back to the standard layout? Hidden panels come back too.')) return;
    L.layout = {};
    cache();
    applyAll(true);
    const section = current();
    if (section && L.editing) { undecorate(); for (const p of shown(section)) decorate(p); }
    post('/api/hud/layout', { reset_all: true, by: L.me }).catch(e => toast(`Not saved: ${e.message}`, 'error'));
    paintBar();
    toast('Every tab is back to the standard layout.');
  }

  function setEditing(on) {
    on = !!on;
    if (on === L.editing) return;
    L.editing = on;
    document.body.classList.toggle('layout-edit', on);
    $('#layout-btn')?.setAttribute('aria-pressed', String(on));
    if (on) {
      if (typeof clearActive === 'function') clearActive();
      buildBar();
      for (const p of shown(current())) decorate(p);
      announce(`Editing the ${S.view} layout. Each panel has a grip, arrows, widths and a hide button.`);
      $('.lay-grip', current())?.focus({ preventScroll: true });
    } else {
      closeMenu();
      undecorate();
      $('#lay-bar')?.remove();
      announce('Layout saved.');
      $('#layout-btn')?.focus({ preventScroll: true });
    }
  }
  const toggle = () => setEditing(!L.editing);

  // ---- wiring
  window.addEventListener('DOMContentLoaded', () => {  // after app.js has put the skills' panels in place
    for (const sec of $$('.view[data-view]')) { assignKeys(sec); defaults(sec); }
    try { const c = JSON.parse(localStorage.getItem('hud.layout') || 'null'); if (c && typeof c === 'object') { L.layout = c; applyAll(false); } } catch { /* none */ }
    api('/api/hud/layout').then(r => { L.layout = (r && r.layout) || {}; cache(); applyAll(false); if (L.editing) paintBar(); })
      .catch(() => { /* offline: the cached copy stands */ });
  });
  $('#layout-btn')?.addEventListener('click', toggle);
  document.addEventListener('keydown', e => {
    if (!L.editing || e.key !== 'Escape' || document.querySelector('dialog[open]')) return;
    e.preventDefault();
    if (L.menuOpen) { closeMenu(); $('#lay-bar .lay-add > .btn')?.focus(); } else setEditing(false);
  });
  document.addEventListener('pointerdown', e => { if (L.menuOpen && !e.target.closest('.lay-add')) closeMenu(); });
  document.addEventListener('selectstart', e => { if (document.body.classList.contains('lay-drag-on')) e.preventDefault(); });
  Vesper.on('hud:view', () => {
    if (!L.editing) return;
    undecorate();
    for (const p of shown(current())) decorate(p);
    paintBar();
  });
  Vesper.on('hud_layout', data => {  // another window (or the phone) changed it
    if (!data || data.by === L.me) return;
    L.layout = data.layout || {};
    cache();
    applyAll(true);
    if (L.editing) { undecorate(); for (const p of shown(current())) decorate(p); paintBar(); }
  });
  PAL_ACTIONS.unshift(['Edit layout', toggle, 'move, resize and hide panels']);
  PAL_ACTIONS.push(['Reset this tab’s layout', resetTab, 'layout'], ['Reset every tab’s layout', resetAll, 'layout']);
  Vesper.layout = { edit: setEditing, toggle, get editing() { return L.editing; }, get data() { return L.layout; } };
})();
