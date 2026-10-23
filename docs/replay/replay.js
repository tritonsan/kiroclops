/* KiroClops session replay.
 *
 * Replays one real, logged agent session (V6 web-panel run v6p01, 16 October 2026) from
 * data.js, which build_data.py generates from the frozen evidence. Plain JavaScript, no
 * dependencies, no network requests. Two modes share this code:
 *
 *  - interactive (index.html): select a step on the timeline, read its detail, play the story;
 *  - render (render.html, or index.html?render=1): the page is laid out for exactly
 *    1920 x 1080 and window.render(ms) shows the state at ms of a 45-second animation,
 *    deterministically (the DOM is a function of ms alone, whatever was shown before; no
 *    timers, no CSS transitions), for submission/video/tools/render_html.py. window.__ready is a promise that resolves when
 *    the page is built, as that renderer expects.
 */
(function () {
  'use strict';

  const D = window.REPLAY_DATA;
  const root = document.documentElement;
  const RENDER = root.classList.contains('render') || window.__KC_RENDER === true || /[?&]render=1(?:&|$)/.test(location.search);
  root.classList.toggle('render', RENDER);
  const REDUCED = !RENDER && !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const app = document.getElementById('app');
  if (!D || !app) { if (app) app.textContent = 'data.js is missing.'; return; }

  /* ---------------------------------------------------------------- helpers */
  const MINUS = '\u2212', ENDASH = '\u2013', TIMES = '\u00d7', MID = '\u00b7', RARR = '\u2192';
  const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
  const ease = (x) => { x = clamp(x); return x * x * (3 - 2 * x); };
  const easeOut = (x) => 1 - Math.pow(1 - clamp(x), 3);
  const lerp = (a, b, t) => a + (b - a) * t;
  const f4 = (v) => v.toFixed(4);
  const f2 = (v) => v.toFixed(2);
  const int = (v) => Math.round(v).toLocaleString('en-US');
  const clock = (s) => Math.floor(s / 60) + ':' + String(Math.floor(s % 60)).padStart(2, '0');

  function el(tag, props, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? '' : v);
    }
    append(node, kids);
    return node;
  }
  function append(node, kids) {
    for (const kid of kids.flat(Infinity)) {
      if (kid === undefined || kid === null || kid === false) continue;
      node.append(typeof kid === 'object' ? kid : document.createTextNode(String(kid)));
    }
    return node;
  }
  const NS = 'http://www.w3.org/2000/svg';
  function sv(tag, props, ...kids) {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(props || {})) if (v !== undefined && v !== null) node.setAttribute(k, v);
    append(node, kids);
    return node;
  }
  /* `code` and **bold** inside a string become elements; everything else stays text. */
  function rich(text) {
    const frag = document.createDocumentFragment();
    String(text).split(/(`[^`]+`|\*\*[^*]+\*\*)/).forEach((part) => {
      if (!part) return;
      if (part[0] === '`') frag.append(el('code', { text: part.slice(1, -1) }));
      else if (part.startsWith('**')) frag.append(el('strong', { text: part.slice(2, -2) }));
      else frag.append(document.createTextNode(part));
    });
    return frag;
  }

  /* Staged reveal: every panel registers its parts and apply(p) shows them for p in [0, 1]. */
  class Reveal {
    constructor() { this.items = []; this.bars = []; this.counts = []; this.hooks = []; this.headExtra = null; }
    add(node, at = 0, dur = 0.16) { this.items.push({ node, at, dur }); node.classList.add('rv'); return node; }
    bar(node, from, to, at, dur = 0.2) { this.bars.push({ node, from, to, at, dur }); }
    count(node, to, fmt, at, dur = 0.2) { this.counts.push({ node, to, fmt, at, dur }); }
    hook(fn) { this.hooks.push(fn); }
    apply(p) {
      for (const it of this.items) {
        const a = ease((p - it.at) / it.dur);
        it.node.style.opacity = a >= 1 ? '1' : a.toFixed(3);
        it.node.style.transform = a >= 1 ? 'none' : 'translateY(' + ((1 - a) * 12).toFixed(2) + 'px)';
      }
      for (const b of this.bars) b.node.style.width = (lerp(b.from, b.to, ease((p - b.at) / b.dur)) * 100).toFixed(2) + '%';
      for (const c of this.counts) c.node.textContent = c.fmt(lerp(0, c.to, ease((p - c.at) / c.dur)));
      for (const h of this.hooks) h(p);
    }
  }

  /* small syntax highlighter for the excerpts (Python and JavaScript) */
  const HL = {
    py: /(#.*$)|("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|(\b\d+(?:\.\d+)?\b)|(\b(?:import|from|for|in|if|else|return|def|not|None|True|False|and|or|is|continue|break|print)\b)/g,
    js: /(\/\/.*$)|("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|(\b\d+(?:\.\d+)?\b)|(\b(?:const|let|function|export|import|from|if|else|for|of|return|null|true|false|NaN|Infinity)\b)/g,
  };
  function highlight(line, lang) {
    const re = new RegExp(HL[lang].source, 'g');
    const frag = document.createDocumentFragment();
    let last = 0, m;
    while ((m = re.exec(line)) !== null) {
      if (m[0] === '') { re.lastIndex++; continue; }
      if (m.index > last) frag.append(document.createTextNode(line.slice(last, m.index)));
      const cls = m[1] ? 'c' : m[2] ? 's' : m[3] ? 'n' : 'k';
      frag.append(el('span', { class: cls, text: m[0] }));
      last = m.index + m[0].length;
    }
    if (last < line.length) frag.append(document.createTextNode(line.slice(last)));
    return frag;
  }
  /* mode: 'plain' (numbered lines), 'add' (added lines, new-file numbers) or 'diff' (+/- marks) */
  function codeBlock(spec) {
    const lines = spec.text.split('\n');
    const body = el('div', { class: 'cb', role: 'region', tabindex: '0', 'aria-label': 'Code excerpt: ' + spec.title });
    lines.forEach((line, k) => {
      let cls = 'cl', gutter = String(spec.first + k), code = line;
      if (spec.mode === 'add') cls += ' add';
      if (spec.mode === 'diff') {
        const sign = line[0];
        cls += sign === '+' ? ' add' : sign === '-' ? ' del' : '';
        gutter = sign === ' ' ? '' : sign;
        code = line.slice(1);
      }
      body.append(el('div', { class: cls }, el('span', { class: 'ln', text: gutter }), el('span', { class: 'tx' }, highlight(code, spec.lang))));
    });
    return el('div', { class: 'codeblock' }, el('div', { class: 'cb-h' }, el('b', { text: spec.title }), el('span', { text: spec.where })), body);
  }

  /* ---------------------------------------------------------------- data views */
  const TOOLS = D.tools.map((t) => t.id);
  const COUNT = Object.fromEntries(D.tools.map((t) => [t.id, t.count]));
  const S = D.steps.map((s) => Object.assign({}, s, { start: s.t0, end: s.t1 == null ? s.t0 : s.t1 }));
  const J = {};
  D.jobs.forEach((j) => { J[j.op + ':' + (j.candidate || '')] = j; });
  const jPrepare = J['prepare:'], jC01 = J['evaluate:candidate-01'], jProbe = J['evaluate:probe-01'], jC02 = J['evaluate:candidate-02'], jFinal = J['finalize:candidate-02'];
  const SC = D.scores, HO = D.heldout, LAG = D.lag;
  const TMAX = 3000;
  const MCP = new Set(['get_job', 'inspect_session']);
  const KEYTOOL = new Set(['prepare_experiment', 'evaluate_candidate', 'cancel_job', 'finalize_candidate']);
  const ticks = D.timeline.map((r) => ({ t: r[0], tool: TOOLS[r[1]], detail: r[2] }));
  const whenOf = (s) => (s.t1 != null ? Math.round(s.t0) + ENDASH + Math.round(s.t1) : Math.round(s.t0)) + ' s';
  const callsText = (s) => {
    const n = Object.values(s.calls).reduce((a, b) => a + b, 0);
    return n + ' tool calls: ' + Object.entries(s.calls).sort((a, b) => b[1] - a[1]).map(([k, v]) => k + ' ' + v).join(', ');
  };

  const COPY = [
    { chip: 'Prepare', head: 'Prepare: the reference and the unchanged app, measured' },
    { chip: 'Candidate 01', head: 'Candidate 01: inside the limits, but close to them' },
    { chip: 'Reads the result', head: 'Kiro reads the result with scripts of its own' },
    { chip: 'Decides, codes', head: 'Decision to code: motion anchored to the input instant' },
    { chip: 'Probe', head: 'Probe: three cases, one per recipe family' },
    { chip: 'Cancel, last fix', head: 'Cancel the probe and apply a last fix' },
    { chip: 'Candidate 02', head: 'Candidate 02: the same four windows, measured again' },
    { chip: 'Seal, held-out', head: 'Sealed, then tested on 56 cases Kiro never saw' },
  ];
  function metaOf(i) {
    const s = S[i];
    const tool = (name) => el('code', { text: name });
    switch (i) {
      case 0: return [tool('prepare_experiment'), ' ' + MID + ' t = ' + whenOf(s) + ' ' + MID + ' job ran ' + int(jPrepare.dur) + ' s'];
      case 1: return [tool('evaluate_candidate'), ' ' + MID + ' t = ' + whenOf(s) + ' ' + MID + ' job ran ' + int(jC01.dur) + ' s'];
      case 2: case 3: return ['t = ' + whenOf(s) + ' ' + MID + ' ' + callsText(s)];
      case 4: return [tool('evaluate_candidate'), ' with ', tool('case_ids'), ' ' + MID + ' t = ' + whenOf(s) + ' ' + MID + ' partial probe'];
      case 5: return [tool('cancel_job'), ' ' + MID + ' t = ' + whenOf(s) + ' ' + MID + ' probe ran ' + int(jProbe.dur) + ' s'];
      case 6: return [tool('evaluate_candidate'), ' ' + MID + ' t = ' + whenOf(s) + ' ' + MID + ' job ran ' + int(jC02.dur) + ' s'];
      default: return [tool('finalize_candidate'), ' ' + MID + ' t = ' + whenOf(s) + ' ' + MID + ' job ran ' + int(jFinal.dur) + ' s'];
    }
  }

  /* ---------------------------------------------------------------- panels */
  const tile = (big, label, cls) => el('div', { class: 'tile' }, el('div', { class: 'big num ' + (cls || '') }, big), el('div', { class: 'lab' }, label));

  function windowsCard(rv, both) {
    const card = el('div', { class: 'sub-card wcard' });
    card.append(
      el('h4', { class: 'ct' }, both ? 'The same windows: candidate 01 ' + RARR + ' candidate 02' : 'The four worst windows of candidate 01'),
      el('p', { class: 'cs' }, 'error / limit. The bar is the share of the limit; the white mark is the limit.'));
    D.windows.forEach((w, k) => {
      const a = both ? w.c02 : w.c01;
      const track = el('div', { class: 'track' });
      if (both) { const ghost = el('i', { class: 'ghost' }); ghost.style.width = (w.c01.ratio * 100) + '%'; track.append(ghost); }
      const fill = el('i', { class: 'fill ' + (both || a.ratio < 0.7 ? 'ok' : 'warn') });
      track.append(fill, el('b', { class: 'limit' }));
      const row = el('div', { class: 'wrow' },
        el('div', {}, el('span', { class: 'wcase', text: w.case }), el('span', { class: 'wmeta', text: w.object + ' ' + MID + ' ' + w.window })),
        el('div', { class: 'wbarc' }, track),
        el('div', { class: 'wnum' },
          el('span', { class: 'r' }, both ? [el('span', { class: 'was', text: f2(w.c01.ratio) }), f2(a.ratio)] : f2(a.ratio)),
          f4(a.error) + ' / ' + f4(a.limit)));
      rv.add(row, 0.12 + k * 0.08, 0.14);
      rv.bar(fill, both ? w.c01.ratio : 0, a.ratio, 0.2 + k * 0.08, both ? 0.3 : 0.22);
      card.append(row);
    });
    return card;
  }

  function lagTile(rv, both) {
    const L1 = LAG.c01, L2 = LAG.c02;
    if (!L1 || !L2) return null;
    const hist = el('div', { class: 'hist' });
    ['-10', '0', '10'].forEach((bin) => {
      const bars = el('div', { class: 'hbars' });
      const mk = (count, cls) => {
        const b = el('div', { class: 'hbar ' + cls }, el('b', { text: String(count || 0) }));
        bars.append(b);
        return b;
      };
      const grow = (node, count, at) => rv.hook((p) => { node.style.height = (count / L1.n * 100 * ease((p - at) / 0.3)).toFixed(2) + '%'; });
      const b1 = mk(L1[bin] || 0, both ? 'ghost' : 'c01');
      grow(b1, L1[bin] || 0, both ? 0.1 : 0.3);
      if (both) grow(mk(L2[bin] || 0, 'c02'), L2[bin] || 0, 0.45);
      hist.append(el('div', { class: 'hcol' }, bars, el('div', { class: 'hlab', text: (bin === '0' ? '0' : bin === '10' ? '+10' : MINUS + '10') + ' ms' })));
    });
    const cap = both
      ? el('p', { class: 'hcap' }, el('b', { text: '0 ms' }), ' on ' + L2['0'] + ' of ' + L2.n + ' windows. Candidate 01 (outline): ' + MINUS + '10 ms on ' + L1['-10'] + '.')
      : el('p', { class: 'hcap' }, el('b', { text: MINUS + '10 ms' }), ' on ' + L1['-10'] + ' of ' + L1.n + ' windows: the candidate\u2019s motion is measured about 10 ms off the reference.');
    return el('div', { class: 'tile' }, el('div', { class: 'ct' }, 'Measured lag per window'), el('p', { class: 'cs' }, 'number of windows by lag_s'), hist, cap);
  }

  const PANELS = [];

  /* 1 - prepare */
  PANELS.push((rv) => {
    const lead = rv.add(el('p', { class: 'lead' }, rich('Kiro calls `prepare_experiment`. KiroClops records the reference clip and the unchanged app and measures both for ' + SC.calibration.cases + ' calibration cases (' + int(jPrepare.dur) + ' s).')), 0.02);
    const flow = el('ol', { class: 'flow' },
      el('li', {}, el('b', {}, el('em', { text: '1' }), 'Record'), el('span', { text: 'The reference clip and the unchanged app, for each calibration case.' })),
      el('li', {}, el('b', {}, el('em', { text: '2' }), 'Measure'), el('span', { text: 'OpenCV 5 tracks the objects and events in every recording, frame by frame.' })),
      el('li', {}, el('b', {}, el('em', { text: '3' }), 'Meanwhile'), el('span', { text: 'Kiro reads the app\u2019s source (' + D.meta.prepare_reads + ' files) and waits on the job with get_job.' })));
    [...flow.children].forEach((li, k) => rv.add(li, 0.16 + k * 0.17, 0.2));
    const tiles = el('div', { class: 'tiles' },
      rv.add(tile(String(SC.calibration.cases), 'calibration cases, each with a reference clip', ''), 0.66),
      rv.add(tile(int(jPrepare.dur) + ' s', 'measurement job for the reference and the unchanged app', ''), 0.74),
      rv.add(tile(f4(SC.calibration.unchanged), 'calibration error of the unchanged app (post-hoc recomputation)', 'slate'), 0.82));
    return [lead, flow, tiles];
  });

  /* 2 - candidate 01 (feedback) */
  PANELS.push((rv) => {
    const lead = rv.add(el('p', { class: 'lead' }, rich('First edit of `src/panel-motion.js` (+3/' + MINUS + '1 lines), re-recorded and measured: eligible, calibration error **' + f4(SC.calibration.c01) + '**. All ' + D.meta.windows + ' windows are inside their limits, but several are close to them.')), 0.02);
    const left = windowsCard(rv, false);
    const side = el('div', { class: 'stack' });
    side.append(
      rv.add(tile(f4(SC.calibration.c01), 'calibration error, mean of ' + SC.calibration.cases + ' cases (unchanged app: ' + f4(SC.calibration.unchanged) + ', post-hoc)', 'amber'), 0.4),
      rv.add(lagTile(rv, false) || el('div'), 0.55));
    const cols = el('div', { class: 'cols' }, left, side);
    const out = [lead, cols];
    if (!RENDER) {
      const d = D.diffs['candidate-01'];
      out.push(el('div', { class: 'note' }, 'The edit itself, from the diff:'),
        codeBlock({ title: 'src/panel-motion.js', where: 'candidate 01, +3 ' + MINUS + '1 lines', first: 1, text: d.text, mode: 'diff', lang: 'js' }));
    }
    return out;
  });

  /* 3 - the agent reads the result with its own scripts */
  PANELS.push((rv) => {
    const lead = rv.add(el('p', { class: 'lead' }, 'Next, Kiro examines candidate 01\u2019s OpenCV 5 result with three scripts of its own: how close each window is to its limit, and the measured timing offset.'), 0.02);
    const captions = [
      ['windows.py', 'prints every window\u2019s error, limit and measured lag from the exported feedback.'],
      ['track.py', 'tracks the bookmark and star glyphs in candidate 01\u2019s own exported frames.'],
      ['latfit.py', 'fits the per-input latency of its motion model to those tracks.'],
    ];
    rv.headExtra = el('div', { class: 'snav', 'aria-hidden': 'true' }, captions.map((c) => el('span', { text: c[0] })));
    const nav = rv.headExtra;
    const blocks = captions.map(([name, text]) => {
      const c = D.code[name];
      return el('figure', { class: 'script' },
        el('figcaption', {}, el('b', { text: name }), ' ' + text),
        codeBlock({ title: name, where: 'agent-scratch, lines ' + c.first + ENDASH + c.last, first: c.first, text: c.text, mode: 'plain', lang: 'py' }));
    });
    blocks.forEach((b, k) => rv.add(b, 0.1 + k * 0.08, 0.2));
    rv.hook((p) => {
      if (!RENDER) return;
      const idx = Math.min(2, Math.floor(clamp(p / 0.97) * 3));
      blocks.forEach((b, k) => {
        const local = clamp(p * 3 - k);
        b.style.display = k === idx ? '' : 'none';
        b.style.opacity = k === idx ? ease(local / 0.12).toFixed(3) : '';
        b.style.transform = '';
      });
      [...nav.children].forEach((n, k) => n.setAttribute('class', k === idx ? 'on' : 'off'));
    });
    const sc = D.scripts;
    const when = (v) => Math.round(v) + ' s';
    const note = RENDER ? null : el('p', { class: 'note' },
      el('b', { text: 'From the trace. ' }),
      el('code', { text: 'windows.py' }), ' was written at ' + when(sc['windows.py'].written) + '. ',
      el('code', { text: 'track.py' }), ' and ', el('code', { text: 'latfit.py' }), ' were first written at ' + when(sc['track.py'].written) + ' and ' + when(sc['latfit.py'].written) +
      ' and run on the baseline recordings while candidate 01 was still being measured; at ' + when(sc['track.py'].on_c01) + ' and ' + when(sc['latfit.py'].on_c01) + ' they run on candidate 01\u2019s own frames and result.');
    return [lead, blocks, note];
  });

  /* 4 - decision to code */
  PANELS.push((rv) => {
    const lead = rv.add(el('p', { class: 'lead' }, rich('Kiro extends `src/motion/spring.js`, adds `src/motion/input-clock.js`, rewrites `src/panel-motion.js` (+86/' + MINUS + '15), builds a local node test and iterates twice.')), 0.02);
    const st = D.diff_stats;
    const maxLines = Math.max(...st['candidate-02'].map((f) => f.added + f.removed));
    const group = (title, sub, files, at) => {
      const sum = files.reduce((a, f) => [a[0] + f.added, a[1] + f.removed], [0, 0]);
      const list = el('div', { class: 'dfiles' }, files.map((f) => el('div', { class: 'dfile-item' },
        el('div', { class: 'dline' }, el('span', { class: 'dfile', title: f.file }, RENDER ? f.file.split('/').pop() : f.file.split('/').flatMap((part, k, all) => (k < all.length - 1 ? [part + '/', document.createElement('wbr')] : [part]))), el('span', { class: 'dnum' }, el('span', { class: 'a', text: '+' + f.added }), ' ', el('span', { class: 'd', text: MINUS + f.removed }))),
        el('div', { class: 'dbar' }, el('i', { class: 'a', style: 'width:' + (f.added / maxLines * 100) + '%' }), el('i', { class: 'd', style: 'width:' + (f.removed / maxLines * 100) + '%' })))));
      return rv.add(el('div', { class: 'dgroup' }, el('h5', {}, title, el('span', { text: sub + ', +' + sum[0] + ' ' + MINUS + sum[1] })), list), at, 0.2);
    };
    const stats = el('div', { class: 'dstat sub-card' },
      group('Candidate 01', '1 file', st['candidate-01'], 0.12),
      group('Candidate 02', '3 files', st['candidate-02'], 0.24));
    const ic = D.diffs['input-clock.js'], pm = D.diffs['panel-motion.js'];
    const code = el('div', { class: 'codes' },
      rv.add(codeBlock({ title: 'src/motion/input-clock.js', where: 'new file, lines ' + ic.first + ENDASH + ic.last + ': the input clock and why it exists', first: ic.first, text: ic.text, mode: 'add', lang: 'js' }), 0.34, 0.25),
      RENDER ? null : el('div', {}, el('p', { class: 'note' }, rich('How the rewritten motion law uses it: a new mode starts ' + D.meta.response_lead_ms + ' ms (`RESPONSE_LEAD`) before the instant of the input, not at the frame boundary before it.')),
        codeBlock({ title: 'src/panel-motion.js', where: 'rewritten, lines ' + pm.first + ENDASH + pm.last, first: pm.first, text: pm.text, mode: 'add', lang: 'js' }),
        el('p', { class: 'note' }, el('b', { text: 'From the trace. ' }), 'A draft of ', el('code', { text: 'input-clock.js' }), ' is kept in ', el('code', { text: 'agent-scratch/' }), '. The file was written at ' + Math.round(D.scripts['input-clock.js'].draft) + ' s, rewritten at ' + Math.round(D.scripts['input-clock.js'].rewritten) + ' s and edited again before the probe; the code above is the version in candidate 02\u2019s diff.')));
    return [lead, stats, code];
  });

  /* 5 - probe */
  PANELS.push((rv) => {
    const lead = rv.add(el('p', { class: 'lead' }, 'A partial probe on three cases, one from each recipe family added in V4 and V5. It re-records only those cases.'), 0.02);
    const total = SC.calibration.cases, probed = D.probe.cases.length;
    const meter = rv.add(el('div', { class: 'meter' },
      el('div', { class: 'cells', 'aria-hidden': 'true' }, Array.from({ length: total }, (_, k) => el('i', { class: k < probed ? 'on' : '' }))),
      el('span', { text: probed + ' of ' + total + ' calibration cases re-recorded' })), 0.08, 0.12);
    const cases = el('div', { class: 'cases' }, D.probe.cases.map((c, k) => {
      const hot = c.family === 'reverse-wide';
      const card = el('div', { class: 'case' + (hot ? ' hot' : '') },
        el('h4', { text: c.family }),
        el('div', { class: 'meta', text: 'viewport ' + c.width + ' px ' + MID + ' ' + c.repeat + ' ' + MID + ' recipe ' + c.recipe.toUpperCase() }),
        el('p', { text: hot ? 'Held candidate 01\u2019s worst window, 0.85 of its limit.' : 'Not among the five worst windows of candidate 01.' }));
      return rv.add(card, 0.16 + k * 0.11, 0.14);
    }));
    const note = rv.add(el('p', { class: 'note' }, rich('A partial probe never counts as a candidate and cannot be finalized. It is a spot check across the three new recipe families, not a ranking of the worst cases: only one of the three probed cases is among the five worst windows.')), 0.5, 0.14);
    return [lead, meter, cases, note];
  });

  /* 6 - cancel and last fix */
  PANELS.push((rv) => {
    const lead = rv.add(el('p', { class: 'lead' }, rich('Kiro cancels the probe after ' + int(jProbe.dur) + ' s to apply a last fix: one more `str_replace` in `src/panel-motion.js`. It re-runs its local node test and calls `evaluate_candidate` again.')), 0.02);
    const tCancel = Math.round(S[5].start) + ' s', tNext = Math.round(S[6].start) + ' s', gap = Math.round(S[6].start - S[5].start);
    const steps = [
      [tCancel, '', 'str_replace', 'One last edit of src/panel-motion.js.'],
      [tCancel, 'bad', 'cancel_job', 'The probe ends as job_cancelled. It was a partial run, so it does not count as a candidate.'],
      [tCancel, '', 'node test-lead.mjs', 'The agent\u2019s own local test, run once more before measuring.'],
      [tNext, 'good', 'evaluate_candidate', 'Candidate 02: a full calibration of all ' + SC.calibration.cases + ' cases, ' + gap + ' seconds after the cancel.'],
    ];
    const seq = el('ol', { class: 'seq' }, steps.map((s, k) => rv.add(el('li', { class: s[1] },
      el('span', { class: 'tm', text: s[0] }), el('span', { class: 'dt' }), el('span', { class: 'tx' }, el('b', {}, el('code', { text: s[2] })), el('span', { text: s[3] }))), 0.1 + k * 0.13, 0.16)));
    return [lead, seq];
  });

  /* 7 - candidate 02 (result) */
  PANELS.push((rv) => {
    const r = (jC01.score / jC02.score).toFixed(1);
    const lagNote = LAG.c02 ? ', and the measured lag is 0 on ' + LAG.c02['0'] + ' of ' + LAG.c02.n + ' windows' : '';
    const lead = rv.add(el('p', { class: 'lead' }, rich('Full calibration: eligible, error **' + f4(SC.calibration.c02) + '** (' + r + TIMES + ' lower than candidate 01). The worst window drops from 0.85 to 0.43 of its limit' + lagNote + '.')), 0.02);
    const left = windowsCard(rv, true);
    const side = el('div', { class: 'stack' });
    side.append(
      rv.add(tile(f4(SC.calibration.c02), 'calibration error (candidate 01: ' + f4(SC.calibration.c01) + ', unchanged app: ' + f4(SC.calibration.unchanged) + ', post-hoc)', 'green'), 0.4),
      rv.add(lagTile(rv, true) || el('div'), 0.55));
    return [lead, el('div', { class: 'cols' }, left, side)];
  });

  /* 8 - finalize and held-out */
  const drag = { svg: null, draw: null };
  PANELS.push((rv) => {
    const f = HO.failed;
    const lead = RENDER ? null : rv.add(el('p', { class: 'lead' }, rich('`finalize_candidate` seals candidate 02 and opens the held-out cases it never saw (' + int(jFinal.dur) + ' s of measurement).')), 0.02);
    const big = el('span', { class: 'big' }, '0', el('small', { text: ' / ' + HO.cases }));
    rv.count(big.firstChild, HO.passed, (v) => String(Math.round(v)), 0.06, 0.12);
    const verdict = rv.add(el('div', { class: 'verdict' }, big, el('span', { class: 'lab', text: RENDER ? 'held-out comparisons passed (cases Kiro never saw)' : 'held-out comparisons passed' })), 0.05);
    const roles = rv.add(el('div', { class: 'roles' },
      el('span', { class: 'pill' }, 'validation ', el('b', { text: HO.roles.validation[0] + '/' + HO.roles.validation[1] })),
      el('span', { class: 'pill' }, 'regression ', el('b', { text: HO.roles.regression[0] + '/' + HO.roles.regression[1] })),
      el('span', { class: 'pill bad' }, '1 failed: ', el('b', { text: f.family + ', ' + f.width + ' px' }))), 0.18);
    const fail = el('div', { class: 'failcard' }, el('h4', { class: 'ct' }, 'Position error after the release, against its limit'));
    f.windows.forEach((w, k) => {
      const track = el('div', { class: 'track' }, el('i', { class: 'fill rose' }), el('b', { class: 'limit', style: 'right:auto;left:' + (w.limit * 100) + '%' }));
      const row = el('div', { class: 'frow' }, el('span', { class: 'nm', text: w.object }), track,
        el('span', { class: 'vl' }, el('b', { text: f2(w.error) }), ' vs ' + (w.limit === 0.05 ? '0.05' : w.limit.toFixed(3)) + ' (' + w.before.toFixed(3) + ' before)'));
      rv.add(row, 0.34 + k * 0.05, 0.12);
      rv.bar(track.firstChild, 0, w.error, 0.38 + k * 0.05, 0.2);
      fail.append(row);
    });
    rv.add(fail, 0.3, 0.14);
    const left = el('div', {}, verdict, roles, fail);

    const dragBox = el('div', { class: 'drag sub-card' },
      el('h4', { class: 'ct' }, RENDER ? 'Handle after the drag release, 1440 px' : 'Handle position after the drag is released (1440 px viewport)'),
      RENDER ? null : el('p', { class: 'cs' }, 'OpenCV 5 track of the panel handle: after the release Kiro\u2019s panel springs back toward where the drag started.'));
    const svg = sv('svg', { role: 'img', 'aria-label': 'Handle x position against time after the drag release: the reference and the unchanged app move to the left edge, Kiro\u2019s sealed candidate moves back to the right.' });
    dragBox.append(svg);
    drag.svg = svg;
    rv.add(dragBox, 0.28, 0.14);
    rv.hook((p) => { if (drag.draw) drag.draw(clamp((p - 0.4) / 0.28)); });
    const banner = rv.add(el('div', { class: 'banner' }, rich('The run is recorded as **failed** under the pre-registered rule and the panel domain is not accepted. The calibration set has no drag-release case; the held-out set exists to catch exactly this, and it did.')), 0.76, 0.14);
    return [lead, el('div', { class: 'cols' }, left, dragBox), banner];
  });

  function drawDrag(progress) {
    const svg = drag.svg;
    const W = Math.max(260, Math.round(svg.parentNode.clientWidth - 32));
    const H = RENDER ? 262 : (W < 420 ? 200 : 230);
    const R = RENDER ? { l: 70, r: 20, t: 12, b: 36 } : { l: 52, r: 14, t: 10, b: 30 };
    svg.setAttribute('width', W); svg.setAttribute('height', H); svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    const dr = HO.drag, T0 = -0.7, T1 = 0.95;
    const X = (t) => R.l + (t - T0) / (T1 - T0) * (W - R.l - R.r);
    const Y = (px) => H - R.b - px / dr.size[0] * (H - R.t - R.b);
    const g = sv('g');
    g.append(sv('rect', { class: 'win', x: X(dr.window_s[0]), y: R.t, width: Math.max(0, X(dr.window_s[1]) - X(dr.window_s[0])), height: Math.max(0, H - R.t - R.b) }));
    [0, 720, 1440].forEach((v) => { g.append(sv('line', { class: 'gl', x1: R.l, x2: W - R.r, y1: Y(v), y2: Y(v) }), sv('text', { class: 'ax', x: R.l - 8, y: Y(v) + 5, 'text-anchor': 'end' }, String(v))); });
    [-0.5, 0, 0.5].forEach((t) => g.append(sv('text', { class: 'ax', x: X(t), y: H - 8, 'text-anchor': 'middle' }, (t > 0 ? '+' : t < 0 ? MINUS : '') + Math.abs(t).toFixed(1) + ' s')));
    g.append(sv('line', { class: 'rel', x1: X(0), x2: X(0), y1: R.t, y2: H - R.b }), sv('text', { x: X(0) - 6, y: R.t + 16, 'text-anchor': 'end', fill: '#e6e9f2' }, 'release'));
    const tEnd = lerp(T0, T1, progress);
    const path = (series) => series.filter((p) => p[0] <= tEnd).map((p, k) => (k ? 'L' : 'M') + X(p[0]).toFixed(1) + ' ' + Y(p[1]).toFixed(1)).join(' ');
    g.append(sv('path', { class: 'ln-ref', d: path(dr.series.reference) }), sv('path', { class: 'ln-un', d: path(dr.series.unchanged) }), sv('path', { class: 'ln-ca', d: path(dr.series.candidate) }));
    if (progress > 0.8) {
      const a = Math.min(1, (progress - 0.8) / 0.2);
      const last = (s) => s[s.length - 1];
      g.append(
        sv('text', { class: 'lbl-ca', x: W - R.r, y: Y(last(dr.series.candidate)[1]) - 12, 'text-anchor': 'end', opacity: a }, 'Kiro\u2019s candidate 02'),
        sv('text', { class: 'lbl-ref', x: W - R.r, y: Y(last(dr.series.reference)[1]) - 40, 'text-anchor': 'end', opacity: a }, 'reference and'),
        sv('text', { class: 'lbl-ref', x: W - R.r, y: Y(last(dr.series.reference)[1]) - 17, 'text-anchor': 'end', opacity: a }, 'unchanged app'));
    }
    svg.replaceChildren(g);
  }
  drag.draw = (p) => { drag.last = p; if (drag.svg && drag.svg.parentNode) drawDrag(p); };

  /* ---------------------------------------------------------------- page */
  const chips = [], panels = [];
  let timeline = null, detail = null, chart = null, clockEl = null;

  function buildHeader() {
    const badge = (t) => el('span', { class: 'badge', text: t });
    const sep = () => el('span', { class: 'sep', 'aria-hidden': 'true', text: ' ' + MID + ' ' });
    return el('header', { class: 'top' },
      el('p', { class: 'kicker' }, el('span', { text: 'Agentic Vision evidence ' + MID }), ' ', el('span', {}, 'session ', el('span', { class: 'keep-case', text: D.meta.session }), ' ' + MID), ' ', el('span', { text: '16 October 2026' })),
      el('h1', {}, el('span', { class: 'brand', text: 'KiroClops' }), ': one logged agent session, replayed'),
      el('p', { class: 'sub', text: 'How OpenCV 5 measurements changed what a coding agent did next, replayed from the audit log of one Kiro CLI session.' }),
      el('p', { class: 'badges' }, badge('Kiro CLI'), sep(), badge('8 MCP tools'), sep(), badge('OpenCV 5'), sep(), badge(D.meta.calls + ' audited tool calls')),
      RENDER ? null : el('p', { class: 'intro' }, 'A developer shows the motion they want as a reference clip. Kiro edits the real app\u2019s motion code. Through eight MCP tools Kiro asks KiroClops to re-record each candidate in headless Chromium and measure it frame by frame with OpenCV 5 against pre-registered limits. Held-out cases open only after Kiro seals a candidate. Select a step to see what the measurements showed and what Kiro did next.'));
  }

  const LEGEND = [['get_job'], ['execute_pwsh'], ['fs_write'], ['str_replace'], ['read_file'], ['inspect_session'], ['tool_load', 'list_directory']];
  function buildTimelineCard() {
    const mins = Math.round(D.meta.span_s / 60);
    chips.length = 0;
    const chipRow = el('div', { class: 'chips', role: 'tablist', 'aria-label': 'Key steps of the session' }, S.map((s, i) => {
      const c = el('button', { type: 'button', class: 'chip', role: 'tab', id: 'tab-' + (i + 1), 'aria-selected': 'false', 'aria-controls': 'detail', tabindex: '-1', 'data-i': i,
        'aria-label': 'Step ' + s.n + ': ' + COPY[i].chip + ', ' + whenOf(s) },
        el('span', { class: 'chip-top' }, el('span', { class: 'n', text: String(s.n) }), el('span', { class: 'when', text: whenOf(s) })),
        el('span', { class: 'ct', text: COPY[i].chip }));
      chips.push(c);
      return c;
    }));
    const svg = sv('svg', { class: 'tl-svg', 'aria-hidden': 'true', focusable: 'false' });
    const legend = el('ul', { class: 'legend', 'aria-label': 'Tick colours by tool' },
      LEGEND.map((names) => el('li', {}, el('i', { style: 'background:var(--t-' + (names.length > 1 ? 'other' : names[0]) + ')' }), names.length > 1 ? 'other' : names[0], ' ', el('b', { text: String(names.reduce((a, n) => a + COUNT[n], 0)) }))),
      el('li', {}, el('i', { style: 'background:var(--t-key)' }), 'step calls ', el('b', { text: String([...KEYTOOL].reduce((a, n) => a + COUNT[n], 0)) })));
    clockEl = el('span', { class: 'clock', 'aria-hidden': 'true', text: 'session clock 0:00' });
    const ctl = RENDER ? null : el('div', { class: 'tl-ctl' },
      el('button', { type: 'button', class: 'btn', id: 'prev', text: 'Previous' }),
      el('button', { type: 'button', class: 'btn primary', id: 'play', text: 'Play the replay' }),
      el('button', { type: 'button', class: 'btn', id: 'next', text: 'Next' }));
    legend.append(el('li', { class: 'gap', 'aria-hidden': 'true' }), el('li', {}, clockEl));
    timeline = { card: null, chipRow, svg, legend };
    return (timeline.card = el('section', { class: 'card tl', 'aria-labelledby': 'tl-h' },
      el('div', { class: 'tl-head' },
        el('h2', { id: 'tl-h', text: 'Session timeline' }),
        el('p', { class: 'tl-sub', text: mins + ' minutes ' + MID + ' ' + D.meta.calls + ' audited tool calls ' + MID + ' select a step' }),
        ctl),
      chipRow, svg, legend));
  }

  function buildStage() {
    detail = el('section', { class: 'card detail', id: 'detail', role: 'tabpanel', tabindex: '0', 'aria-labelledby': 'tab-1' });
    panels.length = 0;
    PANELS.forEach((build, i) => {
      const rv = new Reveal();
      const s = S[i];
      const body = build(rv, s);
      const root = el('div', { class: 'pn', id: 'pn-' + (i + 1), hidden: i !== 0 },
        el('header', { class: 'ph' }, el('span', { class: 'ph-n', 'aria-hidden': 'true', text: String(s.n) }),
          el('div', {}, el('h3', { class: 'ph-h', text: COPY[i].head }), el('p', { class: 'ph-m' }, metaOf(i))), rv.headExtra),
        body);
      panels.push({ root, rv });
      detail.append(root);
    });
    chart = buildChart();
    return el('div', { class: 'stage' }, detail, chart.card);
  }

  /* ---- score chart ---- */
  const SCALE = 0.055;
  function buildChart() {
    const cal = SC.calibration, ho = SC.heldout;
    const rows = [
      { g: 0, label: 'Unchanged app', value: cal.unchanged, cls: 'slate', note: 'post-hoc' },
      { g: 0, label: 'Candidate 01', value: cal.c01, cls: 'c01', note: 'better on ' + cal.better.c01 + ' of ' + cal.cases + ' cases' },
      { g: 0, label: 'Candidate 02', value: cal.c02, cls: 'c02', note: 'better on ' + cal.better.c02 + ' of ' + cal.cases + ' cases' },
      { g: 1, label: 'Unchanged app', value: ho.unchanged, cls: 'slate', note: 'post-hoc' },
      { g: 1, label: 'Sealed candidate 02', value: ho.sealed, cls: 'c02', note: 'better on ' + ho.better + ' of ' + ho.cases + ' cases' },
    ];
    const card = el('aside', { class: 'card score', 'aria-labelledby': 'score-h' },
      el('h2', { id: 'score-h', text: 'Score against the reference' }),
      el('p', { class: 'cs', text: 'mean position error, lower is better' }));
    const refs = [];
    let group = -1;
    rows.forEach((r, k) => {
      if (r.g !== group) {
        group = r.g;
        card.append(el('div', { class: 'sgroup' }, group === 0 ? 'Calibration ' + MID + ' ' + cal.cases + ' cases' : 'Held-out ' + MID + ' ' + ho.cases + ' cases',
          el('span', { text: group === 0 ? 'seen by the agent' : 'opened after sealing' })));
      }
      const fill = el('i', { class: 'fill ' + r.cls });
      const val = el('span', { class: 'srow-v', text: f4(r.value) });
      const row = el('div', { class: 'srow' },
        el('div', { class: 'srow-top' }, el('span', { class: 'srow-l', text: r.label }), el('span', { class: 'srow-n', text: r.note })),
        el('div', { class: 'srow-bar' }, el('div', { class: 'track' }, fill), val));
      refs.push({ row, fill, val, value: r.value });
      card.append(row);
    });
    card.append(el('p', { class: 'sfoot' }, el('b', { text: 'Post-hoc recomputation from recorded errors' }),
      RENDER ? ': unchanged app and \u201cbetter on\u201d counts.' : ' for the unchanged app and the \u201cbetter on\u201d counts, with the same formula as the candidate scores, which are the recorded job scores.'));
    return { card, refs };
  }
  /* activity per step: which rows are in play */
  const ACTIVE = [[0], [1], [0, 1], [0, 1], [0, 1], [0, 1], [2], [3, 4]];
  function setChart(vals, active) {
    chart.refs.forEach((r, k) => {
      const v = vals ? vals[k] : 1;
      r.fill.style.width = (r.value / SCALE * 100 * v).toFixed(2) + '%';
      if (RENDER) r.val.style.opacity = v > 0.02 ? Math.min(1, v * 1.6).toFixed(3) : '0';
      r.row.classList.toggle('dimmed', !!active && !active.includes(k));
    });
  }

  /* ---------------------------------------------------------------- timeline drawing */
  const TL = { x: null, G: null, W: 0, H: 0, y2: 0, y3: 0, y1: 0, ph: null, lit: null, band: null, conns: [], dots: [], jobs: [], tipTicks: [], playSec: 0, sel: 0 };
  function drawTimeline() {
    const svg = timeline.svg, rowEl = timeline.chipRow;
    const W = Math.round(rowEl.clientWidth || timeline.card.clientWidth - 40);
    if (!W) return;
    const narrow = !RENDER && W < 560;
    const flat = !RENDER && (narrow || window.innerWidth < 1041); /* chips scroll sideways: no connector lines */
    const G = RENDER ? { gl: 214, gr: 28, conn: 30, l1: 26, l2: 28, l3: 28, gap: 6, ax: 32 }
      : narrow ? { gl: 6, gr: 6, conn: 12, l1: 12, l2: 16, l3: 16, gap: 5, ax: 22 }
      : { gl: 128, gr: 14, conn: flat ? 12 : 34, l1: 18, l2: 26, l3: 26, gap: 6, ax: 26 };
    const x = (t) => G.gl + clamp(t / TMAX) * (W - G.gl - G.gr);
    const y1 = G.conn + 6, y2 = y1 + G.l1 + G.gap, y3 = y2 + G.l2 + G.gap, yAxis = y3 + G.l3 + 8, H = yAxis + G.ax;
    Object.assign(TL, { x, G, W, H, y1, y2, y3, yAxis });
    svg.setAttribute('width', W); svg.setAttribute('height', H); svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    const kids = [];
    const defs = sv('defs', {},
      sv('pattern', { id: 'kc-hatch', width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' },
        sv('rect', { width: 6, height: 6, fill: 'rgba(251,113,133,.10)' }), sv('rect', { width: 2.2, height: 6, fill: 'rgba(251,113,133,.55)' })),
      sv('clipPath', { id: 'kc-lit' }, TL.lit = sv('rect', { x: 0, y: 0, width: 0, height: H })));
    kids.push(defs);
    [[y1, G.l1], [y2, G.l2], [y3, G.l3]].forEach(([y, h]) => kids.push(sv('rect', { class: 'lane', x: G.gl, y, width: Math.max(0, W - G.gl - G.gr), height: h, rx: 5 })));
    if (!narrow) {
      const lab = (y, h, t) => sv('text', { class: 'lane-l', x: 4, y: y + h / 2 + (RENDER ? 6 : 4.5) }, t);
      kids.push(lab(y1, G.l1, 'OpenCV 5 jobs'), lab(y2, G.l2, 'MCP calls'), lab(y3, G.l3, 'Shell + file tools'));
    }
    // axis
    kids.push(sv('line', { class: 'axis', x1: G.gl, x2: W - G.gr, y1: yAxis, y2: yAxis }));
    for (let m = 0; m <= 50; m += 5) {
      const big = m % 10 === 0;
      kids.push(sv('line', { class: 'axis-t', x1: x(m * 60), x2: x(m * 60), y1: yAxis, y2: yAxis + (big ? 7 : 4) }));
      if (big && (!narrow || m % 10 === 0)) kids.push(sv('text', { class: 'ax-l', x: Math.min(x(m * 60), W - 2), y: yAxis + (RENDER ? 28 : 21), 'text-anchor': m === 0 ? 'start' : m === 50 ? 'end' : 'middle' }, m === 0 ? '0' : m === 50 ? '50 min' : (narrow ? String(m) : m + ' min')));
    }
    // jobs lane
    TL.jobs = [];
    const kindOf = (j) => (j.op === 'prepare' ? 'prepare' : j.op === 'finalize' ? 'final' : j.candidate === 'probe-01' ? 'probe' : j.candidate === 'candidate-01' ? 'c01' : 'c02');
    const labelOf = { prepare: 'prepare', c01: 'candidate 01', probe: 'probe', c02: 'candidate 02', final: '' };
    const dur = (j) => int(j.dur) + ' s';
    const JOBTITLE = {
      prepare: (j) => 'prepare_experiment: reference and unchanged app, ' + j.cases + ' ' + 'calibration cases measured in ' + dur(j),
      c01: (j) => 'evaluate_candidate, candidate 01: full calibration, ' + dur(j) + ', error ' + f4(j.score),
      probe: (j) => 'evaluate_candidate, partial probe on 3 cases: cancelled after ' + dur(j),
      c02: (j) => 'evaluate_candidate, candidate 02: full calibration, ' + dur(j) + ', error ' + f4(j.score),
      final: (j) => 'finalize_candidate: ' + j.cases + ' held-out cases measured in ' + dur(j) + ' (continues beyond the 50 minute axis)',
    };
    D.jobs.forEach((j) => {
      const k = kindOf(j), xa = x(j.start), xb = x(j.start + j.dur);
      const w = Math.max(3, xb - xa);
      const bar = sv('rect', { class: 'job ' + k, x: xa, y: y1, width: w, height: G.l1, rx: 4 },
        sv('title', {}, JOBTITLE[k](j)));
      kids.push(bar);
      TL.jobs.push({ bar, kind: k });
      const label = labelOf[k];
      if (!narrow && label && w > label.length * (RENDER ? 9.6 : 7.1) + 10) kids.push(sv('text', { class: 'job-l', x: xa + 7, y: y1 + G.l1 / 2 + (RENDER ? 6 : 4.2) }, label));
      if (k === 'final') kids.push(sv('path', { d: 'M' + xb + ' ' + (y1 + 2) + ' l' + (RENDER ? 11 : 8) + ' ' + (G.l1 / 2 - 2) + ' l-' + (RENDER ? 11 : 8) + ' ' + (G.l1 / 2 - 2) + ' z', fill: 'var(--green)', opacity: 0.8 }));
    });
    // ticks (dim copy always, bright copy clipped to the playhead)
    const mk = (cls) => {
      const g = sv('g', { class: cls });
      ticks.forEach((tk) => {
        const key = KEYTOOL.has(tk.tool), lane = key || MCP.has(tk.tool) ? 2 : 3;
        const top = lane === 2 ? y2 : y3, h = lane === 2 ? G.l2 : G.l3;
        const pad = key ? 2 : Math.round(h * 0.24);
        g.append(sv('line', { class: key ? 'keytick' : 'tick t-' + tk.tool, x1: x(tk.t), x2: x(tk.t), y1: top + pad, y2: top + h - pad }));
      });
      return g;
    };
    kids.push(mk('dim'), sv('g', { 'clip-path': 'url(#kc-lit)' }, mk('lit')));
    TL.tipTicks = ticks.map((tk) => ({ tk, x: x(tk.t), lane: KEYTOOL.has(tk.tool) || MCP.has(tk.tool) ? 2 : 3 }));
    // selection band, connectors, playhead
    TL.band = sv('rect', { class: 'band', x: 0, y: y1 - 3, width: 4, height: y3 + G.l3 - y1 + 6, rx: 3 });
    kids.push(TL.band);
    TL.conns = []; TL.dots = [];
    S.forEach((s, i) => {
      const tx = x((s.start + s.end) / 2);
      const chip = chips[i];
      const cx = chip.offsetLeft + chip.offsetWidth / 2;
      if (flat) {
        const d = sv('circle', { class: 'cdot', cx: tx, cy: 4, r: 3 });
        kids.push(d); TL.dots.push(d); TL.conns.push(null);
      } else {
        const c = sv('path', { class: 'conn', d: 'M' + cx.toFixed(1) + ' 1 C' + cx.toFixed(1) + ' ' + (G.conn * 0.62).toFixed(1) + ' ' + tx.toFixed(1) + ' ' + (G.conn * 0.38).toFixed(1) + ' ' + tx.toFixed(1) + ' ' + (y1 - 1) });
        const d = sv('circle', { class: 'cdot', cx: tx, cy: y1 - 3, r: RENDER ? 4.5 : 3.5 });
        kids.push(c, d); TL.conns.push(c); TL.dots.push(d);
      }
    });
    TL.ph = sv('g', { class: 'ph' }, sv('line', { class: 'ph-line', x1: 0, x2: 0, y1: y1 - 2, y2: yAxis }), sv('path', { class: 'ph-tri', d: 'M-6.5 ' + (yAxis + 9) + ' L6.5 ' + (yAxis + 9) + ' L0 ' + (yAxis - 1) + ' Z' }));
    kids.push(TL.ph);
    svg.replaceChildren(...kids);
    setSelection(TL.sel);
    setPlayhead(TL.playSec);
  }
  function setPlayhead(sec) {
    TL.playSec = sec;
    if (!TL.x) return;
    const px = TL.x(sec);
    TL.ph.setAttribute('transform', 'translate(' + px.toFixed(2) + ' 0)');
    TL.lit.setAttribute('width', Math.max(0, px).toFixed(2));
    clockEl.textContent = 'session clock ' + clock(sec);
  }
  function setSelection(i) {
    TL.sel = i;
    if (!TL.x) return;
    const s = S[i];
    const xa = TL.x(s.start), xb = TL.x(s.end);
    TL.band.setAttribute('x', (xa - 2).toFixed(1));
    TL.band.setAttribute('width', Math.max(4, xb - xa + 4).toFixed(1));
    TL.conns.forEach((c, k) => { if (c) c.classList.toggle('on', k === i); });
    TL.dots.forEach((d, k) => d.classList.toggle('on', k === i));
    const kinds = ['prepare', 'c01', null, null, 'probe', null, 'c02', 'final'];
    TL.jobs.forEach((j) => j.bar.classList.toggle('on', kinds[i] === j.kind));
  }

  /* hover tooltip for the thin ticks */
  function wireTips() {
    const tip = el('div', { class: 'tip', hidden: true });
    document.body.append(tip);
    const svg = timeline.svg;
    const show = (ev) => {
      const r = svg.getBoundingClientRect(), px = ev.clientX - r.left, py = ev.clientY - r.top;
      let best = null;
      if (py >= TL.y2 - 4 && py <= TL.y3 + TL.G.l3 + 4) {
        const lane = py < TL.y3 - 2 ? 2 : 3;
        for (const t of TL.tipTicks) { const d = Math.abs(t.x - px); if (t.lane === lane && d <= 4 && (!best || d < best.d)) best = { t, d }; }
      }
      if (!best) { tip.hidden = true; return; }
      const tk = best.t.tk;
      tip.replaceChildren(el('b', { text: tk.tool }), ' at ' + tk.t.toFixed(1) + ' s', tk.detail ? el('span', { text: tk.detail }) : null);
      tip.hidden = false;
      tip.style.left = Math.min(window.innerWidth - 350, ev.clientX + 14) + 'px';
      tip.style.top = (ev.clientY + 16) + 'px';
    };
    svg.addEventListener('pointermove', show);
    svg.addEventListener('pointerdown', show);
    svg.addEventListener('pointerleave', () => { tip.hidden = true; });
  }

  /* ---------------------------------------------------------------- media, links, trace table (interactive only) */
  function buildMedia() {
    const fig = (src, start, title, text) => el('figure', {},
      el('video', { controls: true, preload: 'metadata', playsinline: true, src: src + '#t=' + start, 'aria-label': title }),
      el('figcaption', {}, el('b', { text: title }), ' ' + text));
    return el('section', { class: 'card media', 'aria-labelledby': 'media-h' },
      el('h2', { id: 'media-h', text: 'Recordings from this session' }),
      el('p', { class: 'lede', text: 'Two short scenes made from the same run. Play them here; they have no sound.' }),
      el('div', { class: 'vids' },
        fig('good-demo-v1.mp4', 5, 'The run and the result (11 s).', 'The logged tool calls of this session, then the reference clip next to Kiro\u2019s candidate 02 as re-recorded, with the measured error of each tracked object for the unchanged app and for Kiro\u2019s code.'),
        fig('heldout-regression.mp4', 8, 'The held-out regression (10 s).', 'The drag-release case at 1440 px, recorded for the reference, the unchanged app and Kiro\u2019s sealed candidate 02, with the OpenCV 5 handle tracks and the post-release error of 0.86 against a limit of 0.05.')));
  }
  function buildEvidence() {
    const gh = D.meta.repo + '/blob/' + D.meta.branch + '/', tree = D.meta.repo + '/tree/' + D.meta.branch + '/';
    const link = (href, text, desc) => el('li', {}, el('a', { href, target: '_blank', rel: 'noopener', text }), ' ' + ENDASH + ' ' + desc);
    const sec = el('section', { class: 'card evidence', 'aria-labelledby': 'ev-h' },
      el('h2', { id: 'ev-h', text: 'Evidence behind this page' }),
      el('p', { class: 'lede', text: 'Everything above comes from these files and from the frozen session archive (available on request). Nothing was measured again for this page.' }),
      el('ul', { class: 'links' },
        link(gh + 'submission/AGENTIC_VISION.md', 'AGENTIC_VISION.md', 'the write-up: trace table, evidence table, baseline comparison'),
        link(gh + 'submission/evidence/agentic-trace-v6p01/trace.json', 'trace.json', 'the 230 audited tool calls, five jobs, diff statistics'),
        link(tree + 'submission/evidence/agentic-trace-v6p01/agent-scratch', 'agent-scratch/', 'the scripts the agent wrote during the run'),
        link(tree + 'submission/evidence/agentic-trace-v6p01', 'candidate diffs', 'the code changes of candidate 01 and candidate 02'),
        link(gh + 'evidence/panel-v6p01/heldout-outcome.json', 'panel-v6p01/heldout-outcome.json', 'result of each of the 56 held-out cases'),
        link(gh + 'evidence/v6-agent-baseline/result.json', 'v6-agent-baseline/result.json', 'unchanged app against the candidates, per case (post-hoc)'),
        link(gh + 'submission/diagrams/agent-loop.png', 'agent-loop.png', 'the perception, decision and action loop'),
        link(gh + 'submission/TECHNICAL_REPORT.md', 'TECHNICAL_REPORT.md', 'the technical report'),
        link(D.meta.repo, 'Repository', 'KiroClops source and evidence')));
    const det = el('details', { class: 'calls' }, el('summary', { text: 'All ' + D.meta.calls + ' audited tool calls' }));
    let built = false;
    det.addEventListener('toggle', () => {
      if (!det.open || built) return;
      built = true;
      const body = el('tbody', {}, ticks.map((tk, k) => el('tr', { class: KEYTOOL.has(tk.tool) ? 'key' : '' },
        el('td', { text: tk.t.toFixed(1) + ' s' }),
        el('td', { class: 't' }, el('i', { style: 'background:' + (KEYTOOL.has(tk.tool) ? 'var(--t-key)' : 'var(--t-' + (COUNT[tk.tool] ? tk.tool : 'other') + ', var(--t-other))') }), tk.tool),
        el('td', { class: 'd', text: tk.detail }))));
      det.append(el('div', { class: 'twrap', tabindex: '0', role: 'region', 'aria-label': 'Audited tool calls' },
        el('table', { class: 'trace' }, el('thead', {}, el('tr', {}, el('th', { text: 'Time' }), el('th', { text: 'Tool' }), el('th', { text: 'Summary of the arguments' }))), body)));
    });
    sec.append(det);
    return sec;
  }

  /* ---------------------------------------------------------------- interactive behaviour */
  let cur = -1, anim = null, playTimer = null, playing = false;
  const SCHED = [0, 4000, 9000, 15500, 22000, 25500, 28500, 34500, 45000];

  function tween(from, to, ms, fn, done) {
    if (anim) cancelAnimationFrame(anim.id);
    if (REDUCED || ms <= 0) { fn(to); if (done) done(); anim = null; return; }
    const t0 = performance.now();
    const a = { id: 0 };
    anim = a;
    const tick = (now) => {
      const u = clamp((now - t0) / ms);
      fn(lerp(from, to, easeOut(u)));
      if (u < 1) a.id = requestAnimationFrame(tick); else if (done) done();
    };
    a.id = requestAnimationFrame(tick);
  }
  let revealAnim = 0;
  function reveal(i) {
    cancelAnimationFrame(revealAnim);
    const rv = panels[i].rv;
    if (REDUCED) { rv.apply(1); return; }
    const t0 = performance.now();
    rv.apply(0);
    const tick = (now) => {
      const u = clamp((now - t0) / 1100);
      rv.apply(easeOut(u));
      if (u < 1) revealAnim = requestAnimationFrame(tick);
    };
    revealAnim = requestAnimationFrame(tick);
  }
  function select(i, opts) {
    opts = opts || {};
    i = clamp(i, 0, S.length - 1);
    if (i === cur && !opts.force) return;
    cur = i;
    chips.forEach((c, k) => { c.setAttribute('aria-selected', k === i ? 'true' : 'false'); c.tabIndex = k === i ? 0 : -1; });
    panels.forEach((p, k) => { p.root.hidden = k !== i; });
    detail.setAttribute('aria-labelledby', 'tab-' + (i + 1));
    setSelection(i);
    tween(TL.playSec, S[i].start, 650, setPlayhead);
    setChart(null, ACTIVE[i]);
    reveal(i);
    if (opts.focus) chips[i].focus();
    const row = timeline.chipRow;
    if (row.scrollWidth > row.clientWidth + 2) {
      const c = chips[i];
      row.scrollTo({ left: c.offsetLeft - (row.clientWidth - c.offsetWidth) / 2, behavior: REDUCED ? 'auto' : 'smooth' });
    }
    if (opts.user && history.replaceState) { try { history.replaceState(null, '', '#step-' + (i + 1)); } catch (e) { /* ignore */ } }
    document.getElementById('prev').disabled = i === 0;
    document.getElementById('next').disabled = i === S.length - 1;
  }
  function stopPlay() {
    playing = false; clearTimeout(playTimer);
    const b = document.getElementById('play');
    if (b) b.textContent = 'Play the replay';
  }
  function playNext() {
    playTimer = setTimeout(() => {
      if (!playing) return;
      if (cur < S.length - 1) { select(cur + 1); playNext(); } else stopPlay();
    }, Math.max(3000, SCHED[cur + 1] - SCHED[cur]));
  }
  function wireInteractive() {
    chips.forEach((c, i) => {
      c.addEventListener('click', () => { stopPlay(); select(i, { user: true }); });
      c.addEventListener('keydown', (ev) => {
        const key = ev.key;
        const to = key === 'ArrowRight' || key === 'ArrowDown' ? Math.min(S.length - 1, i + 1) : key === 'ArrowLeft' || key === 'ArrowUp' ? Math.max(0, i - 1) : key === 'Home' ? 0 : key === 'End' ? S.length - 1 : -1;
        if (to < 0) return;
        ev.preventDefault(); stopPlay(); select(to, { focus: true, user: true });
      });
    });
    document.getElementById('prev').addEventListener('click', () => { stopPlay(); select(cur - 1, { user: true }); });
    document.getElementById('next').addEventListener('click', () => { stopPlay(); select(cur + 1, { user: true }); });
    document.getElementById('play').addEventListener('click', () => {
      if (playing) { stopPlay(); return; }
      playing = true;
      document.getElementById('play').textContent = 'Pause';
      if (cur >= S.length - 1) select(0);
      playNext();
    });
    const fromHash = () => { const m = /^#step-(\d)$/.exec(location.hash); return m && +m[1] >= 1 && +m[1] <= S.length ? +m[1] - 1 : null; };
    window.addEventListener('hashchange', () => { const h = fromHash(); if (h !== null) { stopPlay(); select(h); } });
    return fromHash();
  }

  /* ---------------------------------------------------------------- render mode: state at ms of a 45 s animation */
  const CHART_AT = [[1300, 1100], [5300, 1100], [30300, 1300], [36300, 900], [37300, 1100]]; /* [start, duration] of each bar */
  function locate(ms) { for (let k = 0; k < S.length; k++) if (ms < SCHED[k + 1]) return k; return S.length - 1; }
  function playheadAt(ms, i) {
    const a = SCHED[i], b = SCHED[i + 1], u = ms - a, s = S[i];
    const prev = i === 0 ? 0 : S[i - 1].end, G = 700;
    if (u < G) return lerp(prev, s.start, ease(u / G));
    if (s.end > s.start) return lerp(s.start, s.end, clamp((u - G) / (b - a - G - 500)));
    return s.start;
  }
  let lastSel = -2;
  window.render = function (ms) {
    ms = clamp(+ms || 0, 0, SCHED[SCHED.length - 1] - 1);
    const i = locate(ms), a = SCHED[i], u = ms - a;
    app.style.opacity = ease(ms / 450).toFixed(3);
    if (lastSel !== i) {
      lastSel = i;
      chips.forEach((c, k) => { c.setAttribute('aria-selected', k === i ? 'true' : 'false'); });
    }
    chips.forEach((c, k) => c.classList.toggle('future', k > i));
    setSelection(i);
    setPlayhead(playheadAt(ms, i));
    // panels: the old one fades out, then the new one fades in (their text never overlaps)
    panels.forEach((p, k) => {
      let o = 0, prog = 0;
      if (k === i) { const delay = i === 0 ? 120 : 170; o = ease((u - delay) / 260); prog = clamp((u - delay) / (SCHED[i + 1] - a - delay)); }
      else if (k === i - 1 && u < 170) { o = 1 - ease(u / 170); prog = 1; }
      p.root.style.visibility = o > 0 ? 'visible' : 'hidden';
      p.root.style.opacity = o.toFixed(3);
      if (o > 0) { p.rv.apply(prog); p.canon = false; }
      else if (p.canon !== true) { p.rv.apply(0); p.canon = true; } /* hidden panels sit in one fixed state, so the DOM is a function of ms alone */
    });
    // chart: a bar grows when the replay reaches its step
    const vals = CHART_AT.map(([start, dur]) => ease((ms - start) / dur));
    setChart(vals, ACTIVE[i]);
  };

  /* ---------------------------------------------------------------- boot */
  function build() {
    app.replaceChildren(
      el('div', { class: 'bg-grid', 'aria-hidden': 'true' }), el('div', { class: 'bg-glow', 'aria-hidden': 'true' }),
      buildHeader(),
      el('main', {}, buildTimelineCard(), el('h2', { class: 'sr-only', text: 'Step detail and score' }), buildStage(), RENDER ? null : buildMedia(), RENDER ? null : buildEvidence()),
      el('footer', { class: 'foot' }, 'Real data from session v6p01 (Kiro CLI 2.31, single native-uncertified run). Source: ',
        RENDER ? 'github.com/tritonsan/kiroclops' : el('a', { href: D.meta.repo, target: '_blank', rel: 'noopener', text: 'github.com/tritonsan/kiroclops' })));
  }
  build();
  let startStep = 0;
  if (!RENDER) { wireTips(); const h = wireInteractive(); if (h !== null) startStep = h; }
  const ready = (async () => {
    if (document.fonts && document.fonts.ready) await document.fonts.ready;
    drawTimeline();
    if (RENDER) {
      /* A renderer sets every frame itself, starting at 0. Opened by hand, the page shows its last frame
         (or the frame given as ?render=1&t=<ms>) instead of a blank first frame. */
      const m = /[?&]t=(\d+(?:\.\d+)?)/.exec(location.search);
      window.render(m ? +m[1] : SCHED[SCHED.length - 1] - 1);
    } else {
      cur = -1;
      select(startStep, { force: true });
      setChart(null, ACTIVE[startStep]);
      if (window.ResizeObserver) {
        let raf = 0;
        new ResizeObserver(() => { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => { drawTimeline(); if (drag.svg && drag.svg.parentNode && drag.last !== undefined) drag.draw(drag.last); }); }).observe(timeline.card);
      }
    }
    window.__replayReady = true;
  })();
  window.__ready = ready;
})();
