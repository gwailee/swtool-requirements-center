/* Bounded attachment queue shared by the hub and legacy ticket forms. */
(() => {
  const MAX_SIZE = 1024 ** 3, completed = new WeakMap();
  function validate(files) {
    if (files.length > 100) throw Error('每批最多选择 100 个附件');
    const large = files.find(file => file.size > MAX_SIZE);
    if (large) throw Error(large.name + '：单个附件不能超过 1 GiB');
  }
  const size = value => (value / 1024 / 1024).toFixed(1) + ' MiB';
  async function upload(send, id, files, commentId, options = {}) {
    files = Array.from(files); validate(files); if (!files.length) return;
    const container = options.container || document.body;
    container.querySelector?.('.attachment-upload-queue')?.remove();
    const panel = document.createElement('div'); panel.className = 'attachment-upload-queue';
    const summary = document.createElement('p'), total = document.createElement('progress'), cancel = document.createElement('button');
    summary.setAttribute('role', 'status'); summary.setAttribute('aria-live', 'polite');
    total.max = files.reduce((sum, file) => sum + file.size, 0) || 1; total.value = 0;
    total.setAttribute('aria-label', '附件总体上传进度'); cancel.type = 'button'; cancel.textContent = '取消上传';
    const controller = new AbortController(); let cancelled = false, received = 0, active = '', completing = false, activeFile = null;
    const abort = () => { cancelled = true; controller.abort(); cancel.disabled = true; };
    cancel.onclick = abort; options.signal?.addEventListener('abort', abort, {once:true});
    if (options.signal?.aborted) abort();
    panel.append(summary, total, cancel);
    const rows = files.map(file => {
      const row = document.createElement('p'); row.textContent = file.name + '（' + size(file.size) + '）等待上传'; panel.append(row); return row;
    });
    container.append(panel);
    const base = '/api/tickets/' + encodeURIComponent(id) + '/attachment-uploads';
    // Scope retry memory to the sender (and therefore its authenticated workspace).
    let byTicket = completed.get(send); if (!byTicket) { byTicket = new Map(); completed.set(send, byTicket); }
    const key = (options.scopeKey || '') + ':' + id + ':' + (commentId || '');
    let done = byTicket.get(key); if (!done) { done = {files:new WeakSet(), pending:new WeakMap()}; byTicket.set(key, done);
      if (byTicket.size > 100) byTicket.delete(byTicket.keys().next().value); }
    const post = (path, data) => { options.assertCurrent?.(); return send(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data), signal:controller.signal}); };
    try {
      for (let index = 0; index < files.length; index++) {
        const file = files[index], row = rows[index]; activeFile = file; completing = false;
        const pending = done.pending.get(file);
        if (pending) {
          active = pending; completing = true;
          await post(active + '/complete', {});
          done.files.add(file); done.pending.delete(file); active = ''; completing = false;
        }
        if (done.files.has(file)) { received += file.size; total.value = received; row.textContent = file.name + '：已上传'; continue; }
        if (cancelled) throw Error('上传已取消');
        summary.textContent = '正在上传 ' + (index + 1) + '/' + files.length + '，剩余 ' + (files.length-index) + ' 个附件';
        const session = await post(base, {name:file.name, mime_type:file.type || 'application/octet-stream', size:file.size, comment_id:commentId || null});
        if (!/^[a-f0-9]{32}$/.test(session.upload_id) || !Number.isInteger(session.chunk_size) || session.chunk_size < 1 || session.chunk_size > 1024 ** 2) throw Error('服务器返回的上传参数无效');
        active = base + '/' + encodeURIComponent(session.upload_id);
        let offset = 0;
        const chunkSize = Math.min(Number(session.chunk_size) || 1024 ** 2, 1024 ** 2);
        while (offset < file.size) {
          if (cancelled) throw Error('上传已取消');
          const chunk = file.slice(offset, offset + chunkSize);
          const contentBase64 = await new Promise((resolve, reject) => {
            const reader = new FileReader(); reader.onerror = () => reject(Error('附件读取失败'));
            reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.readAsDataURL(chunk);
          });
          const result = await post(active + '/chunks', {offset, contentBase64});
          const next = Number(result.received);
          if (next !== offset + chunk.size) throw Error('附件上传位置不匹配，请重试');
          offset = next; total.value = Math.min(received + offset, total.max * 0.99);
          row.textContent = file.name + '：' + size(offset) + '/' + size(file.size) + '（' + Math.floor(offset / (file.size || 1) * 100) + '%）';
          summary.textContent = '总体 ' + Math.floor(total.value / total.max * 100) + '%，剩余 ' + (files.length-index) + ' 个附件';
          options.onProgress?.({file, index, loaded:offset, total:file.size, overallLoaded:total.value, overallTotal:total.max});
        }
        summary.textContent = '分片传输完成，正在保存附件…';
        completing = true; done.pending.set(file, active);
        await post(active + '/complete', {}); done.files.add(file); done.pending.delete(file); received += file.size; active = ''; row.textContent = file.name + '：上传完成';
      }
      summary.textContent = '全部附件上传完成'; total.value = total.max; cancel.remove();
      // Keep successful File identities for repeated form submissions after later failures.
    } catch (error) {
      if (active && !completing) { try { options.assertCurrent?.(); await send(active, {method:'DELETE'}); } catch {} }
      if (activeFile) rows[files.indexOf(activeFile)].textContent = activeFile.name + (completing ? '：等待确认保存，请重试' : '：上传失败，请重试');
      summary.textContent = (cancelled ? '上传已取消' : '上传失败：' + error.message) + '；再次提交可重试，已完成附件会跳过'; cancel.remove();
      throw Error(summary.textContent);
    } finally { options.signal?.removeEventListener('abort', abort); }
  }
  window.SWToolAttachmentUploads = {upload, validate, MAX_SIZE};
})();
