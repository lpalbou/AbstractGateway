// Browser checks of the merged Providers page (DESIGN-v3 §7, §13.8), run by
// tests/test_gateway_console_browser_providers.py against a hermetic scratch gateway.
//
//   node providers.mjs <base-url> <admin-token> <playwright-node-modules> <fake-openai-key>
//
// GET /engines is a fixture (page.route); every engine POST is recorded and answered here, never
// sent to the gateway. Prints one JSON line: {"failures": [...], "checks": N}.
import { createRequire } from "node:module";
import path from "node:path";

const [BASE, ADMIN, PW, FAKE_KEY] = process.argv.slice(2);
if (!FAKE_KEY) throw new Error("providers.mjs needs 4 arguments");
const require = createRequire(path.join(PW, "/"));
const { chromium } = require("playwright-core");

const failures = [];
let checks = 0;
function check(ok, what, detail) {
  checks += 1;
  if (!ok) failures.push(detail === undefined ? what : `${what}: ${JSON.stringify(detail)}`);
}

const act = (id, label, extra = {}) => ({ id, label, enabled: true, ...extra });
const docs = (url) => act("docs", "Docs", { method: "GET", url });
const row = (id, name, extra) => ({
  id, name, description: `${name} fixture row.`, supported: true, support_reason: null, installed: true, version: "1.0.0",
  install_location: null, running: null, reachable: null, base_url: null, models_count: null,
  install: { available: true, method: "wheel", needs_admin: false, steps: ["pip install"], notes: "fixture", command_preview: ["pip install x"] },
  active_job: null, provider: id, ...extra,
});
const ENGINES = {
  schema: "gateway_engines_v2", probed: true, install_allowed: true, generated_at: "2026-10-01T10:00:00Z",
  engines: [
    row("ollama", "Ollama", { running: true, reachable: true, base_url: "http://localhost:11434", models_count: 3, actions: [act("stop", "Stop", { path: "/api/gateway/engines/ollama/stop" }), docs("https://docs.ollama.com")] }),
    row("lmstudio", "LM Studio", { running: false, reachable: false, actions: [act("start", "Start", { path: "/api/gateway/engines/lmstudio/start" }), docs("https://lmstudio.ai/docs")] }),
    row("mlx", "MLX (mlx-lm)", { actions: [docs("https://github.com/ml-explore/mlx-lm")] }),
    row("llamacpp", "llama.cpp", { installed: false, version: null, provider: "huggingface", actions: [act("install", "Install", { path: "/api/gateway/engines/llamacpp/install" }), docs("https://github.com/abetlen/llama-cpp-python")] }),
    row("vllm", "vLLM", { supported: false, installed: false, version: null, support_reason: "vLLM does not run on macOS (fixture reason).", actions: [docs("https://docs.vllm.ai")] }),
    row("huggingface", "Hugging Face (transformers)", { installed: false, version: null, actions: [docs("https://huggingface.co/docs/transformers")] }),
  ],
};
// One install waiting for an administrator: Continue / Re-check / Cancel are offered.
const HF_JOB = { job_id: "job-hf-1", engine: "huggingface", state: "needs_admin", can_cancel: true, message: "This step needs an administrator.", admin_prompt: { reason: "fixture", command: "sudo true", button: "Continue with administrator password" }, continue_actions: ["approve_admin", "recheck"] };

const posts = [];
let engineGets = 0;
async function routeEngines(page) {
  await page.route(/\/api\/gateway\/engines(\/.*)?(\?.*)?$/, async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const p = url.pathname.replace(/^\/api\/gateway/, "");
    if (req.method() === "GET" && p === "/engines") { engineGets += 1; return route.fulfill({ json: ENGINES }); }
    if (req.method() === "GET" && p === "/engines/jobs") return route.fulfill({ json: { ok: true, schema: "engine_install_jobs_v1", jobs: [HF_JOB] } });
    if (req.method() === "GET" && p.startsWith("/engines/jobs/")) return route.fulfill({ json: HF_JOB });
    if (req.method() === "POST") {
      posts.push({ path: p, body: req.postDataJSON ? req.postDataJSON() : null });
      if (p.endsWith("/stop")) return route.fulfill({ json: { ok: true, running: false } });
      if (p.endsWith("/start")) return route.fulfill({ json: { ok: true, running: true } });
      if (p.endsWith("/install")) return route.fulfill({ json: { ok: true, job_id: "job-cpp-1", engine: "llamacpp", state: "done", message: "llama.cpp is installed." } });
      return route.fulfill({ json: HF_JOB });
    }
    return route.fulfill({ status: 404, json: { message: `fixture: unexpected ${req.method()} ${p}` } });
  });
}

async function signIn(page) {
  await page.waitForSelector("#login-form");
  await page.fill("#login-user", "admin");
  await page.fill("#login-token", ADMIN);
  await page.click("#login-button");
  await page.waitForFunction(() => document.body.classList.contains("signed-in"), null, { timeout: 30000 });
  await page.keyboard.press("Escape").catch(() => {});
}
const card = (id) => `#engines-core-root [data-engine-card="${id}"]`;
async function buttons(page, id) {
  return page.$$eval(`${card(id)} button, ${card(id)} a`, (els) => els.map((e) => e.textContent.trim()));
}
async function waitCards(page) {
  const ok = await page.waitForFunction(() => document.querySelectorAll('#engines-core-root [data-engine-card]').length === 6, null, { timeout: 20000 }).then(() => true, () => false);
  check(ok, "six local provider cards rendered");
}

const browser = await chromium.launch();
try {
  // 1. `#engines` deep link lands on Providers, with the local provider cards.
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => failures.push(`pageerror: ${e.message}`));
  await routeEngines(page);
  await page.goto(`${BASE}/console#engines`);
  await signIn(page);
  await page.waitForSelector("#tab-providers.active", { timeout: 15000 }).catch(() => {});
  check(await page.$("#tab-providers.active") !== null, "#engines lands on the Providers tab");
  check((await page.textContent("#page-title")) === "Providers", "page title Providers", await page.textContent("#page-title"));
  check(await page.$("#tab-button-engines") === null && await page.$("#tab-engines") === null, "no Engines nav item or panel");
  const nav = await page.$$eval("#nav-group-models", (els) => Array.from(els[0].parentElement.querySelectorAll(".shell_nav_label")).map((e) => e.textContent.trim()));
  check(JSON.stringify(nav) === JSON.stringify(["Providers", "Models", "Multimodal"]), "sidebar MODELS = Providers · Models · Multimodal", nav);
  await waitCards(page);
  // The connections arrive with the provider list (loaded after sign-in, independently of the engines).
  const conns = await page.waitForFunction(() => { const c = document.querySelector('[data-provider-connection="lmstudio"]'); return !!c && c.textContent.includes("studio Mac"); }, null, { timeout: 15000 }).then(() => true, () => false);
  check(conns, "local provider cards show their stored connections");
  const order = await page.$$eval("#engines-core-root [data-engine-card]", (els) => els.map((e) => e.dataset.engineCard).sort());
  check(JSON.stringify(order) === JSON.stringify(["huggingface", "llamacpp", "lmstudio", "mlx", "ollama", "vllm"]), "one card per engine id the API returns", order);
  // Sections in order: Local providers, Remote providers, Available Providers.
  const heads = await page.$$eval("#tab-providers h2.section-title", (els) => els.map((e) => e.textContent.replace(/[^A-Za-z ]/g, "").trim()));
  check(JSON.stringify(heads) === JSON.stringify(["Local providers", "Remote providers", "Available Providers"]), "sections in order", heads);

  // 2. Engine states and actions per card (0.10.0 Engines page parity).
  const status = async (id) => (await page.textContent(`${card(id)} .ui-card__status`)).trim();
  check((await status("ollama")) === "Running", "ollama Running", await status("ollama"));
  check((await status("lmstudio")) === "Installed, stopped", "lmstudio stopped", await status("lmstudio"));
  check((await status("mlx")) === "Ready", "mlx Ready", await status("mlx"));
  check((await status("llamacpp")) === "Not installed", "llamacpp Not installed", await status("llamacpp"));
  check((await status("vllm")) === "Not for this computer", "vllm unsupported", await status("vllm"));
  check((await status("huggingface")) === "Needs your approval", "huggingface waiting job", await status("huggingface"));
  check((await page.textContent(card("vllm"))).includes("vLLM does not run on macOS (fixture reason)."), "unsupported card says why");
  const b = {};
  for (const id of ["ollama", "lmstudio", "mlx", "llamacpp", "vllm", "huggingface"]) b[id] = await buttons(page, id);
  for (const id of Object.keys(b)) check(b[id].includes("Learn more"), `${id}: Learn more`, b[id]);
  check(b.ollama.includes("Stop") && b.ollama.includes("Browse models"), "ollama: Stop + Browse models", b.ollama);
  check(b.lmstudio.includes("Start") && b.lmstudio.includes("Browse models"), "lmstudio: Start + Browse models", b.lmstudio);
  check(b.mlx.includes("Browse models"), "mlx: Browse models", b.mlx);
  check(b.llamacpp.includes("Install") && b.llamacpp.includes("Browse models"), "llamacpp: Install + Browse models", b.llamacpp);
  check(!b.vllm.some((t) => ["Install", "Start", "Stop", "Browse models"].includes(t)), "vllm: no engine action on an unsupported host", b.vllm);
  check(b.huggingface.includes("Continue with administrator password") && b.huggingface.includes("Re-check") && b.huggingface.includes("Cancel"), "huggingface: Continue / Re-check / Cancel", b.huggingface);
  check(b.ollama.includes("Add connection") && b.lmstudio.includes("Add connection") && b.vllm.includes("Set up connection"), "connection buttons on ollama / lmstudio / vllm", [b.ollama, b.lmstudio, b.vllm]);
  check(!b.mlx.some((t) => /connection/i.test(t)), "in-process engines carry no connection", b.mlx);
  check((await page.textContent(card("lmstudio"))).includes("LM Studio (studio Mac)") && (await page.textContent(card("lmstudio"))).includes("http://192.168.1.20:1234/v1"), "lmstudio card lists its connections");

  // 3. The actions send exactly the Engines page's requests.
  const click = (id, text) => page.click(`${card(id)} button:text-is("${text}")`);
  await click("ollama", "Stop");
  await page.waitForFunction(() => document.body.textContent.includes("Ollama is stopped."), null, { timeout: 10000 }).catch(() => {});
  check(posts.some((x) => x.path === "/engines/ollama/stop"), "Stop posts /engines/ollama/stop", posts);
  await waitCards(page);
  await click("lmstudio", "Start");
  await page.waitForFunction(() => document.body.textContent.includes("LM Studio is running."), null, { timeout: 10000 }).catch(() => {});
  check(posts.some((x) => x.path === "/engines/lmstudio/start"), "Start posts /engines/lmstudio/start", posts);
  await waitCards(page);
  await click("llamacpp", "Install");
  await page.waitForSelector(`${card("llamacpp")} .ui-confirm`, { timeout: 5000 }).catch(() => {});
  const confirmBtns = await buttons(page, "llamacpp");
  check(confirmBtns.includes("Install now") && confirmBtns.includes("Not now"), "Install asks first (Install now / Not now)", confirmBtns);
  await click("llamacpp", "Install now");
  await page.waitForTimeout(800);
  const inst = posts.find((x) => x.path === "/engines/llamacpp/install");
  check(!!inst && inst.body && inst.body.dry_run === false, "Install now posts the install", posts);
  await waitCards(page);
  await click("huggingface", "Continue with administrator password");
  await page.waitForTimeout(600);
  check(posts.some((x) => x.path === "/engines/jobs/job-hf-1/continue" && x.body && x.body.action === "approve_admin"), "Continue posts …/continue approve_admin", posts);
  await waitCards(page);
  await click("huggingface", "Cancel");
  await page.waitForTimeout(600);
  check(posts.some((x) => x.path === "/engines/jobs/job-hf-1/cancel"), "Cancel posts …/cancel", posts);
  const before = engineGets;
  await page.click('#engines-core-root [data-engine-action="refresh"]');
  await page.waitForTimeout(800);
  check(engineGets > before, "Check again re-reads the engines", { before, after: engineGets });

  // 4. Connections: Add opens the modal on the provider's family; Edit opens the stored row.
  await waitCards(page);
  await click("ollama", "Add connection");
  await page.waitForSelector("#provider-modal-backdrop:not(.hidden)", { timeout: 5000 }).catch(() => {});
  check(await page.$("#provider-modal-backdrop:not(.hidden)") !== null, "Add connection opens the connection modal");
  check((await page.inputValue("#endpoint-provider-family")) === "ollama", "modal family = ollama", await page.inputValue("#endpoint-provider-family"));
  await page.evaluate(() => document.getElementById("provider-modal-backdrop").classList.add("hidden"));
  await page.click(`${card("lmstudio")} [data-provider-connection-edit="lmstudio-lan"]`);
  await page.waitForTimeout(300);
  check((await page.inputValue("#endpoint-base-url")) === "http://192.168.1.20:1234/v1" && (await page.inputValue("#endpoint-id")) === "lmstudio-lan", "Edit opens the stored connection");
  await page.evaluate(() => document.getElementById("provider-modal-backdrop").classList.add("hidden"));
  await click("vllm", "Set up connection");
  await page.waitForTimeout(300);
  check((await page.inputValue("#endpoint-provider-family")) === "openai-compatible" && (await page.inputValue("#endpoint-profile-id")) === "vllm", "vLLM connection = custom OpenAI-compatible with id vllm");
  await page.evaluate(() => document.getElementById("provider-modal-backdrop").classList.add("hidden"));

  // 5. Remote providers: the five remote families, connection state with a fingerprint only.
  const presets = await page.$$eval("#provider-preset-grid .provider-preset", (els) => els.map((e) => e.id.replace(/^provider-preset-/, "")));
  check(JSON.stringify(presets) === JSON.stringify(["openai", "anthropic", "openrouter", "portkey", "openai-compatible"]), "remote presets (no local family twice)", presets);
  const openaiState = await page.textContent('#provider-preset-openai .provider-preset__state');
  check(/^Connected · key [0-9a-f]{8}$/.test(openaiState.trim()), "OpenAI preset: Connected + 8-char fingerprint", openaiState);
  check((await page.textContent('#provider-preset-anthropic .provider-preset__state')).trim() === "Not connected", "Anthropic preset: Not connected");
  await page.click('#provider-preset-anthropic');
  await page.waitForTimeout(300);
  check((await page.inputValue("#endpoint-provider-family")) === "anthropic", "a preset opens its connection modal");
  await page.evaluate(() => document.getElementById("provider-modal-backdrop").classList.add("hidden"));
  check(!(await page.content()).includes(FAKE_KEY), "the API key never reaches the page");

  // 6. Available Providers table unchanged.
  const ths = await page.$$eval("#tab-providers table thead th", (els) => els.map((e) => e.textContent.trim()));
  check(JSON.stringify(ths) === JSON.stringify(["Name", "Provider ID", "Type", "Models", "Status", "Actions"]), "Available Providers headers", ths);
  const rows = await page.$$eval("#endpoint-profiles-table tr", (els) => els.map((e) => e.textContent));
  check(rows.length >= 2 && rows.some((t) => t.includes("endpoint:openai")) && rows.some((t) => t.includes("endpoint:lmstudio-lan")), "Available Providers rows", rows.length);
  check((await page.$$eval("#endpoint-profiles-table button", (els) => els.map((e) => e.textContent.trim()))).some((t) => t.includes("Delete")), "table keeps Edit/Delete");

  // 7. Browse models opens the catalog filtered to the provider.
  await waitCards(page);
  await click("mlx", "Browse models");
  await page.waitForSelector("#tab-catalog.active", { timeout: 8000 }).catch(() => {});
  check(await page.$("#tab-catalog.active") !== null, "Browse models opens Models");
  check(/provider=mlx/.test(await page.evaluate(() => location.hash)), "catalog filtered to mlx", await page.evaluate(() => location.hash));
  await ctx.close();

  // 8. A persisted "engines" tab lands on Providers; phone width has no horizontal scroll.
  const ctx2 = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
  await ctx2.addInitScript(() => { try { if (!sessionStorage.getItem("seeded")) { localStorage.setItem("abstractgateway_active_tab_v1", "engines"); sessionStorage.setItem("seeded", "1"); } } catch {} });
  const p2 = await ctx2.newPage();
  p2.on("pageerror", (e) => failures.push(`pageerror(390): ${e.message}`));
  await routeEngines(p2);
  await p2.goto(`${BASE}/console`);
  await signIn(p2);
  await waitCards(p2).catch(() => {});
  check(await p2.$("#tab-providers.active") !== null, "persisted 'engines' tab folds onto Providers");
  check((await p2.$$("#engines-core-root [data-engine-card]")).length === 6, "local provider cards at 390");
  const overflow = await p2.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
  check(!overflow, "no horizontal page scroll at 390");
  await ctx2.close();
} finally {
  await browser.close();
}
console.log(JSON.stringify({ failures, checks }));
