/*
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 上海商江网络科技有限公司版权所有
软工云（swtool.com）开发团队；uqtool@163.com
*/
(() => {
  const status = document.getElementById("guide-status");
  const scriptLoads = new Map();
  const loadScript = (src, onError) => new Promise((resolve) => {
    const url = new URL(src, location.href).href;
    if (scriptLoads.has(url)) return scriptLoads.get(url).then(resolve);
    const existing = [...(document.querySelectorAll?.("script[src]") || [])]
      .find((script) => new URL(script.src, location.href).href === url);
    if (existing) {
      scriptLoads.set(url, Promise.resolve(true));
      return resolve(true);
    }
    const loaded = new Promise((finish) => {
      const script = document.createElement("script");
      script.src = url;
      script.onload = () => finish(true);
      script.onerror = () => {
        onError?.();
        finish(false);
      };
      document.head.append(script);
    });
    scriptLoads.set(url, loaded);
    loaded.then(resolve);
  });

  const mermaidCandidates = [
    "./vendor/mermaid.min.js",
    "/plugins/requirements-center/standalone/web/vendor/mermaid.min.js",
    "/vendor/mermaid.min.js",
    "/mermaid.min.js",
  ];
  const getMermaidRenderer = (candidate) => {
    const queue = [candidate];
    const seen = new Set();
    while (queue.length) {
      const value = queue.shift();
      if (!value || (typeof value !== "object" && typeof value !== "function") || seen.has(value)) continue;
      seen.add(value);
      if (typeof value.render === "function") return value;
      ["default", "mermaid", "api", "mermaidAPI"].forEach((key) => queue.push(value[key]));
    }
    return null;
  };
  const waitForMermaid = async (deadline) => {
    while (Date.now() < deadline) {
      const renderer = getMermaidRenderer(window.mermaid);
      if (renderer) {
        window.mermaid = renderer;
        return true;
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    return false;
  };
  const loadMermaid = async ([candidate, ...remaining], deadline = Date.now() + 10000) => {
    if (!candidate) return false;
    const loaded = await loadScript(candidate);
    window.__swtoolMermaidDiagnostic = loaded ? "MERMAID_RENDERER_MISSING" : "MERMAID_RESOURCE_LOAD_FAILED";
    if (loaded && await waitForMermaid(deadline)) {
      window.__swtoolMermaidDiagnostic = "";
      return true;
    }
    if (Date.now() >= deadline) return false;
    return loadMermaid(remaining, deadline);
  };

  window.__swtoolMermaidReady = loadMermaid(mermaidCandidates);

  const loadGuide = () => loadScript("./requirements-guide.js?v=20261026.6", () => {
    if (status) status.textContent = "指南脚本加载失败。";
    console.error("requirements-guide.js 加载失败");
  });

  window.__swtoolMermaidReady.then(loadGuide);

  if (location.pathname.startsWith("/plugins/requirements-center/standalone/web/")) {
    const stylesheet = document.createElement("link");
    stylesheet.rel = "stylesheet";
    stylesheet.href = "/css/styles.css";
    document.head.prepend(stylesheet);
    const header = document.querySelector(".guide-tools");
    header.classList.add("masthead");
    header.querySelector(".guide-tools-actions").classList.add("actions", "masthead-actions");
    loadScript("/js/nav-compat.js").then((loaded) => loaded && loadScript("/js/brand.js"));
  }
})();
