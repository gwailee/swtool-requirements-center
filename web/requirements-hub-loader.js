/*
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 上海商江网络科技有限公司版权所有
软工云（swtool.com）开发团队；uqtool@163.com
*/
(() => {
  if (!location.pathname.startsWith("/plugins/requirements-center/standalone/web/")) return;
  const stylesheet = document.createElement("link");
  stylesheet.rel = "stylesheet";
  stylesheet.href = "/css/styles.css";
  document.head.prepend(stylesheet);
  const header = document.querySelector(".hub-header");
  header.classList.add("masthead");
  header.querySelector("nav").classList.add("actions", "masthead-actions");
  document.querySelector("#hub-home .eyebrow").hidden = true;
  const navigation = document.createElement("script");
  navigation.src = "/js/nav-compat.js";
  navigation.onload = () => {
    const brand = document.createElement("script");
    brand.src = "/js/brand.js";
    document.head.append(brand);
  };
  document.head.append(navigation);
})();
