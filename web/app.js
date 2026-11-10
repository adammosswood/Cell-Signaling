"use strict";
const {graph, registry, history: revisions} = JSON.parse(document.getElementById("atlas-data").textContent);
const $ = id => document.getElementById(id);
const nodes = new Map(graph.nodes.map(node => [node.id, node]));
const papers = new Map(graph.papers.map(paper => [paper.id, paper]));
const receptors = new Map(registry.receptors.map(record => [record.id, record]));
const ordered = [...graph.nodes.map(node => ({...node, ...receptors.get(node.id)})),
  ...registry.receptors.filter(record => !nodes.has(record.id))];
const descriptions = {
  demonstrated: "Demonstrated step", assembled: "Across studies", hypothesis: "Hypothesis",
  reviewed: "Curated", pending: "Research pending", no_qualifying_evidence: "No qualifying evidence found",
  no_effect: "No effect in the reported system",
};
const arrows = {activation: "→", inhibition: "⊣", recruitment: "⇢", production: "→", depletion: "⊣", feedback: "↺"};
let selected = "gq";
let selectedEdge = null;
let visible = [];
let notice = null;

function element(tag, value, parent) {
  const node = document.createElement(tag);
  if (value) node.textContent = value;
  if (parent) parent.append(node);
  return node;
}
function option(select, value, title) {
  const item = element("option", title, select);
  item.value = value;
}
function linked(parent, label, url) {
  const link = element("a", label, parent);
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}
function evidence(parent, items) {
  if (!items.length) return;
  const section = element("details", null, parent);
  element("summary", `Primary evidence (${items.length})`, section);
  const list = element("ul", null, section);
  for (const item of items) {
    const paper = papers.get(item.paper);
    const row = element("li", null, list);
    linked(row, `${paper.title} (${paper.year})`, paper.url);
    if (paper.status !== "published") row.append(` · ${paper.status}`);
    element("p", `${item.locator} · ${item.context}`, row);
    facts(row, [["Perturbation", item.perturbation], ["Readout", item.readout], ["Finding", item.finding]]);
  }
}
function facts(parent, items) {
  const list = element("dl", null, parent);
  for (const [label, value] of items) {
    if (value) {
      element("dt", label, list);
      element("dd", value, list);
    }
  }
}
function label(id) {
  return nodes.get(id)?.label || receptors.get(id)?.label || id;
}
function edgeTitle(edge) {
  return `${label(edge.source)} ${arrows[edge.effect]} ${label(edge.target)}`;
}
function inForce(record) {
  return record.evidence.some(item => !["retracted", "concern", "superseded", "notice"].includes(papers.get(item.paper).status));
}
function selectable(parent, value, pressed, onClick) {
  const button = element("button", value, parent);
  button.type = "button";
  button.setAttribute("aria-pressed", String(pressed));
  button.addEventListener("click", onClick);
  return button;
}
function edgeDetail(parent, edge) {
  const card = element("article", null, parent);
  element("strong", edgeTitle(edge), card);
  element("p", `${descriptions[edge.tier]} · ${edge.effect} · ${edge.status}`, card).className = "muted";
  element("p", edge.summary, card);
  facts(card, [["Status rationale", edge.reason], ["Assumption", edge.assumption], ["Missing link", edge.missing_link], ["Discriminating test", edge.test]]);
  evidence(card, edge.evidence);
}
function drawDetails() {
  const detail = $("details");
  detail.replaceChildren();
  const node = nodes.get(selected);
  const receptor = receptors.get(selected);
  element("h2", selectedEdge ? "Connection evidence" : label(selected), detail).id = "detail-title";
  if (selectedEdge) {
    edgeDetail(detail, selectedEdge);
    return;
  }
  if (receptor) {
    element("p", `${receptor.gene} · ${receptor.family} · ${receptor.subfamily}`, detail).className = "muted";
    const source = registry.sources.find(record => record.name === receptor.source);
    linked(detail, source.name, receptor.url || source.url);
  }
  if (node) element("p", node.summary, detail);
  else {
    element("p", "This receptor is indexed. Receptor-specific signaling curation is pending.", detail);
    return;
  }
  const assays = graph.assays.filter(item => item.node === selected && inForce(item)
      && (!$("modality").value || item.modality === $("modality").value));
  element("h3", "How to measure this junction", detail);
  if (!assays.length) element("p", "No measurement has been curated for this junction under the current filter.", detail);
  for (const item of assays) {
    const card = element("article", null, detail);
    element("strong", `${item.label} · ${item.modality}`, card);
    facts(card, [["Measures", item.measures], ["Validated in", item.validation], ["Controls", item.controls], ["Interpretation", item.limit]]);
    evidence(card, item.evidence);
  }
  const responses = graph.responses.filter(item => item.node === selected && inForce(item));
  element("h3", "Reported responses", detail);
  if (!responses.length) element("p", "Response curation is pending for this junction.", detail);
  for (const item of responses) {
    const card = element("article", null, detail);
    element("strong", item.level === "cellular" ? "Cellular" : "Physiological", card);
    element("p", item.summary, card);
    evidence(card, item.evidence);
  }
  const related = graph.edges.filter(edge => edge.status !== "retired" && (edge.source === selected || edge.target === selected));
  if (related.length) element("h3", "Connected evidence", detail);
  for (const edge of related) edgeDetail(detail, edge);
}

function routes(start) {
  const tier = $("tier").value;
  const compartment = $("compartment").value;
  const modality = $("modality").value;
  const edges = graph.edges.filter(edge => edge.status !== "retired");
  const depth = new Map([[start, 0]]);
  const reachable = [];
  const queue = [start];
  for (let i = 0; i < queue.length; i++) {
    for (const edge of edges.filter(item => item.source === queue[i])) {
      reachable.push(edge);
      if (!depth.has(edge.target)) {
        depth.set(edge.target, depth.get(edge.source) + 1);
        queue.push(edge.target);
      }
    }
  }
  const measured = new Set(graph.assays.filter(item => item.modality === modality && inForce(item)).map(item => item.node));
  const matching = reachable.filter(edge => (!tier || edge.tier === tier) &&
    (!compartment || nodes.get(edge.target).compartment === compartment) &&
    (!modality || measured.has(edge.target)));
  const show = new Set(matching.map(edge => edge.id));
  if (tier || compartment || modality) {
    const trace = [...new Set(matching.map(edge => edge.source))];
    const visited = new Set(trace);
    for (let i = 0; i < trace.length; i++) {
      for (const edge of reachable.filter(item => item.target === trace[i] && depth.get(item.source) < depth.get(item.target))) {
        show.add(edge.id);
        if (!visited.has(edge.source)) {
          visited.add(edge.source);
          trace.push(edge.source);
        }
      }
    }
  }
  return {depth, matching, edges: reachable.filter(edge => show.has(edge.id))};
}
function drawWires() {
  const svg = $("wires");
  svg.replaceChildren();
  const ns = "http://www.w3.org/2000/svg";
  const defs = document.createElementNS(ns, "defs");
  for (const [id, shape] of [["arrow", "M 0 0 L 7 3.5 L 0 7 z"], ["stop", "M 6 0 L 6 7"]]) {
    const marker = document.createElementNS(ns, "marker");
    for (const [key, value] of Object.entries({id, markerWidth: 7, markerHeight: 7, refX: 6, refY: 3.5, orient: "auto"})) marker.setAttribute(key, value);
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", shape);
    path.setAttribute("stroke", "context-stroke");
    path.setAttribute("fill", id === "arrow" ? "context-stroke" : "none");
    marker.append(path);
    defs.append(marker);
  }
  svg.append(defs);
  const bounds = svg.getBoundingClientRect();
  const mobile = matchMedia("(max-width: 620px)").matches;
  for (const edge of visible) {
    const a = $("node-" + edge.source)?.getBoundingClientRect();
    const b = $("node-" + edge.target)?.getBoundingClientRect();
    if (!a || !b) continue;
    const x1 = (mobile ? a.left : a.right) - bounds.left;
    const x2 = b.left - bounds.left;
    const y1 = a.top + a.height / 2 - bounds.top;
    const y2 = b.top + b.height / 2 - bounds.top;
    const bend = (x1 + x2) / 2;
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", mobile ? `M ${x1} ${y1} H 6 V ${y2} H ${x2}` : `M ${x1} ${y1} C ${bend} ${y1} ${bend} ${y2} ${x2} ${y2}`);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", edge.tier === "hypothesis" ? "var(--hyp)" : "currentColor");
    path.setAttribute("stroke-width", "1.35");
    if (edge.tier !== "demonstrated") path.setAttribute("stroke-dasharray", edge.tier === "assembled" ? "5 3" : "2 3");
    path.setAttribute("marker-end", edge.effect === "inhibition" || edge.effect === "depletion" ? "url(#stop)" : "url(#arrow)");
    svg.append(path);
  }
}
function drawGraph() {
  const start = $("start").value;
  const levels = $("levels");
  const routeList = $("route-details");
  levels.replaceChildren();
  routeList.replaceChildren();
  if (!start) {
    visible = [];
    element("p", "Choose a starting point or try a broader search.", levels);
    drawWires();
    return;
  }
  const {depth, edges, matching} = routes(start);
  visible = edges;
  const paths = new Set([start, ...edges.flatMap(edge => [edge.source, edge.target])]);
  const grouped = new Map();
  for (const [id, step] of depth) {
    if (!paths.has(id)) continue;
    if (!grouped.has(step)) grouped.set(step, []);
    grouped.get(step).push(id);
  }
  const available = $("diagram").clientWidth;
  const columns = Math.min(grouped.size, Math.max(1, Math.floor(available / 95)));
  levels.style.gridTemplateColumns = `repeat(${columns}, minmax(0, 1fr))`;
  for (const group of grouped.values()) {
    const column = element("div", null, levels);
    column.className = "level";
    for (const id of group) {
      const button = selectable(column, label(id), id === selected && !selectedEdge, () => {
        selected = id;
        selectedEdge = null;
        drawDetails();
        syncPressed();
      });
      button.className = "node";
      button.id = "node-" + id;
      element("small", nodes.get(id)?.kind || "receptor", button);
    }
  }
  if (matching.length) element("h3", "Visible connections", routeList);
  else element("p", "No curated downstream connections match this selection.", routeList).className = "muted";
  for (const edge of edges) {
    const button = selectable(routeList, `${edgeTitle(edge)} · ${descriptions[edge.tier]}${edge.status === "contested" ? " · contested" : ""}`,
      selectedEdge?.id === edge.id, () => {
        selectedEdge = edge;
        drawDetails();
        syncPressed();
      });
    button.dataset.edge = edge.id;
  }
  requestAnimationFrame(drawWires);
}
function syncPressed() {
  for (const button of $("levels").querySelectorAll("button")) button.setAttribute("aria-pressed", String(!selectedEdge && button.id === "node-" + selected));
  for (const button of $("route-details").querySelectorAll("button")) button.setAttribute("aria-pressed", String(button.dataset.edge === selectedEdge?.id));
}
function refreshOptions() {
  const select = $("start");
  const previous = select.value || selected;
  const terms = $("search").value.toLowerCase().trim().split(/\s+/);
  select.replaceChildren();
  const results = ordered.filter(row => terms.every(term => [row.id, row.label, row.gene, row.family, row.subfamily, ...(row.aliases || [])]
    .filter(Boolean).join(" ").toLowerCase().includes(term)));
  if (!results.length) option(select, "", "No matches");
  for (const row of results) option(select, row.id, row.gene ? `${row.label} · ${row.gene}` : row.label);
  if (results.some(row => row.id === previous)) select.value = previous;
  selected = select.value || selected;
  selectedEdge = null;
  $("matches").textContent = `${results.length} matching starting points · ${registry.receptors.length} receptors indexed · ${graph.nodes.length} junctions curated`;
  drawGraph();
  if (select.value) drawDetails();
  else {
    $("details").replaceChildren();
    element("h2", "No matching junction", $("details")).id = "detail-title";
  }
}
function drawHistory() {
  const parent = $("history");
  const coverage = element("details", null, parent);
  element("summary", "Curation coverage and receptor sources", coverage);
  const list = element("ul", null, coverage);
  for (const item of graph.coverage) {
    const row = element("li", `${item.scope} · ${descriptions[item.status]}. ${item.summary}`, list);
    evidence(row, item.evidence);
  }
  for (const note of registry.notes) element("p", note, coverage);
  for (const source of registry.sources) {
    const line = element("p", null, coverage);
    linked(line, source.name, source.url);
    line.append(` · ${source.release}`);
  }
  const log = element("details", null, parent);
  element("summary", `Merged literature updates (${revisions.length})`, log);
  for (const entry of revisions.slice().reverse()) {
    const row = element("article", null, log);
    element("strong", entry.published, row);
    element("p", entry.summary, row);
    element("p", entry.review, row).className = "muted";
  }
  const retired = graph.edges.filter(edge => edge.status === "retired");
  if (retired.length) {
    const archive = element("details", null, parent);
    element("summary", `Retired connections (${retired.length})`, archive);
    for (const edge of retired) edgeDetail(archive, edge);
  }
}
for (const value of [...new Set(graph.nodes.map(node => node.compartment))].sort()) option($("compartment"), value, value);
for (const value of [...new Set(graph.assays.map(assay => assay.modality))].sort()) option($("modality"), value, value);
$("filters").addEventListener("submit", event => event.preventDefault());
$("search").addEventListener("input", refreshOptions);
$("start").addEventListener("change", () => {
  selected = $("start").value;
  selectedEdge = null;
  drawGraph();
  drawDetails();
});
for (const id of ["tier", "compartment", "modality"]) $(id).addEventListener("change", () => {
  selectedEdge = null;
  drawGraph();
  if ($("start").value) drawDetails();
});
window.addEventListener("resize", () => {
  clearTimeout(notice);
  notice = setTimeout(drawGraph, 100);
});
refreshOptions();
drawHistory();
