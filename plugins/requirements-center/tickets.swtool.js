/*
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 上海商江网络科技有限公司版权所有
软工云（swtool.com）开发团队；uqtool@163.com
*/
(function () {
  const byId = (id) => document.getElementById(id);
  const form = byId("ticketForm");
  if (!byId("ticketHistory")) return;
  const elements = {
    access: byId("ticketAccess"), status: byId("ticketStatus"), history: byId("ticketHistory"),
    refresh: byId("ticketRefresh"), detailEmpty: byId("ticketDetailEmpty"), detailContent: byId("ticketDetailContent"),
    detailMeta: byId("ticketDetailMeta"), detailTitle: byId("ticketDetailTitle"), titleStatus: byId("ticketTitleStatus"), titleControls: byId("ticketTitleControls"), detailDescription: byId("ticketDetailDescription"),
    ratingForm: byId("ticketRatingForm"), ratingComment: byId("ticketRatingComment"), ratingHint: byId("ticketRatingHint"), ratingSummary: byId("ticketRatingSummary"),
    platforms: () => form ? Array.from(form.querySelectorAll('input[name="ticketPlatform"]:checked')).map((input) => input.value) : [],
    controls: byId("ticketStateControls"), comments: byId("ticketComments"), replyForm: byId("ticketReplyForm"),
    reply: byId("ticketReply"), replyHint: byId("ticketReplyHint"), attachmentInput: byId("ticketAttachments"),
    attachmentPreviews: byId("ticketAttachmentPreviews"), replyAttachmentInput: byId("ticketReplyAttachments"),
    replyAttachmentPreviews: byId("ticketReplyAttachmentPreviews"), detailAttachments: byId("ticketDetailAttachments"),
    searchForm: byId("ticketSearchForm"), search: byId("ticketSearch"), searchClear: byId("ticketSearchClear"),
    previous: byId("ticketPrevious"), next: byId("ticketNext"), pageStatus: byId("ticketPageStatus"),
    milestones: byId("ticketMilestones"), milestoneSummary: byId("ticketMilestonesSummary"),
    milestoneViewport: byId("ticketMilestonesViewport"), milestoneTrack: byId("ticketMilestonesTrack"),
    milestoneHint: byId("ticketMilestonesHint"), milestoneToggle: byId("ticketMilestonesToggle"), milestoneZoom: byId("ticketMilestonesZoom")
  };
  const initialParams = new URLSearchParams(location.search);
  const initialPage = Number.parseInt(initialParams.get("page") || "1", 10);
  const state = {auth: null, tickets: [], milestoneTickets: [], selected: null, detailRequestSeq: 0, attachmentFiles: [], replyAttachmentFiles: [], q: (initialParams.get("q") || "").trim().slice(0, 100), page: Number.isInteger(initialPage) && initialPage > 0 ? initialPage : 1, pageSize: 10, total: 0, totalPages: 0, milestoneVisible: true, milestoneExpanded: false, milestoneZoom: {scale: 1, center: 0.5}};
  const statusNames = {open: "待处理", in_progress: "处理中", pending: "待补充", resolved: "已解决", closed: "已关闭"};
  const visibilityNames = {public: "公开", private: "私有"};
  const formatTime = (value) => value ? new Intl.DateTimeFormat("zh-CN", {dateStyle: "medium", timeStyle: "short"}).format(new Date(value)) : "";
  const isAdmin = () => state.auth && ["admin", "superadmin"].includes(state.auth.user && state.auth.user.role);
  const canWrite = () => state.auth && state.auth.authenticated && (isAdmin() || (state.auth.user && state.auth.user.emailVerified));
  const node = (tag, className, text) => { const item = document.createElement(tag); if (className) item.className = className; if (text !== undefined) item.textContent = text; return item; };
  const getMermaidRenderer = (candidate) => {
    const values = [candidate, candidate && candidate.default, candidate && candidate.mermaid];
    return values.find((value) => value && typeof value.render === "function") || null;
  };
  function renderTicketMermaid(container, source) {
    const drawing = node("div", "ticket-mermaid-drawing");
    drawing.setAttribute("aria-live", "polite");
    container.append(drawing);
    const mermaid = getMermaidRenderer(window.mermaid);
    if (!mermaid || typeof mermaid.render !== "function") {
      drawing.append(node("p", "ticket-mermaid-error", "流程图渲染不可用，请查看上方源码。"));
      return;
    }
    if (typeof mermaid.initialize === "function") {
      try { mermaid.initialize({startOnLoad: false, securityLevel: "strict"}); } catch (_) {}
    }
    const renderId = `ticket-mermaid-${Math.random().toString(36).slice(2)}`;
    let rendered;
    try { rendered = mermaid.render(renderId, String(source)); }
    catch (_) { drawing.replaceChildren(node("p", "ticket-mermaid-error", "流程图渲染失败，请查看上方源码。")); return; }
    Promise.resolve(rendered).then((result) => {
      const svg = result && typeof result === "object" ? result.svg : result;
      if (!svg) throw new Error("empty mermaid output");
      drawing.innerHTML = String(svg);
    }).catch(() => {
      drawing.replaceChildren(node("p", "ticket-mermaid-error", "流程图渲染失败，请查看上方源码。"));
    });
  }
  function renderMarkdown(target, source) {
    if (window.SwtoolTicketContent) return window.SwtoolTicketContent.render(target, source);
    target.replaceChildren();
    const lines = String(source || '').split(/\r?\n/);
    const inline = (parent, value) => {
      const pattern = /`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g;
      let start = 0, match;
      while ((match = pattern.exec(value))) {
        parent.append(document.createTextNode(value.slice(start, match.index)));
        if (match[1]) parent.append(node('code', '', match[1]));
        else { const link = node('a', '', match[2]); link.href = match[3]; link.target = '_blank'; link.rel = 'noopener noreferrer'; parent.append(link); }
        start = pattern.lastIndex;
      }
      parent.append(document.createTextNode(value.slice(start)));
    };
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) { index++; continue; }
      if (/^```/.test(line)) {
        const language = line.slice(3).trim().toLowerCase();
        const code = []; index++;
        while (index < lines.length && !/^```/.test(lines[index])) code.push(lines[index++]);
        if (index < lines.length) index++;
        const pre = node('pre', language === "mermaid" ? "ticket-mermaid-source" : "");
        pre.append(node('code', '', code.join('\n')));
        if (language === "mermaid") {
          const wrapper = node("div", "ticket-mermaid");
          wrapper.append(pre);
          renderTicketMermaid(wrapper, code.join("\n"));
          target.append(wrapper);
        } else target.append(pre);
        continue;
      }
      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) { const element = node(`h${heading[1].length}`); inline(element, heading[2]); target.append(element); index++; continue; }
      if (line.includes('|') && index + 1 < lines.length && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[index + 1])) {
        const table = node('table'), head = node('thead'), body = node('tbody');
        const cells = (value) => value.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim());
        const row = (values, tag) => { const tr = node('tr'); values.forEach((value) => { const cell = node(tag); inline(cell, value); tr.append(cell); }); return tr; };
        head.append(row(cells(line), 'th')); index += 2;
        while (index < lines.length && lines[index].trim() && lines[index].includes('|')) body.append(row(cells(lines[index++]), 'td'));
        table.append(head, body);
        const tableWrap = node("div", "ticket-markdown-table-wrap");
        tableWrap.append(table); target.append(tableWrap); continue;
      }
      if (/^[-*]\s+/.test(line)) {
        const list = node('ul');
        while (index < lines.length && /^[-*]\s+/.test(lines[index])) { const item = node('li'); inline(item, lines[index++].replace(/^[-*]\s+/, '')); list.append(item); }
        target.append(list); continue;
      }
      const paragraph = node('p');
      while (index < lines.length && lines[index].trim() && !/^```|^#{1,3}\s|^[-*]\s/.test(lines[index])) {
        if (paragraph.childNodes.length) paragraph.append(node('br'));
        inline(paragraph, lines[index++]);
      }
      target.append(paragraph);
    }
  }
  const request = async (url, options) => {
    const response = await fetch(url, Object.assign({credentials: "same-origin", cache: "no-store"}, options || {}));
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `请求失败（${response.status}）`);
    return payload;
  };
  const writeOptions = (method, body) => ({method, headers: {"Content-Type": "application/json", "X-CSRF-Token": state.auth.csrfToken || ""}, body: JSON.stringify(body)});
  const setMessage = (message, error) => { if (!elements.status) return; elements.status.textContent = message || ""; elements.status.classList.toggle("is-error", Boolean(error)); };
  let pendingTicket = null, pendingReply = null, createBusy = false, replyBusy = false;
  const uploadContext = () => JSON.stringify([state.auth?.user?.id || "", state.auth?.csrfToken || "", location.origin]);
  const uploadSend = (path, options = {}) => request(path.replace(/^\/api(?=\/)/, "/api"), {
    ...options, headers: {...options.headers, "X-CSRF-Token": state.auth?.csrfToken || ""}
  });
  const uploader = () => {
    if (!window.SWToolAttachmentUploads) throw Error("附件上传组件尚未加载，请刷新页面");
    return window.SWToolAttachmentUploads;
  };
  const lockForm = (target) => {
    const controls = [...target.querySelectorAll("input, textarea, select, button")];
    const previous = controls.map(control => control.disabled);
    controls.forEach(control => { control.disabled = true; });
    return () => controls.forEach((control, index) => { control.disabled = previous[index]; });
  };
  const previewError = "当前浏览器无法预览 HEIC/HEIF，请先转换为 JPEG 或 PNG 后重试";
  const isUnsupportedImage = (file) => /^(?:image\/heic|image\/heif)$/i.test(file.type || "") || /\.(?:heic|heif)$/i.test(file.name || "");

  function updateUrl(ticketId) {
    const params = new URLSearchParams();
    if (state.q) params.set("q", state.q);
    if (state.page > 1) params.set("page", String(state.page));
    const selectedId = ticketId === undefined ? state.selected && state.selected.id : ticketId;
    if (selectedId) params.set("ticket", selectedId);
    history.replaceState(null, "", `/tickets${params.size ? `?${params}` : ""}`);
  }

  function renderAttachmentPreviews() {
    elements.attachmentPreviews.replaceChildren();
    state.attachmentFiles.forEach((item, index) => {
      const card = node("div", "ticket-attachment-preview");
      let preview = item.invalid ? node("span", "ticket-attachment-file ticket-attachment-load-error", "预览不可用") : (item.file.type.startsWith("image/") ? Object.assign(document.createElement("img"), {src: item.url, alt: item.file.name}) : node("span", "ticket-attachment-file", item.file.type || "附件"));
      if (!item.invalid && preview.tagName === "IMG") preview.addEventListener("error", () => { item.invalid = true; preview.replaceWith(node("span", "ticket-attachment-file ticket-attachment-load-error", "预览不可用")); setMessage(previewError, true); }, {once: true});
      const remove = node("button", "", "移除"); remove.type = "button"; remove.setAttribute("aria-label", `移除附件 ${item.file.name}`);
      remove.addEventListener("click", () => { URL.revokeObjectURL(item.url); state.attachmentFiles.splice(index, 1); renderAttachmentPreviews(); });
      card.append(preview, node("span", "", item.file.name), remove); elements.attachmentPreviews.append(card);
    });
  }

  function selectAttachments(files) {
    const selected = Array.from(files || []);
    try { uploader().validate(selected); }
    catch (error) { setMessage(error.message, true); elements.attachmentInput.value = ""; return; }
    state.attachmentFiles.forEach((item) => URL.revokeObjectURL(item.url));
    state.attachmentFiles = selected.map((file) => ({file, url: URL.createObjectURL(file), invalid: isUnsupportedImage(file)})); renderAttachmentPreviews(); setMessage(state.attachmentFiles.some((item) => item.invalid) ? previewError : "", state.attachmentFiles.some((item) => item.invalid));
  }

  function renderReplyAttachmentPreviews() {
    elements.replyAttachmentPreviews.replaceChildren();
    state.replyAttachmentFiles.forEach((item, index) => {
      const card = node("div", "ticket-attachment-preview");
      let preview = item.invalid ? node("span", "ticket-attachment-file ticket-attachment-load-error", "预览不可用") : (item.file.type.startsWith("image/") ? Object.assign(document.createElement("img"), {src: item.url, alt: item.file.name}) : node("span", "ticket-attachment-file", item.file.type || "附件"));
      if (!item.invalid && preview.tagName === "IMG") preview.addEventListener("error", () => { item.invalid = true; preview.replaceWith(node("span", "ticket-attachment-file ticket-attachment-load-error", "预览不可用")); setMessage(previewError, true); }, {once: true});
      const remove = node("button", "", "移除"); remove.type = "button"; remove.setAttribute("aria-label", `移除附件 ${item.file.name}`);
      remove.addEventListener("click", () => { URL.revokeObjectURL(item.url); state.replyAttachmentFiles.splice(index, 1); renderReplyAttachmentPreviews(); });
      card.append(preview, node("span", "", item.file.name), remove); elements.replyAttachmentPreviews.append(card);
    });
  }

  function selectReplyAttachments(files) {
    const selected = Array.from(files || []);
    try { uploader().validate(selected); }
    catch (error) { setMessage(error.message, true); elements.replyAttachmentInput.value = ""; return; }
    state.replyAttachmentFiles.forEach((item) => URL.revokeObjectURL(item.url));
    state.replyAttachmentFiles = selected.map((file) => ({file, url: URL.createObjectURL(file), invalid: isUnsupportedImage(file)})); renderReplyAttachmentPreviews(); setMessage(state.replyAttachmentFiles.some((item) => item.invalid) ? previewError : "", state.replyAttachmentFiles.some((item) => item.invalid));
  }

  function clearReplyAttachments() {
    state.replyAttachmentFiles.forEach((item) => URL.revokeObjectURL(item.url));
    state.replyAttachmentFiles = []; elements.replyAttachmentInput.value = ""; renderReplyAttachmentPreviews();
  }


  if (elements.ratingForm) {
    elements.ratingForm.querySelectorAll(".ticket-rating-stars label").forEach((label) => {
      const input = label.querySelector("input");
      input?.addEventListener("change", syncRatingStars);
      label.addEventListener("mouseenter", () => { elements.ratingForm.dataset.hovered = input?.value || ""; syncRatingStars(); });
      label.addEventListener("mouseleave", () => { elements.ratingForm.dataset.hovered = ""; syncRatingStars(); });
    });
  }

  function renderAccess() {
    if (!elements.access) return;
    if (!state.auth || !state.auth.authenticated) {
      elements.access.replaceChildren(document.createTextNode("游客可查看全部公开需求。"), node("a", "guide-link", "登录后新增需求"));
      elements.access.lastChild.href = "/ticket_submit.html"; if (form) form.hidden = true; return;
    }
    if (!canWrite()) {
      elements.access.replaceChildren(document.createTextNode("新增或补充需求前，请先完成邮箱验证。"), node("a", "guide-link", "前往账户验证"));
      elements.access.lastChild.href = "/keys"; if (form) form.hidden = true; return;
    }
    elements.access.textContent = isAdmin() ? "管理员可新增、回复并管理全部需求状态。" : "邮箱已验证，可以新增公开需求并补充自己的需求。";
    if (form) form.hidden = false;
  }

  function renderMilestones() {
    if (!elements.milestoneTrack || !elements.milestoneSummary) return;
    elements.milestoneTrack.replaceChildren();
    if (!state.milestoneTickets.length) {
      elements.milestoneSummary.textContent = "暂无公开需求里程碑。";
      elements.milestoneTrack.append(node("p", "ticket-empty", "暂无公开需求。"));
      return;
    }
    const tickets = [...state.milestoneTickets].sort((left, right) => {
      const leftTime = Date.parse(left.created_at || "") || 0;
      const rightTime = Date.parse(right.created_at || "") || 0;
      return leftTime - rightTime || String(left.id || "").localeCompare(String(right.id || ""));
    });
    const closedCount = tickets.filter((ticket) => ["closed", "resolved"].includes(String(ticket.status || "").toLowerCase())).length;
    elements.milestoneSummary.textContent = `全部 ${tickets.length} 个公开需求 · ${closedCount} 个已完成 · 按日期累计，从远到近排列`;
    const groups = [];
    tickets.forEach((ticket, index) => {
      const date = ticket.created_at ? new Intl.DateTimeFormat("zh-CN", {year: "numeric", month: "2-digit", day: "2-digit"}).format(new Date(ticket.created_at)) : "时间未知";
      let group = groups[groups.length - 1];
      if (!group || group.date !== date) { group = {date, items: []}; groups.push(group); }
      group.items.push({ticket, index});
    });
    const availableHeight = elements.milestoneViewport && elements.milestoneViewport.clientHeight;
    const isMobile = window.matchMedia?.("(max-width: 760px)").matches;
    const width = isMobile
      ? Math.max(320, Math.min(720, document.documentElement.clientWidth || 420))
      : 1000;
    const viewportHeight = window.visualViewport?.height || window.innerHeight || 700;
    const mobileHeight = Math.max(440, Math.min(520, Math.round(viewportHeight * 0.6)));
    const height = state.milestoneExpanded && availableHeight
      ? Math.max(260, Math.min(560, availableHeight - 22))
      : isMobile ? mobileHeight : 320;
    const left = 50;
    const right = width - 24;
    const top = 28;
    const bottom = height - 52;
    const plotHeight = bottom - top;
    const maxValue = tickets.length;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.classList.add("ticket-cumulative-chart");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("width", String(width));
    svg.setAttribute("height", String(height));
    svg.style.setProperty("height", `${height}px`, "important");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", `公开需求历史累计曲线，共 ${tickets.length} 个需求`);
    const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    const gradient = document.createElementNS("http://www.w3.org/2000/svg", "linearGradient");
    gradient.setAttribute("id", "ticketCurveGradient"); gradient.setAttribute("x1", "0%"); gradient.setAttribute("x2", "100%");
    [["0%", "#74f0c0"], ["55%", "#f7d774"], ["100%", "#ff8a65"]].forEach(([offset, color]) => { const stop = document.createElementNS("http://www.w3.org/2000/svg", "stop"); stop.setAttribute("offset", offset); stop.setAttribute("stop-color", color); gradient.append(stop); });
    defs.append(gradient); svg.append(defs);
    const addSvg = (tag, attrs, textValue) => { const item = document.createElementNS("http://www.w3.org/2000/svg", tag); Object.entries(attrs || {}).forEach(([key, value]) => item.setAttribute(key, value)); if (textValue !== undefined) item.textContent = textValue; svg.append(item); return item; };
    for (let tick = 0; tick <= Math.min(maxValue, 5); tick += 1) {
      const value = Math.round(maxValue * tick / Math.min(maxValue, 5));
      const y = bottom - (value / maxValue) * plotHeight;
      addSvg("line", {x1: left, y1: y, x2: right, y2: y, class: "ticket-chart-grid"});
      addSvg("text", {x: left - 10, y: y + 4, class: "ticket-chart-axis-label", "text-anchor": "end"}, String(value));
    }
    const completedY = bottom - (closedCount / maxValue) * plotHeight;
    addSvg("line", {x1: left, y1: completedY, x2: right, y2: completedY, class: "ticket-chart-completed-line"});
    addSvg("text", {x: right - 4, y: completedY - 6, class: "ticket-chart-completed-label", "text-anchor": "end"}, `已完成 ${closedCount}`);
    addSvg("line", {x1: left, y1: top, x2: left, y2: bottom, class: "ticket-chart-axis"});
    addSvg("line", {x1: left, y1: bottom, x2: right, y2: bottom, class: "ticket-chart-axis"});
    const maxScale = Math.max(2, tickets.length);
    const zoom = state.milestoneZoom;
    zoom.scale = Math.max(1, Math.min(maxScale, Number(zoom.scale) || 1));
    const halfWindow = 0.5 / zoom.scale;
    const start = Math.max(0, Math.min(1 - halfWindow * 2, zoom.center - halfWindow));
    const end = Math.min(1, start + halfWindow * 2);
    zoom.center = (start + end) / 2;
    const xForRatio = (ratio) => left + ((ratio - start) / Math.max(0.0001, end - start)) * (right - left);
    const points = tickets.map((ticket, index) => {
      const ratio = tickets.length === 1 ? 0.5 : index / (tickets.length - 1);
      return {ticket, index, ratio, x: xForRatio(ratio), y: bottom - ((index + 1) / maxValue) * plotHeight, date: groups.find((group) => group.items.some((entry) => entry.index === index))?.date || "时间未知"};
    });
    const visiblePoints = points.filter((point) => point.ratio >= start - 0.02 && point.ratio <= end + 0.02);
    const pathData = visiblePoints.map((point, index) => {
      if (index === 0) return `M ${point.x} ${point.y}`;
      const previous = visiblePoints[index - 1];
      const control = Math.max(5, (point.x - previous.x) / 2);
      return `C ${previous.x + control} ${previous.y}, ${point.x - control} ${point.y}, ${point.x} ${point.y}`;
    }).join(" ");
    addSvg("path", {d: pathData, class: "ticket-chart-line"});
    const visibleDates = groups.filter((group) => {
      const ratio = tickets.length === 1 ? 0.5 : group.items[0].index / (tickets.length - 1);
      return ratio >= start && ratio <= end;
    });
    let lastDateX = -Infinity;
    visibleDates.forEach((group) => {
      const ratio = tickets.length === 1 ? 0.5 : group.items[0].index / (tickets.length - 1);
      const dateX = xForRatio(ratio);
      if (dateX - lastDateX >= 68 || zoom.scale >= maxScale * 0.7) {
        addSvg("text", {x: dateX, y: height - 18, class: "ticket-chart-date", "text-anchor": "middle"}, group.date);
        lastDateX = dateX;
      }
    });
    const truncate = (value, length) => { const text = String(value || "未命名需求"); return text.length > length ? `${text.slice(0, Math.max(1, length - 1))}…` : text; };
    const gap = points.length > 1 ? ((right - left) * zoom.scale) / (points.length - 1) : right - left;
    const step = Math.max(1, Math.ceil(28 / Math.max(1, gap)));
    const markerCandidates = points.filter((point) => point.ratio >= start && point.ratio <= end);
    const markerPoints = markerCandidates.filter((point, index) => index === 0 || index === markerCandidates.length - 1 || point.index % step === 0 || gap >= 24);
    const labelLayout = (point, pointIndex) => {
      const previous = markerPoints[pointIndex - 1];
      const next = markerPoints[pointIndex + 1];
      const nearestGap = Math.min(previous ? point.x - previous.x : Infinity, next ? next.x - point.x : Infinity);
      const dense = nearestGap < 68;
      const maxChars = dense ? Math.max(4, Math.min(7, Math.floor((nearestGap - 10) / 6))) : 11;
      const lane = pointIndex % 2;
      const preferBelow = lane === 1;
      const nearTop = point.y - 18 < top;
      const nearBottom = point.y + 28 > bottom;
      const below = nearTop || (!nearBottom && preferBelow);
      return {maxChars, y: point.y + (below ? 27 : -16), below};
    };
    const palettes = {
      light: ["#1769aa", "#8a4f08", "#a22c5b", "#177245", "#5b3aa4"],
      contrast: ["#00e5ff", "#ffe600", "#ff6b35", "#7dff5b", "#ff4fd8"],
      dark: ["#74f0c0", "#f7d774", "#ff8a65", "#82b1ff", "#e88cff"]
    };
    const palette = palettes[document.body.dataset.theme] || palettes.dark;
    markerPoints.forEach((point, pointIndex) => {
      const status = point.ticket.status || "open";
      const layout = labelLayout(point, pointIndex);
      const color = palette[point.index % palette.length];
      addSvg("text", {x: point.x, y: layout.y, class: `ticket-chart-point-label ticket-chart-point-label-${layout.below ? "below" : "above"}`, "text-anchor": "middle"}, truncate(point.ticket.title, layout.maxChars));
      const completed = ["closed", "resolved"].includes(String(status).toLowerCase());
      const circle = addSvg("circle", {cx: point.x, cy: point.y, r: 7, class: `ticket-chart-point ticket-chart-point-${status}`, style: `--ticket-point-color:${color}`, tabindex: "0", role: "button", "aria-label": `第 ${point.index + 1} 个需求：${point.ticket.title || "未命名需求"}，${point.date}，累计 ${point.index + 1} 个`});
      addSvg("circle", {cx: point.x, cy: point.y, r: 4, class: `ticket-chart-point-core ${completed ? "ticket-chart-point-core-completed" : "ticket-chart-point-core-incomplete"}`, "aria-hidden": "true"});
      const showTooltip = () => {
        const previous = svg.querySelector(".ticket-chart-tooltip");
        if (previous) previous.remove();
        const tooltipWidth = 190;
        const tooltipHeight = 50;
        const tooltipX = Math.max(4, Math.min(width - tooltipWidth - 4, point.x - tooltipWidth / 2));
        const tooltipY = point.y > tooltipHeight + 18 ? point.y - tooltipHeight - 16 : point.y + 16;
        const tooltip = document.createElementNS("http://www.w3.org/2000/svg", "g");
        tooltip.classList.add("ticket-chart-tooltip");
        tooltip.setAttribute("role", "status");
        const box = document.createElementNS("http://www.w3.org/2000/svg", "rect");
        box.setAttribute("x", tooltipX); box.setAttribute("y", tooltipY); box.setAttribute("width", tooltipWidth); box.setAttribute("height", tooltipHeight); box.setAttribute("rx", "4");
        const title = document.createElementNS("http://www.w3.org/2000/svg", "text");
        title.setAttribute("x", tooltipX + 10); title.setAttribute("y", tooltipY + 19); title.classList.add("ticket-chart-tooltip-title"); title.textContent = truncate(point.ticket.title, 25);
        const meta = document.createElementNS("http://www.w3.org/2000/svg", "text");
        meta.setAttribute("x", tooltipX + 10); meta.setAttribute("y", tooltipY + 38); meta.classList.add("ticket-chart-tooltip-meta"); meta.textContent = `${point.date} · 累计 ${point.index + 1}`;
        tooltip.append(box, title, meta); svg.append(tooltip);
      };
      const openDetail = () => { showTooltip(); loadDetail(point.ticket.id); };
      circle.addEventListener("click", openDetail);
      circle.addEventListener("mouseenter", showTooltip);
      circle.addEventListener("mouseleave", () => { if (document.activeElement !== circle) { const tooltip = svg.querySelector(".ticket-chart-tooltip"); if (tooltip) tooltip.remove(); } });
      circle.addEventListener("focus", showTooltip);
      circle.addEventListener("blur", () => { const tooltip = svg.querySelector(".ticket-chart-tooltip"); if (tooltip) tooltip.remove(); });
      circle.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openDetail(); } });
    });
    elements.milestoneTrack.append(svg);
  }

  function updateMilestoneZoom(nextScale, anchorRatio, anchorPosition) {
    const total = state.milestoneTickets.length;
    if (!total) return;
    const maxScale = Math.max(2, total);
    const oldScale = state.milestoneZoom.scale || 1;
    const scale = Math.max(1, Math.min(maxScale, nextScale));
    const position = Number.isFinite(anchorPosition) ? Math.max(0, Math.min(1, anchorPosition)) : 0.5;
    const anchor = Number.isFinite(anchorRatio) ? anchorRatio : state.milestoneZoom.center;
    state.milestoneZoom.scale = scale;
    state.milestoneZoom.center = scale === 1 ? 0.5 : anchor + (0.5 - position) / scale;
    if (oldScale !== scale || state.milestoneZoom.center !== anchor) requestAnimationFrame(renderMilestones);
  }

  function installMilestoneInteractions() {
    const viewport = elements.milestoneViewport;
    if (!viewport || viewport.dataset.zoomBound) return;
    viewport.dataset.zoomBound = "true";
    let drag = null;
    let pinch = null;
    const ratioAt = (clientX) => {
      const rect = viewport.getBoundingClientRect();
      return Math.max(0, Math.min(1, (clientX - rect.left) / Math.max(1, rect.width)));
    };
    const startPinch = (touches) => {
      const first = touches[0]; const second = touches[1];
      pinch = {
        ids: [first.identifier, second.identifier],
        distance: Math.hypot(second.clientX - first.clientX, second.clientY - first.clientY),
        scale: state.milestoneZoom.scale,
        center: state.milestoneZoom.center,
        position: ratioAt((first.clientX + second.clientX) / 2)
      };
    };
    viewport.addEventListener("wheel", (event) => {
      if (!state.milestoneTickets.length) return;
      event.preventDefault();
      const position = ratioAt(event.clientX);
      const scale = state.milestoneZoom.scale || 1;
      const nextScale = scale * Math.pow(1.12, -event.deltaY / 100);
      const windowWidth = 1 / scale;
      const anchor = Math.max(0, Math.min(1, state.milestoneZoom.center - windowWidth / 2 + position * windowWidth));
      updateMilestoneZoom(nextScale, anchor, position);
    }, {passive: false});
    viewport.addEventListener("pointerdown", (event) => {
      if (event.pointerType === "touch" || event.button !== 0 || event.target.closest("circle")) return;
      drag = {id: event.pointerId, x: event.clientX, center: state.milestoneZoom.center, scale: state.milestoneZoom.scale};
      viewport.setPointerCapture?.(event.pointerId);
    });
    viewport.addEventListener("pointermove", (event) => {
      if (!drag || drag.id !== event.pointerId || drag.scale <= 1) return;
      const rect = viewport.getBoundingClientRect();
      const delta = (event.clientX - drag.x) / Math.max(1, rect.width) / drag.scale;
      state.milestoneZoom.center = Math.max(0.5 / drag.scale, Math.min(1 - 0.5 / drag.scale, drag.center - delta));
      requestAnimationFrame(renderMilestones);
    });
    const endDrag = (event) => { if (drag && (!event || drag.id === event.pointerId)) drag = null; };
    viewport.addEventListener("pointerup", endDrag); viewport.addEventListener("pointercancel", endDrag);
    viewport.addEventListener("touchstart", (event) => {
      if (event.touches.length < 2) return;
      event.preventDefault();
      if (!pinch) startPinch(event.touches);
    }, {passive: false});
    viewport.addEventListener("touchmove", (event) => {
      if (event.touches.length < 2) return;
      event.preventDefault();
      if (!pinch) { startPinch(event.touches); return; }
      const first = Array.from(event.touches).find((touch) => touch.identifier === pinch.ids[0]);
      const second = Array.from(event.touches).find((touch) => touch.identifier === pinch.ids[1]);
      if (!first || !second) { startPinch(event.touches); return; }
      const distance = Math.hypot(second.clientX - first.clientX, second.clientY - first.clientY);
      const midpoint = ratioAt((first.clientX + second.clientX) / 2);
      const scale = pinch.scale * Math.pow(distance / Math.max(1, pinch.distance), 1.7);
      const windowWidth = 1 / pinch.scale;
      const anchor = Math.max(0, Math.min(1, pinch.center - windowWidth / 2 + pinch.position * windowWidth));
      updateMilestoneZoom(scale, anchor, midpoint);
    }, {passive: false});
    const endPinch = (event) => {
      if (event.touches.length < 2) { pinch = null; return; }
      if (!pinch || !Array.from(event.touches).some((touch) => touch.identifier === pinch.ids[0]) || !Array.from(event.touches).some((touch) => touch.identifier === pinch.ids[1])) startPinch(event.touches);
    };
    viewport.addEventListener("touchend", endPinch); viewport.addEventListener("touchcancel", endPinch);
  }

  function setMilestoneExpanded(expanded) {
    state.milestoneExpanded = expanded;
    if (elements.milestones) elements.milestones.classList.toggle("is-expanded", expanded);
    if (elements.milestoneZoom) {
      elements.milestoneZoom.setAttribute("aria-expanded", String(expanded));
      elements.milestoneZoom.textContent = expanded ? "缩小" : "放大";
    }
    if (state.milestoneVisible) requestAnimationFrame(renderMilestones);
  }

  function setMilestoneVisible(visible) {
    state.milestoneVisible = visible;
    if (elements.milestoneViewport) elements.milestoneViewport.hidden = !visible;
    if (elements.milestoneHint) elements.milestoneHint.hidden = !visible;
    if (elements.milestoneZoom) elements.milestoneZoom.hidden = !visible;
    if (elements.milestoneToggle) {
      elements.milestoneToggle.setAttribute("aria-expanded", String(visible));
      elements.milestoneToggle.textContent = visible ? "隐藏" : "展开";
    }
    if (!visible) setMilestoneExpanded(false);
  }

  function renderList() {
    elements.history.replaceChildren();
    elements.previous.disabled = state.page <= 1;
    elements.next.disabled = state.totalPages === 0 || state.page >= state.totalPages;
    elements.pageStatus.textContent = state.total ? `第 ${state.page} / ${state.totalPages} 页，共 ${state.total} 项` : "共 0 项";
    if (!state.tickets.length) { elements.history.append(node("p", "ticket-empty", state.q ? "没有匹配的公开需求。" : "暂无公开需求。")); return; }
    state.tickets.forEach((ticket) => {
      const button = node("button", `ticket-list-item${state.selected && state.selected.id === ticket.id ? " is-active" : ""}`);
      button.type = "button";
      const top = node("span", "ticket-list-item-top");
      top.append(node("strong", "", ticket.title), node("span", `ticket-status ticket-status-${ticket.status || "open"}`, statusNames[ticket.status] || ticket.status));
      const delivery = ticket.expected_delivery_date ? `预计 ${ticket.expected_delivery_date}` : "";
      button.append(top, node("small", "", `${ticket.ownedByViewer ? "我的需求 · " : ""}${visibilityNames[ticket.visibility] || "公开"}${delivery ? ` · ${delivery}` : ""} · ${formatTime(ticket.updated_at || ticket.created_at)}`));
      button.addEventListener("click", () => loadDetail(ticket.id));
      elements.history.append(button);
    });
  }

  function renderControls(ticket) {
    elements.controls.replaceChildren();
    elements.titleStatus?.replaceChildren();
    elements.titleControls?.replaceChildren();
    if (isAdmin() || ticket.ownedByViewer) {
      const visibility = node("select", "ticket-status-select");
      visibility.id = "ticketVisibilityEdit"; visibility.setAttribute("aria-label", "需求可见范围");
      Object.entries(visibilityNames).forEach(([value, label]) => { const option = node("option", "", label); option.value = value; option.selected = ticket.visibility === value; visibility.append(option); });
      const saveVisibility = node("button", "", "更新可见范围"); saveVisibility.type = "button";
      saveVisibility.addEventListener("click", async () => {
        saveVisibility.disabled = true;
        try { state.selected = await request(`/api/tickets/${ticket.id}`, writeOptions("PATCH", {visibility: visibility.value})); setMessage("需求可见范围已更新"); renderDetail(state.selected); await loadTickets(ticket.id); }
        catch (error) { setMessage(error.message, true); } finally { saveVisibility.disabled = false; }
      });
      elements.controls.append(visibility, saveVisibility);
    }
    if (isAdmin()) {
      const select = node("select", "ticket-status-select");
      const quickStatuses = {pending: "待审核", open: "待解决", in_progress: "解决中", resolved: "已解决"};
      Object.entries(quickStatuses).forEach(([value, label]) => { const option = node("option", "", label); option.value = value; option.selected = (ticket.status === "closed" ? "resolved" : ticket.status) === value; select.append(option); });
      select.setAttribute("aria-label", "快速改变需求状态");
      const saveStatus = node("button", "primary", "保存状态");
      saveStatus.type = "button"; saveStatus.setAttribute("aria-label", "保存需求状态");
      saveStatus.addEventListener("click", async () => {
        const previous = ticket.status === "closed" ? "resolved" : ticket.status;
        select.disabled = saveStatus.disabled = true;
        try {
          const updated = await updateStatus(select.value);
          if (!updated) select.value = previous;
        } catch (_) { select.value = previous; }
        finally { select.disabled = saveStatus.disabled = false; }
      });
      elements.titleStatus?.replaceChildren(select, saveStatus);
    } else if (ticket.ownedByViewer) {
      const next = ticket.status === "closed" ? "open" : "closed";
      const button = node("button", "", next === "open" ? "重新打开" : "关闭需求"); button.type = "button";
      button.addEventListener("click", () => updateStatus(next)); elements.controls.append(button);
    }
  }

  function syncRatingStars() {
    const form = elements.ratingForm;
    if (!form) return;
    const selected = Number(form.querySelector("input:checked")?.value || 0);
    const hovered = Number(form.dataset.hovered || 0);
    form.querySelectorAll(".ticket-rating-stars label").forEach((label) => {
      const value = Number(label.querySelector("input")?.value || 0);
      label.classList.toggle("is-active", value <= (hovered || selected));
    });
  }

  function attachmentLink(attachment) {
    const link = document.createElement("a");
    link.href = attachment.url; link.target = "_blank"; link.rel = "noopener";
    if (String(attachment.mime || "").startsWith("image/")) {
      const image = document.createElement("img");
      image.src = attachment.url; image.alt = attachment.name || "需求附件";
      image.loading = "lazy"; image.decoding = "async";
      image.addEventListener("error", () => image.replaceWith(node("span", "ticket-attachment-file ticket-attachment-load-error", "图片加载失败")), {once: true});
      link.append(image);
    } else link.append(node("span", "ticket-attachment-file", attachment.mime || "附件"));
    link.append(node("span", "", attachment.name || "附件"));
    return link;
  }

  function renderDetail(ticket) {
    elements.detailEmpty.hidden = true; elements.detailContent.hidden = false;
    const platforms = Array.isArray(ticket.platforms) && ticket.platforms.length ? ticket.platforms : (ticket.platform ? [ticket.platform] : []);
    const platformNames = {h5_mobile: "H5手机", pc_browser: "PC浏览器", android: "安卓版", windows: "Windows版", ios: "苹果手机版", mac: "苹果电脑版"};
    const platformText = platforms.map((platform) => platformNames[platform] || platform).join("、");
    elements.detailMeta.replaceChildren();
    const metaItems = [
      ["ticket-meta-status", "状态", statusNames[ticket.status] || ticket.status],
      ["ticket-meta-visibility", "可见范围", visibilityNames[ticket.visibility] || "公开"],
      ["ticket-meta-platform", "平台", platformText],
      ["ticket-meta-delivery", "交付", ticket.expected_delivery_date ? `预计交付 ${ticket.expected_delivery_date}` : ""],
      ["ticket-meta-updated", "更新", formatTime(ticket.updated_at || ticket.created_at)]
    ];
    metaItems.filter(([, , value]) => value).forEach(([className, label, value]) => {
      const item = node("span", `ticket-detail-meta-item ${className}`);
      item.append(node("span", "ticket-detail-meta-label", label), node("span", "ticket-detail-meta-value", value));
      elements.detailMeta.append(item);
    });
    elements.detailTitle.textContent = ticket.title; renderMarkdown(elements.detailDescription, ticket.description);
    const ratings = Array.isArray(ticket.ratings) ? ticket.ratings.filter((rating) => rating && Number.isFinite(Number(rating.score))) : [];
    const fallbackRating = ticket.rating && Number.isFinite(Number(ticket.rating.score)) ? Number(ticket.rating.score) : null;
    const ratingCount = Number.isFinite(Number(ticket.ratingCount)) ? Number(ticket.ratingCount) : (ratings.length || (fallbackRating === null ? 0 : 1));
    const ratingTotal = ratings.reduce((total, rating) => total + Number(rating.score), 0) || (fallbackRating === null ? 0 : fallbackRating);
    if (elements.ratingSummary) elements.ratingSummary.textContent = ratingCount ? `已有 ${ratingCount} 人评价 · 平均 ${(ratingTotal / ratingCount).toFixed(1)} 星` : "";
    const privateTicket = ticket.visibility === "private";
    const alreadyRated = Boolean(ticket.ratedByViewer);
    const viewerCanRate = ticket.status === "closed" && canWrite() && (!privateTicket || ticket.ownedByViewer);
    const eligible = viewerCanRate && !alreadyRated;
    elements.ratingForm.hidden = !viewerCanRate;
    elements.ratingForm.dataset.hovered = "";
    elements.ratingForm.querySelectorAll("input").forEach((input) => { input.disabled = !eligible; input.checked = false; });
    syncRatingStars();
    elements.ratingComment.disabled = !eligible;
    elements.ratingComment.value = "";
    elements.ratingForm.querySelector("button").hidden = !eligible;
    elements.ratingHint.textContent = alreadyRated ? "你已评价该需求，每位用户只能评价一次。" : (eligible ? "关闭后的公开需求可由已验证用户评价，私有需求仅提交者可评价。" : "");
    elements.detailAttachments.replaceChildren();
    (ticket.attachments || []).forEach((attachment) => elements.detailAttachments.append(attachmentLink(attachment)));
    elements.comments.replaceChildren();
    if (!(ticket.comments || []).length) elements.comments.append(node("p", "ticket-empty", "暂无回复。"));
    (ticket.comments || []).forEach((comment) => { const article = node("article", "ticket-comment"); const head = node("div", "ticket-comment-meta", `${comment.author || "需求用户"} · ${formatTime(comment.created_at)}`); const body = node("div", "ticket-comment-body"); renderMarkdown(body, comment.body); article.append(head, body); const attachments = node("div", "ticket-detail-attachments"); (comment.attachments || []).forEach((attachment) => attachments.append(attachmentLink(attachment))); if (attachments.childElementCount) article.append(attachments); elements.comments.append(article); });
    renderControls(ticket);
    elements.replyForm.hidden = !(canWrite() && (isAdmin() || ticket.ownedByViewer));
    if (!state.auth || !state.auth.authenticated) elements.replyHint.textContent = "登录并完成邮箱验证后，可补充自己的需求。";
    else if (!canWrite()) elements.replyHint.textContent = "请先前往账户页面完成邮箱验证。";
    else if (!isAdmin() && !ticket.ownedByViewer) elements.replyHint.textContent = "只有需求作者和管理员可以补充此需求。";
    else elements.replyHint.textContent = "补充内容也会公开展示。";
  }

  async function loadTickets(preferredId) {
    try {
      const urlTicketId = new URLSearchParams(location.search).get("ticket");
      const requestedId = preferredId || urlTicketId || (state.selected && state.selected.id);
      const query = new URLSearchParams({page: String(state.page), page_size: String(state.pageSize)}); if (state.q) query.set("q", state.q);
      const payload = await request(`/api/tickets?${query}`); state.tickets = Array.isArray(payload.tickets) ? payload.tickets : []; state.page = payload.page || 1; state.total = payload.total || 0; state.totalPages = payload.total_pages || 0;
      if (state.totalPages && state.page > state.totalPages) { state.page = state.totalPages; await loadTickets(requestedId); return; }
      // 列表和详情优先展示，里程碑属于辅助信息，避免全量分页请求阻塞首屏。
      const latestTicket = state.tickets.reduce((latest, ticket) => {
        if (!latest) return ticket;
        const latestTime = Date.parse(latest.updated_at || latest.created_at || "") || 0;
        const ticketTime = Date.parse(ticket.updated_at || ticket.created_at || "") || 0;
        return ticketTime > latestTime ? ticket : latest;
      }, null);
      const selectedId = requestedId || (latestTicket && latestTicket.id);
      renderList();
      updateUrl(selectedId);
      if (selectedId) await loadDetail(selectedId);
      loadMilestoneTickets().then(() => {
        if (state.milestoneVisible) renderMilestones();
      }).catch(() => {
        state.milestoneTickets = [];
        if (elements.milestoneSummary) elements.milestoneSummary.textContent = "里程碑暂时无法加载，可稍后刷新。";
      });
    } catch (error) { elements.history.textContent = error.message; }
  }
  async function loadMilestoneTickets() {
    const pageSize = 50;
    const first = await request(`/api/tickets?page=1&page_size=${pageSize}&scope=public`);
    const all = Array.isArray(first.tickets) ? [...first.tickets] : [];
    const totalPages = Number(first.total_pages) || 1;
    for (let page = 2; page <= totalPages; page += 1) {
      const payload = await request(`/api/tickets?page=${page}&page_size=${pageSize}&scope=public`);
      if (Array.isArray(payload.tickets)) all.push(...payload.tickets);
    }
    state.milestoneTickets = all;
  }
  async function loadDetail(id) {
    const requestSeq = ++state.detailRequestSeq;
    try {
      const ticket = await request(`/api/tickets/${id}`);
      if (requestSeq !== state.detailRequestSeq) return;
      state.selected = ticket; updateUrl(id); renderList(); renderDetail(state.selected);
    } catch (error) {
      if (requestSeq === state.detailRequestSeq) setMessage(error.message, true);
    }
  }
  async function updateStatus(status) {
    try { state.selected = await request(`/api/tickets/${state.selected.id}`, writeOptions("PATCH", {status})); setMessage(status === "closed" ? "需求已关闭，状态已更新。" : "需求状态已更新"); renderDetail(state.selected); await loadTickets(state.selected.id); }
    catch (error) { setMessage(error.message, true); return false; }
    return true;
  }

  if (form) form.addEventListener("submit", async (event) => {
    event.preventDefault(); if (createBusy) return;
    const title = byId("ticketTitle").value.trim(), platforms = elements.platforms(), description = byId("ticketMessage").value.trim();
    if (!title || !platforms.length || !description) { setMessage("请填写标题、至少选择一个平台和问题描述", true); return; }
    if (state.attachmentFiles.some(item => item.invalid)) { setMessage(previewError, true); return; }
    const files = state.attachmentFiles.map(item => item.file);
    try { uploader().validate(files); } catch (error) { setMessage(error.message, true); return; }
    if (pendingTicket && pendingTicket.context !== uploadContext()) { setMessage("登录身份已变化，请刷新确认原需求后重试", true); return; }
    createBusy = true; const unlock = lockForm(form);
    try {
      if (!pendingTicket) {
        pendingTicket = {id: null, context: uploadContext()};
        const visibility = byId("ticketVisibility")?.value || "private", expected_delivery_date = byId("ticketExpectedDeliveryDate")?.value || "";
        const ticket = await request("/api/tickets", writeOptions("POST", {title, platform: platforms[0], platforms, description, visibility, expected_delivery_date}));
        pendingTicket.id = ticket.id;
      }
      if (!pendingTicket.id) throw Error("提交结果未确认，请刷新列表确认已建需求，避免重复创建");
      await uploader().upload(uploadSend, pendingTicket.id, files, null, {container: form});
      const id = pendingTicket.id; pendingTicket = null;
      state.attachmentFiles.forEach(item => URL.revokeObjectURL(item.url)); state.attachmentFiles = [];
      state.q = ""; state.page = 1; elements.search.value = ""; form.reset(); renderAttachmentPreviews();
      setMessage("需求已提交"); await loadTickets(id);
    } catch (error) { setMessage(error.message + (pendingTicket?.id ? "；需求已保存，再次提交仅重试附件（正文修改请在详情编辑）" : ""), true); }
    finally { unlock(); createBusy = false; }
  });
  elements.replyForm.addEventListener("submit", async (event) => {
    event.preventDefault(); if (replyBusy) return;
    const selectedId = state.selected?.id, body = elements.reply.value.trim(); if (!selectedId || !body) return;
    if (pendingReply && pendingReply.context !== uploadContext()) { setMessage("登录身份已变化，请刷新确认原回复后重试", true); return; }
    if (pendingReply && pendingReply.ticketId !== selectedId) { setMessage("上一需求的回复附件尚未完成，请先返回原需求重试", true); return; }
    if (state.replyAttachmentFiles.some(item => item.invalid)) { setMessage(previewError, true); return; }
    const files = state.replyAttachmentFiles.map(item => item.file);
    try { uploader().validate(files); } catch (error) { setMessage(error.message, true); return; }
    replyBusy = true; const unlock = lockForm(elements.replyForm);
    try {
      if (!pendingReply) {
        pendingReply = {ticketId: selectedId, commentId: null, context: uploadContext()};
        const result = await request(`/api/tickets/${selectedId}/comments`, writeOptions("POST", {body}));
        pendingReply.commentId = result.created_comment_id || result.comment_id || (result.body === body && !result.title && !Array.isArray(result.comments) ? result.id : null);
      }
      if (!pendingReply.commentId && files.length) throw Error("回复已保存但编号未确认，请刷新详情确认，避免重复回复");
      await uploader().upload(uploadSend, selectedId, files, pendingReply.commentId, {container: elements.replyForm});
      pendingReply = null;
      if (state.selected?.id === selectedId) { elements.replyForm.reset(); clearReplyAttachments(); setMessage("补充已提交"); await loadDetail(selectedId); }
    } catch (error) { setMessage(error.message + (pendingReply ? "；再次提交仅重试附件，正文修改请编辑原回复" : ""), true); }
    finally { unlock(); replyBusy = false; }
  });
  elements.ratingForm.addEventListener("submit", async (event) => {
    event.preventDefault(); const score = Number(elements.ratingForm.querySelector("input:checked")?.value); if (!score) return;
    const button = elements.ratingForm.querySelector("button"); button.disabled = true;
    try { await request(`/api/tickets/${state.selected.id}/rating`, writeOptions("POST", {score, comment: elements.ratingComment.value.trim()})); setMessage("需求评价已提交"); await loadDetail(state.selected.id); }
    catch (error) { setMessage(error.message, true); button.disabled = false; }
  });
  elements.refresh.addEventListener("click", () => loadTickets());
  elements.searchForm.addEventListener("submit", (event) => { event.preventDefault(); state.q = elements.search.value.trim().slice(0, 100); state.page = 1; loadTickets(); });
  elements.searchClear.addEventListener("click", () => { elements.search.value = ""; state.q = ""; state.page = 1; loadTickets(); elements.search.focus(); });
  elements.previous.addEventListener("click", () => { if (state.page > 1) { state.page -= 1; loadTickets(); } });
  elements.next.addEventListener("click", () => { if (state.page < state.totalPages) { state.page += 1; loadTickets(); } });
  if (elements.attachmentInput) elements.attachmentInput.addEventListener("change", () => selectAttachments(elements.attachmentInput.files));
  elements.replyAttachmentInput.addEventListener("change", () => selectReplyAttachments(elements.replyAttachmentInput.files));
  if (elements.milestoneToggle) elements.milestoneToggle.addEventListener("click", () => setMilestoneVisible(!state.milestoneVisible));
  if (elements.milestoneZoom) elements.milestoneZoom.addEventListener("click", () => setMilestoneExpanded(!state.milestoneExpanded));
  if (elements.milestoneViewport) elements.milestoneViewport.addEventListener("dblclick", () => {
    if (window.matchMedia("(max-width: 760px)").matches) elements.milestoneViewport.classList.toggle("is-landscape");
  });
  const rerenderMilestonesOnResize = () => {
    if (state.milestoneVisible) requestAnimationFrame(renderMilestones);
  };
  window.addEventListener("resize", rerenderMilestonesOnResize, {passive: true});
  window.visualViewport?.addEventListener("resize", rerenderMilestonesOnResize, {passive: true});
  installMilestoneInteractions();
  elements.search.value = state.q;
  Promise.all([request("/api/auth/status"), loadTickets()]).then(([auth]) => { state.auth = auth; renderAccess(); if (state.selected) renderDetail(state.selected); }).catch((error) => { setMessage(error.message, true); });
})();
