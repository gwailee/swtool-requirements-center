/*
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 上海商江网络科技有限公司版权所有
软工云（swtool.com）开发团队；uqtool@163.com
*/
/* Community Markdown conventions, with DOM construction for untrusted tickets. */
(function () {
  'use strict';
  const base = new URL('.', document.currentScript.src), loads = new Map();
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
  function load(path, ready) {
    if (ready()) return Promise.resolve();
    if (!loads.has(path)) loads.set(path, new Promise((resolve, reject) => {
      const script = node('script'); script.src = new URL(path, base).href;
      const timer = setTimeout(() => { script.remove(); reject(Error('resource timeout')); }, 10000);
      script.onload = () => { clearTimeout(timer); ready() ? resolve() : reject(Error('renderer missing')); };
      script.onerror = () => { clearTimeout(timer); script.remove(); reject(Error('resource unavailable')); };
      document.head.append(script);
    }).catch(error => { loads.delete(path); throw error; }));
    return loads.get(path);
  }
  const mermaid = () => {
    const queue = [window.mermaid], seen = new Set();
    while (queue.length) { const v = queue.shift(); if (!v || seen.has(v)) continue; seen.add(v); if (typeof v.render === 'function') return v; ['default','mermaid','api','mermaidAPI'].forEach(k => { if (v[k]) queue.push(v[k]); }); }
  };
  function svgResult(value, seen = new Set()) {
    if (typeof value === 'string') return value;
    if (!value || typeof value !== 'object' || seen.has(value)) return '';
    seen.add(value); for (const key of ['svg','default','result','data']) { const svg = svgResult(value[key], seen); if (svg) return svg; } return '';
  }
  function safeSvg(source) {
    const parsed = new DOMParser().parseFromString(source, 'image/svg+xml'), svg = parsed.documentElement;
    if (svg.localName !== 'svg' || parsed.querySelector('parsererror')) throw Error('invalid SVG');
    // Keep Mermaid's static drawing; no foreign HTML, navigation or callbacks.
    svg.querySelectorAll('script,foreignObject,iframe,object,embed,a,style,animate,animateMotion,animateTransform,set').forEach(n => n.remove());
    [svg, ...svg.querySelectorAll('*')].forEach(n => [...n.attributes].forEach(a => {
      if (/^on/i.test(a.name) || /(?:href|src)$/i.test(a.name) || (/url\s*\(/i.test(a.value) && !/^url\(#[\w-]+\)$/.test(a.value)) || /(?:javascript|data|https?):/i.test(a.value)) n.removeAttribute(a.name);
    }));
    return document.importNode(svg, true);
  }
  async function formula(target, source, display) {
    try {
      if (!document.querySelector('link[data-ticket-katex]')) { const css = node('link'); css.rel = 'stylesheet'; css.href = new URL('vendor/katex/katex.min.css', base).href; css.dataset.ticketKatex = '1'; document.head.append(css); }
      await load('vendor/katex/katex.min.js', () => window.katex && typeof window.katex.render === 'function');
      window.katex.render(source, target, {displayMode: display, throwOnError: true, strict: 'ignore', trust: false, output: 'htmlAndMathml'});
    } catch (_) { target.textContent = (display ? '$$' : '$') + source + (display ? '$$' : '$'); target.title = '公式暂不可渲染，已保留源码'; }
  }
  async function diagram(target, source) {
    const drawing = node('div', undefined, 'ticket-mermaid-drawing'); target.append(drawing);
    try {
      await load('vendor/mermaid.min.js', mermaid); const renderer = mermaid();
      renderer.initialize({startOnLoad: false, securityLevel: 'strict', flowchart: {htmlLabels: false}});
      let timer; const output = await Promise.race([Promise.resolve().then(() => renderer.render('ticket-diagram-' + Math.random().toString(36).slice(2), source)), new Promise((_, reject) => { timer = setTimeout(() => reject(Error('render timeout')), 10000); })]).finally(() => clearTimeout(timer));
      drawing.replaceChildren(safeSvg(svgResult(output)));
    } catch (_) { drawing.replaceChildren(node('p', '流程图暂不可渲染，已保留源码。', 'ticket-render-error')); }
  }
  const relationRenders = new WeakMap();
  async function renderRelations(target, ticket, {navigate, isCurrent = () => true} = {}) {
    const token = {}; relationRenders.set(target, token);
    const current = () => relationRenders.get(target) === token && target.isConnected && isCurrent();
    target.replaceChildren(); target.classList.add('ticket-markdown', 'ticket-relation-diagram');
    const children = Array.isArray(ticket.child_tickets) ? ticket.child_tickets : [];
    if (!ticket.parent_ticket && !children.length) return;
    const drawing = node('div', undefined, 'ticket-mermaid-drawing'); target.append(drawing);
    const entities = value => Array.from(String(value || '未命名需求').slice(0, 100)).map(c => /^[\p{L}\p{N} ]$/u.test(c) ? c : '#' + c.codePointAt(0) + ';').join('');
    const records = [{item: ticket, key: 'r0', current: true}];
    if (ticket.parent_ticket) records.push({item: ticket.parent_ticket, key: 'r1'});
    children.slice(0, 20).forEach((item, index) => records.push({item, key: 'r' + (index + 2)}));
    const lines = ['flowchart TD', ...records.map(row => row.key + '["' + entities(row.item.title) + '"]')];
    if (ticket.parent_ticket) lines.push('r1 --> r0');
    children.slice(0, 20).forEach((_, index) => lines.push('r0 --> r' + (index + 2)));
    if (children.length > 20) target.append(node('p', '图中仅显示前 20 个直属子工单，其余请见下方关系列表。'));
    try {
      await load('vendor/mermaid.min.js', mermaid);
      if (!current()) return;
      const renderer = mermaid();
      renderer.initialize({startOnLoad: false, securityLevel: 'strict', flowchart: {htmlLabels: false}});
      let timer;
      const output = await Promise.race([
        Promise.resolve().then(() => renderer.render('ticket-relations-' + Math.random().toString(36).slice(2), lines.join('\n'))),
        new Promise((_, reject) => { timer = setTimeout(() => reject(Error('render timeout')), 10000); })
      ]).finally(() => clearTimeout(timer));
      if (!current()) return;
      const svg = safeSvg(svgResult(output));
      // Mermaid strict mode may leave its numeric entities as literal SVG text.
      // Decode once into textContent only, after sanitizing; never parse label HTML.
      for (const label of svg.querySelectorAll('text,tspan')) {
        if (label.children.length) continue;
        label.textContent = label.textContent.replace(/&#([0-9]{1,7});/g, (entity, number) => {
          const point = Number(number);
          return point <= 0x10ffff && !(point >= 0xd800 && point <= 0xdfff) ? String.fromCodePoint(point) : entity;
        });
      }
      svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('focusable', 'false');
      svg.querySelectorAll('[tabindex]').forEach(n => n.removeAttribute('tabindex'));
      // Only internal generated node IDs can receive application navigation handlers.
      for (const element of svg.querySelectorAll('g.node')) {
        const match = /^flowchart-(r[0-9]+)-[0-9]+$/.exec(element.getAttribute('id') || '');
        const row = match && records.find(record => record.key === match[1]);
        if (!row || row.current || typeof row.item.id !== 'string' || typeof navigate !== 'function') continue;
        element.classList.add('ticket-relation-node');
        element.addEventListener('click', () => { if (current()) navigate(row.item.id); });
      }
      drawing.replaceChildren(svg);
    } catch (_) {
      if (current()) drawing.replaceChildren(node('p', '关系图暂不可用，请使用下方关系列表。', 'ticket-render-error'));
    }
  }
  function inline(parent, source) {
    // Images use the same-origin attachment/upload routes, never external trackers.
    const image = /!\[([^\]]*)\]\(([^\s)]+)\)/.exec(source);
    if (image) {
      inline(parent, source.slice(0, image.index)); let url;
      try { url = new URL(image[2], location.href); } catch (_) {}
      if (url && url.origin === new URL(location.href).origin && /^(?:\/uploads\/|\/api\/.*\/attachments\/)/.test(url.pathname)) {
        const img = node('img'); img.src = url.href; img.alt = image[1]; img.loading = 'lazy'; parent.append(img);
      } else parent.append(document.createTextNode(image[0]));
      inline(parent, source.slice(image.index + image[0].length)); return;
    }
    const pattern = /`([^`\n]+)`|\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$([^$\n]+)\$|\*\*([^\n]+?)\*\*|__([^\n]+?)__|\*([^*\n]+)\*|~~([^\n]+?)~~|\[([^\]]+)\]\(([^\s)]+)\)/g;
    let start = 0, m;
    while ((m = pattern.exec(source))) {
      parent.append(document.createTextNode(source.slice(start, m.index)));
      if (m[1]) parent.append(node('code', m[1]));
      else if (m[2] || m[3] || m[4] || m[5]) { const display = !!(m[2] || m[3]), span = node('span', m[0], display ? 'math-block' : 'math-inline'); parent.append(span); void formula(span, m[2] || m[3] || m[4] || m[5], display); }
      else if (m[6] || m[7] || m[8] || m[9]) parent.append(node(m[8] ? 'em' : m[9] ? 'del' : 'strong', m[6] || m[7] || m[8] || m[9]));
      else { let url; try { url = new URL(m[11], location.href); } catch (_) {} if (url && /^https?:$/.test(url.protocol) && !m[11].startsWith('//')) { const a = node('a', m[10]); a.href = url.href; a.target = '_blank'; a.rel = 'noopener noreferrer'; parent.append(a); } else parent.append(document.createTextNode(m[0])); }
      start = pattern.lastIndex;
    }
    parent.append(document.createTextNode(source.slice(start)));
  }
  function render(target, value) {
    target.replaceChildren(); target.classList.add('ticket-markdown', 'guide-markdown');
    const lines = String(value || '').replace(/\r\n?/g, '\n').split('\n'); let i = 0;
    const cells = s => s.trim().replace(/^\||\|$/g, '').split('|').map(v => v.trim());
    while (i < lines.length) {
      const line = lines[i]; if (!line.trim()) { i++; continue; }
      if (/^\s*```/.test(line)) { const lang = line.trim().slice(3).trim().toLowerCase(), code = []; i++; while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]); if (i < lines.length) i++; const pre = node('pre', undefined, 'guide-code'), codeNode = node('code', code.join('\n')); pre.append(codeNode); target.append(pre); if (lang === 'mermaid') { const wrapper = node('div', undefined, 'ticket-mermaid'); pre.replaceWith(wrapper); wrapper.append(pre); void diagram(wrapper, code.join('\n')); } else if (lang === 'math' || lang === 'latex') { const math = node('div', code.join('\n'), 'math-block'); pre.replaceWith(math); void formula(math, code.join('\n'), true); } continue; }
      if (/^\s*(\$\$|\\\[)\s*$/.test(line)) { const end = line.trim() === '$$' ? '$$' : '\\]', parts = []; i++; while (i < lines.length && lines[i].trim() !== end) parts.push(lines[i++]); if (i < lines.length) i++; const math = node('div', parts.join('\n'), 'math-block'); target.append(math); void formula(math, parts.join('\n'), true); continue; }
      if (/^\s*(?:---+|\*\*\*+|___+)\s*$/.test(line)) { target.append(node('hr')); i++; continue; }
      const heading = /^(#{1,6})\s+(.+)$/.exec(line);
      if (heading) { const h = node('h' + heading[1].length); inline(h, heading[2]); target.append(h); i++; continue; }
      if (line.includes('|') && i + 1 < lines.length && cells(lines[i + 1]).every(c => /^:?-{3,}:?$/.test(c))) { const wrap = node('div', undefined, 'document-table-wrap'), table = node('table'), head = node('thead'), body = node('tbody'); const row = (s, tag) => { const tr = node('tr'); cells(s).forEach(v => { const td = node(tag); inline(td, v); tr.append(td); }); return tr; }; head.append(row(line, 'th')); i += 2; while (i < lines.length && lines[i].trim() && lines[i].includes('|')) body.append(row(lines[i++], 'td')); table.append(head, body); wrap.append(table); target.append(wrap); continue; }
      if (/^\s*(?:[-*+] |\d+\. )/.test(line)) { const ordered = /^\s*\d+\./.test(line), list = node(ordered ? 'ol' : 'ul'), re = ordered ? /^\s*\d+\.\s+/ : /^\s*[-*+]\s+/; while (i < lines.length && re.test(lines[i])) { const li = node('li'); inline(li, lines[i++].replace(re, '')); list.append(li); } target.append(list); continue; }
      const quote = /^>\s?/.test(line), p = node(quote ? 'blockquote' : 'p'); inline(p, quote ? line.replace(/^>\s?/, '') : line); target.append(p); i++;
    }
  }
  window.SwtoolTicketContent = {render, renderRelations};
})();
