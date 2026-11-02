"""Resources content only. The shell, top-right widgets and themes stay AbstractUIC's.

Uses the console's existing snapshot poll, mutation handlers, bindAccountModal
(kit bindModal), and data-af-tip (kit tooltips). No synthetic readings or new poll.
"""

RESOURCES_HTML = r"""
<div id="resources-dashboard" class="resources-dashboard">
  <div class="res-heading"><div><span class="res-eyebrow">HOST OVERVIEW</span><h2 id="res-host-name">Host resources</h2><span id="res-topology" class="state-pill">Memory topology unknown</span></div><div id="res-actions" class="actions"><span id="res-freshness" class="muted" role="status">Waiting for a sample</span></div></div>
  <div id="res-messages"></div>
  <div id="res-cards" class="res-cards" aria-label="Current host resources"></div>
  <div class="res-visuals">
    <div class="res-chart-panel"><div class="res-panel-head"><h3>Resource activity</h3><div class="res-ranges" aria-label="History range"><button type="button" class="secondary" data-res-range="5" aria-pressed="false">5m</button><button type="button" class="secondary" data-res-range="15" aria-pressed="true">15m</button><button type="button" class="secondary" data-res-range="60" aria-pressed="false">1h</button></div></div><div class="res-chart-tabs" aria-label="Metric group"><button type="button" class="secondary" data-res-chart="memory" aria-pressed="true">Memory</button><button type="button" class="secondary" data-res-chart="compute" aria-pressed="false">Compute</button><button type="button" class="secondary" data-res-chart="io" aria-pressed="false">Disk &amp; network</button><button type="button" class="res-info secondary" aria-label="About resource history" data-af-tip="Real samples collected since this page opened. The selected range is a viewing window, not retained server history. Unknown readings and missed intervals leave gaps. Use Left/Right/Home/End on the chart to inspect samples.">i</button></div><div id="res-legend" class="res-legend"></div><div id="res-chart" class="res-chart" role="group" tabindex="0" aria-label="Resource history. Use arrow keys to inspect samples."></div><p id="res-chart-caption" class="res-chart-caption muted">Collecting real samples…</p></div>
    <div class="res-capacity-panel"><div class="res-panel-head"><h3 id="res-capacity-title">Memory capacity</h3><button type="button" class="res-info secondary" aria-label="About memory capacity" data-af-tip="System RAM and GPU memory retain their own scopes. Unified accelerator allocations overlap RAM. Dedicated VRAM is a separate pool. Estimated weights, KV caches and process RSS may overlap these counters and must not be added to them.">i</button></div><div id="res-capacity"></div><button type="button" class="secondary res-inspect" data-res-inspect="memory">Inspect memory counters ↗</button></div>
  </div>
</div>
"""

RESOURCES_MODAL_HTML = r"""
<div id="resources-inspector-backdrop" class="af-modal-backdrop" hidden>
  <div class="af-modal af-modal--wide res-inspector" role="dialog" aria-modal="true" aria-labelledby="resources-inspector-title">
    <div class="af-modal__header"><h2 id="resources-inspector-title" class="af-modal__title">Resource details</h2><button id="resources-inspector-close" type="button" class="af-modal__close" aria-label="Close resource inspector" data-af-tip="Close">×</button></div>
    <div class="af-modal__body">
      <div id="res-memory-details" hidden>
        <div class="res-mem-mode"><span id="res-mem-kind" class="state-pill"></span><button type="button" class="secondary res-info" aria-label="About memory layout" data-af-tip="Unified memory is shared by the CPU and GPU. Dedicated VRAM is a separate pool. The allocations below can overlap: do not add them together.">i</button></div>
        <div class="res-mem-pools">
          <article class="res-mem-pool"><div class="res-mem-label"><span>Available RAM</span><button id="res-mem-ram-info" type="button" class="secondary res-info" aria-label="About available RAM">i</button></div><strong id="res-mem-ram-value" class="res-mem-value">—</strong><div id="res-mem-ram-bar" class="res-mem-track" role="img"><span></span></div><span id="res-mem-ram-caption" class="res-mem-caption"></span></article>
          <article class="res-mem-pool res-mem-gpu"><div class="res-mem-label"><span id="res-mem-gpu-label">GPU memory</span><button id="res-mem-gpu-info" type="button" class="secondary res-info" aria-label="About GPU memory">i</button></div><strong id="res-mem-gpu-value" class="res-mem-value">—</strong><div id="res-mem-gpu-bar" class="res-mem-track" role="img"><span></span></div><div class="res-mem-caption"><span id="res-mem-gpu-caption"></span><span id="res-mem-gpu-scope" class="res-mem-badge"></span></div></article>
        </div>
        <div class="res-mem-detail-head"><span>Allocations</span><span class="res-mem-badge">May overlap</span></div>
        <div class="res-mem-facts">
          <div><div class="res-mem-label"><span>Model weights</span><button id="res-mem-weights-info" type="button" class="secondary res-info" aria-label="About model weights">i</button></div><strong id="res-mem-weights-value">—</strong><span id="res-mem-weights-note" class="res-mem-fact-note" hidden>Partial</span></div>
          <div><div class="res-mem-label"><span>Model cache</span><button id="res-mem-cache-info" type="button" class="secondary res-info" aria-label="About model cache">i</button></div><strong id="res-mem-cache-value">—</strong></div>
          <div><div class="res-mem-label"><span>Process memory</span><button id="res-mem-process-info" type="button" class="secondary res-info" aria-label="About process memory" data-af-tip="Resident process memory (RSS), including mapped model files. GPU buffers and model weights may overlap this counter. It is not an extra allocation to add to the other values.">i</button></div><strong id="res-mem-process-value">—</strong></div>
        </div>
        <div class="res-mem-footer"><span id="res-mem-model-count"></span><span id="res-mem-session-count" tabindex="0" data-af-tip="Session KV caches are a separate inventory and may overlap model cache bytes. Clearing a cache requires re-encoding on the next turn; it does not delete conversation history."></span></div>
      </div>
      <div id="res-gateway-details" hidden><p class="muted">Pause controls workflow steps. Restart and quit affect connected clients.</p><div id="res-gateway-legacy"></div></div>
      <div id="res-system-details" hidden></div>
    </div>
  </div>
</div>
<div id="res-memory-legacy" hidden aria-hidden="true"></div>
"""

RESOURCES_CSS = r"""
  /* Content-scoped, kit tokens only: never redefine a theme or the shell. */
  #tab-models .tab-grid { max-width: none; }
  #tab-models .tab-stack { display: grid; grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr); gap: 18px; }
  #models-host-section { grid-column: 1 / -1; grid-row: 1; padding: 0; border: 0; background: transparent; box-shadow: none; }
  #models-loaded-section { grid-column: 1 / -1; grid-row: 2; }
  #models-caches-section { grid-column: 1; grid-row: 3; }
  #gateway-host-section { grid-column: 2; grid-row: 3; }
  #tab-models section { min-width: 0; }
  .res-heading, .res-panel-head, .res-card-label, .res-card-foot, .res-pool-labels { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  .res-heading { margin-bottom: 18px; flex-wrap: wrap; }
  .res-heading h2 { margin: 4px 0 8px; font-size: var(--font-size-2xl); font-weight: 600; }
  .res-eyebrow { color: var(--text-secondary); font-size: var(--font-size-xxs); letter-spacing: .12em; }
  #res-freshness { font-size: var(--font-size-sm); }
  .res-cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
  .res-card, .res-chart-panel, .res-capacity-panel { border: 1px solid var(--ui-border-1); border-radius: var(--radius-lg); background: var(--ui-surface-1); min-width: 0; }
  .res-card { padding: 17px; position: relative; overflow: hidden; }
  .res-card-label { color: var(--text-secondary); font-size: var(--font-size-md); justify-content: flex-start; gap: 8px; }
  #tab-models .res-card-trigger { background: transparent; border: 0; padding: 0; min-height: 28px; font: inherit; color: inherit; text-align: left; }
  .res-card-value { font-size: calc(32px * var(--font-scale)); line-height: 1.35; font-weight: 600; margin: 16px 0 8px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
  .res-card-value small { font-size: var(--font-size-sm); font-weight: 400; color: var(--text-secondary); }
  .res-card-foot { color: var(--text-secondary); font-size: var(--font-size-sm); flex-wrap: wrap; gap: 4px 8px; min-height: 20px; }
  .res-card-foot span { min-width: 0; overflow-wrap: anywhere; }
  .res-card-spark { height: 26px; margin: 12px -17px -17px; color: var(--info); }
  .res-card-spark svg { height: 100%; width: 100%; display: block; }
  .res-card[data-res-card="gpu"] .res-card-spark { color: var(--success); }
  .res-card[data-res-card="cpu"] .res-card-spark { color: var(--accent); }
  .res-card[data-res-card="storage"] .res-card-spark { color: var(--warning); }
  #tab-models .res-info, .res-inspector .res-info { display: inline-grid; place-items: center; min-width: 26px; min-height: 26px; width: 26px; height: 26px; padding: 0; border-radius: 50%; font-size: var(--font-size-xs); font-style: italic; font-family: serif; font-weight: 500; background: transparent; color: var(--text-secondary); border: 1px solid var(--ui-border-2); }
  .res-visuals { display: grid; grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr); gap: 18px; margin-top: 18px; }
  .res-panel-head { padding: 17px 18px 0; flex-wrap: wrap; }
  .res-panel-head h3 { margin: 0; font-weight: 600; font-size: var(--font-size-base); }
  .res-ranges { display: flex; gap: 3px; }
  .res-ranges button, .res-chart-tabs button { min-height: 30px; padding: 4px 9px; font-size: var(--font-size-sm); background: transparent; color: var(--text-secondary); font-weight: 500; }
  .res-ranges button[aria-pressed="true"] { color: var(--text-primary); background: var(--ui-surface-3); }
  .res-chart-tabs { display: flex; align-items: center; gap: 10px; border-bottom: 1px solid var(--ui-border-1); margin: 14px 18px 0; flex-wrap: wrap; }
  .res-chart-tabs button { border: 0; border-radius: 0; padding: 6px 0 10px; border-bottom: 2px solid transparent; }
  .res-chart-tabs button[aria-pressed="true"] { border-bottom-color: var(--accent); color: var(--text-primary); }
  .res-chart-tabs .res-info { margin-left: auto; margin-bottom: 6px; }
  .res-legend { display: flex; flex-wrap: wrap; gap: 12px; padding: 12px 18px 0; }
  .res-legend button { background: transparent; border: 0; color: var(--text-secondary); min-height: 28px; padding: 0; font-size: var(--font-size-sm); font-weight: 400; }
  .res-legend button[aria-pressed="false"] { opacity: .6; text-decoration: line-through; }
  .res-swatch { display: inline-block; width: 7px; height: 7px; border-radius: var(--radius-sm); background: var(--info); margin-right: 6px; }
  .res-swatch.second { background: var(--success); }
  .res-chart { height: 225px; margin: 5px 12px 0; position: relative; touch-action: pan-y; }
  .res-chart svg { width: 100%; height: 100%; }
  .res-chart .res-axis { fill: var(--text-secondary); font-size: var(--font-size-xs); }
  .res-chart .res-grid { stroke: var(--ui-border-1); stroke-dasharray: 3 5; }
  .res-chart .res-line-0 { stroke: var(--info); fill: none; }
  .res-chart .res-line-1 { stroke: var(--success); fill: none; }
  .res-chart .res-dot-0 { fill: var(--info); }
  .res-chart .res-dot-1 { fill: var(--success); }
  .res-chart-readout { position: absolute; top: 4px; right: 12px; max-width: calc(100% - 35px); border: 1px solid var(--ui-border-2); background: var(--ui-surface-2); border-radius: var(--radius-sm); padding: 7px 10px; font-size: var(--font-size-sm); pointer-events: none; box-shadow: var(--shadow-sm, none); }
  .res-chart-readout span { display: block; }
  .res-chart-caption { padding: 10px 18px; margin: 0; border-top: 1px solid var(--ui-border-1); font-size: var(--font-size-sm); }
  .res-pool { margin: 17px 18px; }
  .res-pool + .res-pool { border-top: 1px solid var(--ui-border-1); padding-top: 14px; }
  .res-pool strong { display: block; font-size: var(--font-size-xl); font-weight: 600; font-variant-numeric: tabular-nums; }
  .res-pool-title { display: block; color: var(--text-secondary); font-size: var(--font-size-sm); margin-bottom: 5px; }
  .res-pool-labels { font-size: var(--font-size-sm); color: var(--text-secondary); margin-top: 6px; flex-wrap: wrap; gap: 3px 8px; }
  .res-track { height: 8px; background: var(--ui-surface-3); border: 1px solid var(--ui-border-1); border-radius: var(--radius-sm); overflow: hidden; margin-top: 10px; }
  .res-track span { display: block; height: 100%; background: var(--info); }
  .res-pool.gpu .res-track span { background: var(--success); }
  .res-pool .res-scope { display: block; font-size: var(--font-size-xs); color: var(--text-secondary); margin-top: 6px; }
  .res-swap { border-top: 1px solid var(--ui-border-1); padding: 12px 18px; display: flex; gap: 8px; justify-content: space-between; flex-wrap: wrap; font-size: var(--font-size-sm); }
  .res-inspect { margin: 0 18px 15px; background: transparent; color: var(--text-secondary); font-size: var(--font-size-sm); }
  .res-gateway-summary { color: var(--text-secondary); font-size: var(--font-size-sm); margin: 14px 0; }
  .res-gateway-summary span { display: block; margin: 5px 0; }
  .res-inspector { max-width: min(920px, calc(100vw - 24px)); }
  .res-inspector[data-inspector="memory"] { width: min(680px, calc(100vw - 24px)); max-width: min(680px, calc(100vw - 24px)); }
  .res-mem-mode { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
  .res-mem-pools { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
  .res-mem-pool { border: 1px solid var(--ui-border-1); border-radius: var(--radius-lg); background: var(--ui-surface-1); padding: 16px; min-width: 0; }
  .res-mem-label { display: flex; align-items: center; justify-content: space-between; gap: 6px; font-size: var(--font-size-sm); color: var(--text-secondary); }
  .res-mem-value { display: block; margin: 16px 0; font-size: calc(30px * var(--font-scale)); line-height: 1.25; font-weight: 600; font-variant-numeric: tabular-nums; }
  .res-mem-track { height: 7px; background: var(--ui-surface-3); border-radius: var(--radius-sm); overflow: hidden; }
  .res-mem-track > span { display: block; height: 100%; background: var(--info); }
  .res-mem-gpu .res-mem-track > span { background: var(--success); }
  .res-mem-caption { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 4px; color: var(--text-secondary); font-size: var(--font-size-xs); margin-top: 10px; }
  .res-mem-badge { color: var(--text-secondary); font-size: var(--font-size-xs); white-space: nowrap; }
  .res-mem-detail-head { display: flex; align-items: center; justify-content: space-between; margin: 22px 0 12px; font-size: var(--font-size-sm); font-weight: 500; }
  .res-mem-facts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; }
  .res-mem-facts > div { min-width: 0; }
  .res-mem-facts strong { display: block; margin-top: 10px; font-size: var(--font-size-xl); font-weight: 600; font-variant-numeric: tabular-nums; }
  .res-mem-fact-note { color: var(--text-secondary); font-size: var(--font-size-xs); }
  .res-mem-footer { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; border-top: 1px solid var(--ui-border-1); margin-top: 22px; padding-top: 12px; font-size: var(--font-size-sm); color: var(--text-secondary); }
  @media (max-width: 479.98px) { .res-mem-pools { grid-template-columns: minmax(0, 1fr); gap: 10px; } .res-mem-pool { padding: 12px 14px; } .res-mem-value { margin: 8px 0 12px; font-size: calc(27px * var(--font-scale)); } .res-mem-facts { grid-template-columns: minmax(0, 1fr); gap: 0; } .res-mem-facts > div { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 8px; min-height: 40px; } .res-mem-facts strong { margin: 0; font-size: var(--font-size-base); } .res-mem-facts .res-mem-label { justify-content: flex-start; } .res-mem-fact-note { grid-column: 2; } .res-mem-detail-head, .res-mem-footer { margin-top: 16px; } }
  .res-inspector .entity-overview { margin-top: 15px; }
  .res-inspector .res-details-list { display: grid; grid-template-columns: minmax(110px, 1fr) minmax(0, 2fr); margin: 0; font-size: var(--font-size-base); }
  .res-details-list dt, .res-details-list dd { margin: 0; padding: 12px 0; border-bottom: 1px solid var(--ui-border-1); overflow-wrap: anywhere; }
  .res-details-list dt { color: var(--text-secondary); padding-right: 12px; }
  @media (max-width: 1023.98px) { .res-cards { grid-template-columns: repeat(2, minmax(0, 1fr)); } #tab-models .tab-stack, .res-visuals { grid-template-columns: minmax(0, 1fr); } #models-caches-section, #gateway-host-section, #models-loaded-section { grid-column: 1; grid-row: auto; } #gateway-host-section { order: 3; } #models-caches-section { order: 4; } }
  @media (max-width: 479.98px) { .res-heading { align-items: start; } .res-card { padding: 12px; } .res-card-value { font-size: calc(26px * var(--font-scale)); } .res-card-spark { margin: 10px -12px -12px; } .res-card-label { gap: 4px; font-size: var(--font-size-sm); } .res-chart { margin-inline: 6px; } .res-chart-tabs { gap: 8px; margin-inline: 12px; } .res-chart-tabs button { font-size: var(--font-size-xs); } .res-chart-tabs .res-info { margin-left: 0; } .res-heading .actions { width: 100%; justify-content: space-between; } }
"""

RESOURCES_JS = r"""
    // Samples belong to this signed-in principal and this host, never a theme.
    const resUi = { samples: [], range: 15, chart: "memory", hidden: new Set(), cursor: null, release: null, hostKey: null, last: null, error: null };
    function resourceNumber(value) { return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null; }
    function resourceMemoryView(data) {
      const mem = data && data.memory || {}, d = mem.device || {}, ram = mem.ram || {};
      const backend = String(d.backend || "").toLowerCase();
      // Explicit provider evidence takes precedence, including integrated CUDA.
      const declared = d.memory_topology || mem.memory_topology;
      const flag = typeof d.unified_memory === "boolean" ? d.unified_memory : mem.unified_memory;
      const hardware = data && data.system && data.system.hardware || {};
      const owner = data && data.host || mem.host || {};
      const appleSilicon = hardware.platform === "darwin" && ["arm64", "aarch64"].includes(hardware.machine) && !!owner.host_name && hardware.host_name === owner.host_name;
      const topology = typeof flag === "boolean" ? (flag ? "unified" : "dedicated")
        : declared ? (declared === "unified" ? "unified" : declared === "dedicated" || declared === "discrete" ? "dedicated" : "unknown")
        : backend === "metal" || backend === "mps" && appleSilicon ? "unified" : backend === "cuda" ? "gpu" : "unknown";
      const total = resourceNumber(d.total_bytes), free = resourceNumber(d.free_bytes);
      let device;
      if (topology !== "unified" && total !== null && free !== null && free <= total) {
        // CUDA total/free are DEVICE-WIDE; never substitute process-held bytes.
        device = { used: total - free, ceiling: total, free, label: topology === "unknown" ? "GPU memory" : "GPU VRAM", scope: "Device-wide", note: topology === "unknown" ? "Physical topology unreported; independently measured device counter." : "Separate from system RAM; the reported GPU device only." };
      } else if (topology === "unified") {
        const heap = deviceMeterView(d);
        device = { used: heap.used, ceiling: heap.ceiling, free: null, label: "Accelerator heap", scope: heap.scope, note: "Overlaps unified RAM · memory-mapped GGUF weights may be absent" };
      } else {
        const allocated = resourceNumber(d.process_held_bytes) ?? resourceNumber(d.allocated_bytes);
        device = { used: allocated, ceiling: null, free: null, label: "GPU allocations", scope: "This process only", note: "Device-wide capacity unavailable; physical memory topology not reported." };
      }
      return { topology, backend, ram, device, title: topology === "unified" ? "Unified memory" : topology === "dedicated" ? "RAM + dedicated VRAM" : topology === "gpu" ? "RAM + GPU VRAM" : "Memory topology unreported" };
    }
    function resourceSnapshot(data, failedAt = null) {
      const view = resourceMemoryView(data), system = data && data.system || {}, gpu = data && data.gpu || {};
      const good = (block, key) => block && block.supported !== false ? resourceNumber(block[key]) : null;
      // Browser-local history uses receipt time, avoiding server/client clock
      // skew. Preserve server time separately for the freshness inspector.
      return { at: failedAt || Date.now(), sourceAt: resourceNumber(data && data.ts) === null ? null : data.ts * 1000,
        ram: resourceNumber(view.ram.used_bytes), device: view.device.used,
        cpu: good(system.cpu, "utilization_pct"), gpu: gpu.supported === true ? resourceNumber(gpu.utilization_gpu_pct) : null,
        disk: good(system.disk_io, "read_bytes_per_s"), net: good(system.network, "read_bytes_per_s"),
        topology: view.topology, deviceScope: view.device.scope, deviceLabel: view.device.label };
    }
    function resourcePushSample(sample) {
      let previous = resUi.samples[resUi.samples.length - 1];
      // A scope/topology change is a series boundary, never a connecting line.
      if (previous && (previous.topology !== sample.topology || previous.deviceScope !== sample.deviceScope)) { resUi.samples = []; previous = null; }
      if (!previous || sample.at > previous.at) resUi.samples.push(sample);
      const cutoff = Date.now() - 3600000;
      resUi.samples = resUi.samples.filter(s => s.at >= cutoff).slice(-720);
    }
    function resourceInfo(label, text) { return `<button type="button" class="secondary res-info" aria-label="About ${esc(label)}" data-af-tip="${esc(text)}">i</button>`; }
    function resourceFmt(value, suffix = "") { return value === null ? "Unavailable" : _fmtBytes(value) + suffix; }
    function resourcePool(label, used, total, available, scope, gpu = false, ceilingWord = "total") {
      const fraction = used !== null && total !== null && total > 0 ? Math.max(0,Math.min(100, used/total*100)) : null;
      return `<div class="res-pool${gpu ? " gpu" : ""}"><span class="res-pool-title">${esc(label)}</span><strong>${available === null ? resourceFmt(used) : resourceFmt(available) + " available"}</strong><div class="res-track" role="img" aria-label="${esc(used === null ? "Unavailable" : resourceFmt(used) + " used" + (total === null ? "" : " of " + resourceFmt(total)))}"><span style="width:${fraction ?? 0}%"></span></div><div class="res-pool-labels"><span>${used === null ? "Used: unavailable" : resourceFmt(used) + " used"}</span><span>${total === null ? "Capacity unknown" : resourceFmt(total) + " " + ceilingWord}</span></div><span class="res-scope">${esc(scope)}</span></div>`;
    }
    function renderResourcesDashboard(data, error = null) {
      const root = $("resources-dashboard"); if (!root) return;
      const host = data && data.host || {}, principal = state.principal || {};
      const key = [principal.tenant_id,principal.user_id,host.host_id || host.host_name || "local"].join(":");
      if (data && key !== resUi.hostKey) { resUi.samples = []; resUi.hostKey = key; }
      resUi.last = data; resUi.error = error;
      const sample = resourceSnapshot(data, error ? Date.now() : null);
      if (!data && resUi.samples.length) { const previous = resUi.samples[resUi.samples.length-1]; sample.topology=previous.topology; sample.deviceScope=previous.deviceScope; sample.deviceLabel=previous.deviceLabel; }
      resourcePushSample(sample);
      const view = resourceMemoryView(data), system = data && data.system || {}, gpu = data && data.gpu || {};
      const ram = view.ram, used = resourceNumber(ram.used_bytes), total = resourceNumber(ram.total_bytes), available = resourceNumber(ram.available_bytes);
      // available_bytes is an OS measurement, not total-used (OS accounting differs).
      const pct = resourceNumber(ram.percent) ?? (used !== null && total > 0 ? used/total*100 : null);
      const cpu = system.cpu || {}, disk = system.storage || {}, swap = system.swap || {};
      const cpuPct = cpu.supported !== false ? resourceNumber(cpu.utilization_pct) : null;
      const gpuPct = gpu.supported === true ? resourceNumber(gpu.utilization_gpu_pct) : null;
      const diskUsed = disk.supported !== false ? resourceNumber(disk.used_bytes) : null, diskTotal = resourceNumber(disk.total_bytes);
      const cards = [
        ["ram", "System memory", used === null ? "—" : _fmtBytes(used), total === null ? "" : "/ " + _fmtBytes(total), pct === null ? "Unavailable" : _fmtPct(pct) + " used", available === null ? "Available not reported" : _fmtBytes(available) + " available", "OS system RAM accounting. Available memory is reported by the OS; it need not equal total minus used. This is not a promise that another model will fit."],
        ["cpu", "CPU", cpuPct === null ? "—" : _fmtPct(cpuPct), "", cpuPct === null ? cpu.reason || system.reason || "Not reported by this gateway" : "Host-wide", cpu.logical_cores ? cpu.logical_cores + " logical cores" : "", "CPU busy time between host samples, excluding idle and I/O wait. The first interval is unavailable while counters warm up."],
        ["gpu", "GPU utilization", gpuPct === null ? "—" : _fmtPct(gpuPct), "", gpuPct === null ? gpu.reason || error || "Probe unavailable" : "Host-wide", gpu.source || "", "GPU busy time, separate from memory use. The source probe and unavailable state are explicit. On multiple GPUs this probe may report the average utilization."],
        ["storage", "Data storage", diskUsed === null ? "—" : _fmtBytes(diskUsed), diskTotal === null ? "" : "/ " + _fmtBytes(diskTotal), "Gateway data volume", resourceNumber(disk.free_bytes) === null ? disk.reason || system.reason || "Not reported" : _fmtBytes(disk.free_bytes) + " available", "Capacity of the volume containing gateway data. Model downloads may live on another volume. Disk I/O in the activity chart is host-wide, not limited to this volume."]
      ];
      // Keep DOM/focus stable across polls (including open info popovers).
      const signature = cards.map(c => c[0]).join();
      if ($("res-cards").dataset.signature !== signature) {
        $("res-cards").innerHTML = cards.map(c => `<article class="res-card" data-res-card="${c[0]}"><div class="res-card-label"><button type="button" class="res-card-trigger" data-res-detail="${c[0]==="ram"||c[0]==="gpu"?"memory":"system"}">${c[1]}</button>${resourceInfo(c[1],c[6])}</div><div class="res-card-value"><span></span> <small></small></div><div class="res-card-foot"><span></span><span></span></div><div class="res-card-spark"></div></article>`).join("");
        $("res-cards").dataset.signature = signature;
      }
      cards.forEach(c => { const el = $("res-cards").querySelector(`[data-res-card="${c[0]}"]`); el.querySelector(".res-card-value span").textContent = c[2]; el.querySelector(".res-card-value small").textContent = c[3]; const foot = el.querySelectorAll(".res-card-foot span"); foot[0].textContent = c[4]; foot[1].textContent = c[5]; });
      $("res-host-name").textContent = host.host_name || host.host_id || "Host resources";
      $("res-topology").textContent = view.title;
      $("res-topology").dataset.afTip = view.topology === "gpu" ? "CUDA device memory is displayed separately. Physical topology was not explicitly reported; integrated devices can override this through unified_memory." : "Based on explicit memory topology, the MLX Metal backend, or MPS with same-host Apple Silicon evidence. Operating-system name alone does not establish unified memory.";
      $("res-topology").tabIndex = 0;
      $("res-freshness").textContent = error ? "Snapshot unavailable · retry with refresh" : "Received " + new Date(sample.at).toLocaleTimeString() + " · refreshes every 5s";
      $("res-freshness").dataset.afTip = error || "Browser receipt time. Host snapshot: " + (sample.sourceAt === null ? "timestamp not reported" : new Date(sample.sourceAt).toLocaleString()) + ". History uses browser receipt time; source clocks may differ.";
      $("res-freshness").tabIndex = 0;
      $("res-capacity-title").textContent = view.topology === "unified" ? "Unified memory capacity" : "Memory capacity";
      let capacity = resourcePool("System RAM",used,total,available,"Host-wide · operating-system accounting");
      if (view.backend || view.device.used !== null) capacity += resourcePool(view.device.label,view.device.used,view.device.ceiling,view.device.free,view.device.scope + " · " + view.device.note,true,view.topology === "unified" ? "limit" : "total");
      else capacity += '<div class="res-pool"><span class="res-scope">Accelerator capacity not reported. System RAM remains the capacity reference.</span></div>';
      capacity += `<div class="res-swap"><span>Swap used ${resourceInfo("swap usage","OS swap bytes in use. Swap use alone does not establish memory pressure. A zero is shown only when reported by the host.")}</span><strong>${swap.supported === false ? "Unavailable" : resourceFmt(resourceNumber(swap.used_bytes))}</strong></div>`;
      // The capacity panel has no editable controls; retain its info focus anyway.
      const focusedInfo = document.activeElement && $("res-capacity").contains(document.activeElement) ? document.activeElement.getAttribute("aria-label") : null;
      $("res-capacity").innerHTML = capacity;
      if (focusedInfo) Array.from($("res-capacity").querySelectorAll("button")).find(b=>b.getAttribute("aria-label")===focusedInfo)?.focus();
      resourcesRenderChart(); resourcesSparks(); resourcesRunnerSummary(); renderResourcesMemory();
    }
    function resourceMemoryInspectorView(data) {
      const view = resourceMemoryView(data), breakdown = memoryBreakdown(data || {});
      const rows = data && Array.isArray(data.models) ? data.models : null;
      const loaded = rows ? rows.filter(r => r && r.resident === true) : null;
      const weights = breakdown.items.filter(i => i.key.startsWith("model:"));
      const knownEmpty = rows !== null && rows.every(r => r && r.resident === false);
      const weightBytes = weights.length ? weights.reduce((sum, i) => sum + i.bytes, 0) : knownEmpty ? 0 : null;
      const partial = !!loaded && weights.length > 0 && weights.length < loaded.length;
      const cache = breakdown.items.find(i => i.key === "model_caches");
      const process = breakdown.items.find(i => i.key === "process_rss");
      return { ...view, weightBytes, partial, estimated: weights.some(i => i.estimated),
        cacheBytes: cache ? cache.bytes : null, processBytes: process ? process.bytes : null,
        modelCount: loaded === null ? null : loaded.length,
        sessionCount: data && Array.isArray(data.session_caches) ? data.session_caches.length : null,
        sessionBytes: breakdown.items.find(i => i.key === "session_caches")?.bytes ?? null };
    }
    function renderResourcesMemory() {
      const data = resUi.last, view = resourceMemoryInspectorView(data), ram = view.ram, gpu = view.device;
      const fmt = value => value === null ? "—" : _fmtBytes(value);
      const available = resourceNumber(ram.available_bytes), total = resourceNumber(ram.total_bytes);
      const shared = view.topology === "unified", gpuAvailable = !shared && gpu.free !== null;
      const gpuValue = gpuAvailable ? gpu.free : gpu.used;
      const meter = (id, value, ceiling, name) => {
        const fraction = value !== null && ceiling !== null && ceiling > 0 ? Math.min(100,Math.max(0,value/ceiling*100)) : null;
        $(id).firstElementChild.style.width = `${fraction ?? 0}%`;
        $(id).setAttribute("aria-label", value === null ? name + " unavailable" : `${name}: ${_fmtBytes(value)}${ceiling === null ? "" : " of " + _fmtBytes(ceiling)}`);
      };
      $("res-mem-kind").textContent = shared ? "Unified memory" : view.topology === "dedicated" ? "Separate RAM & VRAM" : view.topology === "gpu" ? "RAM & GPU memory" : "Memory type unknown";
      $("res-mem-ram-value").textContent = fmt(available);
      $("res-mem-ram-caption").textContent = total === null ? "Capacity not reported" : _fmtBytes(total) + " total";
      $("res-mem-ram-info").dataset.afTip = "Available system RAM, reported by the operating system. This is not a model-fit guarantee. " + (resourceNumber(ram.used_bytes) === null ? "Used RAM is not reported." : "Reported used RAM: " + _fmtBytes(ram.used_bytes) + ". OS used and available counters need not sum to installed RAM.");
      meter("res-mem-ram-bar",available,total,"Available RAM");
      $("res-mem-gpu-label").textContent = shared ? "GPU allocations" : gpuAvailable ? "Available VRAM" : "GPU memory";
      $("res-mem-gpu-value").textContent = fmt(gpuValue);
      $("res-mem-gpu-caption").textContent = shared ? "Shares RAM" : gpu.ceiling === null ? "Capacity not reported" : _fmtBytes(gpu.ceiling) + " total";
      $("res-mem-gpu-scope").textContent = gpu.used === null && gpu.ceiling === null ? "Not reported" : gpu.scope === "all processes" ? "All processes" : gpu.scope === "Device-wide" ? "GPU device" : "This process";
      $("res-mem-gpu-info").dataset.afTip = shared
        ? `GPU allocations overlap system RAM. ${gpu.ceiling === null ? "The accelerator limit is not reported." : "Accelerator limit: " + _fmtBytes(gpu.ceiling) + "."} ${gpu.scope}. Memory-mapped GGUF weights may be absent; do not subtract this counter from RAM.`
        : `${gpu.scope}. ${gpu.note} ${gpu.used === null ? "Usage not reported." : "Reported use: " + _fmtBytes(gpu.used) + "."}`;
      meter("res-mem-gpu-bar",gpuValue,gpu.ceiling,shared ? "GPU allocations (shared RAM)" : gpuAvailable ? "Available VRAM" : "GPU memory");
      $("res-mem-weights-value").textContent = (view.estimated && view.weightBytes !== null ? "~" : "") + fmt(view.weightBytes);
      $("res-mem-weights-note").hidden = !view.partial;
      $("res-mem-weights-info").dataset.afTip = view.weightBytes === null ? "The host did not report weight sizes for loaded models. A dash means unknown, not zero." : `${view.estimated ? "Includes estimated on-disk weight sizes." : "Weight sizes reported by the model provider."} ${view.partial ? "Partial: some loaded model sizes are not reported." : ""} Weights may overlap GPU allocations and process memory; this is not a promise of memory freed by unloading.`;
      $("res-mem-cache-value").textContent = fmt(view.cacheBytes);
      $("res-mem-cache-info").dataset.afTip = "Prompt/KV cache bytes reported for resident models. Session caches may overlap this counter; do not add them together. " + (view.sessionBytes === null ? "Session cache size is not reported." : "Session inventory: " + _fmtBytes(view.sessionBytes) + ".");
      $("res-mem-process-value").textContent = fmt(view.processBytes);
      $("res-mem-model-count").textContent = view.modelCount === null ? "Models unavailable" : `${view.modelCount} model${view.modelCount === 1 ? "" : "s"} loaded`;
      $("res-mem-session-count").textContent = view.sessionCount === null ? "Session caches unavailable" : `${view.sessionCount} session cache${view.sessionCount === 1 ? "" : "s"}`;
    }
    function resourceSeries() {
      const last = resUi.samples[resUi.samples.length-1];
      return resUi.chart === "memory" ? [["ram","RAM used"],["device",last ? last.deviceLabel + " · " + last.deviceScope : "GPU memory"]] : resUi.chart === "compute" ? [["cpu","CPU"],["gpu","GPU"]] : [["disk","Disk read · host"],["net","Network receive · host"]];
    }
    function resourcesRenderChart() {
      const chart = $("res-chart"); if (!chart) return;
      const width = Math.max(240,chart.clientWidth), height = chart.clientHeight || 225, left = 43, right = width-15, bottom = height-28, top = 17;
      const now = Date.now(), cutoff = now-resUi.range*60000;
      const samples = resUi.samples.filter(s=>s.at>=cutoff), spec = resourceSeries();
      const scale = resUi.chart === "memory" ? 1024**3 : resUi.chart === "io" ? 1024**2 : 1;
      const known = samples.flatMap(s=>spec.map(([key])=>s[key])).filter(v=>v!==null).map(v=>v/scale);
      const max = resUi.chart === "compute" ? 100 : Math.max(1, Math.ceil(Math.max(0,...known)*1.15));
      // Window covers collected time only; its caption always states that scope.
      const first = samples.length ? Math.min(now,samples[0].at) : now;
      const start = Math.max(cutoff,Math.min(first,now-5000)), end = Math.max(now,start+1);
      const x = t=>left+(right-left)*(t-start)/(end-start), y=v=>bottom-v/max*(bottom-top);
      const unit = resUi.chart === "memory" ? "GiB" : resUi.chart === "compute" ? "%" : "MiB/s";
      let svg = `<svg viewBox="0 0 ${width} ${height}" aria-hidden="true">`;
      for (let i=0;i<5;i++) { const v=max*i/4; svg+=`<line class="res-grid" x1="${left}" x2="${right}" y1="${y(v)}" y2="${y(v)}"/><text class="res-axis" x="${left-8}" y="${y(v)+4}" text-anchor="end">${Number(v.toFixed(1))}</text>`; }
      spec.forEach(([key],j)=>{
        if (resUi.hidden.has(key)) return;
        let segment=[];
        const flush=()=>{ if(segment.length>1) svg+=`<polyline class="res-line-${j}" points="${segment.join(" ")}" stroke-width="2" stroke-linejoin="round"/>`; segment=[]; };
        samples.forEach((s,i)=>{ const v=s[key]; if(v===null) {flush();return;} if(i && s.at-samples[i-1].at>15000)flush(); const point=`${x(s.at)},${y(v/scale)}`; segment.push(point); svg+=`<circle class="res-dot-${j}" cx="${x(s.at)}" cy="${y(v/scale)}" r="2.5"/>`; }); flush();
      });
      [start,(start+end)/2,end].forEach((t,i)=>svg+=`<text class="res-axis" x="${x(t)}" y="${height-7}" text-anchor="${i===0?"start":i===2?"end":"middle"}">${esc(new Date(t).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}))}</text>`);
      let readout="";
      if(resUi.cursor!==null && samples.length) {
        const i=Math.max(0,Math.min(samples.length-1,resUi.cursor)), s=samples[i];
        svg+=`<line x1="${x(s.at)}" x2="${x(s.at)}" y1="${top}" y2="${bottom}" stroke="var(--text-secondary)" stroke-dasharray="3 4"/>`;
        const lines=spec.filter(([key])=>!resUi.hidden.has(key)).map(([key,label])=>`${label}: ${s[key]===null?"Unavailable":(s[key]/scale).toFixed(1)+" "+unit}`);
        readout=`<div class="res-chart-readout"><strong>${esc(new Date(s.at).toLocaleTimeString())}</strong>${lines.map(s=>`<span>${esc(s)}</span>`).join("")}</div>`;
        chart.setAttribute("aria-label",`Resource history. ${lines.join(". ")}. Use arrow keys.`);
      }
      chart.innerHTML=svg+"</svg>"+readout;
      const legend=$("res-legend");
      const legendKey=JSON.stringify(spec);
      if(legend.dataset.key!==legendKey) {legend.innerHTML=spec.map(([key,label],j)=>`<button type="button" data-res-series="${key}" aria-pressed="${!resUi.hidden.has(key)}"><span class="res-swatch${j?" second":""}"></span>${esc(label)}</button>`).join("");legend.dataset.key=legendKey;}
      legend.querySelectorAll("button").forEach(b=>b.setAttribute("aria-pressed",String(!resUi.hidden.has(b.dataset.resSeries))));
      const age=resUi.samples.length?Math.max(0,Math.floor((now-resUi.samples[0].at)/1000)):0;
      $("res-chart-caption").textContent=`${unit} · ${samples.length} sample${samples.length===1?"":"s"} · since this page opened (${age<60?age+"s":Math.floor(age/60)+"m"}) · ${resUi.range}m window${samples.length<2?" · collecting a trend…":""}`;
    }
    function resourcesSparks() {
      const keys={ram:"ram",cpu:"cpu",gpu:"gpu"};
      document.querySelectorAll(".res-card").forEach(card=>{
        const key=keys[card.dataset.resCard], box=card.querySelector(".res-card-spark");
        if(!key) { const disk=resUi.last && resUi.last.system && resUi.last.system.storage || {}; const used=resourceNumber(disk.used_bytes), total=resourceNumber(disk.total_bytes); box.innerHTML=total>0 && used!==null?`<svg viewBox="0 0 300 26" preserveAspectRatio="none" aria-hidden="true"><rect x="17" y="15" width="${Math.min(266,266*used/total)}" height="3" rx="1.5" fill="currentColor"/></svg>`:"";return; }
        const points=resUi.samples.slice(-60), max=Math.max(1,...points.map(s=>s[key]||0));
        let paths=[],segment=[];const flush=()=>{if(segment.length>1)paths.push(`<polyline points="${segment.join(" ")}" stroke="currentColor" fill="none" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`);segment=[];};
        points.forEach((s,i)=>{if(s[key]===null){flush();return;} if(i&&s.at-points[i-1].at>15000)flush();segment.push(`${i*300/Math.max(1,points.length-1)},${25-s[key]/max*20}`);});flush();box.innerHTML=`<svg viewBox="0 0 300 26" preserveAspectRatio="none" aria-hidden="true">${paths.join("")}</svg>`;
      });
    }
    function resourcesRunnerSummary() {
      const el=$("res-gateway-summary");if(!el)return;
      const runner=state.hostRunner, tray=state.hostTray;
      const text=runner?`${Number(runner.inflight_ticks)||0} workflow steps in flight · ${runner.paused?"Paused":"Running"}`:"Runner state unavailable";
      const incident=runner&&runner.last_hang;
      const value=text+(incident?"\nRecovered from a hang · "+_gwFmtWhen(incident.at):"");
      if(el.textContent!==value)el.textContent=value;
      el.style.whiteSpace="pre-line";
    }
    function resourcesCloseInspector() {
      $("resources-inspector-backdrop").hidden=true;
      const release=resUi.release;resUi.release=null;if(release)release();
    }
    function resourcesOpenInspector(which) {
      resourcesCloseInspector();
      ["memory","gateway","system"].forEach(k=>$("res-"+k+"-details").hidden=k!==which);
      $("resources-inspector-title").textContent=which==="memory"?"Memory":which==="gateway"?"Manage gateway":"System metrics";
      $("resources-inspector-backdrop").querySelector(".res-inspector").dataset.inspector=which;
      if(which==="memory")renderResourcesMemory();
      if(which==="system") {
        const sys=resUi.last&&resUi.last.system||{};
        const cpu=sys.cpu||{},swap=sys.swap||{},disk=sys.storage||{},io=sys.disk_io||{},net=sys.network||{};
        const rate=v=>resourceNumber(v)===null?"Unavailable":_fmtBytes(v)+"/s";
        const rows=[["CPU utilization",resourceNumber(cpu.utilization_pct)===null?cpu.reason||"Unavailable":_fmtPct(cpu.utilization_pct)],["Logical cores",cpu.logical_cores??"Unavailable"],["Load average",Array.isArray(cpu.load_average)?cpu.load_average.map(v=>v.toFixed(2)).join(" / "):"Unavailable"],["Swap used",resourceFmt(resourceNumber(swap.used_bytes))],["Data volume used",resourceFmt(resourceNumber(disk.used_bytes))],["Data volume available",resourceFmt(resourceNumber(disk.free_bytes))],["Disk read / write",rate(io.read_bytes_per_s)+" / "+rate(io.write_bytes_per_s)],["Disk scope",io.scope||io.reason||"Unavailable"],["Network receive / send",rate(net.read_bytes_per_s)+" / "+rate(net.write_bytes_per_s)],["Network scope",net.scope||net.reason||"Unavailable"]];
        $("res-system-details").innerHTML='<p class="muted">Host observations, sampled independently. Missing data stays unavailable.</p><dl class="res-details-list">'+rows.map(([name,value])=>`<dt>${esc(name)}</dt><dd>${esc(String(value))}</dd>`).join("")+"</dl>";
      }
      $("resources-inspector-backdrop").hidden=false;
      resUi.release=bindAccountModal($("resources-inspector-backdrop"),resourcesCloseInspector);
    }
    function resetResourcesDashboard() {
      resUi.samples=[];resUi.last=null;resUi.hostKey=null;resUi.cursor=null;
      resourcesCloseInspector();
      $("res-cards").textContent="";delete $("res-cards").dataset.signature;
    }
    function initResourcesDashboard() {
      const host=$("models-host-section");
      const memory=$("res-memory-legacy");
      ["models-meters","models-breakdown","models-host-facts"].forEach(id=>memory.append($(id)));
      const oldHead=host.querySelector(".section-head");
      $("res-actions").append($("models-refresh"));oldHead.remove();
      $("res-messages").append($("models-message"),$("models-degraded"));
      const gateway=$("gateway-host-section"), management=$("res-gateway-legacy");
      management.append(gateway.querySelector(".entity-overview"),gateway.querySelector(".actions"));
      const summary=document.createElement("p");summary.id="res-gateway-summary";summary.className="res-gateway-summary";gateway.append(summary);
      const manage=document.createElement("button");manage.type="button";manage.className="secondary";manage.textContent="Manage gateway ↗";manage.dataset.resInspect="gateway";gateway.append(manage);
      // Existing notes become the kit's focus/touch/hover info, not hidden risks.
      [gateway,$("models-loaded-section"),$("models-caches-section")].forEach(section=>{
        const note=section.querySelector(".section-note"), head=section.querySelector(".section-title");
        if(note&&head) {head.insertAdjacentHTML("beforeend",resourceInfo(head.textContent.trim(),note.textContent.trim()));note.remove();}
      });
      $("models-host-section").addEventListener("click",e=>{const trigger=e.target.closest("[data-res-detail]");if(trigger){resourcesOpenInspector(trigger.dataset.resDetail);return;}const card=e.target.closest(".res-card");if(card&&!e.target.closest("button"))resourcesOpenInspector(card.dataset.resCard==="ram"||card.dataset.resCard==="gpu"?"memory":"system");});
      document.querySelectorAll("[data-res-inspect]").forEach(b=>b.addEventListener("click",()=>resourcesOpenInspector(b.dataset.resInspect)));
      $("resources-inspector-close").onclick=resourcesCloseInspector;
      $("resources-dashboard").addEventListener("click",e=>{
        const b=e.target.closest("button");if(!b)return;
        if(b.dataset.resRange){resUi.range=Number(b.dataset.resRange);resUi.cursor=null;document.querySelectorAll("[data-res-range]").forEach(t=>t.setAttribute("aria-pressed",String(t===b)));resourcesRenderChart();}
        if(b.dataset.resChart){resUi.chart=b.dataset.resChart;resUi.cursor=null;document.querySelectorAll("[data-res-chart]").forEach(t=>t.setAttribute("aria-pressed",String(t===b)));resourcesRenderChart();}
        if(b.dataset.resSeries){const key=b.dataset.resSeries;resUi.hidden.has(key)?resUi.hidden.delete(key):resUi.hidden.add(key);resourcesRenderChart();}
      });
      const chart=$("res-chart");
      chart.addEventListener("keydown",e=>{if(!["ArrowLeft","ArrowRight","Home","End"].includes(e.key))return;e.preventDefault();const n=resUi.samples.filter(s=>s.at>=Date.now()-resUi.range*60000).length;resUi.cursor=e.key==="Home"?0:e.key==="End"?n-1:Math.max(0,Math.min(n-1,(resUi.cursor??n-1)+(e.key==="ArrowRight"?1:-1)));resourcesRenderChart();});
      chart.addEventListener("pointermove",e=>{const r=chart.getBoundingClientRect(), samples=resUi.samples.filter(s=>s.at>=Date.now()-resUi.range*60000);if(!samples.length)return;const target=samples[0].at+Math.max(0,Math.min(1,(e.clientX-r.left-43)/(r.width-58)))*(Date.now()-samples[0].at);resUi.cursor=samples.reduce((best,s,i)=>Math.abs(s.at-target)<Math.abs(samples[best].at-target)?i:best,0);resourcesRenderChart();});
      chart.addEventListener("pointerleave",()=>{resUi.cursor=null;resourcesRenderChart();});
      new ResizeObserver(()=>resourcesRenderChart()).observe(chart);
      new MutationObserver(resourcesRunnerSummary).observe($("gateway-host-state"),{childList:true,subtree:true,characterData:true});
    }
    initResourcesDashboard();
"""
