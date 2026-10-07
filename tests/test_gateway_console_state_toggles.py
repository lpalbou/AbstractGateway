"""Web console state toggles, sign-in card and type scale (DESIGN 2026-09-30 §2–§6).

The console's real functions are cut out of the served page and driven in a node VM with a
fake DOM (the pattern of test_gateway_console_start_at_login.py):

- the sign-in recovery flow: ONE link → busy "Sending…" → the code step with the honest
  message, "Use code" disabled until 8 digits, "Send a new code (in 30 s)", "Back to token",
  inline errors (wrong code, no email address);
- the switch helpers: unavailable = aria-disabled + a visible reason and the click is ignored;
  a failed change reverts; a change waiting for an inline confirmation keeps the old state;
- the kit's verb-toggle guard (`findVerbToggleLabels`, ui-kit src/state_toggle_lint.ts) over the
  console's source, and the type-scale root cause (the console's own label rules).

The browser half (computed styles, the kit's checkLabelScale) is
test_gateway_console_browser_state_toggles.py.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
from node_requirement import require_node

pytestmark = pytest.mark.basic

CONSOLE_PY = Path(__file__).resolve().parents[1] / "src" / "abstractgateway" / "console.py"


def _html() -> str:
    from abstractgateway.console import gateway_console_html

    return gateway_console_html()


def _slice(html: str, start: str, end: str) -> str:
    a = html.index(start)
    return html[a : html.index(end, a)]


# ---------------------------------------------------------------------------- markup


def test_sign_in_card_is_the_design_card() -> None:
    html = _html()
    card = _slice(html, '<section id="login-section"', "</section>")
    # One status pill, no duplicate caption ("token: missing").
    assert card.count("af-gateway-signin__status ") == 1
    assert 'id="login-source"' not in html
    assert ">Not signed in<" in card and "<h2>Sign in</h2>" in card
    assert "Use the token your gateway admin gave you." in card
    # Labels above fields; "Token" with the Show button inside the field.
    assert '<label class="af-gateway-signin__label" for="login-token">Token</label>' in card
    assert 'class="af-gateway-signin__token-input"' in card
    # ONE quiet link, hidden until /session/recovery says it is on; the code step hidden.
    assert card.count('class="af-gateway-signin__link"') == 2  # the link + "Send a new code"
    assert ">Forgot your token? Email me a sign-in code</button>" in card
    assert 'id="recovery-section" class="af-gateway-signin__recovery" hidden' in card
    assert 'id="recovery-code-step" class="af-gateway-signin__code" hidden' in card
    assert 'inputmode="numeric" maxlength="8"' in card
    assert "Email me a sign-in code</button>" in card and "recovery-forgot" not in html
    # The console no longer carries its own copy of the sign-in CSS (the kit's is synced).
    own_css = _slice(html, "<style>", "</style>")
    assert not re.search(r"(^|\})\s*\.af-gateway-signin__checkbox\s*\{", own_css)
    assert not re.search(r"(^|\})\s*\.af-gateway-signin__form\s*\{", own_css)


def test_switches_replace_the_verb_buttons() -> None:
    html = _html()
    # The gateway's own template (AbstractCore's embedded screens are the core console's).
    own = CONSOLE_PY.read_text(encoding="utf-8")
    for sid in (
        "email-cap-email", "email-cap-agent-tools", "email-cap-recovery",
        "my-email-notify-job-failed", "my-email-notify-approval", "my-email-agent-tools", "my-email-enabled",
    ):
        assert re.search(rf'<button type="button" role="switch" id="{sid}" class="af-switch', html), sid
        assert f'id="{sid}-reason" class="af-switch__reason" hidden' in html, sid
    for label in ("Mailboxes for users", "Agent email tools for users", "Sign-in by email", "Job failed", "Approval needed", "Active"):
        assert f'<span class="af-switch__label">{label}</span>' in html, label
    assert 'role="switch" class="af-switch af-switch--sm hidden" aria-checked="false"' in html  # Workflows paused
    assert html.count('aria-label="Start at login"') == 2
    # The old verb labels and Save-per-section buttons are gone.
    for gone in (
        "Email off", "Email on", "Agent tools on", "Agent tools off", '"Turn on…"', '"Turn off…"', "Turn off</button>",
        "Pause workflows", "Save and test", "Save notifications", "Save email defaults", "Save policy", "Save limits",
        'id="my-email-agent-tools-save"', 'id="email-caps-save"', ">Disable<", '"Disable"', "smtp.example.com",
        "Contact only — never used for auth", 'placeholder="optional"', "Runtime binding",
    ):
        assert gone not in own, gone


def test_users_table_and_create_user_follow_design() -> None:
    html = _html()
    # DESIGN-v2 §2.1: one Accounts table (users + entities); "Email for everyone" BELOW it.
    # Round-2 polish: no Role column (the kind chip says Admin / User / Entity, its title the role).
    # Round 8: ONE Email column ("address · state"); the workspace policy left for its own page.
    assert "<th>Name</th><th>Email</th><th>Runtime</th><th>Active</th><th>Actions</th>" in html
    users = html[html.index('<section id="users-section"'):html.index('<div id="my-email-section"')]
    assert "my-workspace-policy" not in html
    assert "<th>State</th>" not in users
    assert users.index('<table class="users-table accounts-table" data-ui-no-stack>') < users.index('id="email-cap-email"')
    assert users.index('id="open-create-user"') < users.index('id="accounts-create-entity"') < users.index("<table")
    # Round 8: the three email switches sit directly in the card (no Advanced disclosure).
    card = _slice(users, '<section id="email-caps-section"', "</section>")
    assert "<details" not in card
    assert card.index('id="email-cap-email"') < card.index('id="email-cap-agent-tools"') < card.index('id="email-cap-recovery"')
    form = _slice(html, '<div id="user-create-form"', '<div id="user-create-done"')
    # Email address at the top level; Runtime + Tenant in a VISIBLE section named for them
    # (R15 D1: no "Advanced" disclosure anywhere).
    title = '<h4 id="new-user-runtime-tenant-title" class="named-section__title">Runtime and tenant</h4>'
    assert form.index('for="new-email">Email address</label>') < form.index(title)
    assert "Where sign-in codes and notifications go. Leave empty if they have none; they can add it later." in form
    assert "<details" not in form and "Advanced" not in form
    section = _slice(form, '<section id="new-user-runtime-tenant"', "</section>")
    assert 'for="new-runtime">Runtime</label>' in section and 'for="new-tenant">Tenant</label>' in section
    assert "new-email" not in section
    assert "Give this token to ${who}. It is shown once." in html


def test_account_page_order_and_single_save() -> None:
    html = _html()
    page = _slice(html, '<div id="my-email-section"', '<section id="entities-list-section"')
    order = [page.index(k) for k in ('id="my-email-registered-title"', 'id="my-email-mailbox-title"', 'id="my-email-notify-title"', 'id="my-email-tools-title"', 'id="my-email-advanced"')]
    assert order == sorted(order)
    # Exactly one Save: the Email address field's inline one.
    assert re.findall(r">Save<", page) == [">Save<"]
    # DESIGN-v2 §3: IMAP first (and the default), then Google, Microsoft.
    tabs = re.findall(r'role="tab" type="button" data-email-tab="([a-z]+)"', page)
    assert tabs == ["imap", "google", "microsoft"], tabs
    assert re.search(r'id="my-email-tab-imap"[^>]*aria-selected="true"', page)
    assert ">Connect</button>" in page and page.count('id="my-email-connect-go"') == 1
    assert "Use an app password if your provider needs one." in page
    assert "Disconnect this mailbox? Your agents lose email until you connect again. Policy and limits are kept." in page
    # Servers always visible (no disclosure); no User name / Display name fields; a Login only behind a small link.
    assert 'id="my-email-servers"' not in page and "Display name" not in page
    assert re.search(r'id="my-email-login-field" class="af-form__field" hidden', page)
    assert "My provider uses a different login name" in page


def test_runtimes_note_says_what_a_runtime_is() -> None:
    html = _html()
    assert "A runtime is a user&#39;s own data plane" in html or "A runtime is a user's own data plane" in html
    assert "binding" not in _slice(html, '<div id="user-create-form"', '<div id="user-create-done"').lower()


def test_type_scale_root_cause_is_fixed_at_the_rule() -> None:
    """The ~20 px bold "checkbox labels" came from the console's own copy of
    `.af-gateway-signin__checkbox` (18 px / 700) reused outside the sign-in card. No rule of
    the console's own CSS may size a label-like selector above 15 px or 600."""
    html = _html()
    own_css = _slice(html, "<style>", "</style>")
    label_like = re.compile(r"(^|[\s,>+~])(label\b|\.af-gateway-signin__checkbox|\.af-gateway-signin__label|\.af-form__label|\.af-switch__label|\.check-row)")
    bad = []
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", own_css):
        if not label_like.search(sel.strip()):
            continue
        for px in re.findall(r"font-size:\s*(\d+(?:\.\d+)?)px", body):
            if float(px) > 15:
                bad.append((sel.strip(), f"font-size {px}px"))
        for w in re.findall(r"font-weight:\s*(\d+)", body):
            if int(w) > 600:
                bad.append((sel.strip(), f"font-weight {w}"))
    assert bad == []
    # The reuse outside the card is gone too: only the sign-in card's own checkbox wears the class.
    assert html.count('class="af-gateway-signin__checkbox"') == 1


# ---------------------------------------------------------------------------- the kit's verb guard


def test_kit_verb_toggle_guard_over_the_console_sources() -> None:
    from abstractgateway import console_islands_sync

    kit = console_islands_sync.locate_kit()
    if kit is None:
        pytest.skip("abstractuic ui-kit not present (non-monorepo checkout)")
    lint = kit / "src" / "state_toggle_lint.ts"
    assert lint.is_file(), lint
    node = require_node()
    script = (
        f"import {{ findVerbToggleLabels }} from {json.dumps(str(lint))};\n"
        "import fs from 'node:fs';\n"
        f"const src = fs.readFileSync({json.dumps(str(CONSOLE_PY))}, 'utf8');\n"
        "console.log(JSON.stringify(findVerbToggleLabels(src)));\n"
    )
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "lint.mjs"
        path.write_text(script, encoding="utf-8")
        proc = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr[-2000:]
    hits = json.loads(proc.stdout.strip().splitlines()[-1])
    assert hits == [], hits


# ---------------------------------------------------------------------------- VM harness

DOM = r"""
const els = {};
class El {
  constructor(id) { this.id = id; this.hidden = false; this.disabled = false; this.value = ""; this._text = ""; this.attrs = {}; this.className = ""; this.title = ""; this.children = []; this.focused = false; this.onclick = null; }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v == null ? "" : v); if (!this._text) this.children = []; }
  set innerHTML(v) { this._html = String(v); }
  get innerHTML() { return this._html || ""; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  removeAttribute(n) { delete this.attrs[n]; }
  focus() { this.focused = true; }
  append(...c) { this.children.push(...c); }
  remove() {}
  after() {}
  get classList() { const self = this; return { add() {}, remove() {}, toggle() {}, contains() { return false; } }; }
}
const $ = (id) => els[id] || (els[id] = new El(id));
const document = { createElement: () => new El(""), activeElement: null, querySelectorAll: () => [] };
"""


def _run(body: str, *, fns: str) -> dict:
    node = require_node()
    js = "const vm = require('vm');\n" + f"const FNS = {json.dumps(fns)};\n" + body
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "h.js"
        path.write_text(js, encoding="utf-8")
        proc = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _signin_fns() -> str:
    html = _html()
    return _slice(html, "function setLoginStatus(", "function confirmAction(") + "\n" + _slice(html, "    async function login() {", "    async function signOut() {")


RECOVERY_HARNESS = DOM + r"""
const scenario = JSON.parse(process.argv[2]);
let resolveRequest = null;
const calls = [];
async function api(path, opts = {}) {
  calls.push([opts.method || "GET", path, opts.body ? JSON.parse(opts.body) : null]);
  if (path === "/api/gateway/session/recovery") return { available: true };
  if (path === "/api/gateway/session/recovery/request") return await new Promise((res) => { resolveRequest = res; });
  if (path === "/api/gateway/session/recovery/redeem") {
    if (scenario.redeem === "wrong") { const e = new Error("refused"); e.status = 401; throw e; }
    return { ok: true };
  }
  throw new Error("unexpected " + path);
}
const state = {};
let refreshed = 0;
const ctx = vm.createContext({ $, api, state, document, console, Date, setTimeout: () => 0, clearTimeout: () => {}, location: { origin: "http://127.0.0.1:18110" }, refresh: async () => { refreshed++; } });
vm.runInContext(FNS + "\n;this.load = loadRecoveryOptions; this.req = requestRecoveryCode; this.use = useRecoveryCode; this.back = recoveryBackToToken; this.input = recoveryCodeInput;", ctx);
(async () => {
  $("recovery-code-step").hidden = true;
  $("login-user").value = scenario.user;
  await ctx.load();
  const shown = !$("recovery-section").hidden;
  const pending = ctx.req(false);
  await new Promise((r) => setImmediate(r));
  const busy = { text: $("recovery-link").textContent, ariaBusy: $("recovery-link").getAttribute("aria-busy"), disabled: $("recovery-link").disabled, codeHidden: $("recovery-code-step").hidden };
  resolveRequest(scenario.answer);
  await pending;
  const after = {
    codeHidden: $("recovery-code-step").hidden, linkHidden: $("recovery-section").hidden,
    sent: $("recovery-sent-message").textContent, requestError: $("recovery-request-message").textContent, requestErrorHidden: $("recovery-request-message").hidden,
    resend: $("recovery-resend").textContent, resendDisabled: $("recovery-resend").disabled,
    useDisabled: $("recovery-use").disabled, focused: $("recovery-code-input").focused,
  };
  let redeem = null;
  if (!after.codeHidden) {
    $("recovery-code-input").value = "1234";
    ctx.input();
    const useAt4 = $("recovery-use").disabled;
    $("recovery-code-input").value = "12a345678";
    ctx.input();
    const cleaned = $("recovery-code-input").value;
    const useAt8 = $("recovery-use").disabled;
    await ctx.use();
    redeem = { useAt4, cleaned, useAt8, error: $("recovery-code-error").textContent, errorHidden: $("recovery-code-error").hidden, refreshed, codeHidden: $("recovery-code-step").hidden };
    ctx.back();
  }
  const back = { codeHidden: $("recovery-code-step").hidden, linkHidden: $("recovery-section").hidden, link: $("recovery-link").textContent };
  console.log(JSON.stringify({ shown, busy, after, redeem, back, calls }));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _recovery(scenario: dict) -> dict:
    node = require_node()
    js = "const vm = require('vm');\n" + f"const FNS = {json.dumps(_signin_fns())};\n" + RECOVERY_HARNESS
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "h.js"
        path.write_text(js, encoding="utf-8")
        proc = subprocess.run([node, str(path), json.dumps(scenario)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


SENT = {"sent": True, "to": "a•••@•••", "expires_in_s": 600, "message": "A sign-in code is on its way to a•••@•••. It expires in 10 minutes."}


def test_recovery_link_goes_busy_then_reveals_the_code_step() -> None:
    out = _recovery({"user": "alice", "answer": SENT, "redeem": "ok"})
    assert out["shown"] is True
    assert out["busy"] == {"text": "Sending…", "ariaBusy": "true", "disabled": True, "codeHidden": True}
    assert out["after"]["codeHidden"] is False and out["after"]["linkHidden"] is True
    assert out["after"]["sent"] == SENT["message"]
    assert out["after"]["resend"] == "Send a new code (in 30 s)" and out["after"]["resendDisabled"] is True
    assert out["after"]["useDisabled"] is True and out["after"]["focused"] is True
    # ONE purpose: a sign-in session (reset_token stays in the API only).
    assert ["POST", "/api/gateway/session/recovery/request", {"user_id": "alice", "purpose": "sign_in"}] in out["calls"]
    r = out["redeem"]
    assert r["useAt4"] is True and r["cleaned"] == "12345678" and r["useAt8"] is False
    assert r["refreshed"] == 1 and r["errorHidden"] is True
    assert out["back"] == {"codeHidden": True, "linkHidden": False, "link": "Forgot your token? Email me a sign-in code"}


def test_wrong_code_is_said_inline() -> None:
    out = _recovery({"user": "alice", "answer": SENT, "redeem": "wrong"})
    assert out["redeem"]["error"] == "That code is wrong, expired or already used. Send a new one."
    assert out["redeem"]["errorHidden"] is False and out["redeem"]["refreshed"] == 0 and out["redeem"]["codeHidden"] is False


def test_no_email_address_is_said_and_no_code_step_opens() -> None:
    answer = {"sent": False, "reason_code": "no_email_address", "message": "This account has no email address, so a code can't be sent. Ask your gateway admin for a token."}
    out = _recovery({"user": "bob", "answer": answer})
    assert out["after"]["codeHidden"] is True and out["after"]["linkHidden"] is False
    assert out["after"]["requestError"] == answer["message"] and out["after"]["requestErrorHidden"] is False
    assert out["redeem"] is None


SWITCH_HARNESS = DOM + r"""
const ctx = vm.createContext({ $, document, console, esc: (s) => String(s) });
vm.runInContext(FNS + "\n;this.create = afSwitchCreate; this.set = afSwitchSet; this.bind = afSwitchBind;", ctx);
(async () => {
  const out = {};
  // Unavailable: aria-disabled + visible reason, described by it, click ignored.
  const a = ctx.create({ id: "sw-a", label: "Agent email tools", checked: false, unavailableReason: "Connect a mailbox first." });
  let calls = 0;
  ctx.bind(a.button, async () => { calls++; });
  await a.button.onclick();
  out.unavailable = { role: a.button.getAttribute("role"), checked: a.button.getAttribute("aria-checked"), disabled: a.button.getAttribute("aria-disabled"), reason: a.reason.textContent, reasonHidden: a.reason.hidden, describedby: a.button.getAttribute("aria-describedby"), calls, htmlDisabled: a.button.disabled };
  // Available again: the reason disappears.
  ctx.set(a.button, { reason: "" });
  out.available = { disabled: a.button.getAttribute("aria-disabled"), reasonHidden: a.reason.hidden };
  // Success: applied, busy cleared.
  const b = ctx.create({ id: "sw-b", label: "Job failed", checked: true });
  let seen = null;
  ctx.bind(b.button, async (next) => { seen = next; });
  await b.button.onclick();
  out.applied = { next: seen, checked: b.button.getAttribute("aria-checked"), busy: b.button.getAttribute("aria-busy") };
  // Failure: reverts and reports.
  let err = "";
  ctx.bind(b.button, async () => { throw new Error("refused"); }, (e) => { err = e.message; });
  await b.button.onclick();
  out.failed = { checked: b.button.getAttribute("aria-checked"), err, busy: b.button.getAttribute("aria-busy") };
  // Pending inline confirmation (returns false): the old state stays.
  const c = ctx.create({ id: "sw-c", label: "Active", checked: true, small: true });
  ctx.bind(c.button, async () => false);
  await c.button.onclick();
  out.pending = { checked: c.button.getAttribute("aria-checked"), cls: c.button.className };
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_switch_helpers_render_state_reason_and_revert() -> None:
    html = _html()
    fns = _slice(html, "    function afSwitchCreate(", "    function inlineState(")
    out = _run(SWITCH_HARNESS, fns=fns)
    assert out["unavailable"] == {
        "role": "switch", "checked": "false", "disabled": "true", "reason": "Connect a mailbox first.",
        "reasonHidden": False, "describedby": "sw-a-reason", "calls": 0, "htmlDisabled": False,
    }
    assert out["available"] == {"disabled": None, "reasonHidden": True}
    assert out["applied"] == {"next": False, "checked": "false", "busy": None}
    assert out["failed"] == {"checked": "false", "err": "refused", "busy": None}
    assert out["pending"] == {"checked": "true", "cls": "af-switch af-switch--sm"}
