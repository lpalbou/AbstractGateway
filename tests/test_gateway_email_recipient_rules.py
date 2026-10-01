"""Recipient rules over HTTP and in the console (round 3, DESIGN-v3 §4 / §13.3).

- API: PUT /me/email/policy carries `always_allow` / `always_deny` (a list given replaces that
  list; the older `{mode, entries}` body still works and maps to the mode's list); the policy
  check evaluates To, Cc and Bcc; a real send through the user's context is refused with the
  rule named ("Not sent: x@denied.gov is on your Always denied list (denied.gov).") and a send to
  an allowed domain goes out.
- Console: ONE renderer (`renderEmailRecipientRules(policy, apiBase)`) draws the mode, the two
  chip lists and the precedence sentence, and every change auto-saves through `<apiBase>/policy`
  (driven in a node VM with a fake DOM, the test_gateway_console_state_toggles.py pattern).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ALICE, connect_body, plane_of


def _connect(gateway, imap, smtp) -> None:
    r = gateway["client"].put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp))
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------- API


@pytest.mark.integration
def test_put_carries_both_lists_and_replaces_only_the_lists_given(gateway, imap, smtp) -> None:
    _connect(gateway, imap, smtp)
    c, h = gateway["client"], gateway["alice"]
    r = c.put("/api/gateway/me/email/policy", headers=h, json={"mode": "allowlist", "always_allow": ["abstractframework.ai"], "always_deny": ["xxx.gov"]})
    assert r.status_code == 200, r.text
    pol = r.json()["policy"]
    assert pol["mode"] == "allowlist" and pol["always_allow"] == ["abstractframework.ai"] and pol["always_deny"] == ["xxx.gov"]
    # An omitted list is kept.
    r = c.put("/api/gateway/me/email/policy", headers=h, json={"mode": "denylist", "always_deny": ["xxx.gov", "spam.example"]})
    pol = r.json()["policy"]
    assert pol["mode"] == "denylist" and pol["always_allow"] == ["abstractframework.ai"]
    assert pol["always_deny"] == ["xxx.gov", "spam.example"] and pol["entries"] == pol["always_deny"]
    # GET returns the same shape.
    got = c.get("/api/gateway/me/email", headers=h).json()["policy"]
    assert got["always_allow"] == ["abstractframework.ai"] and got["always_deny"] == ["xxx.gov", "spam.example"]
    # Structural validation: a pattern or a dotless word is refused with the reason.
    r = c.put("/api/gateway/me/email/policy", headers=h, json={"mode": "denylist", "always_deny": ["*.gov"]})
    assert r.status_code == 400 and r.json()["detail"]["reason_code"] == "email_invalid_settings"
    assert "Always denied" in r.json()["detail"]["cause"]


@pytest.mark.integration
def test_older_mode_entries_body_still_maps_to_the_mode_list(gateway, imap, smtp) -> None:
    _connect(gateway, imap, smtp)
    c, h = gateway["client"], gateway["alice"]
    c.put("/api/gateway/me/email/policy", headers=h, json={"mode": "allowlist", "always_deny": ["xxx.gov"]})
    r = c.put("/api/gateway/me/email/policy", headers=h, json={"mode": "allowlist", "entries": [ALICE, "example.org"]})
    pol = r.json()["policy"]
    assert pol["always_allow"] == [ALICE, "example.org"] and pol["always_deny"] == ["xxx.gov"]
    r = c.put("/api/gateway/me/email/policy", headers=h, json={"mode": "denylist", "entries": ["spam.example"]})
    pol = r.json()["policy"]
    assert pol["always_deny"] == ["spam.example"] and pol["always_allow"] == [ALICE, "example.org"]


@pytest.mark.integration
def test_check_evaluates_to_cc_and_bcc_with_deny_winning(gateway, imap, smtp) -> None:
    _connect(gateway, imap, smtp)
    c, h = gateway["client"], gateway["alice"]
    c.put("/api/gateway/me/email/policy", headers=h, json={"mode": "allowlist", "always_allow": ["abstractframework.ai", "denied.gov"], "always_deny": ["denied.gov"]})
    r = c.post(
        "/api/gateway/me/email/policy/check",
        headers=h,
        json={"to": ["anyone@abstractframework.ai"], "cc": ["x@denied.gov"], "bcc": ["y@mail.denied.gov", ALICE]},
    )
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["allowed"] is False
    got = {(v["field"], v["address"]): (v["allowed"], v["source"]) for v in doc["recipients"]}
    assert got == {
        ("to", "anyone@abstractframework.ai"): (True, "always_allow"),
        ("cc", "x@denied.gov"): (False, "always_deny"),
        ("bcc", "y@mail.denied.gov"): (False, "always_deny"),
        ("bcc", ALICE): (True, "self"),
    }
    # The older body (addresses = To) still works.
    r = c.post("/api/gateway/me/email/policy/check", headers=h, json={"addresses": ["stranger@else.test"]})
    assert r.json()["allowed"] is False and r.json()["recipients"][0]["source"] == "mode"


@pytest.mark.integration
def test_refusal_transcript_send_to_denied_domain_refused_allowed_domain_sent(gateway, imap, smtp) -> None:
    """The A6 transcript: a send through the user's own context (agent tools and notifications
    use this path) to x@denied.gov is refused naming the rule; anyone@abstractframework.ai goes
    out when that domain is Always allowed (mode: Only the Allowed list)."""
    from abstractcore.comms.email import EmailPolicyRefused, OutgoingMessage
    from abstractgateway.mail import accounts as mail_accounts

    _connect(gateway, imap, smtp)
    r = gateway["client"].put(
        "/api/gateway/me/email/policy",
        headers=gateway["alice"],
        json={"mode": "allowlist", "always_allow": ["abstractframework.ai"], "always_deny": ["denied.gov"]},
    )
    assert r.status_code == 200, r.text
    ctx = mail_accounts.email_context(plane_of("alice"))
    with pytest.raises(EmailPolicyRefused) as info:
        ctx.send(OutgoingMessage(to=("x@denied.gov",), subject="s", text="b"))
    print(f"TRANSCRIPT send to=x@denied.gov -> REFUSED: {info.value.cause}")
    assert info.value.cause == "Not sent: x@denied.gov is on your Always denied list (denied.gov)."
    assert smtp.messages == []
    # Cc and Bcc are checked too: one denied Bcc refuses the whole message.
    with pytest.raises(EmailPolicyRefused) as info2:
        ctx.send(OutgoingMessage(to=("anyone@abstractframework.ai",), bcc=("x@denied.gov",), subject="s", text="b"))
    print(f"TRANSCRIPT send to=anyone@abstractframework.ai bcc=x@denied.gov -> REFUSED: {info2.value.cause}")
    assert smtp.messages == []
    result = ctx.send(OutgoingMessage(to=("anyone@abstractframework.ai",), subject="Hello", text="b"))
    print(f"TRANSCRIPT send to=anyone@abstractframework.ai -> SENT: accepted={list(result.accepted)} message_id={result.message_id}")
    assert list(result.accepted) == ["anyone@abstractframework.ai"] and len(smtp.messages) == 1


# ---------------------------------------------------------------------------- console


def _html() -> str:
    from abstractgateway.console import gateway_console_html

    return gateway_console_html()


def _slice(html: str, start: str, end: str) -> str:
    a = html.index(start)
    return html[a : html.index(end, a)]


@pytest.mark.basic
def test_advanced_markup_has_mode_two_lists_and_the_precedence_sentence() -> None:
    page = _slice(_html(), 'id="my-email-recipient-rules"', 'id="my-email-per-hour"')
    assert '<option value="allowlist">Only the Allowed list</option>' in page
    assert '<option value="denylist">Anyone not on the Denied list</option>' in page
    assert ">Always allowed</span>" in page and ">Always denied</span>" in page
    assert "Denied always wins. Your own address is always allowed. A domain also covers its subdomains." in page
    for fid in ("my-email-allow-list", "my-email-allow-add", "my-email-allow-add-button", "my-email-deny-list", "my-email-deny-add", "my-email-deny-add-button"):
        assert f'id="{fid}"' in page, fid
    # Auto-save: no Save button in the rules.
    assert ">Save<" not in page


DOM = r"""
const els = {};
class El {
  constructor(id) { this.id = id; this.hidden = false; this.disabled = false; this.value = ""; this._text = ""; this.attrs = {}; this.className = ""; this.title = ""; this.children = []; this.onclick = null; this.type = ""; }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v == null ? "" : v); if (!this._text) this.children = []; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  append(...c) { this.children.push(...c); }
}
const $ = (id) => els[id] || (els[id] = new El(id));
const document = { createElement: () => new El("") };
"""

HARNESS = DOM + r"""
const calls = [];
const states = {};
let fail = null;
async function api(path, opts = {}) {
  const body = opts.body ? JSON.parse(opts.body) : null;
  calls.push([opts.method || "GET", path, body]);
  if (fail) { const e = new Error(fail); throw e; }
  return { ok: true, policy: { mode: body.mode, entries: [], always_allow: body.always_allow.map((x) => x.toLowerCase()), always_deny: body.always_deny.map((x) => x.toLowerCase()) } };
}
function inlineState(id, text, tone) { states[id] = [text, tone]; }
function emailErrorText(e) { return e.message; }
const state = {};
const ctx = vm.createContext({ $, api, inlineState, emailErrorText, state, document, console });
vm.runInContext(FNS + "\n;this.render = renderEmailRecipientRules; this.add = addEmailRecipientRule; this.save = saveEmailRecipientRules;", ctx);
const chips = (id) => $(id).children.map((li) => li.className === "chip" ? li.children[0].textContent : `(${li.textContent})`);
(async () => {
  ctx.render({ mode: "allowlist", entries: ["abstractframework.ai"], always_allow: ["abstractframework.ai"], always_deny: [] });
  const first = { mode: $("my-email-policy-mode").value, allow: chips("my-email-allow-list"), deny: chips("my-email-deny-list") };
  $("my-email-deny-add").value = "xxx.gov";
  await ctx.add("always_deny");
  const added = { deny: chips("my-email-deny-list"), input: $("my-email-deny-add").value, state: states["my-email-deny-state"], call: calls[calls.length - 1] };
  // Remove a chip with its x button.
  await $("my-email-allow-list").children[0].children[1].onclick();
  const removed = { allow: chips("my-email-allow-list"), state: states["my-email-allow-state"], call: calls[calls.length - 1] };
  // The entity modal reuses the renderer with its own API base.
  ctx.render({ mode: "denylist", entries: ["spam.example"], always_allow: [], always_deny: ["spam.example"] }, "/api/gateway/accounts/ember/email");
  $("my-email-allow-add").value = "partner.example";
  await ctx.add("always_allow");
  const entity = { mode: $("my-email-policy-mode").value, call: calls[calls.length - 1] };
  // A failed save says why and shows what is stored.
  fail = "The Always denied entry is not valid: '*.gov': patterns are not supported.";
  $("my-email-deny-add").value = "*.gov";
  await ctx.add("always_deny");
  const failed = { deny: chips("my-email-deny-list"), input: $("my-email-deny-add").value, state: states["my-email-deny-state"] };
  let missing = null;
  try { ctx.render({ mode: "allowlist", entries: [] }); } catch (e) { missing = e.message; }
  console.log(JSON.stringify({ first, added, removed, entity, failed, missing }));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.basic
def test_one_renderer_draws_both_lists_and_auto_saves_through_the_api_base() -> None:
    from node_requirement import require_node

    node = require_node()
    html = _html()
    fns = _slice(html, "    const RECIPIENT_RULES_BASE", "    async function loadMyEmail() {")
    js = "const vm = require('vm');\n" + f"const FNS = {json.dumps(fns)};\n" + HARNESS
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "h.js"
        path.write_text(js, encoding="utf-8")
        proc = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["first"] == {"mode": "allowlist", "allow": ["abstractframework.ai"], "deny": ["(Nobody yet.)"]}
    assert out["added"]["deny"] == ["xxx.gov"] and out["added"]["input"] == ""
    assert out["added"]["state"] == ["xxx.gov added.", "ok"]
    assert out["added"]["call"] == ["PUT", "/api/gateway/me/email/policy", {"mode": "allowlist", "always_allow": ["abstractframework.ai"], "always_deny": ["xxx.gov"]}]
    assert out["removed"]["allow"] == ["(Nobody yet: your agents may send only to your own address.)"]
    assert out["removed"]["call"][2] == {"mode": "allowlist", "always_allow": [], "always_deny": ["xxx.gov"]}
    assert out["entity"]["mode"] == "denylist"
    assert out["entity"]["call"] == ["PUT", "/api/gateway/accounts/ember/email/policy", {"mode": "denylist", "always_allow": ["partner.example"], "always_deny": ["spam.example"]}]
    assert out["failed"]["deny"] == ["spam.example"] and out["failed"]["input"] == "*.gov"
    assert out["failed"]["state"][1] == "error" and "patterns are not supported" in out["failed"]["state"][0]
    # A policy without the two lists fails loudly (never silently drawn empty).
    assert out["missing"] and "always_allow" in out["missing"]
