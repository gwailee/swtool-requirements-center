/*
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 上海商江网络科技有限公司版权所有
软工云（swtool.com）开发团队；uqtool@163.com
*/
/* Shared WordPress-style installed plugin list for the host and standalone center. */
(()=>{
  const label=(tag,text)=>{const node=document.createElement(tag);node.textContent=text;return node};
  async function render(container,request,{admin=false,onchange=()=>{}}={}){
    container.replaceChildren();
    const heading=label('h3',admin?'插件管理':'已激活插件');container.append(heading);
    const status=label('p','正在读取插件…');status.setAttribute('role','status');container.append(status);
    const list=document.createElement('div');container.append(list);
    async function refresh(){
      const data=await request(admin?'/admin/plugins':'/api/plugins');
      if(!container.isConnected)return;
      list.replaceChildren();
      for(const plugin of data.items){
        const row=document.createElement('section');row.className='plugin-row';
        row.append(label('strong',plugin.displayName||plugin.name),label('span','v'+plugin.version+' · '+(plugin.active?'已激活':'已停用')));
        if(plugin.description)row.append(label('p',plugin.description));
        if(plugin.active&&plugin.frontendUrl){const link=label('a','打开插件');link.href=plugin.frontendUrl;link.target='_blank';link.rel='noopener';row.append(link)}
        if(admin){const toggle=label('button',plugin.active?'停用':'激活');toggle.type='button';toggle.onclick=async()=>{
          toggle.disabled=true;
          try{await request('/admin/plugins/'+encodeURIComponent(plugin.name),{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({active:!plugin.active})});await refresh();await onchange();status.textContent='插件状态已更新，无需重启。'}catch(error){status.textContent=error.message}finally{toggle.disabled=false}
        };row.append(toggle)}
        list.append(row);
      }
      status.textContent=data.items.length?'':'暂无已安装插件';
    }
    if(admin){
      const note=label('p','需求中心插件 ZIP 是供兼容宿主安装的适配包，不执行上传的 Python。页面可下载需求中心独立源码 ZIP；Windows EXE 与 Linux service Release 由对应平台另行构建。');
      const download=label('button','下载需求中心插件 ZIP（兼容宿主）');download.type='button';
      download.onclick=async()=>{try{const response=await fetch('/api/v1/admin/plugins/requirements-center/package',{headers:{Authorization:'Bearer '+localStorage.getItem('ws_token')}});if(!response.ok)throw Error('插件包下载失败');const url=URL.createObjectURL(await response.blob());const anchor=document.createElement('a');anchor.href=url;anchor.download='requirements-center-plugin.zip';anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}catch(error){status.textContent=error.message}};
      const file=document.createElement('input');file.type='file';file.accept='.zip';file.setAttribute('aria-label','上传核验的需求中心插件包');
      file.onchange=async()=>{if(!file.files[0])return;file.disabled=true;try{const body=new FormData();body.append('file',file.files[0]);await request('/admin/plugins/upload',{method:'POST',body});await refresh();await onchange();status.textContent='需求中心插件已安装'}catch(error){status.textContent=error.message}finally{file.disabled=false;file.value=''}};
      container.append(note,download,file);
    }
    try{await refresh()}catch(error){status.textContent=error.message}
  }
  async function open(){
    const popup=document.createElement('dialog');popup.className='panel';
    const close=label('button','返回');close.type='button';close.onclick=()=>popup.close();
    const container=document.createElement('div');popup.append(close,container);document.body.append(popup);popup.addEventListener('close',()=>popup.remove(),{once:true});popup.showModal();
    const request=async(path,options={})=>{const response=await fetch('/api/v1'+path,{...options,headers:{...options.headers,Authorization:'Bearer '+localStorage.getItem('ws_token')}});const data=await response.json();if(!response.ok)throw Error(data.detail||'请求失败');return data};
    await render(container,request);
  }
  document.addEventListener('click',event=>{if(event.target.closest('#installedPluginsButton'))open().catch(()=>{})});
  window.SWToolPlugins={render,open};
})();
