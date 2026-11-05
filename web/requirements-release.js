/*
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 上海商江网络科技有限公司版权所有
软工云（swtool.com）开发团队；uqtool@163.com
*/
(() => {
  "use strict";
  const root = document.getElementById("requirements-release");
  if (!root) return;
  const pageVersion = root.dataset.currentVersion || "0.0.0";
  const button = document.getElementById("requirements-release-check");
  const status = document.getElementById("requirements-release-status");
  const result = document.getElementById("requirements-release-result");
  const latest = document.getElementById("requirements-release-latest");
  const notes = document.getElementById("requirements-release-notes");
  const featureList = document.getElementById("requirements-release-features");
  const download = document.getElementById("requirements-release-download");
  const host = window.SWToolRequirementsHost?.active ? window.SWToolRequirementsHost : null;
  // An independently deployed center must never resolve release links against
  // its own installation.  The cloud site is the canonical release authority;
  // the hosted Hub keeps using its authenticated same-origin bridge below.
  const officialOrigin = "https://swtool.com";
  const officialReleasesUrl = `${officialOrigin}/plugins/requirements-center/api/tools/releases`;
  const officialSourceUrl = `${officialOrigin}/plugins/requirements-center/api/tools/source`;
  const versionKey = (value) => String(value || "0").trim().split(".").map((part) => Number(part.match(/^\d+/)?.[0] || 0));
  const compareVersions = (left, right) => {
    const a = versionKey(left), b = versionKey(right);
    for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
      const difference = (a[index] || 0) - (b[index] || 0);
      if (difference) return difference;
    }
    return 0;
  };
  const selectLatest = (releases) => (Array.isArray(releases) ? releases : [])
    .filter((release) => release && ["source", "standalone"].includes(release.platform) && release.version)
    .sort((left, right) => compareVersions(right.version, left.version))[0] || null;
  const setStatus = (message, error = false) => { status.textContent = message; status.dataset.state = error ? "error" : ""; };
  const request = async () => {
    const response = host
      ? await host.request("/api/requirements/releases", {}, false)
      : await fetch(officialReleasesUrl, {credentials: "omit", cache: "no-store", headers: {Accept: "application/json"}});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(data.detail || data.error || `检查失败（${response.status}）`);
    return data;
  };
  const show = (data) => {
    const release = selectLatest(data.releases);
    if (!release) throw Error("服务端没有可用的源码发行版本");
    const url = new URL(String(release.url || officialSourceUrl), officialOrigin);
    if (!["http:", "https:"].includes(url.protocol)) throw Error("发行下载链接无效");
    result.hidden = false;
    latest.textContent = `最新版本：${release.version}`;
    notes.textContent = release.notes || "";
    featureList.replaceChildren(...(Array.isArray(release.features) ? release.features : []).map((feature) => { const item = document.createElement("li"); item.textContent = String(feature); return item; }));
    download.href = url.href;
    download.setAttribute("aria-label", `下载 ${release.version} 源码包`);
    const currentVersion = data.current_version || pageVersion;
    setStatus(compareVersions(release.version, currentVersion) > 0 ? "发现新版本，可手动下载升级。运行目录不会自动替换。" : "当前已是最新版本。");
  };
  const downloadSource = async (event) => {
    if (!host) {
      event.preventDefault();
      download.setAttribute("aria-busy", "true");
      setStatus("正在准备源码包…");
      try {
        const response = await fetch(officialSourceUrl, {credentials: "omit", cache: "no-store", headers: {Accept: "application/json"}});
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw Error(data.detail || data.error || `下载失败（${response.status}）`);
        const encoded = data.data_base64;
        const limit = 16 * 1024 * 1024;
        if (typeof encoded !== "string" || !encoded.length || encoded.length > Math.ceil(limit / 3) * 4 || encoded.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) throw Error("下载内容无效或超过大小限制。");
        const binary = atob(encoded);
        if (binary.length > limit || binary.slice(0, 2) !== "PK") throw Error("服务器未返回有效 ZIP 文件。");
        const objectUrl = URL.createObjectURL(new Blob([Uint8Array.from(binary, (character) => character.charCodeAt(0))], {type: "application/zip"}));
        const link = document.createElement("a");
        link.href = objectUrl;
        link.download = "requirements-center-source.zip";
        link.click();
        URL.revokeObjectURL(objectUrl);
        setStatus("源码包已开始下载。");
      } catch (error) {
        setStatus(error.message || "源码包下载失败，请稍后重试。", true);
      } finally {
        download.removeAttribute("aria-busy");
      }
      return;
    }
    event.preventDefault();
    download.setAttribute("aria-busy", "true");
    setStatus("正在准备源码包…");
    try {
      const response = await host.request("/api/requirements/source", {}, false);
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw Error(data.detail || data.error || `下载失败（${response.status}）`);
      }
      const blob = await response.blob();
      if (!blob.size) throw Error("源码包为空");
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = "requirements-center-source.zip";
      link.click();
      URL.revokeObjectURL(objectUrl);
      setStatus("源码包已开始下载。");
    } catch (error) {
      setStatus(error.message || "源码包下载失败，请稍后重试。", true);
    } finally {
      download.removeAttribute("aria-busy");
    }
  };
  const check = async () => {
    button.disabled = true; result.hidden = true; setStatus("正在检查版本…");
    try { show(await request()); } catch (error) { setStatus(error.message || "版本检查失败，请稍后重试。", true); }
    finally { button.disabled = false; }
  };
  button.addEventListener("click", check);
  download.addEventListener("click", downloadSource);
  window.SWToolRequirementsRelease = {compareVersions, selectLatest};
})();
