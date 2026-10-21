'use strict';
/* A small QR code encoder (byte mode, versions 1-10, error correction L/M/Q/H, automatic mask), for the phone link
   in Setup. Written for Vesper after the ISO/IEC 18004 algorithm as laid out by Project Nayuki's MIT-licensed
   generator; no dependencies. QR.matrix(text, { ecl, mask }) -> rows of booleans (true = dark);
   QR.svg(text, { ecl, label }) -> an <svg> that scales crisply. Also loadable in Node (module.exports). */
(function (root) {
  const ECL = { L: 1, M: 0, Q: 3, H: 2 };  // format bits
  const ECL_I = { L: 0, M: 1, Q: 2, H: 3 };
  // per version 1..10: error-correction codewords per block, and number of blocks
  const ECC_LEN = [
    [7, 10, 15, 20, 26, 18, 20, 24, 30, 18],
    [10, 16, 26, 18, 24, 16, 18, 22, 22, 26],
    [13, 22, 18, 26, 18, 24, 18, 22, 20, 24],
    [17, 28, 22, 16, 22, 28, 26, 26, 24, 28],
  ];
  const BLOCKS = [
    [1, 1, 1, 1, 1, 2, 2, 2, 2, 4],
    [1, 1, 1, 2, 2, 4, 4, 4, 5, 5],
    [1, 1, 2, 2, 4, 4, 6, 6, 8, 8],
    [1, 1, 2, 4, 4, 4, 5, 6, 8, 8],
  ];
  const MAX_VER = 10;

  function rawModules(ver) {
    let n = (16 * ver + 128) * ver + 64;
    if (ver >= 2) {
      const a = Math.floor(ver / 7) + 2;
      n -= (25 * a - 10) * a - 55;
      if (ver >= 7) n -= 36;
    }
    return n;
  }
  function dataCodewords(ver, e) { return Math.floor(rawModules(ver) / 8) - ECC_LEN[e][ver - 1] * BLOCKS[e][ver - 1]; }
  function alignPositions(ver) {
    if (ver === 1) return [];
    const n = Math.floor(ver / 7) + 2, size = ver * 4 + 17;
    const step = Math.ceil((ver * 4 + 4) / (n * 2 - 2)) * 2;
    const out = [6];
    for (let pos = size - 7; out.length < n; pos -= step) out.splice(1, 0, pos);
    return out;
  }

  // ---- Reed-Solomon over GF(256), polynomial 0x11D
  function gfMul(x, y) {
    let z = 0;
    for (let i = 7; i >= 0; i--) { z = (z << 1) ^ ((z >>> 7) * 0x11d); z ^= ((y >>> i) & 1) * x; }
    return z & 0xff;
  }
  function rsDivisor(degree) {
    const r = new Array(degree).fill(0);
    r[degree - 1] = 1;
    let root = 1;
    for (let i = 0; i < degree; i++) {
      for (let j = 0; j < r.length; j++) { r[j] = gfMul(r[j], root); if (j + 1 < r.length) r[j] ^= r[j + 1]; }
      root = gfMul(root, 0x02);
    }
    return r;
  }
  function rsRemainder(data, div) {
    const r = new Array(div.length).fill(0);
    for (const b of data) {
      const f = b ^ r.shift();
      r.push(0);
      for (let i = 0; i < div.length; i++) r[i] ^= gfMul(div[i], f);
    }
    return r;
  }

  function utf8(text) {
    if (typeof TextEncoder !== 'undefined') return [...new TextEncoder().encode(text)];
    return [...Buffer.from(text, 'utf8')];
  }

  function encodeData(bytes, ver, e) {
    const bits = [];
    const put = (val, len) => { for (let i = len - 1; i >= 0; i--) bits.push((val >>> i) & 1); };
    put(0b0100, 4);
    put(bytes.length, ver < 10 ? 8 : 16);
    for (const b of bytes) put(b, 8);
    const cap = dataCodewords(ver, e) * 8;
    put(0, Math.min(4, cap - bits.length));
    put(0, (8 - (bits.length % 8)) % 8);
    for (let pad = 0xec; bits.length < cap; pad ^= 0xec ^ 0x11) put(pad, 8);
    const out = [];
    for (let i = 0; i < bits.length; i += 8) out.push(parseInt(bits.slice(i, i + 8).join(''), 2));
    return out;
  }

  function addEcc(data, ver, e) {
    const nBlocks = BLOCKS[e][ver - 1], eccLen = ECC_LEN[e][ver - 1];
    const raw = Math.floor(rawModules(ver) / 8);
    const nShort = nBlocks - (raw % nBlocks), shortLen = Math.floor(raw / nBlocks);
    const div = rsDivisor(eccLen);
    const blocks = [];
    for (let i = 0, k = 0; i < nBlocks; i++) {
      const dat = data.slice(k, k + shortLen - eccLen + (i < nShort ? 0 : 1));
      k += dat.length;
      const ecc = rsRemainder(dat, div);
      if (i < nShort) dat.push(0);
      blocks.push(dat.concat(ecc));
    }
    const out = [];
    for (let i = 0; i < blocks[0].length; i++) {
      for (let j = 0; j < blocks.length; j++) if (i !== shortLen - eccLen || j >= nShort) out.push(blocks[j][i]);
    }
    return out;
  }

  function build(ver, e, codewords, mask) {
    const size = ver * 4 + 17;
    const mod = Array.from({ length: size }, () => new Array(size).fill(false));
    const fn = Array.from({ length: size }, () => new Array(size).fill(false));
    const set = (x, y, dark) => { mod[y][x] = dark; fn[y][x] = true; };
    for (let i = 0; i < size; i++) { set(6, i, i % 2 === 0); set(i, 6, i % 2 === 0); }
    for (const [cx, cy] of [[3, 3], [size - 4, 3], [3, size - 4]]) {
      for (let dy = -4; dy <= 4; dy++) {
        for (let dx = -4; dx <= 4; dx++) {
          const x = cx + dx, y = cy + dy, d = Math.max(Math.abs(dx), Math.abs(dy));
          if (x >= 0 && x < size && y >= 0 && y < size) set(x, y, d !== 2 && d !== 4);
        }
      }
    }
    const al = alignPositions(ver), last = al.length - 1;
    for (let i = 0; i < al.length; i++) {
      for (let j = 0; j < al.length; j++) {
        if ((i === 0 && j === 0) || (i === 0 && j === last) || (i === last && j === 0)) continue;
        for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) set(al[i] + dx, al[j] + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
      }
    }
    drawFormat(set, size, e, 0);  // reserve; redrawn with the real mask
    if (ver >= 7) {
      let rem = ver;
      for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
      const bits = (ver << 12) | rem;
      for (let i = 0; i < 18; i++) {
        const bit = ((bits >>> i) & 1) !== 0, a = size - 11 + (i % 3), b = Math.floor(i / 3);
        set(a, b, bit); set(b, a, bit);
      }
    }
    // codewords, in the zigzag
    let i = 0;
    for (let right = size - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      for (let v = 0; v < size; v++) {
        for (let j = 0; j < 2; j++) {
          const x = right - j, up = ((right + 1) & 2) === 0, y = up ? size - 1 - v : v;
          if (!fn[y][x] && i < codewords.length * 8) { mod[y][x] = ((codewords[i >>> 3] >>> (7 - (i & 7))) & 1) !== 0; i++; }
        }
      }
    }
    const masked = m => {
      const out = mod.map(r => r.slice());
      for (let y = 0; y < size; y++) {
        for (let x = 0; x < size; x++) {
          if (fn[y][x]) continue;
          let inv;
          switch (m) {
            case 0: inv = (x + y) % 2 === 0; break;
            case 1: inv = y % 2 === 0; break;
            case 2: inv = x % 3 === 0; break;
            case 3: inv = (x + y) % 3 === 0; break;
            case 4: inv = (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0; break;
            case 5: inv = (x * y) % 2 + (x * y) % 3 === 0; break;
            case 6: inv = ((x * y) % 2 + (x * y) % 3) % 2 === 0; break;
            default: inv = ((x + y) % 2 + (x * y) % 3) % 2 === 0;
          }
          if (inv) out[y][x] = !out[y][x];
        }
      }
      drawFormat((x, y, d) => { out[y][x] = d; }, size, e, m);
      return out;
    };
    if (mask != null) return masked(mask);
    let best = null, bestScore = Infinity;
    for (let m = 0; m < 8; m++) {
      const cand = masked(m), score = penalty(cand);
      if (score < bestScore) { best = cand; bestScore = score; }
    }
    return best;
  }

  function drawFormat(set, size, e, mask) {
    const data = (ECL[e] << 3) | mask;
    let rem = data;
    for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    const bits = ((data << 10) | rem) ^ 0x5412;
    const bit = i => ((bits >>> i) & 1) !== 0;
    for (let i = 0; i <= 5; i++) set(8, i, bit(i));
    set(8, 7, bit(6)); set(8, 8, bit(7)); set(7, 8, bit(8));
    for (let i = 9; i < 15; i++) set(14 - i, 8, bit(i));
    for (let i = 0; i < 8; i++) set(size - 1 - i, 8, bit(i));
    for (let i = 8; i < 15; i++) set(8, size - 15 + i, bit(i));
    set(8, size - 8, true);
  }

  /* The four penalty rules of the standard: runs, 2x2 blocks, finder look-alikes, and the dark/light balance. */
  function penalty(m) {
    const n = m.length;
    let score = 0, dark = 0;
    const lines = [];
    for (let y = 0; y < n; y++) { lines.push(m[y]); lines.push(m.map(r => r[y])); }
    const p1 = [true, false, true, true, true, false, true, false, false, false, false];
    const p2 = [false, false, false, false, true, false, true, true, true, false, true];
    for (const line of lines) {
      let run = 1;
      for (let i = 1; i <= n; i++) {
        if (i < n && line[i] === line[i - 1]) run++;
        else { if (run >= 5) score += 3 + (run - 5); run = 1; }
      }
      for (let i = 0; i + 11 <= n; i++) {
        let a = true, b = true;
        for (let k = 0; k < 11; k++) { if (line[i + k] !== p1[k]) a = false; if (line[i + k] !== p2[k]) b = false; }
        if (a) score += 40;
        if (b) score += 40;
      }
    }
    for (let y = 0; y < n; y++) {
      for (let x = 0; x < n; x++) {
        if (m[y][x]) dark++;
        if (x < n - 1 && y < n - 1) {
          const c = m[y][x];
          if (c === m[y][x + 1] && c === m[y + 1][x] && c === m[y + 1][x + 1]) score += 3;
        }
      }
    }
    const total = n * n;
    score += Math.max(0, Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1) * 10;
    return score;
  }

  function matrix(text, { ecl = 'M', mask = null } = {}) {
    const e = ECL_I[ecl] ?? 1;
    const bytes = utf8(String(text));
    for (let ver = 1; ver <= MAX_VER; ver++) {
      const lenBits = ver < 10 ? 8 : 16;
      if (4 + lenBits + bytes.length * 8 <= dataCodewords(ver, e) * 8) {
        return build(ver, ecl in ECL ? ecl : 'M', addEcc(encodeData(bytes, ver, e), ver, e), mask);
      }
    }
    throw new Error('Text too long for a small QR code');
  }

  /* One <path> of dark squares with a 4-module quiet zone: sharp at any size, themable with currentColor. */
  function svg(text, { ecl = 'M', label = 'QR code' } = {}) {
    const m = matrix(text, { ecl }), n = m.length, q = 4;
    let d = '';
    for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) if (m[y][x]) d += `M${x + q} ${y + q}h1v1h-1z`;
    const NS = 'http://www.w3.org/2000/svg';
    const el = document.createElementNS(NS, 'svg');
    el.setAttribute('viewBox', `0 0 ${n + q * 2} ${n + q * 2}`);
    el.setAttribute('role', 'img');
    el.setAttribute('aria-label', label);
    el.setAttribute('shape-rendering', 'crispEdges');
    const bg = document.createElementNS(NS, 'rect');
    bg.setAttribute('width', n + q * 2); bg.setAttribute('height', n + q * 2); bg.setAttribute('class', 'qr-bg');
    const p = document.createElementNS(NS, 'path');
    p.setAttribute('d', d); p.setAttribute('class', 'qr-fg');
    el.append(bg, p);
    return el;
  }

  const QR = { matrix, svg };
  if (typeof module !== 'undefined' && module.exports) module.exports = QR;
  else root.QR = QR;
})(typeof window !== 'undefined' ? window : globalThis);
