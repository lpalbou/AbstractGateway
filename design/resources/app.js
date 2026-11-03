/* Standalone design prototype. No network calls and no host mutations. */
'use strict';
const $ = id => document.getElementById(id);
const state = { scenario: 'normal', range: 15, chart: 'memory', view: 'models', streaming: true, hiddenSeries: new Set(), sample: 0, cursor: null, lastSample: Date.now(), models: [], caches: [] };
const catalog = [
  { id: 'qwen', name: 'Qwen3.8-27B-oQ4e-mtp', provider: 'hf-gguf', owner: 'Jundot', size: 15.7, kv: 765, context: '32,768', locked: false },
  { id: 'embed', name: 'nomic-embed-text-v1.5', provider: 'huggingface', owner: 'nomic-ai', size: .52, kv: 0, context: '8,192', locked: false },
  { id: 'small', name: 'Qwen3-4B', provider: 'mlx', owner: 'Qwen', size: 2.3, kv: 256, context: '16,384', locked: false }
];
const info = {
  ram: ['System memory', 'RAM used across the whole host, including other applications. “Available” is the operating system’s available_bytes counter, not a guarantee that another model will fit. Existing source: /host/metrics/live → memory.ram.'],
  cpu: ['CPU · proposed telemetry', 'Total CPU busy time and load relative to logical core count. These values are simulated. The current host metrics API needs a CPU sampler before this card can be connected.'],
  gpu: ['GPU utilization', 'Device busy time across the host, from ioreg on macOS or nvidia-smi on supported NVIDIA hosts. This measures activity, not memory fullness. Source: /host/metrics/live → gpu.'],
  disk: ['Storage · proposed telemetry', 'Capacity of the volume containing gateway data. Model downloads may live on another volume and must be shown separately. Disk capacity, throughput and network rates need new host probes.'],
  capacity: ['Capacity, with scopes intact', 'The top bar partitions total RAM into used and available. Accelerator heap is a separate, overlapping counter. It must never be added to RAM use, model weights or process RSS.'],
  heap: ['Accelerator heap', 'This preview uses the screenshot’s process-local Metal counter and its 105.5 GiB limit. Prefer a host-wide heap counter when available and label that scope. Memory-mapped GGUF weights may be absent from the heap. A low heap reading does not mean RAM is free.'],
  history: ['Resource history · proposed collection', 'Hover, tap, or focus this chart and use the arrow keys to inspect a sample. Current APIs provide snapshots. The plotted history is simulated; 1-hour persisted history needs a bounded server-side sampler. Hide individual series through the legend.'],
  models: ['Models and caches', 'Only provider-verified resident models count as loaded. Configured models remain separate. Model weight sizes may be estimates. Model KV and session cache counters may overlap and are never added together.'],
  gateway: ['Workflow controls', 'Pause stops new workflow steps; calls already in flight finish first. The gateway and connected apps remain reachable. Entity loops in other processes have their own controls. Mutations remain admin-only.'],
  weights: ['Estimated model weights', 'The ~ marker means this is an on-disk weight estimate, not a measured allocation and not a promise of how much RAM unloading will recover.'],
  pressure: ['Memory pressure · proposed telemetry', 'Normal / elevated / critical must come from operating-system pressure signals. RAM used percentage alone is insufficient. Swap usage and swap-in rate should be collected alongside pressure.']
};
const colors = ['#ae9af9', '#65d6c1'];
const labels = { memory: ['RAM used', 'Metal heap · process'], compute: ['CPU', 'GPU'], io: ['Disk read', 'Network receive'] };
const details = $('details');
const confirmDialog = $('confirm');
let confirmAction = null, returnFocus = null, tipAnchor = null, tipTimer, toastTimer;

function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 5500); }
function readOnly() { return state.scenario === 'readonly'; }
function totals() { return { weights: state.models.reduce((n, m) => n + m.size, 0), kv: state.models.reduce((n, m) => n + m.kv, 0) }; }
function measuredRam() { return state.scenario === 'pressure' ? 117.8 : 59.4; }
function dialog(title, html) {
  hideTip(); returnFocus = document.activeElement; $('drawer-title').textContent = title; $('drawer-content').innerHTML = html;
  if (!details.open) details.showModal();
  $('close-details').focus();
}
function list(rows) { return `<div class="detail-list">${rows.map(([a,b]) => `<div><span>${a}</span><strong>${b}</strong></div>`).join('')}</div>`; }
function source(text, proposed = false) { return `<span class="source-tag${proposed ? ' proposed' : ''}">${text}</span>`; }
function ask(title, description, action, accept = 'Confirm simulation') {
  hideTip(); $('confirm-title').textContent = title; $('confirm-description').textContent = description;
  $('accept-confirm').textContent = accept; confirmAction = action; confirmDialog.showModal(); $('cancel-confirm').focus();
}
function closeInspector() { details.close(); returnFocus?.focus(); }
function showTip(button) {
  clearTimeout(tipTimer); if (tipAnchor && tipAnchor !== button) hideTip();
  const entry = info[button.dataset.info]; if (!entry) return;
  tipAnchor = button; $('tooltip').innerHTML = `<strong>${entry[0]}</strong>${entry[1]}`;
  $('tooltip').hidden = false; button.setAttribute('aria-describedby', 'tooltip'); button.setAttribute('aria-expanded', 'true');
  const r = button.getBoundingClientRect(), t = $('tooltip').getBoundingClientRect();
  $('tooltip').style.left = `${Math.min(Math.max(10, r.left - 16), window.innerWidth - t.width - 10)}px`;
  $('tooltip').style.top = `${r.bottom + t.height + 18 > window.innerHeight ? Math.max(10, r.top - t.height - 12) : r.bottom + 12}px`;
}
function hideTip() { clearTimeout(tipTimer); $('tooltip').hidden = true; tipAnchor?.removeAttribute('aria-describedby'); tipAnchor?.setAttribute('aria-expanded', 'false'); tipAnchor = null; }
document.addEventListener('pointerover', e => { const button = e.target.closest('.info'); if (button) showTip(button); });
document.addEventListener('pointerout', e => { if (e.target.closest('.info') && !e.relatedTarget?.closest('.info')) tipTimer = setTimeout(hideTip, 180); });
document.addEventListener('focusin', e => { if (e.target.matches('.info')) showTip(e.target); });
document.addEventListener('focusout', e => { if (e.target.matches('.info')) hideTip(); });
document.addEventListener('click', e => { const button = e.target.closest('.info'); if (button) { e.stopPropagation(); showTip(button); } });
$('tooltip').addEventListener('pointerenter', () => clearTimeout(tipTimer));
$('tooltip').addEventListener('pointerleave', () => tipTimer = setTimeout(hideTip, 180));
document.addEventListener('keydown', e => { if (e.key === 'Escape') hideTip(); });
window.addEventListener('scroll', hideTip, true);
window.addEventListener('resize', () => { hideTip(); renderChart(); });

function series(group = state.chart) {
  const empty = !state.models.length, pressure = state.scenario === 'pressure';
  const base = group === 'memory' ? [measuredRam(), pressure ? 72 : 16.4] : group === 'compute' ? [18, 6] : [24, 2.8];
  return base.map((b, line) => Array.from({ length: 61 }, (_, i) => {
    if ((group === 'memory' || group === 'compute') && line === 1 && state.scenario === 'unavailable') return null;
    const ago = state.range * (60-i)/60, t = 60-ago;
    const wave = (Math.sin(t * 2.6 + line * 3 + state.sample * .3) + Math.sin(t * 6.8 + line)) * (group === 'memory' ? .7 : group === 'compute' ? 4 : 3);
    const lift = group === 'memory' && !empty ? -Math.min(1,Math.max(0,ago-7)) * (line ? 13 : 15) : 0;
    return i === 60 ? b : Math.max(0, b + wave + lift + (group !== 'memory' && ago > 5 && ago < 8 ? Math.sin((ago-5)/3*Math.PI) * (line ? 33 : 18) : 0));
  }));
}
function renderSparks() {
  const all = [series('memory')[0], series('compute')[0], series('compute')[1]];
  ['ram','cpu','gpu'].forEach((key, k) => {
    const points = all[k], max = k === 0 ? 128 : 100, color = [colors[0], '#76b6ff', colors[1]][k];
    if (points.every(v => v === null)) { $(`spark-${key}`).innerHTML = ''; return; }
    const p = points.map((v,i) => `${i*5},${28-v/max*26}`).join(' ');
    $(`spark-${key}`).innerHTML = `<svg viewBox="0 0 300 30" preserveAspectRatio="none" aria-hidden="true"><path d="M0,30 L${p.replaceAll(' ', ' L')} L300,30 Z" fill="${color}" opacity=".10"/><polyline points="${p}" fill="none" stroke="${color}" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>`;
  });
}
function renderChart() {
  const chart = $('chart'), width = Math.max(280, chart.clientWidth), height = chart.clientHeight, left = 32, right = width - 15, top = 15, bottom = height - 27;
  const data = series(), max = state.chart === 'memory' ? 128 : state.chart === 'compute' ? 100 : 80;
  const x = i => left + (right-left)*i/60, y = v => bottom - v/max*(bottom-top);
  $('chart-unit').textContent = state.chart === 'memory' ? 'GiB' : state.chart === 'compute' ? '%' : 'MiB/s';
  let svg = `<svg viewBox="0 0 ${width} ${height}" aria-hidden="true"><defs>${colors.map((c,j) => `<linearGradient id="area-${j}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${c}" stop-opacity=".14"/><stop offset="1" stop-color="${c}" stop-opacity="0"/></linearGradient>`).join('')}</defs>`;
  for (let i = 0; i < 5; i++) { const v = max*i/4; svg += `<line class="grid-line" x1="${left}" x2="${right}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${left-8}" y="${y(v)+3}" text-anchor="end">${v}</text>`; }
  if (state.chart === 'memory' && state.range >= 8 && state.models.length) { const eventX = x(60-8/state.range*60); svg += `<line x1="${eventX}" x2="${eventX}" y1="${top}" y2="${bottom}" stroke="var(--brand)" stroke-opacity=".5" stroke-dasharray="3 5"/>`; }
  data.forEach((points,j) => { if (state.hiddenSeries.has(j) || points.every(v => v === null)) return; const line = points.map((v,i) => `${x(i)},${y(v)}`).join(' '); svg += `<path d="M${x(0)},${bottom} L${line.replaceAll(' ', ' L')} L${x(60)},${bottom} Z" fill="url(#area-${j})"/><polyline points="${line}" stroke="${colors[j]}" fill="none" stroke-width="2" stroke-linejoin="round"/><circle cx="${x(60)}" cy="${y(points[60])}" r="3" fill="${colors[j]}"/>`; });
  [0,15,30,45,60].forEach((i,j) => { const ago = state.range*(60-i)/60; svg += `<text class="axis" x="${x(i)}" y="${height-7}" text-anchor="${j === 0 ? 'start' : j === 4 ? 'end' : 'middle'}">${ago ? `−${Number(ago.toFixed(1))}m` : 'Now'}</text>`; });
  if (state.cursor !== null) svg += `<line x1="${x(state.cursor)}" x2="${x(state.cursor)}" y1="${top}" y2="${bottom}" stroke="var(--muted)" stroke-dasharray="3 3"/>`;
  svg += '</svg>';
  if (state.cursor !== null) {
    const ago = state.range*(60-state.cursor)/60;
    const rows = data.map((s,j) => state.hiddenSeries.has(j) ? '' : `<div style="color:${colors[j]}">${labels[state.chart][j]}: ${s[state.cursor] === null ? 'Unavailable' : s[state.cursor].toFixed(1) + (state.chart === 'memory' ? ' GiB' : state.chart === 'compute' ? '%' : ' MiB/s')}</div>`).join('');
    svg += `<div class="chart-readout"><strong>${ago ? `${ago.toFixed(1)} minutes ago` : 'Latest sample'} · simulated</strong>${rows}</div>`;
    chart.setAttribute('aria-label', `Resource history. Sample ${state.cursor + 1} of 61. ${data.map((s,j) => `${labels[state.chart][j]} ${s[state.cursor] === null ? 'unavailable' : s[state.cursor].toFixed(1)}`).join('. ')}. Use arrow keys.`);
  }
  chart.innerHTML = svg;
  $('legend').innerHTML = labels[state.chart].map((label,j) => `<button data-series="${j}" aria-pressed="${!state.hiddenSeries.has(j)}"><i class="swatch" style="background:${colors[j]}"></i>${label}${j === 1 && state.scenario === 'unavailable' && state.chart !== 'io' ? ' · unavailable' : ''}</button>`).join('');
  const hasEvent = state.chart === 'memory' && state.range >= 8 && state.models.length;
  $('chart-caption').innerHTML = state.chart === 'memory' ? hasEvent ? `<span class="event-marker"></span> Model loaded <span class="muted">· 8m ago · simulated</span>` : '<span class="muted">No model events in this window · simulated</span>' : `<span class="muted">${state.chart === 'compute' ? 'CPU collection proposed · GPU probe exists' : 'Disk and network collection proposed'}</span>`;
  $('inspect-event').hidden = !hasEvent;
}
$('chart').addEventListener('pointermove', e => { const r = $('chart').getBoundingClientRect(); state.cursor = Math.max(0,Math.min(60,Math.round((e.clientX-r.left-32)/(r.width-47)*60))); renderChart(); });
$('chart').addEventListener('pointerleave', () => { state.cursor = null; renderChart(); });
$('chart').addEventListener('keydown', e => { if (!['ArrowLeft','ArrowRight','Home','End'].includes(e.key)) return; e.preventDefault(); state.cursor = e.key === 'Home' ? 0 : e.key === 'End' ? 60 : Math.max(0,Math.min(60,(state.cursor ?? 59)+(e.key === 'ArrowRight' ? 1 : -1))); renderChart(); });
$('legend').addEventListener('click', e => { const button = e.target.closest('[data-series]'); if (!button) return; const n = Number(button.dataset.series); state.hiddenSeries.has(n) ? state.hiddenSeries.delete(n) : state.hiddenSeries.add(n); renderChart(); $('legend').querySelector(`[data-series="${n}"]`).focus(); });
$('ranges').addEventListener('click', e => { const button = e.target.closest('[data-range]'); if (!button) return; state.range = Number(button.dataset.range); state.cursor = null; $('ranges').querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', b === button)); renderChart(); });
$('chart-tabs').addEventListener('click', e => { const button = e.target.closest('[data-chart]'); if (!button) return; state.chart = button.dataset.chart; state.hiddenSeries.clear(); state.cursor = null; $('chart-tabs').querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', b === button)); renderChart(); });

function renderModels() {
  $('model-count').textContent = state.models.length; $('cache-count').textContent = state.caches.length;
  $('configured-label').hidden = state.view !== 'models';
  const total = totals(); $('work-summary').innerHTML = `~${total.weights.toFixed(1)} GiB weights <span>·</span> ${total.kv} MiB model KV`;
  if (state.view === 'caches') {
    $('work-content').innerHTML = state.caches.length ? state.caches.map(c => `<div class="cache-row"><span class="model-icon">⌘</span><div><div class="model-title">${c.name}</div><div class="model-sub">Qwen3.8 · session KV · ${c.tokens} tokens</div></div><span class="cache-size">${c.size} MiB</span><button class="button mutation" data-clear="${c.id}">Clear…</button></div>`).join('') : '<div class="empty-state"><strong>No session caches</strong>Caches appear here when a session creates one.</div>';
    return;
  }
  const known = $('show-configured').checked ? catalog.filter(m => !state.models.some(r => r.id === m.id)) : [];
  let html = state.models.map(m => `<div class="model-row"><span class="model-icon">${m.id === 'embed' ? '⊞' : '✧'}</span><div><button class="model-title" data-model="${m.id}">${m.name} ↗</button><div class="model-sub"><span class="status good"><span class="dot"></span> Loaded</span><span>${m.provider}</span><span>${m.id === 'embed' ? 'Embeddings' : 'Text generation'}</span></div><div class="model-meta"><span>Weights<strong>~${m.size} GiB <button class="info" data-info="weights" aria-label="About estimated model weights">i</button></strong></span><span>KV<strong>${m.kv} MiB</strong></span><span>Context<strong>${m.context}</strong></span></div></div><div class="row-actions"><button class="icon-button mutation" data-lock="${m.id}" aria-label="${m.locked ? 'Unlock' : 'Keep'} ${m.name}${m.locked ? '' : ' in memory'}" title="${m.locked ? 'Unlock model' : 'Keep model loaded'}">${m.locked ? '◆' : '◇'}</button><button class="button mutation" data-unload="${m.id}" ${m.locked ? 'disabled title="Unlock this model before unloading"' : ''}>Unload…</button></div></div>`).join('');
  if (!state.models.length) html += '<div class="empty-state"><span class="model-icon" style="margin:0 auto 10px">✧</span><strong>No models loaded</strong>Load a model to prepare it for its next request.</div>';
  html += known.map(m => `<div class="model-row cached"><span class="model-icon">✧</span><div><button class="model-title" data-model="${m.id}">${m.name} ↗</button><div class="model-sub">Configured · not in memory · ${m.provider}</div></div><button class="button mutation" data-load="${m.id}">Load…</button></div>`).join('');
  if (state.models.length) html += `<div class="memory-row"><span>Weights / host RAM <button class="info" data-info="weights" aria-label="About weight size ratio">i</button></span><div class="thin-track"><span style="width:${Math.min(100,total.weights/128*100)}%"></span></div><strong>~${(total.weights/128*100).toFixed(1)}%</strong></div>`;
  $('work-content').innerHTML = html;
}
$('work-tabs').addEventListener('click', e => { const b = e.target.closest('[data-view]'); if (!b) return; state.view = b.dataset.view; $('work-tabs').querySelectorAll('[data-view]').forEach(t => t.setAttribute('aria-pressed', t === b)); renderModels(); });
$('show-configured').addEventListener('change', renderModels);
$('work-content').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b || b.classList.contains('info')) return;
  if (b.dataset.model) { modelDetail(b.dataset.model); return; }
  if (readOnly()) return;
  if (b.dataset.lock) { const m = state.models.find(m => m.id === b.dataset.lock); m.locked = !m.locked; renderModels(); toast(`${m.locked ? 'Model kept in memory' : 'Model unlocked'} · simulation`); $('work-content').querySelector(`[data-lock="${m.id}"]`)?.focus(); }
  if (b.dataset.unload) { const m = state.models.find(m => m.id === b.dataset.unload); if (!m || m.locked) return; ask('Unload this model?', `${m.name} will leave provider memory. Its next request must load it again. The weight estimate is ~${m.size} GiB; actual freed memory can differ. This preview changes only simulated model state.`, () => { state.models = state.models.filter(r => r.id !== m.id); renderModels(); toast('Model unloaded · simulated host counters remain a separate snapshot'); $('load-model').focus(); }, 'Unload model'); }
  if (b.dataset.clear) { const c = state.caches.find(c => c.id === b.dataset.clear); ask('Clear this session cache?', `${c.name}: ${c.size} MiB of reported cache. Conversation history stays; the next turn must encode the prompt again. Active calls may still hold buffers.`, () => { state.caches = state.caches.filter(r => r.id !== c.id); renderModels(); toast('Session cache cleared · simulation'); $('work-tabs').querySelector('[data-view="caches"]').focus(); }, 'Clear cache'); }
  if (b.dataset.load) loadModel(b.dataset.load);
});
function modelDetail(id) {
  const m = state.models.find(m => m.id === id) || catalog.find(m => m.id === id), resident = state.models.some(m => m.id === id);
  dialog(m.name, `${source('Existing residency API')}<p>Model details stay close to the resource they describe. Residency is verified by the provider; routing defaults do not imply a loaded model.</p>${list([['Provider',m.provider],['Model owner',m.owner],['Residency',resident ? 'Loaded' : 'Configured · not in memory'],['Weights',`~${m.size} GiB · estimated`],['Model KV',resident ? `${m.kv} MiB` : 'Not resident'],['Context',m.context],['Keep in memory',resident && m.locked ? 'Locked' : 'Unlocked']])}<h3>Measurement</h3><p>Weight estimates, process memory and accelerator heap describe overlapping allocations. They are separate counters. Unload success should be verified against provider residency and accelerator allocation, rather than RSS alone.</p>`);
}
function loadModel(id) {
  if (readOnly()) return;
  const m = catalog.find(m => m.id === id);
  if (!m || state.models.some(r => r.id === id)) { toast('This model is already loaded.'); return; }
  ask('Load model into memory?', `${m.name} has ~${m.size} GiB of estimated weights. Context and runtime overhead require additional memory. A production version should offer the existing context-fit estimate before loading.`, () => { state.models.push({...m}); renderModels(); if (details.open) details.close(); toast(`${m.name} loaded · simulation`); }, 'Load model');
}
function openLoad() {
  if (readOnly()) return;
  const available = catalog.filter(m => !state.models.some(r => r.id === m.id));
  dialog('Load a model', `${source('Existing model load API')}<p>Prepare a configured model for its next request. Weight sizes below are estimates. Context-fit estimates remain advisory.</p>${available.length ? `<label for="model-picker">Configured model</label><select id="model-picker">${available.map(m => `<option value="${m.id}">${m.name} · ~${m.size} GiB</option>`).join('')}</select><p>Available system memory: ${ (128-measuredRam()).toFixed(1) } GiB. This is not an allocation guarantee.</p><button class="button primary full" id="confirm-load">Review load…</button>` : '<p>All configured models are already loaded.</p>'}`);
  $('confirm-load')?.addEventListener('click', () => loadModel($('model-picker').value));
}
function memoryDetail() {
  const t = totals(); dialog('Memory counters', `${source('Existing snapshot API')}<p>Independent views of overlapping allocations. These counters must not be summed into a memory breakdown.</p>${list([['System RAM',`${measuredRam()} / 128 GiB · host`],['Accelerator heap',state.scenario === 'unavailable' ? 'Unavailable · probe failed' : `${state.scenario === 'pressure' ? 72 : 16.4} / 105.5 GiB · process`],['Gateway process RSS','16.8 GiB · process'],['Model weights',`~${t.weights.toFixed(1)} GiB · estimate`],['Model KV caches',`${t.kv} MiB · model stores`],['Session cache bytes',`${state.caches.reduce((s,c)=>s+c.size,0)} MiB · session inventory`]])}<p>GGUF memory-mapped weights may be missing from accelerator heap. Session caches may overlap model KV. A cache clear or unload can free allocations without an immediate RSS drop.</p>`);
}
function metricDetail(key) {
  if (key === 'ram') return memoryDetail();
  const [title, text] = info[key];
  dialog(title, `${source(key === 'gpu' ? 'Existing GPU API' : 'New collector required',key !== 'gpu')}<p>${text}</p>${key === 'gpu' ? list([['Probe',state.scenario === 'unavailable' ? 'Unavailable · ioreg timed out' : 'ioreg'],['GPU utilization',state.scenario === 'unavailable' ? 'Unavailable' : '6% · host-wide'],['Heap scope','This process'],['Last value',state.scenario === 'unavailable' ? '6% · stale, 45s ago' : '6% · simulated sample']]) : key === 'cpu' ? list([['CPU utilization','18% · simulated'],['Load average','2.1 · simulated'],['Logical cores','16 · simulated'],['Proposed additions','Process CPU · loop lag · load']]) : list([['Data volume capacity','712 / 1,024 GiB · simulated'],['Disk read','24 MiB/s · simulated'],['Network receive','2.8 MiB/s · simulated'],['Proposed additions','I/O latency · download volume']])}`);
}
function openIncident() { dialog('Last gateway restart', `${source('Existing runner incident API')}<p>Recovered after a hang on October 28 at 17:01. The event loop and watchdog thread were blocked.</p>${list([['State','Recovered'],['Cause','Native code held the interpreter lock'],['Scope','Previous gateway process'],['Evidence','Incident JSON and thread dump']])}<h3>Proposed diagnostic actions</h3><p>The production inspector should show the incident timestamp, blocked duration, source frame, and dump path returned in <code>/host/runner → last_hang</code>. Authorized access to the stack dump needs a verified read or download route.</p>`); }
function openGateway() {
  dialog('Manage gateway', `${source('Existing control APIs')}<p>These operations affect this gateway. Restart and shutdown affect connected clients; pause applies to the workflow runner.</p>${list([['Workflow runner',state.paused ? 'Paused' : 'Running'],['Start at login','On'],['Desktop icon','Visible'],['Version','0.18.0']])}<div style="display:flex;gap:8px;flex-wrap:wrap"><button class="button mutation" id="restart">Restart gateway…</button><button class="button danger mutation" id="shutdown">Quit gateway…</button></div><h3>Settings</h3><p>Update checks, start-at-login settings and desktop integration belong here, outside the monitoring overview. Each operation needs its existing capability check and read-after-write verification.</p>`);
  $('restart').disabled = readOnly(); $('shutdown').disabled = readOnly();
  $('restart').addEventListener('click', () => ask('Restart gateway?', 'Connected clients will briefly disconnect. Active calls may be interrupted. This demonstration only updates simulated uptime.', () => { $('uptime').textContent = '0m'; toast('Gateway restarted · simulation'); }, 'Restart gateway'));
  $('shutdown').addEventListener('click', () => ask('Quit gateway?', 'The console and connected clients will lose access until the gateway is started again. This demonstration only changes the gateway badge.', () => { $('gateway-badge').textContent = 'Gateway stopped · simulation'; $('runner-state').textContent = 'Stopped'; $('pause-workflows').disabled = true; toast('Gateway stopped · simulation. Reset preview to restore.'); }, 'Quit gateway'));
}
function openCoverage() {
  dialog('Data sources & coverage', `<p>All values in this preview are simulated. Existing endpoints were inspected in the local source on October 30, 2026. The running gateway was not queried.</p><h3>Available now</h3>${list([['RAM / accelerator / GPU','GET /host/metrics/live'],['Models / caches / counters','GET /host/state'],['Runner / previous hang','GET /host/runner'],['Host-wide run inventory','GET /host/runs · admin only'],['Load / unload / keep loaded','POST /models/{load,unload,lock,unlock}'],['Clear session cache','POST /sessions/{id}/prompt_cache/clear_all'],['Advisory context fit','GET /models/context_estimate']])}<h3>Requires instrumentation</h3>${list([['Host','CPU · pressure · swap · uptime'],['Storage / transport','Volume capacity · I/O · network'],['Serving performance','Queue depth · TTFT · tokens/s · p95'],['Service health','Loop lag · error rate · probe freshness'],['History','Bounded server sampler · range query']])}<p>Path prefix: <code>/api/gateway</code>. No new API paths are claimed by this proposal. “New” labels identify proposed metrics; all charts and actions remain local simulations.</p>`);
}
$('load-model').addEventListener('click', openLoad);
$('allocation').addEventListener('click', memoryDetail);
$('coverage').addEventListener('click', openCoverage);
$('incident').addEventListener('click', openIncident);
$('inspect-event').addEventListener('click', () => dialog('Model load event', `${source('Proposed history annotation',true)}<p>Qwen3.8 was loaded during this simulated window. An event overlay helps correlate a memory step with an operator action.</p>${list([['Event','Model loaded'],['Weight estimate','~15.7 GiB'],['Attribution','Simulated · not observed'],['History range',`${state.range} minutes`]])}<p>A production event must carry an authoritative timestamp, action outcome and model reference. Avoid interpreting coincident changes as proof of causation.</p>`));
$('gateway-settings').addEventListener('click', openGateway);
$('pause-workflows').addEventListener('click', () => {
  if (readOnly()) return;
  ask(state.paused ? 'Resume workflow steps?' : 'Pause new workflow steps?', state.paused ? 'The workflow runner can start new steps again. This is a simulation.' : 'Calls already in flight finish first. No new workflow steps start until resumed. The console stays reachable; entity loops in other processes are unaffected.', () => { state.paused = !state.paused; $('runner-state').textContent = state.paused ? 'Paused' : 'Running'; $('pause-workflows').textContent = state.paused ? '▷ Resume workflows' : 'Ⅱ Pause workflows'; toast(state.paused ? 'Workflow steps paused · simulation' : 'Workflows resumed · simulation'); }, state.paused ? 'Resume workflows' : 'Pause workflows');
});
$('close-details').addEventListener('click', closeInspector);
details.addEventListener('click', e => { if (e.target === details && e.clientX < details.getBoundingClientRect().left) closeInspector(); });
$('cancel-confirm').addEventListener('click', () => { confirmDialog.close(); confirmAction = null; });
$('accept-confirm').addEventListener('click', () => { const action = confirmAction; confirmAction = null; confirmDialog.close(); action?.(); });
confirmDialog.addEventListener('cancel', () => confirmAction = null);
$('theme').addEventListener('click', () => { document.body.classList.toggle('light'); $('theme').setAttribute('aria-label',`Switch to ${document.body.classList.contains('light') ? 'dark' : 'light'} theme`); });
$('stream').addEventListener('click', () => { state.streaming = !state.streaming; $('stream').textContent = state.streaming ? 'Ⅱ Pause preview' : '▷ Resume preview'; $('sample-status').textContent = state.streaming ? 'Sampled every 5s · simulated' : 'Preview paused · values frozen'; });
document.querySelectorAll('.metric').forEach(card => {
  const label = card.querySelector('.metric-head').firstChild, trigger = document.createElement('button');
  trigger.className = 'metric-trigger'; trigger.textContent = label.textContent; trigger.setAttribute('aria-label',`Inspect ${label.textContent.trim()}`); label.replaceWith(trigger);
  trigger.addEventListener('click', () => metricDetail(card.dataset.detail));
  card.addEventListener('click', e => { if (!e.target.closest('button')) metricDetail(card.dataset.detail); });
});
function reset() {
  state.scenario = $('scenario').value; state.models = state.scenario === 'empty' ? [] : [{...catalog[0]}];
  state.caches = state.scenario === 'empty' ? [] : [{id:'research',name:'Research assistant',tokens:'12,480',size:286},{id:'notes',name:'SmartNote ingestion',tokens:'6,400',size:148},{id:'chat',name:'Quick conversation',tokens:'2,560',size:58}];
  state.paused = false; state.lastSample = Date.now(); state.sample = 0;
  document.body.classList.toggle('scenario-readonly', readOnly());
  const pressure = state.scenario === 'pressure', unavailable = state.scenario === 'unavailable', ram = measuredRam(), free = (128-ram).toFixed(1);
  $('ram-value').textContent = ram.toFixed(1); $('ram-summary').textContent = `${(ram/128*100).toFixed(1)}% used`; $('ram-available').textContent = `${free} GiB available`;
  $('capacity-value').textContent = free; $('capacity-used').textContent = `Used ${ram} GiB`; $('capacity-fill').style.width = `${ram/128*100}%`;
  $('capacity-bar').setAttribute('aria-label', `${ram} GiB used, ${free} GiB available`);
  $('gpu-value').textContent = unavailable ? '—' : '6'; $('gpu-summary').textContent = unavailable ? 'Probe unavailable · retrying' : 'Host-wide · ioreg'; $('gpu-memory').textContent = unavailable ? 'Last: 6% · 45s ago' : `${pressure ? '72.0' : '16.4'} GiB heap`;
  $('heap-value').innerHTML = unavailable ? 'Unavailable' : `${pressure ? '72.0' : '16.4'} <small>/ 105.5 GiB</small>`;
  $('heap-fill').style.width = unavailable ? '0%' : `${(pressure?72:16.4)/105.5*100}%`;
  $('heap-scope').textContent = unavailable ? 'Last value: 16.4 GiB · 45s ago · stale' : 'This process · overlaps unified RAM';
  $('pressure-value').textContent = pressure ? 'Elevated' : 'Normal'; $('pressure-value').style.color = pressure ? 'var(--warn)' : 'var(--teal)'; $('swap-value').textContent = pressure ? '6.2 GiB' : '0 B';
  $('overall').innerHTML = `<span class="dot" style="background:${pressure||unavailable ? 'var(--warn)' : 'var(--teal)'}"></span>${pressure ? 'Memory pressure elevated' : unavailable ? 'Partial visibility · GPU unavailable' : 'Plenty of headroom'}`;
  $('overall').style.color = pressure||unavailable ? 'var(--warn)' : 'var(--teal)';
  const notice = $('notice'); notice.hidden = !pressure && !unavailable && !readOnly();
  notice.innerHTML = pressure ? 'Memory pressure is elevated. Review loaded models and caches before loading more.<button class="text-button" id="notice-action">Review memory ↗</button>' : unavailable ? 'GPU probe timed out. Current GPU values are unavailable; the last sample is marked stale.<button class="text-button" id="notice-action">Inspect probe ↗</button>' : readOnly() ? 'Read-only preview. Monitoring is available; host and model operations require an administrator.' : '';
  $('notice-action')?.addEventListener('click', () => unavailable ? metricDetail('gpu') : memoryDetail());
  $('gateway-badge').innerHTML = '<span class="dot"></span> Gateway running'; $('runner-state').textContent = 'Running'; $('uptime').textContent = '31h'; $('pause-workflows').textContent = 'Ⅱ Pause workflows'; $('pause-workflows').disabled = false;
  renderModels(); renderSparks(); renderChart();
}
$('scenario').addEventListener('change', reset);
$('refresh').addEventListener('click', () => { reset(); toast('Preview reset · all actions and values are simulated'); });
const groups = [['ACCOUNTS',[['◔','Accounts']]],['WORK',[['⑂','Workflows'],['ϟ','Triggers'],['✦','Skills & MCP'],['◎','Runtimes'],['▣','Apps']]],['MODELS',[['◇','Providers'],['⇄','OpenAI API'],['▤','Models'],['✧','Multimodal']]],['SYSTEM',[['▦','Resources'],['▷','Sandbox'],['⌘','Network']]]];
$('navigation').innerHTML = groups.map(([name,items]) => `<div class="nav-group">${name}</div>${items.map(([icon,label]) => `<div class="nav-item${label === 'Resources' ? ' active' : ''}"${label === 'Resources' ? ' aria-current="page"' : ' title="Navigation context only in this design preview"'}><span class="nav-icon" aria-hidden="true">${icon}</span>${label}</div>`).join('')}`).join('');
reset();
setInterval(() => {
  if (!state.streaming || document.visibilityState !== 'visible' || details.open || confirmDialog.open) return;
  state.sample++; state.lastSample = Date.now(); renderSparks();
  if (state.cursor === null && document.activeElement !== $('chart') && !document.activeElement?.closest('#legend')) renderChart();
}, 5000);
