/* First-run setup never contacts a remote origin from the browser. */
window.STUDIO_READY = false;
(() => {
  const node = id => document.getElementById(id);
  let current = null, digest = null, pollTimer = null, pending = false;
  async function request(path, options = {}) {
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `Setup request failed (${response.status})`);
    return data;
  }
  const bytes = n => `${(n / 1e9).toFixed(2)} GB`;
  function render(data) {
    current = data;
    window.STUDIO_READY = Boolean(data.ready);
    window.dispatchEvent(new Event('studio-readiness'));
    const active = ['downloading', 'cancelling'].includes(data.state);
    node('local-state').textContent = active ? 'Local · model setup online' : 'Local · private · offline';
    node('setup-panel').classList.toggle('hidden', data.ready);
    node('setup-blockers').textContent = (data.environment?.blockers || []).join(' ');
    node('setup-size').textContent = `Pinned model: ${bytes(data.total_bytes)} on disk, plus download headroom and space for your audiobooks. No Hugging Face account or token is required.`;
    const labels = {missing:'Voice model not installed', needs_acceptance:'Accept the voice model licence',
      ready:'Voice model ready', downloading:'Downloading or verifying the voice model…',
      cancelling:'Stopping the download…', cancelled:'Download stopped; saved bytes are kept', error:'Model setup needs attention'};
    node('setup-status').textContent = data.error || labels[data.state] || 'Checking the voice model…';
    node('setup-progress').textContent = `${bytes(data.completed_bytes)} of ${bytes(data.total_bytes)} stored locally · no time estimate`;
    node('setup-progress').classList.toggle('hidden', !active && !data.completed_bytes);
    node('setup-cancel').classList.toggle('hidden', !active);
    node('setup-cancel').disabled = pending || data.state === 'cancelling';
    node('setup-accept').disabled = active || pending;
    node('setup-start').textContent = data.state === 'error' ? 'Retry setup' : data.state === 'cancelled' ? 'Resume download' : data.completed_bytes >= data.total_bytes ? 'Accept & verify existing model' : data.completed_bytes ? 'Resume model setup' : 'Download voice model';
    node('setup-start').disabled = active || pending || !digest || !node('setup-accept').checked || Boolean(data.environment?.blockers?.length);
  }
  async function refresh() {
    clearTimeout(pollTimer);
    try {
      render(await request('/api/setup'));
      if (['downloading','cancelling'].includes(current.state)) pollTimer = setTimeout(refresh, 1000);
    } catch (error) {
      window.STUDIO_READY = false;
      window.dispatchEvent(new Event('studio-readiness'));
      node('setup-panel').classList.remove('hidden');
      node('setup-status').textContent = `Could not check setup: ${error.message}. Reload to retry.`;
      node('setup-start').disabled = true;
    }
  }
  node('setup-accept').addEventListener('change', () => { if(current) render(current); });
  node('setup-start').addEventListener('click', async () => {
    if (pending || !digest || !node('setup-accept').checked) return;
    pending = true; if(current) render(current);
    try {
      await request('/api/setup/start', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({accepted:true, license_sha256:digest})});
      pending = false; await refresh();
    } catch (error) { pending = false; await refresh(); node('setup-status').textContent = error.message; }
  });
  node('setup-cancel').addEventListener('click', async () => {
    if(pending) return;
    pending = true; if(current) render(current);
    try { await request('/api/setup/cancel', {method:'POST'}); pending = false; await refresh(); }
    catch(error) { pending = false; await refresh(); node('setup-status').textContent = error.message; }
  });
  window.addEventListener('pagehide', () => clearTimeout(pollTimer));
  (async () => {
    try {
      const legal = await request('/api/setup/license');
      node('setup-license').textContent = legal.text;
      digest = legal.license_sha256;
      await refresh();
    } catch(error) {
      node('setup-panel').classList.remove('hidden');
      node('setup-status').textContent = `Could not load the licence: ${error.message}. Reload to retry.`;
    }
  })();
})();
