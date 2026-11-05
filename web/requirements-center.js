/*
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 上海商江网络科技有限公司版权所有
软工云（swtool.com）开发团队；uqtool@163.com
*/
// Milestone chart ported from SWTool requirements-center (tickets.js).
// Host adapter keeps SWTool authentication and owner permissions.
(()=>{
  const node=(tag,cls,text)=>{const el=document.createElement(tag);el.className=cls;if(text!==undefined)el.textContent=text;return el};
  const state={milestoneTickets:[],milestoneView:"curve",milestoneVisible:true,milestoneExpanded:false,milestoneZoom:{scale:1,center:0.5}};
  let elements={},session=null,generation=0,flight=null,scheduled=null;
  const loadDetail=id=>window.SWToolOpenRequirement?.(id);
  const statusNames = {pending:"待审核",open:"待解决",in_progress:"处理中",resolved:"已解决"};
  const normalizeStatus = status => status === "closed" ? "resolved" : status;
  const timestamp = value => {
    if (value === null || value === undefined || value === "") return null;
    const parsed = typeof value === "number" ? value * 1000 : Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const formatTime = value => new Date(value).toLocaleString("zh-CN");
  function milestoneSchedule(ticket, today = Date.now()) {
    const completed = normalizeStatus(ticket.status) === "resolved";
    const history = Array.isArray(ticket.status_history) ? ticket.status_history : [];
    const resolvedEvent = history.slice().reverse().find((event) => ["resolved", "closed"].includes(String(event?.status || "").toLowerCase()));
    const completedAt = completed
      ? timestamp(ticket.resolved_at) || timestamp(resolvedEvent?.at) || timestamp(ticket.updated_at) || timestamp(ticket.created_at) || today
      : null;
    const start = timestamp(ticket.created_at) || (completedAt !== null ? completedAt - 7 * 86400000 : today);
    let target = timestamp(ticket.expected_resolution_at ?? ticket.expected_delivery_date);
    const inferredTarget = target === null;
    if (inferredTarget) target = start + 7 * 86400000;
    return {start,target,completedAt,completed,inferredTarget,overdue:!completed && target < today};
  }
  function openGanttTicket(id) {
    if (state.milestoneExpanded) setMilestoneExpanded(false);
    loadDetail(id);
    elements.milestoneTrack.querySelectorAll('[data-ticket-id]').forEach(row=>row.setAttribute('aria-pressed',String(row.dataset.ticketId===String(id))));
    document.getElementById('issue-detail')?.scrollIntoView({block:'start',behavior:'smooth'});
  }
  function setMilestoneView(view, focus = false) {
    state.milestoneView = view === 'gantt' ? 'gantt' : 'curve';
    elements.milestoneTabs.forEach(tab=>{
      const active = tab.dataset.milestoneView === state.milestoneView;
      tab.classList.toggle('is-active',active);tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;
      if(active && focus)tab.focus();
    });
    elements.milestoneViewport.classList.toggle('is-gantt',state.milestoneView==='gantt');
    elements.milestoneViewport.setAttribute('aria-labelledby',state.milestoneView==='gantt'?'ticketGanttTab':'ticketCurveTab');
    elements.milestoneZoom.hidden=!state.milestoneVisible || state.milestoneView==='gantt';
    elements.milestoneHint.textContent=state.milestoneView==='gantt'?'每行一条需求，最新提交置顶；点击查看详情。圆点为开始，菱形为目标，勾号为实际解决；图内滑动查看。':'按时间日期累计工单数；点击节点查看需求详情，滚轮或双指缩放，拖动平移。';
    if(state.milestoneVisible)renderMilestones();
  }
  function milestoneTone(ticket) {
    const identity = String(ticket.id ?? ticket.title ?? "");
    let hash = 0;
    for (const character of identity) hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
    return hash % 7;
  }

  function renderGantt(tickets) {
    const container = node("div", "ticket-gantt");
    const schedules = tickets.map((ticket, index) => ({ticket, schedule: milestoneSchedule(ticket), index}));
    const dates = schedules.flatMap(({schedule}) => [schedule.start, schedule.target, schedule.completedAt]).filter(Number.isFinite);
    const minDate = dates.length ? Math.min(...dates) : Date.now() - 86400000;
    const maxDate = Math.max(Date.now(), dates.length ? Math.max(...dates) : Date.now() + 86400000);
    const span = Math.max(86400000, maxDate - minDate);
    const axis = node("div", "ticket-gantt-axis");
    axis.setAttribute("aria-label", "最新需求置顶，时间轴从左到右");
    for (let index = 0; index <= 4; index += 1) {
      const value = minDate + span * index / 4;
      axis.append(node("span", "ticket-gantt-axis-label", new Date(value).toLocaleDateString("zh-CN")));
    }
    container.append(axis);
    const todayRatio = Math.max(0, Math.min(1, (Date.now() - minDate) / span));
    const todayLine = node("span", "ticket-gantt-today-line");
    todayLine.style.left = `calc(var(--gantt-label-width) + (100% - var(--gantt-label-width)) * ${todayRatio})`;
    todayLine.setAttribute("aria-label", "今日");
    container.append(todayLine);
    const grid = node("span", "ticket-gantt-grid");
    grid.setAttribute("aria-hidden", "true");
    container.append(grid);
    schedules.sort((left, right) => {
      const leftLatest = timestamp(left.ticket.updated_at) ?? left.schedule.start ?? 0;
      const rightLatest = timestamp(right.ticket.updated_at) ?? right.schedule.start ?? 0;
      return rightLatest - leftLatest
        || (right.schedule.target ?? 0) - (left.schedule.target ?? 0)
        || (right.schedule.completedAt ?? 0) - (left.schedule.completedAt ?? 0)
        || String(left.ticket.id || "").localeCompare(String(right.ticket.id || ""))
        || left.index - right.index;
    });
    const statusColors = document.body.dataset.theme === "light"
      ? {pending:"#a16207",open:"#1d4ed8",in_progress:"#7e22ce",resolved:"#15803d"}
      : {pending:"#fbbf24",open:"#60a5fa",in_progress:"#c084fc",resolved:"#4ade80"};
    schedules.forEach(({ticket, schedule}) => {
      const statusColor = statusColors[normalizeStatus(ticket.status)] || statusColors.open;
      const row = node("button", "ticket-gantt-row"); row.type = "button"; row.dataset.ticketId = ticket.id; row.dataset.status=normalizeStatus(ticket.status);
      row.style.setProperty("--gantt-identity", statusColor);
      const label = node("span", "ticket-gantt-label");
      const titleNode = node("span", "ticket-gantt-title", ticket.title || "未命名需求");
      titleNode.title = ticket.title || "未命名需求";
      label.append(titleNode);
      const baseStatus = statusNames[normalizeStatus(ticket.status)] || "待解决";
      const completionStatus = "已解决";
      const statusText = `${schedule.completed ? completionStatus : baseStatus}${schedule.overdue ? " · 已逾期" : ""}`;
      const status = node("span", "ticket-gantt-status", statusText);status.dataset.status=normalizeStatus(ticket.status);status.style.color=statusColor;
      label.append(status);
      const timeline = node("span", "ticket-gantt-timeline");
      if (schedule.start !== null && schedule.target !== null) { const start = (Math.min(schedule.start, schedule.target) - minDate) / span * 100; const end = (Math.max(schedule.start, schedule.target) - minDate) / span * 100; const bar = node("span", `ticket-gantt-bar${schedule.completed ? " is-complete" : schedule.overdue ? " is-overdue" : ""}`); bar.style.left = `${Math.max(0, Math.min(100, start))}%`; bar.style.width = `${Math.max(1, Math.min(100, end) - Math.max(0, start))}%`; bar.title = schedule.completed ? (`已解决：${formatTime(schedule.completedAt)}`) : schedule.overdue ? "已逾期" : "进行中"; timeline.append(bar); }
      [[schedule.start, "ticket-gantt-start", "开始"], [schedule.target, "ticket-gantt-target", schedule.inferredTarget ? "推算目标" : "目标"], [schedule.completedAt, "ticket-gantt-completion", "实际完成"]].forEach(([value, className, labelText]) => { if (value === null) return; const marker = node("span", `ticket-gantt-marker ${className}`); marker.style.left = `${Math.max(0, Math.min(100, (value - minDate) / span * 100))}%`; marker.title = `${labelText} ${new Date(value).toLocaleString("zh-CN")}`; timeline.append(marker); });
      label.setAttribute("title", `${ticket.title || "未命名需求"} · ${statusText}`);
      row.setAttribute("aria-label", `${ticket.title || "未命名需求"}，${statusText}`);
      row.append(label, timeline); row.addEventListener("click", () => openGanttTicket(ticket.id)); container.append(row);
    });
    const legend = node("div", "ticket-gantt-legend");
    [["开始", "ticket-gantt-start"], ["目标", "ticket-gantt-target"], ["实际完成", "ticket-gantt-completion"], ["今日", "ticket-gantt-today-key"], ["已解决", "ticket-gantt-complete-key"], ["逾期", "ticket-gantt-overdue-key"]].forEach(([text, kind]) => { const item = node("span", "ticket-gantt-legend-item"); item.append(node("i", kind), node("span", "", text)); legend.append(item); }); container.append(legend);
    return container;
  }

  function renderMilestones() {
    if (!elements.milestoneTrack || !elements.milestoneSummary) return;
    elements.milestoneTrack.replaceChildren();
    if (!state.milestoneTickets.length) {
      elements.milestoneSummary.textContent = "暂无可见需求里程碑。";
      elements.milestoneTrack.append(node("p", "ticket-empty", "暂无可见需求。"));
      return;
    }
    const tickets = [...state.milestoneTickets].sort((left, right) => {
      const leftTime = Date.parse(left.created_at || "") || 0;
      const rightTime = Date.parse(right.created_at || "") || 0;
      return leftTime - rightTime || String(left.id || "").localeCompare(String(right.id || ""));
    });
    if (state.milestoneView === 'gantt') {
      const completed = tickets.filter(t=>milestoneSchedule(t).completed).length;
      const overdue = tickets.filter(t=>milestoneSchedule(t).overdue).length;
      elements.milestoneSummary.textContent = `可见需求 ${tickets.length} 项 · 已解决 ${completed} · 逾期 ${overdue} · 横轴：日期，纵轴：需求；按提交与预计解决时间展示`;
      elements.milestoneTrack.append(renderGantt(tickets));return;
    }
    const closedCount = tickets.filter((ticket) => ["closed", "resolved"].includes(String(ticket.status || "").toLowerCase())).length;
    elements.milestoneSummary.textContent = `全部 ${tickets.length} 个可见需求 · ${closedCount} 个已完成 · 按日期累计，从远到近排列`;
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
    svg.setAttribute("aria-label", `可见需求历史累计曲线，共 ${tickets.length} 个需求`);
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
      addSvg("line", {x1: left - 5, y1: y, x2: left, y2: y, class: "ticket-chart-tick"});
      addSvg("text", {x: left - 10, y: y + 4, class: "ticket-chart-axis-label", "text-anchor": "end"}, String(value));
    }
    const completedY = bottom - (closedCount / maxValue) * plotHeight;
    addSvg("line", {x1: left, y1: completedY, x2: right, y2: completedY, class: "ticket-chart-completed-line"});
    addSvg("text", {x: right - 4, y: completedY - 6, class: "ticket-chart-completed-label", "text-anchor": "end"}, `已完成 ${closedCount}`);
    addSvg("line", {x1: left, y1: top, x2: left, y2: bottom, class: "ticket-chart-axis"});
    addSvg("line", {x1: left, y1: bottom, x2: right, y2: bottom, class: "ticket-chart-axis"});
    addSvg("text", {x: 14, y: (top + bottom) / 2, class: "ticket-chart-axis-title", "text-anchor": "middle", transform: `rotate(-90 14 ${(top + bottom) / 2})`}, "累计工单数");
    addSvg("text", {x: (left + right) / 2, y: height - 2, class: "ticket-chart-axis-title", "text-anchor": "middle"}, "时间日期");
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
      const dx = point.x - previous.x, dy = point.y - previous.y;
      // Alternate shallow bends while keeping the cumulative line monotonic and passing through every node.
      const wave = (point.index % 2 ? 1 : -1) * Math.min(8, Math.abs(dy) * 0.2, Math.abs(dx) * 0.08);
      return `C ${previous.x + dx / 3} ${previous.y + dy / 3 + wave}, ${point.x - dx / 3} ${point.y - dy / 3 + wave}, ${point.x} ${point.y}`;
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
      light: ["#b91c1c", "#c2410c", "#a16207", "#15803d", "#0e7490", "#1d4ed8", "#7e22ce"],
      contrast: ["#ff8080", "#ffb366", "#ffe600", "#7dff5b", "#00e5ff", "#82b1ff", "#e88cff"],
      dark: ["#f87171", "#fb923c", "#facc15", "#4ade80", "#22d3ee", "#60a5fa", "#c084fc"]
    };
    const palette = palettes[document.body.dataset.theme] || palettes.dark;
    markerPoints.forEach((point, pointIndex) => {
      const status = point.ticket.status || "open";
      const layout = labelLayout(point, pointIndex);
      const color = palette[milestoneTone(point.ticket)];
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
      if (state.milestoneView === 'gantt' || !state.milestoneTickets.length) return;
      event.preventDefault();
      const position = ratioAt(event.clientX);
      const scale = state.milestoneZoom.scale || 1;
      const nextScale = scale * Math.pow(1.12, -event.deltaY / 100);
      const windowWidth = 1 / scale;
      const anchor = Math.max(0, Math.min(1, state.milestoneZoom.center - windowWidth / 2 + position * windowWidth));
      updateMilestoneZoom(nextScale, anchor, position);
    }, {passive: false});
    viewport.addEventListener("pointerdown", (event) => {
      if (state.milestoneView === "gantt" || event.pointerType === "touch" || event.button !== 0 || event.target.closest("circle")) return;
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
      if (state.milestoneView === "gantt" || event.touches.length < 2) return;
      event.preventDefault();
      if (!pinch) startPinch(event.touches);
    }, {passive: false});
    viewport.addEventListener("touchmove", (event) => {
      if (state.milestoneView === "gantt" || event.touches.length < 2) return;
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
    if (elements.milestoneZoom) elements.milestoneZoom.hidden = !visible || state.milestoneView === "gantt";
    if (elements.milestoneToggle) {
      elements.milestoneToggle.setAttribute("aria-expanded", String(visible));
      elements.milestoneToggle.textContent = visible ? "隐藏" : "展开";
    }
    if (!visible) setMilestoneExpanded(false);
  }


  function mount(){
    const panel=document.getElementById('issuesPanel');if(!panel)return false;
    if(!document.getElementById('ticketMilestones')){
      const section=document.createElement('section');section.id='ticketMilestones';section.className='panel ticket-milestones';
      section.innerHTML='<div class="section-title"><div><small>FEATURE MILESTONES</small><h2>功能里程碑</h2></div><div class="ticket-milestone-actions"><button id="ticketMilestonesToggle" type="button" class="quiet" aria-expanded="true">隐藏</button><button id="ticketMilestonesZoom" type="button" class="quiet" aria-expanded="false">放大</button></div></div><div class="ticket-milestone-tabs" role="tablist" aria-label="里程碑视图"><button id="ticketCurveTab" role="tab" aria-selected="true" aria-controls="ticketMilestonesViewport" data-milestone-view="curve" class="is-active" type="button">曲线图</button><button id="ticketGanttTab" role="tab" aria-selected="false" aria-controls="ticketMilestonesViewport" data-milestone-view="gantt" tabindex="-1" type="button">甘特图</button></div><p id="ticketMilestonesSummary" role="status" aria-live="polite">登录后加载需求里程碑。</p><div id="ticketMilestonesViewport" class="ticket-milestones-viewport" role="tabpanel" aria-labelledby="ticketCurveTab" tabindex="0" aria-label="需求历史累计曲线"><div id="ticketMilestonesTrack" class="ticket-milestones-track"></div></div><div class="ticket-milestone-footer"><p id="ticketMilestonesHint" class="muted">按时间日期累计工单数，时间从远到近排列；点击曲线节点查看需求详情，滚轮或双指缩放，拖动平移。</p><button id="ticketMilestoneNewIssue" class="primary" type="button">新增需求</button></div>';
      panel.querySelector('.issue-heading')?.after(section);
      elements={milestoneTabs:[...section.querySelectorAll('[data-milestone-view]')],milestones:section,milestoneSummary:document.getElementById('ticketMilestonesSummary'),milestoneViewport:document.getElementById('ticketMilestonesViewport'),milestoneTrack:document.getElementById('ticketMilestonesTrack'),milestoneHint:document.getElementById('ticketMilestonesHint'),milestoneToggle:document.getElementById('ticketMilestonesToggle'),milestoneZoom:document.getElementById('ticketMilestonesZoom')};
      elements.milestoneToggle.onclick=()=>{setMilestoneVisible(!state.milestoneVisible);if(state.milestoneVisible)renderMilestones()};
      elements.milestoneZoom.onclick=()=>setMilestoneExpanded(!state.milestoneExpanded);
      document.getElementById('ticketMilestoneNewIssue').onclick=()=>window.SWToolOpenNewIssue?.();
      elements.milestoneTabs.forEach(tab=>{
        tab.onclick=()=>setMilestoneView(tab.dataset.milestoneView);
        tab.onkeydown=event=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();setMilestoneView(event.key==='Home'?'curve':event.key==='End'?'gantt':state.milestoneView==='curve'?'gantt':'curve',true)}};
      });
      setMilestoneView(state.milestoneView);
      installMilestoneInteractions();
    }
    return true;
  }
  const contextKey=()=>token+'|'+(window.SWToolRequirementsContext?.()||'');
  function sync(){
    if(!mount())return;
    const newIssue=document.getElementById('ticketMilestoneNewIssue');
    if(newIssue){newIssue.hidden=typeof window.SWToolOpenNewIssue!=='function';newIssue.disabled=typeof window.SWToolOpenNewIssue!=='function';}
    if(session!==contextKey()){session=contextKey();++generation;flight=null;state.milestoneTickets=[];state.milestoneZoom={scale:1,center:0.5};elements.milestoneTrack.replaceChildren();elements.milestoneSummary.textContent=token?'正在加载累计曲线…':'登录后加载需求里程碑。'}
    elements.milestones.hidden=!requirementsActive;
    if(!requirementsActive){++generation;flight=null;state.milestoneTickets=[];elements.milestoneTrack.replaceChildren();}
  }
  async function load(){
    sync();if(!token||!requirementsActive||!elements.milestones||flight)return;
    const current=contextKey(),seq=generation;const request={};flight=request;
    const isCurrent=()=>current===contextKey()&&seq===generation&&flight===request&&requirementsActive;
    try{
      const tickets=[];let page=1,totalPages=1;
      do{const data=await api('/api/tickets?page='+page+'&page_size=100');if(!isCurrent())return;tickets.push(...(data.tickets||[]));totalPages=data.total_pages||0;page++}while(page<=totalPages);
      state.milestoneTickets=tickets.map(ticket=>({...ticket,created_at:typeof ticket.created_at==='number'?new Date(ticket.created_at*1000).toISOString():ticket.created_at}));
      renderMilestones();
    }catch(error){
      const workspaceChanged = error?.name === "WorkspaceChangedError" || error?.message === "workspace changed";
      if(isCurrent() && !workspaceChanged) elements.milestoneSummary.textContent='里程碑加载失败：'+error.message;
    }finally{if(flight===request)flight=null}
  }
  function schedule(){clearTimeout(scheduled);scheduled=setTimeout(()=>{sync();if(routeView()==='issues')load()},100)}
  window.SWToolRequirementsMilestones={sync,load,schedule};
  new MutationObserver(schedule).observe(document.getElementById('workspace'),{childList:true});
  window.addEventListener('swtool:requirements-theme',()=>{if(elements.milestoneTrack&&state.milestoneVisible)renderMilestones()});
  window.addEventListener('hashchange',schedule);window.addEventListener('resize',()=>{if(elements.milestoneTrack&&state.milestoneVisible)renderMilestones()});
  schedule();
})();
