/*
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 上海商江网络科技有限公司版权所有
软工云（swtool.com）开发团队；uqtool@163.com
*/
(() => {
  "use strict";
  const prefix = "/plugins/requirements-center/standalone/web";
  if (!location.pathname.startsWith(prefix + "/") && location.pathname !== "/requirements-center.html") return;
  const api = "/plugins/requirements-center/api";
  let authReady, initialPrincipal, initialToken, invalidated = false;
  const currentToken = () => typeof window.swtoolWebviewSessionToken === "function" ? window.swtoolWebviewSessionToken() : null;
  const session = () => {
    if (initialPrincipal !== undefined && currentToken() !== initialToken) invalidated = true;
    return invalidated ? "" : "host-cookie-session";
  };
  const ensureAuth = () => authReady ||= new Promise((resolve, reject) => {
    if (typeof window.swtoolWebviewSessionToken === "function" || window.SWToolWebViewAuth) return resolve();
    const script = document.createElement("script");
    script.src = "/js/webview-session.js?v=20260817-webview-auth-v4";
    script.onload = resolve; script.onerror = () => reject(Error("宿主 WebView 会话组件加载失败"));
    document.head.append(script);
  });
  const raw = async (path, options = {}) => {
    await ensureAuth();
    if (!session()) throw Error("宿主登录身份已变化，请重新加载页面");
    if (!path.startsWith("/") || path.startsWith("//")) throw Error("拒绝非宿主请求");
    const headers = new Headers(options.headers || {}); headers.delete("Authorization");
    return fetch(path, {...options, credentials: "same-origin", cache: "no-store", redirect: "error", headers});
  };
  const json = (value, status = 200) => new Response(JSON.stringify(value), {status, headers: {"Content-Type": "application/json"}});
  const payload = async (path, options) => {
    const response = await raw(path, options), data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = Error(data.error || data.detail || `请求失败（${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return data;
  };
  const auth = async () => {
    await ensureAuth();
    if (!session()) throw Error("宿主登录身份已变化，请重新加载页面");
    const token = currentToken();
    const status = await payload(`${api}/auth/status`);
    const user = status.user || {};
    const principal = status.authenticated ? String(user.user_id || user.id || "") : "";
    if (currentToken() !== token || (initialPrincipal !== undefined && principal !== initialPrincipal)) invalidated = true;
    if (invalidated) throw Error("宿主登录身份已变化，请重新加载页面");
    if (principal && initialPrincipal === undefined) {
      initialPrincipal = principal;
      initialToken = token;
    }
    return status;
  };
  const blocked = () => json({error: "此操作暂未桥接"}, 405);
  const relationSummary = (item) => item && typeof item === "object" && typeof item.id === "string" && item.id
    ? {id: item.id, title: typeof item.title === "string" ? item.title : "", status: typeof item.status === "string" ? item.status : "open"}
    : null;
  const project = (item) => ({
    id: item.id, title: item.title || "", description: item.description || "", status: item.status || "open", case_id: item.case_id,
    author_username: item.author_username || item.author || "需求用户", author_id: item.author_id || item.user_id || "",
    created_at: item.created_at, updated_at: item.updated_at || item.created_at,
    status_history: item.status_history, resolved_at: item.resolved_at,
    parent_ticket: relationSummary(item.parent_ticket),
    child_tickets: Array.isArray(item.child_tickets) ? item.child_tickets.slice(0, 200).map(relationSummary).filter(Boolean) : [],
    children_truncated: item.children_truncated === true || (Array.isArray(item.child_tickets) && item.child_tickets.length > 200),
    expected_resolution_at: item.expected_resolution_at || item.expected_delivery_date,
    platforms: Array.isArray(item.platforms) ? item.platforms.filter((value) => typeof value === "string") : (item.platform ? [String(item.platform)] : []),
    platform: item.platform,
    custom_platform: item.custom_platform || "",
    rating: item.rating, ratings: item.ratings, can_manage: item.can_manage,
    ratedByViewer: item.ratedByViewer, ownedByViewer: item.ownedByViewer, ratingCount: item.ratingCount,
    labels: Array.isArray(item.labels) ? item.labels.filter((label) => typeof label === "string") : [],
    reproduction_steps: item.reproduction_steps || "", expected_result: item.expected_result || "",
    comments: (item.comments || []).map((comment) => ({
      id: comment.id, author_id: comment.author_id || comment.user_id || "",
      author_username: comment.author_username || comment.author || "需求用户",
      body: comment.body || "", created_at: comment.created_at, edited_at: comment.edited_at, editableByViewer: comment.editableByViewer === true,
      attachments: (comment.attachments || []).map((attachment) => ({id: attachment.id, name: attachment.name || attachment.original_name, original_name: attachment.original_name, mime_type: attachment.mime_type || attachment.mime})),
    })),
    attachments: (item.attachments || []).map((attachment) => ({id: attachment.id, name: attachment.name || attachment.original_name, original_name: attachment.original_name, mime_type: attachment.mime_type || attachment.mime})),
  });
  const projectHistory = (item) => {
    const result = project(item);
    result.status_events = (item.status_events || item.status_history || []).map((event, index, history) => ({changed_at: event.changed_at || event.at, from_status: event.from_status || history[index - 1]?.status, to_status: event.to_status || event.status}));
    return result;
  };
  const contextId = () => window.SWToolRequirementsContext?.() || "";
  const binaryResponse = (result) => {
    if (!result || typeof result.data_base64 !== "string") return null;
    const binary = atob(result.data_base64);
    const headers = {"Content-Type": result.content_type || "application/octet-stream"};
    if (result.content_disposition) headers["Content-Disposition"] = result.content_disposition;
    return new Response(Uint8Array.from(binary, (character) => character.charCodeAt(0)), {status: result.status || 200, headers});
  };
  const fileBase64 = async (file) => {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 0x8000) binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
    return btoa(binary);
  };
  const jsonAttachments = async (form) => {
    const attachments = [];
    for (const value of form?.values?.() || []) {
      if (!value || typeof value.arrayBuffer !== "function") continue;
      attachments.push({name: value.name || "attachment", contentBase64: await fileBase64(value)});
    }
    return attachments;
  };
  async function request(path, options = {}, remote = true) {
    const method = String(options.method || "GET").toUpperCase(), center = remote ? contextId() : "";
    const homeDriver = !center && /^\/api\/requirements\/driver(?:\/(host-key|start|stop|resume|rollback|interject)|\/logs(?:\?[^#]*)?)?$/.test(path);
    const nearPath = homeDriver ? path.slice(4) : path.startsWith(`${api}/local-centers`) ? path.slice(api.length) : path.startsWith("/api/local-centers") ? path.slice(4) : "";
    if (nearPath) {
      const collection = /^\/local-centers$/.test(nearPath);
      const single = /^\/local-centers\/[A-Za-z0-9_-]+$/.test(nearPath);
      const tickets = /^\/local-centers\/[A-Za-z0-9_-]+\/tickets(?:\?[^#]*)?$/.test(nearPath);
      const ticket = /^\/local-centers\/[A-Za-z0-9_-]+\/tickets\/[A-Za-z0-9_-]+$/.test(nearPath);
      const comments = /^\/local-centers\/[A-Za-z0-9_-]+\/tickets\/[A-Za-z0-9_-]+\/comments$/.test(nearPath);
      const comment = /^\/local-centers\/[A-Za-z0-9_-]+\/tickets\/[A-Za-z0-9_-]+\/comments\/[A-Za-z0-9_-]+$/.test(nearPath);
      const rating = /^\/local-centers\/[A-Za-z0-9_-]+\/tickets\/[A-Za-z0-9_-]+\/rating$/.test(nearPath);
      const attachments = /^\/local-centers\/[A-Za-z0-9_-]+\/tickets\/[A-Za-z0-9_-]+\/attachments(?:\?comment_id=[A-Za-z0-9_-]+)?$/.test(nearPath);
      const attachment = /^\/local-centers\/[A-Za-z0-9_-]+\/tickets\/[A-Za-z0-9_-]+\/attachments\/[A-Za-z0-9_-]+(?:\?download=1)?$/.test(nearPath);
      const driver = nearPath === "/requirements/driver" || /^\/local-centers\/[A-Za-z0-9_-]+\/driver$/.test(nearPath);
      const driverLogs = /^\/requirements\/driver\/logs(?:\?[^#]*)?$/.test(nearPath) || /^\/local-centers\/[A-Za-z0-9_-]+\/driver\/logs(?:\?[^#]*)?$/.test(nearPath);
      const driverAction = /^\/requirements\/driver\/(host-key|start|stop|resume|rollback|interject)$/.test(nearPath) || /^\/local-centers\/[A-Za-z0-9_-]+\/driver\/(host-key|start|stop|resume|rollback|interject)$/.test(nearPath);
      const allowed = (collection && ["GET", "POST"].includes(method)) || (single && ["GET", "PATCH", "DELETE"].includes(method)) ||
        (tickets && ["GET", "POST"].includes(method) && (method !== "POST" || !nearPath.includes("?"))) ||
        (ticket && ["GET", "PATCH", "DELETE"].includes(method)) || (comments && method === "POST") ||
        (comment && ["PATCH", "DELETE"].includes(method)) ||
        (rating && method === "POST") || (attachments && method === "POST") || (attachment && ["GET", "DELETE"].includes(method)) ||
        (driver && method === "GET") || (driverLogs && method === "GET") || (driverAction && method === "POST");
      if (!allowed) return blocked();
      const headers = new Headers(options.headers || {});
      headers.delete("Authorization");
      let body = options.body;
      if (typeof FormData !== "undefined" && body instanceof FormData) {
        if (attachments && method === "POST") {
          body = JSON.stringify({attachments: await jsonAttachments(body)});
          headers.set("Content-Type", "application/json");
        } else headers.delete("Content-Type");
      }
      const status = await auth();
      if (!status.authenticated || !initialPrincipal || (method !== "GET" && !status.csrfToken)) throw Error("请先登录宿主并重新加载页面");
      headers.set("X-SWTool-Expected-User", initialPrincipal);
      if (method !== "GET") headers.set("X-CSRF-Token", status.csrfToken);
      const result = await raw(`${api}${nearPath}`, {...options, method, headers, body});
      const data = result.ok && result.status !== 204 ? await result.json() : null;
      const currentStatus = await auth();
      if (!currentStatus.authenticated || !initialPrincipal) throw Error("请先登录宿主并重新加载页面");
      if (!result.ok || result.status === 204) return result;
      if (attachment && method === "GET") return binaryResponse(data) || json(data);
      if (tickets && method === "GET") {
        const items = (data.items || data.tickets || []).map(projectHistory);
        return json({...data, items, tickets: items});
      }
      if ((ticket || tickets || comments || attachments) && (data.ticket || data.id && (data.title !== undefined || data.comments))) return json(projectHistory(data.ticket || data));
      return json(data);
    }
    const remoteTicket = /^\/api\/tickets(?:\?[^#]*)?$/.test(path) || /^\/api\/tickets\/[A-Za-z0-9_-]+(?:\/comments(?:\/[A-Za-z0-9_-]+)?)?$/.test(path);
    const remoteAttachment = /^\/api\/tickets\/[A-Za-z0-9_-]+\/attachments(?:\/[A-Za-z0-9_-]+)?(?:\?[^#]*)?$/.test(path);
    const localTicket = !center && (remoteTicket || remoteAttachment);
    const ticketWrite = center && ((method === "POST" && (path === "/api/tickets" || /^\/api\/tickets\/[A-Za-z0-9_-]+\/comments(?:\/[A-Za-z0-9_-]+)?$/.test(path) || remoteAttachment)) || (["PATCH", "DELETE"].includes(method) && (/^\/api\/tickets\/[A-Za-z0-9_-]+$/.test(path) || /^\/api\/tickets\/[A-Za-z0-9_-]+\/comments\/[A-Za-z0-9_-]+$/.test(path))));
    const localArchiveImport = !remote && path === `${api}/tools/import` && method === "POST";
    if (method !== "GET" && !ticketWrite && !localTicket && !localArchiveImport && (remote || !(/^\/api\/requirements\/(?:key(?:\/(?:rotate|reveal))?|connections(?:\/[A-Za-z0-9_-]+)?)$/.test(path)))) return blocked();
    if (remote && center && path === "/api/requirements/cases" && method === "GET") return json({error: "远端服务暂不支持关联用例查询", code: "remote_cases_unsupported"}, 501);
    if (remote && center && !remoteTicket && !remoteAttachment && path !== "/api/requirements/identity") return json({error: "目标中心暂不支持此能力，请在目标中心操作"}, 403);
    if (path === "/api/requirements/cases" && method === "GET") return json({error: "宿主暂不支持关联用例查询"}, 501);
    if ((path === "/me" || path === "/api/auth/status") && method === "GET") {
      const status = await auth();
      if (path === "/api/auth/status") return json(status);
      if (!status.authenticated) return json({error: "请先登录宿主"}, 401);
      const user = status.user || {};
      return json({...user, roles: user.roles || (["admin", "superadmin"].includes(String(user.role).toLowerCase()) ? ["admin"] : ["user"])});
    }
    if (path === "/api/plugins/requirements-center" && method === "GET") {
      const branding = await payload("/api/branding"); return json({active: branding.requirementsCenterEnabled === true});
    }
    if (path === "/api/requirements/identity" && method === "GET") {
      if (center) {
        const identity = await payload(`${api}/tools/connections/${encodeURIComponent(center)}/identity`);
        return json({...identity, user_id: identity.user_id || identity.id, roles: Array.isArray(identity.roles) ? identity.roles : []});
      }
      const status = await auth(), user = status.user || {};
      return json({id: user.user_id || user.id, user_id: user.user_id || user.id, username: user.username || "", roles: ["admin", "superadmin"].includes(String(user.role).toLowerCase()) ? ["admin"] : ["user"]});
    }
    if (!remote && path === "/api/requirements/source" && method === "GET") {
      const source = await payload(`${api}/tools/source`);
      const encoded = source.data_base64;
      const limit = 16 * 1024 * 1024;
      if (typeof encoded !== "string" || !encoded.length || encoded.length > Math.ceil(limit / 3) * 4 || encoded.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) throw Error("下载内容无效或超过大小限制。");
      const binary = atob(encoded);
      if (binary.length > limit || binary.slice(0, 2) !== "PK") throw Error("服务器未返回有效 ZIP 文件。");
      return new Response(Uint8Array.from(binary, (character) => character.charCodeAt(0)), {headers: {"Content-Type": "application/zip"}});
    }
    if (!remote && path === "/api/requirements/downloads" && method === "GET") {
      return json(await payload(`${api}/tools/downloads`));
    }
    if (!remote && path === "/api/requirements/releases" && method === "GET") {
      return json(await payload(`${api}/tools/releases`));
    }
    if (path === "/api/requirements/connections" && method === "GET") {
      const result = await payload(`${api}/tools/connections`), items = (result.items || result.connections || []).map((item) => ({id: item.id, name: item.name, base_url: item.url || item.base_url || item.api_base, identity: item.identity || {}}));
      return json({items, connections: items});
    }
    if (path === "/api/requirements/key" && method === "GET") {
      const result = await payload(`${api}/tools/status`); return json({exists: Boolean(result.key?.active), suffix: result.key?.suffix || ""});
    }
    const csrf = async () => {
      const status = await auth();
      if (!status.authenticated || !initialPrincipal || !status.csrfToken) throw Error("请先登录宿主并重新加载页面");
      return status.csrfToken;
    };
    if (!remote && path === `${api}/tools/export` && method === "GET") {
      const result = await payload(`${api}/tools/export`);
      const encoded = result.data_base64, limit = 16 * 1024 * 1024;
      if (typeof encoded !== "string" || !encoded.length || encoded.length > Math.ceil(limit / 3) * 4 || encoded.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) throw Error("下载内容无效或超过大小限制。");
      const binary = atob(encoded);
      if (binary.length > limit || binary.slice(0, 2) !== "PK") throw Error("服务器未返回有效 ZIP 文件。");
      return new Response(Uint8Array.from(binary, (character) => character.charCodeAt(0)), {headers: {"Content-Type": "application/zip"}});
    }
    if (!remote && path === `${api}/tools/import` && method === "POST") {
      const form = options.body;
      const file = form?.get?.("file");
      if (!file || typeof file.arrayBuffer !== "function") return json({error: "请选择有效的 ZIP 文件"}, 400);
      const bytes = new Uint8Array(await file.arrayBuffer());
      if (!bytes.length || bytes.length > 16 * 1024 * 1024 || bytes[0] !== 0x50 || bytes[1] !== 0x4b) return json({error: "请选择有效的 ZIP 文件"}, 400);
      let binary = "";
      for (const byte of bytes) binary += String.fromCharCode(byte);
      const headers = {"X-CSRF-Token": await csrf(), "Content-Type": "application/json"};
      const result = await payload(`${api}/tools/import`, {method: "POST", headers, body: JSON.stringify({data_base64: btoa(binary), dry_run: false, user_mapping: options.user_mapping || {}, ownership_confirmed: options.ownership_confirmed === true})});
      return json(result);
    }
    if (path === "/api/requirements/key/rotate" && method === "POST") {
      const scope = JSON.parse(options.body || "{}").scope || "tickets:read";
      if (!["tickets:read", "tickets:write"].includes(scope)) return json({error: "请选择有效的密钥权限"}, 400);
      const result = await payload(`${api}/tools/key`, {method: "POST", headers: {"X-CSRF-Token": await csrf(), "Content-Type": "application/json"}, body: JSON.stringify({scope})}); return json(result);
    }
    if (path === "/api/requirements/key/reveal" && method === "POST") return json(await payload(`${api}/tools/key/reveal`, {method: "POST", headers: {"X-CSRF-Token": await csrf(), "Content-Type": "application/json"}, body: "{}"}));
    if (path === "/api/requirements/key" && method === "DELETE") return json(await payload(`${api}/tools/key/revoke`, {method: "POST", headers: {"X-CSRF-Token": await csrf(), "Content-Type": "application/json"}, body: "{}"}));
    if (path === "/api/requirements/connections" && method === "POST") {
      const body = JSON.parse(options.body || "{}");
      const result = await payload(`${api}/tools/connections`, {method: "POST", headers: {"X-CSRF-Token": await csrf(), "Content-Type": "application/json"}, body: JSON.stringify({name: body.name, base_url: body.base_url, key: body.key, ...(body.id ? {id: body.id} : {})})});
      if (!result.id || result.valid === false) throw Error(result.error || result.reason || "远端连接验证未通过");
      return json({id: result.id, name: result.name, base_url: result.url || result.base_url});
    }
    const remove = path.match(/^\/api\/requirements\/connections\/([^/?]+)$/);
    if (remove && method === "DELETE") return json(await payload(`${api}/tools/connections/${encodeURIComponent(remove[1])}`, {method: "DELETE", headers: {"X-CSRF-Token": await csrf()}}));
    if (localTicket && remoteAttachment && (method === "GET" || method === "DELETE")) {
      if (method === "GET") return raw(`${api}${path.slice(4)}`);
      const result = await payload(`${api}${path.slice(4)}`, {method: "DELETE", headers: {"X-CSRF-Token": await csrf()}});
      return json(result);
    }
    if (localTicket && method === "GET" && remoteAttachment) {
      const result = await payload(`${api}${path.slice(4)}`);
      return binaryResponse(result) || json(result);
    }
    if (localTicket && method !== "GET") {
      const headers = new Headers(options.headers || {});
      headers.delete("Authorization");
      let body = options.body;
      if (remoteAttachment && method === "POST" && typeof FormData !== "undefined" && body instanceof FormData) {
        body = JSON.stringify({attachments: await jsonAttachments(body)});
        headers.set("Content-Type", "application/json");
      } else if (!headers.has("Content-Type") && !(typeof FormData !== "undefined" && body instanceof FormData)) headers.set("Content-Type", "application/json");
      const result = await payload(`${api}${path.slice(4)}`, {method, headers: {...Object.fromEntries(headers.entries()), "X-CSRF-Token": await csrf()}, body});
      return json(projectHistory(result.ticket || result));
    }
    if ((remoteTicket || remoteAttachment) && center) {
      const tail = path.slice("/api/tickets".length);
      const headers = new Headers(options.headers || {});
      headers.delete("Authorization");
      let body = options.body;
      if (remoteAttachment && method === "POST" && typeof FormData !== "undefined" && body instanceof FormData) {
        body = JSON.stringify({attachments: await jsonAttachments(body)});
        headers.set("Content-Type", "application/json");
      } else if (method !== "GET" && !headers.has("Content-Type") && !(typeof FormData !== "undefined" && body instanceof FormData)) headers.set("Content-Type", "application/json");
      const result = await payload(`${api}/tools/connections/${encodeURIComponent(center)}/tickets${tail}`, {method, headers: {...Object.fromEntries(headers.entries()), "X-CSRF-Token": await csrf()}, ...(method === "GET" ? {} : {body})});
      if (remoteAttachment && method === "GET") return binaryResponse(result) || json(result);
      if (remoteAttachment) return json(projectHistory(result.ticket || result));
      if (method !== "GET") return json(projectHistory(result.ticket || result));
      if (tail.startsWith("/")) return json(projectHistory(result.ticket || result));
      const items = (result.items || result.tickets || []).map(projectHistory);
      return json({items, tickets: items, page: result.page, total: result.total, total_pages: result.total_pages});
    }
    if (remote && method !== "GET") return blocked();
    if (!remote && method !== "GET") return blocked();
    const localPath = path.replace(/^\/api\/v1/, "");
    if (/^\/api\/tickets(?:\?.*)?$/.test(localPath)) {
      const query = new URLSearchParams(localPath.split("?")[1] || "");
      const suffix = query.toString();
      const result = await payload(`${api}/tickets${suffix ? "?" + suffix : ""}`), items = (result.items || result.tickets || []).map(projectHistory);
      return json({items, tickets: items, page: result.page, total: result.total, total_pages: result.total_pages});
    }
    const detail = localPath.match(/^\/api\/tickets\/([A-Za-z0-9_-]+)$/);
    if (detail) return json(projectHistory(await payload(`${api}/tickets/${encodeURIComponent(detail[1])}`)));
    return json({error: "宿主 Hub 不支持此接口"}, 404);
  }
  function install() {
    document.documentElement.dataset.requirementsHost = "true";
    for (const id of ["hub-auth", "hub-plugins", "hub-admin", "hub-logout", "hub-export", "hub-import", "hub-new"]) { const item = document.getElementById(id); if (item) item.hidden = true; }
    const archiveHelp = document.getElementById("hub-host-archive-help"); if (archiveHelp) archiveHelp.hidden = false;
    const create = document.getElementById("hub-new"); if (create) create.disabled = true;
    const identity = document.getElementById("hub-user"); if (identity) identity.textContent = "宿主登录会话";
  }
  window.SWToolRequirementsHost = {active: true, request, session, install};
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", install, {once: true}); else install();
})();
