// Client data-flow map: draws the flow as SVG, animates it, answers questions about it,
// and refreshes itself when the engagement changes. Data comes from /engagements/N/dataflow.json.
(() => {
  const root = document.getElementById("flow-app");
  if (!root) return;
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.getElementById("flow-svg");
  const canvas = document.getElementById("flow-canvas");
  const panel = document.getElementById("flow-panel");
  const answer = document.getElementById("flow-answer");
  const queryBox = document.getElementById("flow-query");
  const liveText = document.getElementById("flow-live");
  const url = root.dataset.src;
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let flow = JSON.parse(document.getElementById("flow-data").textContent);
  let selected = null;
  let highlight = null; // {nodes:Set, edges:Set}
  let paused = reduceMotion;

  const LEVEL = { critical: "Critical", serious: "Serious", warning: "Needs attention", ok: "No issues found", pending: "Not assessed yet" };
  const W = 158, H = 58, GAP = 20, TOP = 46;

  const el = (name, attrs = {}, parent) => {
    const e = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.append(e);
    return e;
  };
  const h = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  };
  const cut = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

  function layout() {
    const cols = flow.stages.map(s => flow.nodes.filter(n => n.stage === s.key));
    const colW = Math.max(186, (canvas.clientWidth - 16) / flow.stages.length);
    const tallest = Math.max(...cols.map(c => c.length), 1);
    const height = TOP + tallest * (H + GAP) + 24;
    const pos = {};
    cols.forEach((col, i) => {
      const colH = col.length * (H + GAP) - GAP;
      col.forEach((n, j) => {
        pos[n.id] = { x: 8 + i * colW + (colW - W) / 2, y: TOP + (height - TOP - 24 - colH) / 2 + j * (H + GAP), col: i };
      });
    });
    // Lowest point of each column, so flows that skip a column can pass underneath it.
    const floor = cols.map(col => Math.max(0, ...col.map(n => pos[n.id].y + H)));
    return { pos, colW, floor, width: colW * flow.stages.length + 16, height: height + 34 };
  }

  let floors = [];
  function edgePath(a, b) {
    const y1 = a.y + H / 2, y2 = b.y + H / 2;
    if (b.col > a.col + 1) {
      // Skips a column: go under it instead of behind its boxes.
      const x1 = a.x + W, x2 = b.x - 6;
      const low = Math.max(...floors.slice(a.col + 1, b.col)) + 22;
      return `M${x1},${y1} C${x1 + 50},${y1} ${x1 + 30},${low} ${x1 + 90},${low} L${x2 - 90},${low} C${x2 - 30},${low} ${x2 - 50},${y2} ${x2},${y2}`;
    }
    if (b.col > a.col) {
      const x1 = a.x + W, x2 = b.x - 6, dx = (x2 - x1) / 2;
      return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
    }
    // Same column (e.g. core app -> AWS): loop out to the right.
    const x = a.x + W, bulge = 46;
    return `M${x},${y1} C${x + bulge},${y1} ${x + bulge},${y2} ${x + 6},${y2}`;
  }

  function draw() {
    const { pos, colW, floor, width, height } = layout();
    floors = floor;
    svg.replaceChildren();
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("width", width);
    svg.setAttribute("height", height);
    const defs = el("defs", {}, svg);
    for (const s of ["ok", "warning", "serious", "critical", "pending"]) {
      const m = el("marker", { id: `arrow-${s}`, viewBox: "0 0 10 10", refX: 8, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" }, defs);
      el("path", { d: "M0,0 L10,5 L0,10 z", class: `arrowhead status-${s}` }, m);
    }
    flow.stages.forEach((s, i) => {
      const t = el("text", { x: 8 + i * colW + colW / 2, y: 20, class: "stage-label", "text-anchor": "middle" }, svg);
      t.textContent = s.label.toUpperCase();
    });

    const edgeLayer = el("g", {}, svg);
    const dotLayer = el("g", { class: "dots" }, svg);
    flow.edges.forEach((e, idx) => {
      const a = pos[e.source], b = pos[e.target];
      if (!a || !b) return;
      const d = edgePath(a, b);
      const dim = highlight && !highlight.edges.has(idx);
      const g = el("g", { class: `edge status-${e.status}${dim ? " dim" : ""}${highlight && !dim ? " lit" : ""}` }, edgeLayer);
      el("path", { d, class: "edge-hit" }, g);
      el("path", { d, class: "edge-line", "marker-end": `url(#arrow-${e.status})` }, g);
      const tip = el("title", {}, g);
      tip.textContent = `${nodeName(e.source)} → ${nodeName(e.target)}${e.label ? ` (${e.label})` : ""}\n${e.categories.length ? "Data: " + e.categories.join(", ") : "No personal data listed"}`;
      if (!paused && !dim) {
        const count = e.status === "ok" || e.status === "pending" ? 2 : 3;
        for (let k = 0; k < count; k++) {
          const c = el("circle", { r: e.status === "critical" || e.status === "serious" ? 4 : 3.2, class: `dot status-${e.status}` }, dotLayer);
          el("animateMotion", { dur: "3.2s", repeatCount: "indefinite", begin: `${(-3.2 * k) / count}s`, path: d }, c);
        }
      }
    });

    flow.nodes.forEach(n => {
      const p = pos[n.id];
      const dim = highlight && !highlight.nodes.has(n.id);
      const g = el("g", { class: `node status-${n.status}${selected === n.id ? " selected" : ""}${dim ? " dim" : ""}`, transform: `translate(${p.x},${p.y})`, tabindex: 0, role: "button", "aria-label": `${n.name}: ${LEVEL[n.status]}${n.issues.length ? `, ${n.issues.length} issue(s)` : ""}` }, svg);
      el("rect", { width: W, height: H, rx: 10, class: "node-box" }, g);
      el("rect", { width: 5, height: H - 16, x: 0, y: 8, rx: 2, class: "node-stripe" }, g);
      const name = el("text", { x: 16, y: 25, class: "node-name" }, g);
      name.textContent = cut(n.name, 20);
      const sub = el("text", { x: 16, y: 44, class: "node-sub" }, g);
      sub.textContent = cut(`${flow.locations[n.location] || ""}${n.categories.length ? ` · ${n.categories.length} data type${n.categories.length > 1 ? "s" : ""}` : ""}`, 26);
      if (n.location === "outside") el("circle", { cx: W - 14, cy: H - 14, r: 4, class: "abroad-dot" }, g);
      if (n.issues.length) {
        el("circle", { cx: W - 4, cy: 4, r: 11, class: "node-badge" }, g);
        const t = el("text", { x: W - 4, y: 8, "text-anchor": "middle", class: "node-badge-text" }, g);
        t.textContent = n.issues.length;
      }
      const t = el("title", {}, g);
      t.textContent = `${n.name} — ${LEVEL[n.status]}`;
      const pick = () => select(n.id);
      g.addEventListener("click", pick);
      g.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); pick(); } });
    });
    if (paused) svg.pauseAnimations?.(); else svg.unpauseAnimations?.();
    svg.classList.toggle("paused", paused);
  }

  const nodeName = id => (flow.nodes.find(n => n.id === id) || { name: id }).name;

  function select(id) {
    selected = selected === id ? null : id;
    draw();
    showPanel();
    // When the panel sits below the map, bring it into view.
    const r = panel.getBoundingClientRect();
    if (selected && (r.top > innerHeight || r.bottom < 0)) panel.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
  }

  function showPanel() {
    panel.replaceChildren();
    const n = flow.nodes.find(x => x.id === selected);
    if (!n) {
      panel.append(h("p", "tile-label", "Step details"));
      panel.append(h("p", "muted", "Click any step in the map to see the data it handles, the problems found there and how to fix them."));
      const s = flow.summary;
      const worst = flow.plan[0];
      if (worst) {
        panel.append(h("p", "tile-label", "Most urgent"));
        panel.append(issueCard(worst));
      } else if (s.assessed) {
        panel.append(h("p", "flash flash-info small", "No issues found in this flow."));
      }
      return;
    }
    panel.append(h("p", "tile-label", flow.stages.find(s => s.key === n.stage).label));
    panel.append(h("h3", "panel-title", n.name));
    const meta = h("p", "panel-meta");
    meta.append(h("span", `badge flow-badge status-${n.status}`, LEVEL[n.status]));
    meta.append(h("span", `badge loc-${n.location}`, flow.locations[n.location]));
    panel.append(meta);
    if (n.note) panel.append(h("p", "small text-2", n.note));
    if (n.categories.length) {
      panel.append(h("p", "tile-label", "Personal data here"));
      const chips = h("div", "chips");
      n.categories.forEach(c => chips.append(h("span", "chip", c)));
      panel.append(chips);
    }
    const ins = flow.edges.filter(e => e.target === n.id), outs = flow.edges.filter(e => e.source === n.id);
    if (ins.length || outs.length) {
      panel.append(h("p", "tile-label", "Flows"));
      const ul = h("ul", "flow-list");
      ins.forEach(e => ul.append(h("li", "", `← from ${nodeName(e.source)}${e.label ? ` (${e.label})` : ""}`)));
      outs.forEach(e => ul.append(h("li", "", `→ to ${nodeName(e.target)}${e.label ? ` (${e.label})` : ""}`)));
      panel.append(ul);
    }
    panel.append(h("p", "tile-label", n.issues.length ? `Issues and fixes (${n.issues.length})` : "Issues"));
    if (!n.issues.length) panel.append(h("p", "muted small", n.status === "pending" ? "Run the assessment to check this step." : "Nothing found here."));
    n.issues.forEach(i => panel.append(issueCard(i)));
    if (n.custom_id) {
      const f = document.getElementById("remove-template").content.cloneNode(true).querySelector("form");
      f.action = f.action.replace("NODE", n.custom_id);
      panel.append(f);
    }
  }

  function issueCard(i) {
    const c = h("div", `issue level-${i.level}`);
    const head = h("p", "issue-head");
    head.append(h("span", `badge flow-badge status-${i.level}`, LEVEL[i.level]));
    if (i.provision) head.append(h("span", "muted small", i.provision));
    c.append(head);
    c.append(h("strong", "", i.title));
    if (i.detail) c.append(h("p", "small text-2", i.detail));
    const fix = h("p", "small next-step");
    fix.append(h("strong", "", "Fix: "), document.createTextNode(i.action));
    c.append(fix);
    const a = h("a", "small", i.source === "finding" ? "Open the finding →" : "Open connector evidence →");
    a.href = i.link;
    c.append(a);
    return c;
  }

  // ---- Questions about the flow
  const STOP = new Set("where what which who does do is are the our my to of in go goes going data flow flows show me all any how with from for get gets and personal info information".split(" "));
  function ask(q) {
    const text = q.trim().toLowerCase();
    if (!text) { highlight = null; answer.hidden = true; draw(); return; }
    const nodes = new Set(), edges = new Set();
    const hitNode = pred => flow.nodes.forEach(n => pred(n) && nodes.add(n.id));
    const withEdgesInto = () => flow.edges.forEach((e, i) => nodes.has(e.target) && (edges.add(i), nodes.add(e.source)));
    let msg;
    if (/outside|abroad|foreign|leave|cross.?border|transfer|overseas|international/.test(text)) {
      hitNode(n => n.location === "outside" || n.id === "abroad");
      withEdgesInto();
      const out = flow.edges.filter(e => e.target === "abroad").map(e => nodeName(e.source));
      msg = out.length ? `Data leaves India through ${out.length} path${out.length > 1 ? "s" : ""}: ${out.join(", ")}.` : "No flow out of India has been identified.";
    } else if (/issue|gap|risk|problem|fail|fix|wrong|mitigat|plan/.test(text)) {
      hitNode(n => n.issues.length > 0);
      flow.edges.forEach((e, i) => nodes.has(e.target) && edges.add(i));
      msg = nodes.size ? `${nodes.size} step${nodes.size > 1 ? "s have" : " has"} issues: ${[...nodes].map(nodeName).join(", ")}. The mitigation plan below lists the fixes in order.` : "No issues found in this flow.";
    } else if (/vendor|processor|third|compan|share|partner|supplier/.test(text)) {
      hitNode(n => n.stage === "vendors");
      withEdgesInto();
      msg = `${flow.nodes.filter(n => n.stage === "vendors").length} outside compan${flow.nodes.filter(n => n.stage === "vendors").length === 1 ? "y receives" : "ies receive"} personal data.`;
    } else if (/child|minor|kid|parent/.test(text)) {
      hitNode(n => n.id === "children" || n.id === "collect");
      flow.edges.forEach((e, i) => e.source === "children" && edges.add(i));
      msg = nodes.has("children") ? "Children's data comes in through the collection points and needs verifiable parental consent." : "The client says no users are under 18.";
    } else if (/delet|retention|retain|erase|keep|kept|how long/.test(text)) {
      hitNode(n => n.id === "deletion" || n.id === "core");
      flow.edges.forEach((e, i) => e.target === "deletion" && edges.add(i));
      msg = nodeByid("deletion")?.note || "Retention not described yet.";
    } else if (/right|request|access|correct|grievance|complain|erasure|dsar/.test(text)) {
      hitNode(n => n.id === "rights" || n.id === "customers" || n.id === "core");
      flow.edges.forEach((e, i) => (e.source === "rights" || e.target === "rights") && edges.add(i));
      msg = "Rights requests come in from people and must be handled in the core systems.";
    } else {
      const words = text.split(/[^a-z0-9]+/).filter(w => w.length > 2 && !STOP.has(w));
      const match = s => words.some(w => s.toLowerCase().includes(w) || w.includes(s.toLowerCase().replace(/s$/, "")));
      flow.edges.forEach((e, i) => { if (e.categories.some(match)) { edges.add(i); nodes.add(e.source); nodes.add(e.target); } });
      hitNode(n => match(n.name));
      flow.edges.forEach((e, i) => nodes.has(e.source) && nodes.has(e.target) && edges.add(i));
      const cats = flow.categories.filter(match);
      msg = cats.length
        ? `${cats.join(", ")} move${cats.length === 1 && !cats[0].endsWith("s") ? "s" : ""} through ${nodes.size} steps: ${[...nodes].map(nodeName).join(" → ")}.`
        : nodes.size ? `Found: ${[...nodes].map(nodeName).join(", ")}.` : `Nothing in this flow matches "${q.trim()}". Try a data type (${flow.categories.slice(0, 3).join(", ") || "email"}), "outside India", "vendors" or "issues".`;
    }
    highlight = nodes.size || edges.size ? { nodes, edges } : null;
    answer.textContent = msg;
    answer.hidden = false;
    draw();
  }
  const nodeByid = id => flow.nodes.find(n => n.id === id);

  document.getElementById("flow-ask").addEventListener("submit", e => { e.preventDefault(); ask(queryBox.value); });
  document.querySelectorAll("[data-ask]").forEach(b => b.addEventListener("click", () => { queryBox.value = b.dataset.ask; ask(b.dataset.ask); }));
  document.getElementById("flow-clear").addEventListener("click", () => { queryBox.value = ""; ask(""); });
  const pauseBtn = document.getElementById("flow-pause");
  pauseBtn.setAttribute("aria-pressed", paused);
  pauseBtn.addEventListener("click", () => { paused = !paused; pauseBtn.setAttribute("aria-pressed", paused); pauseBtn.textContent = paused ? "Play animation" : "Pause animation"; draw(); });
  pauseBtn.textContent = paused ? "Play animation" : "Pause animation";

  // ---- Live updates
  function renderSummary() {
    const s = flow.summary;
    document.getElementById("sum-systems").textContent = s.systems;
    document.getElementById("sum-flows").textContent = s.flows;
    const total = s.issues.critical + s.issues.serious + s.issues.warning;
    document.getElementById("sum-issues").textContent = total;
    document.getElementById("sum-issues-detail").textContent = total ? `${s.issues.critical} critical · ${s.issues.serious} serious · ${s.issues.warning} to check` : (s.assessed ? "None found" : "Not assessed yet");
    document.getElementById("sum-abroad").textContent = s.leaves_india ? "Yes" : "No";
    document.getElementById("sum-abroad-tile").classList.toggle("is-warn", s.leaves_india);
    const body = document.getElementById("plan-body");
    body.replaceChildren();
    flow.plan.forEach((p, i) => {
      const tr = h("tr", `level-${p.level}`);
      tr.append(h("td", "num", i + 1));
      const lv = h("td"); lv.append(h("span", `badge flow-badge status-${p.level}`, LEVEL[p.level])); tr.append(lv);
      tr.append(h("td", "", p.where.join(", ")));
      const is = h("td"); is.append(h("strong", "", p.title)); if (p.detail) is.append(h("p", "muted small", p.detail)); tr.append(is);
      tr.append(h("td", "", p.action));
      const pr = h("td", "nowrap small"); const a = h("a", "", p.provision || "Open"); a.href = p.link; pr.append(a); tr.append(pr);
      body.append(tr);
    });
    document.getElementById("plan-empty").hidden = flow.plan.length > 0;
    document.getElementById("plan-table").hidden = flow.plan.length === 0;
  }

  let lastCheck = new Date();
  function tick() {
    const secs = Math.round((new Date() - lastCheck) / 1000);
    liveText.textContent = secs < 5 ? "Live · checked just now" : `Live · checked ${secs}s ago`;
  }
  async function refresh() {
    try {
      const r = await fetch(url, { headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error(r.status);
      const next = await r.json();
      lastCheck = new Date();
      root.classList.remove("offline");
      if (next.version !== flow.version) {
        flow = next;
        if (selected && !flow.nodes.some(n => n.id === selected)) selected = null;
        draw(); showPanel(); renderSummary();
        if (queryBox.value) ask(queryBox.value);
        root.classList.add("changed");
        setTimeout(() => root.classList.remove("changed"), 1600);
      }
    } catch (e) {
      root.classList.add("offline");
      liveText.textContent = "Reconnecting…";
    }
  }
  let resizeTimer;
  addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(draw, 150); });
  draw(); showPanel(); renderSummary();
  setInterval(refresh, 10000);
  setInterval(tick, 1000);
  tick();
})();
