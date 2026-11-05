/*
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 上海商江网络科技有限公司版权所有
软工云（swtool.com）开发团队；uqtool@163.com
*/
(async () => {
  const root = document.getElementById("guide-document"),
    status = document.getElementById("guide-status");
  function showMermaidFailure(code) {
    const details = document.createElement("details"), summary = document.createElement("summary"), diagnostic = document.createElement("p");
    summary.textContent = "诊断详情";
    diagnostic.textContent = `${code} | UA: ${navigator.userAgent}`;
    details.append(summary, diagnostic);
    status.textContent = "流程图资源不可用，已保留流程源码。";
    status.append(details);
  }
  try {
    const response = await fetch("requirements-guide.md");
    if (!response.ok) throw Error("指南加载失败");
    const text = await response.text();
    let block = null,
      type = "",
      lines = [];
    function appendBlock() {
      if (type === "mermaid") {
        const div = document.createElement("pre");
        div.className = "mermaid";
        div.textContent = lines.join("\n");
        root.append(div);
      } else if (type === "math") {
        const div = document.createElement("div");
        div.className = "formula";
        root.append(div);
        if (window.katex)
          katex.render(lines.join("\n"), div, {
            displayMode: true,
            throwOnError: false,
            trust: false,
            strict: "warn",
          });
        else div.textContent = lines.join("\n");
      } else {
        const pre = document.createElement("pre");
        pre.textContent = lines.join("\n");
        root.append(pre);
      }
      block = null;
      lines = [];
    }
    for (const line of text.split("\n")) {
      if (line.startsWith("```")) {
        if (block) appendBlock();
        else {
          block = true;
          type = line.slice(3).trim();
        }
        continue;
      }
      if (line.trim() === "$$") {
        if (block && type === "math") appendBlock();
        else {
          block = true;
          type = "math";
        }
        continue;
      }
      if (block) {
        lines.push(line);
        continue;
      }
      if (!line.trim()) continue;
      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      const node = document.createElement(
        heading ? "h" + heading[1].length : "p",
      );
      node.textContent = heading ? heading[2] : line;
      root.append(node);
    }
    if (block) appendBlock();
    const diagrams=[...root.querySelectorAll('.mermaid')].map(node=>({node,source:node.textContent}));
    function getMermaidRenderer(candidate){
      const queue=[candidate],seen=new Set();
      while(queue.length){
        const value=queue.shift();
        if(!value||(typeof value!=='object'&&typeof value!=='function')||seen.has(value))continue;
        seen.add(value);
        if(typeof value.render==='function')return value;
        ['default','mermaid','api','mermaidAPI'].forEach(key=>queue.push(value[key]));
      }
      return null;
    }
    function extractMermaidOutput(value,seen=new Set()){
      if(typeof value==='string')return {svg:value};
      if(!value||typeof value!=='object'||seen.has(value))return null;
      seen.add(value);
      const bindFunctions=typeof value.bindFunctions==='function'?value.bindFunctions:null;
      if('svg' in value){
        const result=extractMermaidOutput(value.svg,seen);
        if(result)return {...result,bindFunctions:bindFunctions||result.bindFunctions};
      }
      for(const nested of Object.values(value)){
        const result=extractMermaidOutput(nested,seen);
        if(result)return {...result,bindFunctions:bindFunctions||result.bindFunctions};
      }
      return null;
    }
    async function renderMermaid(renderer,id,source){
      const output=Promise.resolve().then(()=>renderer.render(id,source));
      let timeout;
      try{
        return await Promise.race([output,new Promise((_,reject)=>{
          timeout=setTimeout(()=>reject(Error('Mermaid render timeout')),10000);
        })]);
      }finally{clearTimeout(timeout)}
    }
    let requested=0,rendering=false;
    async function renderDiagrams(){
      if (window.__swtoolMermaidReady) await window.__swtoolMermaidReady;
      requested++;
      if(rendering)return;
      rendering=true;
      try{
        let revision;
        do{
          revision=requested;
          const theme=window.SWToolRequirementsTheme?.get()||'dark';
          const contrast=theme==='contrast';
          const mermaid=getMermaidRenderer(window.mermaid);
          if (!mermaid) { showMermaidFailure(window.__swtoolMermaidDiagnostic==='MERMAID_RESOURCE_LOAD_FAILED'?'MERMAID_RESOURCE_LOAD_FAILED':'MERMAID_RENDERER_MISSING'); continue; }
          try {
          if(typeof mermaid.initialize==='function')mermaid.initialize({startOnLoad:false,securityLevel:'strict',theme:contrast?'base':theme==='dark'?'dark':'default',
            ...(contrast?{themeVariables:{background:'#000000',primaryColor:'#000000',primaryTextColor:'#ffffff',primaryBorderColor:'#ffffff',lineColor:'#ffff00',secondaryColor:'#000000',tertiaryColor:'#000000',textColor:'#ffffff',mainBkg:'#000000',nodeBorder:'#ffffff',clusterBkg:'#000000',clusterBorder:'#ffffff',titleColor:'#ffffff',edgeLabelBackground:'#000000',actorBkg:'#000000',actorTextColor:'#ffffff',actorBorder:'#ffffff',signalColor:'#ffffff',signalTextColor:'#ffffff',labelBoxBkgColor:'#000000',labelBoxBorderColor:'#ffffff',labelTextColor:'#ffffff',taskBkgColor:'#000000',taskTextColor:'#ffffff',taskBorderColor:'#ffffff',sectionBkgColor:'#000000',sectionBkgColor2:'#000000',altSectionBkgColor:'#000000',gridColor:'#ffffff'}}:{})});
          } catch {
            showMermaidFailure('MERMAID_INIT_FAILED');continue;
          }
          let failures=0;
          for(const {node,source} of diagrams){
            node.removeAttribute('data-processed');node.classList.add('mermaid');node.textContent=source;
            try{
              const output=await renderMermaid(mermaid,`requirements-guide-mermaid-${Math.random().toString(36).slice(2)}`,source);
              const result=extractMermaidOutput(output);
              if(!result?.svg)throw Error('empty Mermaid output');
              node.innerHTML=result.svg;
              if(typeof result.bindFunctions==='function')result.bindFunctions(node);
            }catch{failures++;node.textContent=source;node.classList.remove('mermaid')}
          }
          if(revision===requested){
            if(failures)showMermaidFailure('MERMAID_RENDER_FAILED');
            else status.textContent='';
          }
        }while(revision!==requested);
      }catch{
        showMermaidFailure('MERMAID_RENDER_FAILED');
      }finally{rendering=false}
    }
    window.addEventListener('swtool:requirements-theme',renderDiagrams);
    await renderDiagrams();
  } catch {
    showMermaidFailure('GUIDE_LOAD_FAILED');
  }
})();
