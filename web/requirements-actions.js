/*
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 上海商江网络科技有限公司版权所有
软工云（swtool.com）开发团队；uqtool@163.com
*/
/* Shared host/standalone requirement context menu; authorization stays server-side. */
(()=>{
  let menu=null,origin=null;
  function close(restore=false){menu?.remove();menu=null;if(restore)origin?.focus();origin=null}
  function bind(node,{onAction,label='撤回需求'}){
    node.tabIndex=0;
    function show(event){
      event.preventDefault();close();origin=node;
      menu=document.createElement('div');menu.className='requirement-context-menu';menu.setAttribute('role','menu');
      const action=document.createElement('button');action.type='button';action.textContent=label;action.setAttribute('role','menuitem');
      action.onclick=()=>{close(true);onAction()};menu.append(action);document.body.append(menu);
      const rect=node.getBoundingClientRect(),x=event.clientX||rect.left,y=event.clientY||rect.bottom;
      menu.style.left=Math.max(8,Math.min(x,innerWidth-menu.offsetWidth-8))+'px';menu.style.top=Math.max(8,Math.min(y,innerHeight-menu.offsetHeight-8))+'px';action.focus();
    }
    node.addEventListener('contextmenu',show);
    node.addEventListener('keydown',event=>{if(event.key==='ContextMenu'||event.shiftKey&&event.key==='F10')show(event)});
  }
  document.addEventListener('pointerdown',event=>{if(menu&&!menu.contains(event.target))close()});
  document.addEventListener('keydown',event=>{if(event.key==='Escape')close(true)});
  addEventListener('scroll',()=>close(),true);addEventListener('blur',()=>close());
  window.SWToolRequirementActions={bind,close};
})();
