"""Console model catalog as cards (mission X2, 2026-09-24).

The operator, on the Models tab ("72 models, 170 artifacts" as one table of
model rows with artifact sub-rows): "seeing only the Q4 is a bit
problematic", "have a top level filter for 4bit and 8bit quant", and
"1 card <-> 1 model so we can distinguish the cards".

This module owns that view. ``console.py`` splices it right after the
console UI layer (``console_ui.py``), inside the same main script, so its
functions share the console's scope (``state``, ``api``, ``$``, ``esc``,
``uiProgressMarkup``, ``trackDownloadJob``, ``dlCancel``...).

- ONE CARD PER MODEL. Header: name, organisation, parameters, licence,
  capability tags, a "Starter" badge when the row carries ``starter``. Body:
  the model's artifacts as compact rows (provider, artifact id in mono with
  ellipsis + tooltip + copy on click, quantization, download size, weights
  and fit pills, one action per state). The recommended artifact is first
  and primary; the others are quieter.
- ONE DOWNLOAD RENDERER. A running download is N's job
  (``state.downloadJobs``, fed by the SSE stream or polling in
  ``console_ui.py``) drawn by ``uiProgressMarkup`` -- the same component the
  guide's download cards use.
- FILTERS: search, quantization (All / 4-bit / 8-bit / Other), provider,
  capability, "Fits this computer", Downloaded / Not downloaded. Filters
  hide, they never cap (ADR-0026): every matching model is drawn. The tab's
  filters live in the URL hash (``#catalog?quant=8bit&provider=mlx``).
- HUGGING FACE SEARCH: a "Catalog / Hugging Face" switch next to the search
  box. In Hugging Face mode Enter asks the same catalog route with
  ``hub=true`` (AbstractCore searches the Hub, cached 24 h) and the answer is
  drawn as the SAME cards; the query lives in the hash (``#catalog?hf=...``).
- THE QUANT FILTER READS ``artifact.quant_class`` FROM THE PAYLOAD
  (AbstractCore ``model_catalog_v1``). It is never derived here from the
  quant string: a gateway whose catalog does not send the field shows a
  notice and a disabled quant filter.
"""

from __future__ import annotations

CATALOG_CSS = r"""
    /* ================= Model catalog as cards (mission X2) ================= */
    #catalog-cards-root, #first-run-model-catalog { display: grid; gap: 18px; min-width: 0; }
    .mc-root { display: grid; gap: 18px; min-width: 0; }
    .mc-host { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 14px; color: var(--text-secondary); font-size: var(--font-size-sm); }
    .mc-host b { color: var(--text-primary); font-weight: 600; }
    .mc-host__facts { margin-right: auto; min-width: 0; }
    /* The filter bar: its top row (search, fits, count, the active filters in
       words) stays under the tab header while the cards scroll; the chip
       panel below it scrolls away, so a 1024 x 768 window keeps its cards.
       The two read as one surface at the top. Opaque (the card tone laid over
       the page tone): the cards pass underneath. Sticky honours the
       scroller's padding, so inside the console's scrolling <main>
       (padding-top 20px) the row lifts by that much to sit flush under the
       header; the guide's scroller has no padding. */
    .mc-bar { position: sticky; top: 0; z-index: 6; display: flex; flex-wrap: wrap; align-items: center; gap: 10px 18px; min-width: 0; padding: 14px 16px 12px; border: 1px solid var(--ui-border-1); border-bottom-color: transparent; border-radius: var(--radius-lg) var(--radius-lg) 0 0; background-color: var(--bg-primary); background-image: linear-gradient(var(--bg-card, var(--bg-secondary)), var(--bg-card, var(--bg-secondary))); }
    .shell_content .mc-bar { top: -20px; }
    /* No chip panel (the data is not there yet, or the Hub search failed): the row closes itself. */
    .mc-bar:has(+ .mc-filters:empty) { border-bottom-color: var(--ui-border-1); border-bottom-left-radius: var(--radius-lg); border-bottom-right-radius: var(--radius-lg); }
    .mc-bar.is-stuck { border-bottom-color: var(--ui-border-1); border-top-left-radius: 0; border-top-right-radius: 0; border-bottom-left-radius: var(--radius-lg); border-bottom-right-radius: var(--radius-lg); box-shadow: 0 10px 22px -16px rgba(0, 0, 0, .6); }
    .mc-mode { display: inline-flex; flex: 0 0 auto; padding: 3px; gap: 2px; border: 1px solid var(--ui-border-2); border-radius: 999px; background: var(--ui-surface-1); }
    .mc-mode button { min-height: 30px; padding: 4px 12px; border: 0; border-radius: 999px; background: transparent; color: var(--text-secondary); font-size: var(--font-size-sm); font-weight: 600; white-space: nowrap; box-shadow: none; cursor: pointer; }
    .mc-mode button:hover:not([aria-pressed="true"]) { background: var(--ui-surface-2); color: var(--text-primary); filter: none; }
    .mc-mode button[aria-pressed="true"] { background: var(--accent-subtle); box-shadow: inset 0 0 0 1px var(--accent); color: var(--text-primary); }
    .mc-mode button:focus-visible, .mc-hf-go:focus-visible { outline: 2px solid var(--info); outline-offset: 2px; }
    .mc-bar .mc-hf-go[hidden] { display: none; }
    .mc-bar input.mc-search { flex: 1 1 340px; min-width: min(100%, 260px); width: auto; margin: 0; }
    .mc-bar .ui-switch { flex: 0 0 auto; }
    .mc-bar__end { display: flex; align-items: center; gap: 12px; margin-left: auto; min-width: 0; }
    .mc-count { color: var(--text-secondary); font-size: var(--font-size-sm); font-variant-numeric: tabular-nums; white-space: nowrap; }
    .mc-count b { color: var(--text-primary); font-weight: 650; }
    .mc-active { display: inline-flex; flex-wrap: wrap; gap: 4px 10px; min-width: 0; color: var(--text-primary); font-size: var(--font-size-sm); font-weight: 600; }
    .mc-active:empty { display: none; }
    .mc-active span + span::before { content: "\00B7"; margin-right: 10px; color: var(--text-muted); font-weight: 400; }
    .mc-bar .mc-to-filters { visibility: hidden; }
    .mc-bar.is-stuck .mc-to-filters { visibility: visible; }
    .mc-filters { margin-top: -18px; padding: 4px 16px 16px; border: 1px solid var(--ui-border-1); border-top: 0; border-bottom-left-radius: var(--radius-lg); border-bottom-right-radius: var(--radius-lg); background: var(--bg-card, var(--bg-secondary)); min-width: 0; }
    .mc-groups { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 10px 26px; min-width: 0; padding-top: 12px; border-top: 1px solid var(--ui-border-1); }
    .mc-root > [data-mc-part]:empty { display: none; }
    .mc-group { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; min-width: 0; margin: 0; padding: 0; border: 0; }
    .mc-group__label { margin-right: 4px; color: var(--text-muted); font-size: var(--font-size-xs); font-weight: 650; letter-spacing: .06em; text-transform: uppercase; }
    button.mc-chip { display: inline-flex; align-items: center; gap: 7px; min-height: 30px; padding: 4px 12px; border: 1px solid var(--ui-border-2); border-radius: 999px; background: transparent; color: var(--text-secondary); font-size: var(--font-size-sm); font-weight: 600; white-space: nowrap; box-shadow: none; cursor: pointer; }
    button.mc-chip:hover:not(:disabled) { background: var(--ui-surface-2); color: var(--text-primary); filter: none; }
    button.mc-chip[aria-pressed="true"] { background: var(--accent-subtle); border-color: var(--accent); color: var(--text-primary); }
    button.mc-chip:disabled { opacity: .45; cursor: not-allowed; }
    button.mc-chip.is-zero:not([aria-pressed="true"]) { color: var(--text-muted); border-style: dashed; }
    button.mc-chip:focus-visible, .mc-id:focus-visible, .mc-search:focus-visible { outline: 2px solid var(--info); outline-offset: 2px; box-shadow: none; }
    .mc-chip__n { color: var(--text-muted); font-size: var(--font-size-xs); font-weight: 600; font-variant-numeric: tabular-nums; }
    button.mc-chip[aria-pressed="true"] .mc-chip__n { color: var(--text-secondary); }
    .mc-seam strong { font-weight: 650; }
    /* Cards: one per model. */
    .mc-list { display: grid; gap: 18px; grid-template-columns: minmax(0, 1fr); align-items: start; min-width: 0; }
    .mc-card { container: mc-card / inline-size; display: grid; gap: 14px; min-width: 0; padding: 18px 20px 12px; }
    .mc-card.is-starter { border-color: var(--accent-border, var(--ui-border-2)); }
    .mc-card__head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; min-width: 0; }
    .mc-card__head .ui-mark { flex: 0 0 40px; }
    .mc-card__titles { display: grid; gap: 4px; min-width: 0; flex: 1 1 320px; }
    .mc-card__title { margin: 0; font-size: var(--font-size-lg); font-weight: 650; line-height: 1.25; color: var(--text-primary); letter-spacing: -.01em; }
    .mc-card__meta { color: var(--text-secondary); font-size: var(--font-size-sm); }
    .mc-card__meta span + span::before { content: "\00B7"; margin: 0 7px; color: var(--text-muted); }
    .mc-card__badges { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
    .mc-badge { display: inline-flex; align-items: center; padding: 3px 10px; border-radius: 999px; font-size: var(--font-size-xs); font-weight: 650; white-space: nowrap; color: var(--accent); background: var(--accent-subtle); border: 1px solid var(--accent-border, var(--ui-border-2)); }
    .mc-tags { display: flex; flex-wrap: wrap; gap: 6px; margin: -4px 0 0 54px; padding: 0; list-style: none; }
    @container mc-card (max-width: 560px) { .mc-tags { margin-left: 0; } }
    .mc-tag { padding: 2px 9px; border-radius: 999px; background: var(--ui-surface-2); color: var(--text-secondary); font-size: var(--font-size-xs); font-weight: 600; white-space: nowrap; }
    .mc-note { color: var(--text-secondary); font-size: var(--font-size-sm); line-height: 1.45; }
    /* Artifact rows: a grid per row with fixed columns, so the columns line
       up across every card. Narrow cards (container query) fold each row
       onto two lines instead of squeezing a column. */
    .mc-arts { display: grid; margin: 0 -10px; padding: 0; list-style: none; min-width: 0; }
    .mc-art { display: grid; align-items: center; gap: 8px 14px; min-width: 0; padding: 11px 10px; border-radius: var(--radius-md);
      grid-template-columns: 112px minmax(0, 2.6fr) repeat(4, minmax(104px, 1fr)) 172px;
      grid-template-areas: "prov id quant size weights fit action" ". job job job job job job"; }
    .mc-art + .mc-art { border-top: 1px solid var(--ui-border-1); border-top-left-radius: 0; border-top-right-radius: 0; }
    .mc-art.is-primary { background: var(--ui-surface-1); box-shadow: inset 3px 0 0 var(--accent); }
    .mc-art.is-primary + .mc-art { border-top-color: transparent; }
    .mc-art__prov { grid-area: prov; min-width: 0; }
    .mc-prov { display: inline-flex; max-width: 100%; padding: 3px 9px; border-radius: var(--radius-sm); background: var(--ui-surface-2); border: 1px solid var(--ui-border-1); color: var(--text-primary); font-size: var(--font-size-xs); font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .mc-art__id { grid-area: id; display: grid; gap: 3px; min-width: 0; }
    .mc-id { display: block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; overflow-wrap: normal; word-break: normal; font-family: var(--font-mono); font-size: var(--font-size-sm); color: var(--text-primary); background: transparent; border: 0; padding: 0; cursor: copy; }
    .mc-art:not(.is-primary) .mc-id { color: var(--text-secondary); }
    .mc-rec { display: inline-flex; align-items: center; gap: 6px; color: var(--text-secondary); font-size: var(--font-size-xs); font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .mc-rec::before { content: ""; flex: 0 0 6px; width: 6px; height: 6px; border-radius: 999px; background: var(--accent); }
    .mc-art__facts { display: contents; }
    .mc-art__quant { grid-area: quant; display: grid; gap: 1px; min-width: 0; font-size: var(--font-size-sm); }
    .mc-art__quant b { color: var(--text-primary); font-weight: 650; white-space: nowrap; }
    .mc-art__quant span, .mc-art__size span { color: var(--text-muted); font-size: var(--font-size-xs); white-space: nowrap; font-variant-numeric: tabular-nums; }
    .mc-art__size { grid-area: size; display: grid; gap: 1px; color: var(--text-primary); font-size: var(--font-size-sm); font-variant-numeric: tabular-nums; white-space: nowrap; }
    .mc-art__weights { grid-area: weights; min-width: 0; }
    .mc-art__fit { grid-area: fit; min-width: 0; }
    .mc-art__fit .ui-pill[title] { cursor: help; }
    .mc-art__action { grid-area: action; display: flex; justify-content: flex-end; align-items: center; gap: 8px; min-width: 0; }
    .mc-art__action .ui-btn { min-height: 32px; padding: 6px 14px; white-space: nowrap; }
    .mc-art__action .mc-muted { color: var(--text-muted); font-size: var(--font-size-xs); text-align: right; }
    .mc-art__job { grid-area: job; display: grid; gap: 8px; min-width: 0; }
    .mc-art__job:empty { display: none; }
    .mc-art:not(.is-primary) .ui-pill.tone-muted { opacity: .85; }
    @container mc-card (max-width: 1060px) {
      .mc-art { grid-template-columns: 104px minmax(0, 1fr) auto; grid-template-areas: "prov id action" "facts facts facts" "job job job"; }
      .mc-art__facts { grid-area: facts; display: flex; flex-wrap: wrap; align-items: center; gap: 6px 18px; min-width: 0; padding-left: 118px; }
      .mc-art__quant, .mc-art__size { display: flex; align-items: baseline; gap: 6px; }
    }
    @container mc-card (max-width: 560px) {
      .mc-art { grid-template-columns: minmax(0, 1fr) auto; grid-template-areas: "prov action" "id id" "facts facts" "job job"; }
      .mc-art__facts { padding-left: 0; }
    }
    .mc-empty { display: grid; justify-items: center; gap: 10px; }
    .mc-empty strong { color: var(--text-primary); font-weight: 650; }
    .mc-installed { display: grid; gap: 12px; min-width: 0; margin-top: 28px; }
    /* AbstractCore's Models screen stays mounted below the cards for what the
       engines hold (including models outside the catalog) and Delete; its own
       catalog table, filters and host line are the cards' job now. */
    #catalog-core-root .acc-host-line, #catalog-core-root .acc-toolbar, #catalog-core-root .acc-section:has([data-acc="catalog-table"]),
    #catalog-core-root .acc-section-head h3 { display: none; }
"""

CATALOG_JS = r"""
    // ================= Model catalog as cards (mission X2) =================
    // Contract: GET /api/gateway/models/catalog (AbstractCore model_catalog_v1:
    // rows[{id, display_name, vendor, params_total, params_active, license,
    // capabilities, starter, artifacts[{provider, artifact, engine, quant,
    // quant_class, bits, download_bytes, size_source, presence{status,...},
    // fit{verdict, notes,...}, supported_on_host, downloadable, recommended}]}],
    // host_profile). Downloads: POST /api/gateway/models/download
    // {provider, artifact}, tracked by the console's ONE download feed
    // (console_ui.py: trackDownloadJob / state.downloadJobs / dlCancel).
    const MC_QUANT_CLASSES = ["2bit", "3bit", "4bit", "5bit", "6bit", "8bit", "16bit", "full", "unknown"];
    const MC_QUANT_LABEL = { "2bit": "2-bit", "3bit": "3-bit", "4bit": "4-bit", "5bit": "5-bit", "6bit": "6-bit", "8bit": "8-bit", "16bit": "16-bit", full: "Full precision", unknown: "Not stated" };
    const MC_QUANT_CHIPS = [["all", "All"], ["4bit", "4-bit"], ["8bit", "8-bit"], ["other", "Other"]];
    const MC_STATUS_CHIPS = [["all", "All"], ["downloaded", "Downloaded"], ["not_downloaded", "Not downloaded"]];
    const MC_CAPS = [["text", "Text"], ["thinking", "Thinking"], ["tools", "Tools"], ["vision", "Vision"], ["audio", "Audio"], ["embedding", "Embedding"], ["voice", "Voice"], ["image", "Image"]];
    const MC_PROVIDER_LABEL = { ollama: "Ollama", lmstudio: "LM Studio", mlx: "MLX", "mlx-gen": "MLX images", "mlx-vlm": "MLX vision", huggingface: "Hugging Face", diffusers: "Diffusers", supertonic: "Supertonic", llamacpp: "llama.cpp" };
    const MC_WEIGHTS = { installed: ["Downloaded", "ok"], absent: ["Not downloaded", "muted"], unknown: ["Unknown", "muted"], not_applicable: ["Remote", "muted"] };
    const MC_FIT = { fits: ["Fits", "ok"], tight: ["Tight", "warn"], partial_offload: ["Partial offload", "warn"], too_large: ["Too large", "err"], unknown: ["Fit unknown", "muted"] };
    const MC_ENGINE_PROVIDER = { mlx: "mlx", ollama: "ollama", lmstudio: "lmstudio", huggingface: "huggingface", llamacpp: "huggingface" };
    const mcStore = { data: null, error: "", loading: false, seq: 0, views: new Map(), busy: new Set(), notices: new Map(), reloadFor: new Set(), reloadTimer: null,
      hub: { q: null, data: null, error: "", loading: false, seq: 0 } };
    // `hf`: null = the curated catalog; a string = Hugging Face mode and its query.
    function mcDefaultFilters() { return { q: "", quant: "all", provider: "all", cap: "all", status: "all", fits: false, hf: null }; }
    function mcHfMode(f) { return typeof (f || {}).hf === "string"; }
    // ---- URL hash: `#catalog?q=...&quant=8bit&provider=mlx&cap=vision&status=downloaded&fits=1`
    function mcParseHash(hash) {
      const f = mcDefaultFilters();
      const m = /^#catalog(?:\?(.*))?$/.exec(String(hash || ""));
      if (!m || !m[1]) return f;
      for (const part of m[1].split("&")) {
        const eq = part.indexOf("=");
        if (eq < 1) continue;
        let key = ""; let value = "";
        try { key = decodeURIComponent(part.slice(0, eq)); value = decodeURIComponent(part.slice(eq + 1).replace(/\+/g, " ")); } catch { continue; }
        if (key === "q") f.q = value;
        else if (key === "quant" && ["4bit", "8bit", "other"].includes(value)) f.quant = value;
        else if (key === "provider" && value) f.provider = value;
        else if (key === "cap" && MC_CAPS.some((c) => c[0] === value)) f.cap = value;
        else if (key === "status" && ["downloaded", "not_downloaded"].includes(value)) f.status = value;
        else if (key === "fits") f.fits = value === "1" || value === "true";
        else if (key === "hf") f.hf = value;
      }
      return f;
    }
    function mcHashFor(f) {
      const parts = [];
      if (f.q) parts.push(`q=${encodeURIComponent(f.q)}`);
      if (f.quant !== "all") parts.push(`quant=${encodeURIComponent(f.quant)}`);
      if (f.provider !== "all") parts.push(`provider=${encodeURIComponent(f.provider)}`);
      if (f.cap !== "all") parts.push(`cap=${encodeURIComponent(f.cap)}`);
      if (f.status !== "all") parts.push(`status=${encodeURIComponent(f.status)}`);
      if (f.fits) parts.push("fits=1");
      if (mcHfMode(f)) parts.push(`hf=${encodeURIComponent(f.hf)}`);
      return parts.length ? `#catalog?${parts.join("&")}` : "#catalog";
    }
    function mcWriteHash(view) {
      if (!view.syncHash || state.activeTab !== "catalog") return;
      const next = mcHashFor(view.filters);
      try {
        if (String(location.hash || "") === next) return;
        if (typeof history !== "undefined" && history && typeof history.replaceState === "function") {
          history.replaceState(null, "", String(location.pathname || "/console") + String(location.search || "") + next);
        }
      } catch { /* the view still filters; only the shareable link is missing */ }
    }
    // Leaving the Models tab drops its `#catalog...` link, so a reload lands
    // where the operator is, not back on the catalog. Before the tab was ever
    // opened the link is still to be read (a `#catalog?...` deep link at boot).
    function mcOnTabChange(tab) {
      const view = mcStore.views.get("tab");
      if (!view) return;
      if (tab === "catalog") { mcWriteHash(view); return; }
      try {
        if (/^#catalog(\?|$)/.test(String(location.hash || "")) && typeof history !== "undefined" && history && typeof history.replaceState === "function") {
          history.replaceState(null, "", String(location.pathname || "/console") + String(location.search || ""));
        }
      } catch { /* nothing to drop */ }
    }
    // ---- Payload readers (server truth; nothing is re-derived) ----
    // The rows a view draws: the curated catalog, or (Hugging Face mode) the
    // Hub answer for the view's query. `mcSrc` is set while one view renders.
    let mcSrc = null;
    function mcViewData(view) {
      if (!mcHfMode(view.filters)) return mcStore.data;
      const hub = mcStore.hub;
      return hub.q === view.filters.hf ? hub.data : null;
    }
    function mcWithView(view, fn) {
      const before = mcSrc;
      mcSrc = { data: mcViewData(view) };
      try { return fn(); } finally { mcSrc = before; }
    }
    function mcData() { return mcSrc ? mcSrc.data : mcStore.data; }
    function mcRowsOf(d) { return (d && Array.isArray(d.rows)) ? d.rows.filter((r) => r && typeof r === "object") : []; }
    function mcRows() { return mcRowsOf(mcData()); }
    function mcArts(row) { return Array.isArray(row.artifacts) ? row.artifacts.filter((a) => a && typeof a === "object") : []; }
    function mcKey(a) { return downloadJobKey(a.provider, a.artifact); }
    function mcJob(a) { return state.downloadJobs.get(mcKey(a)) || null; }
    function mcJobDone(job) { return !!job && (job.state === "done" || job.status === "completed"); }
    function mcInstalled(a) { return ((a.presence || {}).status === "installed") || mcJobDone(mcJob(a)); }
    function mcFits(a) { return a.supported_on_host !== false && ["fits", "tight"].includes(String((a.fit || {}).verdict || "")); }
    // quant_class is AbstractCore's (mission W1). A payload where ANY artifact
    // lacks it cannot drive the quant filter: the view says so, loudly.
    function mcQuantReported() {
      const rows = mcRows();
      if (!rows.length) return true;
      return rows.every((r) => mcArts(r).every((a) => typeof a.quant_class === "string" && MC_QUANT_CLASSES.includes(a.quant_class)));
    }
    function mcQuantBucket(a) { return a.quant_class === "4bit" || a.quant_class === "8bit" ? a.quant_class : "other"; }
    function mcRowCaps(row) {
      const c = row.capabilities || {};
      const out = [];
      if (c.text === true) out.push("text");
      if (c.thinking === true) out.push("thinking");
      if (c.tools === "native" || c.tools === "prompted") out.push("tools");
      if (c.vision === true) out.push("vision");
      if (c.audio === true && c.speech_synthesis !== true) out.push("audio");
      if (c.embedding === true) out.push("embedding");
      if (c.speech_synthesis === true) out.push("voice");
      if (c.image_generation === true) out.push("image");
      return out;
    }
    function mcProviderLabel(p) { return MC_PROVIDER_LABEL[p] || String(p || ""); }
    function mcParams(n) {
      if (!uiNum(n) || n <= 0) return "";
      if (n >= 1e9) return `${(n / 1e9).toFixed(n >= 1e10 ? 0 : 1).replace(/\.0$/, "")}B`;
      return `${Math.round(n / 1e6)}M`;
    }
    function mcTokens(q) { return String(q || "").toLowerCase().split(/\s+/).filter(Boolean); }
    function mcSearchHit(row, a, tokens) {
      if (!tokens.length) return true;
      const hay = [row.display_name, row.id, row.vendor, row.family, a.artifact].map((x) => String(x || "").toLowerCase()).join(" ");
      return tokens.every((t) => hay.includes(t));
    }
    function mcArtMatches(row, a, f, quantOn, tokens) {
      if (quantOn && f.quant !== "all" && mcQuantBucket(a) !== f.quant) return false;
      if (f.provider !== "all" && a.provider !== f.provider) return false;
      if (f.fits && !mcFits(a)) return false;
      if (f.status === "downloaded" && !mcInstalled(a)) return false;
      if (f.status === "not_downloaded" && mcInstalled(a)) return false;
      return mcSearchHit(row, a, tokens);
    }
    // The recommended artifact first; the rest in the catalog's own order.
    function mcOrdered(row) {
      const arts = mcArts(row);
      return arts.filter((a) => a.recommended).concat(arts.filter((a) => !a.recommended));
    }
    function mcVisible(f) {
      const quantOn = mcQuantReported();
      // Hugging Face mode: the Hub already searched for the query.
      const tokens = mcHfMode(f) ? [] : mcTokens(f.q);
      const out = [];
      for (const row of mcRows()) {
        if (f.cap !== "all" && !mcRowCaps(row).includes(f.cap)) continue;
        const arts = mcOrdered(row).filter((a) => mcArtMatches(row, a, f, quantOn, tokens));
        if (arts.length) out.push({ row, arts });
      }
      return out;
    }
    function mcCountArts(list) { return list.reduce((n, x) => n + x.arts.length, 0); }
    function mcPlural(n, one, many) { return `${n} ${n === 1 ? one : many}`; }
    // ---- Markup ----
    function mcHostMarkup() {
      const p = (mcStore.data && mcStore.data.host_profile) || null;
      if (!p) return "";
      const bits = [];
      const chip = p.gpu_name || p.accelerator || "";
      if (chip) bits.push(`<b>${esc(chip)}</b>`);
      if (uiNum(p.ram_bytes)) bits.push(`${esc(uiBytes(p.ram_bytes))} ${p.unified_memory ? "unified memory" : "memory"}`);
      if (uiNum(p.ceiling_bytes)) bits.push(`models up to about ${esc(uiBytes(p.ceiling_bytes))}`);
      return bits.length ? `<span class="mc-host__facts">This computer: ${bits.join(" · ")}</span>` : "";
    }
    function mcChip(group, value, label, on, count, disabled) {
      const n = count === null || count === undefined ? "" : `<span class="mc-chip__n">${esc(count)}</span>`;
      return `<button type="button" class="mc-chip${count === 0 ? " is-zero" : ""}" data-mc-filter="${esc(group)}" data-mc-value="${esc(value)}" aria-pressed="${on ? "true" : "false"}"${disabled ? " disabled" : ""}>${esc(label)}${n}</button>`;
    }
    // A chip's count = the artifacts the view would show with that chip on.
    function mcChipCount(view, group, value) {
      const f = Object.assign({}, view.filters, { [group]: value });
      return mcCountArts(mcVisible(f));
    }
    function mcControlsMarkup(view) {
      const f = view.filters;
      const quantOn = mcQuantReported();
      const rows = mcRows();
      const providers = [];
      for (const r of rows) for (const a of mcArts(r)) if (a.provider && !providers.includes(a.provider)) providers.push(a.provider);
      if (f.provider !== "all" && !providers.includes(f.provider)) providers.push(f.provider);
      const caps = MC_CAPS.filter(([id]) => rows.some((r) => mcRowCaps(r).includes(id)) || f.cap === id);
      const quant = MC_QUANT_CHIPS.map(([id, label]) => mcChip("quant", id, label, quantOn && f.quant === id, quantOn && id !== "all" ? mcChipCount(view, "quant", id) : null, !quantOn)).join("");
      const prov = [["all", "All"]].concat(providers.map((p) => [p, mcProviderLabel(p)])).map(([id, label]) => mcChip("provider", id, label, f.provider === id, id === "all" ? null : mcChipCount(view, "provider", id), false)).join("");
      const cap = [["all", "All"]].concat(caps).map(([id, label]) => mcChip("cap", id, label, f.cap === id, id === "all" ? null : mcChipCount(view, "cap", id), false)).join("");
      const status = MC_STATUS_CHIPS.map(([id, label]) => mcChip("status", id, label, f.status === id, id === "all" ? null : mcChipCount(view, "status", id), false)).join("");
      const group = (label, body, extra) => `<div class="mc-group" role="group" aria-label="${esc(label)}"${extra || ""}><span class="mc-group__label" aria-hidden="true">${esc(label)}</span>${body}</div>`;
      return `<div class="mc-groups">`
        + group("Quantization", quant, quantOn ? "" : ' data-mc-quant="unreported"')
        + group("Provider", prov)
        + group("Capability", cap)
        + group("Status", status)
        + `</div>`;
    }
    function mcCountMarkup(view, list) {
      const total = mcRows().length;
      return `<b>${esc(list.length)}</b> of ${esc(mcPlural(total, "model", "models"))} · <b>${esc(mcCountArts(list))}</b> ${mcCountArts(list) === 1 ? "artifact" : "artifacts"} shown`;
    }
    // The filters in use, in words, on the sticky row (the chips scroll away).
    function mcActiveMarkup(view) {
      const f = view.filters;
      const out = [];
      if (mcHfMode(f)) out.push(f.hf ? `Hugging Face: “${f.hf}”` : "Hugging Face");
      else if (f.q) out.push(`“${f.q}”`);
      if (f.quant !== "all" && mcQuantReported()) out.push((MC_QUANT_CHIPS.find((c) => c[0] === f.quant) || [f.quant, f.quant])[1]);
      if (f.provider !== "all") out.push(mcProviderLabel(f.provider));
      if (f.cap !== "all") out.push((MC_CAPS.find((c) => c[0] === f.cap) || [f.cap, f.cap])[1]);
      if (f.status !== "all") out.push((MC_STATUS_CHIPS.find((c) => c[0] === f.status) || [f.status, f.status])[1]);
      return out.map((t) => `<span>${esc(t)}</span>`).join("");
    }
    // Hugging Face mode: the Hub answered only in part (offline, rate limited).
    function mcHubNoticeMarkup(view) {
      const d = mcData();
      const hub = mcHfMode(view.filters) && d && d.hub;
      if (!hub || hub.ok !== false) return "";
      const errs = Array.isArray(hub.errors) ? hub.errors.map(String).filter(Boolean) : [];
      return `<div class="ui-alert tone-warn" role="alert" data-mc-notice="hub"><strong>Hugging Face could not be reached, so these results may be incomplete.</strong>`
        + `<span>Check that the gateway host is online, then search again.</span>`
        + (errs.length ? `<details class="ui-details"><summary>Show details</summary><pre class="ui-log">${esc(errs.join("\n"))}</pre></details>` : "")
        + `</div>`;
    }
    function mcNoticeMarkup(view) {
      const hub = view ? mcHubNoticeMarkup(view) : "";
      if (!mcData() || mcQuantReported()) return hub;
      return hub + mcQuantNoticeMarkup();
    }
    function mcQuantNoticeMarkup() {
      return `<div class="ui-alert tone-warn mc-seam" role="alert" data-mc-notice="quant_class"><strong>This gateway's catalog does not report quant_class yet.</strong>`
        + `<span>The 4-bit / 8-bit filter needs it, so it is off. Every model and every artifact is still listed below. Updating AbstractCore on the gateway host turns the filter on.</span></div>`;
    }
    function mcFitTitle(fit) {
      const f = fit || {};
      const lines = [];
      if (uiNum(f.need_bytes) && uiNum(f.ceiling_bytes)) lines.push(`Needs about ${uiBytes(f.need_bytes)} of the ${uiBytes(f.ceiling_bytes)} this computer can give a model`);
      if (uiNum(f.free_now_bytes)) lines.push(`Free right now: ${uiBytes(f.free_now_bytes)}`);
      if (f.disk_ok === false) lines.push("Not enough free disk space for the download");
      if (uiNum(f.max_context)) lines.push(`Longest context that fits: ${Number(f.max_context).toLocaleString()} tokens`);
      for (const note of Array.isArray(f.notes) ? f.notes : []) lines.push(String(note));
      return lines.join("\n");
    }
    function mcCurrentDefault() {
      const text = (state.defaults || []).find((r) => r && r.key === "output.text") || null;
      return text && text.provider && text.model ? { provider: String(text.provider), model: String(text.model) } : null;
    }
    function mcCanBeDefault(row) { const c = row.capabilities || {}; return c.text === true && c.embedding !== true; }
    function mcActionMarkup(row, a, job) {
      const key = mcKey(a);
      const admin = !!(state.principal && state.principal.admin);
      const attrs = `data-provider="${esc(a.provider)}" data-artifact="${esc(a.artifact)}"`;
      if (job && dlActive(job)) {
        const jid = dlJobId(job);
        const cancelling = dlFeed.cancelling.has(jid);
        return `<button type="button" class="ui-btn is-ghost" data-mc-action="cancel" data-job="${esc(jid)}"${cancelling ? " disabled" : ""}>${cancelling ? "Cancelling..." : "Cancel"}</button>`;
      }
      if (mcStore.busy.has(key)) return `<button type="button" class="ui-btn is-ghost" disabled aria-busy="true">Starting...</button>`;
      if (mcInstalled(a)) {
        if (!mcCanBeDefault(row)) return "";
        const cur = mcCurrentDefault();
        if (cur && cur.provider === a.provider && cur.model === servedModelId(a.provider, a.artifact)) return uiPill("Default text model", "info");
        return `<button type="button" class="ui-btn is-ghost" data-mc-action="default" ${attrs}${admin ? "" : ' disabled title="Only an admin can change the default model"'}>Use as default</button>`;
      }
      if (!a.downloadable) {
        const why = a.supported_on_host === false ? "Its engine does not run on this computer" : "This build cannot be downloaded from here";
        return `<span class="mc-muted" title="${esc(why)}">Not available here</span>`;
      }
      const label = job && job.status === "failed" ? "Try again" : "Download";
      const tone = a.recommended ? "is-primary" : "is-ghost";
      return `<button type="button" class="ui-btn ${tone}" data-mc-action="download" ${attrs}${admin ? "" : ' disabled title="Only an admin can download models"'}>${label}</button>`;
    }
    function mcJobMarkup(a, job) {
      const key = mcKey(a);
      const notice = mcStore.notices.get(key);
      let out = "";
      if (job && dlActive(job)) {
        const phase = uiJobPhase(job);
        out += uiProgressMarkup(job, job.parent_job ? `${UI_PHASE_LABELS[phase] || "Downloading"} · part of Download all` : (UI_PHASE_LABELS[phase] || "Downloading"));
      } else if (job && job.status === "failed") {
        const why = String(job.error || "").trim();
        out += `<div class="ui-alert tone-err" role="alert"><strong>The download did not finish.</strong><span>${esc(job.message || "Try again.")}</span></div>`
          + (why && why !== String(job.message || "").trim() ? `<details class="ui-details"><summary>Show details</summary><pre class="ui-log">${esc(why)}</pre></details>` : "");
      } else if (job && job.status === "cancelled" && !mcInstalled(a)) {
        out += `<div class="mc-note">Download cancelled. Download it again any time.</div>`;
      }
      if (notice) out += `<div class="ui-alert tone-${esc(notice.tone)}" role="status">${esc(notice.text)}</div>`;
      return out;
    }
    function mcArtMarkup(row, a, alone) {
      const job = mcJob(a);
      const presence = a.presence || {};
      const installed = mcInstalled(a);
      const [wLabel, wTone] = installed ? MC_WEIGHTS.installed : (MC_WEIGHTS[presence.status] || MC_WEIGHTS.unknown);
      const wTitle = [presence.location, presence.evidence].filter(Boolean).join(" · ");
      const weights = job && dlActive(job) ? dlStatePill(job) : uiPill(wLabel, wTone, wTitle);
      const verdict = String((a.fit || {}).verdict || "unknown");
      const [fLabel, fTone] = a.supported_on_host === false ? ["Not for this computer", "muted"] : (MC_FIT[verdict] || MC_FIT.unknown);
      const quantOn = typeof a.quant_class === "string" && MC_QUANT_CLASSES.includes(a.quant_class);
      const raw = a.quant ? String(a.quant) : "";
      const bits = uiNum(a.bits) ? `${a.bits} bits` : "";
      // With quant_class: its label ("8-bit"). Without it: the catalog's own
      // quant string as sent -- never a class guessed from that string.
      const quantMain = quantOn ? (MC_QUANT_LABEL[a.quant_class] || a.quant_class) : (raw || "Not stated");
      const quantSub = bits;
      const exact = ["catalog", "hf_api", "engine"].includes(String(a.size_source || ""));
      const size = uiNum(a.download_bytes) ? `${exact ? "" : "about "}${uiBytes(a.download_bytes)}` : "Size unknown";
      const sizeTitle = uiNum(a.download_bytes) ? (exact ? "Download size" : "Estimated from the parameter count and quantization") : "The catalog has no size for this build";
      const primary = !!a.recommended;
      // The label only means something next to other builds of the same model.
      const rec = primary && !alone ? `<span class="mc-rec">Recommended for this computer</span>` : "";
      return `<li class="mc-art${primary ? " is-primary" : ""}" data-mc-art="${esc(mcKey(a))}" data-provider="${esc(a.provider)}" data-quant-class="${esc(quantOn ? a.quant_class : "")}">`
        + `<div class="mc-art__prov"><span class="mc-prov" title="${esc(a.engine && a.engine !== a.provider ? `${mcProviderLabel(a.provider)} (runs on ${mcProviderLabel(a.engine)})` : mcProviderLabel(a.provider))}">${esc(mcProviderLabel(a.provider))}</span></div>`
        + `<div class="mc-art__id"><code class="mc-id" tabindex="0" role="button" title="${esc(a.artifact)} (click to copy)" data-mc-copy="${esc(a.artifact)}">${esc(a.artifact)}</code>${rec}</div>`
        + `<div class="mc-art__facts">`
        + `<div class="mc-art__quant" title="${esc(raw ? `Quantization: ${raw}` : "The catalog does not name a quantization")}"><b>${esc(quantMain)}</b>${quantSub ? `<span>${esc(quantSub)}</span>` : ""}</div>`
        + `<div class="mc-art__size" title="${esc(sizeTitle)}">${esc(size)}</div>`
        + `<div class="mc-art__weights">${weights}</div>`
        + `<div class="mc-art__fit">${uiPill(fLabel, fTone, mcFitTitle(a.fit))}</div>`
        + `</div>`
        + `<div class="mc-art__action">${mcActionMarkup(row, a, job)}</div>`
        + `<div class="mc-art__job">${mcJobMarkup(a, job)}</div>`
        + `</li>`;
    }
    // The card's mark: two letters of the model's name, like the engine and
    // app cards' marks ("Qw", "Ge", "Ll").
    function mcMark(row) {
      const name = String(row.display_name || row.id || "?").replace(/[^A-Za-z0-9]/g, "");
      return name ? name.charAt(0).toUpperCase() + name.charAt(1).toLowerCase() : "?";
    }
    function mcCardMarkup(item) {
      const row = item.row;
      const params = mcParams(row.params_total);
      const active = mcParams(row.params_active);
      const meta = [row.vendor, params ? `${params} parameters${active ? ` (${active} active)` : ""}` : "", row.license].filter(Boolean);
      const caps = mcRowCaps(row).map((c) => (MC_CAPS.find((x) => x[0] === c) || [c, c])[1]);
      const badges = [];
      if (row.starter) badges.push(`<span class="mc-badge" title="Part of the recommended starter set for this computer">Starter</span>`);
      if (row.source === "hf_search") badges.push(`<span class="mc-badge">Hugging Face</span>`);
      return `<article class="ui-card mc-card${row.starter ? " is-starter" : ""}" data-mc-model="${esc(row.id)}">`
        + `<header class="mc-card__head"><span class="ui-mark" aria-hidden="true">${esc(mcMark(row))}</span><div class="mc-card__titles"><h4 class="mc-card__title">${esc(row.display_name || row.id)}</h4>`
        + (meta.length ? `<div class="mc-card__meta">${meta.map((m) => `<span>${esc(m)}</span>`).join("")}</div>` : "")
        + `</div>${badges.length ? `<div class="mc-card__badges">${badges.join("")}</div>` : ""}</header>`
        + (caps.length ? `<ul class="mc-tags" aria-label="Capabilities">${caps.map((c) => `<li class="mc-tag">${esc(c)}</li>`).join("")}</ul>` : "")
        + `<ul class="mc-arts">${item.arts.map((a) => mcArtMarkup(row, a, mcArts(row).length === 1)).join("")}</ul>`
        + `</article>`;
    }
    function mcListMarkup(view, list) {
      if (mcHfMode(view.filters)) {
        const hub = mcStore.hub;
        const q = view.filters.hf;
        if (!q) return `<div class="ui-empty mc-empty" data-mc-hf="prompt"><strong>Search Hugging Face</strong><span>Type a model name above and press Enter. Results show as cards you can download, like the catalog.</span></div>`;
        if (hub.q === q && hub.error && !hub.data) {
          return `<div class="ui-alert tone-err" role="alert" data-mc-hf="error"><strong>Hugging Face could not be searched right now.</strong><span>Check that the gateway host is online, then search again.</span>`
            + `<details class="ui-details"><summary>Show details</summary><pre class="ui-log">${esc(hub.error)}</pre></details>`
            + `<div class="ui-card__actions"><button type="button" class="ui-btn is-ghost" data-mc-action="hf-search">Search again</button></div></div>`;
        }
        if (!mcData()) return `<div class="ui-empty" data-mc-hf="loading">Searching Hugging Face for “${esc(q)}”...</div>`;
        if (!mcRows().length) {
          return `<div class="ui-empty mc-empty" data-mc-empty="hf"><strong>Hugging Face has no model matching “${esc(q)}”.</strong><span>Try another name, or fewer words.</span></div>`;
        }
      }
      if (mcStore.error && !mcStore.data) {
        return `<div class="ui-alert tone-err" role="alert"><strong>The model catalog did not load.</strong><span>${esc(mcStore.error)}</span></div>`;
      }
      if (!mcStore.data) return `<div class="ui-empty">Loading the model catalog...</div>`;
      if (!list.length) {
        return `<div class="ui-empty mc-empty" data-mc-empty="1"><strong>No model matches these filters.</strong>`
          + `<span>${esc(mcRows().length ? "Change or clear the filters to see the rest of the catalog." : "This gateway's catalog is empty.")}</span>`
          + (mcRows().length ? `<button type="button" class="ui-btn is-ghost" data-mc-action="clear">Clear filters</button>` : "") + `</div>`;
      }
      return list.map(mcCardMarkup).join("");
    }
    function mcSearchPlaceholder(f) { return mcHfMode(f) ? "Search Hugging Face, then press Enter" : "Search by model, organisation or artifact id"; }
    function mcModeMarkup(view) {
      const hf = mcHfMode(view.filters);
      return `<button type="button" data-mc-action="mode" data-mc-mode="catalog" aria-pressed="${hf ? "false" : "true"}">Catalog</button>`
        + `<button type="button" data-mc-action="mode" data-mc-mode="hf" aria-pressed="${hf ? "true" : "false"}">Hugging Face</button>`;
    }
    function mcShellMarkup(view) {
      const f = view.filters;
      return `<div class="mc-root" data-mc-view="${esc(view.key)}">`
        + `<div class="mc-host" data-mc-part="host"></div>`
        + `<div class="mc-bar" role="search" aria-label="Filter the model catalog">`
        + `<div class="mc-mode" role="group" aria-label="Search in" data-mc-part="mode"></div>`
        + `<input type="search" class="mc-search" data-mc-search="1" value="${esc(mcHfMode(f) ? f.hf : f.q)}" placeholder="${esc(mcSearchPlaceholder(f))}" aria-label="${esc(mcHfMode(f) ? "Search Hugging Face" : "Search the model catalog")}" autocomplete="off" spellcheck="false">`
        + `<button type="button" class="ui-btn is-ghost mc-hf-go" data-mc-action="hf-search"${mcHfMode(f) ? "" : " hidden"}>Search</button>`
        + `<label class="ui-switch"><input type="checkbox" data-mc-fits="1"${f.fits ? " checked" : ""}><span>Fits this computer</span></label>`
        + `<div class="mc-bar__end"><span class="mc-active" data-mc-part="active"></span><span class="mc-count" role="status" aria-live="polite" data-mc-part="count"></span>`
        + `<button type="button" class="ui-btn is-quiet mc-to-filters" data-mc-action="filters">Filters</button></div></div>`
        + `<div class="mc-filters" data-mc-part="controls"></div>`
        + `<div data-mc-part="notice"></div>`
        + `<div data-mc-part="message"></div>`
        + `<div class="mc-list" data-mc-part="list"></div>`
        + `</div>`;
    }
    function mcPart(view, name) {
      const el = view.el;
      return el && typeof el.querySelector === "function" ? el.querySelector(`[data-mc-part="${name}"]`) : null;
    }
    function mcRenderView(view) { return mcWithView(view, () => mcRenderView_(view)); }
    function mcRenderView_(view) {
      const el = view.el;
      if (!el) return;
      const have = !!mcData();
      const list = have ? mcVisible(view.filters) : [];
      const parts = {
        host: mcHostMarkup() + `<button type="button" class="ui-btn is-quiet" data-mc-action="refresh">${mcStore.loading ? "Checking..." : "Check again"}</button>`
          + (view.guide ? `<button type="button" class="ui-btn is-quiet" data-mc-action="open-tab">Open in the Models tab</button>` : ""),
        mode: mcModeMarkup(view),
        count: have ? mcCountMarkup(view, list) : "",
        active: mcActiveMarkup(view),
        controls: have ? mcControlsMarkup(view) : "",
        notice: mcNoticeMarkup(view),
        message: view.message ? `<div class="ui-alert tone-${esc(view.message.tone)}" role="status">${esc(view.message.text)}</div>` : "",
        list: mcListMarkup(view, list),
      };
      if (!mcPart(view, "list")) {
        // First paint (or a host without querySelector): the whole view.
        el.innerHTML = mcShellMarkup(view).replace(/(data-mc-part="(\w+)"[^>]*>)/g, (all, open, name) => open + (parts[name] || ""));
        return;
      }
      // Later paints keep the search box and the fits switch (focus, caret)
      // and redraw the rest; a focused chip keeps the focus.
      let focus = null;
      try {
        const a = document.activeElement;
        if (a && el.contains(a) && a.dataset && a.dataset.mcFilter) focus = [a.dataset.mcFilter, a.dataset.mcValue];
        else if (a && el.contains(a) && a.dataset && a.dataset.mcMode) focus = ["mode", a.dataset.mcMode];
      } catch { focus = null; }
      for (const name of Object.keys(parts)) {
        const p = mcPart(view, name);
        if (p && p.innerHTML !== parts[name]) p.innerHTML = parts[name];
      }
      const box = el.querySelector("[data-mc-fits]");
      if (box) box.checked = !!view.filters.fits;
      const search = el.querySelector("[data-mc-search]");
      const hf = mcHfMode(view.filters);
      const want = hf ? view.filters.hf : view.filters.q;
      if (search && search.value !== want && document.activeElement !== search) search.value = want;
      if (search) { search.placeholder = mcSearchPlaceholder(view.filters); search.setAttribute("aria-label", hf ? "Search Hugging Face" : "Search the model catalog"); }
      const go = el.querySelector(".mc-hf-go");
      if (go) go.hidden = !hf;
      if (focus) {
        const again = focus[0] === "mode"
          ? el.querySelector(`[data-mc-mode="${String(focus[1]).replace(/"/g, "")}"]`)
          : el.querySelector(`[data-mc-filter="${focus[0]}"][data-mc-value="${String(focus[1]).replace(/"/g, "")}"]`);
        if (again && typeof again.focus === "function") again.focus();
      }
    }
    function mcRender() { for (const view of mcStore.views.values()) mcRenderView(view); }
    // `is-stuck` once the chip panel has scrolled under the sticky row: the
    // row then gets its own bottom edge and a "Filters" button back up.
    function mcWatchStuck(view) {
      const el = view.el;
      if (view.watching || !el || typeof el.closest !== "function") return;
      const scroller = el.closest(".shell_content, .first-run-scroll");
      if (!scroller || typeof scroller.addEventListener !== "function") return;
      view.watching = true;
      let pending = false;
      view.checkStuck = () => {
        pending = false;
        const bar = el.querySelector(".mc-bar");
        const panel = el.querySelector(".mc-filters");
        if (!bar || !panel || !panel.getClientRects().length) return;
        bar.classList.toggle("is-stuck", panel.getBoundingClientRect().bottom <= bar.getBoundingClientRect().bottom + 1);
      };
      scroller.addEventListener("scroll", () => {
        if (pending) return;
        pending = true;
        requestAnimationFrame(view.checkStuck);
      }, { passive: true });
    }
    // A download tick redraws only the artifact rows whose markup changed
    // (the bar, the search box and the other cards stay as they are).
    function mcOnDownloads() {
      if (!mcStore.data) return;
      let reload = false;
      for (const job of state.downloadJobs.values()) {
        const id = dlJobId(job);
        if (mcJobDone(job) && id && !mcStore.reloadFor.has(id)) { mcStore.reloadFor.add(id); reload = true; }
      }
      for (const view of mcStore.views.values()) {
        const el = view.el;
        if (!el || typeof el.querySelectorAll !== "function") { mcRenderView(view); continue; }
        const byKey = new Map();
        for (const row of mcWithView(view, () => mcRows())) for (const a of mcArts(row)) byKey.set(mcKey(a), [row, a]);
        el.querySelectorAll("li[data-mc-art]").forEach((li) => {
          const hit = byKey.get(li.dataset.mcArt);
          if (!hit) return;
          const html = mcArtMarkup(hit[0], hit[1], mcArts(hit[0]).length === 1);
          if (li.outerHTML !== html) li.outerHTML = html;
        });
        const count = mcPart(view, "count");
        if (count && view.filters.status !== "all") mcRenderView(view);
      }
      if (reload) {
        // A finished download changes the counts and the Downloaded chip now;
        // the catalog reload that follows brings the engines' own answer.
        mcRender();
        clearTimeout(mcStore.reloadTimer);
        mcStore.reloadTimer = setTimeout(() => { mcLoad(); }, 800);
      }
    }
    async function mcLoad() {
      const seq = ++mcStore.seq;
      mcStore.loading = true;
      mcRender();
      try {
        const data = await api("/api/gateway/models/catalog", { slow: true });
        if (seq !== mcStore.seq) return;
        if (!data || data.schema !== "model_catalog_v1" || !Array.isArray(data.rows)) {
          throw new Error(`The gateway answered with an unexpected catalog (schema ${JSON.stringify((data && data.schema) || null)}, expected "model_catalog_v1").`);
        }
        mcStore.data = data;
        mcStore.error = "";
      } catch (err) {
        if (seq !== mcStore.seq) return;
        mcStore.error = String((err && err.message) || err);
      }
      mcStore.loading = false;
      mcRender();
    }
    // Hugging Face mode: the catalog route with `hub=true` and the query.
    // One request per Enter (the Hub rate-limits; AbstractCore caches 24 h).
    async function mcHubSearch(view, query) {
      const q = String(query || "").trim();
      view.filters = Object.assign({}, view.filters, { hf: q });
      mcWriteHash(view);
      if (!q) { mcRenderView(view); return; }
      const seq = ++mcStore.hub.seq;
      mcStore.hub = { q, data: null, error: "", loading: true, seq };
      mcRender();
      try {
        const data = await api(`/api/gateway/models/catalog?q=${encodeURIComponent(q)}&hub=true`, { slow: true });
        if (seq !== mcStore.hub.seq) return;
        if (!data || data.schema !== "model_catalog_v1" || !Array.isArray(data.rows)) {
          throw new Error(`The gateway answered with an unexpected catalog (schema ${JSON.stringify((data && data.schema) || null)}, expected "model_catalog_v1").`);
        }
        mcStore.hub = { q, data, error: "", loading: false, seq };
      } catch (err) {
        if (seq !== mcStore.hub.seq) return;
        mcStore.hub = { q, data: null, error: String((err && err.message) || err), loading: false, seq };
      }
      mcRender();
    }
    function mcSetMode(view, mode) {
      const hf = mode === "hf";
      if (hf === mcHfMode(view.filters)) return;
      if (hf) { const q = view.filters.q; view.filters = Object.assign({}, view.filters, { q: "" }); mcHubSearch(view, q); return; }
      mcSetFilter(view, { hf: null });
    }
    function mcSetFilter(view, patch) {
      view.filters = Object.assign({}, view.filters, patch);
      mcWriteHash(view);
      mcRenderView(view);
    }
    async function mcDownload(view, provider, artifact) {
      const key = downloadJobKey(provider, artifact);
      const rows = mcRowsOf(mcStore.data).concat(mcRowsOf(mcStore.hub.data));
      const row = rows.find((r) => mcArts(r).some((a) => a.provider === provider && a.artifact === artifact)) || null;
      const art = row ? mcArts(row).find((a) => a.provider === provider && a.artifact === artifact) : null;
      mcStore.busy.add(key);
      mcStore.notices.delete(key);
      mcRender();
      const body = { provider, artifact };
      if (art && uiNum(art.download_bytes) && ["catalog", "hf_api"].includes(String(art.size_source || ""))) body.expected_bytes = art.download_bytes;
      try {
        const res = await api("/api/gateway/models/download", { slow: true, method: "POST", body: JSON.stringify(body) });
        mcStore.busy.delete(key);
        if (!res || !res.job) throw new Error("The gateway accepted the download but returned no job to follow.");
        trackDownloadJob(res.job);
      } catch (err) {
        mcStore.busy.delete(key);
        mcStore.notices.set(key, { tone: "err", text: `Could not start the download: ${String((err && err.message) || err)}` });
      }
      mcRender();
    }
    async function mcUseDefault(view, provider, artifact, btn) {
      const model = servedModelId(provider, artifact);
      if (btn) { btn.disabled = true; btn.textContent = "Saving..."; }
      try {
        await api("/api/gateway/config/capability-defaults/output/text", { slow: true, method: "PUT", body: JSON.stringify({ provider, model }) });
        const text = `Default text model: ${mcProviderLabel(provider)} · ${model}.`;
        view.message = { tone: "ok", text };
        if (view.guide) { $("first-run-message").textContent = text; $("first-run-message").className = "message ok"; }
        try { await renderDefaults(await api("/api/gateway/config/capability-defaults")); } catch { /* saved; the grid refreshes on its next visit */ }
        if (view.guide) renderFirstRunModel();
      } catch (err) {
        view.message = { tone: "err", text: `Could not set the default: ${String((err && err.message) || err)}` };
      }
      mcRender();
    }
    function mcAction(view, action, b) {
      if (action === "refresh") { view.message = null; mcLoad(); if (mcHfMode(view.filters) && view.filters.hf) mcHubSearch(view, view.filters.hf); return; }
      if (action === "clear") { view.filters = Object.assign(mcDefaultFilters(), { hf: view.filters.hf }); mcWriteHash(view); mcRenderView(view); return; }
      if (action === "mode") { mcSetMode(view, b.dataset.mcMode); return; }
      if (action === "hf-search") {
        const box = view.el && typeof view.el.querySelector === "function" ? view.el.querySelector("[data-mc-search]") : null;
        mcHubSearch(view, box ? box.value : view.filters.hf);
        return;
      }
      if (action === "filters") {
        const host = view.el && typeof view.el.querySelector === "function" ? view.el.querySelector(".mc-host") : null;
        if (host && typeof host.scrollIntoView === "function") host.scrollIntoView({ block: "start", behavior: "smooth" });
        return;
      }
      if (action === "open-tab") { const f = Object.assign({}, view.filters); closeFirstRunWizard(); openCatalogTab(f); return; }
      if (action === "download") { mcDownload(view, b.dataset.provider, b.dataset.artifact); return; }
      if (action === "cancel") { dlCancel(b.dataset.job, b); return; }
      if (action === "default") { mcUseDefault(view, b.dataset.provider, b.dataset.artifact, b); }
    }
    function mountModelCatalog(key, el, opts) {
      if (!el) return;
      const o = opts || {};
      const known = mcStore.views.get(key);
      const view = known && known.el === el ? known : { key, el, filters: mcDefaultFilters(), syncHash: !!o.syncHash, guide: !!o.guide, message: null };
      if (o.filters) view.filters = Object.assign(mcDefaultFilters(), o.filters);
      mcStore.views.set(key, view);
      el.onclick = (event) => {
        const t = event && event.target;
        if (!t || typeof t.closest !== "function") return;
        const chip = t.closest("[data-mc-filter]");
        if (chip && !chip.disabled) { mcSetFilter(view, { [chip.dataset.mcFilter]: chip.dataset.mcValue }); return; }
        const b = t.closest("[data-mc-action]");
        if (b && !b.disabled) { mcAction(view, b.dataset.mcAction, b); return; }
        const copy = t.closest("[data-mc-copy]");
        if (copy) uiCopy(copy.dataset.mcCopy);
      };
      el.oninput = (event) => {
        const t = event && event.target;
        if (!t || !t.dataset || !t.dataset.mcSearch) return;
        if (mcHfMode(view.filters)) return;  // Hugging Face: one search per Enter
        clearTimeout(view.debounce);
        view.debounce = setTimeout(() => mcSetFilter(view, { q: String(t.value || "") }), 120);
      };
      el.onchange = (event) => {
        const t = event && event.target;
        if (t && t.dataset && t.dataset.mcFits) mcSetFilter(view, { fits: !!t.checked });
      };
      el.onkeydown = (event) => {
        const t = event && event.target;
        if (!t || !t.dataset) return;
        if (event.key === "Escape" && t.dataset.mcSearch) {
          clearTimeout(view.debounce);
          if (t.value) { event.preventDefault(); if (typeof event.stopPropagation === "function") event.stopPropagation(); t.value = ""; mcSetFilter(view, mcHfMode(view.filters) ? { hf: "" } : { q: "" }); }
          return;
        }
        if (event.key === "Enter" && t.dataset.mcSearch && mcHfMode(view.filters)) { event.preventDefault(); mcHubSearch(view, t.value); return; }
        if ((event.key === "Enter" || event.key === " ") && t.dataset.mcCopy) { event.preventDefault(); uiCopy(t.dataset.mcCopy); }
      };
      mcRenderView(view);
      mcWatchStuck(view);
      mcWriteHash(view);
      mcLoad();
      if (mcHfMode(view.filters) && view.filters.hf && !(mcStore.hub.q === view.filters.hf && (mcStore.hub.data || mcStore.hub.loading))) mcHubSearch(view, view.filters.hf);
      // A reload re-attaches to running downloads, so their bars come back.
      if (!known) uiRestoreDownloads();
    }
    function unmountModelCatalog(key) { mcStore.views.delete(key); }
    // The Models tab with these filters (the guide's "Open in the Models tab",
    // an engine card's "Browse models", a `#catalog?...` link).
    function openCatalogTab(filters) {
      setActiveTab("catalog");
      openCoreTab("catalog", { filters: Object.assign(mcDefaultFilters(), filters || {}) });
    }
    if (typeof window !== "undefined" && window && typeof window.addEventListener === "function") {
      window.addEventListener("hashchange", () => {
        const hash = String(location.hash || "");
        if (!state.principal || !/^#catalog(\?|$)/.test(hash)) return;
        openCatalogTab(mcParseHash(hash));
      });
    }
"""
