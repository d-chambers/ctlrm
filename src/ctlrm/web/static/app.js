/* The managed store is authoritative. Local state holds only navigation and view preferences. */
"use strict";
const $ = (selector) => document.querySelector(selector);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const pretty = (value) => String(value ?? "").replaceAll("_", " ");
const id = (prefix) => `${prefix}-${crypto.randomUUID()}`;
const enc = encodeURIComponent;
const readPreference = (key, fallback) => {
  try {
    return JSON.parse(localStorage.getItem(key)) ?? fallback;
  } catch {
    return fallback;
  }
};
const preference = (key, value) => {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* Optional preference storage. */
  }
};
let capability = new URLSearchParams(location.hash.slice(1)).get("token");
try {
  if (capability) sessionStorage.setItem("ctlrm-token", capability);
  else capability = sessionStorage.getItem("ctlrm-token");
} catch {
  /* The URL capability still works without storage. */
}
if (location.hash) history.replaceState(null, "", location.pathname);
const state = {
  data: { projects: [], participants: [], actions: [] },
  tab: "projects",
  selected: null,
  filter: "active",
  opened: [],
  jobs: {},
  views: {},
  search: "",
  file: null,
  artifact: null,
  cache: new Map(),
  error: null,
  revision: 0,
};
let lastSnapshot = "",
  polling = false,
  noticeTimer;
let term = null,
  fit = null,
  socket = null,
  attachment = null,
  fullscreen = false,
  terminalFocus = null;
const drafts = new Map();
const project = (pid) => state.data.projects.find((p) => p.project.id === pid);
const job = (pid, jid) => project(pid)?.jobs.find((j) => j.job.id === jid);
const selectedJob = (pid) => job(pid, state.jobs[pid]) || project(pid)?.jobs[0];
const participants = (pid, jid) =>
  state.data.participants.filter(
    (p) => (!pid || p.project === pid) && (!jid || p.job === jid),
  );
const activeExecution = (j) =>
  j?.run?.executions.find((e) => e.id === j.run.active);
const endpoint = (pid, jid) =>
  `/api/projects/${enc(pid)}${jid ? `/jobs/${enc(jid)}` : ""}`;
const icon = (name) =>
  ({
    folder: "▤",
    branch: "⑂",
    people: "◉",
    terminal: ">_",
    check: "✓",
    arrow: "→",
    doc: "▧",
    graph: "◇",
    bell: "◷",
  })[name] || "•";
const dot = (status) =>
  `<span class="dot ${esc(status)}" aria-hidden="true"></span>`;
const pill = (status, label = pretty(status)) =>
  `<span class="pill ${esc(status)}">${dot(status)}${esc(label)}</span>`;
const taskActive = (p) =>
  p.session?.interactions?.some((m) => m.status === "running") ||
  (p.active &&
    p.session?.ready &&
    !p.session?.recovering &&
    ["pending", "acknowledged"].includes(p.active.status));
const activityLabel = (p) =>
  p.kind === "human"
    ? p.active
      ? "Task needs a response"
      : "No active task"
    : taskActive(p)
      ? "Task active"
      : p.session?.recovering
        ? "Reconciling work"
        : p.session
          ? "No active turn reported"
          : "Waiting for assignment";
const activityDot = (p) =>
  taskActive(p)
    ? '<span class="spinner" aria-label="Task active"></span>'
    : dot(p.session?.status || "planned");
const prLink = (pr) => {
  if (!pr) return '<span class="muted">No PR assigned</span>';
  let url = pr.url;
  if (!url && pr.repository.split("/").length === 2)
    url = `https://github.com/${pr.repository}/pull/${pr.number}`;
  try {
    if (!["https:", "http:"].includes(new URL(url).protocol)) url = null;
  } catch {
    url = null;
  }
  return url
    ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">PR #${esc(pr.number)}</a>`
    : `PR #${esc(pr.number)} · ${esc(pr.repository)}`;
};
async function api(path, body, method = "POST") {
  const response = await fetch(path, {
    headers: {
      Authorization: `Bearer ${capability || ""}`,
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    ...(body !== undefined ? { method, body: JSON.stringify(body) } : {}),
    cache: "no-store",
  });
  const result = await response.json();
  if (!response.ok)
    throw new Error(
      result.error ||
        (typeof result.detail === "string"
          ? result.detail
          : JSON.stringify(result.detail)) ||
        `Request failed (${response.status})`,
    );
  return result;
}
function notify(message) {
  clearTimeout(noticeTimer);
  $("#notice").textContent = message;
  $("#notice").hidden = false;
  noticeTimer = setTimeout(() => {
    $("#notice").hidden = true;
  }, 7000);
}
async function refresh(force = false) {
  if (polling) return;
  polling = true;
  try {
    const data = await api("/api/snapshot");
    const serialized = JSON.stringify(data);
    state.error = null;
    $("#connection").className = "";
    $("#connection").textContent = "● Connected locally";
    if (serialized !== lastSnapshot || force) {
      state.data = data;
      lastSnapshot = serialized;
      state.revision++;
      if (!project(state.selected))
        state.selected =
          data.projects.find((p) => p.status !== "archived")?.project.id ||
          data.projects[0]?.project.id ||
          null;
      state.opened = state.opened.filter((pid) => project(pid));
      if (
        !["projects", "participants", "queue"].includes(state.tab) &&
        !project(state.tab)
      )
        state.tab = "projects";
      for (const key of state.cache.keys())
        if (key.startsWith("activity:")) state.cache.delete(key);
      render();
    }
    updateTerminalState();
  } catch (error) {
    state.error = error.message;
    $("#connection").textContent = "Disconnected · retrying";
    $("#connection").className = "failed";
    if (!lastSnapshot)
      $("#main").innerHTML =
        `<div class="empty"><h1>Connect to Control room</h1><p>${esc(error.message)}</p><p>Open the complete local URL printed by <code>ctlrm serve</code>.</p><button class="secondary" data-action="refresh">Retry connection</button></div>`;
  } finally {
    polling = false;
  }
}
function matchingProjects() {
  const q = state.search.toLowerCase().replace(/^#/, "");
  return state.data.projects.filter(
    (p) =>
      (state.filter === "archived"
        ? p.status === "archived"
        : p.status !== "archived") &&
      (!q ||
        [
          p.project.name,
          p.project.codebase,
          ...(p.project.goals || []),
          ...p.jobs.flatMap((j) => [
            j.job.title,
            j.pr?.number,
            j.pr?.repository,
          ]),
        ]
          .join(" ")
          .toLowerCase()
          .includes(q)),
  );
}
function navigation() {
  $("#nav").innerHTML =
    `<button class="tab ${state.tab === "projects" ? "active" : ""}" data-action="tab" data-id="projects">${icon("folder")} Projects <span class="tab-count">${state.data.projects.length}</span></button><button class="tab ${state.tab === "participants" ? "active" : ""}" data-action="tab" data-id="participants">Participants <span class="tab-count">${state.data.participants.length}</span></button>${state.opened.map((pid) => `<div class="project-tab ${state.tab === pid ? "current" : ""}"><button class="tab ${state.tab === pid ? "active" : ""}" data-action="tab" data-id="${esc(pid)}">${esc(project(pid).project.name || pid)}</button><button class="tab-close" data-action="close-project" data-id="${esc(pid)}" aria-label="Close ${esc(project(pid).project.name)} tab">×</button></div>`).join("")}<button class="human-nav" data-action="tab" data-id="queue">Needs you <span class="badge-count">${state.data.actions.length}</span></button>`;
}
function sidebar() {
  if (project(state.tab)) {
    const p = project(state.tab);
    $("#sidebar").innerHTML =
      `<div class="side-top"><div class="eyebrow">Project workspace</div><h2>${esc(p.project.name)}</h2><strong>Participants</strong></div>${p.jobs.map((j) => `<div class="group-label">${j.status === "completed" ? "✓ " : ""}${esc(j.job.title)}</div>${participants(p.project.id, j.job.id).map(personRow).join("")}`).join("")}<div class="side-bottom">Open a participant’s existing session. Closing the terminal view leaves the agent running.</div>`;
  } else {
    $("#sidebar").innerHTML =
      `<div class="side-top"><div class="section-head"><h2>Projects</h2><button class="icon-button" data-action="new-project" aria-label="New project">＋</button></div><select id="project-filter" class="project-filter" aria-label="Project status"><option value="active" ${state.filter === "active" ? "selected" : ""}>Active & completed</option><option value="archived" ${state.filter === "archived" ? "selected" : ""}>Archived projects</option></select></div><div class="project-list">${
        matchingProjects()
          .map(
            (p) =>
              `<button class="project-item ${state.selected === p.project.id ? "selected" : ""}" data-action="select-project" data-id="${esc(p.project.id)}" title="Click to preview; double-click to open"><span class="project-title">${dot(p.status === "active" ? "ready" : p.status)}${esc(p.project.name || p.project.id)}</span><div class="repo">${esc(p.project.codebase || p.error)}</div><div class="meta">${p.jobs.length} jobs · ${esc(pretty(p.status))}</div></button>`,
          )
          .join("") || '<p class="small muted">No matching projects.</p>'
      }</div><div class="side-bottom"><button class="secondary" data-action="new-project">New project</button><p>Click to preview.<br>Double-click to open a project tab.</p></div>`;
  }
}
function personRow(p) {
  return `<div class="person"><span class="avatar ${p.kind === "human" ? "human" : "agent"}">${p.kind === "human" ? "You" : p.provider === "claude" ? "Cl" : p.provider === "codex" ? "Co" : "Ag"}</span><div class="person-info"><div class="person-name">${esc(pretty(p.name))} ${activityDot(p)}</div><div class="person-role">${esc(p.provider || "Human")} · ${esc(activityLabel(p))}${p.session ? `<br>Session: ${esc(p.session.status)}` : ""}</div></div><button class="person-action" data-action="participant" data-id="${esc(p.id)}" aria-label="Open ${esc(pretty(p.name))}">${icon(p.kind === "human" ? "arrow" : "terminal")}</button></div>`;
}
function banner(pid) {
  const tasks = state.data.actions.filter((a) => !pid || a.project === pid);
  return tasks.length
    ? `<div class="banner"><div><strong>${tasks.length === 1 ? "Your response is needed" : `${tasks.length} tasks need your response`}</strong><p>${esc(pretty(tasks[0].active.task))} · ${esc(job(tasks[0].project, tasks[0].job)?.job.title)}</p></div><button class="secondary" data-action="respond" data-id="${esc(tasks[0].id)}">Review task →</button></div>`
    : "";
}
function jobRows(p) {
  if (!p.jobs.length)
    return '<div class="card"><p>No jobs planned yet. Add a job and choose a reusable workflow.</p></div>';
  return `<table class="job-table"><thead><tr><th>Job & pull request</th><th>Workflow</th><th>Status</th><th></th></tr></thead><tbody>${p.jobs.map((j) => `<tr><td><button class="job-title" data-action="open-job" data-project="${esc(p.project.id)}" data-id="${esc(j.job.id)}">${j.status === "completed" ? '<span class="done" aria-label="Completed">✓</span>' : ""}${esc(j.job.title || j.job.id)}</button><small>${prLink(j.pr)}</small></td><td>${esc(j.job.workflow?.name || "Invalid")}</td><td>${pill(j.status)}${j.paused ? "<small>Dispatch paused</small>" : ""}</td><td><button data-action="open-job" data-project="${esc(p.project.id)}" data-id="${esc(j.job.id)}" aria-label="Open ${esc(j.job.title)}">→</button></td></tr>`).join("")}</tbody></table>`;
}
function projectSummary() {
  const matches = matchingProjects();
  const p = matches.find((p) => p.project.id === state.selected);
  if (!p)
    return `<div class="empty"><h1>${state.data.projects.length ? "Select a project" : "Your projects start here"}</h1><p>A project holds related goals. Plan jobs with reusable YAML workflows, then start each job in its own worktree.</p><button class="primary" data-action="new-project">New project</button></div>`;
  return `<div class="page"><div class="eyebrow">Project overview · ${esc(p.status)}</div><div class="section-head"><h1 class="main-title">${esc(p.project.name || p.project.id)}</h1><button class="primary" data-action="open-project" data-id="${esc(p.project.id)}">Open project →</button></div><p class="subtitle">${esc((p.project.goals || []).join(" · "))}</p><p class="small muted mono">${esc(p.project.codebase)}</p>${p.error ? `<div class="error-box">${esc(p.error)}</div>` : ""}${banner(p.project.id)}<div class="section-head"><h2>Planned jobs</h2><button class="secondary" data-action="new-job" data-project="${esc(p.project.id)}" ${p.status !== "active" ? "disabled" : ""}>Add job</button></div>${jobRows(p)}<div class="two-col"><div class="card"><h3>Design & context</h3><p class="small muted">${p.project.design ? "Project goals and design document" : "No design document supplied"}</p><button class="secondary" data-action="project-view" data-project="${esc(p.project.id)}" data-view="design">Open design</button></div><div class="card"><h3>Project artifacts</h3><p class="small muted">Execution reports, version manifests, and retained code.</p><button class="secondary" data-action="project-view" data-project="${esc(p.project.id)}" data-view="artifacts">Browse artifacts</button></div></div>${p.archive ? `<p class="small muted">Archive: <span class="mono">${esc(p.archive.path)}</span></p>` : ""}</div>`;
}
function workspace(pid) {
  const p = project(pid),
    j = selectedJob(pid),
    view = state.views[pid] || "overview";
  return `<div class="page"><div class="eyebrow">${esc(p.status)} project</div><div class="section-head"><h1 class="main-title">${esc(p.project.name)}</h1><div class="actions"><button class="secondary" data-action="new-job" data-project="${esc(pid)}" ${p.status !== "active" ? "disabled" : ""}>Add job</button><button class="secondary" data-action="archive" data-project="${esc(pid)}" ${p.status === "archived" ? "disabled" : ""}>Archive</button></div></div><p class="small muted">${esc((p.project.goals || []).join(" · "))}</p>${banner(pid)}${j ? `<div class="section-head"><select class="job-picker" id="job-picker" aria-label="Selected job" data-project="${esc(pid)}">${p.jobs.map((x) => `<option value="${esc(x.job.id)}" ${x.job.id === j.job.id ? "selected" : ""}>${x.status === "completed" ? "✓ " : ""}${esc(x.job.title)}</option>`).join("")}</select><span>${prLink(j.pr)}</span></div>` : ""}<nav class="view-tabs" aria-label="Project views">${["overview", "design", "code", "activity", "artifacts"].map((v) => `<button data-action="view" data-view="${v}" class="${view === v ? "active" : ""}">${pretty(v)[0].toUpperCase() + pretty(v).slice(1)}</button>`).join("")}</nav><div id="view-content">${view === "design" ? design(p) : view === "artifacts" ? artifacts(p) : view === "activity" ? '<div id="activity-content" class="muted">Loading accepted events…</div>' : view === "code" ? '<div id="code-view" class="muted">Loading code inventory…</div>' : overview(p, j)}</div></div>`;
}
function design(p) {
  return `<div class="card"><h2>Project design</h2><div class="doc-content">${esc(p.project.design || "No design document was supplied when the project was created.")}</div></div><div class="section-head"><h2>Planned jobs</h2></div>${jobRows(p)}${p.jobs.map((j) => `<div class="card"><h3>${j.status === "completed" ? "✓ " : ""}${esc(j.job.title)}</h3><div class="doc-content">${esc(j.job.instructions)}</div>${j.job.acceptance?.length ? `<h4>Acceptance criteria</h4><ul>${j.job.acceptance.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}<p class="small muted">Workflow: ${esc(j.job.workflow?.name)} · Dependencies: ${esc(j.job.depends_on?.join(", ") || "none")}</p></div>`).join("")}`;
}
function overview(p, j) {
  if (!j) return '<div class="empty">Add a job to see its task workflow.</div>';
  if (!j.job.workflow)
    return `<div class="error-box">${esc(j.error || "Invalid job record")}</div>`;
  return `<div class="section-head"><div><h2>${esc(j.job.workflow.name)} workflow ${pill(j.status)}</h2><p class="small muted">Each node is a fixed task. Outcomes route to the next task; dashed nodes are optional reviewers.</p></div><div class="actions">${j.status === "planned" ? `<button class="primary" data-action="start" data-project="${esc(p.project.id)}" data-job="${esc(j.job.id)}">Start job</button>` : ""}<button class="secondary" data-action="assign-pr" data-project="${esc(p.project.id)}" data-job="${esc(j.job.id)}" ${p.status !== "active" ? "disabled" : ""}>Assign PR</button>${j.run && p.status === "active" ? `<button class="secondary" data-action="job-controls" data-project="${esc(p.project.id)}" data-job="${esc(j.job.id)}">Job controls</button>` : ""}</div></div>${j.paused ? `<div class="error-box">Dispatch paused: ${esc(j.paused)}</div>` : ""}${j.run?.reason ? `<div class="error-box">${esc(j.run.reason)}</div>` : ""}${j.error ? `<div class="error-box">${esc(j.error)}</div>` : ""}${graph(p.project.id, j)}<div class="card"><h3>Job goal</h3><div class="doc-content">${esc(j.job.instructions)}</div><p class="small mono">${j.launch ? `${esc(j.launch.branch)}<br>${esc(j.launch.root)}` : "A dedicated branch and worktree will be created when this job starts."}</p></div>`;
}
function graph(pid, j) {
  const tasks = j.job.workflow.tasks,
    executions = j.run?.executions || [];
  const destinations = [
    ...new Set(
      Object.values(tasks)
        .flatMap((t) => Object.values(t.transitions))
        .filter((x) => x.startsWith("terminal:")),
    ),
  ];
  const order = [],
    seen = new Set();
  function visit(name) {
    if (!tasks[name] || seen.has(name)) return;
    seen.add(name);
    order.push(name);
    const task = tasks[name];
    for (const [outcome, destination] of Object.entries(task.transitions))
      if (!["changes_requested", "blocked", "unavailable"].includes(outcome))
        visit(destination);
    for (const destination of task.specialist_tasks) visit(destination);
    for (const destination of Object.values(task.transitions))
      visit(destination);
  }
  visit(j.job.workflow.entry);
  const nodes = order
    .map((name) => {
      const t = tasks[name];
      const visits = executions.filter((e) => e.task === name),
        latest = visits.at(-1),
        current = j.run?.active === latest?.id && j.run?.status === "running";
      return `<button class="workflow-node ${current ? "current" : ""} ${latest?.status === "completed" ? "completed" : ""} ${t.optional ? "optional" : ""}" data-action="task" data-project="${esc(pid)}" data-job="${esc(j.job.id)}" data-task="${esc(name)}" data-node="${esc(name)}"><strong>${latest?.status === "completed" ? "✓ " : ""}${esc(pretty(name))}</strong><small>${esc(pretty(t.role))} · ${esc(j.job.workflow.roles[t.role].kind)}</small><small>${current ? "Current task · " : ""}${esc(latest?.status || (t.optional ? "Optional" : "Not started"))}${visits.length > 1 ? ` · Visit ${visits.length}` : ""}</small></button>`;
    })
    .join("");
  return `<div class="workflow-viewport" tabindex="0" role="region" aria-label="Task workflow graph"><div class="workflow-canvas" id="workflow-canvas" data-project="${esc(pid)}" data-job="${esc(j.job.id)}"><svg aria-hidden="true"><defs><marker id="route-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#8ca7b1"/></marker></defs><g id="graph-edges"></g></svg>${nodes}${destinations.map((d) => `<div class="workflow-node terminal" data-node="${esc(d)}"><strong>${esc(pretty(d.replace("terminal:", "")))}</strong><small>${d === "terminal:return" ? "Resume the requesting review’s continuation" : "Workflow outcome"}</small></div>`).join("")}</div></div><p class="small muted">Click a task for its instructions, execution history, and versioned artifacts.</p>`;
}
function drawGraph() {
  const canvas = $("#workflow-canvas");
  if (!canvas) return;
  const j = job(canvas.dataset.project, canvas.dataset.job),
    box = canvas.getBoundingClientRect();
  const nodes = new Map(
    [...canvas.querySelectorAll("[data-node]")].map((n) => [
      n.dataset.node,
      n.getBoundingClientRect(),
    ]),
  );
  const edges = [];
  for (const [source, t] of Object.entries(j.job.workflow.tasks)) {
    for (const [outcome, destination] of Object.entries(t.transitions))
      edges.push({ source, destination, outcome, optional: false });
    for (const destination of t.specialist_tasks)
      edges.push({
        source,
        destination,
        outcome: "specialist",
        optional: true,
      });
  }
  $("#graph-edges").innerHTML = edges
    .map(({ source, destination, outcome, optional }, i) => {
      const a = nodes.get(source),
        b = nodes.get(destination);
      if (!a || !b) return "";
      const forward = b.left > a.left && Math.abs(b.top - a.top) < 10;
      const x1 = (forward ? a.right : a.left + a.width * 0.7) - box.left,
        y1 = (forward ? a.top + a.height * 0.55 : a.bottom) - box.top;
      const x2 = (forward ? b.left : b.left + b.width * 0.3) - box.left,
        y2 = (forward ? b.top + b.height * 0.55 : b.top) - box.top;
      let path;
      if (forward)
        path = `M ${x1} ${y1} C ${x1 + 24} ${y1},${x2 - 24} ${y2},${x2} ${y2}`;
      else if (y2 > y1)
        path = `M ${x1} ${y1} C ${x1} ${y1 + 25},${x2} ${y2 - 25},${x2} ${y2}`;
      else {
        const side = 14 + (i % 4) * 6;
        path = `M ${x1} ${y1} C ${x1} ${y1 + 20},${side} ${y1 + 20},${side} ${y1} L ${side} ${Math.max(12, y2 - 20)} Q ${side} ${Math.max(7, y2 - 28)},${x2} ${Math.max(7, y2 - 28)} L ${x2} ${y2}`;
      }
      return `<path d="${path}" fill="none" stroke="${optional ? "#af91b1" : "#8ca7b1"}" stroke-width="1.5" ${optional ? 'stroke-dasharray="5 4"' : ""} marker-end="url(#route-arrow)"><title>${esc(source)} → ${esc(destination)}: ${esc(outcome)}</title></path>`;
    })
    .join("");
}
function artifacts(p) {
  const cards = [
    `<article class="artifact-item"><h3>Project design & goals</h3><p>Project context · supplied when the project was created</p><button class="secondary" data-action="view" data-view="design">Read design</button></article>`,
  ];
  for (const j of p.jobs)
    for (const e of j.run?.executions || []) {
      if (!e.artifact && !e.summary) continue;
      const latest = [...j.run.executions]
        .reverse()
        .find((x) => x.task === e.task);
      cards.push(
        `<article class="artifact-item"><h3>${esc(pretty(e.task))}</h3><p>${esc(j.job.title)} · ${esc(pretty(e.participant))}</p><p>${pill(e.status, e.outcome || e.status)} ${latest.id !== e.id ? "<strong>Prior execution</strong>" : ""}</p><p>${esc((e.summary || "").slice(0, 160))}</p><p class="mono">Execution ${esc(e.id)}<br>${esc(e.artifact || "No captured manifest")}</p><button class="secondary" data-action="execution" data-project="${esc(p.project.id)}" data-job="${esc(j.job.id)}" data-execution="${esc(e.id)}">Open report & artifact</button></article>`,
      );
    }
  return `<div class="section-head"><h2>Project artifacts</h2><span class="small muted">${cards.length} records</span></div><p class="small muted">Reports belong to an exact execution. Code manifests record content hashes; original file bytes are available when retained or still unchanged.</p><div class="artifact-grid">${cards.join("")}</div>${p.archive ? `<div class="card"><h3>Verified project archive</h3><p class="mono small">${esc(p.archive.path)}</p><p class="mono small">SHA-256 ${esc(p.archive.sha256)}</p><p class="small muted">Includes retained code and project records. Open the archive from its local path.</p></div>` : ""}`;
}
function peoplePage() {
  const q = state.search.toLowerCase();
  const list = state.data.participants.filter((p) =>
    [
      p.name,
      p.provider,
      project(p.project)?.project.name,
      job(p.project, p.job)?.job.title,
    ]
      .join(" ")
      .toLowerCase()
      .includes(q),
  );
  return `<div class="page"><h1 class="main-title">Participants</h1><p class="subtitle">All human and agent roles across projects.</p><div class="participant-table">${list.map((p) => `<article class="participant-card">${activityDot(p)}<div class="description"><strong>${esc(pretty(p.name))}</strong><p>${esc(project(p.project)?.project.name)} / ${esc(job(p.project, p.job)?.job.title)}</p><p>${esc(p.provider || "Human")} · ${esc(activityLabel(p))}${p.session ? ` · Session ${esc(p.session.status)}` : ""}</p></div><button class="secondary" data-action="participant" data-id="${esc(p.id)}">${p.kind === "human" ? "View task" : p.session?.attached_available ? "Open terminal" : "Session details"}</button></article>`).join("") || "<p>No participants match this search.</p>"}</div></div>`;
}
function queuePage() {
  return `<div class="page"><h1 class="main-title">Needs you</h1><p class="subtitle">Respond as the human participant assigned to the task.</p>${state.data.actions.map((a) => `<div class="card"><h3>${esc(pretty(a.active.task))}</h3><p>${esc(project(a.project)?.project.name)} / ${esc(job(a.project, a.job)?.job.title)}</p><p class="small muted">Assigned role: ${esc(pretty(a.name))} · Input: ${esc(a.active.input.artifact || "Initial job goal")}</p><button class="primary" data-action="respond" data-id="${esc(a.id)}">Review & respond</button></div>`).join("") || '<div class="card">No human tasks are waiting.</div>'}</div>`;
}
function render() {
  const mainScroll = $("#main").scrollTop,
    sideScroll = $("#sidebar").scrollTop;
  const focused = document.activeElement,
    focusId = focused?.id,
    focusData = focused?.dataset?.action ? { ...focused.dataset } : null;
  navigation();
  sidebar();
  $("#main").innerHTML =
    state.tab === "projects"
      ? projectSummary()
      : state.tab === "participants"
        ? peoplePage()
        : state.tab === "queue"
          ? queuePage()
          : workspace(state.tab);
  $("#main").scrollTop = mainScroll;
  $("#sidebar").scrollTop = sideScroll;
  if (!document.contains(focused)) {
    const replacement = focusId
      ? document.getElementById(focusId)
      : focusData
        ? [...document.querySelectorAll("[data-action]")].find((e) =>
            Object.entries(focusData).every(
              ([key, value]) => e.dataset[key] === value,
            ),
          )
        : null;
    replacement?.focus({ preventScroll: true });
  }
  requestAnimationFrame(drawGraph);
  if (project(state.tab)) {
    if (state.views[state.tab] === "activity") loadActivity(state.tab);
    if (state.views[state.tab] === "code") loadCode(state.tab);
  }
}
async function loadActivity(pid) {
  const revision = state.revision,
    key = `activity:${pid}`;
  try {
    if (!state.cache.has(key))
      state.cache.set(key, await api(endpoint(pid) + "/activity"));
    if (
      state.tab !== pid ||
      state.views[pid] !== "activity" ||
      revision !== state.revision
    )
      return;
    $("#activity-content").innerHTML =
      `<div class="section-head"><h2>Accepted activity</h2><button class="secondary" data-action="refresh-view">Refresh</button></div>${
        state.cache
          .get(key)
          .map(
            (e) =>
              `<div class="activity-row"><strong>${esc(pretty(e.kind))}</strong> <small class="muted">${esc(e.at)} · ${esc(e.job || "Project")}</small>${Object.keys(e.detail).length ? `<pre>${esc(JSON.stringify(e.detail, null, 2))}</pre>` : ""}</div>`,
          )
          .join("") || "<p>No accepted events yet.</p>"
      }`;
  } catch (e) {
    if ($("#activity-content"))
      $("#activity-content").innerHTML =
        `<div class="error-box">${esc(e.message)}</div>`;
  }
}
async function loadCode(pid) {
  const j = selectedJob(pid),
    key = `files:${pid}:${j?.job.id}:${state.artifact || ""}`;
  if (!j) {
    $("#code-view").textContent = "Add and start a job to inspect its code.";
    return;
  }
  const selectedArtifact = state.artifact;
  try {
    if (!state.cache.has(key))
      state.cache.set(
        key,
        await api(
          endpoint(pid, j.job.id) +
            "/files" +
            (selectedArtifact ? `?artifact=${enc(selectedArtifact)}` : ""),
        ),
      );
    if (
      state.tab !== pid ||
      state.views[pid] !== "code" ||
      selectedJob(pid)?.job.id !== j.job.id ||
      state.artifact !== selectedArtifact
    )
      return;
    const listing = state.cache.get(key);
    if (!listing.files.includes(state.file)) state.file = listing.files[0];
    $("#code-view").innerHTML =
      `<div class="section-head"><p class="small muted">Version: ${esc(listing.version)}</p><div class="actions">${selectedArtifact ? '<button class="secondary" data-action="current-code">Current code</button>' : ""}<button class="secondary" data-action="refresh-view">Refresh</button></div></div><div class="code-layout"><nav class="file-tree" aria-label="Code files">${listing.files.map((name) => `<button data-action="file" data-name="${esc(name)}" class="${name === state.file ? "active" : ""}">${esc(name)}</button>`).join("") || "<p>No files in this version.</p>"}</nav><div class="code-content"><div class="code-toolbar" id="code-name">${esc(state.file || "")}</div><pre id="code-text">${state.file ? "Loading…" : ""}</pre></div></div>`;
    if (state.file) await loadFile(pid, j.job.id, state.file, selectedArtifact);
  } catch (e) {
    if ($("#code-view") && state.tab === pid)
      $("#code-view").innerHTML =
        `<div class="error-box">${esc(e.message)}</div>`;
  }
}
async function loadFile(pid, jid, name, artifact) {
  try {
    const result = await api(
      endpoint(pid, jid) +
        `/file?name=${enc(name)}${artifact ? `&artifact=${enc(artifact)}` : ""}`,
    );
    if (
      state.tab === pid &&
      selectedJob(pid)?.job.id === jid &&
      state.file === name &&
      state.artifact === artifact &&
      $("#code-text")
    )
      $("#code-text").textContent = result.text;
  } catch (e) {
    if ($("#code-text") && state.file === name)
      $("#code-text").textContent = e.message;
  }
}
function openProject(pid, jid) {
  if (!state.opened.includes(pid)) state.opened.push(pid);
  state.tab = pid;
  state.selected = pid;
  if (jid) state.jobs[pid] = jid;
  state.file = null;
  state.artifact = null;
  render();
  $("#main").scrollTop = 0;
}
function modal(title, body, onSubmit) {
  const dialog = $("#dialog");
  if (dialog.open) dialog.close();
  $("#dialog-content").innerHTML =
    `<div class="dialog-head"><h2>${esc(title)}</h2><button data-action="close-dialog" aria-label="Close dialog">×</button></div>${body}`;
  dialog.showModal();
  const form = dialog.querySelector("form");
  if (form && onSubmit)
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submit = event.submitter;
      if (submit) submit.disabled = true;
      let errorBox = form.querySelector(".form-error");
      if (!errorBox) {
        errorBox = document.createElement("p");
        errorBox.className = "form-error";
        errorBox.setAttribute("role", "alert");
        form.append(errorBox);
      }
      errorBox.textContent = "";
      try {
        await onSubmit(new FormData(form), form);
        dialog.close();
        await refresh(true);
      } catch (e) {
        errorBox.textContent = e.message;
      } finally {
        if (submit) submit.disabled = false;
      }
    });
}
const input = (label, name, value = "", extra = "") =>
  `<label>${esc(label)}<input name="${name}" value="${esc(value)}" ${extra}></label>`;
const textarea = (label, name, value = "", extra = "") =>
  `<label>${esc(label)}<textarea name="${name}" rows="4" ${extra}>${esc(value)}</textarea></label>`;
const formActions = (label) =>
  `<div class="actions"><button class="primary" type="submit">${esc(label)}</button><button class="secondary" type="button" data-action="close-dialog">Cancel</button></div><p class="form-error" role="alert"></p>`;
const lines = (value) =>
  String(value || "")
    .split("\n")
    .map((x) => x.trim())
    .filter(Boolean);
function newProject() {
  const projectId = id("project");
  modal(
    "New project",
    `<form class="form-grid">${input("Project name", "name", "", 'required maxlength="256"')}${input("Local codebase path", "root", "", 'required placeholder="/path/to/repository"')}${textarea("Goals — one per line", "goals", "", "required")}${textarea("Design document (optional)", "design", "", 'rows="7"')}<p class="form-help">This creates the project records. Add jobs to select their workflows and start agents.</p>${formActions("Create project")}</form>`,
    async (data) => {
      const p = await api("/api/projects", {
        project_id: projectId,
        name: data.get("name"),
        root: data.get("root"),
        goals: lines(data.get("goals")),
        design: data.get("design"),
      });
      state.selected = p.id;
      state.filter = "active";
      notify("Project created. Add its first job.");
    },
  );
}
async function newJob(pid) {
  const templates = await api("/api/templates"),
    jobId = id("job");
  const initial = templates.find((t) => t.name === "ship") || templates[0];
  modal(
    "Plan a job",
    `<form class="form-grid">${input("Job title", "title", "", 'required maxlength="256"')}${textarea("Goal for this job", "instructions", "", "required")}${textarea("Acceptance criteria — one per line", "acceptance")}<label>Workflow template<select id="workflow-template">${templates.map((t) => `<option ${t.name === initial?.name ? "selected" : ""}>${esc(t.name)}</option>`).join("")}<option value="custom">Custom YAML</option></select><span class="form-help">Review the participants, models, and provider permissions before starting the job.</span></label>${textarea("Workflow YAML", "workflow_yaml", initial?.yaml || "", 'required rows="10"')}<label>Depends on completed jobs<select name="depends_on" multiple size="3">${project(
      pid,
    )
      .jobs.map(
        (j) => `<option value="${esc(j.job.id)}">${esc(j.job.title)}</option>`,
      )
      .join(
        "",
      )}</select></label>${input("Base branch or commit", "base", "HEAD", "required")}${formActions("Create planned job")}</form>`,
    async (data) => {
      await api(endpoint(pid) + "/jobs", {
        job_id: jobId,
        title: data.get("title"),
        instructions: data.get("instructions"),
        acceptance: lines(data.get("acceptance")),
        workflow_yaml: data.get("workflow_yaml"),
        depends_on: data.getAll("depends_on"),
        base: data.get("base"),
      });
      state.jobs[pid] = jobId;
      notify("Job planned. Start it when ready.");
    },
  );
  $("#workflow-template").addEventListener("change", (e) => {
    const template = templates.find((t) => t.name === e.target.value);
    if (template)
      $("#dialog textarea[name=workflow_yaml]").value = template.yaml;
  });
}
function assignPR(pid, jid) {
  const pr = job(pid, jid).pr;
  modal(
    "Assign a pull request",
    `<form class="form-grid">${input("PR number", "number", pr?.number || "", 'type="number" min="1" required')}${input("Repository (owner/repository or host/owner/repository)", "repository", pr?.repository || "", "required")}${input("PR URL (optional)", "url", pr?.url || "", 'type="url"')}${formActions("Save PR assignment")}</form>`,
    async (data) => {
      await api(
        endpoint(pid, jid) + "/pr",
        {
          number: Number(data.get("number")),
          repository: data.get("repository"),
          url: data.get("url") || null,
        },
        "PUT",
      );
      notify("PR assignment saved.");
    },
  );
}
function taskDetails(pid, jid, name, executionId) {
  const j = job(pid, jid),
    t = j.job.workflow.tasks[name],
    visits = (j.run?.executions || []).filter((e) => e.task === name),
    e = visits.find((e) => e.id === executionId) || visits.at(-1);
  const participant = participants(pid, jid).find((p) => p.role === t.role);
  modal(
    pretty(name),
    `<p>${esc(pretty(t.role))} · ${esc(j.job.workflow.roles[t.role].kind)}</p><div class="doc-content">${esc(t.instructions)}</div><h3>Outcome routes</h3><div class="route-list">${Object.entries(
      t.transitions,
    )
      .map(([a, b]) => `<span>${esc(pretty(a))} → ${esc(pretty(b))}</span>`)
      .join(
        "",
      )}</div>${t.specialist_tasks.length ? `<p class="small">Allowed specialists: ${esc(t.specialist_tasks.map(pretty).join(", "))}</p>` : ""}<p class="small muted">Input verification: ${t.verify_input ? "all outcomes" : esc(t.verified_outcomes.join(", ") || "not required")} · ${visits.length} execution${visits.length === 1 ? "" : "s"}</p>${e ? `<h3>Execution ${esc(e.id)}</h3><p>${pill(e.status, e.outcome || e.status)} ${e.id !== visits.at(-1).id ? "<strong>Prior execution</strong>" : ""}</p><div class="doc-content">${esc(e.summary || "No outcome report yet.")}</div><p class="mono small">Input: ${esc(e.input.artifact || "Initial job goal")}</p><div class="actions">${e.input.artifact ? `<button class="secondary" data-action="artifact" data-project="${esc(pid)}" data-job="${esc(jid)}" data-artifact="${esc(e.input.artifact)}">Input manifest</button>` : ""}${e.artifact ? `<button class="secondary" data-action="artifact" data-project="${esc(pid)}" data-job="${esc(jid)}" data-artifact="${esc(e.artifact)}">Output manifest</button>` : ""}</div>` : "<p>This task has not been visited.</p>"}${visits.length > 1 ? `<h3>Execution history</h3>${visits.map((x) => `<p><button class="secondary" data-action="execution" data-project="${esc(pid)}" data-job="${esc(jid)}" data-execution="${esc(x.id)}">${esc(x.id)} · ${esc(x.outcome || x.status)}</button></p>`).join("")}` : ""}${participant ? `<div class="actions" style="margin-top:20px"><button class="primary" data-action="participant" data-id="${esc(participant.id)}">${participant.kind === "human" ? "View human task" : "Open agent session"}</button></div>` : ""}`,
  );
}
async function artifactDetails(pid, jid, reference) {
  const a = await api(endpoint(pid, jid) + `/artifacts/${enc(reference)}`);
  modal(
    "Execution artifact",
    `<p class="mono small">${esc(reference)}</p><dl class="status-grid"><dt>Execution</dt><dd>${esc(a.execution_id)}</dd><dt>HEAD</dt><dd>${esc(a.version.head)}</dd><dt>Worktree</dt><dd>${a.version.dirty ? "Contained changes" : "Clean"}</dd><dt>Files</dt><dd>${Object.keys(a.version.files).length}</dd></dl><button class="primary" data-action="artifact-code" data-project="${esc(pid)}" data-job="${esc(jid)}" data-artifact="${esc(reference)}">Browse this version</button><details><summary>Manifest details</summary><pre>${esc(JSON.stringify(a, null, 2))}</pre></details>`,
  );
}
function respond(personId) {
  const a = state.data.actions.find((a) => a.id === personId);
  if (!a) {
    notify("This participant has no pending human task.");
    return;
  }
  const t = a.task,
    e = a.active;
  let requestId = id("response"),
    submitted = null;
  modal(
    `Respond as ${pretty(a.name)}`,
    `<form class="form-grid"><p>${esc(project(a.project).project.name)} / ${esc(job(a.project, a.job).job.title)}</p><div class="doc-content">${esc(t.instructions)}</div><p class="small">Execution: <span class="mono">${esc(e.id)}</span><br>Input: <span class="mono">${esc(e.input.artifact || "Initial job goal")}</span></p>${e.input.summary ? `<details open><summary>Previous handoff</summary><pre>${esc(e.input.summary)}</pre></details>` : ""}<label>Outcome<select name="outcome">${Object.keys(
      t.transitions,
    )
      .map(
        (outcome) =>
          `<option value="${esc(outcome)}">${esc(pretty(outcome))}</option>`,
      )
      .join(
        "",
      )}</select></label>${textarea("Response and findings", "summary", "", 'required maxlength="16384"')}${t.specialist_tasks.length ? `<div><strong>Request a specialist (approved outcome only)</strong>${t.specialist_tasks.map((x) => `<label class="check"><input type="checkbox" name="specialists" value="${esc(x)}">${esc(pretty(x))}</label>`).join("")}</div>${input("Reason for specialist review", "specialist_reason")}` : ""}${t.requires_pr.length ? `<p class="form-help">PR identity is required for: ${esc(t.requires_pr.join(", "))}</p>${input("PR number", "pr_number", "", 'type="number" min="1"')}${input("PR repository", "pr_repository")}${input("PR URL", "pr_url", "", 'type="url"')}` : ""}<p class="form-help">Submitting acknowledges this assignment and reports against the exact input version above. Changed code or a newer execution will be rejected.</p>${formActions("Submit response")}</form>`,
    async (data) => {
      const pr = data.get("pr_number")
        ? {
            number: Number(data.get("pr_number")),
            repository: data.get("pr_repository"),
            url: data.get("pr_url") || null,
          }
        : null;
      const payload = {
        run_id: a.run_id,
        execution_id: e.id,
        participant: a.name,
        input_artifact: e.input.artifact || null,
        outcome: data.get("outcome"),
        summary: data.get("summary"),
        specialists: data.getAll("specialists"),
        specialist_reason: data.get("specialist_reason") || null,
        pr,
      };
      const current = JSON.stringify(payload);
      if (submitted !== current) {
        requestId = id("response");
        submitted = current;
      }
      await api(endpoint(a.project, a.job) + "/respond", {
        request_id: requestId,
        ...payload,
      });
      notify("Response accepted. The workflow can continue.");
    },
  );
}
function sessionDetails(p) {
  const s = p.session;
  modal(
    `${pretty(p.name)} session`,
    s
      ? `<dl class="status-grid"><dt>Process state</dt><dd>${esc(s.status)}</dd><dt>Activity</dt><dd>${esc(activityLabel(p))}</dd><dt>Generation</dt><dd>${s.generation}</dd><dt>Native session</dt><dd>${esc(s.native_id || "Not registered")}</dd></dl>${s.error ? `<div class="error-box">${esc(s.error)}</div>` : ""}<div class="actions">${s.attached_available ? `<button class="primary" data-action="terminal" data-id="${esc(p.id)}">Open terminal</button>` : ""}${["stop", "resume", "replace"].map((a) => `<button class="secondary" data-action="session-control" data-operation="${a}" data-id="${esc(p.id)}">${pretty(a)[0].toUpperCase() + pretty(a).slice(1)}</button>`).join("")}</div><p class="small muted">Stop ends the managed process. Resume retains its native conversation. Replace starts a new conversation after the live process is stopped.</p>${s.recovery ? `<details><summary>Recorded restart instructions</summary><pre>${esc(JSON.stringify(s.recovery, null, 2))}</pre></details>` : "<p>No agent-acknowledged restart instructions yet.</p>"}${s.interactions?.length ? `<h3>Recent conversation messages</h3>${s.interactions.map((m) => `<p><strong>${esc(m.status)}</strong> · ${esc(m.text)}${m.error ? `<br><small>${esc(m.error)}</small>` : ""}</p>`).join("")}` : ""}`
      : "<p>No managed session exists yet. The workflow launches this participant when its task becomes active.</p>",
  );
}
function jobControls(pid, jid) {
  const j = job(pid, jid),
    active = activeExecution(j);
  modal(
    "Job controls",
    `<form class="form-grid"><p>Cancel stops the owned agents. Retry creates a new execution of the current task after its agent has been stopped.</p><label>Operation<select name="action"><option value="cancel">Cancel job</option>${active ? '<option value="retry">Retry current execution</option>' : ""}</select></label>${textarea("Reason (required for retry)", "reason")}${formActions("Apply operation")}</form>`,
    async (data) => {
      await api(endpoint(pid, jid) + "/action", {
        action: data.get("action"),
        request_id: id("control"),
        execution_id: active?.id || null,
        reason: data.get("reason") || null,
      });
      notify("Job operation accepted.");
    },
  );
}
function fitTerminal() {
  if (!term || $("#terminal").hidden) return;
  try {
    fit.fit();
    if (socket?.readyState === WebSocket.OPEN)
      socket.send(
        JSON.stringify({
          type: "resize",
          cols: Math.max(20, Math.min(500, term.cols)),
          rows: Math.max(5, Math.min(200, term.rows)),
        }),
      );
  } catch {
    /* Layout can be transiently hidden during tab transitions. */
  }
}
function disconnectView() {
  if (socket) {
    socket.onclose = null;
    socket.onerror = null;
    socket.close();
    socket = null;
  }
}
function connectTerminal() {
  if (!attachment) return;
  disconnectView();
  const { project: pid, job: jid, session: s } = attachment;
  $("#terminal-status").textContent = "Attaching…";
  const ws = new WebSocket(
    `ws://${location.host}/api/terminal/${enc(pid)}/${enc(jid)}/${enc(s.id)}/${s.generation}`,
  );
  socket = ws;
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    if (socket !== ws) return;
    ws.send(JSON.stringify({ token: capability }));
    $("#terminal-status").textContent = "Connected";
    fitTerminal();
  };
  ws.onmessage = (event) => {
    if (socket === ws && event.data instanceof ArrayBuffer)
      term.write(new Uint8Array(event.data));
  };
  ws.onclose = (event) => {
    if (socket !== ws) return;
    $("#terminal-status").textContent =
      event.reason || "Disconnected — reconnect this view";
  };
  ws.onerror = () => {
    if (socket === ws) $("#terminal-status").textContent = "Connection failed";
  };
}
function openTerminal(p) {
  if (!p.session?.attached_available) {
    sessionDetails(p);
    return;
  }
  if ($("#dialog").open) $("#dialog").close();
  const same =
    attachment?.session.id === p.session.id &&
    attachment?.session.generation === p.session.generation &&
    attachment?.project === p.project &&
    attachment?.job === p.job;
  if (same) {
    $("#terminal").hidden = false;
    fitTerminal();
    return;
  }
  if (attachment) drafts.set(attachment.id, $("#conversation-text").value);
  disconnectView();
  if (term) term.dispose();
  attachment = structuredClone(p);
  $("#terminal").hidden = false;
  $("#terminal-title").textContent =
    `${pretty(p.name)} · ${job(p.project, p.job)?.job.title} · g${p.session.generation}`;
  $("#conversation").hidden = p.session.input_mode !== "native";
  $("#conversation-text").value = drafts.get(p.id) || "";
  $("#terminal-output").replaceChildren();
  term = new Terminal({
    fontSize: 15,
    fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace",
    lineHeight: 1.25,
    scrollback: 10000,
    cursorBlink: true,
    convertEol: false,
    disableStdin: p.session.input_mode === "native",
    theme: { background: "#101c28", foreground: "#e3edf3", cursor: "#83d8ba" },
  });
  fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($("#terminal-output"));
  term.onData((data) => {
    if (
      socket?.readyState === WebSocket.OPEN &&
      attachment.session.input_mode !== "native"
    )
      socket.send(JSON.stringify({ type: "input", data }));
  });
  connectTerminal();
  updateTerminalState();
  requestAnimationFrame(fitTerminal);
}
function closeTerminal() {
  if (attachment) drafts.set(attachment.id, $("#conversation-text").value);
  if (fullscreen) setFullscreen(false);
  disconnectView();
  $("#terminal").hidden = true;
  if (term) {
    term.dispose();
    term = null;
    fit = null;
  }
  const personId = attachment?.id;
  attachment = null;
  const opener = [
    ...document.querySelectorAll("[data-action=participant]"),
  ].find((e) => e.dataset.id === personId);
  opener?.focus({ preventScroll: true });
}
function setFullscreen(enabled) {
  if (!attachment) return;
  fullscreen = enabled;
  if (enabled) {
    terminalFocus = document.activeElement;
    $("#fullscreen-root").append($("#terminal"));
    $("#app").inert = true;
    $("#fullscreen-button").textContent = "Restore";
    $("#fullscreen-button").focus({ preventScroll: true });
    $("#terminal").setAttribute("role", "dialog");
    $("#terminal").setAttribute("aria-modal", "true");
  } else {
    $("#terminal-home").append($("#terminal"));
    $("#app").inert = false;
    $("#fullscreen-button").textContent = "Full screen";
    $("#terminal").removeAttribute("role");
    $("#terminal").removeAttribute("aria-modal");
    if (terminalFocus?.isConnected)
      terminalFocus.focus({ preventScroll: true });
  }
  requestAnimationFrame(fitTerminal);
}
function updateTerminalState() {
  if (!attachment) return;
  const p = state.data.participants.find((p) => p.id === attachment.id),
    current = p?.session;
  const valid =
    current?.generation === attachment.session.generation &&
    current?.id === attachment.session.id;
  if (!valid) {
    disconnectView();
    $("#terminal-status").textContent =
      "Session changed — reopen from Participants";
  }
  const send = $("#conversation button[type=submit]");
  send.disabled =
    !!attachment.sending ||
    !valid ||
    !current?.ready ||
    current?.status !== "ready" ||
    current?.recovering;
  const recent = current?.interactions?.slice(-3) || [];
  $("#conversation-status").textContent = recent
    .map(
      (m) =>
        `${m.status}: ${m.text.slice(0, 75)}${m.status === "uncertain" ? " — inspect before resending" : ""}`,
    )
    .join(" · ");
}
$("#conversation").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!attachment) return;
  const input = $("#conversation-text"),
    text = input.value;
  if (!text.trim()) return;
  const current = attachment,
    button = $("#conversation button[type=submit]");
  if (current.sending) return;
  current.sending = true;
  button.disabled = true;
  // Retain an idempotency key with this exact draft if the response is interrupted.
  if (current.pending?.text !== text)
    current.pending = { text, id: id("message") };
  try {
    await api(
      endpoint(current.project, current.job) +
        `/sessions/${enc(current.session.id)}`,
      {
        action: "interact",
        generation: current.session.generation,
        request_id: current.pending.id,
        text,
      },
    );
    if (attachment === current && input.value === text) {
      input.value = "";
      drafts.delete(current.id);
    }
    delete current.pending;
    notify("Message queued for this conversation.");
    await refresh(true);
  } catch (e) {
    notify(e.message);
  } finally {
    current.sending = false;
    updateTerminalState();
  }
});
function installResize(handle, axis, initial, key) {
  let value = Number(readPreference(key, initial));
  if (!Number.isFinite(value)) value = initial;
  const bounds = () =>
    axis === "x"
      ? [
          window.innerWidth < 700 ? 180 : 230,
          Math.max(230, Math.min(600, window.innerWidth - 420)),
        ]
      : [240, Math.max(240, window.innerHeight - 225)];
  const apply = (next) => {
    const [min, max] = bounds();
    value = Math.round(Math.max(min, Math.min(max, next)));
    document.documentElement.style.setProperty(
      axis === "x" ? "--sidebar" : "--terminal-height",
      value + "px",
    );
    handle.setAttribute("aria-valuenow", value);
    handle.setAttribute("aria-valuemin", min);
    handle.setAttribute("aria-valuemax", max);
    requestAnimationFrame(() => {
      fitTerminal();
      drawGraph();
    });
  };
  let drag = null;
  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    drag = { start: axis === "x" ? event.clientX : event.clientY, value };
    handle.setPointerCapture(event.pointerId);
    document.body.classList.add("resizing");
    event.preventDefault();
    handle.focus();
  });
  handle.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const distance =
      (axis === "x" ? event.clientX : event.clientY) - drag.start;
    apply(drag.value + (axis === "x" ? distance : -distance));
  });
  const end = () => {
    if (!drag) return;
    drag = null;
    document.body.classList.remove("resizing");
    preference(key, value);
  };
  handle.addEventListener("pointerup", end);
  handle.addEventListener("pointercancel", end);
  handle.addEventListener("lostpointercapture", end);
  handle.addEventListener("keydown", (event) => {
    const [min, max] = bounds(),
      step = event.shiftKey ? 40 : 12;
    let next;
    if (event.key === "Home") next = min;
    else if (event.key === "End") next = max;
    else if (event.key === "ArrowLeft" && axis === "x") next = value - step;
    else if (event.key === "ArrowRight" && axis === "x") next = value + step;
    else if (event.key === "ArrowUp" && axis === "y") next = value + step;
    else if (event.key === "ArrowDown" && axis === "y") next = value - step;
    else return;
    event.preventDefault();
    apply(next);
    preference(key, value);
  });
  window.addEventListener("resize", () => apply(value));
  apply(value);
}
installResize($("#sidebar-resizer"), "x", 296, "ctlrm-sidebar-width");
installResize($("#terminal-resizer"), "y", 360, "ctlrm-terminal-height");
new ResizeObserver(() => requestAnimationFrame(fitTerminal)).observe(
  $("#terminal-output"),
);
$("#search").addEventListener("input", (event) => {
  state.search = event.target.value;
  render();
});
document.addEventListener("change", (event) => {
  if (event.target.id === "project-filter") {
    state.filter = event.target.value;
    state.selected = matchingProjects()[0]?.project.id || null;
    state.tab = "projects";
    render();
  }
  if (event.target.id === "job-picker") {
    state.jobs[event.target.dataset.project] = event.target.value;
    state.file = null;
    state.artifact = null;
    render();
  }
});
document.addEventListener("dblclick", (event) => {
  const row = event.target.closest("[data-action=select-project]");
  if (row) openProject(row.dataset.id);
});
document.addEventListener("keydown", (event) => {
  if ($("#dialog").open) return;
  if (fullscreen) {
    if (event.key === "Escape") {
      event.preventDefault();
      setFullscreen(false);
      return;
    }
    if (event.key === "Tab") {
      const focusable = [
        ...$("#terminal").querySelectorAll(
          'button:not([disabled]),textarea:not([disabled]),[tabindex="0"]',
        ),
      ].filter((e) => e.getClientRects().length);
      const first = focusable[0],
        last = focusable.at(-1);
      if (
        event.shiftKey &&
        (document.activeElement === first ||
          !$("#terminal").contains(document.activeElement))
      ) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    }
    return;
  }
  if (
    (event.ctrlKey || event.metaKey) &&
    event.key.toLowerCase() === "k" &&
    !$("#dialog").open
  ) {
    event.preventDefault();
    $("#search").focus();
    $("#search").select();
  }
});
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  const d = button.dataset,
    action = d.action;
  try {
    if (action === "tab") {
      state.tab = d.id;
      render();
    } else if (action === "select-project") {
      state.selected = d.id;
      state.tab = "projects";
      render();
    } else if (action === "open-project") openProject(d.id);
    else if (action === "open-job") {
      openProject(d.project, d.id);
      state.views[d.project] = "overview";
      render();
    } else if (action === "close-project") {
      state.opened = state.opened.filter((p) => p !== d.id);
      if (state.tab === d.id) state.tab = "projects";
      render();
    } else if (action === "view") {
      state.views[state.tab] = d.view;
      state.file = null;
      state.artifact = null;
      render();
    } else if (action === "project-view") {
      state.views[d.project] = d.view;
      openProject(d.project);
    } else if (action === "new-project") newProject();
    else if (action === "new-job") await newJob(d.project);
    else if (action === "close-dialog") $("#dialog").close();
    else if (action === "assign-pr") assignPR(d.project, d.job);
    else if (action === "respond") respond(d.id);
    else if (action === "task") taskDetails(d.project, d.job, d.task);
    else if (action === "execution") {
      const e = job(d.project, d.job)?.run.executions.find(
        (e) => e.id === d.execution,
      );
      if (e) taskDetails(d.project, d.job, e.task, e.id);
    } else if (action === "artifact")
      await artifactDetails(d.project, d.job, d.artifact);
    else if (action === "artifact-code") {
      if ($("#dialog").open) $("#dialog").close();
      openProject(d.project, d.job);
      state.views[d.project] = "code";
      state.artifact = d.artifact;
      render();
    } else if (action === "current-code") {
      state.artifact = null;
      state.file = null;
      render();
    } else if (action === "file") {
      state.file = d.name;
      const j = selectedJob(state.tab);
      $("#code-name").textContent = d.name;
      document
        .querySelectorAll(".file-tree button")
        .forEach((b) =>
          b.classList.toggle("active", b.dataset.name === d.name),
        );
      await loadFile(state.tab, j.job.id, d.name, state.artifact);
    } else if (action === "participant") {
      const p = state.data.participants.find((p) => p.id === d.id);
      if (p?.kind === "human") respond(p.id);
      else if (p?.session?.attached_available) openTerminal(p);
      else if (p) sessionDetails(p);
    } else if (action === "terminal") {
      const p = state.data.participants.find((p) => p.id === d.id);
      if (p) openTerminal(p);
    } else if (action === "close-terminal") closeTerminal();
    else if (action === "fullscreen") setFullscreen(!fullscreen);
    else if (action === "terminal-details") {
      const p = state.data.participants.find((p) => p.id === attachment?.id);
      if (p) sessionDetails(p);
    } else if (action === "reconnect") {
      term?.reset();
      connectTerminal();
    } else if (action === "start") {
      button.disabled = true;
      await api(endpoint(d.project, d.job) + "/start", {});
      notify("Job started in its managed worktree.");
      await refresh(true);
    } else if (action === "job-controls") jobControls(d.project, d.job);
    else if (action === "session-control") {
      const p = state.data.participants.find((p) => p.id === d.id),
        requestId = id("session");
      modal(
        `${pretty(d.operation)} ${pretty(p.name)}`,
        `<form class="form-grid"><p>${d.operation === "replace" ? "This starts a new native conversation. Queued messages from the old conversation will be canceled." : d.operation === "stop" ? "This stops the managed agent process. Its recorded conversation can be resumed later." : "This resumes the recorded native conversation. A live session must be stopped first."}</p>${formActions(pretty(d.operation))}</form>`,
        async () => {
          await api(
            endpoint(p.project, p.job) + `/sessions/${enc(p.session.id)}`,
            {
              action: d.operation,
              generation: p.session.generation,
              request_id: requestId,
            },
          );
          notify("Session operation accepted.");
        },
      );
    } else if (action === "archive") {
      modal(
        "Archive project",
        `<form class="form-grid"><p>All jobs must be completed. Control room will retire their agents, retain the accepted code, and create a verified ZIP. Project metadata remains searchable.</p>${formActions("Archive project")}</form>`,
        async () => {
          await api(endpoint(d.project) + "/archive", {});
          notify("Project archived.");
        },
      );
    } else if (action === "refresh") await refresh(true);
    else if (action === "refresh-view") {
      state.cache.clear();
      render();
    }
  } catch (error) {
    notify(error.message);
  } finally {
    if (button.isConnected) button.disabled = false;
  }
});
window.addEventListener("beforeunload", () => {
  disconnectView();
});
render();
refresh();
setInterval(refresh, 2500);
