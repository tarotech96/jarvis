"use strict";
/*
 * app.js - wires JARVIS together: graph, panels, transcript, hands-free voice.
 *
 * The voice state machine itself lives in voice.js. This file's job is to
 * turn its states into something a human can read at a glance (the HUD, the
 * pill, the dot), and to run one turn of conversation when it asks for one.
 */

const canvas = document.getElementById("graph-canvas");
const graph = new Graph(canvas);

const els = {
  brandDot: document.getElementById("brand-dot"),
  inspector: document.getElementById("inspector"),
  hubsList: document.getElementById("hubs-list"),
  filtersList: document.getElementById("filters-list"),
  filtersLabel: document.getElementById("filters-label"),

  btnFit: document.getElementById("btn-fit"),
  btnLabels: document.getElementById("btn-labels"),
  btnContrast: document.getElementById("btn-contrast"),
  btnSources: document.getElementById("btn-sources"),
  btnConnectors: document.getElementById("btn-connectors"),
  connectorsPanel: document.getElementById("connectors-panel"),
  connectorList: document.getElementById("connector-list"),
  connectorsNote: document.getElementById("connectors-note"),
  sourcesPanel: document.getElementById("sources-panel"),
  sourcesMachine: document.getElementById("sources-machine"),
  sourcesList: document.getElementById("sources-list"),
  sourcesNote: document.getElementById("sources-note"),
  btnSourcesApply: document.getElementById("btn-sources-apply"),

  reactor: document.getElementById("reactor"),
  reactorLabel: document.getElementById("reactor-state-label"),
  reactorHint: document.getElementById("reactor-hint"),
  liveDot: document.getElementById("live-dot"),
  modelChip: document.getElementById("model-chip"),
  hudTicks: document.getElementById("hud-ticks"),
  hudLevel: document.getElementById("hud-level"),

  livePill: document.getElementById("live-pill"),
  livePillText: document.getElementById("live-pill-text"),
  transcript: document.getElementById("transcript"),
  askInput: document.getElementById("ask-input"),
  askExample: document.getElementById("ask-example"),
  askCaption: document.getElementById("ask-caption"),
  statusBanner: document.getElementById("status-banner"),

  btnMic: document.getElementById("btn-mic"),
  btnMute: document.getElementById("btn-mute"),
  btnBrief: document.getElementById("btn-brief"),
  btnPlan: document.getElementById("btn-plan"),
  btnMemory: document.getElementById("btn-memory"),
};

const MAX_HISTORY = 10;
const MAX_TURNS_ON_SCREEN = 30;
const MODE_KEY = "jarvis.voiceMode";     // remembered across reloads
const SOURCES_KEY = "jarvis.sources";    // per-machine, see loadSources()
let history = [];
let voiceOutAvailable = true;

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function showStatusBanner(text) {
  els.statusBanner.textContent = text;
  els.statusBanner.hidden = false;
  clearTimeout(showStatusBanner._t);
  showStatusBanner._t = setTimeout(() => { els.statusBanner.hidden = true; }, 9000);
}

// ============================================================ example line

// Fixed suggestions go stale the moment the vault changes, so these are
// built from notes that are actually indexed.
const BASE_PROMPTS = ["Brief me", "What's on my calendar?", "What's in my inbox?"];
let EXAMPLE_PROMPTS = BASE_PROMPTS.slice();

function setExamplesFromGraph(nodes) {
  const hubs = [...nodes].sort((a, b) => b.degree - a.degree).slice(0, 4);
  const prompts = BASE_PROMPTS.slice();
  if (hubs[0]) prompts.push(`What's in ${hubs[0].title}?`);
  if (hubs[1] && hubs[2]) {
    prompts.push(`What connects ${hubs[1].title} and ${hubs[2].title}?`);
  }
  if (hubs[3]) prompts.push(`Search my notes for ${hubs[3].title}`);
  EXAMPLE_PROMPTS = prompts;
  exampleIdx = 0;
  rotateExample();
}

let exampleIdx = 0;
function rotateExample() {
  els.askExample.textContent = "Try: “" + EXAMPLE_PROMPTS[exampleIdx] + "”";
  exampleIdx = (exampleIdx + 1) % EXAMPLE_PROMPTS.length;
}
rotateExample();
setInterval(rotateExample, 6000);

// ================================================================ HUD ring

/*
 * The tick ring is drawn here rather than in markup: 60 <line> elements is
 * noise in the HTML, and generating them keeps the geometry in one place.
 */
(function buildHudTicks() {
  const CX = 110, CY = 110, COUNT = 60;
  const frag = document.createDocumentFragment();
  for (let i = 0; i < COUNT; i++) {
    const a = (i / COUNT) * Math.PI * 2 - Math.PI / 2;
    const major = i % 5 === 0;
    const r1 = major ? 90 : 94, r2 = 100;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", (CX + Math.cos(a) * r1).toFixed(2));
    line.setAttribute("y1", (CY + Math.sin(a) * r1).toFixed(2));
    line.setAttribute("x2", (CX + Math.cos(a) * r2).toFixed(2));
    line.setAttribute("y2", (CY + Math.sin(a) * r2).toFixed(2));
    line.setAttribute("opacity", major ? "1" : "0.42");
    frag.appendChild(line);
  }
  els.hudTicks.appendChild(frag);
})();

const HUD_SWEEP_C = 2 * Math.PI * 86;
const HUD_LEVEL_C = 2 * Math.PI * 58;
document.getElementById("hud-sweep")
  .setAttribute("stroke-dasharray", `${(HUD_SWEEP_C * 0.22).toFixed(1)} ${HUD_SWEEP_C}`);
els.hudLevel.setAttribute("stroke-dasharray", `0 ${HUD_LEVEL_C}`);

function setHudLevel(level) {
  const arc = Math.max(0.02, Math.min(1, level)) * HUD_LEVEL_C;
  els.hudLevel.setAttribute("stroke-dasharray", `${arc.toFixed(1)} ${HUD_LEVEL_C}`);
}

// ============================================================== inspector

/*
 * Markdown gets rendered; .txt and extracted .pdf text stay verbatim,
 * because in those a "#" is a hash and a "*" is an asterisk.
 */
function isMarkdown(path) {
  return /\.(md|markdown|mdx)$/i.test(path || "");
}

/*
 * A README's first H1 is where the note's title came from, so rendering
 * both puts the same words on screen twice.
 */
function dropLeadingTitle(source, title) {
  const lines = source.split("\n");
  const first = lines.findIndex((l) => l.trim());
  if (first < 0) return source;
  const heading = lines[first].match(/^#\s+(.*)$/);
  if (heading && heading[1].trim() === (title || "").trim()) {
    return lines.slice(first + 1).join("\n");
  }
  return source;
}

function renderInspector(note) {
  const raw = note.content || note.excerpt || "";
  const body = isMarkdown(note.path)
    ? `<div class="inspector-body md">${renderMarkdown(dropLeadingTitle(raw, note.title))}</div>`
    : `<div class="inspector-body">${escapeHtml(raw)}</div>`;
  els.inspector.innerHTML = `
    <div class="card-label">File</div>
    <div class="inspector-title">${escapeHtml(note.title)}</div>
    <div class="inspector-type">${escapeHtml(note.repo || note.type)}</div>
    ${body}
    <div class="inspector-path">${escapeHtml(note.path || "")}</div>
  `;
}

function renderInspectorEmpty() {
  els.inspector.innerHTML = `
    <div class="card-label">File</div>
    <div class="inspector-empty">
      Click a node to focus it — only that node and its connections light up,
      and you can read its note here. Shift-click a second node to trace the
      path between them.
    </div>
  `;
}

async function openNote(id) {
  try {
    const res = await fetch(`/api/note?id=${encodeURIComponent(id)}`);
    if (!res.ok) return;
    renderInspector(await res.json());
  } catch (e) {
    // non-fatal; leave the inspector showing whatever it had
  }
}

graph.onFocusNote = (node) => openNote(node.id);
graph.onClearFocus = () => renderInspectorEmpty();
graph.onHover = (node) => { if (node) els.askCaption.textContent = node.title; };

// =================================================================== hubs

function renderHubs(nodes) {
  const top = [...nodes].sort((a, b) => b.degree - a.degree).slice(0, 8);
  els.hubsList.innerHTML = "";
  for (const n of top) {
    const li = document.createElement("li");
    li.className = "hub-item";
    li.innerHTML = `<span class="hub-dot" style="background:${typeColor(n.type)}"></span>
      <span class="hub-title">${escapeHtml(n.title)}</span>
      <span class="hub-degree">${n.degree}</span>`;
    li.addEventListener("click", () => { graph.focusById(n.id); openNote(n.id); });
    els.hubsList.appendChild(li);
  }
}

/* Fallback only: a repo that names itself in its README wins over this. */
function humanizeType(type) {
  return type.replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
}

function renderFilters(nodes, types) {
  const counts = {};
  for (const n of nodes) counts[n.type] = (counts[n.type] || 0) + 1;

  // "Projects" is right when the buckets are git repositories, which is what
  // a folder of checkouts gives you. Point the vault at a plain notes folder
  // and they're just folders, so say so rather than inventing projects.
  const meta = Object.values(types || {});
  const repoish = meta.filter((t) => t.repo).length > meta.length / 2;
  els.filtersLabel.textContent = meta.length && repoish ? "Projects" : "Folders";
  els.filtersList.innerHTML = "";
  for (const type of Object.keys(counts).sort((a, b) => counts[b] - counts[a])) {
    const row = document.createElement("div");
    row.className = "filter-row";
    row.innerHTML = `<span class="filter-dot" style="background:${typeColor(type)}"></span>
      <span class="filter-name">${escapeHtml(
        (types && types[type] && types[type].label) || humanizeType(type)
      )}</span>
      <span class="filter-count">${counts[type]}</span>`;
    row.addEventListener("click", () => {
      const nowHidden = !row.classList.contains("disabled");
      row.classList.toggle("disabled", nowHidden);
      graph.setTypeVisible(type, !nowHidden);
    });
    els.filtersList.appendChild(row);
  }
}

// ================================================================ loading

async function loadGraph() {
  try {
    const res = await fetch("/api/graph");
    if (!res.ok) throw new Error("bad response");
    const data = await res.json();
    graph.load(data);   // assigns the categorical colours
    renderHubs(data.nodes);
    renderFilters(data.nodes, data.types);
    setExamplesFromGraph(data.nodes);
  } catch (e) {
    showStatusBanner("Couldn't load the vault graph - is agent/main.py running?");
  }
}

async function checkStatus() {
  try {
    const res = await fetch("/api/status");
    const status = await res.json();

    if (status.model) {
      els.modelChip.textContent = status.model_id;
      els.modelChip.classList.remove("is-warn");
    } else {
      els.modelChip.textContent = "keyword mode";
      els.modelChip.classList.add("is-warn");
      els.modelChip.title = status.note || "";
    }

    voiceOutAvailable = !!status.transcriber;
    if (!voiceOutAvailable) {
      els.btnMic.title = "No ElevenLabs key configured - voice is off";
      showStatusBanner("No ElevenLabs key configured - voice is off. Text still works.");
    }
  } catch (e) {
    showStatusBanner("Can't reach JARVIS's server at all - is agent/main.py running?");
  }
}

// ============================================================== sources

/*
 * Which folders JARVIS reads. The default comes from the machine itself
 * (agent/data.py picks ~/Documents/Works on a Mac, D:\Works on Windows), so
 * the common case needs no clicking at all - this panel is for narrowing it.
 *
 * The choice is kept in localStorage rather than a file on disk: guardrail 2
 * says memory/ is the only thing JARVIS writes to. The consequence is that
 * it's per-browser, and is re-sent to the server on every load.
 */
let sources = null;
let sourcesSelection = new Set();

function readSavedSources() {
  try {
    const raw = localStorage.getItem(SOURCES_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (e) { return null; }
}
function saveSources(list) {
  try { localStorage.setItem(SOURCES_KEY, JSON.stringify(list)); } catch (e) {}
}

function sameSet(a, b) {
  return a.length === b.length && [...a].sort().join("|") === [...b].sort().join("|");
}

async function postSources(folders) {
  const res = await fetch("/api/sources", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ folders }),
  });
  const result = await res.json();
  if (!res.ok) throw new Error(result.error || "couldn't switch folders");
  return result;
}

function renderSources() {
  if (!sources) return;
  els.sourcesMachine.innerHTML =
    `${sources.platform} · <strong>${escapeHtml(sources.root)}</strong>`
    + (sources.root_exists ? ""
       : ` <span class="is-missing">— this folder doesn't exist on this machine</span>`);

  els.sourcesList.innerHTML = "";
  for (const c of sources.candidates) {
    const row = document.createElement("div");
    row.className = "source-row" + (c.is_root ? " source-row-root" : "")
      + (sourcesSelection.has(c.path) ? " is-on" : "");
    row.innerHTML = `<span class="tick">${sourcesSelection.has(c.path) ? "✓" : ""}</span>
      <span>${escapeHtml(c.is_root ? "Everything under this folder" : c.name)}</span>`;
    row.addEventListener("click", () => {
      if (sourcesSelection.has(c.path)) sourcesSelection.delete(c.path);
      else sourcesSelection.add(c.path);
      renderSources();
    });
    els.sourcesList.appendChild(row);
  }
  els.btnSourcesApply.disabled = sourcesSelection.size === 0;
  els.sourcesNote.textContent = `${sourcesSelection.size} selected`
    + (sources.sources ? ` · ${sources.sources} source files readable` : "");
}

async function loadSources() {
  try {
    sources = await (await fetch("/api/sources")).json();
  } catch (e) {
    showStatusBanner("Couldn't read the folder list from the server.");
    return;
  }
  const saved = readSavedSources();
  // Re-apply this browser's choice before the graph is ever drawn, so the
  // page never flashes the default folders and then swaps them out.
  if (saved && saved.length && !sameSet(saved, sources.selected)) {
    try {
      const result = await postSources(saved);
      sources.selected = result.selected;
    } catch (e) {
      showStatusBanner("Saved folders are gone - falling back to " + sources.root);
      saveSources(null);
    }
  }
  sourcesSelection = new Set(sources.selected);
  renderSources();
}

els.btnSources.addEventListener("click", () => {
  const open = els.sourcesPanel.hidden;
  els.sourcesPanel.hidden = !open;
  els.btnSources.classList.toggle("is-on", open);
  els.connectorsPanel.hidden = true;
  els.btnConnectors.classList.remove("is-on");
  if (open) renderSources();
});

els.btnSourcesApply.addEventListener("click", async () => {
  const folders = [...sourcesSelection];
  els.btnSourcesApply.disabled = true;
  els.sourcesNote.textContent = "reindexing…";
  try {
    const result = await postSources(folders);
    saveSources(result.selected);
    sources.selected = result.selected;
    sourcesSelection = new Set(result.selected);
    await loadGraph();
    sources.sources = result.sources;
    els.sourcesNote.textContent =
      `${result.notes} notes, ${result.edges} links, ${result.sources} source files`;
  } catch (e) {
    showStatusBanner(e.message);
    els.sourcesNote.textContent = "failed";
  }
  els.btnSourcesApply.disabled = false;
});

// ============================================================ connectors

/*
 * Gmail, Calendar, Slack and Jira. Everything here is read-only - there is
 * no code in this project that can send or edit anything, so "connect" only
 * ever means "let JARVIS read".
 *
 * Secrets travel one way. The server never sends a token back, so a secret
 * field renders empty even when it's set, and submitting it empty leaves
 * the stored value alone.
 */
let connectorsData = null;
let openConnector = null;
let authPoll = null;
let slackChannels = null;   // fetched on demand, not part of the saved state
/*
 * Which channels are ticked, held OUTSIDE the render.
 *
 * It used to be derived from the text field on every render - and since
 * every tick re-renders, and the render refills that field from the SERVER
 * state, each click was immediately undone. Clicking a channel looked like
 * it did nothing at all. Null means "not editing yet, read it from the
 * server"; a fresh server payload resets it back to null.
 */
let slackSelected = null;

function slackSelection(connector) {
  if (slackSelected === null) {
    const field = connector.fields.find((f) => f.name === "channels");
    slackSelected = new Set(
      ((field && field.value) || "").split(",").map((x) => x.trim()).filter(Boolean)
    );
  }
  return slackSelected;
}

const STATE_TEXT = {
  connected: "connected",
  needs_signin: "sign-in needed",
  not_configured: "not connected",
};

async function loadConnectors() {
  try {
    connectorsData = await (await fetch("/api/connectors")).json();
  } catch (e) {
    showStatusBanner("Couldn't read the connector list from the server.");
    return;
  }
  renderConnectors();
  watchAuth();
}

/* The Google sign-in happens in another browser tab, so poll until the
   server says it finished - there is nothing to await here. */
function watchAuth() {
  const running = connectorsData && connectorsData.auth && connectorsData.auth.running;
  if (running && !authPoll) {
    authPoll = setInterval(loadConnectors, 1500);
  } else if (!running && authPoll) {
    clearInterval(authPoll);
    authPoll = null;
    const err = connectorsData && connectorsData.auth && connectorsData.auth.error;
    if (err) showStatusBanner(err);
  }
}

async function connectorAction(id, action, fields) {
  els.connectorsNote.textContent =
    action === "signin" ? "opening browser…"
    : action === "cancel_signin" ? "cancelling…"
    : "checking…";
  try {
    const res = await fetch("/api/connectors", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, action, fields }),
    });
    const result = await res.json();
    if (result.channels) slackChannels = result.channels;
    // The server has just told us what it stored; that wins over local edits.
    if (result.connectors) slackSelected = null;
    if (result.connectors) {
      connectorsData = result;
      renderConnectors();
      watchAuth();
    }
    // The panel's own note carries the message; a toast over the top of it
    // is the same sentence twice, covering the panel it refers to.
    els.connectorsNote.textContent = result.message || result.error || "";
    els.connectorsNote.classList.toggle("is-error", !!result.error);
  } catch (e) {
    els.connectorsNote.textContent = "failed";
  }
}

function renderConnectors() {
  if (!connectorsData) return;
  els.connectorList.innerHTML = "";

  for (const c of connectorsData.connectors) {
    const row = document.createElement("div");
    row.className = "connector";
    const open = openConnector === c.id;
    const busy = c.id === "google" && connectorsData.auth.running;

    const head = document.createElement("div");
    head.className = "connector-head";
    head.innerHTML = `<span class="conn-dot conn-${c.state}"></span>
      <span class="conn-label">${escapeHtml(c.label)}</span>
      <span class="conn-state">${busy ? "signing in…" : STATE_TEXT[c.state]}</span>`;
    head.addEventListener("click", () => {
      openConnector = open ? null : c.id;
      if (openConnector !== "slack") { slackChannels = null; slackSelected = null; }
      renderConnectors();
    });
    row.appendChild(head);

    if (open) {
      const body = document.createElement("div");
      body.className = "connector-body";
      const link = c.setup_url
        ? ` <a href="${escapeHtml(c.setup_url)}" target="_blank" rel="noopener">Open the console →</a>`
        : "";
      body.innerHTML = `<div class="conn-reads">${escapeHtml(c.reads)}</div>
        <div class="conn-setup">${escapeHtml(c.setup)}${link}</div>`;

      const inputs = {};
      const addField = (f, parent) => {
        const wrap = document.createElement("label");
        wrap.className = "conn-field";
        const hint = f.locked ? " (set in .env)"
          : f.secret && f.filled ? "" : "";
        wrap.innerHTML = `<span>${escapeHtml(f.label)}${hint}</span>`;
        const input = document.createElement("input");
        input.type = f.secret ? "password" : "text";
        input.value = f.value || "";
        input.disabled = !!f.locked;
        input.autocomplete = "off";
        wrap.appendChild(input);
        inputs[f.name] = input;
        parent.appendChild(wrap);
      };

      // Google hands you a JSON file; pasting it whole beats copying two
      // long strings out of it and getting one of them subtly wrong.
      let pasteBox = null;
      if (c.paste && c.state === "not_configured") {
        const wrap = document.createElement("label");
        wrap.className = "conn-field";
        wrap.innerHTML = `<span>Paste the downloaded JSON</span>`;
        pasteBox = document.createElement("textarea");
        pasteBox.rows = 3;
        pasteBox.placeholder = '{ "installed": { "client_id": "…", "client_secret": "…" } }';
        wrap.appendChild(pasteBox);
        body.appendChild(wrap);

        const manual = document.createElement("details");
        manual.className = "conn-manual";
        manual.innerHTML = "<summary>or type the ID and secret</summary>";
        c.fields.forEach((f) => addField(f, manual));
        body.appendChild(manual);
      } else {
        c.fields.forEach((f) => addField(f, body));
      }

      const collect = () => {
        const fields = {};
        for (const [name, el] of Object.entries(inputs)) {
          if (!el.disabled) fields[name] = el.value;
        }
        if (pasteBox && pasteBox.value.trim()) {
          fields.credentials_json = pasteBox.value;
        }
        return fields;
      };

      if (c.id === "slack" && slackChannels) {
        const chosen = slackSelection(c);
        const syncField = () => {
          if (inputs.channels) inputs.channels.value = [...chosen].join(",");
        };
        syncField();
        // Typing in the field by hand still works, and re-ticks the list.
        if (inputs.channels) {
          inputs.channels.addEventListener("input", () => {
            slackSelected = new Set(
              inputs.channels.value.split(",").map((x) => x.trim()).filter(Boolean)
            );
            renderConnectors();
          });
        }

        const pick = document.createElement("div");
        pick.className = "conn-channels";
        for (const ch of slackChannels) {
          const on = chosen.has(ch.id);
          const row = document.createElement("div");
          row.className = "channel-row" + (on ? " is-on" : "")
            + (ch.is_member ? "" : " is-out");
          row.innerHTML = `<span class="tick">${on ? "✓" : ""}</span>
            <span class="channel-name">${escapeHtml(ch.name)}</span>
            <span class="channel-id">${escapeHtml(ch.id)}</span>
            <span class="channel-note">${ch.is_member ? "" : "invite the bot first"}</span>`;
          row.addEventListener("click", () => {
            if (chosen.has(ch.id)) chosen.delete(ch.id);
            else chosen.add(ch.id);
            syncField();
            renderConnectors();
          });
          pick.appendChild(row);
        }
        body.appendChild(pick);
      }

      const actions = document.createElement("div");
      actions.className = "conn-actions";
      const button = (text, primary, onClick, disabled) => {
        const b = document.createElement("button");
        b.className = "text-btn" + (primary ? " text-btn-primary" : "");
        b.textContent = text;
        b.disabled = !!disabled;
        b.addEventListener("click", onClick);
        actions.appendChild(b);
        return b;
      };

      // One primary action per state, so there is never a "which button
      // first?" - sign-in saves whatever is in the form on its way.
      if (c.kind === "oauth") {
        if (busy) {
          // A sign-in you walked away from shouldn't hold the panel hostage.
          button("Cancel sign-in", false, () => connectorAction(c.id, "cancel_signin"));
        } else {
          const label = c.state === "connected" ? "Sign in again"
            : c.state === "needs_signin" ? "Sign in with Google"
            : "Connect with Google";
          button(label, c.state !== "connected",
                 () => connectorAction(c.id, "signin", collect()));
        }
      } else {
        if (c.id === "slack") {
          button(slackChannels ? "Reload channels" : "Load my channels", false,
                 () => connectorAction(c.id, "list_channels", collect()));
        }
        button(c.state === "Connected" ? "Reconnect" : "Connect",
               c.state !== "Connected",
               () => connectorAction(c.id, "save", collect()));
      }
      if (c.state !== "not_configured") {
        button("Disconnect", false, () => connectorAction(c.id, "disconnect"));
        actions.lastChild.classList.add("text-btn-danger");
      }

      body.appendChild(actions);
      row.appendChild(body);
    }
    els.connectorList.appendChild(row);
  }
}

els.btnConnectors.addEventListener("click", () => {
  const open = els.connectorsPanel.hidden;
  els.connectorsPanel.hidden = !open;
  els.btnConnectors.classList.toggle("is-on", open);
  els.sourcesPanel.hidden = true;
  els.btnSources.classList.remove("is-on");
  if (open) loadConnectors();
});

async function init() {
  await loadSources();   // must settle before the graph is fetched
  await loadGraph();
  checkStatus();
  loadConnectors();
}
init();

// ============================================================== transcript

/*
 * Every tool returns {speech, card}: speech is what gets said out loud, card
 * is the structured detail. Both land in the transcript, so nothing a tool
 * returned can silently disappear (see agent/tools.py, and the memory
 * guardrail in particular - a written fact must always be visible).
 */
function cardHtml(card) {
  if (!card) return "";
  switch (card.kind) {
    case "brief": {
      const parts = [];
      if (card.calendar && card.calendar.length) {
        parts.push(`<div class="card-group-label">Calendar</div>`);
        parts.push(`<ul class="card-list">` + card.calendar.map((ev) =>
          `<li>${escapeHtml(ev.title)}<span class="card-list-meta">${escapeHtml(ev.time)}</span></li>`
        ).join("") + `</ul>`);
      }
      parts.push(`<div class="card-stats">
        <span>${card.unread_count} unread</span>
        <span>${card.slack_count} new on Slack</span>
      </div>`);
      if (card.slipped && card.slipped.length) {
        parts.push(`<div class="card-group-label">Still open</div>`);
        parts.push(`<ul class="card-list">` + card.slipped.map((t) =>
          `<li>${escapeHtml(t.title)}<span class="card-list-meta">${escapeHtml(t.project)} · ${escapeHtml(t.priority)}</span></li>`
        ).join("") + `</ul>`);
      }
      return parts.join("");
    }
    case "calendar":
      return (card.items && card.items.length)
        ? `<ul class="card-list">` + card.items.map((ev) =>
            `<li>${escapeHtml(ev.title)}<span class="card-list-meta">${escapeHtml(ev.time)}</span></li>`
          ).join("") + `</ul>`
        : `<div class="card-fact">Nothing on the calendar.</div>`;
    case "inbox":
      return `<ul class="card-list">` + (card.items || []).map((i) =>
        `<li><strong>${escapeHtml(i.subject)}</strong>
          <span class="card-list-meta">${escapeHtml(i.from)} · ${escapeHtml(i.date)} · ${
            i.already_tracked ? "tracked: " + escapeHtml(i.existing_note) : "new"
          }</span></li>`
      ).join("") + `</ul>`;
    case "slack":
      return `<ul class="card-list">` + (card.items || []).map((m) =>
        `<li><strong>${escapeHtml(m.from)}</strong> in ${escapeHtml(m.channel)}
          <span class="card-list-meta">${escapeHtml(m.text)}</span></li>`
      ).join("") + `</ul>`;
    case "plan":
      return `<ul class="card-list">` + (card.items || []).map((t) =>
        `<li>${escapeHtml(t.title)}<span class="card-list-meta">${escapeHtml(t.project)} · ${escapeHtml(t.priority)}</span></li>`
      ).join("") + `</ul>`;
    case "answer": {
      // Only the detail. The old citation lists ("FROM YOUR NOTES",
      // "SOURCE FILES (5 OF 120)") repeated paths the answer had already
      // named inline, and buried the answer under a directory listing.
      // The note behind the answer still lights up in the graph instead.
      return card.detail ? `<div class="answer-detail md">${renderMarkdown(card.detail)}</div>` : "";
    }
    case "tasks": {
      if (!card.items || !card.items.length) {
        return `<div class="card-fact">Nothing assigned to you.</div>`;
      }
      return `<ul class="card-list">` + card.items.map((t) => {
        const doing = t.category === "indeterminate";
        return `<li>
          ${t.key ? `<span class="task-key">${escapeHtml(t.key)}</span>` : ""}
          ${escapeHtml(t.title)}
          ${t.status_name ? `<span class="task-state${doing ? " is-doing" : ""}">${escapeHtml(t.status_name)}</span>` : ""}
          <span class="card-list-meta">${escapeHtml(t.project || "")}${t.priority ? " · " + escapeHtml(t.priority) : ""}${t.updated ? " · " + escapeHtml(t.updated) : ""}</span>
        </li>`;
      }).join("") + `</ul>`;
    }
    case "memory":
      return `<div class="card-fact">"${escapeHtml(card.fact)}"</div>
        <div class="card-list-meta">${escapeHtml(card.path)}</div>`;
    case "search":
      return `<ul class="card-list">` + (card.results || []).map((n) =>
        `<li><strong>${escapeHtml(n.title)}</strong><span class="card-list-meta">${escapeHtml(n.path)}</span>
          ${escapeHtml(n.excerpt)}</li>`
      ).join("") + `</ul>`;
    case "path":
      return (card.titles && card.titles.length)
        ? `<div class="card-fact">${card.titles.map(escapeHtml).join(" &rarr; ")}</div>` : "";
    case "research":
      return `<div class="card-fact">${escapeHtml(card.abstract || "")}</div>`
        + (card.source ? `<div class="card-list-meta">Source: ${escapeHtml(card.source)}</div>` : "")
        + (card.related_note ? `<div class="card-list-meta">Related note: ${escapeHtml(card.related_note.title)}</div>` : "");
    case "model_error":
      return `<div class="card-fact card-error">${escapeHtml(card.detail || "")}</div>`;
    default:
      return `<pre class="card-raw">${escapeHtml(JSON.stringify(card, null, 2))}</pre>`;
  }
}

function addTurn(who, text, { tool = null, card = null } = {}) {
  const el = document.createElement("div");
  el.className = `turn turn-${who}`;
  const toolTag = tool ? `<span class="turn-tool">${escapeHtml(tool.replace(/_/g, " "))}</span>` : "";
  const body = card ? cardHtml(card) : "";
  el.innerHTML = `<div class="turn-who">${who === "you" ? "you" : "jarvis"}</div>
    <div class="turn-text">${escapeHtml(text)}${toolTag}</div>
    ${body ? `<div class="ask-card">${body}</div>` : ""}`;
  els.transcript.appendChild(el);
  els.transcript.hidden = false;
  while (els.transcript.children.length > MAX_TURNS_ON_SCREEN) {
    els.transcript.removeChild(els.transcript.firstChild);
  }
  // Scroll the TOP of the new turn into view, not its bottom. A long card
  // would otherwise park the reader at the file list, with the answer they
  // asked for scrolled off above it.
  els.transcript.scrollTop = el.offsetTop - els.transcript.offsetTop;
  return el;
}

function focusRelatedNote(card) {
  if (!card) return;
  let noteId = null;
  if ((card.kind === "search" || card.kind === "answer")
      && card.results && card.results.length) {
    const note = card.results.find((r) => r.id);
    noteId = note ? note.id : null;
  } else if (card.kind === "path" && card.path && card.path.length) {
    noteId = card.path[card.path.length - 1];
  } else if (card.kind === "research" && card.related_note) {
    noteId = card.related_note.id;
  }
  if (noteId) { graph.focusById(noteId); openNote(noteId); }
}

function recordHistory(message, result) {
  history.push({ message, speech: result.speech, card: result.card, tool: result.tool });
  if (history.length > MAX_HISTORY) history = history.slice(-MAX_HISTORY);
}

// =================================================================== turns

/* One round trip. Returns the result so the caller can decide to speak it. */
async function runTurn(message, { echoUser = true } = {}) {
  if (!message || !message.trim()) return null;
  if (echoUser) addTurn("you", message);
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, history }),
    });
    const result = await res.json();
    addTurn("jarvis", result.speech, { tool: result.tool, card: result.card });
    focusRelatedNote(result.card);
    recordHistory(message, result);
    return result;
  } catch (e) {
    addTurn("jarvis", "Couldn't reach the server for that.");
    return null;
  }
}

/* The fixed-endpoint buttons (brief / plan) take the same path home. */
async function runToolButton(endpoint, label, tool) {
  addTurn("you", label);
  setBusy(true);
  try {
    const res = await fetch(endpoint, { method: "POST" });
    const result = await res.json();
    addTurn("jarvis", result.speech, { tool, card: result.card });
    recordHistory(label, { ...result, tool });
    await voice.speak(result.speech);
  } catch (e) {
    addTurn("jarvis", "Couldn't reach the server for that.");
  } finally {
    setBusy(false);
  }
}

// ============================================================ voice engine

const STATE_LABEL = {
  off: "offline",
  armed: "listening",
  capturing: "listening",
  transcribing: "transcribing",
  thinking: "thinking",
  speaking: "speaking",
};

const PILL_TEXT = {
  capturing: "Listening…",
  transcribing: "Transcribing…",
  thinking: "Thinking…",
  speaking: "Speaking…",
};

let manualBusy = false;   // a typed/button turn is in flight

function setBusy(busy) {
  manualBusy = busy;
  paintState(voice.state);
}

function paintState(rawState) {
  // With the mic closed there's no engine state to show, but a typed question
  // is still work in progress - surface it as thinking rather than offline.
  const state = rawState === "off" && manualBusy ? "thinking" : rawState;

  els.reactor.dataset.state = state;
  els.reactorLabel.textContent = STATE_LABEL[state] || state;

  const live = voice.running;
  els.brandDot.classList.toggle("is-live", live);
  els.btnMic.classList.toggle("is-live", live);
  els.btnMic.classList.toggle("is-hot", state === "capturing");

  els.liveDot.className = "live-dot"
    + (state === "armed" || state === "capturing" ? " is-live" : "")
    + (state === "transcribing" || state === "thinking" ? " is-busy" : "")
    + (state === "speaking" ? " is-speaking" : "");

  const pill = PILL_TEXT[state];
  els.livePill.hidden = !pill;
  if (pill) els.livePillText.textContent = pill;

  if (state !== "capturing" && state !== "armed") setHudLevel(0.02);
}

function paintMode(mode) {
  els.reactorHint.textContent =
    mode === "off" ? "click the mic once to go hands-free"
    : mode === "live" ? "open mic — just talk · click for wake word"
    : "say “Jarvis…” · click for open mic";
  els.reactorHint.style.cursor = mode === "off" ? "default" : "pointer";
}

const voice = new VoiceEngine({
  onState: paintState,
  onLevel: (level) => setHudLevel(level),
  onCaption: (text, kind) => {
    els.askCaption.textContent = kind === "you" && text ? "“" + text + "”" : text;
  },
  onError: (message) => showStatusBanner(message),
  onModeChange: paintMode,
  // The engine hands us a finished utterance; we run the turn and hand back
  // what to say, so the engine can go straight from thinking to speaking.
  onUtterance: async (text) => {
    const result = await runTurn(text, { echoUser: true });
    return result ? { speech: result.speech } : { speech: "Couldn't reach the server for that." };
  },
});

paintMode("off");
paintState("off");

// ------------------------------------------------------------- mic control

function savedMode() {
  try { return localStorage.getItem(MODE_KEY) || "live"; } catch (e) { return "live"; }
}
function saveMode(mode) {
  try { localStorage.setItem(MODE_KEY, mode); } catch (e) { /* private window */ }
}

async function enableVoice(mode) {
  if (!voiceOutAvailable) {
    showStatusBanner("No ElevenLabs key configured - voice is off. Type instead.");
    return false;
  }
  const ok = await voice.start(mode);
  if (ok) saveMode(mode);
  paintMode(voice.mode);
  return ok;
}

async function toggleMic() {
  if (voice.running) {
    voice.stopAll();
    saveMode("off");
    paintMode("off");
  } else {
    const mode = savedMode() === "wake" ? "wake" : "live";
    await enableVoice(mode);
  }
}

els.btnMic.addEventListener("click", toggleMic);

els.reactorHint.addEventListener("click", () => {
  if (!voice.running) { toggleMic(); return; }
  const next = voice.mode === "live" ? "wake" : "live";
  voice.setMode(next);
  saveMode(next);
  paintMode(next);
});

// Lucide volume-2 / volume-x. Multi-path icons, so the whole svg body is
// swapped rather than one `d` attribute.
const SPEAKER_BODY = "M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298z";
const SPEAKER_ON = `<path d="${SPEAKER_BODY}"/><path d="M16 9a5 5 0 0 1 0 6"/>`
  + `<path d="M19.364 18.364a9 9 0 0 0 0-12.728"/>`;
const SPEAKER_OFF = `<path d="${SPEAKER_BODY}"/><path d="m22 9-6 6"/><path d="m16 9 6 6"/>`;

els.btnMute.addEventListener("click", () => {
  const muted = !els.btnMute.classList.contains("is-muted");
  els.btnMute.classList.toggle("is-muted", muted);
  els.btnMute.title = muted ? "Unmute JARVIS's voice" : "Mute JARVIS's voice";
  els.btnMute.querySelector("svg").innerHTML = muted ? SPEAKER_OFF : SPEAKER_ON;
  voice.setMuted(muted);
});

/*
 * Browsers will not open a microphone without a user gesture, so the mic
 * cannot come up on page load - that one gesture is a hard rule, not a design
 * choice. What we can do is make ANY first interaction count, and remember
 * the choice, so it's one click ever rather than one click per question.
 */
if (savedMode() !== "off") {
  const armOnFirstGesture = () => {
    document.removeEventListener("pointerdown", armOnFirstGesture);
    document.removeEventListener("keydown", armOnFirstGesture);
    if (!voice.running) enableVoice(savedMode());
  };
  document.addEventListener("pointerdown", armOnFirstGesture);
  document.addEventListener("keydown", armOnFirstGesture);
  els.reactorHint.textContent = "click anywhere to open the mic";
}

// ================================================================= buttons

els.btnFit.addEventListener("click", () => graph.fitToView());

els.btnLabels.addEventListener("click", () => {
  const on = !els.btnLabels.classList.contains("is-on");
  els.btnLabels.classList.toggle("is-on", on);
  graph.setLabelsVisible(on);
});

els.btnContrast.addEventListener("click", () => {
  const bright = !els.btnContrast.classList.contains("is-on");
  els.btnContrast.classList.toggle("is-on", bright);
  graph.setContrast(bright ? 2.2 : 1);
});

els.btnBrief.addEventListener("click", () => runToolButton("/api/brief", "brief me", "brief_me"));
els.btnPlan.addEventListener("click", () => runToolButton("/api/plan", "plan my day", "plan_day"));

els.btnMemory.addEventListener("click", () => {
  const fact = prompt("Remember what?");
  if (fact && fact.trim()) submitText("remember " + fact.trim());
});

// ============================================================ typed input

async function submitText(message) {
  setBusy(true);
  try {
    const result = await runTurn(message);
    if (result) await voice.speak(result.speech);
  } finally {
    setBusy(false);
  }
}

els.askInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && els.askInput.value.trim()) {
    const message = els.askInput.value;
    els.askInput.value = "";
    submitText(message);
  }
});

// ================================================================ keyboard

window.addEventListener("keydown", (e) => {
  if (document.activeElement === els.askInput) return;
  if (e.code === "Space") { e.preventDefault(); toggleMic(); }
  if (e.code === "Escape") { e.preventDefault(); voice.panic(); }
});

window.addEventListener("beforeunload", () => voice.stopAll());
