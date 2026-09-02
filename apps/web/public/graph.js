"use strict";
/*
 * graph.js - force-directed canvas graph for JARVIS.
 *
 * Canvas, not SVG (SVG stalls past ~1500 nodes - a DOM node per element).
 * Repulsion uses a spatial grid with a distance cutoff so cost stays
 * near-linear instead of O(n^2).
 */

const PHYSICS = {
  CELL_SIZE: 140,          // spatial grid cell size == repulsion cutoff
  REPULSION: 2600,
  SPRING_LENGTH: 110,
  SPRING_STRENGTH: 0.02,
  GRAVITY: 0.0006,
  DAMPING: 0.82,
  WARMUP_TICKS: 480,       // synchronous ticks before first render ("settles on load")
  BREATHE_AMPLITUDE: 0.06, // small perpetual motion after settling ("keeps breathing")
  BREATHE_SPEED: 0.0009,
};

const VISUAL = {
  NODE_MIN_R: 5,
  NODE_MAX_R: 22,
  HOVER_DIM_ALPHA: 0.1,
  // Fitting 138 real notes on screen lands around scale 0.5, and a graph
  // with no labels at its default zoom is unreadable. Collision rejection
  // against discs and other labels already keeps this from turning into
  // soup - at this density only the hubs win space, which is the point.
  LABEL_MIN_SCALE: 0.3,
  PULSE_INTERVAL_MS: [2200, 4200],
  FIT_PADDING: 60,
  // Frame the bulk of the graph, not its outliers. One disconnected note
  // drifting off under repulsion would otherwise shrink everything else to
  // a speck - which also drops the whole graph below LABEL_MIN_SCALE.
  FIT_PERCENTILE: 0.02,
  FIT_DURATION_MS: 600,
  SETTLE_REFIT_MS: [900, 2100],  // springs keep contracting after warmup; re-fit
};

// The rails float over the canvas, so "fit" has to aim at the gap between
// them rather than at the middle of the window.
function viewInsets() {
  if (window.innerWidth <= 960) return { left: 20, right: 20, top: 70, bottom: 190 };
  if (window.innerWidth <= 1240) return { left: 290, right: 250, top: 70, bottom: 190 };
  return { left: 345, right: 300, top: 70, bottom: 200 };
}

function easeInOutCubic(t) {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

/*
 * Categorical colour, assigned once from the whole graph and then frozen.
 *
 * Frozen matters: hiding a folder through the Filter panel must not repaint
 * the folders that are still on screen. Only the four largest get a hue -
 * the palette is validated for exactly four on this surface - and the rest
 * share the neutral, identified by name in the legend rather than by colour.
 */
const SERIES_SLOTS = ["--series-1", "--series-2", "--series-3", "--series-4"];
const typeSlots = new Map();

function assignTypeColors(nodes) {
  const counts = {};
  for (const n of nodes) counts[n.type] = (counts[n.type] || 0) + 1;
  const ordered = Object.keys(counts)
    .sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
  typeSlots.clear();
  ordered.forEach((t, i) => {
    typeSlots.set(t, i < SERIES_SLOTS.length ? SERIES_SLOTS[i] : "--series-other");
  });
}

function typeColor(type) {
  const slot = typeSlots.get(type) || "--series-other";
  return getComputedStyle(document.documentElement).getPropertyValue(slot).trim();
}

// Author-written links are the real structure; membership edges are the
// weak scaffolding that stops a folder of repo docs being unconnected dust.
// They are drawn faint enough that the real links read through them.
const EDGE_ALPHA = { wikilink: 0.22, link: 0.22, repo: 0.1, group: 0.07 };

class Graph {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.nodes = [];
    this.nodeById = new Map();
    this.edges = [];
    this.adjacency = new Map(); // id -> Set(id)

    this.scale = 1;
    this.offsetX = 0;
    this.offsetY = 0;

    this.hoverId = null;
    this.focusId = null;
    this.pathFirstId = null;   // shift-click anchor
    this.highlightedPath = null; // array of ids

    this.hiddenTypes = new Set();
    this.labelsVisible = true;
    this.contrast = 1;          // link/label brightness multiplier
    this._camAnim = null;       // in-flight fitToView tween

    this.dragNode = null;
    this.isPanning = false;
    this.dragMoved = false;
    this.lastMouse = { x: 0, y: 0 };

    this.pulses = []; // {edge, t}
    this._nextPulseAt = 0;

    this.onFocusNote = null;
    this.onHover = null;
    this.onClearFocus = null;

    this._resize = this._resize.bind(this);
    window.addEventListener("resize", this._resize);
    this._resize();

    this._bindEvents();
    this._raf = requestAnimationFrame(this._tick.bind(this));
  }

  // ---------------------------------------------------------------- data
  load(data) {
    assignTypeColors(data.nodes);
    const w = this.canvas.width / (window.devicePixelRatio || 1);
    const h = this.canvas.height / (window.devicePixelRatio || 1);

    this.nodes = data.nodes.map((n, i) => {
      const angle = (i / data.nodes.length) * Math.PI * 2;
      const r0 = 120 + Math.random() * 60;
      return {
        ...n,
        x: Math.cos(angle) * r0,
        y: Math.sin(angle) * r0,
        vx: 0, vy: 0,
        radius: VISUAL.NODE_MIN_R +
          Math.sqrt(n.degree) * ((VISUAL.NODE_MAX_R - VISUAL.NODE_MIN_R) / 3),
        seed: Math.random() * Math.PI * 2,
        color: typeColor(n.type),
      };
    });
    this.nodeById = new Map(this.nodes.map((n) => [n.id, n]));
    this.edges = data.edges.filter(
      (e) => this.nodeById.has(e.source) && this.nodeById.has(e.target)
    );

    this.adjacency = new Map();
    for (const n of this.nodes) this.adjacency.set(n.id, new Set());
    for (const e of this.edges) {
      this.adjacency.get(e.source).add(e.target);
      this.adjacency.get(e.target).add(e.source);
    }

    // Warm up synchronously so the graph "settles on load" rather than
    // visibly unfolding from a circle every time the page opens.
    for (let i = 0; i < PHYSICS.WARMUP_TICKS; i++) this._step(1, false);

    this.offsetX = w / 2;
    this.offsetY = h / 2;
    this.fitToView({ animate: false });
    // The warmup gets the shape right but the springs go on tightening for a
    // second or so afterwards, which would leave the first fit too loose.
    // One delayed re-fit lands it - unless the human has already taken over.
    this._userMoved = false;
    for (const delay of VISUAL.SETTLE_REFIT_MS) {
      setTimeout(() => { if (!this._userMoved) this.fitToView(); }, delay);
    }
    this._scheduleNextPulse();
  }

  setLabelsVisible(visible) { this.labelsVisible = visible; }

  setContrast(level) { this.contrast = level; }

  /* Frames every visible node inside the gap between the two rails. */
  fitToView({ animate = true } = {}) {
    const visible = this.nodes.filter((n) => this._visible(n));
    if (!visible.length) return;

    const span = (values) => {
      const sorted = [...values].sort((a, b) => a - b);
      const k = Math.floor(sorted.length * VISUAL.FIT_PERCENTILE);
      return [sorted[k], sorted[sorted.length - 1 - k]];
    };
    const maxR = Math.max(...visible.map((n) => n.radius));
    const [loX, hiX] = span(visible.map((n) => n.x));
    const [loY, hiY] = span(visible.map((n) => n.y));
    const minX = loX - maxR, maxX = hiX + maxR;
    const minY = loY - maxR, maxY = hiY + maxR;

    const dpr = window.devicePixelRatio || 1;
    const w = this.canvas.width / dpr, h = this.canvas.height / dpr;
    const ins = viewInsets();
    const boxW = Math.max(120, w - ins.left - ins.right);
    const boxH = Math.max(120, h - ins.top - ins.bottom);
    const pad = VISUAL.FIT_PADDING;

    const scale = Math.min(4, Math.max(0.15, Math.min(
      (boxW - pad) / Math.max(1, maxX - minX),
      (boxH - pad) / Math.max(1, maxY - minY)
    )));
    const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
    const target = {
      scale,
      offsetX: ins.left + boxW / 2 - cx * scale,
      offsetY: ins.top + boxH / 2 - cy * scale,
    };

    if (!animate) {
      this.scale = target.scale;
      this.offsetX = target.offsetX;
      this.offsetY = target.offsetY;
      this._camAnim = null;
      return;
    }
    this._camAnim = {
      from: { scale: this.scale, offsetX: this.offsetX, offsetY: this.offsetY },
      to: target,
      start: performance.now(),
    };
  }

  _stepCamera(now) {
    if (!this._camAnim) return;
    const { from, to, start } = this._camAnim;
    const t = Math.min(1, (now - start) / VISUAL.FIT_DURATION_MS);
    const k = easeInOutCubic(t);
    this.scale = from.scale + (to.scale - from.scale) * k;
    this.offsetX = from.offsetX + (to.offsetX - from.offsetX) * k;
    this.offsetY = from.offsetY + (to.offsetY - from.offsetY) * k;
    if (t >= 1) this._camAnim = null;
  }

  setTypeVisible(type, visible) {
    if (visible) this.hiddenTypes.delete(type);
    else this.hiddenTypes.add(type);
  }

  focusById(id, { fromShiftClick = false } = {}) {
    const node = this.nodeById.get(id);
    if (!node) return;

    if (fromShiftClick && this.focusId && this.focusId !== id) {
      this.highlightedPath = this._shortestPath(this.focusId, id);
    } else {
      this.highlightedPath = null;
    }
    this.focusId = id;
    this._userMoved = true;
    if (this.onFocusNote) this.onFocusNote(node);
  }

  _shortestPath(startId, endId) {
    if (startId === endId) return [startId];
    const visited = new Set([startId]);
    const queue = [[startId]];
    while (queue.length) {
      const path = queue.shift();
      const last = path[path.length - 1];
      for (const next of this.adjacency.get(last) || []) {
        if (next === endId) return [...path, next];
        if (!visited.has(next)) {
          visited.add(next);
          queue.push([...path, next]);
        }
      }
    }
    return null;
  }

  // ------------------------------------------------------------ physics
  _step(dt, breathing) {
    const cell = PHYSICS.CELL_SIZE;
    const grid = new Map();
    for (const n of this.nodes) {
      const key = `${Math.floor(n.x / cell)},${Math.floor(n.y / cell)}`;
      if (!grid.has(key)) grid.set(key, []);
      grid.get(key).push(n);
    }

    for (const n of this.nodes) {
      let fx = 0, fy = 0;
      const cx = Math.floor(n.x / cell), cy = Math.floor(n.y / cell);
      for (let gx = cx - 1; gx <= cx + 1; gx++) {
        for (let gy = cy - 1; gy <= cy + 1; gy++) {
          const bucket = grid.get(`${gx},${gy}`);
          if (!bucket) continue;
          for (const other of bucket) {
            if (other === n) continue;
            let dx = n.x - other.x, dy = n.y - other.y;
            let distSq = dx * dx + dy * dy;
            if (distSq > cell * cell * 2.25) continue;
            if (distSq < 1) { distSq = 1; dx = Math.random() - 0.5; dy = Math.random() - 0.5; }
            const dist = Math.sqrt(distSq);
            const force = PHYSICS.REPULSION / distSq;
            fx += (dx / dist) * force;
            fy += (dy / dist) * force;
          }
        }
      }
      // gravity toward origin, keeps the whole graph from drifting
      fx -= n.x * PHYSICS.GRAVITY;
      fy -= n.y * PHYSICS.GRAVITY;

      n._fx = fx;
      n._fy = fy;
    }

    for (const e of this.edges) {
      const a = this.nodeById.get(e.source), b = this.nodeById.get(e.target);
      let dx = b.x - a.x, dy = b.y - a.y;
      const dist = Math.max(1, Math.sqrt(dx * dx + dy * dy));
      const displacement = dist - PHYSICS.SPRING_LENGTH;
      const force = displacement * PHYSICS.SPRING_STRENGTH;
      const fx = (dx / dist) * force, fy = (dy / dist) * force;
      a._fx += fx; a._fy += fy;
      b._fx -= fx; b._fy -= fy;
    }

    const t = performance.now();
    for (const n of this.nodes) {
      if (n === this.dragNode) { n.vx = 0; n.vy = 0; continue; }
      n.vx = (n.vx + n._fx * dt) * PHYSICS.DAMPING;
      n.vy = (n.vy + n._fy * dt) * PHYSICS.DAMPING;
      if (breathing) {
        n.vx += Math.sin(t * PHYSICS.BREATHE_SPEED + n.seed) * PHYSICS.BREATHE_AMPLITUDE;
        n.vy += Math.cos(t * PHYSICS.BREATHE_SPEED * 1.3 + n.seed) * PHYSICS.BREATHE_AMPLITUDE;
      }
      n.x += n.vx * dt;
      n.y += n.vy * dt;
    }
  }

  // --------------------------------------------------------------- pulse
  _scheduleNextPulse() {
    const [lo, hi] = VISUAL.PULSE_INTERVAL_MS;
    this._nextPulseAt = performance.now() + lo + Math.random() * (hi - lo);
  }

  _maybeSpawnPulse(now) {
    if (!this.edges.length || now < this._nextPulseAt) return;
    const edge = this.edges[Math.floor(Math.random() * this.edges.length)];
    this.pulses.push({ edge, t: 0 });
    this._scheduleNextPulse();
  }

  // -------------------------------------------------------------- frame
  _tick() {
    this._step(1, true);
    const now = performance.now();
    this._stepCamera(now);
    this._maybeSpawnPulse(now);
    this.pulses = this.pulses.filter((p) => p.t < 1);
    for (const p of this.pulses) p.t += 0.012;
    this._draw();
    this._raf = requestAnimationFrame(this._tick.bind(this));
  }

  _worldToScreen(x, y) {
    return [x * this.scale + this.offsetX, y * this.scale + this.offsetY];
  }
  _screenToWorld(x, y) {
    return [(x - this.offsetX) / this.scale, (y - this.offsetY) / this.scale];
  }

  _visible(n) { return !this.hiddenTypes.has(n.type); }

  /*
   * A node is "lit" when it's the anchor or a direct neighbour of it. Hover
   * wins over focus so pointing at something always previews it, but with no
   * pointer on the canvas a clicked node keeps its neighbourhood lit - which
   * is what the inspector's own instructions promise.
   */
  _anchorId() { return this.hoverId || this.focusId; }

  _lit(n) {
    const anchor = this._anchorId();
    if (!anchor) return true;
    if (n.id === anchor) return true;
    return !!this.adjacency.get(anchor)?.has(n.id);
  }

  _draw() {
    const ctx = this.ctx;
    const dpr = window.devicePixelRatio || 1;
    const w = this.canvas.width / dpr, h = this.canvas.height / dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const pathSet = this.highlightedPath ? new Set(this.highlightedPath) : null;

    // edges
    for (const e of this.edges) {
      const a = this.nodeById.get(e.source), b = this.nodeById.get(e.target);
      if (!this._visible(a) || !this._visible(b)) continue;
      const [ax, ay] = this._worldToScreen(a.x, a.y);
      const [bx, by] = this._worldToScreen(b.x, b.y);

      let alpha = (EDGE_ALPHA[e.kind] ?? EDGE_ALPHA.link) * this.contrast;
      let width = 1;
      let color = "rgba(255,255,255,0.5)";
      const anchor = this._anchorId();
      const hoverLit = anchor && (a.id === anchor || b.id === anchor);
      const onPath = pathSet && pathSet.has(a.id) && pathSet.has(b.id) &&
        Math.abs(this.highlightedPath.indexOf(a.id) - this.highlightedPath.indexOf(b.id)) === 1;

      if (anchor && !hoverLit) alpha = VISUAL.HOVER_DIM_ALPHA * 0.4;
      if (hoverLit) { alpha = 0.7; width = 1.5; }
      if (onPath) { alpha = 0.95; width = 2.2; color = "rgba(94,234,212,0.9)"; }

      ctx.strokeStyle = color;
      ctx.globalAlpha = alpha;
      ctx.lineWidth = width;
      ctx.beginPath();
      ctx.moveTo(ax, ay);
      ctx.lineTo(bx, by);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;

    // idle pulses traveling along edges
    for (const p of this.pulses) {
      const a = this.nodeById.get(p.edge.source), b = this.nodeById.get(p.edge.target);
      if (!this._visible(a) || !this._visible(b)) continue;
      const x = a.x + (b.x - a.x) * p.t, y = a.y + (b.y - a.y) * p.t;
      const [sx, sy] = this._worldToScreen(x, y);
      ctx.globalAlpha = 1 - Math.abs(p.t - 0.5) * 1.2;
      ctx.fillStyle = "#5eead4";
      ctx.beginPath();
      ctx.arc(sx, sy, 2.4, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;

    // nodes
    const labelCandidates = [];
    for (const n of this.nodes) {
      if (!this._visible(n)) continue;
      const [sx, sy] = this._worldToScreen(n.x, n.y);
      if (sx < -60 || sx > w + 60 || sy < -60 || sy > h + 60) continue;

      const connected = this._lit(n);
      const isFocus = n.id === this.focusId;
      const onPath = pathSet && pathSet.has(n.id);
      let alpha = connected ? 1 : VISUAL.HOVER_DIM_ALPHA;
      const r = n.radius * this.scale * (n.id === this.hoverId ? 1.15 : 1);

      ctx.globalAlpha = alpha;
      if (isFocus || onPath) {
        ctx.beginPath();
        ctx.arc(sx, sy, r + 4, 0, Math.PI * 2);
        ctx.strokeStyle = "#5eead4";
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
      ctx.beginPath();
      ctx.arc(sx, sy, r, 0, Math.PI * 2);
      ctx.fillStyle = n.color;
      ctx.fill();
      ctx.globalAlpha = 1;

      if (connected) labelCandidates.push({ n, sx, sy, r });
    }

    // labels: most-connected first, reject collisions
    if (this.labelsVisible && this.scale >= VISUAL.LABEL_MIN_SCALE) {
      labelCandidates.sort((a, b) => b.n.degree - a.n.degree);
      const overlaps = (a, b) =>
        a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;

      // Obstacles = every drawn disc. A label over an unrelated node reads as
      // that node's label, which is worse than no label at all.
      const discs = labelCandidates.map(({ n, sx, sy, r }) =>
        ({ id: n.id, x: sx - r, y: sy - r, w: r * 2, h: r * 2 }));

      const placed = [];
      ctx.font = "11px -apple-system, Segoe UI, Roboto, sans-serif";
      for (const { n, sx, sy, r } of labelCandidates) {
        const text = n.title;
        const metrics = ctx.measureText(text);
        const boxW = metrics.width + 8, boxH = 14;
        const box = { x: sx + r + 6, y: sy - boxH / 2, w: boxW, h: boxH };
        const bx = box.x;
        // A label running off the right edge slides under the Filter card.
        if (box.x + box.w > w - 8) continue;
        if (placed.some((p) => overlaps(box, p))) continue;
        if (discs.some((d) => d.id !== n.id && overlaps(box, d))) continue;
        placed.push(box);
        ctx.globalAlpha = n.id === this.hoverId || n.id === this.focusId
          ? 1 : Math.min(1, 0.75 * this.contrast);
        ctx.fillStyle = "#e8eaed";
        ctx.fillText(text, bx, sy + 4);
      }
      ctx.globalAlpha = 1;
    }
  }

  // -------------------------------------------------------------- input
  _resize() {
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = window.innerWidth * dpr;
    this.canvas.height = window.innerHeight * dpr;
    this.canvas.style.width = window.innerWidth + "px";
    this.canvas.style.height = window.innerHeight + "px";
  }

  _nodeAt(screenX, screenY) {
    const [wx, wy] = this._screenToWorld(screenX, screenY);
    let best = null, bestDist = Infinity;
    for (const n of this.nodes) {
      if (!this._visible(n)) continue;
      const dx = n.x - wx, dy = n.y - wy;
      const d = Math.sqrt(dx * dx + dy * dy);
      const hitR = n.radius + 8 / this.scale;
      if (d <= hitR && d < bestDist) { best = n; bestDist = d; }
    }
    return best;
  }

  _bindEvents() {
    const c = this.canvas;
    c.addEventListener("mousedown", (e) => {
      this.lastMouse = { x: e.clientX, y: e.clientY };
      this.dragMoved = false;
      const hit = this._nodeAt(e.clientX, e.clientY);
      if (hit) { this.dragNode = hit; this._userMoved = true; }
      else { this.isPanning = true; c.classList.add("dragging"); }
    });

    window.addEventListener("mousemove", (e) => {
      const dx = e.clientX - this.lastMouse.x, dy = e.clientY - this.lastMouse.y;
      if (Math.abs(dx) + Math.abs(dy) > 2) this.dragMoved = true;

      if (this.dragNode) {
        const [wx, wy] = this._screenToWorld(e.clientX, e.clientY);
        this.dragNode.x = wx; this.dragNode.y = wy;
        this.dragNode.vx = 0; this.dragNode.vy = 0;
      } else if (this.isPanning) {
        this._userMoved = true;
        this._camAnim = null;
        this.offsetX += dx; this.offsetY += dy;
      } else {
        const hit = this._nodeAt(e.clientX, e.clientY);
        const newHover = hit ? hit.id : null;
        if (newHover !== this.hoverId) {
          this.hoverId = newHover;
          if (this.onHover) this.onHover(hit || null);
        }
      }
      this.lastMouse = { x: e.clientX, y: e.clientY };
    });

    window.addEventListener("mouseup", (e) => {
      if (!this.dragMoved) {
        const hit = this._nodeAt(e.clientX, e.clientY);
        if (hit) {
          this.focusById(hit.id, { fromShiftClick: e.shiftKey });
        } else if (e.target === c) {
          // Clicking the void clears focus - otherwise the graph would stay
          // dimmed around a node you've stopped caring about.
          this.focusId = null;
          this.highlightedPath = null;
          if (this.onClearFocus) this.onClearFocus();
        }
      }
      this.dragNode = null;
      this.isPanning = false;
      c.classList.remove("dragging");
    });

    c.addEventListener("wheel", (e) => {
      e.preventDefault();
      this._userMoved = true;
      this._camAnim = null;
      const [wx, wy] = this._screenToWorld(e.clientX, e.clientY);
      const factor = Math.exp(-e.deltaY * 0.001);
      this.scale = Math.min(4, Math.max(0.15, this.scale * factor));
      const [sx, sy] = this._worldToScreen(wx, wy);
      this.offsetX += e.clientX - sx;
      this.offsetY += e.clientY - sy;
    }, { passive: false });
  }
}

window.Graph = Graph;
window.typeColor = typeColor;
