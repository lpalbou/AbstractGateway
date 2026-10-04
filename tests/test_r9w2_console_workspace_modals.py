"""Round 9 (DESIGN.md R9.2) console markup contract, always on (the browser proof is
test_gateway_console_browser_r9w2.py, opt-in):

- NO "Workspaces" sidebar entry or page; nothing of the old policy model (access mode, any folder
  for old clients, per-user override map, /workspace/policy/self) is left in the console.
- Accounts: an admin-only "Shared workspace & allowed folders" button at the top opens the GATEWAY
  policy modal (GET/PUT /workspace/policy); the folder icon on every account row (users, entities,
  admins) opens THAT account's modal, the kit WorkspaceChooser (islands mountWorkspaceChooser,
  GET/PUT /workspace/policy/{account}).
- Tooltips: every console icon button carries an explicit sentence in `data-af-tip` (the kit
  tooltip, bound once through the islands' bindTooltips) and no native `title`; the Accounts
  sentences are the ones R9.2 names.
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


def test_no_workspaces_page_and_no_old_model(html: str) -> None:
    nav = html[html.index('<nav class="shell_nav"') : html.index("</nav>")]
    assert re.findall(r'id="tab-button-([a-z]+)"', nav)[:2] == ["users", "workflows"]
    for gone in ('id="tab-button-workspaces"', 'id="tab-workspaces"', 'id="workspaces-root"', "loadWorkspaces", "renderWorkspaces",
                 "openWorkspacesFor", "workspacesLink", 'TABS = ["users", "workspaces"', "old clients", "Any folder (old", "client_workspace_scope_overrides",
                 "workspace_default_mode", "user_workspace_policies", "user-workspace-policy", "/workspace/policy/self", "userPolicyKeys",
                 "Allow everything except", "Allow my list"):
        assert gone not in html, gone
    # A saved tab or a `#workspaces[?account=]` link lands on Accounts (folded), never a blank page.
    assert 'const TAB_FOLDS = { entities: "users", engines: "providers", workspaces: "users" };' in html


def test_accounts_gateway_policy_button_and_modals(html: str) -> None:
    head = html[html.index('<div class="accounts-head__actions">') : html.index('id="open-create-user"')]
    # The FIRST control at the top of Accounts, admin-only (hidden until the principal is an admin).
    assert re.search(r'<button id="accounts-gw-workspace" class="[^"]*\bhidden\b[^"]*" type="button">Shared workspace &amp; allowed folders</button>', head)
    assert '$("accounts-gw-workspace").classList.toggle("hidden", !p.admin);' in html
    assert '$("accounts-gw-workspace").onclick = () => openGatewayWorkspace();' in html
    for mid in ("gateway-workspace", "account-workspace"):
        assert f'id="{mid}-backdrop" class="af-modal-backdrop" hidden' in html
        assert f'$("{mid}-close").onclick = close' in html
    assert '<h2 id="gateway-workspace-title" class="af-modal__title">Shared workspace &amp; allowed folders</h2>' in html
    js = _script(html)
    gw = js[js.index("function wsText()") : js.index("function wsAccountKey(")]
    # One PUT per change, the gateway policy fields of the R9 WORKSPACE API, path-checked rows.
    assert 'api("/api/gateway/workspace/policy")' in gw and 'api("/api/gateway/workspace/policy", { method: "PUT"' in gw
    from abstractgateway.console_workspaces import WORKSPACES_JS

    modals = WORKSPACES_JS[WORKSPACES_JS.index("// ---- Workspace folders modals (round 9).") :]
    for field in ("shared_workspace", "posture", "default_mode", "folders"):
        assert field in gw, field
    # Exactly two dimensions (DESIGN R9 FINAL): no other control in either modal.
    for gone in ("launch_folder_trust", "Launch-folder trust", "allowed_folders", "never_allowed", "allow_any_folder", "Allow any folder", "other_sessions", "Other sessions", "afSwitchCreate", "wsSwitchField"):
        assert gone not in modals, gone
    # The two postures (R9 amendments) as a segmented switch, the matching list under it.
    # The words come from the kit chooser's table (islands workspaceChooserText), never retyped here.
    for key in ("T.postureAllowedOnly", "T.postureAnyExceptDenied", "T.accessRead", "T.accessReadWrite", "T.accessDenied", "T.everythingElse", "T.sharedLabel"):
        assert key in gw, key
    assert 'role", "radiogroup"' in gw and "lib.workspaceChooserText" in js
    assert 'if (p.posture === "allowed_only") {' in gw and "put({ posture: next })" in gw and "put({ default_mode: m })" in gw and "put({ folders: list })" in gw
    # The effective line format (ADVERSARY V15): posture label · Shared workspace (rw) · path (mode).
    assert 'join(" · ")' in gw and 'deny: "denied"' in js
    assert "/api/gateway/workspace/path-check" in js and "input.onblur = async" in js
    acc = js[js.index("async function openAccountWorkspace(a)") :]
    assert "lib.mountWorkspaceChooser(host," in acc and "/api/gateway/workspace/policy/${encodeURIComponent(key)}" in acc
    assert 'if (a.own) return "me";' in js


def test_folder_icon_on_every_account_with_the_r92_sentences(html: str) -> None:
    js = _script(html)
    tips = js[js.index("const ACCOUNT_TIPS = {") : js.index("};", js.index("const ACCOUNT_TIPS = {"))]
    for sentence in (
        "`Email address and mailbox of ${n}`",
        "`OpenAI API access for ${n}`",
        "`Activity log of ${n}`",
        "`Workspace folders ${n}'s agents may use`",
        "`Manage ${n} (mind, voice, prompt…)`",
        "`Rotate ${n}'s sign-in token`",
        "`Archive ${n} (kept, hidden)`",
    ):
        assert sentence in tips, sentence
    render = js[js.index("function renderAccounts(rows)") : js.index("function askRotateAccount(")]
    assert "accountIconButton(icon, ACCOUNT_TIPS[key](a.id), aria, danger)" in render
    # The folder icon is added for EVERY live account, before the user/entity branch.
    ws = render.index('add("workspace", "folder"')
    assert ws < render.index('if (a.kind === "entity") {\n            add("manage"')
    assert "openAccountWorkspace(a)" in render


def test_icon_buttons_use_the_kit_tooltip_never_a_native_title(html: str) -> None:
    assert "data-tip" not in html.replace("data-tip-", "")
    assert not re.search(r"\.icon-btn\[data-tip", html)
    # Bound once, through the islands; a bundle without it fails loudly.
    assert "islands.tooltips = lib.bindTooltips(document);" in html
    assert 'typeof lib.bindTooltips !== "function") throw new Error(' in html
    from abstractgateway.console_islands import ISLANDS_CSS, ISLANDS_JS

    assert "bindTooltips" in ISLANDS_JS and "mountWorkspaceChooser" in ISLANDS_JS and "workspaceChooserText" in ISLANDS_JS
    assert ".af-tooltip" in ISLANDS_CSS and "--z-tooltip" in ISLANDS_CSS
    # Static icon-only buttons: data-af-tip, no title.
    for tag in re.findall(r"<button[^>]*>", html):
        if re.search(r'class="[^"]*\b(icon-only|af-modal__close|shell_nav_close|shell_nav_toggle|af-topbar__btn)\b', tag):
            assert "data-af-tip=" in tag and " title=" not in tag, tag
    js = _script(html)
    # Dynamic icon buttons: accounts, workflows, apps gears, OpenAI key eye, model trash.
    assert 'b.setAttribute("data-af-tip", tip);' in js
    wf = js[js.index("function workflowIconButton(") : js.index("function workflowActionButtons(")]
    assert 'b.setAttribute("data-af-tip", title);' in wf and "b.title" not in wf
    for sentence in ("`Export ${label} as a .flow file`", "`Open ${label} in AbstractFlow`", "`Archive ${label} (kept, hidden)`"):
        assert sentence in js, sentence
    for tag in re.findall(r"<button[^`]*?ui-icon-btn[^`]*?>", html):
        assert "data-af-tip=" in tag and " title=" not in tag, tag
    assert re.search(r'class="ui-btn is-ghost oai-eye"[^>]*data-af-tip="\$\{oaiStore.reveal \? "Hide your API key" : "Show your API key"\}"', html)
    trash = js[js.index("function mcDeleteButton(") : js.index("}", js.index('data-mc-action="delete-ask"', js.index("function mcDeleteButton(")))]
    assert "data-af-tip" in trash and "title=" not in trash
