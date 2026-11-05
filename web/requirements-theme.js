/*
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 上海商江网络科技有限公司版权所有
软工云（swtool.com）开发团队；uqtool@163.com
*/
/* Shared standalone theme; apply before paint, with no account/token dependency. */
(()=>{
  const key='swtool_requirements_theme',themes={dark:'黑色',light:'白色',contrast:'高对比度'};
  const hostKey='swtool-cloud-display-theme';
  const hasTheme=theme=>Object.prototype.hasOwnProperty.call(themes,theme);
  const hosted=location.pathname.startsWith('/plugins/requirements-center/standalone/web/')||location.pathname==='/requirements-center.html';
  let hostBody;
  if(hosted)try{if(window.parent!==window)hostBody=window.parent.document.body}catch{}
  function hostTheme(){
    if(!hosted)return;
    const current=hostBody?.dataset.theme;
    if(hasTheme(current))return current;
    try{return localStorage.getItem(hostKey)}catch{}
  }
  let selected;
  try{selected=localStorage.getItem(key)}catch{}
  if(hosted)selected=hostTheme();
  if(!hasTheme(selected))selected='dark';
  function apply(theme,persist=false){
    if(!hasTheme(theme))return;
    selected=theme;document.documentElement.dataset.theme=theme;
    document.documentElement.style.colorScheme=theme==='light'?'light':'dark';
    if(document.body)document.body.dataset.theme=theme;
    document.querySelectorAll('[data-requirements-theme]').forEach(select=>select.value=theme);
    if(persist)try{
      localStorage.setItem(hosted?hostKey:key,theme);
      if(hosted&&hostBody)hostBody.dataset.theme=theme;
    }catch{}
    window.dispatchEvent(new CustomEvent('swtool:requirements-theme',{detail:{theme}}));
  }
  function mount(){
    document.querySelectorAll('[data-theme-control]').forEach(host=>{
      const label=document.createElement('label');label.className='requirements-theme-picker';label.textContent='外观';
      const select=document.createElement('select');select.dataset.requirementsTheme='';select.setAttribute('aria-label','外观主题');
      for(const [value,name] of Object.entries(themes)){const option=document.createElement('option');option.value=value;option.textContent=name;select.append(option)}
      select.onchange=()=>apply(select.value,true);label.append(select);
      if(typeof host.replaceChildren==='function')host.replaceChildren(label);
      else { while(host.firstChild)host.removeChild(host.firstChild); host.appendChild(label); }
    });apply(selected);
  }
  window.SWToolRequirementsTheme={get:()=>selected,set:theme=>apply(theme,true)};
  apply(selected);
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount,{once:true});else mount();
  if(hostBody)new MutationObserver(()=>apply(hostTheme())).observe(hostBody,{attributes:true,attributeFilter:['data-theme']});
  addEventListener('storage',event=>{
    if(hosted&&event.key===hostKey)apply(hasTheme(event.newValue)?event.newValue:'dark');
    else if(!hosted&&event.key===key&&hasTheme(event.newValue))apply(event.newValue);
  });
})();
