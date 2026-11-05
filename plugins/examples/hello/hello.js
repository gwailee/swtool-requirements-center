/*
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 上海商江网络科技有限公司版权所有
软工云（swtool.com）开发团队；uqtool@163.com
*/
(async()=>{const result=document.getElementById('result');try{const token=localStorage.getItem('ws_token');const response=await fetch('../api/hello',{headers:{Authorization:'Bearer '+token}});const data=await response.json();if(!response.ok)throw Error(data.detail);result.textContent=data.message+'，你好 '+data.user}catch(error){result.textContent=error.message}})();
