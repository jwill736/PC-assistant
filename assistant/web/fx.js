'use strict';
/* HUD effects (window.FX): the motion setting, one shared animation loop that sleeps when nothing changes,
   the canvas orb, number tickers and text reveals. No dependency on app.js, which drives all of it.
   Rules: only transform/opacity loop; nothing animates while the window is hidden; "reduced" keeps the states
   without travel; "off" keeps nothing moving at all. */
(() => {
  const root = document.documentElement;
  const mq = matchMedia('(prefers-reduced-motion: reduce)');

  // ------------------------------------------------------------ motion setting (Setup → Display)
  const motionListeners = [];
  const motion = {
    get mode() { return root.dataset.motion || 'full'; },
    get full() { return this.mode === 'full'; },
    stored() { try { return localStorage.getItem('hud.motion'); } catch { return null; } },
    /* 'full' | 'reduced' | 'off', or null to follow Windows' "Animation effects" again */
    set(mode) {
      try { if (mode) localStorage.setItem('hud.motion', mode); else localStorage.removeItem('hud.motion'); } catch { /* storage unavailable */ }
      root.dataset.motion = mode || (mq.matches ? 'reduced' : 'full');
      for (const fn of motionListeners) { try { fn(root.dataset.motion); } catch (err) { console.error(err); } }
    },
    on(fn) { motionListeners.push(fn); },
  };
  mq.addEventListener?.('change', () => { if (!motion.stored()) motion.set(null); });

  // ------------------------------------------------------------ the one rAF loop
  /* Subscribers are called every frame with (t, dt) and return false when they're done. With no subscribers,
     or while the window is hidden, no frame is requested at all. */
  const subs = new Set();
  let running = false, lastT = 0;
  function frame(t) {
    const dt = lastT ? Math.min(64, t - lastT) : 16.7;
    lastT = t;
    for (const fn of [...subs]) {
      let keep = false;
      try { keep = fn(t, dt); } catch (err) { console.error(err); }
      if (keep === false) subs.delete(fn);
    }
    if (subs.size && !document.hidden) requestAnimationFrame(frame);
    else { running = false; lastT = 0; }
  }
  const loop = {
    add(fn) { subs.add(fn); if (!running && !document.hidden) { running = true; requestAnimationFrame(frame); } },
    remove(fn) { subs.delete(fn); },
    get size() { return subs.size; },
  };
  function onVisibility() {
    if (document.hidden) root.dataset.hidden = '';
    else {
      delete root.dataset.hidden;
      if (subs.size && !running) { running = true; requestAnimationFrame(frame); }
    }
  }
  document.addEventListener('visibilitychange', onVisibility);
  onVisibility();

  // ------------------------------------------------------------ colours (theme tokens, read at draw time)
  const css = name => getComputedStyle(root).getPropertyValue(name).trim();
  function oklch(str) {
    const m = /oklch\(\s*([\d.]+)(%?)\s+([\d.]+)\s+([\d.]+)/.exec(str || '');
    if (!m) return null;
    return [m[2] ? Number(m[1]) / 100 : Number(m[1]), Number(m[3]), Number(m[4])];
  }
  const colStr = (c, a = 1) => `oklch(${c[0].toFixed(4)} ${c[1].toFixed(4)} ${c[2].toFixed(2)} / ${a.toFixed(3)})`;
  function mix(a, b, p) {
    let dh = b[2] - a[2];
    if (dh > 180) dh -= 360; else if (dh < -180) dh += 360;
    return [a[0] + (b[0] - a[0]) * p, a[1] + (b[1] - a[1]) * p, (a[2] + dh * p + 360) % 360];
  }
  const easeOut = p => 1 - Math.pow(1 - p, 3);

  // ------------------------------------------------------------ the orb
  /* Geometry: an outer dial of 60 ticks (the ones under the level light up), a thin ring with rotating arcs,
     dashed counter-rotating rings while thinking, a hexagon whose vertices push out with the audio level, and a
     core. Colour says who holds the floor: ion = listening to J, iris = thinking, ember = speaking, grey = idle. */
  const ACCENT = { listen: '--ion', think: '--iris', speak: '--ember', error: '--alert' };
  const ANIMATED = new Set(['listen', 'think', 'speak', 'loading']);
  const TAU = Math.PI * 2;
  const orb = {
    views: [],
    state: 'off',
    level: 0, target: 0, lastLevelAt: -1e9, prev: 0,
    col: null, from: null, to: null, colT: 1,
    rot: 0, rot2: 0, rot3: 0,
    ripples: [], lastRipple: 0, lastDraw: 0,
    pal: null,

    attach(canvas, { mini = false } = {}) {
      if (!canvas || !canvas.getContext) return;
      const v = { el: canvas, ctx: canvas.getContext('2d'), mini, size: 0, dpr: 1 };
      this.views.push(v);
      const fit = () => {
        const r = canvas.getBoundingClientRect();
        const size = Math.round(Math.min(r.width, r.height)) || (mini ? 36 : 120);
        const dpr = Math.min(2, window.devicePixelRatio || 1);
        if (size === v.size && dpr === v.dpr) return;
        v.size = size; v.dpr = dpr;
        canvas.width = Math.round(size * dpr); canvas.height = Math.round(size * dpr);
        this.draw(performance.now());
      };
      if ('ResizeObserver' in window) new ResizeObserver(fit).observe(canvas);
      fit();
    },
    refresh() {  // theme changed: re-read the tokens
      const get = n => oklch(css(n)) || [0.7, 0.01, 250];
      this.pal = {
        line: css('--line-2') || '#333', grid: css('--grid') || '#222', ink: css('--ink') || '#eee', muted: get('--muted'),
        idle: get('--muted'), ion: get('--ion'), iris: get('--iris'), ember: get('--ember'), alert: get('--alert'),
      };
      this.to = this.accentFor(this.state);
      if (!this.col) this.col = this.to;
      this.colT = 1; this.col = this.to;
      this.draw(performance.now());
    },
    accentFor(state) {
      const key = ACCENT[state];
      return key ? this.pal[key.slice(2)] : this.pal.idle;
    },
    setState(state) {
      if (!this.pal) this.refresh();
      if (state === this.state) return;
      this.state = state;
      this.from = this.col || this.accentFor(state);
      this.to = this.accentFor(state);
      this.colT = motion.mode === 'off' ? 1 : 0;
      if (this.colT === 1) this.col = this.to;
      this.wake();
    },
    setLevel(rms) {
      const v = Math.max(0, Math.min(1, Number(rms) || 0));
      this.target = Math.min(1, Math.pow(v, 0.8) * 1.15);
      this.lastLevelAt = performance.now();
      this.wake();
    },
    wake() { loop.add(this._tick || (this._tick = (t, dt) => this.tick(t, dt))); },

    tick(t, dt) {
      const full = motion.mode === 'full';
      const k = dt / 16.67;
      // speaking with no level stream (Windows/browser voices): a soft synthetic cadence
      if (this.state === 'speak' && t - this.lastLevelAt > 400) {
        this.target = 0.22 + 0.2 * (0.5 + 0.5 * Math.sin(t * 0.0105)) * (0.6 + 0.4 * Math.sin(t * 0.0041 + 1));
      } else if (t - this.lastLevelAt > 140) {
        this.target *= Math.pow(0.86, k);
      }
      if (!['listen', 'speak'].includes(this.state) && t - this.lastLevelAt > 400) this.target = 0;
      const a = this.target > this.level ? 0.42 : 0.14;
      this.prev = this.level;
      this.level += (this.target - this.level) * (1 - Math.pow(1 - a, k));
      if (this.level < 0.002 && this.target < 0.002) this.level = 0;
      if (this.colT < 1) {
        this.colT = Math.min(1, this.colT + dt / 260);
        this.col = mix(this.from, this.to, easeOut(this.colT));
      }
      if (full) {
        const sp = { listen: 0.00032, think: 0.0011, speak: 0.00045, loading: 0.0016 }[this.state] || 0;
        this.rot = (this.rot + dt * sp) % TAU;
        this.rot2 = (this.rot2 + dt * sp * 0.6) % TAU;
        this.rot3 = (this.rot3 - dt * sp * 0.9) % TAU;
        if (this.state === 'speak' && this.level > 0.42 && this.level - this.prev > 0.012 && t - this.lastRipple > 300) {
          this.ripples.push(t); this.lastRipple = t;
          if (this.ripples.length > 3) this.ripples.shift();
        }
        this.ripples = this.ripples.filter(r0 => t - r0 < 900);
      } else this.ripples.length = 0;
      const changing = this.colT < 1 || Math.abs(this.target - this.level) > 0.002 || this.level > 0.002;
      const keep = (full && (ANIMATED.has(this.state) || this.ripples.length)) || changing;
      // full motion: every frame; reduced: at most 10 Hz (a meter, not an animation)
      if (full || t - this.lastDraw > 100 || !keep) { this.draw(t); this.lastDraw = t; }
      return keep;
    },

    draw(t) {
      if (!this.pal) return;
      for (const v of this.views) {
        if (!v.size) continue;
        const { ctx } = v;
        ctx.setTransform(v.dpr, 0, 0, v.dpr, 0, 0);
        ctx.clearRect(0, 0, v.size, v.size);
        ctx.save();
        ctx.translate(v.size / 2, v.size / 2);
        if (v.mini) this.drawMini(ctx, v.size / 2 - 1.5, t); else this.drawFull(ctx, v.size / 2 - 2, t);
        ctx.restore();
      }
    },

    drawFull(ctx, R, t) {
      const P = this.pal, col = this.col || P.idle, lv = this.level, st = this.state;
      const live = ANIMATED.has(st) || st === 'error';
      const A = a => colStr(col, a);
      // halo
      if (live || lv > 0.01) {
        const g = ctx.createRadialGradient(0, 0, R * 0.1, 0, 0, R);
        g.addColorStop(0, A(0.16 + 0.22 * lv)); g.addColorStop(0.55, A(0.05 + 0.06 * lv)); g.addColorStop(1, A(0));
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(0, 0, R, 0, TAU); ctx.fill();
      }
      // dial: 60 ticks, long every 5th; the ones under the level are lit (clockwise from the top)
      const N = 60, lit = Math.round(lv * N);
      ctx.lineCap = 'butt';
      for (const pass of [0, 1]) {
        ctx.beginPath();
        for (let i = 0; i < N; i++) {
          const on = (i < lit) === !!pass;
          if (!on) continue;
          const ang = -Math.PI / 2 + (i / N) * TAU;
          const r1 = R * 0.97, r0 = R * (i % 5 === 0 ? 0.86 : 0.91);
          ctx.moveTo(Math.cos(ang) * r0, Math.sin(ang) * r0); ctx.lineTo(Math.cos(ang) * r1, Math.sin(ang) * r1);
        }
        ctx.strokeStyle = pass ? A(0.95) : P.line; ctx.lineWidth = pass ? 1.6 : 1; ctx.stroke();
      }
      // ring with rotating arcs
      ctx.beginPath(); ctx.arc(0, 0, R * 0.78, 0, TAU); ctx.strokeStyle = P.grid; ctx.lineWidth = 1; ctx.stroke();
      if (live || st === 'standby') {
        ctx.lineWidth = 2; ctx.strokeStyle = A(st === 'standby' ? 0.5 : 0.95);
        for (const [s0, s1] of st === 'standby' ? [[-0.12, 0.12]] : [[0, 0.95], [2.25, 2.6], [4.1, 4.28]]) {
          ctx.beginPath(); ctx.arc(0, 0, R * 0.78, this.rot + s0 - Math.PI / 2, this.rot + s1 - Math.PI / 2); ctx.stroke();
        }
      }
      if (st === 'standby') {  // the mic is live: one ion bead at the top
        ctx.fillStyle = colStr(P.ion, 0.9); ctx.beginPath(); ctx.arc(0, -R * 0.78, 2.2, 0, TAU); ctx.fill();
      }
      // thinking: two dashed counter-rotating rings and an orbiting bead
      if (st === 'think') {
        ctx.setLineDash([2, 5]); ctx.lineWidth = 1.2;
        ctx.strokeStyle = A(0.8); ctx.beginPath(); ctx.arc(0, 0, R * 0.66, this.rot2, this.rot2 + TAU); ctx.stroke();
        ctx.lineDashOffset = 0; ctx.setLineDash([6, 4, 1, 4]); ctx.strokeStyle = A(0.5);
        ctx.beginPath(); ctx.arc(0, 0, R * 0.58, this.rot3, this.rot3 + TAU); ctx.stroke();
        ctx.setLineDash([]);
        const oa = this.rot * 3.2 - Math.PI / 2;
        ctx.fillStyle = A(1); ctx.beginPath(); ctx.arc(Math.cos(oa) * R * 0.78, Math.sin(oa) * R * 0.78, 2.6, 0, TAU); ctx.fill();
      }
      // ripples while speaking
      for (const r0 of this.ripples) {
        const p = (t - r0) / 900;
        ctx.beginPath(); ctx.arc(0, 0, R * (0.48 + 0.5 * easeOut(p)), 0, TAU);
        ctx.strokeStyle = A(0.55 * (1 - p)); ctx.lineWidth = 1.5; ctx.stroke();
      }
      // hexagon -> 12-gon: vertices push out with the level
      const n = lv > 0.02 ? 12 : 6, base = R * 0.44 * (1 + 0.2 * lv);
      ctx.beginPath();
      for (let i = 0; i <= n; i++) {
        const ang = (i / n) * TAU - Math.PI / 2 + this.rot2 * 0.5;
        const push = n === 12 ? (i % 2 ? 0.86 + lv * 0.3 * (0.6 + 0.4 * Math.sin(i * 2.3 + t * 0.006)) : 1) : 1;
        const r = base * push;
        if (i) ctx.lineTo(Math.cos(ang) * r, Math.sin(ang) * r); else ctx.moveTo(Math.cos(ang) * r, Math.sin(ang) * r);
      }
      ctx.closePath();
      ctx.fillStyle = A(live ? 0.07 + 0.16 * lv : 0.04); ctx.fill();
      ctx.strokeStyle = A(live ? 0.95 : 0.55); ctx.lineWidth = 1.4; ctx.lineJoin = 'round'; ctx.stroke();
      // inner geometry: three hairline diagonals of the hexagon
      ctx.beginPath();
      for (let i = 0; i < 3; i++) {
        const ang = (i / 6) * TAU - Math.PI / 2 + this.rot2 * 0.5;
        ctx.moveTo(Math.cos(ang) * base, Math.sin(ang) * base); ctx.lineTo(-Math.cos(ang) * base, -Math.sin(ang) * base);
      }
      ctx.strokeStyle = A(0.14); ctx.lineWidth = 1; ctx.stroke();
      // core
      const cr = R * (0.12 + 0.08 * lv);
      if (live) {
        const g = ctx.createRadialGradient(0, 0, 0, 0, 0, cr * 3.2);
        g.addColorStop(0, A(0.55)); g.addColorStop(1, A(0));
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(0, 0, cr * 3.2, 0, TAU); ctx.fill();
      }
      ctx.fillStyle = live ? A(1) : A(0.7); ctx.beginPath(); ctx.arc(0, 0, cr, 0, TAU); ctx.fill();
      // muted / off: a slash
      if (st === 'muted' || st === 'off') {
        ctx.strokeStyle = colStr(P.muted, 0.9); ctx.lineWidth = 1.5; ctx.beginPath();
        ctx.moveTo(-R * 0.55, R * 0.55); ctx.lineTo(R * 0.55, -R * 0.55); ctx.stroke();
      }
    },

    drawMini(ctx, R, t) {
      const P = this.pal, col = this.col || P.idle, lv = this.level, st = this.state;
      const live = ANIMATED.has(st) || st === 'error';
      const A = a => colStr(col, a);
      ctx.beginPath(); ctx.arc(0, 0, R * 0.92, 0, TAU); ctx.strokeStyle = P.line; ctx.lineWidth = 1; ctx.stroke();
      if (live || st === 'standby') {
        ctx.beginPath(); ctx.arc(0, 0, R * 0.92, this.rot - Math.PI / 2, this.rot + (st === 'standby' ? 0.3 : 1.4) - Math.PI / 2);
        ctx.strokeStyle = A(0.95); ctx.lineWidth = 2; ctx.stroke();
      }
      const base = R * 0.56 * (1 + 0.18 * lv);
      ctx.beginPath();
      for (let i = 0; i <= 6; i++) {
        const ang = (i / 6) * TAU - Math.PI / 2;
        if (i) ctx.lineTo(Math.cos(ang) * base, Math.sin(ang) * base); else ctx.moveTo(Math.cos(ang) * base, Math.sin(ang) * base);
      }
      ctx.strokeStyle = A(live ? 0.9 : 0.5); ctx.lineWidth = 1.2; ctx.stroke();
      ctx.fillStyle = live ? A(1) : colStr(P.ember, 0.95);
      ctx.beginPath(); ctx.arc(0, 0, R * (0.2 + 0.1 * lv), 0, TAU); ctx.fill();
      if (st === 'muted' || st === 'off') {
        ctx.strokeStyle = colStr(P.muted, 0.9); ctx.lineWidth = 1.3; ctx.beginPath();
        ctx.moveTo(-R * 0.6, R * 0.6); ctx.lineTo(R * 0.6, -R * 0.6); ctx.stroke();
      }
      void t;
    },
  };
  motion.on(() => orb.wake());

  // ------------------------------------------------------------ number tickers
  /* tick(el, "6.1h", key): the number in the text counts from its last value under the same key to the new one
     (ease-out, 520 ms). The final text is exposed to assistive tech at once; the moving digits are aria-hidden. */
  const NUM = /-?\d[\d,]*(?:\.\d+)?/;
  const lastText = new Map();
  function tick(el, text, key) {
    text = text == null ? '' : String(text);
    const prev = key ? lastText.get(key) : undefined;
    if (key) lastText.set(key, text);
    const vis = document.createElement('span');
    vis.setAttribute('aria-hidden', 'true');
    const sr = document.createElement('span');
    sr.className = 'sr-only'; sr.textContent = text;
    el.replaceChildren(sr, vis);
    const m = NUM.exec(text), pm = prev != null ? NUM.exec(prev) : null;
    if (!m || !pm || prev === text) { vis.textContent = text; return el; }
    const parse = s => Number(s.replace(/,/g, ''));
    const from = parse(pm[0]), to = parse(m[0]);
    if (!Number.isFinite(from) || !Number.isFinite(to) || from === to) { vis.textContent = text; return el; }
    el.classList.add(to > from ? 'tick-up' : 'tick-down');
    setTimeout(() => el.classList.remove('tick-up', 'tick-down'), 650);
    if (motion.mode !== 'full') { vis.textContent = text; return el; }
    const dec = (m[0].split('.')[1] || '').length, commas = m[0].includes(',');
    const head = text.slice(0, m.index), tail = text.slice(m.index + m[0].length);
    const fmt = v => {
      const s = v.toFixed(dec);
      return commas ? Number(s).toLocaleString(undefined, { minimumFractionDigits: dec, maximumFractionDigits: dec }) : s;
    };
    const t0 = performance.now(), dur = 520;
    let shown = '';
    vis.textContent = head + fmt(from) + tail;
    loop.add(now => {
      const p = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - p, 4);
      const s = head + fmt(from + (to - from) * e) + tail;
      if (s !== shown) { vis.textContent = s; shown = s; }
      if (p >= 1) { vis.textContent = text; return false; }
      return true;
    });
    return el;
  }

  // ------------------------------------------------------------ text reveals
  const GLYPHS = '01<>/\\_|=+*#ABCDEFXZ';
  /* decode: characters cycle and lock left to right (mono text, short strings). type: a typewriter with a caret.
     Both write only visible text; callers expose the final string to screen readers separately. */
  function reveal(el, text, { mode = 'type', ms } = {}) {
    text = text == null ? '' : String(text);
    const token = (el._fx = (el._fx || 0) + 1);
    if (motion.mode !== 'full' || !text) { el.textContent = text; return; }
    const total = ms || (mode === 'decode' ? Math.min(520, 160 + text.length * 14) : Math.min(1100, 120 + text.length * 16));
    const t0 = performance.now();
    const caret = document.createElement('span');
    caret.className = 'caret';
    loop.add(now => {
      if (el._fx !== token) return false;
      const p = Math.min(1, (now - t0) / total);
      const n = Math.floor(easeOut(p) * text.length);
      if (mode === 'decode') {
        let tail = '';
        for (let i = n; i < Math.min(text.length, n + 14); i++) tail += text[i] === ' ' ? ' ' : GLYPHS[(Math.random() * GLYPHS.length) | 0];
        el.textContent = text.slice(0, n) + tail;
      } else {
        el.textContent = text.slice(0, n);
        el.append(caret);
      }
      if (p >= 1) { el.textContent = text; return false; }
      return true;
    });
  }

  // ------------------------------------------------------------ spotlight + flare
  /* One passive pointer listener; writes --mx/--my on the hovered panel only, at most once a frame. */
  function spotlight(container) {
    if (!container || !matchMedia('(pointer: fine)').matches) return;
    let lit = null, px = 0, py = 0, queued = false;
    const apply = () => {
      queued = false;
      if (!lit) return false;
      const r = lit.getBoundingClientRect();
      lit.style.setProperty('--mx', `${Math.round(px - r.left)}px`);
      lit.style.setProperty('--my', `${Math.round(py - r.top)}px`);
      return false;
    };
    container.addEventListener('pointermove', e => {
      if (motion.mode !== 'full') return;
      const p = e.target.closest?.('.panel');
      if (p !== lit) { lit?.classList.remove('lit'); lit = p; p?.classList.add('lit'); }
      px = e.clientX; py = e.clientY;
      if (lit && !queued) { queued = true; loop.add(apply); }
    }, { passive: true });
    container.addEventListener('pointerleave', () => { lit?.classList.remove('lit'); lit = null; }, { passive: true });
  }
  function flare(fromEl) {
    if (motion.mode !== 'full' || !fromEl || document.hidden) return;
    const r = fromEl.getBoundingClientRect();
    if (!r.width) return;
    const ring = document.createElement('div');
    ring.className = 'flare-ring';
    ring.style.left = `${r.left + r.width / 2}px`; ring.style.top = `${r.top + r.height / 2}px`;
    document.body.append(ring);
    setTimeout(() => ring.remove(), 600);
  }

  window.FX = { motion, loop, orb, tick, reveal, spotlight, flare, css };
})();
