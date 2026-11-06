/*
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 上海商江网络科技有限公司版权所有
软工云（swtool.com）开发团队；uqtool@163.com
*/
/* Shared native distribution / host federation workspace. Remote secrets never enter this client. */
const hostBridge = window.SWToolRequirementsHost?.active ? window.SWToolRequirementsHost : null;
const createPage = location.pathname.endsWith("/requirements-center-create.html") || new URLSearchParams(location.search).get("create") === "1";
const hubHomePath = hostBridge ? "/requirements-center.html" : "/requirements-hub.html";
const hubCreatePath = hostBridge ? "/requirements-center.html?create=1" : "/requirements-center-create.html";
const legacySettings = !createPage && new URLSearchParams(location.search).get("settings") === "1";
if (legacySettings) location.replace(hubCreatePath);
let token = hostBridge ? hostBridge.session() : (localStorage.getItem("ws_token") || ""),
  requirementsActive = true;
let bootFlight = null, bootSession = "";
class WorkspaceChangedError extends Error {
  constructor() {
    super("workspace changed");
    this.name = "WorkspaceChangedError";
  }
}
const isWorkspaceChanged = (error) => error?.name === "WorkspaceChangedError";
const routeView = () => "issues";
const hub = {
  user: null,
  identity: null,
  center: "",
  localCenter: null,
  generation: 0,
  page: 1,
  pages: 1,
  selected: "",
  justCreated: null,
  sorting: false,
  dragTicket: null,
  listRequestSeq: 0,
  urls: [],
  connections: [],
};
const el = (id) => document.getElementById(id),
  esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
const originalDetailHome = el("hub-detail")?.parentElement;
const detailHome = () => originalDetailHome;
function placeMobileDetail() {
  const detail = el("hub-detail");
  const home = detailHome();
  if (!detail || !home) return;
  const mobile = window.matchMedia?.("(max-width: 760px)").matches;
  const selected = mobile && hub.selected
    ? document.querySelector(`#hub-list button[data-ticket-id="${CSS.escape(hub.selected)}"]`)
    : null;
  const target = selected?.closest("li");
  if (target) {
    target.append(detail);
    detail.classList.add("hub-detail-inline");
  } else {
    home.append(detail);
    detail.classList.remove("hub-detail-inline");
  }
}
const states = { pending: "待审核", open: "待解决", in_progress: "处理中", resolved: "已解决", withdrawn: "已撤回" },
  time = (value) =>
    value
      ? new Date(
          typeof value === "number" ? value * 1000 : value,
        ).toLocaleString("zh-CN")
      : "—";
function notify(message) {
  el("hub-message").textContent = message || "";
  if (el("hub-dialog").open) {
    let node = el("hub-dialog-message");
    if (!node) {
      node = document.createElement("p");
      node.id = "hub-dialog-message";
      node.setAttribute("role", "status");
      el("hub-dialog-body").prepend(node);
    }
    node.textContent = message || "";
  }
}
async function request(path, options = {}, remote = true) {
  if (remote && hub.localCenter && /^\/api\/tickets(?:[/?]|$)/.test(path))
    path = "/api/local-centers/" + encodeURIComponent(hub.localCenter.id) + path.slice(4);
  if (remote && hub.center && options.method && options.method !== "GET" && !canWrite())
    throw new Error("远端 Key 未获写入能力，请轮换授权 Key 或在目标中心操作");
  const generation = hub.generation,
    session = token;
  if (hostBridge) {
    const response = await hostBridge.request(path, options, remote);
    if ((remote && generation !== hub.generation) || session !== token)
      throw new WorkspaceChangedError();
    if (!response.ok) {
      let message = "请求失败（" + response.status + ")";
      try { const data = await response.json(); message = data.error || data.detail || message; } catch {}
      if ((remote && generation !== hub.generation) || session !== token) throw new WorkspaceChangedError();
      const error = Error(message);
      error.status = response.status;
      throw error;
    }
    return response;
  }
  const pluginPrefix=location.pathname.startsWith('/extensions/requirements-center/')&&path.startsWith('/api/')&&path!=='/api/plugins/requirements-center'?'/extensions/requirements-center/api':'';
  const url =
    "/api/v1" + pluginPrefix +
    (remote && hub.center
      ? "/api/requirements/connections/" +
        encodeURIComponent(hub.center) +
        "/proxy" +
        path
      : path);
  const response = await fetch(url, {
    ...options,
    headers: { ...(options.headers || {}), Authorization: "Bearer " + session },
  });
  if ((remote && generation !== hub.generation) || session !== token)
    throw new WorkspaceChangedError();
  if (!response.ok) {
    let message = "请求失败（" + response.status + "）";
    try {
      const data = await response.json();
      message = typeof data.detail === "string" ? data.detail : message;
    } catch {}
    if ((remote && generation !== hub.generation) || session !== token)
      throw new WorkspaceChangedError();
    const error = Error(message);
    error.status = response.status;
    throw error;
  }
  return response;
}
async function api(path, options = {}) {
  const hostTicket = hostBridge && !hub.center && !hub.localCenter && /^\/api\/tickets(?:[/?]|$)/.test(path);
  return readJson(path, options, !hostTicket);
}
const json = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
async function local(path, options = {}) {
  return readJson(path, options, false);
}
const ticketApi = (path, options = {}) => hostBridge && !hub.center && !hub.localCenter ? request(path, options, false).then((response) => response.json()) : api(path, options);
let driverTimer = null, driverLogTimer = null, driverProgressTimer = null, driverView = null, confirmedHostKey = null;
let driverExpanded = false, driverTicket = null;
let driverInterjectState = {identity:"", sending:false, message:""};
let driverViewContext = "";
let driverProgressFlight = null, driverProgressContext = "", driverProgressItems = [], driverProgressTotal = 0, driverProgressError = "";
let driverLogContext = "", driverLogRunId = null, driverLogCursor = 0, driverLogText = "", driverLogAtBottom = true, driverLogInitialized = false;
let driverLogFlight = null, driverLogSnapshot = "";
let driverTerminalChunks = [], driverEventKeys = new Set(), driverErrorKey = "", driverStatus = "", driverEventsInitialized = false;
const driverDrafts = new Map();
const driverPath = () => hub.localCenter ? "/api/local-centers/" + encodeURIComponent(hub.localCenter.id) + "/driver" : "/api/requirements/driver";
const driverDraftKey = () => JSON.stringify([token, driverPath()]);
function driverLogContextKey() {
  return JSON.stringify([token, hub.user?.id || "", hub.generation, hub.center, hub.localCenter?.id || ""]);
}
function driverInterjectIdentity() {
  return JSON.stringify([driverLogContextKey(), driverPath(), driverView?.config?.target_kind || "",
    driverView?.config?.ssh_target_id || "", driverView?.config?.current_ticket_id || "",
    driverView?.config?.run_session_id || ""]);
}
function updateDriverInterjectState() {
  const identity = driverInterjectIdentity();
  if (driverInterjectState.identity !== identity) driverInterjectState = {identity, sending:false, message:""};
  const ready = Boolean(driverViewContext === driverLogContextKey() && ["running", "retry_wait"].includes(driverView?.driver?.status)
    && driverView?.config?.current_ticket_id && driverView?.config?.run_session_id);
  el("hub-driver-interject-text").disabled = !ready;
  el("hub-driver-interject-send").disabled = !ready || driverInterjectState.sending;
  el("hub-driver-interject-status").textContent = driverInterjectState.message
    || (ready ? "发送后等待当前操作结束，再由 CLI 处理" : "等待活动工单");
}
function driverTerminalTail(value, limit = 1048576) {
  const bytes = new TextEncoder().encode(String(value || ""));
  let start = Math.max(0, bytes.length - limit);
  while (start < bytes.length && (bytes[start] & 0xc0) === 0x80) start++;
  return new TextDecoder().decode(bytes.slice(start));
}
function renderDriverTerminal(shouldScroll = driverLogAtBottom) {
  let remaining = 1048576;
  for (let i = driverTerminalChunks.length - 1; i >= 0; i--) {
    const chunk = driverTerminalChunks[i];
    chunk.text = driverTerminalTail(chunk.text, remaining);
    remaining -= new TextEncoder().encode(chunk.text).length;
    if (remaining === 0 || !chunk.text) {
      driverTerminalChunks.splice(0, chunk.text ? i : i + 1);
      break;
    }
  }
  driverLogText = driverTerminalChunks.filter(chunk => chunk.kind === "cli").map(chunk => chunk.text).join("");
  const output = el("hub-driver-log-output");
  if (!output) return;
  output.textContent = driverTerminalChunks.map(chunk => chunk.text).join("");
  if (shouldScroll) output.scrollTop = output.scrollHeight;
}
function resetDriverTerminal() {
  driverTerminalChunks = [];
  driverEventKeys = new Set();
  driverErrorKey = "";
  driverStatus = "";
  driverLogRunId = null;
  driverLogCursor = 0;
  driverLogInitialized = false;
  driverLogSnapshot = "";
  driverEventsInitialized = false;
  driverLogAtBottom = true;
  renderDriverTerminal();
}
function ensureDriverLogContext() {
  const contextKey = driverLogContextKey();
  if (contextKey !== driverLogContext) {
    driverLogContext = contextKey;
    resetDriverTerminal();
    el("hub-driver-log-meta").textContent = "等待日志";
  }
}
function appendDriverTerminal(kind, text) {
  if (!text) return;
  const last = driverTerminalChunks.at(-1);
  if (kind === "event" && last && !last.text.endsWith("\n")) text = "\n" + text;
  if (last?.kind === kind) last.text += text;
  else driverTerminalChunks.push({kind, text});
  renderDriverTerminal();
}
function appendDriverEvents(data) {
  ensureDriverLogContext();
  const occurrences = new Map();
  if (!driverEventsInitialized) {
    for (const event of data.events || []) {
      const identity = JSON.stringify(event);
      const occurrence = (occurrences.get(identity) || 0) + 1;
      occurrences.set(identity, occurrence);
      driverEventKeys.add(event.storage_event_id ? JSON.stringify(["storage", event.storage_event_id]) : event.event_id || event.id || JSON.stringify([identity, occurrence]));
    }
    driverStatus = data.driver?.status || "";
    const code = data.driver?.last_error_code;
    driverErrorKey = /^[a-z][a-z0-9_]{0,79}$/.test(code || "") ? JSON.stringify([data.driver.status, code]) : "";
    driverEventsInitialized = true;
    return;
  }
  for (const event of data.events || []) {
    const identity = JSON.stringify(event);
    const occurrence = (occurrences.get(identity) || 0) + 1;
    occurrences.set(identity, occurrence);
    const key = event.storage_event_id ? JSON.stringify(["storage", event.storage_event_id]) : event.event_id || event.id || JSON.stringify([identity, occurrence]);
    if (driverEventKeys.has(key)) continue;
    driverEventKeys.add(key);
    const storedDate = typeof event.storage_at === "number" && Number.isFinite(event.storage_at) ? new Date(event.storage_at * 1000) : null;
    const timePrefix = storedDate && Number.isFinite(storedDate.getTime()) ? "[" + storedDate.toLocaleString() + "] " : "[历史时间未知] ";
    const code = /^[a-z][a-z0-9_]{0,79}$/.test(event.code || "") ? event.code : "";
    appendDriverTerminal("event", timePrefix + [event.type || event.stage || "里程碑", event.ticket_id, event.commit || event.reverted, code]
      .filter(Boolean).join(" · ").replace(/[\r\n\x00-\x1f\x7f]/g, " ") + "\n");
    const detail = String(event.detail || "").replace(/[\r\n\x00-\x1f\x7f]/g, " ").trim().slice(0, 240);
    if (detail) appendDriverTerminal("event", timePrefix + "错误详情：" + detail + "\n");
  }
  // Keep a bounded identity history even if a later response contains an older event window.
  while (driverEventKeys.size > 4096) driverEventKeys.delete(driverEventKeys.values().next().value);
  const status = data.driver?.status || "";
  if (status && status !== "stopped" && status !== driverStatus && /^[a-z][a-z0-9_]{0,79}$/.test(status) &&
      !(data.events || []).some(event => (event.type || event.stage) === status))
    appendDriverTerminal("event", status + "\n");
  driverStatus = status;
  const code = data.driver?.last_error_code;
  const errorKey = /^[a-z][a-z0-9_]{0,79}$/.test(code || "") ? JSON.stringify([data.driver.status, code]) : "";
  const eventWithDetail = (data.events || []).some(event => event.code === code && event.detail);
  if (errorKey && errorKey !== driverErrorKey && !(data.events || []).some(event => event.code === code)) {
    appendDriverTerminal("event", "暂停原因：" + code + "\n");
    const detail = String(data.driver?.last_error_detail || "").replace(/[\r\n\x00-\x1f\x7f]/g, " ").trim().slice(0, 240);
    if (detail) appendDriverTerminal("event", "错误详情：" + detail + "\n");
  } else if (errorKey && errorKey !== driverErrorKey && !eventWithDetail) {
    const detail = String(data.driver?.last_error_detail || "").replace(/[\r\n\x00-\x1f\x7f]/g, " ").trim().slice(0, 240);
    if (detail) appendDriverTerminal("event", "错误详情：" + detail + "\n");
  }
  driverErrorKey = errorKey;
}
const needsInfoLabels = new Set(["待补充", "待补充信息", "needs-info", "needs_info"]);
function ticketNeedsInfo(ticket) {
  return (Array.isArray(ticket.labels) ? ticket.labels : []).some(label => needsInfoLabels.has(String(label).trim().toLowerCase()));
}
function driverTicketCreatedAt(ticket) {
  const value = ticket?.created_at;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value !== "string" || !value.trim()) return null;
  const numeric = Number(value);
  if (Number.isFinite(numeric)) return numeric;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed / 1000 : null;
}
function compareDriverTicketsByCreatedAt(a, b) {
  const aCreated = driverTicketCreatedAt(a), bCreated = driverTicketCreatedAt(b);
  if (aCreated !== null && bCreated !== null && aCreated !== bCreated) return aCreated - bCreated;
  if ((aCreated === null) !== (bCreated === null)) return aCreated === null ? 1 : -1;
  return String(a.id).localeCompare(String(b.id));
}
function renderDriverProgress(data = driverView) {
  const root = el("hub-driver-progress-list"), meta = el("hub-driver-progress-meta");
  if (!root || !meta) return;
  if (driverProgressError) {
    meta.textContent = "同步失败";
    root.replaceChildren(Object.assign(document.createElement("p"), {className:"muted", textContent:driverProgressError}));
    return;
  }
  meta.textContent = driverProgressItems.length ? `${driverProgressTotal} 张工单` : "暂无工单";
  root.replaceChildren();
  const state = data?.driver || {}, active = ["running", "retry_wait", "interrupted"].includes(state.status);
  const claimEvent = active ? [...(data?.events || [])].reverse().find(event => event.ticket_id && ["ticket_claimed", "running"].includes(event.type || event.stage)) : null;
  const currentId = String(state.current_ticket_id || data?.config?.current_ticket_id || claimEvent?.ticket_id || "");
  const tickets = driverProgressItems.filter(item => item && item.id != null);
  const current = tickets.find(item => String(item.id) === currentId && item.status === "open" && !ticketNeedsInfo(item));
  const complete = tickets.filter(item => item.status === "resolved");
  const waiting = tickets.filter(item => item.status === "open" && !ticketNeedsInfo(item) && String(item.id) !== String(current?.id || ""))
    .sort(compareDriverTicketsByCreatedAt);
  const groups = [
    {title: "正在处理", items: current ? [current] : [], kind: "current", icon: ""},
    {title: "待处理", items: waiting, kind: "waiting", icon: "◷"},
    {title: "已完成", items: complete, kind: "complete", icon: "✓"},
  ];
  for (const group of groups) {
    const section = document.createElement("section"), heading = document.createElement("h4"), list = document.createElement("ul");
    section.className = "hub-driver-progress-group";
    heading.textContent = `${group.title} · ${group.items.length}`;
    section.append(heading);
    for (const ticket of group.items) {
      const row = document.createElement("li"), icon = document.createElement("span"), label = document.createElement("span"), title = String(ticket.title || "未命名工单");
      row.className = `hub-driver-progress-item is-${group.kind}${group.kind === "current" && active ? " is-active" : ""}`;
      icon.className = "hub-driver-progress-icon";
      icon.setAttribute("aria-hidden", "true");
      if (group.kind === "current") {
        icon.setAttribute("role", "status");
        icon.setAttribute("aria-label", "正在处理");
      } else {
        icon.textContent = group.icon;
      }
      label.append(document.createTextNode(title));
      const id = document.createElement("small");
      id.textContent = String(ticket.id);
      label.append(id);
      row.append(icon, label);
      list.append(row);
    }
    if (!group.items.length) {
      const row = document.createElement("li");
      row.className = "muted";
      row.textContent = group.kind === "current" && active ? "正在等待领取工单" : "暂无工单";
      list.append(row);
    }
    section.append(list);
    root.append(section);
  }
  if (!driverProgressItems.length && driverProgressTotal === 0) {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = "暂无可显示的工单；待补充工单不会进入待处理列表。";
    root.append(note);
  }
}
async function refreshDriverProgress() {
  const panel = el("hub-driver-panel"), generation = hub.generation, session = token;
  if (!panel || panel.hidden || !hub.user) return;
  const contextKey = driverLogContextKey();
  if (driverProgressFlight?.contextKey === contextKey) return;
  clearTimeout(driverProgressTimer);
  if (contextKey !== driverProgressContext) {
    driverProgressContext = contextKey;
    driverProgressItems = [];
    driverProgressTotal = 0;
    driverProgressError = "";
    el("hub-driver-progress-meta").textContent = "同步中";
    el("hub-driver-progress-list").replaceChildren(Object.assign(document.createElement("p"), {className:"muted", textContent:"正在读取工单…"}));
  }
  const flight = {contextKey};
  driverProgressFlight = flight;
  try {
    const first = await api("/api/tickets?page=1&page_size=100&q=");
    if (generation !== hub.generation || session !== token || driverProgressFlight !== flight) return;
    const pages = Math.min(1000, Math.max(1, Number(first.total_pages) || 1)), items = [...(first.items || first.tickets || [])];
    for (let start = 2; start <= pages; start += 4) {
      const batch = await Promise.all(Array.from({length:Math.min(4, pages - start + 1)}, (_, index) =>
        api("/api/tickets?page=" + (start + index) + "&page_size=100&q=")));
      if (generation !== hub.generation || session !== token || driverProgressFlight !== flight) return;
      for (const page of batch) items.push(...(page.items || page.tickets || []));
    }
    driverProgressItems = items;
    driverProgressTotal = Number(first.total) || items.length;
    driverProgressError = "";
    renderDriverProgress();
  } catch (error) {
    if (generation !== hub.generation || session !== token || driverProgressFlight !== flight || isWorkspaceChanged(error)) return;
    driverProgressError = "工单列表暂不可用";
    renderDriverProgress();
  } finally {
    if (driverProgressFlight === flight) driverProgressFlight = null;
    if (generation === hub.generation && session === token && contextKey === driverProgressContext && !document.hidden && !panel.hidden)
      driverProgressTimer = setTimeout(refreshDriverProgress, 30000);
  }
}
async function refreshDriverLog() {
  const panel = el("hub-driver-panel"), generation = hub.generation, session = token;
  if (!panel || panel.hidden || !hub.user) return;
  const contextKey = driverLogContextKey();
  if (driverLogFlight?.contextKey === contextKey) return;
  clearTimeout(driverLogTimer);
  ensureDriverLogContext();
  const flight = {contextKey};
  driverLogFlight = flight;
  try {
    for (let page = 0; page < 4; page++) {
      const query = "?cursor=" + encodeURIComponent(String(driverLogCursor)) +
        (driverLogRunId ? "&run_id=" + encodeURIComponent(driverLogRunId) : "");
      const data = await driverRequest("/logs" + query);
      if (generation !== hub.generation || session !== token || contextKey !== driverLogContextKey() || driverLogFlight !== flight) return;
      const runId = data.run_id || null;
      const runChanged = runId !== driverLogRunId;
      if (runChanged) {
        driverLogRunId = runId;
        driverLogCursor = 0;
        driverLogInitialized = false;
        driverLogSnapshot = "";
        driverTerminalChunks = driverTerminalChunks.filter(chunk => chunk.kind !== "cli");
        driverLogAtBottom = true;
      }
      const nextText = String(data.text || "");
      if (data.reset) {
        driverTerminalChunks = driverTerminalChunks.filter(chunk => chunk.kind !== "cli");
        driverLogSnapshot = "";
      }
      if (driverLogInitialized) appendDriverTerminal("cli", nextText);
      else driverLogSnapshot = driverTerminalTail(driverLogSnapshot + nextText);
      renderDriverTerminal();
      if (Number.isSafeInteger(data.cursor) && data.cursor >= 0) driverLogCursor = data.cursor;
      const details = [data.tool, data.ticket_id && "工单 " + data.ticket_id, runId && "更新于 " + time(new Date().toISOString())].filter(Boolean);
      el("hub-driver-log-meta").textContent = details.join(" · ") || "暂无运行日志";
      if (data.has_more !== true) {
        if (!driverLogInitialized) {
          appendDriverTerminal("cli", driverLogSnapshot);
          driverLogSnapshot = "";
        }
        driverLogInitialized = true;
        break;
      }
    }
  } catch (error) {
    if (generation !== hub.generation || session !== token || contextKey !== driverLogContextKey() || driverLogFlight !== flight || isWorkspaceChanged(error)) return;
    el("hub-driver-log-meta").textContent = "日志暂不可用，稍后重试";
  } finally {
    if (driverLogFlight === flight) driverLogFlight = null;
    if (generation === hub.generation && session === token && contextKey === driverLogContextKey() && !document.hidden && !panel.hidden)
      driverLogTimer = setTimeout(refreshDriverLog, 2000);
  }
}
el("hub-driver-log-output")?.addEventListener("scroll", () => {
  const output = el("hub-driver-log-output");
  driverLogAtBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 24;
});
function markDriverDraft() {
  driverDrafts.set(driverDraftKey(), {dirty:true,
    tool:el("hub-driver-cli").value,
    runtime:el("hub-driver-runtime").value,
    workspace:el("hub-driver-workspace").value,
    target:el("hub-driver-target").value,
    startup:el("hub-driver-startup-prompt").value,
    completion:el("hub-driver-completion-prompt").value});
}
const driverRequest = (suffix = "", options = {}) => hub.localCenter ? api(driverPath() + suffix, options) : local(driverPath() + suffix, options);
async function refreshDriverPanel() {
  clearTimeout(driverTimer);
  const panel = el("hub-driver-panel"), generation = hub.generation, session = token;
  if (!panel) return;
  if (driverViewContext !== driverLogContextKey()) {
    driverView = null;
    updateDriverInterjectState();
  }
  const toggle = el("hub-driver-toggle");
  const eligible = !hub.center && Boolean(hub.user) && Boolean(driverTicket) && canEdit(driverTicket);
  if (toggle) {
    toggle.hidden = !eligible;
    toggle.setAttribute("aria-expanded", String(driverExpanded));
    toggle.textContent = driverExpanded ? "收起工单驱动" : "展开工单驱动";
  }
  panel.hidden = !eligible || !driverExpanded;
  if (panel.hidden) {
    clearTimeout(driverLogTimer);
    clearTimeout(driverProgressTimer);
    driverLogContext = "";
    driverProgressContext = "";
    driverProgressFlight = null;
    driverLogRunId = null;
    driverLogCursor = 0;
    resetDriverTerminal();
    return;
  }
  const allowed = !hostBridge || !hub.localCenter || hub.localCenter.can_manage === true;
  for (const id of ["start", "stop", "resume", "host-key", "delete-ssh"]) el("hub-driver-" + id).disabled = true;
  el("hub-driver-add-ssh").hidden = Boolean(hostBridge);
  el("hub-driver-delete-ssh").hidden = Boolean(hostBridge);
  el("hub-driver-billing").textContent = hostBridge ? "近端 SSH 按 SWTool 每日 SSH 费率扣费；余额不足会阻止启动或中断。请在 SSH 工作台添加目标后刷新。" : "独立需求中心不收取 SWTool SSH 费用；SSH 凭据加密保存。";
  if (!allowed) {
    clearTimeout(driverLogTimer);
    clearTimeout(driverProgressTimer);
    resetDriverTerminal();
    el("hub-driver-state").textContent = "仅创建者可用";
    el("hub-driver-reason").textContent = "请选择由你创建的近端需求中心，再绑定 SSH 工作台。";
    return;
  }
  try {
    const data = await driverRequest();
    if (generation !== hub.generation || session !== token) return;
    driverView = data;
    driverViewContext = driverLogContextKey();
    const state = data.driver || {}, status = state.status || "stopped", active = ["running", "retry_wait", "interrupted"].includes(status);
    updateDriverInterjectState();
    refreshDriverLog();
    renderDriverProgress(data);
    refreshDriverProgress();
    el("hub-driver-state").textContent = ({running:"持续检测中",retry_wait:"等待重试",stopped:"已停止",interrupted:"服务中断，需先停止远端"})[status] || status;
    el("hub-driver-reason").textContent = [data.reason, state.last_error_code && "暂停原因：" + state.last_error_code].filter(Boolean).join(" · ");
    const runtime = data.config?.max_runtime_seconds || 0;
    el("hub-driver-retry").textContent = `每 60 秒检测；工单重试 ${state.ticket_retry_count || 0}/3，全局重试 ${state.global_retry_count || 0}/10；本轮已完成 ${state.completed_tickets || 0} 单；连续时限 ${runtime ? runtime + " 秒" : "不限"}。`;
    if (data.billing) el("hub-driver-billing").textContent = data.billing;
    const draftKey = driverDraftKey(), draft = driverDrafts.get(draftKey);
    const select = el("hub-driver-target");
    select.replaceChildren();
    for (const target of data.targets || []) {
      const option = document.createElement("option");
      option.value = target.id;
      option.dataset.kind = target.kind || "ssh";
      option.textContent = target.name || `${target.username || ""}@${target.hostname || target.host || "SSH"}:${target.port || 22}`;
      select.append(option);
    }
    if (!select.options.length) {
      select.add(new Option(hostBridge ? "没有 SSH，请在 SSH 工作台自行添加" : "没有 SSH，请点击添加 SSH 目标", ""));
    }
    const wanted = draft?.dirty ? draft.target : (data.config?.target_kind === "local" ? "local" : data.config?.ssh_target_id);
    if (Array.from(select.options).some(option => option.value === wanted)) select.value = wanted;
    const prompts = draft?.dirty ? draft : {
      tool:data.config?.tool || "swtool",
      runtime:data.config?.max_runtime_seconds || 0,
      workspace:data.config?.workspace || "",
      target:select.value,
      startup:data.config?.startup_prompt_template || data.default_startup_prompt_template || "",
      completion:data.config?.completion_prompt_template || data.default_completion_prompt_template || "",
      dirty:false};
    driverDrafts.set(draftKey, prompts);
    el("hub-driver-cli").value = prompts.tool;
    el("hub-driver-runtime").value = prompts.runtime;
    el("hub-driver-workspace").value = active ? (data.config?.workspace || "") : prompts.workspace;
    el("hub-driver-workspace").disabled = active;
    el("hub-driver-startup-prompt").value = prompts.startup;
    el("hub-driver-completion-prompt").value = prompts.completion;
    el("hub-driver-cli").disabled = active;
    select.disabled = active;
    const localTarget = select.selectedOptions[0]?.dataset.kind === "local";
    el("hub-driver-host-key").hidden = localTarget;
    el("hub-driver-host-key").disabled = active || !select.value || localTarget;
    el("hub-driver-delete-ssh").hidden = localTarget || Boolean(hostBridge);
    el("hub-driver-delete-ssh").disabled = active || !select.value || localTarget || select.value === data.config?.ssh_target_id;
    el("hub-driver-runtime").disabled = active;
    el("hub-driver-completion-prompt").disabled = active;
    el("hub-driver-startup-prompt").disabled = active;
    el("hub-driver-start").disabled = active || !select.value;
    el("hub-driver-stop").disabled = !data.driver || status === "stopped";
    el("hub-driver-resume").disabled = ["running", "retry_wait"].includes(status) || !data.driver || state.last_error_code === "termination_unconfirmed";
    appendDriverEvents(data);
    driverTimer = setTimeout(() => {
      if (generation === hub.generation && session === token && !document.hidden) refreshDriverPanel();
    }, 10000);
  } catch (error) {
    if (generation !== hub.generation || session !== token) return;
    el("hub-driver-state").textContent = "状态获取失败";
    el("hub-driver-reason").textContent = error.message;
  }
}
el("hub-driver-toggle")?.addEventListener("click", () => {
  driverExpanded = !driverExpanded;
  refreshDriverPanel();
});
if (!createPage) {
el("hub-driver-startup-prompt").oninput = markDriverDraft;
el("hub-driver-completion-prompt").oninput = markDriverDraft;
el("hub-driver-cli").onchange = markDriverDraft;
el("hub-driver-runtime").oninput = markDriverDraft;
el("hub-driver-workspace").oninput = markDriverDraft;
el("hub-driver-target").onchange = () => {
  markDriverDraft();
  confirmedHostKey = null;
  const localTarget = el("hub-driver-target").selectedOptions[0]?.dataset.kind === "local";
  el("hub-driver-host-key").hidden = localTarget;
  el("hub-driver-host-key").disabled = !el("hub-driver-target").value || localTarget;
  el("hub-driver-delete-ssh").hidden = localTarget || Boolean(hostBridge);
  el("hub-driver-delete-ssh").disabled = !el("hub-driver-target").value || localTarget;
};
el("hub-driver-interject-form")?.addEventListener("submit", safe(async (event) => {
  event.preventDefault();
  const input = el("hub-driver-interject-text"), button = el("hub-driver-interject-send");
  const text = input.value.trim();
  if (!text || button.disabled || driverInterjectState.sending || driverViewContext !== driverLogContextKey() || !driverView?.config?.current_ticket_id) return;
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  const interjection_id = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  const identity = driverInterjectIdentity();
  const flight = driverInterjectState = {identity, sending:true, message:"正在发送…"};
  updateDriverInterjectState();
  try {
    const receipt = await driverRequest("/interject", json("POST", {interjection_id, text}));
    if (driverInterjectIdentity() !== identity || driverInterjectState !== flight) return;
    if (receipt?.ok !== true || typeof receipt.duplicate !== "boolean" || !Number.isSafeInteger(receipt.pending) || receipt.pending < 0)
      throw Error("SWTOOL API 未确认插话结果");
    if (input.value.trim() === text) input.value = "";
    driverInterjectState.message = receipt.duplicate ? "已接收（重复请求）" : `已排队 · ${receipt.pending}`;
  } catch (error) {
    if (driverInterjectIdentity() === identity && driverInterjectState === flight)
      driverInterjectState.message = error.message || "发送失败";
  } finally {
    if (driverInterjectIdentity() === identity && driverInterjectState === flight) {
      driverInterjectState.sending = false;
      updateDriverInterjectState();
    }
  }
}));
el("hub-driver-add-ssh").onclick = () => dialog("添加 SSH 目标", '<form id="hub-driver-ssh-form" class="hub-dialog-form"><p>凭据加密保存。请通过可信渠道取得 SSH SHA256 指纹；执行时使用该 SSH 用户现有的 CLI 登录状态与权限，请确认其有权访问项目目录。可通过终端一键部署安装 CLI 与需求驱动程序。</p><label>目标名称<input name="name" required maxlength="80"></label><label>公网主机名或 IP<input name="hostname" required maxlength="253" autocomplete="off"></label><label>端口<input name="port" type="number" min="1" max="65535" value="22" required></label><label>SSH 用户名<input name="username" required maxlength="64" autocomplete="off"></label><label>主机指纹（SHA256）<input name="host_fingerprint" required placeholder="SHA256:…" autocomplete="off"></label><label>凭据类型<select name="credential_kind"><option value="password">密码</option><option value="private_key">无口令专用私钥</option></select></label><label>SSH 凭据<textarea name="credential" required maxlength="65536" autocomplete="off"></textarea></label><button class="primary" type="submit">加密保存</button></form>');
el("hub-dialog-body").addEventListener("submit", safe(async (event) => {
  if (event.target.id !== "hub-driver-ssh-form") return;
  event.preventDefault();
  const form = event.target, data = Object.fromEntries(new FormData(form));
  data.port = Number(data.port);
  try { await driverRequest("/ssh-targets", json("POST", data)); }
  finally { form.elements.namedItem("credential").value = ""; delete data.credential; }
  closeDialog();
  await refreshDriverPanel();
  notify("SSH 目标已加密保存；启动前请检测并确认主机指纹。");
}));
el("hub-driver-delete-ssh").onclick = safe(async () => {
  const id = el("hub-driver-target").value;
  if (!id || !confirm("删除此 SSH 目标及其加密凭据？")) return;
  await driverRequest("/ssh-targets/" + encodeURIComponent(id), {method:"DELETE"});
  confirmedHostKey = null;
  await refreshDriverPanel();
});
el("hub-driver-host-key").onclick = safe(async () => {
  const generation = hub.generation, target = el("hub-driver-target").value;
  const data = await driverRequest("/host-key", json("POST", {ssh_target_id:target}));
  if (generation !== hub.generation || target !== el("hub-driver-target").value) return;
  const key = data.host_key;
  if (!key || !confirm(`请通过可信渠道核对主机指纹。\n${key.fingerprint || key.algorithm + " " + key.publicKey}\n连接地址：${key.address}\n确认信任该主机？`)) return;
  confirmedHostKey = {target, key, generation};
  notify("主机指纹已明确确认；启动时会再次核验。");
});
el("hub-driver-start").onclick = safe(async () => {
  const workspace = el("hub-driver-workspace").value;
  if ((workspace && !workspace.startsWith("/")) || /[\0\r\n]/.test(workspace)) throw Error("项目目录须为空或远端绝对路径，且不能包含换行或空字符。");
  const target = el("hub-driver-target").value;
  const localTarget = el("hub-driver-target").selectedOptions[0]?.dataset.kind === "local";
  const runtime = Number(el("hub-driver-runtime").value);
  const startupPrompt = el("hub-driver-startup-prompt").value;
  const completionPrompt = el("hub-driver-completion-prompt").value;
  if (!startupPrompt.trim() || new TextEncoder().encode(startupPrompt).length > 8192 || !completionPrompt.trim() || new TextEncoder().encode(completionPrompt).length > 8192) throw Error("两种提示语均须为非空文本且各不超过 8KiB。");
  if (!Number.isInteger(runtime) || runtime < 0 || (runtime > 0 && runtime < 60) || runtime > 31536000) throw Error("连续运行时限须为 0（不限）或 60 至 31536000 秒。");
  if (!localTarget && (!confirmedHostKey || confirmedHostKey.target !== target || confirmedHostKey.generation !== hub.generation)) throw Error("请先检测并确认当前 SSH 主机指纹。");
  if (!confirm("确认持续处理此中心待解决工单，并授权验收通过后 Git push、更新里程碑和关闭工单？可随时手动停止。")) return;
  const draftKey = driverDraftKey();
  await driverRequest("/start", json("POST", {workspace,tool:el("hub-driver-cli").value,target_kind:localTarget ? "local" : "ssh",ssh_target_id:localTarget ? "" : target,host_key:localTarget ? undefined : confirmedHostKey.key,host_key_confirmed:localTarget ? undefined : true,push_authorized:true,close_authorized:true,max_runtime_seconds:runtime,startup_prompt_template:startupPrompt,completion_prompt_template:completionPrompt}));
  driverDrafts.delete(draftKey);
  await refreshDriverPanel();
});
el("hub-driver-stop").onclick = safe(async () => {
  if (!confirm("确认停止持续检测并终止当前远端工单？未完成工单将保留。")) return;
  await driverRequest("/stop", json("POST", {}));
  await refreshDriverPanel();
});
el("hub-driver-resume").onclick = safe(async () => {
  const runtime = Number(el("hub-driver-runtime").value);
  const startupPrompt = el("hub-driver-startup-prompt").value;
  const completionPrompt = el("hub-driver-completion-prompt").value;
  if (!startupPrompt.trim() || new TextEncoder().encode(startupPrompt).length > 8192 || !completionPrompt.trim() || new TextEncoder().encode(completionPrompt).length > 8192) throw Error("两种提示语均须为非空文本且各不超过 8KiB。");
  if (!Number.isInteger(runtime) || runtime < 0 || (runtime > 0 && runtime < 60) || runtime > 31536000) throw Error("连续运行时限须为 0（不限）或 60 至 31536000 秒。");
  if (el("hub-driver-workspace").value !== (driverView?.config?.workspace || "")) throw Error("恢复沿用原项目目录；修改目录需先停止，再重新启动。");
  if (!confirm("确认按保存的执行目标、项目目录与授权策略恢复？修改目录需先停止再重新启动。历史重试次数保留，连续时限从原启动时间累计。")) return;
  const draftKey = driverDraftKey();
  await driverRequest("/resume", json("POST", {max_runtime_seconds:runtime,startup_prompt_template:startupPrompt,completion_prompt_template:completionPrompt}));
  driverDrafts.delete(draftKey);
  await refreshDriverPanel();
});
document.addEventListener("visibilitychange", () => { if (!createPage && !document.hidden && !el("hub-driver-panel").hidden) refreshDriverPanel(); });
}
async function readJson(path, options, remote) {
  const generation = hub.generation,
    session = token;
  const result = await (await request(path, options, remote)).json();
  if ((remote && generation !== hub.generation) || session !== token)
    throw new WorkspaceChangedError();
  return result;
}
function safe(handler) {
  return async (event) => {
    const button = event?.currentTarget;
    try {
      if (button?.tagName === "BUTTON") button.disabled = true;
      await handler(event);
    } catch (error) {
      if (isWorkspaceChanged(error)) return;
      notify(error.message);
      if (!el("hub-dialog").open)
        el("hub-message").scrollIntoView({ block: "nearest" });
    } finally {
      if (button?.tagName === "BUTTON") button.disabled = false;
    }
  };
}
function resetDetail() {
  const detail = el("hub-detail");
  detail?.removeAttribute("aria-busy");
  clearPendingImagePastes(detail);
  hub.urls.forEach((url) => URL.revokeObjectURL(url));
  hub.urls = [];
  driverTicket = null;
  driverExpanded = false;
  refreshDriverPanel();
  detail?.replaceChildren();
}
function dialog(title, html) {
  clearPendingImagePastes(el("hub-dialog-body"));
  el("hub-dialog-body").querySelectorAll('input[type="password"]').forEach((field) => { field.value = ""; });
  el("hub-dialog-title").textContent = title;
  el("hub-dialog-body").innerHTML = html;
  if (!el("hub-dialog").open) el("hub-dialog").showModal();
}
function closeDialog() {
  clearPendingImagePastes(el("hub-dialog-body"));
  el("hub-dialog-body").querySelectorAll('input[type="password"]').forEach((field) => { field.value = ""; });
  if (el("hub-dialog").open) el("hub-dialog").close();
  el("hub-dialog-body").replaceChildren();
}
function canWrite() {
  if (hub.localCenter) return true;
  if (!hub.center) return true;
  const identity = hub.identity || {};
  if (identity.scope !== "tickets:write") return false;
  if (identity.capabilities && typeof identity.capabilities === "object")
    return identity.capabilities.remote_write === true;
  return (identity.roles || []).some((role) => ["admin", "superadmin"].includes(String(role).toLowerCase())) ||
    ["admin", "superadmin"].includes(String(identity.role || "").toLowerCase());
}
function canManage(item) {
  return hub.localCenter?.can_manage === true || item.can_manage === true || (hub.identity?.roles || []).some((role) => ["admin", "superadmin"].includes(String(role).toLowerCase()));
}
function canSortTickets() {
  return canWrite() && (hub.localCenter?.can_manage === true || ["admin", "superadmin"].some((role) => (hub.identity?.roles || []).some((value) => String(value).toLowerCase() === role)) || ["admin", "superadmin"].includes(String(hub.identity?.role || "").toLowerCase()));
}
async function moveTicket(sourceId, targetId, direction, generation = hub.generation, center = hub.center, localCenter = hub.localCenter?.id || "", listSeq = hub.listRequestSeq) {
  if (!canSortTickets() || hub.sorting || !sourceId || !targetId || sourceId === targetId || generation !== hub.generation || center !== hub.center || localCenter !== (hub.localCenter?.id || "") || listSeq !== hub.listRequestSeq) return;
  const payload = direction === "before" ? {move_before: targetId} : {move_after: targetId};
  hub.sorting = true; const status = el("hub-sort-status"); if (status) status.textContent = "正在保存顺序…"; updateSortControls();
  try {
    await ticketApi("/api/tickets/" + encodeURIComponent(sourceId), json("PATCH", payload));
    if (generation !== hub.generation || center !== hub.center || localCenter !== (hub.localCenter?.id || "")) return;
    if (status) status.textContent = "顺序已保存";
    await loadList();
  } catch (error) {
    if (generation === hub.generation && center === hub.center && localCenter === (hub.localCenter?.id || "")) { if (status) status.textContent = "顺序未更改：" + error.message; await loadList().catch(() => {}); }
  } finally { hub.sorting = false; updateSortControls(); }
}
function updateSortControls() {
  let tools = el("hub-sort-tools");
  if (!tools) {
    const list = el("hub-list"); if (!list?.parentElement) return;
    tools = document.createElement("div"); tools.id = "hub-sort-tools"; tools.className = "hub-sort-tools";
    tools.innerHTML = '<button id="hub-sort-up" type="button" aria-label="上移需求">↑ 上移</button><button id="hub-sort-down" type="button" aria-label="下移需求">↓ 下移</button><span id="hub-sort-status" role="status" aria-live="polite">拖动标题或按 Alt+方向键调整顺序</span>';
    list.before(tools);
    el("hub-sort-up").addEventListener("click", () => moveSelected(-1));
    el("hub-sort-down").addEventListener("click", () => moveSelected(1));
  }
  tools.hidden = !canSortTickets();
  const index = (hub.currentItems || []).findIndex((item) => String(item.id) === String(hub.selected));
  el("hub-sort-up").disabled = hub.sorting || index <= 0;
  el("hub-sort-down").disabled = hub.sorting || index < 0 || index >= (hub.currentItems || []).length - 1;
  el("hub-list")?.querySelectorAll("strong").forEach((title) => { title.draggable = canSortTickets() && !hub.sorting; });
}
function moveSelected(delta) { const index = (hub.currentItems || []).findIndex((item) => String(item.id) === String(hub.selected)); if (index < 0) return; const target = hub.currentItems[index + delta]; if (target) moveTicket(String(hub.selected), String(target.id), delta < 0 ? "before" : "after"); }
function canEdit(item) {
  const owner = hub.identity?.user_id || hub.identity?.id;
  return canWrite() && (canManage(item) || item.ownedByViewer === true || (owner != null && item.author_id != null && String(item.author_id) === String(owner)));
}
el("hub-dialog-close").onclick = closeDialog;
el("hub-dialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeDialog();
});

async function authenticate(kind) {
  if (hostBridge) { notify("请先登录宿主；认证由宿主会话管理。"); return; }
  const form = el("hub-auth-form"),
    data = new FormData(form);
  el("hub-auth-message").textContent = "正在处理…";
  try {
    const response = await fetch(
      "/api/v1/auth/" + kind,
      json("POST", {
        username: data.get("username"),
        password: data.get("password"),
      }),
    );
    const result = await response.json();
    if (!response.ok)
      throw Error(
        typeof result.detail === "string"
          ? result.detail
          : "请检查用户名与密码（注册密码至少 8 位）",
      );
    if (!result.access_token) {
      el("hub-auth-message").textContent = result.message;
      form.elements.namedItem("password").value = "";
      return;
    }
    token = result.access_token;
    // Invalidate an automatic boot that may still be using the previous
    // account/session. Its late responses must not invalidate this login.
    hub.generation++;
    localStorage.setItem("ws_token", token);
    form.elements.namedItem("password").value = "";
    await boot();
  } catch (error) {
    if (isWorkspaceChanged(error)) return;
    el("hub-auth-message").textContent = error.message;
  }
}
el("hub-auth-form").onsubmit = (event) => {
  event.preventDefault();
  authenticate("login");
};
el("hub-register").onclick = () => {
  if (el("hub-auth-form").reportValidity()) authenticate("register");
};
function logout() {
  token = "";
  hub.generation++;
  window.SWToolRequirementActions?.close();
  hub.user = null;
  hub.identity = null;
  hub.center = "";
  hub.localCenter = null;
  hub.connections = [];
  localStorage.removeItem("ws_token");
  localStorage.removeItem("ws_me");
  resetDetail();
  closeDialog();
  el("hub-settings-panel").replaceChildren();
  el("hub-list").replaceChildren();
  el("hub-auth").hidden = false;
  el("workspace").hidden = true;
  el("hub-auth-message").textContent = "";
  window.SWToolRequirementsMilestones?.sync();
}
el("hub-logout").onclick = logout;
function ensureThemeControlVisible() {
  document.querySelectorAll("[data-theme-control]").forEach((control) => {
    control.hidden = false;
    control.removeAttribute("aria-hidden");
    control.style.removeProperty("display");
  });
}
async function boot() {
  const session = token,
    generation = hub.generation;
  if (!session) return;
  if (bootFlight && bootSession === session) return bootFlight;
  bootSession = session;
  bootFlight = (async () => {
   try {
    hub.user = await local("/me");
    if (session !== token) return;
    el("hub-user").textContent = hub.user.username;
    el("hub-admin").hidden = !hub.user.roles.includes("admin");
    if (hostBridge) { el("hub-admin").hidden = true; el("hub-plugins").hidden = true; el("hub-logout").hidden = true; }
    el("hub-auth").hidden = true;
    el("workspace").hidden = false;
    ensureThemeControlVisible();
    const plugin = await local("/api/plugins/requirements-center");
    requirementsActive = plugin.active;
    el("issuesPanel").hidden = !requirementsActive || createPage;
    if (requirementsActive) {
      if (createPage) { await settings(); return; }
      await connections();
      if (session !== token) return;
      if (generation === hub.generation) await switchCenter("", new URLSearchParams(location.search).get("local_center"));
    } else notify("需求中心插件已停用，管理员可在账号管理中重新激活。");
   } catch (error) {
    if (session !== token || isWorkspaceChanged(error)) return;
    if (hostBridge) {
      el("hub-auth").hidden = true; el("workspace").hidden = false;
      notify([401, 403].includes(error.status) ? "宿主登录状态无效，请返回宿主重新登录。" : "需求中心暂时不可用，请稍后重试。" + (error.status ? `（HTTP ${error.status}）` : ""));
    }
    else { logout(); el("hub-auth-message").textContent = error.message; }
   }
  })();
  const flight = bootFlight;
  try { await flight; } finally { if (bootFlight === flight) { bootFlight = null; bootSession = ""; } }
}
async function connections() {
  const generation = hub.generation, session = token;
  const result = await local("/api/requirements/connections");
  const connections = result.items || result.connections || [];
  let localCenters = [], localError = null;
  if (hostBridge) {
    try {
      const localResult = await local("/api/local-centers");
      localCenters = localResult.centers || [];
    } catch (error) { localError = error; }
  }
  if (generation !== hub.generation || session !== token) return;
  hub.connections = connections;
  const select = el("hub-centers");
  select.replaceChildren();
  const home = document.createElement("option");
  home.value = "";
  home.textContent = "本机需求中心.本机";
  select.append(home);
  for (const item of localCenters) {
    const option = document.createElement("option");
    option.value = "local:" + item.id;
    option.textContent = item.name + ".本机";
    select.append(option);
  }
  for (const item of hub.connections) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.name + ".远端 · " + item.base_url;
    select.append(option);
  }
  select.value = hub.localCenter ? "local:" + hub.localCenter.id : hub.center;
  if (localError) notify(localError.message + "；近端中心列表加载失败");
}
function selectLocalCenter() {
  const select = el("hub-centers"), value = "local:" + hub.localCenter.id;
  let option = Array.from(select.options).find((entry) => entry.value === value);
  if (!option) {
    option = document.createElement("option");
    option.value = value;
    select.append(option);
  }
  option.textContent = hub.localCenter.name + ".本机";
  select.value = value;
}
async function switchCenter(id, localId = null) {
  clearCreatedTicket();
  hub.dragTicket = null;
  document.querySelectorAll(".hub-sort-target").forEach((node) => node.classList.remove("hub-sort-target"));
  driverExpanded = false;
  if (localId && !hostBridge) {
    el("hub-new").hidden = true;
    el("hub-archive").hidden = true;
    notify("近端中心仅在宿主中可用，请从宿主打开需求 Hub。");
    return;
  }
  if (el("hub-dialog").open) closeDialog();
  clearPendingImagePastes();
  hub.center = id;
  hub.localCenter = null;
  el("hub-centers").value = id;
  if (localId || new URLSearchParams(location.search).has("local_center")) {
    const url = new URL(location.href);
    if (localId) url.searchParams.set("local_center", localId);
    else url.searchParams.delete("local_center");
    window.history.replaceState(null, "", url.href);
  }
  el("hub-status").disabled = false;
  hub.generation++;
  const generation = hub.generation,
    session = token;
  window.SWToolRequirementActions?.close();
  hub.page = 1;
  hub.selected = "";
  resetDetail();
  el("hub-list").replaceChildren();
  hub.identity = null;
  el("hub-archive").hidden = true;
  notify("正在连接需求中心…");
  window.SWToolRequirementsMilestones?.sync();
  try {
    if (localId) {
      const data = await local("/api/local-centers/" + encodeURIComponent(localId));
      if (generation !== hub.generation || session !== token) return;
      hub.localCenter = {...(data.center || data), id: (data.center || data).id || localId};
      selectLocalCenter();
      hub.identity = {...hub.user, user_id: hub.user.user_id || hub.user.id, roles: hub.localCenter.can_manage === true ? ["admin"] : ["user"]};
    } else hub.identity = await api("/api/requirements/identity");
    if (hostBridge) { el("hub-new").hidden = true; el("hub-new").disabled = false; }
    el("hub-identity").textContent =
      hub.localCenter ? hub.localCenter.name + " · 全中心需求" : "目标账号：" +
      hub.identity.username +
      " · " +
      (hub.identity.roles.includes("admin") ? "管理员" : "普通用户");
    el("hub-archive").hidden = Boolean(hub.localCenter) || !hub.identity.roles.includes("admin");
    installDownloadsButton();
    notify("");
    await loadList();
    window.SWToolRequirementsMilestones?.load();
    await refreshDriverPanel();
  } catch (error) {
    if (generation !== hub.generation || session !== token || isWorkspaceChanged(error)) return;
    notify(error.message);
    el("hub-identity").textContent = "连接失败，可在“创建中心”重新配置";
    await refreshDriverPanel();
  }
}
el("hub-centers").onchange = safe(() => {
  const value = el("hub-centers").value;
  return value.startsWith("local:") ? switchCenter("", value.slice(6)) : switchCenter(value);
});
function clearCreatedTicket() {
  hub.justCreated = null;
  el("hub-created-ticket")?.remove();
}
function renderCreatedTicket() {
  el("hub-created-ticket")?.remove();
  const created = hub.justCreated;
  if (!created || created.generation !== hub.generation || created.center !== hub.center || created.local !== (hub.localCenter?.id || "")) return;
  if (hub.currentItems.some((item) => String(item.id) === created.id)) return;
  const section = document.createElement("section"); section.id = "hub-created-ticket";
  section.setAttribute("aria-label", "刚创建的需求");
  const label = document.createElement("p"); label.textContent = "刚创建的需求（不计入本页列表）";
  const button = document.createElement("button"); button.type = "button";
  button.textContent = created.title || "查看刚创建的需求";
  button.className = "primary";
  button.onclick = () => {
    if (section.isConnected && hub.justCreated === created && created.generation === hub.generation && created.center === hub.center && created.local === (hub.localCenter?.id || "")) safe(() => openTicket(created.id))();
  };
  section.append(label, button);
  el("hub-list").before(section);
}
async function loadList() {
  const listRequestSeq = ++hub.listRequestSeq;
  const contextGeneration = hub.generation, contextCenter = hub.center, contextLocal = hub.localCenter?.id || "";
  const query = new URLSearchParams({
    page: hub.page,
    page_size: 20,
    q: el("hub-search").value,
  });
  query.set("status", el("hub-status").value);
  const data = await api(
    "/api/tickets?" + query,
  );
  if (listRequestSeq !== hub.listRequestSeq || contextGeneration !== hub.generation || contextCenter !== hub.center || contextLocal !== (hub.localCenter?.id || "")) return;
  hub.pages = data.total_pages || 1;
  el("hub-page").textContent =
    hub.page + " / " + hub.pages + " · " + data.total + " 条";
  el("hub-prev").disabled = hub.page <= 1;
  el("hub-next").disabled = hub.page >= hub.pages;
  const list = el("hub-list");
  const detail = el("hub-detail"), home = detailHome();
  if (detail && home && detail.parentElement !== home) home.append(detail);
  list.replaceChildren();
  hub.currentItems = Array.isArray(data.items) ? data.items : [];
  const visibleById = new Map(hub.currentItems.map((item) => [String(item.id), item]));
  const visibleDepth = (item) => {
    let depth = 0, parentId = item.parent_ticket_id || item.metadata?.parent_ticket_id || item.parent_ticket?.id, seen = new Set([String(item.id)]);
    while (parentId && depth < 8) {
      const key = String(parentId);
      if (seen.has(key)) break;
      seen.add(key);
      if (!visibleById.has(key)) { depth++; break; }
      const parent = visibleById.get(key);
      depth++; parentId = parent.parent_ticket_id || parent.metadata?.parent_ticket_id || parent.parent_ticket?.id;
    }
    return depth;
  };
  updateSortControls();
  for (const [itemIndex, item] of hub.currentItems.entries()) {
    const li = document.createElement("li"),
      button = document.createElement("button");
    button.dataset.ticketId = item.id;
    const depth = visibleDepth(item);
    button.dataset.depth = String(depth);
    if (depth) button.classList.add("hub-child-ticket");
    button.title = item.title || "未命名需求";
    button.setAttribute("aria-pressed", String(item.id === hub.selected));
    button.innerHTML =
      "<strong>" +
      (depth ? "↳ " : "") + esc(item.title) +
      '</strong><span class="hub-status ' +
      esc(item.status) +
      '">' +
      esc(states[item.status] || item.status) +
      "</span><small>" +
      esc(item.author_username) +
      " · " +
      esc(time(item.updated_at)) +
      "</small>";
    const titleNode = button.querySelector("strong");
    if (titleNode) {
      titleNode.draggable = canSortTickets() && !hub.sorting;
      button.setAttribute("aria-label", `${depth ? "子工单，" : "工单，"}${item.title || "未命名需求"}${canSortTickets() ? "，拖动标题或按 Alt 加上下方向键排序" : ""}`);
      titleNode.addEventListener("dragstart", (event) => { if (!canSortTickets() || hub.sorting || listRequestSeq !== hub.listRequestSeq || contextGeneration !== hub.generation || contextCenter !== hub.center || contextLocal !== (hub.localCenter?.id || "")) { event.preventDefault(); return; } hub.dragTicket = {id: String(item.id), generation: contextGeneration, center: contextCenter, local: contextLocal, listSeq: listRequestSeq}; event.dataTransfer?.setData("text/plain", String(item.id)); });
      titleNode.addEventListener("dragover", (event) => { const drag = hub.dragTicket; if (!drag || drag.id === String(item.id) || drag.generation !== hub.generation || drag.center !== hub.center || drag.local !== (hub.localCenter?.id || "") || drag.listSeq !== hub.listRequestSeq) return; event.preventDefault(); titleNode.classList.add("hub-sort-target"); });
      titleNode.addEventListener("dragleave", () => titleNode.classList.remove("hub-sort-target"));
      titleNode.addEventListener("drop", (event) => { event.preventDefault(); event.stopPropagation(); titleNode.classList.remove("hub-sort-target"); const source = hub.dragTicket; hub.dragTicket = null; const rect = titleNode.getBoundingClientRect(); if (listRequestSeq === hub.listRequestSeq) moveTicket(source?.id, String(item.id), event.clientY < rect.top + rect.height / 2 ? "before" : "after", source?.generation, source?.center, source?.local, source?.listSeq); });
      titleNode.addEventListener("dragend", () => { hub.dragTicket = null; document.querySelectorAll(".hub-sort-target").forEach((node) => node.classList.remove("hub-sort-target")); });
      button.addEventListener("keydown", (event) => { if (!canSortTickets() || !event.altKey || !["ArrowUp", "ArrowDown"].includes(event.key)) return; event.preventDefault(); event.stopPropagation(); const target = hub.currentItems[itemIndex + (event.key === "ArrowUp" ? -1 : 1)]; if (target) moveTicket(String(item.id), String(target.id), event.key === "ArrowUp" ? "before" : "after", contextGeneration, contextCenter, contextLocal, listRequestSeq); });
    }
    button.onclick = (event) => {
      event.preventDefault();
      event.stopPropagation();
      const scrollX = window.scrollX, scrollY = window.scrollY;
      safe(async () => {
        await openTicket(item.id);
        requestAnimationFrame(() => window.scrollTo(scrollX, scrollY));
      })();
    };
    button.addEventListener("click", () => { hub.selected = String(item.id); updateSortControls(); });
    if (canEdit(item)) window.SWToolRequirementActions?.bind(button,{label:item.status==='withdrawn'?'重新提交':'撤回需求',onAction:safe(()=>toggleWithdrawal(item))});
    li.append(button);
    list.append(li);
  }
  if (!data.items.length)
    list.innerHTML = '<li class="muted">暂无需求，点击“新增需求”开始。</li>';
  renderCreatedTicket();
  if (!hub.selected && data.items.length) await openTicket(data.items[0].id);
}
el("hub-refresh").onclick = safe(async () => {
  clearCreatedTicket();
  hub.page = 1;
  await loadList();
});
el("hub-search").onkeydown = (event) => {
  if (event.key === "Enter") el("hub-refresh").click();
};
el("hub-status").onchange = () => {
  hub.page = 1;
  el("hub-refresh").click();
};
el("hub-prev").onclick = safe(async () => {
  clearCreatedTicket();
  hub.page--;
  await loadList();
});
el("hub-next").onclick = safe(async () => {
  clearCreatedTicket();
  hub.page++;
  await loadList();
});
async function attachmentBlob(response) {
  const type = response.headers.get("Content-Type") || "";
  if (!type.toLowerCase().includes("json")) return response.blob();
  const payload = await response.json();
  if (!payload || typeof payload.data_base64 !== "string") throw Error(payload?.error || "附件响应无效");
  const binary = atob(payload.data_base64);
  return new Blob([Uint8Array.from(binary, (character) => character.charCodeAt(0))], {type: payload.content_type || "application/octet-stream"});
}
async function download(path, name, remote = true) {
  const blob = await attachmentBlob(await request(path, {}, remote));
  const url = URL.createObjectURL(blob),
    anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function attachments(parent, items, ticketId) {
  const generation = hub.generation;
  if (!items || !items.length) return;
  const container = document.createElement("div");
  container.className = "issue-attachments";
  parent.append(container);
  for (const item of items || []) {
    if (generation !== hub.generation || hub.selected !== ticketId || !parent.isConnected) return;
    const isImage = (item.mime_type || item.mime || "").startsWith("image/");
    const block = document.createElement(isImage ? "figure" : "div");
    if (isImage) block.className = "issue-attachment-image";
    const button = document.createElement("button");
    button.className = "issue-attachment-link";
    button.textContent = "下载 · " + (item.name || item.original_name);
    const path =
      "/api/tickets/" +
      encodeURIComponent(ticketId) +
      "/attachments/" +
      encodeURIComponent(item.id);
    button.onclick = safe(() =>
      download(path, item.name || item.original_name),
    );
    block.append(button);
    container.append(block);
    if (isImage) {
      try {
        const response = await request(path);
        const blob = await attachmentBlob(response);
        if (generation !== hub.generation || hub.selected !== ticketId || !parent.isConnected) return;
        const dataUrl = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result);
          reader.onerror = () => reject(reader.error || new Error("图片预览失败"));
          reader.onabort = () => reject(new Error("图片预览失败"));
          reader.readAsDataURL(blob);
        });
        if (generation !== hub.generation || hub.selected !== ticketId || !parent.isConnected) return;
        const image = document.createElement("img");
        image.src = dataUrl;
        image.title = "点击放大，再次点击还原";
        image.tabIndex = 0;
        const toggleImage = () => image.classList.toggle("is-expanded");
        image.onclick = toggleImage;
        image.onkeydown = (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggleImage(); } };
        image.alt = item.name || item.original_name || "需求附件";
        image.loading = "eager";
        image.onerror = () => {
          if (generation !== hub.generation || hub.selected !== ticketId || !parent.isConnected) return;
          image.remove();
          const message = document.createElement("small");
          message.textContent = "图片预览失败，请下载查看";
          block.append(message);
        };
        block.prepend(image);
      } catch (error) {
        if (generation !== hub.generation || hub.selected !== ticketId || !parent.isConnected || isWorkspaceChanged(error)) return;
        const message = document.createElement("small");
        message.textContent = error.message;
        block.append(message);
      }
    }
  }
}
async function toggleWithdrawal(item){
  if (!canEdit(item)) throw Error('你没有修改此需求的权限');
  const withdrawn=item.status==='withdrawn',generation=hub.generation;
  const nextStatus = withdrawn ? (canManage(item) ? 'open' : 'pending') : 'withdrawn';
  if(!confirm(withdrawn?'重新提交此需求？提交后将进入“'+states[nextStatus]+'”状态。':'撤回此需求？原文、附件与历史会保留，可从“回收站”找回。'))return;
  await api('/api/tickets/'+encodeURIComponent(item.id),json('PATCH',{status:nextStatus}));
  if(generation!==hub.generation)return;
  hub.selected='';hub.page=1;resetDetail();await loadList();window.SWToolRequirementsMilestones?.load();
}
async function purgeTicket(item) {
  if (item.status !== 'withdrawn' || !canEdit(item)) throw Error('只能清理你有权限管理的回收站需求');
  if (!confirm('彻底清理此需求？正文、附件与历史将永久删除，无法恢复。')) return;
  const generation = hub.generation;
  const hostHome = hostBridge && !hub.center && !hub.localCenter;
  try {
    await request('/api/tickets/' + encodeURIComponent(item.id), {method:'DELETE'}, !hostHome);
  } catch (error) {
    if (!isWorkspaceChanged(error) && hub.center && [403,404,405,501].includes(error.status)) throw Error('目标中心暂不支持彻底清理，请在目标中心操作。');
    throw error;
  }
  if (generation !== hub.generation) return;
  hub.selected='';hub.page=1;resetDetail();await loadList();window.SWToolRequirementsMilestones?.load();
}
  function renderTicketRelations(ticket, navigate) {
    const parent = ticket.parent_ticket, children = Array.isArray(ticket.child_tickets) ? ticket.child_tickets.slice(0, 200) : [];
    if (!parent && !children.length) return null;
    const section = document.createElement("section"); section.className = "ticket-relations"; section.setAttribute("aria-label", "工单关系");
    const heading = document.createElement("h3"); heading.textContent = "工单关系";
    const tree = document.createElement("ol"); tree.className = "ticket-relation-tree";
    const makeItem = (item, current = false) => {
      const row = document.createElement("li"), label = document.createElement(current ? "span" : "button");
      label.textContent = String(item.title || "未命名需求");
      if (current) label.setAttribute("aria-current", "page");
      else { label.type = "button"; label.onclick = () => { if (label.isConnected) navigate(String(item.id)); }; }
      row.append(label); return row;
    };
    if (parent) tree.append(makeItem(parent));
    const current = makeItem(ticket, true); tree.append(current);
    if (children.length) { const childList = document.createElement("ul"); children.forEach((child) => childList.append(makeItem(child))); current.append(childList); }
    const diagram = document.createElement("div"); diagram.className = "ticket-relation-diagram";
    section.append(heading, diagram, tree);
    if (ticket.children_truncated === true) { const note = document.createElement("p"); note.textContent = "直属子工单仅显示前 200 项。"; section.append(note); }
    return section;
  }

async function openTicket(id) {
  const detailGeneration = hub.generation, detailCenter = hub.center, detailLocal = hub.localCenter?.id || "";
  const detailRequest = (hub.detailRequestSeq = (hub.detailRequestSeq || 0) + 1);
  hub.selected = id;
  updateSortControls();
  const detail = el("hub-detail");
  const requestCurrent = () => detailRequest === hub.detailRequestSeq && hub.selected === id && detailGeneration === hub.generation && detailCenter === hub.center && detailLocal === (hub.localCenter?.id || "");
  resetDetail();
  detail.setAttribute("aria-busy", "true");
  detail.innerHTML = '<p class="hub-detail-loading" role="status">正在加载需求…</p>';
  placeMobileDetail();
  let item;
  try { item = await api("/api/tickets/" + encodeURIComponent(id)); }
  catch (error) {
    if (requestCurrent()) {
      detail.removeAttribute("aria-busy");
      detail.innerHTML = '<p class="hub-detail-error" role="status">需求加载失败，请重试。</p>';
    }
    if (!requestCurrent()) return;
    throw error;
  }
  if (detailRequest !== hub.detailRequestSeq || hub.selected !== id || detailGeneration !== hub.generation || detailCenter !== hub.center || detailLocal !== (hub.localCenter?.id || "")) return;
  detail.removeAttribute("aria-busy");
  resetDetail();
  driverTicket = item;
  refreshDriverPanel();
  detail.innerHTML =
    '<div class="hub-ticket-heading"><div id="hub-title-status" class="hub-title-status"></div><h2>' +
    esc(item.title) +
    '</h2><div id="hub-title-actions" class="hub-title-actions"><button id="hub-edit" type="button">编辑需求</button></div></div><p class="hub-platforms"><strong>关联平台：</strong>' +
    esc((Array.isArray(item.platforms) ? item.platforms : (item.platform ? [item.platform] : [])).map((value) => ({h5_mobile:"H5",pc_browser:"PC浏览器",android:"安卓",windows:"Windows",ios:"iOS手机",mac:"iOS电脑",linux:"Linux",embedded:"嵌入式",other:"其他"}[value] || value)).concat(item.custom_platform ? [item.custom_platform] : []).join("、") || "未指定") +
    '</p><p class="muted">提交人 ' +
    esc(item.author_username) +
    " · " +
    esc(time(item.created_at)) +
    "<br>预计解决 " +
    esc(time(item.expected_resolution_at)) +
    '</p><div class="hub-description">' +
    esc(item.description) +
    "</div><p>" +
    esc((item.labels || []).join(" · ")) +
    '</p><div id="hub-files"></div><h3>回复与补充</h3><div id="hub-comments"></div><form id="hub-comment-form"><label>回复内容<textarea name="body" required></textarea></label><label>回复附件<input name="files" type="file" multiple accept="image/*,.pdf,.txt,.csv,.zip,.json"></label><div class="hub-pasted-files" aria-label="粘贴的图片"></div><button type="submit" class="primary">提交回复</button></form><h3>状态历史</h3><div id="hub-history"></div>';
  window.SwtoolTicketContent?.render(detail.querySelector(".hub-description"), item.description);
  const relationGeneration = hub.generation;
  const relationCurrent = () => relationGeneration === hub.generation && detailCenter === hub.center && detailLocal === (hub.localCenter?.id || "") && hub.selected === id;
  const relationNavigate = (targetId) => { if (relationCurrent()) safe(() => openTicket(targetId))(); };
  const relations = renderTicketRelations(item, relationNavigate);
  if (relations) {
    const disclosure = document.createElement("details");
    disclosure.className = "ticket-relations-disclosure";
    const summary = document.createElement("summary"); summary.textContent = "工单关系";
    disclosure.append(summary, relations);
    detail.querySelector(".hub-description").after(disclosure);
    disclosure.addEventListener("toggle", () => {
      if (disclosure.open && !disclosure.dataset.rendered) {
        window.SwtoolTicketContent?.renderRelations?.(relations.querySelector(".ticket-relation-diagram"), item, {navigate: relationNavigate, isCurrent: relationCurrent});
        disclosure.dataset.rendered = "true";
      }
    });
  }
  placeMobileDetail();
  if (item.reproduction_steps) {
    const p = document.createElement("p");
    p.textContent = "复现步骤：" + item.reproduction_steps;
    detail.querySelector(".hub-description").after(p);
  }
  if (item.expected_result) {
    const p = document.createElement("p");
    p.textContent = "期望结果：" + item.expected_result;
    detail.querySelector(".hub-description").after(p);
  }
  const titleStatus = el("hub-title-status");
  if (titleStatus && canManage(item)) {
    const select = document.createElement("select");
    select.setAttribute("aria-label", "快速改变需求状态");
    const quickStates = {pending: "待审核", open: "待解决", in_progress: "解决中", resolved: "已解决"};
    Object.entries(quickStates).forEach(([value, label]) => {
      const option = document.createElement("option"); option.value = value; option.textContent = label;
      option.selected = (item.status === "closed" ? "resolved" : item.status) === value; select.append(option);
    });
    select.onchange = safe(async () => {
      const previous = item.status === "closed" ? "resolved" : item.status;
      select.disabled = true;
      try {
        await api("/api/tickets/" + encodeURIComponent(item.id), {method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify({status: select.value})});
        await openTicket(item.id); await loadList();
      } catch (error) { select.value = previous; notify(error.message || "状态保存失败"); }
      finally { select.disabled = false; }
    });
    titleStatus.append(select);
  } else if (titleStatus) {
    titleStatus.append(Object.assign(document.createElement("span"), {className: "hub-status " + String(item.status || ""), textContent: states[item.status] || item.status || ""}));
  }
  const titleActions = el("hub-title-actions");
  el("hub-edit").onclick = safe(() => editForm(item));
  el("hub-edit").hidden = !canEdit(item);
  const withdraw=document.createElement('button');withdraw.id='hub-withdraw';withdraw.textContent=item.status==='withdrawn'?'重新提交':'撤回需求';withdraw.onclick=safe(()=>toggleWithdrawal(item));titleActions?.append(withdraw);
  withdraw.hidden = !canEdit(item);
  if (item.status === 'withdrawn' && canEdit(item)) {
    const purge = document.createElement('button');
    purge.id = 'hub-purge';purge.textContent = '彻底清理';purge.onclick = safe(() => purgeTicket(item));titleActions?.append(purge);
  }
  attachments(el("hub-files"), item.attachments, id);
  for (const comment of item.comments || []) {
    const block = document.createElement("article");
    block.className = "hub-comment";
    block.innerHTML =
      "<small>" +
      esc(comment.author_username || "用户 #" + comment.author_id) +
      " · " +
      esc(time(comment.created_at)) +
      '</small><div class="hub-comment-body">' +
      esc(comment.body) +
      "</div>";
    window.SwtoolTicketContent?.render(block.querySelector(".hub-comment-body"), comment.body);
    const actions = document.createElement("div");
    actions.className = "hub-comment-actions";
    block.append(actions);
    const generation = hub.generation;
    const active = () => generation === hub.generation && hub.selected === id && block.isConnected;
    if (comment.editableByViewer === true) {
      const edit = document.createElement("button");
      edit.type = "button"; edit.textContent = "编辑回复";
      const remove = document.createElement("button");
      remove.type = "button"; remove.textContent = "删除回复";
      actions.append(edit, remove);
      edit.onclick = safe(async () => {
        if (!active() || actions.hidden) return;
        const textarea = document.createElement("textarea");
        textarea.value = comment.body || ""; textarea.maxLength = 4000; textarea.required = true;
        textarea.setAttribute("aria-label", "编辑回复内容");
        const save = document.createElement("button"); save.type = "button"; save.textContent = "保存";
        const cancel = document.createElement("button"); cancel.type = "button"; cancel.textContent = "取消";
        const editor = document.createElement("div"); editor.className = "hub-comment-editor";
        editor.append(textarea, save, cancel); actions.hidden = true; block.append(editor); textarea.focus();
        let submitting = false;
        cancel.onclick = () => { editor.remove(); actions.hidden = false; };
        save.onclick = async () => {
          const body = textarea.value.trim();
          if (submitting || !active() || !body) return;
          submitting = true; [save, cancel, textarea].forEach((node) => { node.disabled = true; });
          try {
            await api("/api/tickets/" + encodeURIComponent(id) + "/comments/" + encodeURIComponent(comment.id), json("PATCH", {body, if_updated_at: comment.edited_at || comment.created_at}));
            if (active()) await openTicket(id);
          } catch (error) { if (active()) notify(error.message || "编辑回复失败"); }
          finally { if (active()) [save, cancel, textarea].forEach((node) => { node.disabled = false; }); }
        };
      });
      let deleting = false;
      remove.onclick = safe(async () => {
        if (!active() || deleting || !confirm("确认删除这条回复？")) return;
        deleting = true;
        try {
          await api("/api/tickets/" + encodeURIComponent(id) + "/comments/" + encodeURIComponent(comment.id), {method: "DELETE"});
          if (active()) await openTicket(id);
        } catch (error) { if (active()) notify(error.message || "删除回复失败"); }
        finally { deleting = false; }
      });
    }
    const reply = document.createElement("button");
    reply.type = "button"; reply.textContent = "回复";
    reply.hidden = !canWrite();
    reply.onclick = () => {
      const form = el("hub-comment-form");
      const textarea = form?.elements.namedItem("body");
      if (!active() || !canWrite() || form?.hidden || !textarea || textarea.disabled || textarea.readOnly) return;
      const author = String(comment.author_username || comment.author || "用户 #" + comment.author_id).replace(/[\r\n]+/g, " ");
      const quote = "> 回复 " + author + "：\n" + String(comment.body || "").replace(/\r\n?/g, "\n").split("\n").map((line) => "> " + line).join("\n");
      const draft = textarea.value;
      const next = draft + (draft ? "\n\n" : "") + quote + "\n\n";
      if (next.length > (textarea.maxLength > 0 ? textarea.maxLength : 4000)) {
        notify("引用后回复超过4000字，请先精简草稿或原文");
        return;
      }
      textarea.value = next;
      textarea.focus();
      textarea.scrollIntoView({block: "center", behavior: "smooth"});
    };
    actions.append(reply);
    el("hub-comments").append(block);
    attachments(block, comment.attachments, id);
  }
  for (const event of item.status_events || []) {
    const p = document.createElement("p");
    p.textContent =
      time(event.changed_at) +
      " · " +
      (states[event.from_status] || "新增") +
      " → " +
      (states[event.to_status] || event.to_status);
    el("hub-history").append(p);
  }
  if (hostBridge) {
    el("hub-comment-form").hidden = !canWrite();
    const link = document.createElement("a"); if (hub.center) link.href = "/tickets"; link.textContent = hub.center ? (canWrite() ? "远端编辑/回复按目标中心 Key 所属账号权限执行" : "目标 Key 未获写入能力，请更换具备写入权限的 Key") : "本机需求可编辑，工单驱动仅管理员或创建者可用";
    if (hub.center) el("hub-detail").append(link);
  }
  const commentForm = el("hub-comment-form");
  const commentImages = canWrite() && !commentForm.hidden
    ? attachImagePaste(commentForm)
    : null;
  let savedComment = null;
  commentForm.onsubmit = (event) => {
    event.preventDefault();
    safe(async () => {
      const form = event.currentTarget,
        body = form.elements.namedItem("body").value,
        files = [...form.elements.namedItem("files").files],
        before = new Set((item.comments || []).map((x) => x.id));
      let updated = savedComment?.body === body ? {comments: [savedComment]} : await api(
        "/api/tickets/" + id + "/comments",
        json("POST", { body }),
      );
      if (files.length || commentImages?.files().length || !Array.isArray(updated.comments)) updated = await api("/api/tickets/" + encodeURIComponent(id));
      const comment = savedComment?.body === body ? savedComment : [...(updated.comments || [])]
        .reverse()
        .find(
          (x) =>
            !before.has(x.id) &&
            (String(x.author_id || x.user_id) === String(hub.identity.user_id || hub.identity.id) || x.editableByViewer === true) &&
            x.body === body,
        );
      if ((files.length || commentImages?.files().length) && !comment)
        throw Error("回复已提交，无法确认附件归属，请刷新后重试");
      savedComment = comment || null;
      if (commentImages) await commentImages.submit(upload, id, files, comment?.id);
      else await upload(id, files, comment?.id);
      await openTicket(id);
      await loadList();
      window.SWToolRequirementsMilestones?.load();
    })({ currentTarget: event.currentTarget.querySelector('button[type="submit"]') });
  };
  if (hub.localCenter) {
    const score = item.rating?.score ?? item.rating;
    const ratingSummary = document.createElement("p");
    ratingSummary.textContent = (Number.isFinite(score) ? "评分：" + score + " / 5" : "暂无评分") +
      (Number.isInteger(item.ratingCount) ? " · " + item.ratingCount + " 人评价" : "") +
      (item.ratedByViewer ? " · 您已评价" : "");
    detail.append(ratingSummary);
    if (!item.ratedByViewer) {
    const generation = hub.generation;
    const ratingForm = document.createElement("form");
    const currentRating = () => generation === hub.generation && hub.selected === id && ratingForm.isConnected;
    ratingForm.className = "hub-dialog-form";
    ratingForm.innerHTML = '<label>评分（1–5）<select name="rating"><option>1</option><option>2</option><option>3</option><option>4</option><option>5</option></select></label><button type="submit">提交评分</button><p role="status"></p>';
    if (Number.isInteger(score)) ratingForm.elements.namedItem("rating").value = String(score);
    ratingForm.onsubmit = async (event) => {
      event.preventDefault();
      if (!currentRating()) return;
      const button = ratingForm.querySelector("button"), message = ratingForm.querySelector("p");
      if (button.disabled) return;
      button.disabled = true;
      try {
        const rating = Number(ratingForm.elements.namedItem("rating").value);
        if (!Number.isInteger(rating) || rating < 1 || rating > 5) throw Error("请选择 1–5 分");
        await api("/api/tickets/" + encodeURIComponent(id) + "/rating", json("POST", {rating}));
        if (!currentRating()) return;
        message.textContent = "评分已保存";
        await openTicket(id);
      } catch (error) { if (currentRating() && !isWorkspaceChanged(error)) message.textContent = error.message; }
      finally { if (currentRating()) button.disabled = false; }
    };
    detail.append(ratingForm);
    }
  }
  document
    .querySelectorAll("#hub-list button")
    .forEach((button) =>
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.ticketId === item.id),
      ),
    );
}
window.SWToolOpenRequirement = (id) => safe(() => openTicket(id))();
async function upload(id, files, commentId, send = api) {
  for (const file of files) {
    const form = new FormData();
    form.append("file", file);
    await send(
      "/api/tickets/" +
        id +
        "/attachments" +
        (commentId ? "?comment_id=" + commentId : ""),
      { method: "POST", body: form },
    );
  }
}
const MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024;
function createImagePasteController(form, enabled, render) {
  let files = [];
  let message = "";
  const draw = () => render(files.slice(), (index) => {
    files = files.filter((_, position) => position !== index);
    draw();
  }, message);
  const onPaste = (event) => {
    if (typeof enabled === "function" ? !enabled() : !enabled) return;
    const images = Array.from(event.clipboardData?.items || [])
      .filter((item) => item.kind === "file" && /^image\//i.test(item.type || ""))
      .map((item) => item.getAsFile())
      .filter(Boolean);
    if (!images.length) return;
    event.preventDefault();
    const valid = images.filter((file) => /^image\//i.test(file.type || "") && file.size <= MAX_ATTACHMENT_SIZE);
    message = valid.length !== images.length ? "每张图片不得超过 10 MiB，超出限制的图片未添加" : "";
    files.push(...valid);
    draw();
  };
  if (enabled) form.addEventListener("paste", onPaste);
  return {
    files: () => files.slice(),
    clear() { files = []; message = ""; draw(); },
    dispose() { form.removeEventListener?.("paste", onPaste); files = []; message = ""; draw(); pendingImagePastes.delete(this); },
    async submit(uploadFiles, id, selectedFiles, commentId) {
      await uploadFiles(id, [...selectedFiles, ...files], commentId);
      files = [];
      draw();
    },
  };
}
const pendingImagePastes = new Set();
function clearPendingImagePastes(root) {
  for (const controller of [...pendingImagePastes]) {
    if (!root || root === controller.form || root.contains?.(controller.form) || containsNode(root, controller.form)) controller.dispose();
  }
}
function containsNode(root, node) {
  for (let current = node; current; current = current.parentElement) if (current === root) return true;
  return false;
}
function attachImagePaste(form) {
  const list = form.querySelector(".hub-pasted-files");
  const controller = createImagePasteController(form, () =>
    canWrite() && form.isConnected && !form.hidden &&
    !form.elements.namedItem("files").disabled && !form.querySelector('button[type="submit"]').disabled,
    (files, remove, message = "") => {
      list.replaceChildren();
      if (message) {
        const status = document.createElement("p"); status.setAttribute("role", "status"); status.textContent = message; list.append(status);
      }
      files.forEach((file, index) => {
        const row = document.createElement("span"), name = document.createElement("span"), button = document.createElement("button");
        name.textContent = file.name || "剪贴板图片";
        button.type = "button"; button.textContent = "移除"; button.setAttribute("aria-label", "移除 " + (file.name || "剪贴板图片"));
        button.onclick = () => remove?.(index);
        row.append(name, button); list.append(row);
      });
    });
  controller.form = form;
  pendingImagePastes.add(controller);
  return controller;
}
  function attachParentPicker(targetForm, fetchTickets, currentContext = () => "", expectedCenter = "default") {
    if (!targetForm) return null;
    const context = currentContext(); let sequence = 0, allowed = new Set();
    const group = document.createElement("fieldset"); group.className = "ticket-parent-picker";
    const legend = document.createElement("legend"); legend.textContent = "父工单（可选）";
    const label = document.createElement("label"), input = document.createElement("input"); label.textContent = "搜索父工单标题"; input.type = "search"; input.maxLength = 100; label.append(input);
    const search = document.createElement("button"); search.type = "button"; search.textContent = "搜索父工单";
    const choiceLabel = document.createElement("label"), select = document.createElement("select"); choiceLabel.textContent = "父工单"; choiceLabel.append(select);
    const message = document.createElement("p"); message.setAttribute("role", "status"); message.setAttribute("aria-live", "polite");
    const reset = () => { const none = document.createElement("option"); none.value = ""; none.textContent = "无父工单"; select.replaceChildren(none); select.value = ""; allowed = new Set(); };
    reset(); group.append(legend, label, search, choiceLabel, message); targetForm.prepend(group);
    search.onclick = async () => {
      const requestSequence = ++sequence; reset();
      if (context !== currentContext()) { message.textContent = "中心已切换，请重新打开创建表单。"; return; }
      search.disabled = true; message.textContent = "正在搜索父工单…";
      try {
        const result = await fetchTickets("/api/tickets?" + new URLSearchParams({q: input.value.trim(), page_size: "20", page: "1"}));
        if (requestSequence !== sequence || context !== currentContext() || !group.isConnected) return;
        const items = (Array.isArray(result.items) ? result.items : (Array.isArray(result.tickets) ? result.tickets : [])).slice(0, 20).filter((ticket) => ticket && (ticket.center_id == null || String(ticket.center_id) === String(expectedCenter)));
        for (const ticket of items.slice(0, 20)) { if (!ticket?.id) continue; const option = document.createElement("option"); option.value = String(ticket.id); option.textContent = String(ticket.title || "未命名需求"); select.append(option); allowed.add(option.value); }
        message.textContent = items.length ? "请选择父工单，或保留无父工单。" : "没有同中心匹配项，请用更精确关键字搜索，或保留无父工单。";
      } catch { if (requestSequence === sequence && context === currentContext()) message.textContent = "父工单搜索失败，可保留无父工单继续创建。"; }
      finally { if (requestSequence === sequence) search.disabled = false; }
    };
    return {value: () => context === currentContext() && allowed.has(select.value) ? select.value : null, reset};
  }
async function editForm(item = null) {
  if (!canWrite()) throw Error("目标 Key 未获写入能力，请轮换授权 Key 或在目标中心操作");
  if (item && !canEdit(item)) throw Error("只能编辑远端 Key 所属账号的需求");
  const localCenter = Boolean(hub.localCenter);
  const hostHome = Boolean(hostBridge && !hub.center && !hub.localCenter);
  const editStates = hostBridge ? {pending: "待审核", open: "待解决", in_progress: "处理中", resolved: "已解决"} : {...states, pending: "待审核"};
  const day = (value) =>
    new Date(value == null || value === "" ? Date.now() + 7 * 86400000 :
      typeof value === "number" || /^\d+(?:\.\d+)?$/.test(value) ? Number(value) * 1000 : value)
      .toISOString()
      .slice(0, 10);
  const visibility = item?.visibility === "private" ? "private" : "public";
  dialog(
    item ? "编辑需求" : "新增需求",
    '<form id="hub-editor" class="hub-dialog-form"><label>标题<input name="title" required maxlength="200" value="' +
      esc(item?.title || "") +
      '"></label><label>描述<textarea name="description" required>' +
      esc(item?.description || "") +
      '</textarea></label><label>标签（逗号分隔）<input name="labels" value="' +
      esc((item?.labels || []).join(",")) +
      '"></label><label>可见性<select name="visibility"><option value="public" ' + (visibility === "public" ? "selected" : "") + '>公开</option><option value="private" ' + (visibility === "private" ? "selected" : "") + '>私有</option></select></label><fieldset class="hub-platform-picker"><legend>关联平台</legend><div class="hub-platform-options" style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px"><label style="display:flex;align-items:center;min-height:42px;padding:0 10px"><input type="checkbox" name="platforms" value="h5_mobile">H5</label><label style="display:flex;align-items:center;min-height:42px;padding:0 10px"><input type="checkbox" name="platforms" value="pc_browser">PC浏览器</label><label style="display:flex;align-items:center;min-height:42px;padding:0 10px"><input type="checkbox" name="platforms" value="android">安卓</label><label style="display:flex;align-items:center;min-height:42px;padding:0 10px"><input type="checkbox" name="platforms" value="windows">Windows</label><label style="display:flex;align-items:center;min-height:42px;padding:0 10px"><input type="checkbox" name="platforms" value="ios">iOS手机</label><label style="display:flex;align-items:center;min-height:42px;padding:0 10px"><input type="checkbox" name="platforms" value="mac">iOS电脑</label><label style="display:flex;align-items:center;min-height:42px;padding:0 10px"><input type="checkbox" name="platforms" value="linux">Linux</label><label style="display:flex;align-items:center;min-height:42px;padding:0 10px"><input type="checkbox" name="platforms" value="embedded">嵌入式</label><label style="display:flex;align-items:center;min-height:42px;padding:0 10px"><input type="checkbox" name="platforms" value="other">其他</label></div><label id="hub-custom-platform-label" hidden>自定义平台名称<input name="custom_platform" maxlength="120" value="' + esc(item?.custom_platform || "") + '"></label></fieldset><label>预计解决日期<input name="due" type="date" required value="' +
      day(item?.expected_delivery_date ?? item?.expected_resolution_at) +
      '"></label>' +
      (item
        ? '<label>状态<select name="status">' +
          Object.entries(editStates)
            .map(
              ([key, label]) =>
                '<option value="' +
                key +
                '" ' +
                (key === item.status ? "selected" : "") +
                ">" +
                label +
                "</option>",
            )
            .join("") +
          "</select></label>"
        : "") +
      '<label>添加附件<input name="files" type="file" multiple accept="image/*,.pdf,.txt,.csv,.zip,.json"></label><div class="hub-pasted-files" aria-label="粘贴的图片"></div><button class="primary" type="submit">保存需求</button><p id="hub-editor-message" role="status"></p></form>',
  );
  const form = el("hub-editor");
  const editorContext = JSON.stringify([hub.generation, hub.center, hub.localCenter?.id || "", token]);
  const currentEditorContext = () => JSON.stringify([hub.generation, hub.center, hub.localCenter?.id || "", token]);
  const parentPicker = item ? null : attachParentPicker(form, ticketApi, currentEditorContext, hub.localCenter?.id || "default");
  let savedId = item?.id;
  let savedVersion = item?.updated_at;
  let savedStatus = item?.status;
  const platformInputs = [...form.querySelectorAll('input[name="platforms"]')];
  const existingPlatforms = Array.isArray(item?.platforms) ? item.platforms : (item?.platform ? [item.platform] : []);
  platformInputs.forEach((input) => { input.checked = existingPlatforms.includes(input.value); });
  const customPlatformLabel = form.querySelector("#hub-custom-platform-label"), otherInput = form.querySelector("input[value=other]");
  const updateCustomPlatform = () => { customPlatformLabel.hidden = !otherInput.checked; };
  otherInput.addEventListener("change", updateCustomPlatform); updateCustomPlatform();
  const editorImages = attachImagePaste(form);

  form.onsubmit = async (event) => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    const message = el("hub-editor-message");
    if (button.disabled) return;
    button.disabled = true;
    if (message) message.textContent = "正在保存…";
    try {
      if (editorContext !== currentEditorContext()) throw Error("中心已切换，请重新打开创建表单。");
      if (!String(form.elements.namedItem("description").value || "").trim()) {
        el("hub-editor-message").textContent = "请填写需求描述";
        button.disabled = false;
        return;
      }
      const platforms = platformInputs.filter((input) => input.checked).map((input) => input.value);
      if (!platforms.length) {
        el("hub-editor-message").textContent = "请先选择关联平台";
        button.disabled = false;
        return;
      }
      const data = {
        title: form.elements.namedItem("title").value,
        description: form.elements.namedItem("description").value,
        visibility: form.elements.namedItem("visibility").value,
        labels: form.elements
          .namedItem("labels")
          .value.split(/[,，]/)
          .map((s) => s.trim())
          .filter(Boolean),
        platforms,
        custom_platform: form.elements.namedItem("custom_platform").value.trim(),
      };
      if (!savedId) data.parent_ticket_id = parentPicker?.value() || null;
      data.expected_delivery_date = form.elements.namedItem("due").value;
      const selectedStatus = item ? form.elements.namedItem("status").value : null;
      if (hostHome && savedId) {
        delete data.labels;
        data.if_updated_at = savedVersion;
      } else if (item) {
        data.status = selectedStatus;
        if (localCenter) data.if_updated_at = savedVersion;
      }
      let result = await ticketApi(
        "/api/tickets" + (savedId ? "/" + savedId : ""),
        json(savedId ? "PATCH" : "POST", data),
      );
      if (editorContext !== currentEditorContext()) throw Error("中心已切换，请重新打开需求。");
      savedId = result.id;
      savedVersion = result.updated_at;
      savedStatus = result.status;
      if (hostHome && selectedStatus && selectedStatus !== savedStatus) {
        result = await ticketApi("/api/tickets/" + savedId, json("PATCH", {status: selectedStatus}));
        savedVersion = result.updated_at;
      }
      savedStatus = result.status;
      await editorImages.submit((id, files, commentId) => upload(id, files, commentId, ticketApi), result.id, [...form.elements.namedItem("files").files]);
      if (editorContext !== currentEditorContext()) return;
      if (message) message.textContent = "保存成功，正在刷新…";
      closeDialog();
      hub.selected = result.id;
      if (!item) {
        el("hub-search").value = "";
        el("hub-status").value = "";
        hub.page = 1;
        hub.justCreated = {id: String(result.id), title: String(result.title || data.title || ""), generation: hub.generation, center: hub.center, local: hub.localCenter?.id || ""};
      }
      try {
        await loadList();
        if (editorContext !== currentEditorContext()) return;
        await openTicket(result.id);
      }
      catch (refreshError) { notify("已保存，但刷新失败；请手动刷新查看。", "error"); return; }
      if (editorContext !== currentEditorContext()) return;
      window.SWToolRequirementsMilestones?.load();
      notify("需求已保存");
    } catch (error) {
      if (editorContext === currentEditorContext() && el("hub-editor-message")) el("hub-editor-message").textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
  form.querySelector('button[type="submit"]').disabled = false;
}
window.SWToolOpenNewIssue = () => safe(() => editForm())();
el("hub-new").onclick = window.SWToolOpenNewIssue;
el("hub-source").onclick = safe(() => download('/api/requirements/source', 'requirements-center-source.zip', false));
function installDownloadsButton() {
  const existing = el("hub-tools").querySelector('[data-action="downloads"]');
  if (existing) existing.remove();
  if (!hub.identity?.roles?.includes("admin")) return;
  const downloadsButton = document.createElement("button");
  downloadsButton.type = "button";
  downloadsButton.dataset.action = "downloads";
  downloadsButton.textContent = "下载记录";
  downloadsButton.setAttribute("aria-label", "查看需求中心 ZIP 下载记录");
  downloadsButton.addEventListener("click", safe(async () => {
    const data = await request("/api/requirements/downloads", {method: "GET"}, false).then((response) => response.json());
    const escape = (value) => String(value ?? "").replace(/[&<>\"']/g, (character) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[character]));
    const rows = (data.records || data.items || []).map((row) => `${escape(row.created_at)} | ${escape(row.kind)} | ${escape(row.ip || row.client_ip)} | ${escape(row.bytes)} bytes`).join("<br>");
    const counts = Array.isArray(data.counts) ? Object.fromEntries(data.counts.map((item) => [item.kind, item.count])) : (data.counts || {});
    dialog("ZIP 下载记录", `源码 ZIP：${counts.source || 0} 次<br>插件 ZIP：${counts.plugin || 0} 次<br><br>${rows || "暂无记录"}`);
  }));
  el("hub-tools").append(downloadsButton);
}
installDownloadsButton();
el("hub-archive-menu").onclick = safe(() => dialog("导入导出", '<button id="hub-dialog-export">导出全部需求 ZIP</button><button id="hub-dialog-import">导入全部需求 ZIP</button>'));
el("hub-dialog-body").addEventListener("click", (event) => {
  if (event.target.id === "hub-dialog-export") { safe(() => download(hostBridge ? "/plugins/requirements-center/api/tools/export" : "/issues/export/zip", "requirements.zip", false))(); closeDialog(); }
  if (event.target.id === "hub-dialog-import") { closeDialog(); el("hub-import-file").value = ""; el("hub-import-file").click(); }
});
el("hub-import-file").onchange = safe(async () => {
  const file = el("hub-import-file").files[0];
  if (!file) return;
  if (!confirm("将导入需求；必须确认来源用户映射，重复ID或疑似标题重复拒绝，是否继续？"))
    return;
  const identityMap = prompt('请输入已确认的 source 用户ID到现有本地账号ID映射 JSON，例如 {"1":2}');
  if (!identityMap) return;
  const userMapping = JSON.parse(identityMap);
  if (!userMapping || Array.isArray(userMapping) || typeof userMapping !== "object") throw Error("用户映射必须是 JSON 对象");
  const ownershipConfirmed = hostBridge ? confirm("未映射的来源用户及其需求将归属当前管理员。明确确认接管这些需求的所有权？") : false;
  if (hostBridge && !ownershipConfirmed) return;
  if (hostBridge) {
    notify("正在校验并导入…");
    const result = await request("/plugins/requirements-center/api/tools/import", {method: "POST", body: (() => { const body = new FormData(); body.append("file", file); return body; })(), user_mapping: userMapping, ownership_confirmed: ownershipConfirmed}, false).then((response) => response.json());
    notify("导入结果：" + JSON.stringify(result));
    hub.selected = "";
    await loadList();
    window.SWToolRequirementsMilestones?.load();
    el("hub-import-file").value = "";
    return;
  }
  const body = new FormData();
  body.append("file", file);
  body.append("identity_map", identityMap);
  notify("正在校验并导入…");
  const result = await local("/issues/import/zip", { method: "POST", body });
  notify("导入结果：" + JSON.stringify(result));
  hub.selected = "";
  await loadList();
  window.SWToolRequirementsMilestones?.load();
  el("hub-import-file").value = "";
});
async function settings() {
  let generatedKey = "";
  const settingsSession = token, settingsGeneration = hub.generation;
  const info = await local("/api/requirements/key");
  await connections();
  if (settingsSession !== token || settingsGeneration !== hub.generation) return;
  const panel = el("hub-settings-panel");
  panel.hidden = false;
  panel.innerHTML =
    '<h2>创建中心</h2><p><a href="' + hubHomePath + '">返回需求 Hub</a></p>' +
    (hostBridge ? '<h3>近端中心</h3><p>数据由服务器保存，创建者拥有该中心的管理权限。</p><button id="hub-local-add">添加近端中心</button><button id="hub-local-retry">刷新近端中心</button><p id="hub-local-message" role="status"></p><div id="hub-local-centers"></div>' : '') +
    '<h3>我的个人 Key</h3><p class="muted">Key 权限以服务器授权为准；可通过查看恢复，旧版不可恢复的 Key 请轮换。请仅通过可信渠道交给目标中心。</p><input id="hub-key-value" readonly aria-label="个人 Key" value="' +
      (info.exists ? "•••••• · " + esc(info.suffix) : "尚未生成") +
      '"><label>新密钥权限<select id="hub-key-scope"><option value="tickets:read">只读本人需求</option><option value="tickets:write">读取并写入本人需求</option></select></label><div class="hub-key-actions"><button id="hub-key-create">' +
      (info.exists ? "轮换 Key" : "生成 Key") +
      '</button><button id="hub-key-eye" aria-label="查看个人 Key">👀 查看</button><button id="hub-key-copy">复制</button><button id="hub-key-revoke">撤销</button></div><h3>远端中心</h3><button id="hub-connection-add">添加远端中心</button><div id="hub-connections"></div><p id="hub-settings-message" role="status"></p>';
  const keyField = el("hub-key-value");
  const currentSettings = () => settingsSession === token && (!hostBridge || hostBridge.session() === settingsSession) && settingsGeneration === hub.generation && el("hub-key-value") === keyField && keyField.isConnected;
  const action = (handler) => async () => {
    try {
      if (!currentSettings()) return;
      await handler();
    } catch (error) {
      if (currentSettings()) el("hub-settings-message").textContent = error.message;
    }
  };
  el("hub-key-create").onclick = action(async () => {
    if (info.exists && !confirm("轮换会立即使旧 Key 失效，是否继续？")) return;
    const scope = el("hub-key-scope").value;
    if (!["tickets:read", "tickets:write"].includes(scope)) throw new Error("请选择有效的密钥权限。");
    const data = await local("/api/requirements/key/rotate", json("POST", {scope}));
    if (!currentSettings()) return;
    el("hub-key-value").value = data.key;
    generatedKey = data.key;
    info.exists = true;
    el("hub-key-create").textContent = "轮换 Key";
  });
  el("hub-key-eye").onclick = action(async () => {
    if (!generatedKey) {
      let data;
      try {
        data = await local("/api/requirements/key/reveal", {method: "POST"});
      } catch (error) {
        if (error.status === 409) throw Error("旧 Key 无法恢复，请轮换");
        throw error;
      }
      if (!currentSettings()) return;
      if (!data.key) throw Error("旧版 Key 无法恢复，请轮换");
      generatedKey = data.key;
    }
    keyField.value = generatedKey;
  });
  el("hub-key-copy").onclick = action(async () => {
    if (!generatedKey) throw new Error("请先点击查看恢复 Key");
    await navigator.clipboard.writeText(generatedKey);
    if (!currentSettings()) return;
    el("hub-settings-message").textContent = "Key 已复制";
  });
  el("hub-key-revoke").onclick = action(async () => {
    if (!confirm("撤销后使用该 Key 的连接立即失效，是否继续？")) return;
    await local("/api/requirements/key", { method: "DELETE" });
    if (!currentSettings()) return;
    await settings();
  });
  if (hostBridge) {
  const refreshLocalCenters = async () => {
    try {
      const result = await local("/api/local-centers");
      if (!currentSettings()) return;
      el("hub-local-centers").replaceChildren();
      for (const center of result.centers || []) {
        const item = document.createElement("div");
        item.className = "hub-local-center";
        const link = document.createElement("a");
        link.href = hubHomePath + "?local_center=" + encodeURIComponent(center.id);
        link.textContent = center.name;
        item.append(link);
        const edit = document.createElement("button");
        edit.type = "button"; edit.textContent = "编辑";
        edit.onclick = () => {
          dialog("编辑近端中心", '<form id="hub-local-edit-form" class="hub-dialog-form"><label>名称<input name="name" required maxlength="100" value="' + esc(center.name) + '"></label><button class="primary" type="submit">保存</button><p id="hub-local-edit-message" role="status"></p></form>');
          const form = el("hub-local-edit-form");
          form.onsubmit = async (event) => { event.preventDefault(); const button = form.querySelector("button"); button.disabled = true; try { await local("/api/local-centers/" + encodeURIComponent(center.id), json("PATCH", {name: form.elements.namedItem("name").value})); closeDialog(); await refreshLocalCenters(); } catch (error) { el("hub-local-edit-message").textContent = error.message; } finally { button.disabled = false; } };
        };
        const remove = document.createElement("button");
        remove.type = "button"; remove.textContent = "移除";
        remove.onclick = action(async () => { if (!confirm("确认移除该近端中心？有工单时需要先处理工单。")) return; await local("/api/local-centers/" + encodeURIComponent(center.id), {method: "DELETE"}); await refreshLocalCenters(); });
        item.append(edit, remove); el("hub-local-centers").append(item);
      }
      el("hub-local-message").textContent = result.centers?.length ? "" : "暂无近端中心";
    } catch (error) { if (currentSettings()) el("hub-local-message").textContent = error.message + "；可点击刷新重试"; }
  };
  el("hub-local-retry").onclick = refreshLocalCenters;
  el("hub-local-add").onclick = () => {
    dialog("添加近端中心", '<form id="hub-local-form" class="hub-dialog-form"><p>数据由服务器保存，创建者拥有该中心的管理权限。</p><label>名称<input name="name" required maxlength="80"></label><button class="primary" type="submit">创建中心</button><p id="hub-local-form-message" role="status"></p></form>');
    const form = el("hub-local-form");
    form.onsubmit = async (event) => {
      event.preventDefault();
      const button = form.querySelector("button");
      button.disabled = true;
      try {
        const name = form.elements.namedItem("name").value.trim();
        if (!name) throw Error("请填写名称");
        const result = await local("/api/local-centers", json("POST", {name}));
        if (!currentSettings() || !form.isConnected) return;
        location.href = hubHomePath + "?local_center=" + encodeURIComponent(result.center.id);
      } catch (error) { if (currentSettings() && form.isConnected) el("hub-local-form-message").textContent = error.message; }
      finally { button.disabled = false; }
    };
  };
  void refreshLocalCenters();
  }
  const connectionForm = (item = null) => {
    dialog(item ? "编辑远端中心" : "添加远端中心", '<form id="hub-connection-form" class="hub-dialog-form"><label>名称<input name="name" required maxlength="80" value="' + esc(item?.name || "") + '"></label><label>网址<input name="base_url" type="url" required value="' + esc(item?.base_url || item?.url || "") + '"></label><label>目标用户 Key<input name="key" type="password" autocomplete="off" required></label><button class="primary" type="submit">验证身份并保存</button><p id="hub-connection-message" role="status">' + (item ? "请重新输入目标用户 Key" : "") + '</p></form>');
    const activeForm = el("hub-connection-form");
    activeForm.onsubmit = async (event) => {
      event.preventDefault();
      const form = event.currentTarget,
        button = form.querySelector("button");
      button.disabled = true;
      try {
        await local(
          "/api/requirements/connections",
          json("POST", {
            name: form.elements.namedItem("name").value,
            base_url: form.elements.namedItem("base_url").value,
            key: form.elements.namedItem("key").value,
            ...(item ? {id: item.id} : {}),
          }),
        );
        form.elements.namedItem("key").value = "";
        if (!currentSettings() || !form.isConnected) return;
        closeDialog();
        await settings();
      } catch (error) {
        if (currentSettings() && form.isConnected) el("hub-connection-message").textContent = error.message;
      } finally {
        button.disabled = false;
      }
    };
  };
  el("hub-connection-add").onclick = () => connectionForm();
  for (const item of hub.connections) {
    const block = document.createElement("div");
    block.className = "hub-connection";
    const centerUrl = new URL("/requirements-center.html", item.base_url).href;
    block.innerHTML =
      "<strong>" +
      '<a href="' + esc(centerUrl) + '" target="_blank" rel="noopener">' + esc(item.name) + '</a>' +
      "</strong><small>" +
      '<a href="' + esc(centerUrl) + '" target="_blank" rel="noopener">' + esc(item.base_url) + '</a>' +
      "</small>";
    const actions = document.createElement("div");
    actions.className = "hub-connection-actions";
    const edit = document.createElement("button");
    edit.textContent = "编辑";
    edit.onclick = () => connectionForm(item);
    actions.append(edit);
    const remove = document.createElement("button");
    remove.textContent = "移除连接";
    remove.onclick = action(async () => {
      await local("/api/requirements/connections/" + item.id, {
        method: "DELETE",
      });
      if (!currentSettings()) return;
      await settings();
    });
    actions.append(remove);
    block.append(actions);
    el("hub-connections").append(block);
  }
}
el("hub-settings").onclick = () => { location.href = hubCreatePath; };
el("hub-plugins").onclick = safe(async()=>{
  dialog('插件','<div id="hub-plugin-manager"></div>');
  await window.SWToolPlugins.render(el('hub-plugin-manager'),local,{admin:hub.user.roles.includes('admin'),onchange:async()=>{const state=await local('/api/plugins/requirements-center');requirementsActive=state.active;el('issuesPanel').hidden=!state.active||createPage;if(state.active){if(createPage)await settings();else await switchCenter('')}}});
});
el("hub-admin").onclick = safe(async () => {
  const data = await local("/admin/users");
  dialog(
    "账号审核与密码重置",
    '<p class="muted">注册用户审核通过即可登录。新密码至少 8 位。</p><div id="hub-accounts"></div>',
  );
  for (const user of data.items) {
    const block = document.createElement("section");
    block.className = "hub-account";
    block.innerHTML =
      "<strong>" +
      esc(user.username) +
      "</strong><span>" +
      esc(
        { active: "正常", pending: "待审核", rejected: "未通过" }[
          user.status
        ] || user.status,
      ) +
      "</span>";
    const message = document.createElement("p");
    message.setAttribute("role", "status");
    if (user.status === "pending") {
      for (const [state, label] of [
        ["active", "审核通过"],
        ["rejected", "拒绝注册"],
      ]) {
        const button = document.createElement("button");
        button.textContent = label;
        button.onclick = async () => {
          try {
            await local(
              "/admin/users/" + user.user_id + "/status",
              json("PATCH", { status: state }),
            );
            el("hub-admin").click();
          } catch (error) {
            message.textContent = error.message;
          }
        };
        block.append(button);
      }
    }
    const form = document.createElement("form");
    form.className = "hub-dialog-form";
    form.innerHTML =
      '<label>新密码<input name="password" type="password" autocomplete="new-password" required minlength="8" maxlength="200"></label><button type="submit">重置密码</button>';
    form.onsubmit = async (event) => {
      event.preventDefault();
      const button = form.querySelector("button");
      button.disabled = true;
      try {
        await local(
          "/admin/users/" + user.user_id + "/password",
          json("PATCH", {
            password: form.elements.namedItem("password").value,
          }),
        );
        form.elements.namedItem("password").value = "";
        message.textContent = "密码已重置，请安全通知用户";
      } catch (error) {
        message.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    };
    block.append(form, message);
    el("hub-accounts").append(block);
  }
});
window.addEventListener("storage", (event) => {
  if (hostBridge) return;
  if (event.key === "ws_token" && event.newValue !== token) {
    logout();
  }
});
function setupMobileNav() {
  ensureThemeControlVisible();
  const nav = document.querySelector(".hub-header nav.actions");
  if (!nav) return;
  const mobile = window.matchMedia?.("(max-width: 760px)").matches;
  let menu = nav.querySelector(".hub-mobile-nav");
  const ids = ["hub-settings", "hub-source", "hub-plugins", "hub-admin"];
  const items = ids.map((id) => el(id)).filter(Boolean);
  const guide = [...nav.querySelectorAll("a")].find((link) => link.textContent.trim() === "使用指南");
  if (guide) items.splice(2, 0, guide);
  if (mobile && !menu) {
    menu = document.createElement("details");
    menu.className = "hub-mobile-nav";
    menu.innerHTML = '<summary>导航</summary><div class="hub-mobile-nav-menu"></div>';
    nav.insertBefore(menu, el("hub-logout"));
    menu.dataset.items = "moved";
    items.forEach((item) => menu.querySelector(".hub-mobile-nav-menu").append(item));
  } else if (!mobile && menu) {
    items.forEach((item) => nav.insertBefore(item, menu));
    menu.remove();
  }
}
setupMobileNav();
let mobileLayout = window.matchMedia?.("(max-width: 760px)").matches;
function handleLayoutResize() {
  const nextMobile = window.matchMedia?.("(max-width: 760px)").matches;
  if (nextMobile === mobileLayout) return;
  mobileLayout = nextMobile;
  placeMobileDetail();
  setupMobileNav();
}
addEventListener("resize", handleLayoutResize);
if (token && !legacySettings) boot();

window.SWToolRequirementsContext = () => hub.localCenter ? "local:" + hub.localCenter.id : hub.center;

// Requirement detail presentation and attachment handling.
