/* plan_graph.js: once a semester card is focused (focus_card.js), its course list turns into the plan's
 * prerequisite map. This term's courses fly from their rows to their places in the map, the other courses
 * fade in column by column, then the arrows draw from left to right.
 *
 *   const graph = termGraph(plan, semester);   // null: no course of this term is in the map (GE-only terms)
 *   focusCard(card, { widthRatio: graph.widthRatio(card), prepare: graph.prepare,
 *                     onOpen: graph.show, onCloseStart: graph.hide });   // show/hide get (card, copy)
 *
 * The map is plan.graph from POST /generate-sep without GE slots and completed courses, laid out left to right
 * by prerequisite depth. Red = the critical path, dashed = may be taken in the same term (coreq), blue outline =
 * this term's courses. An arrow already implied by a longer chain is left out (MTH 1 -> MTH 2 -> PHYS 4A makes
 * MTH 1 -> PHYS 4A redundant), so every chain reads as one line. Colours are tokens in assets/theme.css.
 */
(function () {
  // Sizes in the map's own px (the map is scaled to fit the card, then with the card)
  const NODE_W = 108, NODE_H = 42, COL_GAP = 44, ROW = 54, PAD = 14, LEGEND_H = 26;
  const GROUP_GAP = 0.5;        // extra rows between unconnected chains
  const EASE = 'cubic-bezier(0.65, 0, 0.35, 1)';
  const SVG = 'http://www.w3.org/2000/svg';
  const reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

  const shortTerm = t => {                          // "Spring 2027" -> "Sp '27"
    const [season, year] = String(t || '').split(' ');
    return season && year ? `${season.slice(0, 2)} '${year.slice(-2)}` : '';
  };

  /** Nodes with x/y (map px), the arrows to draw, and the map's size. */
  function layout(plan) {
    const termOf = new Map(plan.semesters.flatMap(s => s.courses.map(c => [c.id, { term: s.term, units: c.units }])));
    const nodes = (plan.graph?.nodes || []).map(n => n.data).filter(n => !n.is_ge && n.status !== 'completed');
    const byId = new Map(nodes.map(n => [n.id, n]));
    const critNodes = new Set(plan.critical_path || []);
    const critEdges = new Set(plan.critical_path_edges || []);

    const seen = new Set();
    let edges = (plan.graph?.edges || []).map(e => e.data).filter(e => {
      const key = `${e.source}>${e.target}`;
      if (!byId.has(e.source) || !byId.has(e.target) || e.source === e.target || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    // Leave out a -> c when c is also reachable from a through another course (the graph is a DAG)
    const reachable = (from, to, skip) => {
      const stack = [from], visited = new Set([from]);
      while (stack.length) {
        const id = stack.pop();
        for (const e of edges) {
          if (e === skip || e.source !== id || visited.has(e.target)) continue;
          if (e.target === to) return true;
          visited.add(e.target);
          stack.push(e.target);
        }
      }
      return false;
    };
    edges = edges.filter(e => critEdges.has(e.id) || !reachable(e.source, e.target, e));
    const outOf = id => edges.filter(e => e.source === id);

    // Column = longest prerequisite chain before the course (Kahn); a cycle, if any, stays in column 0
    const layer = new Map(nodes.map(n => [n.id, 0]));
    const indeg = new Map(nodes.map(n => [n.id, 0]));
    edges.forEach(e => indeg.set(e.target, indeg.get(e.target) + 1));
    const queue = nodes.filter(n => !indeg.get(n.id)).map(n => n.id);
    while (queue.length) {
      const id = queue.shift();
      for (const e of outOf(id)) {
        layer.set(e.target, Math.max(layer.get(e.target), layer.get(id) + 1));
        indeg.set(e.target, indeg.get(e.target) - 1);
        if (!indeg.get(e.target)) queue.push(e.target);
      }
    }
    const cols = 1 + Math.max(0, ...layer.values());

    // Chains = connected groups; the critical path's group first, then bigger groups. Unlinked courses go last.
    const linked = new Set(edges.flatMap(e => [e.source, e.target]));
    const groupOf = new Map(), groups = [];
    for (const n of nodes) {
      if (!linked.has(n.id) || groupOf.has(n.id)) continue;
      const group = [], stack = [n.id];
      groupOf.set(n.id, group);
      while (stack.length) {
        const id = stack.pop();
        group.push(id);
        for (const e of edges) {
          const other = e.source === id ? e.target : e.target === id ? e.source : null;
          if (other && !groupOf.has(other)) { groupOf.set(other, group); stack.push(other); }
        }
      }
      groups.push(group);
    }
    groups.sort((a, b) => (b.some(id => critNodes.has(id)) - a.some(id => critNodes.has(id))) || b.length - a.length);

    // Rows: like a tree, each course centred on the courses it unlocks; critical and longer chains on top
    const height = new Map();
    const chainHeight = id => {
      if (!height.has(id)) { height.set(id, 0); height.set(id, 1 + Math.max(-1, ...outOf(id).map(e => chainHeight(e.target)))); }
      return height.get(id);
    };
    const row = new Map();
    let cursor = 0;
    for (const group of groups) {
      let next = cursor;
      const place = id => {
        if (row.has(id)) return;
        row.set(id, null);
        const kids = outOf(id).map(e => e.target)
          .sort((a, b) => (critNodes.has(b) - critNodes.has(a)) || chainHeight(b) - chainHeight(a));
        kids.forEach(place);
        const ys = kids.map(k => row.get(k)).filter(y => y != null);
        row.set(id, ys.length ? (Math.min(...ys) + Math.max(...ys)) / 2 : next++);
      };
      group.filter(id => !edges.some(e => e.target === id))
        .sort((a, b) => (critNodes.has(b) - critNodes.has(a)) || chainHeight(b) - chainHeight(a))
        .forEach(place);
      group.forEach(place);                                   // anything a cycle kept out of reach
      // Two courses in one column never share a row
      for (let c = 0; c < cols; c++) {
        const inCol = group.filter(id => layer.get(id) === c).sort((a, b) => row.get(a) - row.get(b));
        for (let i = 1; i < inCol.length; i++) row.set(inCol[i], Math.max(row.get(inCol[i]), row.get(inCol[i - 1]) + 1));
      }
      cursor = Math.max(...group.map(id => row.get(id))) + 1 + GROUP_GAP;
    }
    nodes.filter(n => !linked.has(n.id)).forEach((n, i) => {  // unlinked courses: one row across the columns
      layer.set(n.id, i % cols);
      row.set(n.id, cursor + Math.floor(i / cols));
    });

    const placed = nodes.map(n => ({
      id: n.id, code: n.label || n.id, layer: layer.get(n.id),
      meta: [shortTerm(n.term || termOf.get(n.id)?.term), `${+(n.units ?? termOf.get(n.id)?.units ?? 0)}u`].filter(Boolean).join(' · '),
      critical: critNodes.has(n.id),
      x: PAD + layer.get(n.id) * (NODE_W + COL_GAP),
      y: PAD + row.get(n.id) * ROW
    }));
    const at = new Map(placed.map(p => [p.id, p]));
    const graphH = PAD * 2 + Math.max(0, ...placed.map(p => p.y - PAD)) + NODE_H;
    return {
      nodes: placed,
      edges: edges.map(e => ({ from: at.get(e.source), to: at.get(e.target), critical: critEdges.has(e.id), coreq: e.relation === 'coreq' })),
      width: PAD * 2 + cols * NODE_W + (cols - 1) * COL_GAP,
      graphH,
      height: graphH + LEGEND_H
    };
  }

  // Right-angle arrow with rounded corners, turning in the gap after the source (fan-outs share a trunk)
  function edgePath(from, to) {
    const x1 = from.x + NODE_W, y1 = from.y + NODE_H / 2, x2 = to.x - 6, y2 = to.y + NODE_H / 2;
    const xm = from.x + NODE_W + COL_GAP / 2;
    if (Math.abs(y2 - y1) < 1) return `M${x1},${y1} H${x2}`;
    const r = Math.min(6, Math.abs(y2 - y1) / 2), d = y2 > y1 ? 1 : -1;
    return `M${x1},${y1} H${xm - r} Q${xm},${y1} ${xm},${y1 + d * r} V${y2 - d * r} Q${xm},${y2} ${xm + r},${y2} H${x2}`;
  }

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text) n.textContent = text;
    return n;
  };

  function render(L, termIds) {
    const stage = el('div', 'pg-stage');
    Object.assign(stage.style, { width: `${L.width}px`, height: `${L.height}px` });

    const svg = document.createElementNS(SVG, 'svg');
    svg.setAttribute('class', 'pg-edges');
    svg.setAttribute('width', L.width);
    svg.setAttribute('height', L.graphH);
    const drawn = L.edges.map(e => {
      const kind = `${e.critical ? ' is-critical' : ''}${e.coreq ? ' is-coreq' : ''}`;
      const path = document.createElementNS(SVG, 'path');
      path.setAttribute('class', `pg-edge${kind}`);
      path.setAttribute('d', edgePath(e.from, e.to));
      const head = document.createElementNS(SVG, 'path');
      const hx = e.to.x, hy = e.to.y + NODE_H / 2;
      head.setAttribute('class', `pg-arrow${kind}`);
      head.setAttribute('d', `M${hx},${hy} L${hx - 7},${hy - 4} L${hx - 7},${hy + 4} Z`);
      svg.append(path, head);
      return { path, head, edge: e };
    });
    stage.append(svg);

    const nodeEls = L.nodes.map(n => {
      const node = el('div', `pg-node${n.critical ? ' is-critical' : ''}${termIds.has(n.id) ? ' is-term' : ''}`);
      Object.assign(node.style, { left: `${n.x}px`, top: `${n.y}px`, width: `${NODE_W}px`, height: `${NODE_H}px` });
      node.append(el('span', 'pg-code', n.code), el('span', 'pg-meta', n.meta));
      stage.append(node);
      return { node, n };
    });

    // Legend: only the kinds this map uses
    const legend = el('div', 'pg-legend');
    Object.assign(legend.style, { left: `${PAD}px`, top: `${L.graphH}px` });
    const key = (swatch, label) => { const k = el('span', 'pg-key'); k.append(el('span', swatch), el('span', null, label)); legend.append(k); };
    if (L.edges.some(e => e.critical)) key('pg-swatch-critical', 'Critical path');
    if (L.edges.some(e => e.coreq)) key('pg-swatch-coreq', 'Same term allowed');
    key('pg-swatch-term', 'This term');
    stage.append(legend);

    return { stage, drawn, nodeEls };
  }

  window.termGraph = function termGraph(plan, sem) {
    if (!plan?.graph || !sem || window.innerWidth < 640) return null;   // phones: the plain enlarged card
    const L = layout(plan);
    const termIds = new Set(sem.courses.filter(c => !c.is_ge).map(c => c.id).filter(id => L.nodes.some(n => n.id === id)));
    if (!termIds.size) return null;

    let wrap = null, parts = null, body = [], k = 1, shown = false;

    return {
      /** Focused width as a share of the viewport: wide maps get a wider card, so node text stays readable */
      widthRatio(card) {
        const cs = getComputedStyle(card);
        const innerW = card.offsetWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
        const fit = Math.min(1, innerW / L.width);
        const scaleFor14px = 14 / (12.5 * fit);                       // node codes are 12.5px in the map
        return Math.min(0.64, Math.max(0.49, scaleFor14px * card.offsetWidth / window.innerWidth));
      },

      /** Build the map (hidden) under the card's header; return the height the focused card needs. */
      prepare(copy) {
        const header = copy.firstElementChild;
        if (!header) return 0;
        const cs = getComputedStyle(copy);
        const padL = parseFloat(cs.paddingLeft), padR = parseFloat(cs.paddingRight), padB = parseFloat(cs.paddingBottom);
        const top = header.offsetTop + header.offsetHeight + parseFloat(getComputedStyle(header).marginBottom);
        const innerW = copy.clientWidth - padL - padR;
        k = Math.min(1, innerW / L.width);
        const prev = copy.style.height;
        copy.style.height = 'auto';
        const naturalH = copy.offsetHeight;
        copy.style.height = prev;
        const mapH = L.height * k;
        const wrapH = Math.max(mapH, naturalH - top - padB);

        body = [...copy.children].slice(1);                            // the course list and notes
        parts = render(L, termIds);
        wrap = el('div', 'pg');
        Object.assign(wrap.style, { left: `${padL}px`, top: `${top}px`, width: `${innerW}px`, height: `${wrapH}px` });
        parts.stage.style.transform =
          `translate(${(innerW - L.width * k) / 2}px, ${(wrapH - mapH) / 2}px) scale(${k})`;
        wrap.append(parts.stage);
        copy.append(wrap);
        return top + mapH + padB;
      },

      /** The card has landed: the list turns into the map. (focus_card passes the card, then its copy) */
      show(_card, copy) {
        if (!wrap) return;
        shown = true;
        if (reducedMotion()) {
          body.forEach(b => (b.style.opacity = '0'));
          wrap.style.opacity = '1';
          return;
        }
        const fade = (target, from, to, opts) =>
          target.animate([{ opacity: from }, { opacity: to }], { duration: 250, easing: 'ease-out', fill: 'forwards', ...opts });
        body.forEach(b => fade(b, 1, 0));
        fade(wrap, 0, 1);

        const s = copy.getBoundingClientRect().width / copy.offsetWidth;   // the card's current scale
        for (const { node, n } of parts.nodeEls) {
          const row = termIds.has(n.id) && copy.querySelector(`[data-course="${CSS.escape(n.id)}"]`);
          if (row) {
            // Fly from the course's row in the list to its place in the map
            const a = row.getBoundingClientRect(), b = node.getBoundingClientRect();
            const dx = (a.left - b.left) / (s * k), dy = (a.top - b.top) / (s * k);
            const z = Math.min(1.6, Math.max(0.8, a.height / b.height));
            node.animate([{ transform: `translate(${dx}px, ${dy}px) scale(${z})`, opacity: 0.3 },
                          { transform: 'none', opacity: 1 }], { duration: 600, easing: EASE, fill: 'backwards' });
          } else {
            node.animate([{ transform: 'scale(.92)', opacity: 0 }, { transform: 'none', opacity: 1 }],
              { duration: 320, delay: 180 + n.layer * 70, easing: 'ease-out', fill: 'backwards' });
          }
        }
        for (const { path, head, edge } of parts.drawn) {
          const delay = 520 + edge.from.layer * 70;
          if (edge.coreq) {
            path.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 380, delay, fill: 'backwards' });
          } else {
            const len = path.getTotalLength();
            path.animate([{ strokeDasharray: `${len}`, strokeDashoffset: len }, { strokeDasharray: `${len}`, strokeDashoffset: 0 }],
              { duration: 380, delay, easing: 'ease-in-out', fill: 'backwards' });
          }
          head.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 120, delay: delay + 300, fill: 'backwards' });
        }
        parts.stage.querySelector('.pg-legend')
          ?.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 300, delay: 700, fill: 'backwards' });
      },

      /** Closing: the list comes back while the card shrinks. */
      hide() {
        if (!wrap || !shown) return;
        if (reducedMotion()) {
          body.forEach(b => (b.style.opacity = ''));
          wrap.style.opacity = '0';
          return;
        }
        const opts = { duration: 200, easing: 'ease-out', fill: 'forwards' };
        wrap.animate([{ opacity: 1 }, { opacity: 0 }], opts);
        body.forEach(b => b.animate([{ opacity: 0 }, { opacity: 1 }], opts));
      }
    };
  };
})();
