// Edge renderer and layout audit for the KiroClops submission diagrams.
// Edges are declared in each page as window.EDGES and attached to element
// sides, so arrows follow the boxes when text reflows. audit() measures every
// glyph, box and arrow and returns the problems; the build refuses to export
// a diagram with any problem.
(function () {
  const canvas = document.getElementById('canvas');
  const NS = 'http://www.w3.org/2000/svg';

  function rel(r) {
    const c = canvas.getBoundingClientRect();
    return { x: r.left - c.left, y: r.top - c.top, w: r.width, h: r.height, r: r.right - c.left, b: r.bottom - c.top };
  }
  function rectOf(el) { return rel(el.getBoundingClientRect()); }
  function element(selector) {
    const el = document.querySelector(selector);
    if (!el) throw new Error('missing element ' + selector);
    return el;
  }
  function anchor(selector, side, shift) {
    const r = rectOf(element(selector));
    shift = shift || 0;
    if (side === 'left') return [r.x, r.y + r.h / 2 + shift];
    if (side === 'right') return [r.r, r.y + r.h / 2 + shift];
    if (side === 'top') return [r.x + r.w / 2 + shift, r.y];
    if (side === 'bottom') return [r.x + r.w / 2 + shift, r.b];
    throw new Error('bad side ' + side);
  }
  function direction(side) {
    return { left: [-1, 0], right: [1, 0], top: [0, -1], bottom: [0, 1] }[side];
  }
  function rounded(points, radius) {
    let d = `M${points[0][0]},${points[0][1]}`;
    for (let i = 1; i < points.length - 1; i++) {
      const [px, py] = points[i - 1], [cx, cy] = points[i], [nx, ny] = points[i + 1];
      const l1 = Math.hypot(cx - px, cy - py), l2 = Math.hypot(nx - cx, ny - cy);
      const r = Math.min(radius, l1 / 2, l2 / 2);
      const ax = cx - (cx - px) / l1 * r, ay = cy - (cy - py) / l1 * r;
      const bx = cx + (nx - cx) / l2 * r, by = cy + (ny - cy) / l2 * r;
      d += ` L${ax},${ay} Q${cx},${cy} ${bx},${by}`;
    }
    const last = points[points.length - 1];
    return d + ` L${last[0]},${last[1]}`;
  }
  function pathData(e, A, B) {
    const route = e.route || 'straight';
    if (route === 'straight') return `M${A[0]},${A[1]} L${B[0]},${B[1]}`;
    if (route === 'hvh') { const x = e.via != null ? e.via : (A[0] + B[0]) / 2; return rounded([A, [x, A[1]], [x, B[1]], B], 12); }
    if (route === 'vhv') { const y = e.via != null ? e.via : (A[1] + B[1]) / 2; return rounded([A, [A[0], y], [B[0], y], B], 12); }
    if (route === 'curve') {
      const k = e.k || 70, da = direction(e.fromSide), db = direction(e.toSide);
      return `M${A[0]},${A[1]} C${A[0] + da[0] * k},${A[1] + da[1] * k} ${B[0] + db[0] * k},${B[1] + db[1] * k} ${B[0]},${B[1]}`;
    }
    throw new Error('bad route ' + route);
  }
  function marker(defs, id, color) {
    const m = document.createElementNS(NS, 'marker');
    m.setAttribute('id', id); m.setAttribute('viewBox', '0 0 10 10'); m.setAttribute('refX', '9'); m.setAttribute('refY', '5');
    m.setAttribute('markerWidth', '7'); m.setAttribute('markerHeight', '7'); m.setAttribute('orient', 'auto-start-reverse');
    const p = document.createElementNS(NS, 'path');
    p.setAttribute('d', 'M0,0.5 L10,5 L0,9.5 z'); p.setAttribute('fill', color);
    m.appendChild(p); defs.appendChild(m);
  }

  const COLORS = { dark: '#334155', agent: '#6d28d9', cv: '#15803d', aws: '#c2410c', muted: '#64748b' };

  window.renderEdges = function () {
    document.querySelectorAll('svg.edges, .badge[data-edge], .elabel[data-edge]').forEach(n => n.remove());
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('class', 'edges');
    svg.setAttribute('width', canvas.clientWidth); svg.setAttribute('height', canvas.clientHeight);
    const defs = document.createElementNS(NS, 'defs'); svg.appendChild(defs);
    Object.entries(COLORS).forEach(([k, c]) => marker(defs, 'arrow-' + k, c));
    // Drawn above zones and boxes (the audit guarantees edges avoid unrelated
    // boxes); badges and labels are appended afterwards, so they sit on top.
    canvas.appendChild(svg);
    const rendered = [];
    (window.EDGES || []).forEach(e => {
      const A = anchor(e.from, e.fromSide, e.fromShift), B = anchor(e.to, e.toSide, e.toShift);
      // align: keep a straight edge exactly horizontal or vertical by moving
      // the target anchor along its side to the source coordinate.
      if (e.align) { if (e.fromSide === 'top' || e.fromSide === 'bottom') B[0] = A[0]; else B[1] = A[1]; }
      const color = e.color || 'dark';
      const p = document.createElementNS(NS, 'path');
      p.setAttribute('d', pathData(e, A, B));
      p.setAttribute('fill', 'none'); p.setAttribute('stroke', COLORS[color]);
      p.setAttribute('stroke-width', e.width || 2.4); p.setAttribute('stroke-linecap', 'round'); p.setAttribute('stroke-linejoin', 'round');
      if (e.dashed) p.setAttribute('stroke-dasharray', '7 6');
      if (!e.noArrow) p.setAttribute('marker-end', `url(#arrow-${color})`);
      if (e.both) p.setAttribute('marker-start', `url(#arrow-${color})`);
      p.dataset.edge = e.id;
      svg.appendChild(p);
      const own = [element(e.from).closest('[data-solid]') || element(e.from), element(e.to).closest('[data-solid]') || element(e.to)];
      const length = p.getTotalLength();
      if (e.badge) {
        const at = p.getPointAtLength(length * (e.badgeAt != null ? e.badgeAt : 0.5));
        const b = document.createElement('div');
        b.className = 'badge ' + (e.badgeClass || ''); b.textContent = e.badge; b.dataset.edge = e.id; b.dataset.solid = ''; b.dataset.box = '';
        b.style.left = (at.x + (e.badgeOffset ? e.badgeOffset[0] : 0)) + 'px'; b.style.top = (at.y + (e.badgeOffset ? e.badgeOffset[1] : 0)) + 'px';
        canvas.appendChild(b); own.push(b);
      }
      if (e.label) {
        const at = p.getPointAtLength(length * (e.labelAt != null ? e.labelAt : 0.5));
        const l = document.createElement('div');
        l.className = 'elabel'; l.textContent = e.label; l.dataset.edge = e.id; l.dataset.solid = ''; l.dataset.box = '';
        l.style.left = (at.x + (e.labelOffset ? e.labelOffset[0] : 0)) + 'px'; l.style.top = (at.y + (e.labelOffset ? e.labelOffset[1] : 0)) + 'px';
        canvas.appendChild(l); own.push(l);
      }
      rendered.push({ id: e.id, path: p, own: own });
    });
    window.__edges = rendered;
    return rendered.length;
  };

  // Audit ------------------------------------------------------------------
  function textRects(root) {
    const out = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      if (!n.textContent.trim()) continue;
      const range = document.createRange(); range.selectNodeContents(n);
      for (const r of range.getClientRects()) {
        if (r.width < 0.5 || r.height < 0.5) continue;
        out.push({ node: n, el: n.parentElement, rect: rel(r) });
      }
    }
    return out;
  }
  function inside(a, b, tol) { tol = tol == null ? 0.5 : tol; return a.x >= b.x - tol && a.y >= b.y - tol && a.r <= b.r + tol && a.b <= b.b + tol; }
  function intersects(a, b, pad) { pad = pad || 0; return a.x < b.r + pad && b.x < a.r + pad && a.y < b.b + pad && b.y < a.b + pad; }
  function name(el) { return el.id || (el.className && String(el.className)) + ':' + (el.textContent || '').trim().slice(0, 24); }

  window.audit = function () {
    const problems = [];
    const C = { x: 0, y: 0, r: canvas.clientWidth, b: canvas.clientHeight };
    const texts = textRects(canvas);
    // 1. Every glyph inside the canvas and inside its own box; no text below 16 px.
    for (const t of texts) {
      const label = t.node.textContent.trim().slice(0, 40);
      if (!inside(t.rect, C, 0)) problems.push({ kind: 'outside_canvas', text: label });
      const box = t.el.closest('[data-box]');
      if (box) {
        const br = rectOf(box), style = getComputedStyle(box);
        const border = parseFloat(style.borderLeftWidth) || 0;
        const content = { x: br.x + border, y: br.y + border, r: br.r - border, b: br.b - border };
        if (!inside(t.rect, content, 0.5)) problems.push({ kind: 'text_overflow', box: name(box), text: label,
          by: { left: +(content.x - t.rect.x).toFixed(1), top: +(content.y - t.rect.y).toFixed(1), right: +(t.rect.r - content.r).toFixed(1), bottom: +(t.rect.b - content.b).toFixed(1) } });
      }
      const size = parseFloat(getComputedStyle(t.el).fontSize);
      if (size < 16) problems.push({ kind: 'font_too_small', size: size, text: label });
    }
    // 2. Solid boxes (nodes, tiles, badges, labels, panels) never overlap each other.
    const solids = Array.from(canvas.querySelectorAll('[data-solid]'));
    for (let i = 0; i < solids.length; i++) {
      for (let j = i + 1; j < solids.length; j++) {
        const a = solids[i], b = solids[j];
        if (a.contains(b) || b.contains(a)) continue;
        if (intersects(rectOf(a), rectOf(b), 4)) problems.push({ kind: 'overlap', a: name(a), b: name(b) });
      }
    }
    // 3. Every solid stays inside the canvas and, with data-in, inside its group
    //    with at least 12 px of breathing room from the group border.
    for (const s of solids) {
      const r = rectOf(s);
      if (!inside(r, C, 0)) problems.push({ kind: 'box_outside_canvas', box: name(s) });
      if (s.dataset.in) {
        const g = rectOf(element(s.dataset.in));
        const room = { x: g.x + 12, y: g.y + 12, r: g.r - 12, b: g.b - 12 };
        if (!inside(r, room, 0)) problems.push({ kind: 'box_too_close_to_group_border', box: name(s), group: s.dataset.in });
      }
    }
    // 3b. Group labels never collide with boxes placed in the group.
    for (const head of canvas.querySelectorAll('[data-head]')) {
      for (const s of solids) {
        if (head.contains(s) || s.contains(head)) continue;
        if (intersects(rectOf(head), rectOf(s), 6)) problems.push({ kind: 'group_label_collision', label: name(head), box: name(s) });
      }
    }
    // 4. Edges never run through an unrelated box or over any label/badge but their own.
    for (const e of window.__edges || []) {
      const len = e.path.getTotalLength();
      const hit = new Set();
      for (let s = 7; s < len - 7; s += 3) {
        const p = e.path.getPointAtLength(s);
        for (const el of solids) {
          if (hit.has(el) || e.own.includes(el)) continue;
          const r = rectOf(el);
          if (p.x > r.x + 1 && p.x < r.r - 1 && p.y > r.y + 1 && p.y < r.b - 1) {
            hit.add(el); problems.push({ kind: 'edge_crosses', edge: e.id, box: name(el) });
          }
        }
      }
    }
    // 5. Edges do not cross each other except where declared.
    const allowed = new Set((window.ALLOWED_CROSSINGS || []).map(pair => pair.slice().sort().join('|')));
    const samples = (window.__edges || []).map(e => {
      const len = e.path.getTotalLength(), pts = [];
      for (let s = 9; s < len - 9; s += 3) { const p = e.path.getPointAtLength(s); pts.push([p.x, p.y]); }
      return { id: e.id, pts: pts };
    });
    for (let i = 0; i < samples.length; i++) {
      for (let j = i + 1; j < samples.length; j++) {
        const key = [samples[i].id, samples[j].id].sort().join('|');
        if (allowed.has(key)) continue;
        let crossed = false;
        for (const a of samples[i].pts) {
          for (const b of samples[j].pts) { if (Math.abs(a[0] - b[0]) < 2.5 && Math.abs(a[1] - b[1]) < 2.5) { crossed = true; break; } }
          if (crossed) break;
        }
        if (crossed) problems.push({ kind: 'edges_touch', a: samples[i].id, b: samples[j].id });
      }
    }
    return problems;
  };

  window.rectsOf = function (selectors) {
    const out = {};
    for (const s of selectors) { const el = document.querySelector(s); out[s] = el ? rectOf(el) : null; }
    return out;
  };

  window.addEventListener('load', () => { document.fonts.ready.then(() => window.renderEdges()); });
})();
