"""Round 11 (DESIGN.md R11.1 FINAL / R11.2) console Workspaces modals — markup and script contract,
always on (the browser proof is test_gateway_console_browser_r11w2.py, opt-in):

- There is no shared workspace: no "Shared workspace" string, no `shared_workspace` field, no
  path-check-on-blur rows, no hand-built posture/permission controls in the console — BOTH modals
  mount the kit WorkspaceChooser (islands mountWorkspaceChooser) and build its state with the
  islands' workspaceAsState.
- "Eligible workspaces" (admins, first control at the top of Accounts; the Runtimes default plane's
  Workspace cell) = level "gateway" on GET/PUT /workspace/policy.
- The workspace icon on every account row = level "account" on GET/PUT
  /workspace/policy/{account} (`me` for the signed-in user's own row).
- Every change is ONE PUT of the chooser's payload; the gateway's refusal sentence is re-thrown as
  is (the kit adds "Not saved.").
- The vendored islands bundle is kit 0.8.2 (the round-11 chooser and its wording table).
"""

from __future__ import annotations

import re

import pytest


@pytest.fixture(scope="module")
def html() -> str:
    from abstractgateway.console import gateway_console_html

    return gateway_console_html()


def _script(html: str) -> str:
    return html[html.rindex("<script>") :]


def _modals_js() -> str:
    from abstractgateway.console_workspaces import WORKSPACES_JS

    return WORKSPACES_JS[WORKSPACES_JS.index("// ---- Workspaces modals (round 11") :]


def test_no_shared_workspace_anywhere_in_the_console(html: str) -> None:
    # The console's own markup and script (the vendored kit bundle names `shared_workspace` only to
    # refuse an older gateway's answer loudly).
    own = re.sub(r'<script[^>]*id="af-console-islands"[^>]*>.*?</script>', "", html, flags=re.S)
    assert len(own) < len(html) - 100_000
    for gone in ("Shared workspace", "shared workspace", "shared_workspace", "Shared workspace &amp;", "sharedLabel", "wsSharedField", "wsg-shared",
                 "/api/gateway/workspace/path-check", "wsFoldersField", "wsPostureField", "wsDefaultModeField", "wsGatewaySummary", "data-ws-reset", "onPut"):
        assert gone not in own, gone


def test_eligible_workspaces_button_and_gateway_level(html: str) -> None:
    head = html[html.index('<div class="accounts-head__actions">') : html.index('id="open-create-user"')]
    assert re.search(r'<button id="accounts-gw-workspace" class="[^"]*\bhidden\b[^"]*" type="button">Eligible workspaces</button>', head)
    assert '<h2 id="gateway-workspace-title" class="af-modal__title">Eligible workspaces</h2>' in html
    assert '$("accounts-gw-workspace").classList.toggle("hidden", !p.admin);' in html
    assert '$("accounts-gw-workspace").onclick = () => openGatewayWorkspace();' in html
    js = _modals_js()
    gw = js[js.index("async function openGatewayWorkspace()") : js.index("function wsAccountKey(")]
    assert 'level: "gateway", path: "/api/gateway/workspace/policy"' in gw
    assert "wsText().gatewayTitle" in gw
    # The Runtimes default plane opens the same modal.
    assert 'wsOpen("Eligible workspaces", "Eligible workspaces of this gateway", () => openGatewayWorkspace())' in html


def test_account_level_on_every_row(html: str) -> None:
    js = _modals_js()
    acc = js[js.index("async function openAccountWorkspace(a)") :]
    assert 'level: "account", path: `/api/gateway/workspace/policy/${encodeURIComponent(wsAccountKey(a))}`' in acc
    assert 'if (a.own) return "me";' in js
    assert "${wsText().title} — ${a.id}" in acc
    render = _script(html)
    render = render[render.index("function renderAccounts(rows)") : render.index("function askRotateAccount(")]
    # The icon goes on EVERY live row the gateway marks available (humans, entities, admins), before
    # the user/entity branch; its tooltip is the R9.2 sentence.
    ws = render.index('add("workspace", "folder"')
    assert ws < render.index('if (a.kind === "entity") {\n            add("manage"')
    assert "openAccountWorkspace(a)" in render
    assert "workspace: (n) => `Workspaces ${n}'s agents may use`" in html


def test_one_kit_chooser_one_put_per_change_refusal_sentence_as_is() -> None:
    js = _modals_js()
    body = js[js.index("async function wsOpenLevel(o)") : js.index("function wsClose(")]
    assert "lib.mountWorkspaceChooser(host, Object.assign({}, props))" in body
    assert "lib.workspaceAsState(out, o.level)" in body and "lib.workspaceAsState(await api(o.path), o.level)" in body
    # save(payload) = exactly one PUT of the chooser's payload, nothing rebuilt here.
    save = body[body.index("save: async (payload) => {") : body.index("wsStore[o.island] = lib.mountWorkspaceChooser")]
    assert save.count("api(") == 1 and 'api(o.path, { method: "PUT", body: JSON.stringify(payload) })' in save
    assert "throw new Error(emailErrorText(e))" in save  # detail.message, as is; the kit appends "Not saved."
    assert "Not saved" not in re.sub(r"^\s*//.*$", "", js, flags=re.M)
    # No console-side policy logic: no clamp, no path check, no cap computation, no hand-built controls.
    code = re.sub(r"^\s*//.*$", "", js, flags=re.M)
    for gone in ("posture ===", "default_mode", "folders", "cap", "radiogroup", "afSwitchCreate", "data-ws-mode", "data-ws-posture"):
        assert gone not in code, gone
    # The bundle must carry the round-11 API, or the console fails loudly.
    assert 'typeof lib.workspaceAsState !== "function"' in js and "ui-kit 0.8.2+ required" in js


def test_vocabulary_workspaces_never_folders() -> None:
    js = _modals_js()
    strings = re.findall(r'"([^"\\]*)"|`([^`]*)`', re.sub(r"^\s*//.*$", "", js, flags=re.M))
    texts = [a or b for a, b in strings]
    offenders = [t for t in texts if re.search(r"folders?|shared", t, flags=re.I)]
    assert offenders == [], offenders


def test_vendored_islands_are_the_round11_kit() -> None:
    from abstractgateway.console_islands import ISLANDS_JS

    assert ISLANDS_JS.startswith("/*! @abstractframework/ui-kit 0.8.2 console islands")
    for needle in ("workspaceAsState", "Eligible workspaces", "The gateway allows this workspace read-only", "Follow the gateway policy", "Use my default", "Add a workspace path"):
        assert needle in ISLANDS_JS, needle
    assert "Shared workspace" not in ISLANDS_JS
