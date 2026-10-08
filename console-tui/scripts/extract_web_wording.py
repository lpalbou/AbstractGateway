#!/usr/bin/env python3
"""Extract the web console's tooltip sentences of the two R15 reference screens.

The terminal console says the web console's words (DESIGN-TUI.md §8). The crate is published
alone, so `cargo test` (tests/r15_wording.rs) checks the terminal's tooltips against a copy:
`tests/fixtures/r15_web_wording.json`. THIS script rebuilds that copy from the live web sources
of the same repository and, by default, exits non-zero when the copy differs:

    python3 scripts/extract_web_wording.py           # check the fixture against the web
    python3 scripts/extract_web_wording.py --write   # regenerate the fixture

What it reads (each anchor is required; a missing file or anchor is a FAILURE, never a pass):

- `src/abstractgateway/console_workspaces.py`: the `ACCOUNT_TIPS` table (the Accounts row
  actions' tooltips), as {key: template} with `{n}` for the account.
- `src/abstractgateway/console.py`: the Accounts confirmation sentences (archive user/entity,
  suspend, deactivate, rotate) and the "Email for everyone" switches (label + description).
- `src/abstractgateway/console_ui.py`: every template literal holding `${name}` inside the app
  card (from `const APP_FIRST_RUN` to the card's settings gear), with `${name}` written `{n}`.
  Any other `${...}` expression stays as written; the test reads it as "any text".
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

CRATE = Path(__file__).resolve().parent.parent
REPO = CRATE.parent
WEB = REPO / "src" / "abstractgateway"
FIXTURE = CRATE / "tests" / "fixtures" / "r15_web_wording.json"


def fail(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    sys.exit(f"extract_web_wording: {msg}")


def read(name: str) -> str:
    p = WEB / name
    if not p.is_file():
        fail(f"{p} not found (the web source is required; a missing file is a FAILURE).")
    return p.read_text(encoding="utf-8")


def account_tips() -> dict[str, str]:
    src = read("console_workspaces.py")
    m = re.search(r"const ACCOUNT_TIPS = \{\n(.*?)\n\s*\};", src, re.S)
    if not m:
        fail("no `const ACCOUNT_TIPS = { ... };` block in console_workspaces.py.")
    out = {}
    for line in m.group(1).splitlines():
        e = re.fullmatch(r"\s*(\w+): \(n\) => `([^`]*)`,?", line)
        if not e:
            fail(f"ACCOUNT_TIPS line not understood: {line!r}")
        out[e.group(1)] = e.group(2).replace("${n}", "{n}")
    if len(out) < 9:
        fail(f"ACCOUNT_TIPS has {len(out)} entries; expected at least 9.")
    return out


def app_tips() -> list[str]:
    src = read("console_ui.py")
    start = src.find("const APP_FIRST_RUN")
    end_anchor = 'data-af-tip="${esc(`${name} settings`)}"'
    end = src.find(end_anchor, start)
    if start < 0 or end < 0:
        fail("the app card anchors (`const APP_FIRST_RUN` … the settings gear's data-af-tip) are missing in console_ui.py.")
    region = src[start : end + len(end_anchor)]
    found = []
    for m in re.finditer(r"`([^`]*\$\{name\}[^`]*)`", region):
        t = m.group(1)
        if "<" in t or "data-" in t:
            continue  # markup between two template literals, not a sentence
        t = t.replace("${name}", "{n}")
        if t not in found:
            found.append(t)
    for must in ("Install the newest {n} terminal app", "Bring the {n} to the front on this computer", "{n} settings"):
        if must not in found:
            fail(f"expected app tooltip {must!r} not found in the app card; the anchors moved.")
    return found


def one(src: str, pattern: str, what: str) -> str:
    m = re.findall(pattern, src)
    if len(m) != 1:
        fail(f"{what}: expected exactly one match in console.py, found {len(m)} (the anchor moved).")
    return m[0]


def account_confirms() -> dict[str, str]:
    """The Accounts confirmation sentences ({n} = the account id)."""
    src = read("console.py")
    out = {
        "archive_user": one(src, r"user: \(id\) => `(Archive \$\{id\}\? They can't[^`]*)`", "archive (user)"),
        "archive_entity": one(src, r"entity: \(id\) => `(Archive \$\{id\}\? It stops acting[^`]*)`", "archive (entity)"),
        "suspend": one(src, r"`(Suspend \$\{a\.id\}\?[^`]*)`", "suspend"),
        "deactivate": one(src, r"`(Deactivate \$\{a\.id\}\?[^`]*)`", "deactivate"),
        "rotate": one(src, r"userConfirmRow\(tr, `(Rotate the token of \$\{a\.id\}\?[^`]*)`", "rotate"),
    }
    return {k: v.replace("${id}", "{n}").replace("${a.id}", "{n}") for k, v in out.items()}


def email_switches() -> list[dict[str, str]]:
    """The "Email for everyone" card's switches: label + description."""
    src = read("console.py")
    out = []
    for cap in ("email", "agent-tools", "recovery"):
        m = re.search(
            rf'id="email-cap-{cap}".*?<span class="af-switch__label">([^<]*)</span>'
            rf'<span class="af-switch__desc" id="email-cap-{cap}-desc">([^<]*)</span>',
            src,
        )
        if not m:
            fail(f"the email-cap-{cap} switch markup moved in console.py.")
        out.append({"label": html.unescape(m.group(1)), "desc": html.unescape(m.group(2))})
    return out


def build() -> dict:
    return {
        "_source": "scripts/extract_web_wording.py (do not edit by hand)",
        "account_tips": account_tips(),
        "app_tips": app_tips(),
        "account_confirms": account_confirms(),
        "email_switches": email_switches(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="regenerate the fixture")
    a = ap.parse_args()
    text = json.dumps(build(), indent=2, ensure_ascii=False) + "\n"
    if a.write:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(text, encoding="utf-8")
        print(f"wrote {FIXTURE}")
        return 0
    if not FIXTURE.is_file():
        fail(f"{FIXTURE} missing; run with --write.")
    if FIXTURE.read_text(encoding="utf-8") != text:
        print(f"{FIXTURE} differs from the web sources; run with --write, then make the terminal match (cargo test says where).")
        return 1
    print("r15 web wording: fixture matches the web sources")
    return 0


# ---------------------------------------------------------------------------
# R15 per-screen fixtures (append-only): each screen worker adds ONE function
# returning the web words its terminal screen must say, and registers it in
# SCREEN_FIXTURES under the screen's name. The fixture is
# tests/fixtures/r15_web_wording_<screen>.json; a missing anchor FAILS.
# ---------------------------------------------------------------------------

SCREEN_FIXTURES: dict = {}


def need(src: str, pattern: str, what: str, flags: int = 0) -> str:
    """Exactly one match of `pattern` (its group 1, or the whole match)."""
    m = list(re.finditer(pattern, src, flags))
    if len(m) != 1:
        fail(f"{what}: expected exactly one match, found {len(m)} (the anchor moved).")
    g = m[0].groups()
    return html.unescape(g[0] if g else m[0].group(0))


def need_first(src: str, pattern: str, what: str, flags: int = 0) -> str:
    """The first match of `pattern` (its group 1); none is a FAILURE."""
    m = re.search(pattern, src, flags)
    if not m:
        fail(f"{what}: no match (the anchor moved).")
    return html.unescape(m.group(1) if m.groups() else m.group(0))


def connection_words() -> dict:
    """R15-A Connection: the web sign-in card's words (the TUI's sign-in surface)."""
    src = read("console.py")
    return {
        "token_label": need(src, r'<label class="af-gateway-signin__label" for="login-token">([^<]*)</label>', "token label"),
        "user_label": need(src, r'<label class="af-gateway-signin__label" for="login-user">([^<]*)</label>', "user label"),
        "show": need(src, r'<button id="toggle-token"[^>]*>([^<]*)</button>', "token reveal"),
        "show_tip": need(src, r'<button id="toggle-token"[^>]*aria-label="([^"]*)"', "token reveal label"),
        "hide": need(src, r'\$\("toggle-token"\)\.textContent = visible \? "Show" : "([^"]*)";', "token hide"),
        "hide_tip": need(src, r'setAttribute\("aria-label", visible \? "Show token" : "([^"]*)"\);', "token hide label"),
        "sign_in": need(src, r'<button id="login-button"[^>]*>([^<]*)</button>', "sign in button"),
        "recovery_link": need(src, r'<button id="recovery-link"[^>]*>([^<]*)</button>', "recovery link"),
        "code_label": need(src, r'<label class="af-gateway-signin__label" for="recovery-code-input">([^<]*)</label>', "code label"),
        "resend": need(src, r'<button id="recovery-resend"[^>]*>([^<]*)</button>', "resend"),
        "use_code": need(src, r'<button id="recovery-use"[^>]*>([^<]*)</button>', "use code"),
        "back": need(src, r'<button id="recovery-back"[^>]*>([^<]*)</button>', "back to token"),
        "network_tip": need(src, r'<button id="tab-button-network"[^>]*title="([^"]*)"', "network nav tooltip"),
    }


SCREEN_FIXTURES["connection"] = connection_words


def preferences_time_zone_words() -> dict:
    """R16-W2 Accounts → Preferences → Time zone: the web modal row's words (console.py) and the kit
    AfTimeZonePicker's (the vendored islands bundle carries ui-kit automation_controls.json)."""
    src = read("console.py")
    isl = read("console_islands.py")
    return {
        "saved": need(src, r'renderAccountPreferences\(a, answer, \{ iface: "time_zone", ok: true, text: "([^"]*)" \}\)', "time zone Saved."),
        "not_saved_prefix": need(src, r'note: \{ ok: false, text: `(Not saved\. )\$\{emailErrorText\(e\)\}` \} \}\)\);\n          \}\n        \},\n      \};\n      accountsUi\.preferencesTz', "time zone Not saved."),
        "seam": need(src, r'throw new Error\("(GET /accounts/\{id\}/preferences answered without a time_zone block \(R16\.1 preferences seam\)\.)"\);', "time zone seam sentence"),
        "gateway_default": need(isl, r'(Gateway default \(\{time_zone\}\))', "kit time_zone_default"),
        "search": need(isl, r'(Search time zones)', "kit time_zone_search"),
    }


SCREEN_FIXTURES["preferences_time_zone"] = preferences_time_zone_words


def workflows_words() -> dict:
    """R15-A Workflows: the page's tooltips, sentences, switches and columns ({n} = the workflow)."""
    src = read("console.py")
    ui = read("console_ui.py")
    def tip(cls: str) -> str:
        t = need(src, r'workflowIconButton\("' + cls + r'", ICONS\.\w+, `([^`]*)`', f"{cls} tooltip")
        return t.replace("${label}", "{n}").replace("${row.name}", "{n}")
    th = need(src, r'<thead><tr>(<th class="workflows-col-name">.*?)</tr></thead>', "workflows table head")
    cols = re.findall(r'<th[^>]*>(?:<span[^>]*>)?([^<]+)<', th)
    if len(cols) < 6:
        fail(f"workflows table head: expected 6+ columns, found {cols!r}.")
    def switch(id_: str) -> str:
        return need(src, r'afSwitchCreate\(\{ id: "' + id_ + r'", label: "([^"]*)"', f"{id_} switch")
    return {
        "export_tip": tip("workflows-export"),
        "open_tip": tip("workflows-open-flow"),
        "unarchive_tip": tip("workflows-unarchive"),
        "archive_tip": tip("workflows-archive"),
        "edit_tip": tip("workflows-desc-edit"),
        "archive_confirm": need(src, r'const text = `(Archive \$\{label\}\? It disappears[^`]*)`', "archive confirm").replace("${label}", "{n}"),
        "reload_tip": need(src, r'<button id="workflows-refresh"[^>]*data-af-tip="([^"]*)"', "reload tooltip"),
        "import_label": need(src, r'<button id="workflows-import"[^>]*>([^<]*)</button>', "import label"),
        "import_tip": need(src, r'<button id="workflows-import"[^>]*title="([^"]*)"', "import tooltip"),
        "search_placeholder": need(src, r'<input id="workflows-search"[^>]*placeholder="([^"]*)"', "search placeholder"),
        "broken_archive_tip": need(src, r'`Archive \$\{g\.count\}`;\s*ar\.title = "([^"]*)";', "broken archive tooltip"),
        "purpose": need(src, r'<p class="section-note workflows-purpose">([^<]*)</p>', "purpose"),
        "available_help": need(src, r'const WORKFLOW_AVAILABLE_HELP = "([^"]*)";', "available help"),
        "broken_sentence": need(src, r'<p class="section-note">(These bundle files are on disk[^<]*)</p>', "broken sentence"),
        "streaming_label": need(ui, r'const STREAMING_DEFAULT_LABEL = "([^"]*)";', "streaming label"),
        "streaming_help": need(ui, r'const STREAMING_DEFAULT_DESC = "([^"]*)";', "streaming help"),
        "other_types": need(ui, r'<h3 id="agent-defaults-other-h" class="section-subtitle">([^<]*)</h3>', "other workflow types"),
        "settings": need(ui, r'<h3 class="section-subtitle">([^<]*)</h3>`;\n\s*if \(!r \|\| typeof r !== "object"', "settings subheading", re.S),
        "switch_drafts": switch("workflows-show-drafts"),
        "switch_older": switch("workflows-show-older"),
        "switch_archived": switch("workflows-show-archived"),
        "col_name": cols[0],
        "col_what": cols[1],
        "col_version": cols[2],
        "col_source": cols[3],
        "col_usedby": cols[4],
        "col_available": cols[5],
    }


SCREEN_FIXTURES["workflows"] = workflows_words


def skills_words() -> dict:
    """R15-A Skills & MCP: tabs, toolbars, row buttons, the agents question, columns."""
    src = read("console.py")
    sk = read("console_skills_mcp.py")
    ui = read("console_ui.py")
    th = need(src, r'<thead><tr>(<th class="sk-col-name">.*?)</tr></thead>', "skills table head")
    cols = re.findall(r'<th[^>]*>(?:<span[^>]*>)?([^<]+)<', th)
    if len(cols) < 5:
        fail(f"skills table head: expected 5+ columns, found {cols!r}.")
    def btn(data: str) -> str:
        return need_first(sk, r'<button type="button" class="secondary" ' + data + r'="\$\{n\}">([^<]*)</button>', data)
    confirm = need(sk, r'<span>(Offer its \$\{k\} tool\$\{k === 1 \? "" : "s"\} to your agents\?[^<]*)</span>', "agents confirm")
    return {
        "tab_skills": need(src, r'<button id="skmcp-tab-skills"[^>]*>([^<]*)</button>', "skills tab"),
        "tab_mcp": need(src, r'<button id="skmcp-tab-mcp"[^>]*>([^<]*)</button>', "mcp tab"),
        "search_placeholder": need(src, r'<input id="skills-search"[^>]*placeholder="([^"]*)"', "skills search"),
        "import_zip": need(src, r'<button id="skills-import-zip"[^>]*>([^<]*)</button>', "import zip"),
        "import_zip_tip": need(src, r'<button id="skills-import-zip"[^>]*title="([^"]*)"', "import zip tooltip"),
        "import_folder": need(src, r'<button id="skills-import-folder"[^>]*>([^<]*)</button>', "import folder"),
        "import_folder_tip": need(src, r'<button id="skills-import-folder"[^>]*title="([^"]*)"', "import folder tooltip"),
        "add_server": need(src, r'<button id="mcp-add"[^>]*>([^<]*)</button>', "add server"),
        "refresh_shelf": need(ui, r'\$\{st\.saving \? "Working\.\.\." : "([^"]*)"\}</button>', "refresh curated shelf"),
        "shelf_label": need(ui, r'<label for="skills-shelf-input">([^<]*)</label>', "shelf label"),
        "skills_purpose": need(src, r'<p class="section-note skmcp-purpose">(Instructions agents load[^<]*)</p>', "skills purpose"),
        "mcp_purpose": need(src, r'<p class="section-note skmcp-purpose">(Tool servers over[^<]*)</p>', "mcp purpose"),
        "agents_label": need_first(sk, r'<span class="af-switch__label">([^<]*)</span>', "agents label"),
        "skill_view": btn("data-skill-view"),
        "skill_export": btn("data-skill-export"),
        "skill_archive": btn("data-skill-archive"),
        "skill_unarchive": btn("data-skill-unarchive"),
        "mcp_edit": btn("data-mcp-edit"),
        "mcp_test": btn("data-mcp-test"),
        "mcp_archive": btn("data-mcp-archive"),
        "mcp_unarchive": btn("data-mcp-unarchive"),
        "agents_confirm": confirm.replace('${k === 1 ? "" : "s"}', "{s}").replace("${k}", "{k}"),
        "col_name": cols[0],
        "col_what": cols[1],
        "col_version": cols[2],
        "col_trust": cols[3],
        "col_source": cols[4],
    }


SCREEN_FIXTURES["skills"] = skills_words


def openai_words() -> dict:
    """R15-A OpenAI API: the cards' buttons, tooltips, segments and the New key question."""
    ui = read("console_ui.py")
    snips = re.findall(r'\["(\w+)", "([^"]+)"\]', need(ui, r'const OAI_SNIPPETS = (\[.*?\]);', "OAI_SNIPPETS"))
    if len(snips) != 3:
        fail(f"OAI_SNIPPETS: expected 3, found {snips!r}.")
    auth = re.findall(r'\{ id: "(\w+)", label: "([^"]+)", text: "([^"]+)" \}', need(ui, r'const OAI_AUTH = \[(.*?)\];', "OAI_AUTH", re.S))
    if len(auth) != 2:
        fail(f"OAI_AUTH: expected 2, found {auth!r}.")
    out = {
        "endpoint_tip": need(ui, r'<label class="ui-switch" title="([^"]*)"><input type="checkbox" role="switch" data-oai-enabled', "endpoint tooltip"),
        "restart_tip": need(ui, r'data-oai-action="restart" title="([^"]*)"', "restart tooltip"),
        "check_tip": need(ui, r'data-oai-action="check" title="([^"]*)"', "check tooltip"),
        "not_in_a_run": need_first(ui, r'disabled title="([^"]*)" data-oai-observer', "observer disabled tooltip"),
        "restart": need(ui, r'oaiStore\.busy === "restart" \? "Restarting\.\.\." : "([^"]*)"\}</button>', "restart label"),
        "check": need(ui, r'oaiStore\.busy === "check" \? "Checking\.\.\." : "([^"]*)"\}</button>', "check label"),
        "new_key": need_first(ui, r'data-oai-action="new-key"\$\{oaiStore\.busy \|\| oaiStore\.keyForm \? " disabled" : ""\}>([^<]*)</button>', "new key label"),
        "copy_example": need(ui, r'data-oai-copy-snippet>([^<]*)</button>', "copy example"),
        "copy": need(ui, r'aria-label="Copy \$\{esc\(label\)\}">([^<]*)</button>', "copy button"),
        "doc_openai": need(ui, r'rel="noopener">(OpenAI API compatibility)</a>', "openai docs link"),
        "doc_core": need(ui, r'rel="noopener">(AbstractCore server)</a>', "abstractcore docs link"),
        # Round 16: named API keys (console_ui.py oaiKey*; console.py accountOpenAIKeys).
        "keys_note": need(ui, r'<span class="ui-addr__note">(One key per app:[^<]*)</span>', "keys note"),
        "admin_token_label": need(ui, r'<span>(The gateway admin token)</span>', "admin token label"),
        "admin_token_note": need(ui, r'<span>The gateway admin token</span><span class="ui-addr__note">([^<]*)</span>', "admin token note"),
        "name_label": need(ui, r'<label class="oai-field__title" for="oai-key-label">([^<]*)</label>', "name label"),
        "name_placeholder": need(ui, r'data-oai-key-label maxlength="80" placeholder="([^"]*)"', "name placeholder"),
        "name_help": need(ui, r'id="oai-key-label-help">([^<]*)</p>', "name help"),
        "make_key": need(ui, r'"Making\.\.\." : "([^"]*)"\}</button>', "make key label"),
        "name_required": need(ui, r'if \(!label\) \{ oaiStore\.keyNotice = \{ tone: "err", text: "([^"]*)" \}', "name required"),
        "made_sentence": need(ui, r'data-oai-made><strong>(Key “\$\{esc\(m\.item\.label\)\}” made\.[^<]*)</strong>', "made sentence").replace("${esc(m.item.label)}", "{label}"),
        "copy_made_tip": need(ui, r'data-oai-action="copy-made" aria-label="([^"]*)"', "copy made tooltip"),
        "made_done": need(ui, r'data-oai-action="made-done">([^<]*)</button>', "done label"),
        "keys_empty": need(ui, r'data-oai-keys-empty>([^<]*)</div>', "keys empty"),
        "keys_table": need(ui, r'data-oai-keys aria-label="([^"]*)"', "keys table label"),
        "revoke": need(ui, r'data-af-tip="Revoke this key: apps using it stop working at once"\$\{[^}]*\}>([^<]*)</button>', "revoke label"),
        "revoke_tip": need(ui, r'data-oai-revoke="\$\{esc\(fp\)\}" data-af-tip="([^"]*)"', "revoke tooltip"),
        "revoke_confirm": need(ui, r'aria-label="Revoke \$\{esc\(key\.label\)\}"><p>(Revoke “\$\{esc\(key\.label\)\}”\?[^<]*)</p>', "revoke question").replace("${esc(key.label)}", "{label}"),
        "revoked": need(ui, r'text: `(Revoked “\$\{key\.label\}”: [^`]*)`', "revoked sentence").replace("${key.label}", "{label}"),
        "account_lead": need(read("console.py"), r'lead\.textContent = `(Lets \$\{a\.id\} use the OpenAI-compatible API[^`]*)`', "account OpenAI lead").replace("${a.id}", "{id}"),
        "account_keys_empty": need(read("console.py"), r'empty\.textContent = `(\$\{a\.id\} has no API keys\.[^`]*)`', "account keys empty").replace("${a.id}", "{id}"),
        "account_revoke_confirm": need(read("console.py"), r'q\.textContent = `(Revoke “\$\{k\.label\}” of \$\{a\.id\}\?[^`]*)`', "account revoke question").replace("${k.label}", "{label}").replace("${a.id}", "{id}"),
    }
    for i, (_, label) in enumerate(snips):
        out[f"snippet_{i}"] = label
    for i, (_, label, text) in enumerate(auth):
        out[f"auth_{i}_label"] = label
        out[f"auth_{i}_text"] = text
    return out


SCREEN_FIXTURES["openai"] = openai_words


def providers_words() -> dict:
    """R15-A Providers: the three sections, the endpoint modal, the connection button, the delete question."""
    src = read("console.py")
    region = need(src, r'(<div id="tab-providers".*?Multimodal Capabilities)', "providers tab region", re.S)
    titles = re.findall(r'<h2 class="section-title"><span class="section-icon[^"]*" aria-hidden="true">[^<]*</span><span>([^<]*)</span></h2>\s*<p class="section-note">([^<]*)</p>', region)
    if len(titles) < 3:
        fail(f"providers sections: expected 3 titles + notes, found {titles!r}.")
    def btn_label(word: str) -> str:
        return need(src, r'innerHTML = `<span class="button-icon" aria-hidden="true">[^<]*</span><span>(' + word + r')</span>`;', f"{word} button")
    return {
        "section_local": titles[0][0],
        "note_local": titles[0][1],
        "section_remote": titles[1][0],
        "note_remote": titles[1][1],
        "section_available": titles[2][0],
        "note_available": titles[2][1],
        "form_description": need(src, r'<p id="provider-modal-description">([^<]*)</p>', "modal description"),
        "key_placeholder": need(src, r'<input id="endpoint-api-key" type="password" placeholder="([^"]*)">', "api key placeholder"),
        "base_url_placeholder": need(src, r'<input id="endpoint-base-url" placeholder="([^"]*)">', "base url placeholder"),
        "description_placeholder": need(src, r'<textarea id="endpoint-description" placeholder="([^"]*)">', "description placeholder"),
        "visible_models": need(src, r'<h4 id="endpoint-visible-models-title" class="named-section__title">([^<]*)</h4>', "visible models"),
        "visible_models_help": need(src, r'<p class="field-help">(Optional\. Use Test to preview discovery[^<]*)</p>', "visible models help"),
        "clear_restriction_tip": need(src, r'<button id="clear-endpoint-models"[^>]*title="([^"]*)"', "clear restriction tooltip"),
        "test_tip": need(src, r'<button id="discover-endpoint-models"[^>]*title="([^"]*)"', "test tooltip"),
        "confirm_tip": need(src, r'<button id="save-endpoint-profile" title="([^"]*)"', "confirm tooltip"),
        "scope_gateway": need(src, r'opt\.textContent = value === "gateway" \? "([^"]*)" : "Only me";', "gateway scope"),
        "scope_user": need(src, r'opt\.textContent = value === "gateway" \? "Gateway-wide" : "([^"]*)";', "user scope"),
        "connect_tip": need(src, r'data-provider-connect="\$\{esc\(e\.id\)\}" title="([^"]*)"', "connect tooltip"),
        "delete_confirm": need(src, r'message: `(Delete \$\{profile\.virtual_provider \|\| "endpoint:" \+ profile\.id\}\? Existing workflows[^`]*)`', "delete question").replace('${profile.virtual_provider || "endpoint:" + profile.id}', "{n}"),
        "edit": btn_label("Edit"),
        "delete": btn_label("Delete"),
        "override": btn_label("Override"),
    }


SCREEN_FIXTURES["providers"] = providers_words


def runtimes_words() -> dict:
    """R15-A Runtimes: the inventory, the inspector's tabs, toolbars, row buttons, confirms and modals."""
    src = read("console.py")
    ws = read("console_workspaces.py")
    head = re.findall(r'runtimes: \["([^"]*)", "([^"]*)"\]', src)
    if len(head) != 1:
        fail(f"runtimes nav title: expected 1, found {head!r}.")
    ths = lambda region: re.findall(r"<th>([^<]*)</th>", region)
    inv = ths(need(src, r'(<thead><tr><th>Runtime</th>.*?</tr></thead>)', "inventory columns"))
    runs = ths(need(src, r'(<thead><tr><th>Run</th><th>Workflow</th><th>Status</th><th>Node</th>.*?</tr></thead>)', "runs columns"))
    ro = ths(need(src, r'(<thead><tr><th>Run</th><th>Workflow</th><th>Status</th><th>Session</th>.*?</tr></thead>)', "read-only runs columns"))
    arts = ths(need(src, r'(<thead><tr><th>Artifact</th>.*?</tr></thead>)', "artifacts columns"))
    caches = ths(need(src, r'(<thead><tr><th>Cache</th>.*?</tr></thead>)', "caches columns"))
    logs = ths(need(src, r'(<thead><tr><th>File</th>.*?</tr></thead>)', "logs columns"))
    statuses = re.findall(r'<option value="[^"]*">([^<]*)</option>', need(src, r'(<select id="runs-status".*?</select>)', "status select"))
    tails = re.findall(r'<option value="\d+">([^<]*)</option>', need(src, r'(<select id="log-modal-tail-size">.*?</select>)', "tail sizes"))
    out = {
        "title": head[0][0],
        "subtitle": head[0][1],
        "note": need(src, r'<span>Runtimes</span></h2>\s*<p class="section-note">([^<]*)</p>', "runtimes note"),
        "reload_tip": need(src, r'<button id="runtimes-refresh"[^>]*data-af-tip="([^"]*)"', "reload tooltip"),
        "detail_reload_tip": need(src, r'<button id="runtime-detail-refresh"[^>]*data-af-tip="([^"]*)"', "detail reload tooltip"),
        "teach": need(src, r'<p id="runtime-detail-teach" class="section-note">([^<]*)</p>', "teaching line"),
        "tab_runs": need(src, r'<button id="runtime-subtab-sessions"[^>]*>([^<]*)</button>', "Runs tab"),
        "tab_artifacts": need(src, r'<button id="runtime-subtab-artifacts"[^>]*>([^<]*)</button>', "Artifacts tab"),
        "tab_cache": need(src, r'<button id="runtime-subtab-caches"[^>]*>([^<]*)</button>', "Cache tab"),
        "tab_logs": need(src, r'<button id="runtime-subtab-logs"[^>]*>([^<]*)</button>', "Logs tab"),
        "chip": need(ws, r'label\.textContent = `(Account: )\$\{', "account chip"),
        "chip_clear_tip": need(ws, r'accountIconButton\("close", `(Show every runtime, not only \$\{filter\.account\}\'s)`', "chip clear tooltip").replace("${filter.account}", "{a}"),
        "eligible": need(src, r'wsOpen\("(Eligible workspaces)"', "eligible workspaces link"),
        "workspaces": need(src, r'policyTd\.append\(wsOpen\("(Workspaces)"', "workspaces link"),
        "none": need(src, r'policyTd\.textContent = "(None)";', "no workspaces"),
        "no_runtimes": need(src, r'"(No runtimes found\.)"', "no runtimes"),
        "status_tip": need(src, r'<select id="runs-status" title="([^"]*)"', "status tooltip"),
        "runs_search": need(src, r'<input id="runs-search"[^>]*placeholder="([^"]*)"', "runs search placeholder"),
        "root_only": need(src, r'<input id="runs-root-only" type="checkbox" checked> ([^<]*)</label>', "root runs only"),
        "root_only_tip": need(src, r'<label class="entity-checkbox" title="([^"]*)"><input id="runs-root-only"', "root only tooltip"),
        "readonly_note": need(src, r'<div id="runtime-runs-readonly" class="hidden">\s*<p class="section-note">([^<]*)</p>', "read-only note"),
        "inspect": need(src, r'inspect\.textContent = "([^"]*)";', "Inspect"),
        "steer": need(src, r'steer\.textContent = "([^"]*)";', "Steer"),
        "cancel": need(src, r'cancel\.textContent = "([^"]*)";', "Cancel"),
        "cancel_confirm": need(src, r'message: `(Cancel run \$\{runId\}\? [^`]*)`, confirmLabel', "cancel question").replace("${runId}", "{id}"),
        "cancel_go": need(src, r'Any in-flight work stops at the next tick\.`, confirmLabel: "([^"]*)"', "cancel button"),
        "steer_title": need(src, r'title: "(Steer run)",', "steer title"),
        "steer_lead": need(src, r'message: `(Guidance folds into \$\{runId\}[^`]*)`', "steer sentence").replace("${runId}", "{id}"),
        "steer_go": need(src, r'confirmLabel: "(Send guidance)"', "send guidance"),
        "steer_placeholder": need(src, r'input: \{ placeholder: "(e\.g\. focus on the failing test first[^"]*)" \}', "steer placeholder"),
        "modality_tip": need(src, r'<select id="runtime-artifacts-modality" title="([^"]*)"', "type tooltip"),
        "artifacts_search": need(src, r'<input id="runtime-artifacts-search"[^>]*placeholder="([^"]*)"', "artifacts placeholder"),
        "artifacts_note": need(src, r'note\.textContent = "(Artifacts are indexed[^"]*)";', "artifacts note"),
        "artifact_tip": need(src, r'tr\.title = `(Click to preview )\$\{name\}`;', "artifact row tooltip") + "{n}",
        "cache_kind_tip": need(src, r'<select id="runtime-caches-kind" title="([^"]*)"', "kind tooltip"),
        "caches_search": need(src, r'<input id="runtime-caches-search"[^>]*placeholder="([^"]*)"', "caches placeholder"),
        "caches_note": need(src, r'<div id="runtime-panel-caches".*?<p class="section-note">([^<]*)</p>', "caches note", re.S),
        "purge": need(src, r'<span class="button-icon" aria-hidden="true">×</span><span>(Purge…)</span>', "Purge…"),
        "purge_tip": need(src, r'btn\.title = "(Delete the CONTENTS of this cache[^"]*)";', "purge tooltip"),
        "purge_title": need(src, r'title: `(Purge )\$\{name\}\?`', "purge title") + "{n}?",
        "purge_confirm": need(src, r'message: `(This deletes the CONTENTS of \$\{name\}: [^`]*)`', "purge question").replace("${name}", "{n}").replace("${dry.files_deleted}", "{files}").replace("${_fmtBytes(dry.bytes_freed)}", "{bytes}"),
        "retained": need(src, r'<span>(Retained runtimes)</span></h2>', "retained title"),
        "retained_note": need(src, r'<span>Retained runtimes</span></h2>\s*<p class="section-note">([^<]*)</p>', "retained note"),
        "forget": need(src, r'forget\.innerHTML = `<span class="button-icon" aria-hidden="true">×</span><span>([^<]*)</span>`;', "Forget"),
        "forget_tip": need(src, r'forget\.title = "([^"]*)";', "forget tooltip"),
        "forget_all": need(src, r'<span>(Forget all stale) \(\$\{stale\.length\}\)</span>', "forget all stale"),
        "forget_all_tip": need(src, r'bulk\.title = "([^"]*)";', "forget all tooltip"),
        "forget_confirm": need(src, r'message: `(This removes \$\{label\} from the data-home registry\.[^`]*)`', "forget question").replace("${label}", "{l}"),
        "logs_home_tip": need(src, r'<select id="runtime-logs-home" title="([^"]*)"', "log home tooltip"),
        "logs_search": need(src, r'<input id="runtime-logs-search"[^>]*placeholder="([^"]*)"', "logs placeholder"),
        "log_tip": need(src, r'tr\.title = `(Click to tail )\$\{f\.name\}`;', "log row tooltip") + "{n}",
        "log_sub": need(src, r'\$\("log-modal-sub"\)\.textContent = `from \$\{home\}( — newest lines at the bottom)`;', "log modal sub"),
        "log_refresh_tip": need(src, r'<button id="log-modal-refresh"[^>]*data-af-tip="([^"]*)"', "log refresh tooltip"),
        "close": need(src, r'<button id="log-modal-close" class="secondary" type="button">([^<]*)</button>', "Close"),
        "prev": need(src, r'mk\("(‹ Prev)"', "Prev"),
        "next": need(src, r'mk\("(Next ›)"', "Next"),
    }
    for i, c in enumerate(inv):
        out[f"inv_col_{i}"] = c
    for i, c in enumerate(runs):
        out[f"runs_col_{i}"] = c
    for i, c in enumerate(ro):
        out[f"ro_col_{i}"] = c
    for i, c in enumerate(arts):
        out[f"art_col_{i}"] = c
    for i, c in enumerate(caches):
        out[f"cache_col_{i}"] = c
    for i, c in enumerate(logs):
        out[f"log_col_{i}"] = c
    for i, c in enumerate(statuses):
        out[f"status_{i}"] = c
    for i, c in enumerate(tails):
        out[f"tail_{i}"] = c
    return out


SCREEN_FIXTURES["runtimes"] = runtimes_words


def setup_words() -> dict:
    """R15-A Setup: the guide's welcome step — titles, lede, tiles' section, the three cards, the recommended set's buttons, the footer's Skip setup."""
    src = read("console.py")
    copy = need(src, r"const FIRST_RUN_STEP_COPY = \{(.*?)\n\s*\};", "FIRST_RUN_STEP_COPY", re.S)
    titles = need(src, r"const FIRST_RUN_STEP_TITLES = \{(.*?)\};", "FIRST_RUN_STEP_TITLES", re.S)
    def field(step: str, key: str) -> str:
        return need(copy, step + r': \{[^}]*?' + key + r': "([^"]*)"', f"{step}.{key}")
    def title(step: str) -> str:
        return need(titles, step + r': "([^"]*)"', f"title {step}")
    out = {
        "title": field("welcome", "title"),
        "hint": field("welcome", "hint"),
        "admin_lede": field("welcome", "lede"),
        "skip": need(src, r'<button id="first-run-skip"[^>]*>([^<]*)</button>', "Skip setup"),
        "skip_tip": need(src, r'<button id="first-run-skip" class="secondary" title="([^"]*)"', "Skip setup tooltip"),
        "finish": need(src, r'<button id="first-run-finish"[^>]*>([^<]*)</button>', "Finish"),
        "next": need(src, r'<button id="first-run-next">([^<]*)</button>', "Next"),
        "guide_tip": need(src, r'<button id="open-setup"[^>]*title="([^"]*)"', "Setup tooltip"),
        "sets_up": need(src, r'<h3>(What this guide sets up)</h3>', "what this guide sets up"),
        "sets_up_sub": need(src, r'<h3>What this guide sets up</h3><span class="ui-sub">([^<]*)</span>', "sets up sub"),
        "looking": need(src, r'<div class="ui-empty">(Looking at this computer\.\.\.)</div>', "looking"),
        "checking": need(src, r'<p class="subtle">(Checking the recommended starter models\.\.\.)</p>', "checking"),
        "recommended": need(src, r'<h3>(Recommended for this computer)</h3>', "recommended heading"),
        "no_text_model": need(src, r'"(No text model is set yet\.)"', "no text model"),
        "no_downloads": need(src, r'<div class="ui-empty">(This gateway reported no recommended downloads\.)</div>', "no downloads"),
        "apply": need(src, r'<button id="first-run-apply-recommended" class="ui-btn is-primary">([^<]*)</button>', "apply"),
        "download_all": need(src, r'<button id="first-run-download-all" class="ui-btn is-ghost">([^<]*)</button>', "download all"),
        "recommended_note": need(src, r'<span>(Sets the recommended models for text, voice[^<]*)</span>', "recommended note"),
        "replace": need(src, r'kept\.length \? "([^"]*)" : "Clear what cannot run here"', "replace mine too"),
        "clear_broken": need(src, r'kept\.length \? "Replace mine too" : "([^"]*)"', "clear what cannot run"),
        "not_available": need(src, r'<strong>(This computer\'s summary is not available right now\.)</strong>', "summary unavailable"),
    }
    for i, step in enumerate(["engines", "model", "apps"]):
        out[f"card_{i}_title"] = field(step, "title")
        out[f"card_{i}_lede"] = field(step, "lede")
        out[f"card_{i}_go"] = "Go to " + title(step).lower()
    return out


SCREEN_FIXTURES["setup"] = setup_words


def host_words() -> dict:
    """R15-A F3 host panel: the web's Gateway card (Resources) — title, note, switch, rows, buttons, questions."""
    src = read("console.py")
    sect = need(src, r'(<section id="gateway-host-section".*?</section>)', "gateway card", re.S)
    keys = re.findall(r'<span class="entity-kv-key">([^<]*)</span>', sect)
    out = {
        "title": need(sect, r'<span>(Gateway)</span></h2>', "title"),
        "note": need(sect, r'<p class="section-note">([^<]*)</p>', "note"),
        "pause": need(sect, r'<span class="af-switch__label">(Workflows paused)</span>', "pause label"),
        "pause_tip": need(sect, r'<button id="gateway-host-pause"[^>]*title="([^"]*)"', "pause tooltip"),
        "check": need(sect, r'<button id="gateway-host-update-check"[^>]*>([^<]*)</button>', "check now"),
        "check_tip": need(sect, r'<button id="gateway-host-update-check"[^>]*title="([^"]*)"', "check tooltip"),
        "update": need(sect, r'<button id="gateway-host-update-start"[^>]*>([^<]*)</button>', "update"),
        "update_tip": need(sect, r'<button id="gateway-host-update-start"[^>]*title="([^"]*)"', "update tooltip"),
        "restart": need(sect, r'<button id="gateway-host-restart"[^>]*>([^<]*)</button>', "restart"),
        "quit": need(sect, r'<button id="gateway-host-quit"[^>]*>([^<]*)</button>', "quit"),
        "login": need(sect, r'<span class="af-switch__label">(Start at login)</span>', "login switch"),
        "restart_question": need(src, r'title: "(Restart AbstractGateway\?)"', "restart title") + " " + need(src, r'title: "Restart AbstractGateway\?", message: "([^"]*)"', "restart message"),
        "restart_go": need(src, r'title: "Restart AbstractGateway\?"[^}]*confirmLabel: "([^"]*)"', "restart button"),
        "quit_question": need(src, r'title: "(Quit AbstractGateway\?)"', "quit title") + " " + need(src, r'title: "Quit AbstractGateway\?", message: "([^"]*)"', "quit message"),
        "quit_go": need(src, r'title: "Quit AbstractGateway\?"[^}]*confirmLabel: "([^"]*)"', "quit button"),
        "update_go": need(src, r'title: "Update available", message: action\.confirm, confirmLabel: "([^"]*)"', "update now"),
    }
    for i, k in enumerate(keys):
        out[f"row_{i}"] = k
    return out


SCREEN_FIXTURES["host"] = host_words


def screens_main(write: bool) -> int:
    rc = 0
    for name, build_fn in SCREEN_FIXTURES.items():
        path = CRATE / "tests" / "fixtures" / f"r15_web_wording_{name}.json"
        text = json.dumps({"_source": "scripts/extract_web_wording.py (do not edit by hand)", **build_fn()}, indent=2, ensure_ascii=False) + "\n"
        if write:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path}")
        elif not path.is_file() or path.read_text(encoding="utf-8") != text:
            print(f"{path} differs from the web sources; run with --write, then make the terminal match.")
            rc = 1
        else:
            print(f"r15 web wording ({name}): fixture matches the web sources")
    return rc



# ---------------------------------------------------------------------------
# R15 group B (worker B): one fixture per screen,
# tests/fixtures/r15_web_wording_<screen>.json, checked (or rewritten with
# --write) after the reference fixture. Each function reads its anchors from
# the web sources and FAILS when one is missing.
# ---------------------------------------------------------------------------


def read_b(name: str) -> str:
    return read(name)


def about_wording() -> dict:
    """About: the kit's About link list (ui-kit AfAbout, bundled in
    console_islands.py) — label, and which identity field is its tooltip
    (`title`) — the version rows' labels, and the top bar's dialog label."""
    isl = read_b("console_islands.py")
    m = re.search(r"function \w+\(e\)\{let t=\w+\(\);return\[(\{id:\"website\".*?)\]\}", isl)
    if not m:
        fail("the kit's About link list (`{id:\"website\",…}`) is missing in console_islands.py.")
    links = []
    for e in re.finditer(r'\{id:"(\w+)",label:"([^"]+)",href:[^,]+,title:(?:e|t)\.(\w+)\}', m.group(1)):
        links.append({"id": e.group(1), "label": e.group(2), "title": e.group(3)})
    if [l["id"] for l in links] != ["website", "source", "docs", "issues", "feedback", "contact"]:
        fail(f"the kit's About links changed: {links!r}")
    rows = re.search(r'return\[\["(AbstractFramework)",.*?\],\["(AbstractGateway)",', isl)
    if not rows:
        fail("the kit's About version rows (AbstractFramework / AbstractGateway) are missing.")
    ui = read_b("console_ui.py")
    label = re.search(r'label: "(About AbstractGateway)"', ui)
    if not label:
        fail("the top bar's About label is missing in console_ui.py.")
    return {
        "links": links,
        "version_rows": [rows.group(1), rows.group(2)],
        "dialog": label.group(1),
    }


def req(src: str, pattern: str, what: str, group: int = 1) -> str:
    """Exactly one match of `pattern` (re.S) in `src`, or FAIL naming `what`."""
    m = re.findall(pattern, src, re.S)
    if len(m) != 1:
        fail(f"{what}: expected exactly one match, found {len(m)} (the anchor moved).")
    v = m[0]
    return v if isinstance(v, str) else v[group - 1]


def network_wording() -> dict:
    """Network (console_ui.py netViewMarkup / netAddressRow /
    netOtherAddressMarkup / netProxyMarkup): mode sentences, address labels
    and pills, button labels, aria labels (the TUI's tooltips), headings
    and sentences."""
    ui = read_b("console_ui.py")
    block = req(ui, r"const NET_MODE_TEXT = \{(.*?)\n    \};", "NET_MODE_TEXT")
    modes = dict(re.findall(r'(\w+): "([^"]*)"', block))
    if set(modes) != {"localhost", "lan", "internet"}:
        fail(f"NET_MODE_TEXT keys changed: {sorted(modes)}")
    kinds = dict(re.findall(r'(\w+): "([^"]*)"', req(ui, r"const NET_KIND_LABEL = \{(.*?)\};", "NET_KIND_LABEL")))
    region = req(ui, r"(function netAddressRow\(a, primary\).*?function netProxySave)", "the Network page markup")
    pills = {
        "works": req(region, r'uiPill\("(Works now)", "ok"\)', "Works now pill"),
        "not_in_mode": req(region, r'uiPill\("(Not in this mode)", "muted"\)', "Not in this mode pill"),
        "public": req(region, r'a\.kind === "public" \? "(Through your proxy only)"', "proxy-only pill"),
        "unknown": req(region, r'"Through your proxy only" : "(Unknown)"', "Unknown pill"),
        "primary": req(region, r'uiPill\("(Primary)", "info"\)', "Primary pill"),
    }
    buttons = {
        "copy": req(region, r'data-net-copy="\$\{esc\(url\)\}" aria-label="Copy \$\{esc\(url\)\}">(Copy)</button>', "Copy button"),
        "lookup": req(region, r'"Looking up\.\.\." : "(Look up my public address)"', "Look up button"),
        "check": req(region, r'tools\.push\(`<button[^`]*\$\{netStore\.loading \? "Checking\.\.\." : "(Check again)"\}', "Check again button"),
        "restart": req(region, r'"Restarting\.\.\." : "(Restart now)"', "Restart now button"),
        "add_origin": req(region, r'"Saving\.\.\." : "(Add origin)"', "Add origin button"),
        "openai": req(region, r'data-net-action="goto-openai">(OpenAI API)</button>', "OpenAI API button"),
        "internet_go": req(region, r'"Saving\.\.\." : "(I understand, use Internet mode)"', "Internet confirm button"),
        "keep": req(region, r'data-net-action="ack-cancel">(Keep) \$\{esc\(conf\.label \|\| "the current mode"\)\}', "Keep button") + " {mode}",
        "keep_default": req(region, r'ack-cancel">Keep \$\{esc\(conf\.label \|\| "(the current mode)"\)\}', "Keep default"),
    }
    aria = {
        "copy": req(region, r'aria-label="(Copy) \$\{esc\(url\)\}"', "Copy aria-label") + " {url}",
        "remove": req(region, r'aria-label="(Remove) \$\{esc\(x\)\}"', "Remove aria-label") + " {origin}",
    }
    text = {
        "who": req(region, r"<h3>(Who can reach this gateway)</h3>", "mode heading"),
        "running": req(region, r'<span class="ui-sub">(Running now:) <b>', "Running now"),
        "addresses": req(region, r"<h3>(Addresses)</h3>", "Addresses heading"),
        "addresses_sub": req(region, r'<h3>Addresses</h3><span class="ui-sub">([^<]*)</span>', "Addresses sub"),
        "other": req(region, r'<h3 id="net-other-h">([^<]*)</h3>', "other-address heading"),
        "other_sub": req(region, r'net-other-h">[^<]*</h3><span class="ui-sub">([^<]*)</span>', "other-address sub"),
        "origins": req(region, r'<h4 id="net-origins-h">([^<]*)</h4>', "Allowed origins heading"),
        "client": req(region, r'<h4 id="net-trust-h">([^<]*)</h4>', "Client address heading"),
        "trust": req(region, r'"Saving\.\.\." : "(Trust proxies on other machines)"', "trust label"),
        "trust_text": req(region, r'id="net-trust-text">([^<]*)</p>', "trust sentence"),
        "trust_danger": req(region, r'id="net-trust-danger">([^<]*)</p>', "trust danger sentence"),
        "empty_origin": req(ui, r'p\.error = "(Type an origin, for example https://gateway\.example\.com\.)"', "empty origin sentence"),
        "applies": req(region, r"<span>(Changes apply to the next request: no restart\.)</span>", "applies sentence"),
        "non_admin_proxy": req(region, r"<span>(Only an admin can change these\.)</span>", "non-admin proxy sentence"),
        "non_admin_mode": req(region, r'<p class="ui-card__note">(Only an admin can change who can reach this gateway\.)</p>', "non-admin mode sentence"),
        "confirm": req(region, r"<p><strong>(Before you open the gateway to the internet)</strong></p>", "Internet confirm heading"),
        "needs_accounts": req(region, r'<span class="ui-seg__lock">(Needs accounts)</span>', "lock tag"),
        "no_address": req(region, r'<div class="ui-empty">(The gateway found no address to show\.)</div>', "no address"),
    }
    if "Advanced" in req(ui, r"(function netProxyMarkup\(d\).*?\n    \})\n", "netProxyMarkup"):
        fail("netProxyMarkup still names an 'Advanced' section (R15 D1 says none).")
    return {"modes": modes, "kinds": kinds, "pills": pills, "buttons": buttons, "aria": aria, "text": text}


def docs_wording() -> dict:
    """Docs assistant: the console's props (console.py docsAssistantProps:
    placeholder, suggestions) and the kit's DocsAssistantDrawer words
    (console_islands.py: title, head buttons + tips, composer, footer,
    history, archive, stop). `{name}` = the source name (AbstractGateway),
    `{title}` = a past conversation's title."""
    con = read_b("console.py")
    props = req(con, r"function docsAssistantProps\(\) \{(.*?)\n\s*\}\n", "docsAssistantProps")
    placeholder = req(props, r'placeholder: "([^"]*)"', "the docs placeholder")
    sugg = re.findall(r'"([^"]+)"', req(props, r"suggestions: \[([^\]]*)\]", "the docs suggestions"))
    if len(sugg) != 3:
        fail(f"expected 3 docs suggestions, found {sugg!r}")
    name = req(con, r'const DOCS_ASSISTANT_SOURCE = \{ app: "gateway", name: "([^"]*)" \};', "DOCS_ASSISTANT_SOURCE")
    isl = read_b("console_islands.py")
    t = {
        "title": req(isl, r'label:"(Docs assistant)",title:', "drawer title"),
        "history": req(isl, r'"aria-label":"(Past conversations)","data-af-tip":"Past conversations"', "Past conversations button"),
        "history_tip": req(isl, r'"aria-label":"Past conversations","data-af-tip":"([^"]*)"', "Past conversations tip"),
        "new": req(isl, r'"aria-label":"(New conversation)","data-af-tip"', "New conversation button"),
        "new_tip": req(isl, r'"aria-label":"New conversation","data-af-tip":"([^"]*)"', "New conversation tip"),
        "send": req(isl, r'sendLabel:"([^"]*)",busyLabel:"Answering…"', "Send"),
        "busy": req(isl, r'sendLabel:"Send",busyLabel:"([^"]*)"', "busy label"),
        "stop": req(isl, r'"aria-label":"(Stop)","data-af-tip":"Stop the answer"', "Stop"),
        "stop_tip": req(isl, r'"aria-label":"Stop","data-af-tip":"([^"]*)"', "Stop tip"),
        "stopped": req(isl, r'content:[A-Za-z_$][\w$]*\?"([^"]*)":String', "Stopped sentence"),
        "empty": req(isl, r'children:\["(Ask anything about) ",e\.source\.name,"\."\]', "empty state") + " {name}.",
        "footer": req(isl, r'children:\["(Grounded on) ",e\.source\.name,"’s documentation \(llms\.txt\) · docs-qa"\]', "footer")
        + " {name}’s documentation (llms.txt) · docs-qa",
        # (console_islands.py holds the bundle in a Python literal: \' is a ').
        "footer_tip": req(isl, r"title:`(Answers come from \$\{e\.source\.name\}\\'s documentation \(llms\.txt\) through the gateway\\'s docs-qa workflow\.)`", "footer tip").replace("${e.source.name}", "{name}").replace("\\'", "'"),
        "history_loading": req(isl, r'role:"status",children:"(Loading past conversations…)"', "history loading"),
        "history_error": req(isl, r'e\.error\|\|"(The history could not be read\.)"', "history error"),
        "history_empty": req(isl, r'children:"(No past conversations yet\.)"', "history empty"),
        "archive_question": req(isl, r'children:"(Archive this conversation\? It stays in the gateway; it leaves this list\.)"', "archive question"),
        "archive_go": req(isl, r'pc-docs-history__btn--danger",onClick:\(\)=>\{n\(null\),e\.onArchive\(r\)\},children:"([^"]*)"', "archive button"),
        "archive_keep": req(isl, r'className:"pc-docs-history__btn",onClick:\(\)=>n\(null\),children:"([^"]*)"', "archive cancel"),
        "archive_tip": req(isl, r'"data-af-tip":`(Archive "\$\{r\.title\}" \(kept, hidden\))`', "archive tip").replace("${r.title}", "{title}"),
        "untitled": req(isl, r'title:p\|\|"([^"]*)"', "untitled"),
        "questions": req(isl, r'r\.runIds\.length>1\?` · \$\{r\.runIds\.length\} (questions)`', "questions count"),
    }
    return {"name": name, "placeholder": placeholder, "suggestions": sugg, "kit": t}


def models_wording() -> dict:
    """Models: the web Models page's words (console_catalog.py) — the row
    actions and their refusals/tooltips, the delete confirmation, the
    head/bar/filter controls, the empty state — and the download cancel
    question (console_ui.py dlCancelMarkup)."""
    cat = read_b("console_catalog.py")
    ui = read_b("console_ui.py")

    def need(src: str, pattern: str, what: str, group: int = 1) -> str:
        m = re.search(pattern, src, re.S)
        if not m:
            fail(f"Models: {what} not found (the anchor moved).")
        return m.group(group)

    out = {
        "delete_tip": need(cat, r'const tip = admin \? `(Delete \$\{a\.artifact \|\| "this model"\} from this computer \(files only\))`', "the delete tooltip")
        .replace('${a.artifact || "this model"}', "{n}"),
        "delete_refused": need(cat, r'"(Only an admin can delete downloaded models)"', "the delete refusal"),
        "delete_checking": need(cat, r'aria-label="Checking" data-af-tip="([^"]+)"', "the checking tooltip"),
        "delete_deleting": need(cat, r'aria-label="Deleting" data-af-tip="([^"]+)"', "the deleting tooltip"),
        "use_default": need(cat, r'data-mc-action="default" \$\{attrs\}\$\{admin \? "" : \' disabled title="[^"]+"\'\}>([^<]+)</button>', "Use as default"),
        "use_default_refused": need(cat, r"title=\"(Only an admin can change the default model)\"", "the default refusal"),
        "default_pill": need(cat, r'uiPill\("(Default text model)", "info"\)', "the default pill"),
        "download": need(cat, r'job\.status === "failed" \? "(Try again)" : "(Download)"', "Download", 2),
        "try_again": need(cat, r'job\.status === "failed" \? "(Try again)" : "(Download)"', "Try again", 1),
        "download_refused": need(cat, r"title=\"(Only an admin can download models)\"", "the download refusal"),
        "unavailable": need(cat, r'title="\$\{esc\(why\)\}">(Not available here)</span>', "Not available here"),
        "unavailable_engine": need(cat, r'a\.supported_on_host === false \? "([^"]+)" : "([^"]+)"', "why (engine)", 1),
        "unavailable_build": need(cat, r'a\.supported_on_host === false \? "([^"]+)" : "([^"]+)"', "why (build)", 2),
        "starting": need(cat, r'aria-busy="true">(Starting\.\.\.)</button>', "Starting..."),
        "cancel": need(cat, r'dlCancelMarkup\(jid, "(Cancel)"', "Cancel"),
        "confirm_tail": need(cat, r'\$\{esc\(size\)\} (Files only — nothing in your runs is touched\.)', "the confirm sentence"),
        "confirm_no_size": need(cat, r'"(Deletes this model\'s files from this computer\.)"', "the confirm sentence without a size"),
        "confirm_keep": need(cat, r'data-mc-action="delete-keep" \$\{attrs\}>(\w+)</button>', "Keep"),
        "confirm_delete": need(cat, r'data-mc-action="delete-confirm" \$\{attrs\}>(\w+)</button>', "Delete"),
        "cancel_question": need(ui, r'<span class="ui-dl-confirm__q">([^<]+)</span>', "the cancel question"),
        "cancel_keep": need(ui, r'data-dl-step="keep"\$\{attrs\}>([^<]+)</button>', "Keep downloading"),
        "cancel_confirm": need(ui, r'data-dl-step="confirm"\$\{attrs\}>([^<]+)</button>', "Stop download"),
        "cancelling": need(ui, r'ui-dl-cancel" data-dl-cancel="\$\{id\}"\$\{attrs\} disabled>([^<]+)</button>', "Cancelling..."),
        "check_again": need(cat, r'mcStore\.loading \? "Checking\.\.\." : "(Check again)"', "Check again"),
        "checking": need(cat, r'mcStore\.loading \? "(Checking\.\.\.)" : "Check again"', "Checking..."),
        "modes": [
            need(cat, r'data-mc-mode="catalog" aria-pressed="\$\{[^}]*\}">(\w+)</button>', "Catalog"),
            need(cat, r'data-mc-mode="hf" aria-pressed="\$\{[^}]*\}">([\w ]+)</button>', "Hugging Face"),
        ],
        "hf_search": need(cat, r'data-mc-action="hf-search"[^>]*>(\w+)</button>', "Search"),
        "fits": need(cat, r'data-mc-fits="1"[^>]*><span>([^<]+)</span>', "Fits this computer"),
        "placeholder_catalog": need(cat, r'mcHfMode\(f\) \? "([^"]+)" : "([^"]+)"; \}', "placeholder", 2),
        "placeholder_hf": need(cat, r'mcHfMode\(f\) \? "([^"]+)" : "([^"]+)"; \}', "placeholder (hf)", 1),
        "groups": [need(cat, rf'group\("({g})", ', g) for g in ("Quantization", "Provider", "Capability", "Status")],
        "quant_chips": [l for _, l in re.findall(r'\["(\w+)", "([^"]+)"\]', need(cat, r"const MC_QUANT_CHIPS = \[(.*?)\];", "MC_QUANT_CHIPS"))],
        "status_chips": [l for _, l in re.findall(r'\["(\w+)", "([^"]+)"\]', need(cat, r"const MC_STATUS_CHIPS = \[(.*?)\];", "MC_STATUS_CHIPS"))],
        "empty": need(cat, r'data-mc-empty="1"><strong>([^<]+)</strong>', "the empty sentence"),
        "empty_hint": need(cat, r'mcRows\(\)\.length \? "([^"]+)" : "This gateway', "the empty hint"),
        "clear": need(cat, r'data-mc-action="clear">([^<]+)</button>', "Clear filters"),
        "not_in_catalog": need(cat, r'<h4 class="mc-extra__title">([^<]+)</h4>', "Not in the catalog"),
    }
    return out


def sandbox_wording() -> dict:
    """Sandbox: the web tab's field labels and help sentences (console.py
    `#tab-sandbox`), the Reasoning / MTP options, the mode buttons' tooltip
    shape (`renderSandboxCapabilityOptions`), and the kit chat's Send label,
    Attach and Clear tooltips (console.py sandboxChatProps + the islands
    bundle)."""
    src = read_b("console.py")
    start = src.find('<div id="tab-sandbox" class="tab-panel">')
    end = src.find("</section>", start)
    if start < 0 or end < 0:
        fail("the Sandbox tab markup (`#tab-sandbox` … `</section>`) is missing in console.py.")
    tab = src[start:end]
    def one_in(pattern: str, what: str, text: str = tab) -> str:
        m = re.findall(pattern, text, re.S)
        if len(m) != 1:
            fail(f"Sandbox {what}: expected exactly one match, found {len(m)} (the anchor moved).")
        return html.unescape(m[0])
    out = {
        "output_label": one_in(r'id="sandbox-modes-label"[^>]*>([^<]+)<', "Output label"),
        "context": one_in(r'id="sandbox-context"[^>]*>([^<]+)<', "context sentence"),
        "system_label": one_in(r'id="sandbox-system-label"[^>]*>([^<]+)<input', "System prompt label"),
        "system_placeholder": one_in(r'id="sandbox-system" placeholder="([^"]*)"', "System prompt placeholder"),
        "system_help": one_in(r'id="sandbox-system"[^>]*><span class="sandbox-field-help">([^<]+)<', "System prompt help"),
        "reasoning_label": one_in(r'id="sandbox-reasoning-label"[^>]*>([^<]+)<select', "Reasoning label"),
        "reasoning_help": one_in(r'</select><span class="sandbox-field-help">(Effort[^<]+)<', "Reasoning help"),
        "mtp_label": one_in(r'id="sandbox-speculation-label"[^>]*>([^<]+)<select', "MTP label"),
        "mtp_help": one_in(r'</select><span class="sandbox-field-help">(Per-request[^<]+)<', "MTP help"),
    }
    reasoning = re.search(r'<select id="sandbox-reasoning">(.*?)</select>', tab, re.S)
    mtp = re.search(r'<select id="sandbox-speculation">(.*?)</select>', tab, re.S)
    if not reasoning or not mtp:
        fail("the Sandbox Reasoning / MTP <select> options are missing in console.py.")
    out["reasoning_options"] = re.findall(r"<option[^>]*>([^<]*)</option>", reasoning.group(1))
    out["mtp_options"] = re.findall(r"<option[^>]*>([^<]*)</option>", mtp.group(1))
    tip = one_in(r'btn\.title = `\$\{label\}: \$\{configured \? `[^`]*` : "([^"]+)"\}`;', "mode tooltip", src)
    out["mode_unconfigured"] = tip
    out["send_label"] = one_in(r'sendLabel: "([^"]+)"', "Send label", src)
    out["seconds_label"] = one_in(r'id="sandbox-seconds-label"[^>]*>([^<]+)<input', "Length label")
    out["seconds_help"] = one_in(r'id="sandbox-seconds"[^>]*><span class="sandbox-field-help">([^<]+)<', "Length help")
    out["seconds_refusal"] = one_in(r'throw new Error\("(Length must be[^"]+)"\)', "Length refusal", src)
    isl = read_b("console_islands.py")
    out["clear_tip"] = one_in(r'af-sandbox-chat__clear","aria-label":"Clear chat","data-af-tip":"([^"]+)"', "Clear tooltip", isl)
    out["attach_tip"] = one_in(r'"data-af-tip":"(Attach files to your question)"', "Attach tooltip", isl)
    return out


def entity_wording() -> dict:
    """Manage entity + Summon a new entity (console.py): the Manage modal's
    tabs, each card's title and description (switch cards: the switch's
    label and description; disclosures: their summary and help), the
    buttons the terminal mirrors, the freeze confirmation; the Summon
    modal's title, lead, "Optional configuration" (R15 D1) and its button."""
    src = read_b("console.py")
    start = src.find('<div id="entity-manage-section"')
    end = src.find('<div id="entity-create-backdrop"', start)
    if start < 0 or end < 0:
        fail("the Manage entity modal (`#entity-manage-section`) is missing in console.py.")
    m = src[start:end]
    def one_in(pattern: str, what: str, text: str = m) -> str:
        found = re.findall(pattern, text, re.S)
        if len(found) != 1:
            fail(f"Entity {what}: expected exactly one match, found {len(found)} (the anchor moved).")
        return html.unescape(found[0])
    tabs = [html.unescape(t) for t in re.findall(r'id="entity-subtab-\w+"[^>]*>([^<]+)</button>', m)]
    if len(tabs) != 6:
        fail(f"the Manage tabs changed: {tabs!r}")
    cards = {}
    for title, desc in re.findall(r'class="af-card__title">([^<]+)</h3>\s*<p class="af-card__desc">([^<]+)</p>', m):
        cards[html.unescape(title)] = html.unescape(desc)
    cards["Identity"] = one_in(r'<p class="af-form__help">(Verify memory checks[^<]+)</p>', "Identity help")
    cards["Awake or asleep"] = one_in(r'id="entity-state-awake-desc">([^<]+)<', "Awake description")
    pt_label = one_in(r'<span class="af-switch__label">(Personal time)</span>', "Personal time label")
    cards[pt_label] = one_in(r'id="entity-owntime-toggle-desc">([^<]+)<', "Personal time description")
    cand = one_in(r'<summary>(Memories from sleep waiting for your review) \(', "candidates summary")
    cards[cand] = one_in(r'<p class="af-form__help">(Sleep proposes[^<]+)</p>', "candidates help")
    dz = one_in(r'<summary>(Danger zone: rebuild its memory index)</summary>', "danger zone summary")
    cards[dz] = one_in(r'<p class="af-form__help">(Only when the status below says MISMATCH[^<]+)</p>', "danger zone help")
    for must in ("Right now", "Visit", "Emergency freeze", "Mind", "Voice", "Work order", "Tools per phase", "Instructions"):
        if must not in cards:
            fail(f"the Manage card {must!r} is missing (the anchors moved).")
    buttons = {
        "verify": one_in(r'id="entity-verify"[^>]*>([^<]+)<', "Verify memory button"),
        "talk": one_in(r'id="entity-chat-open"[^>]*>([^<]+)<', "Open visit button"),
        "freeze": one_in(r'id="entity-loop-freeze"[^>]*>([^<]+)<', "Freeze now button"),
        "reembed": one_in(r'id="entity-reembed"[^>]*>([^<]+)<', "Rebuild index button"),
    }
    freeze_q = one_in(r'aria-label="Confirm freeze"[^>]*>\s*<span>([^<]+)</span>', "freeze confirmation")
    sub = {
        "sleep_question": one_in(r'aria-label="Confirm sleep"[^>]*>\s*<span>([^<]+)</span>', "sleep confirmation"),
        "reembed_question": one_in(r'aria-label="Confirm rebuild"[^>]*>\s*<span>([^<]+)</span>', "rebuild confirmation"),
        "reason_help": one_in(r'id="entity-state-reason-help" class="af-form__help">([^<]+)<', "Reason help"),
        "empty_phase_label": one_in(r'<span class="af-switch__label">(Empty phase means no tools)</span>', "Empty phase label"),
        "empty_phase_desc": one_in(r'id="entity-tools-denyall-desc">([^<]+)<', "Empty phase description"),
        "schedule_help": one_in(r'<summary id="entity-schedule-summary">[^<]*</summary>\s*<p class="af-form__help">([^<]+)</p>', "Schedule help"),
        "audition_label": one_in(r'id="entity-voice-audition"[^>]*>([^<]+)<', "Hear a sample button"),
        "give_task_label": one_in(r'id="entity-workorder-save"[^>]*>([^<]+)<', "Give this task button"),
        "end_task_label": one_in(r'id="entity-workorder-clear"[^>]*>([^<]+)<', "End the work order button"),
        "close_visit_label": one_in(r'id="entity-chat-close"[^>]*>([^<]+)<', "Close visit button"),
    }
    c0 = src.find('<div id="entity-create-backdrop"')
    c1 = src.find('<div id="templates-backdrop"', c0)
    c = src[c0:c1]
    create = {
        "title": one_in(r'<h2 id="entity-create-title">([^<]+)</h2>', "Summon title", c),
        "lead": one_in(r'</h2>\s*<p class="section-note">([^<]+)</p>', "Summon lead", c),
        "admin_note": one_in(r'id="entity-create-admin-note"[^>]*>([^<]+)</p>', "admin note", c),
        "optional_title": one_in(r'id="entity-optional-title"[^>]*>([^<]+)</h3>', "Optional configuration title", c),
        "optional_lead": one_in(r'id="entity-optional-title"[^>]*>[^<]+</h3>\s*<p class="section-note">([^<]+)</p>', "Optional configuration lead", c),
        "create_label": one_in(r'id="entity-create"[^>]*>.*?<span>([^<]+)</span></button>', "Validate & create label", c),
        "create_tip": one_in(r'id="entity-create" title="([^"]+)"', "Validate & create tooltip", c),
        "cancel_label": one_in(r'id="entity-create-cancel"[^>]*>([^<]+)</button>', "Cancel label", c),
    }
    t0 = src.find('<div id="templates-backdrop"')
    t1 = src.find('<div id="af-docs-assistant-root">', t0)
    tpl_src = src[t0:t1]
    def btn(i: str) -> dict:
        m = re.search(r'<button id="' + i + r'"([^>]*)>(?:<span[^>]*>[^<]*</span><span>)?([^<]+)<', tpl_src)
        if not m:
            fail(f"Spark templates button #{i} is missing in console.py.")
        tip = re.search(r'title="([^"]*)"', m.group(1))
        return {"label": html.unescape(m.group(2)), "tip": html.unescape(tip.group(1)) if tip else ""}
    templates = {
        "title": one_in(r'<h2 id="templates-title">([^<]+)</h2>', "templates title", tpl_src),
        "lead": one_in(r'</h2>\s*<p class="section-note">([^<]+)</p>', "templates lead", tpl_src),
        "picker_tip": one_in(r'<select id="tpl-select" title="([^"]+)"', "template picker tooltip", tpl_src),
        "view": btn("tpl-view"),
        "edit": btn("tpl-edit"),
        "new": btn("tpl-new"),
        "save": btn("tpl-save"),
        "cancel": btn("tpl-cancel"),
        "close": btn("templates-close"),
        "spark_label": one_in(r'<label>(Spark \(JSON[^<]+)<textarea', "spark label", tpl_src),
    }
    return {"tabs": tabs, "cards": cards, "buttons": buttons, "freeze_question": freeze_q, "create": create, "sub": sub, "templates": templates}



def _one(src: str, pattern: str, what: str, flags: int = 0) -> str:
    m = re.findall(pattern, src, flags)
    if len(m) != 1:
        fail(f"{what}: expected exactly one match, found {len(m)} (the anchor moved).")
    return m[0]


def multimodal_wording() -> dict:
    """Multimodal: the page head, the scope sentences, the table's columns,
    the row actions' words and tooltip templates (R14-W7 icon buttons:
    `{label} {key}`, `Clear {key}`, `Download {artifact} with {provider}`,
    `Copy: {instruction}`), the Weights pill vocabulary, the status
    vocabulary, the banner, and the "Configure capability default" modal."""
    src = read_b("console.py")
    head = re.search(r'defaults: \["(Multimodal Capabilities)", "([^"]+)"\]', src)
    if not head:
        fail("the Multimodal heading (`defaults: [\"Multimodal Capabilities\", …]`) is missing.")
    scope = re.search(r'\$\("defaults-scope"\)\.textContent = p\.admin\s*\? "([^"]+)"\s*: "([^"]+)";', src)
    if not scope:
        fail("the Multimodal scope sentences (defaults-scope) are missing.")
    cols = _one(src, r'<table class="capability-table">\s*<thead><tr>((?:<th>[^<]+</th>)+)</tr></thead>', "the capability table head")
    labels = re.search(
        r'function defaultRowActionLabel\(row\) \{\s*if \(row\?\.covered_by === "input\.text"\) return row\?\.overrideable \? "(Override)" : "(Covered by input\.text)";\s*'
        r'if \(row\?\.derived_from === "input\.text"\) return "(Derived \\u2190 input\.text)";\s*'
        r'if \(defaultRowConfigured\(row\)\) return "(Edit)";.*?return defaultRowIsTaskParent\(row\) \? "(Set for all)" : "(Configure)";',
        src, re.S)
    if not labels:
        fail("defaultRowActionLabel's words moved.")
    weights = dict(re.findall(r'(\w+): \{ label: "([^"]+)", cls: "\w+" \}', _one(src, r"const WEIGHT_LABELS = \{(.*?)\};", "WEIGHT_LABELS", re.S)))
    unknown = re.search(r'if \(a\.status === "unknown"\) return a\.downloadable \? \{ label: "([^"]+)".*?: \{ label: "([^"]+)"', src)
    if not unknown or len(weights) != 4:
        fail("the Weights pill vocabulary (WEIGHT_LABELS / weightView) moved.")
    status = []
    body = _one(src, r"function defaultRowStatus\(row\) \{(.*?)\n\s*\}\n", "defaultRowStatus", re.S)
    for lab in re.findall(r'label: (?:rowHasProviderModel\(row\) \? )?"([^"]+)"', body):
        status.append(lab.replace("\\u2190", "←"))
    inh = re.search(r"label: `(inherited ← )\$\{defaultRowParentKey\(row\)\}`", body)
    if not inh:
        fail("defaultRowStatus' inherited label moved.")
    status.append(inh.group(1) + "{parent}")
    if "configured" not in status or "not configured" not in status:
        fail(f"defaultRowStatus' labels moved: {status!r}")
    def tip(pattern: str, what: str) -> str:
        return _one(src, pattern, what).replace("${defaultRowActionLabel(row)}", "{label}").replace("${key}", "{key}") \
            .replace("${artifact}", "{artifact}").replace("${row.provider}", "{provider}").replace("${availability.instruction}", "{instruction}")
    modal = _one(src, r'(<div id="default-modal-backdrop".*?)<div id="log-modal-backdrop"', "the Configure capability default modal", re.S)
    mtitle = _one(modal, r'<h2 id="default-modal-title">([^<]+)</h2>', "modal title")
    mdesc = _one(modal, r'<p id="default-modal-description">([^<]+)</p>', "modal description")
    buttons = []
    for bid in ("close-default-modal", "clear-default", "test-default", "save-default"):
        m = re.search(rf'<button id="{bid}"([^>]*)>(?:<span class="button-icon"[^>]*>[^<]*</span>)?(?:<span>)?([^<]+)', modal)
        if not m:
            fail(f"modal button {bid} moved.")
        t = re.search(r'title="([^"]*)"', m.group(1))
        buttons.append({"label": m.group(2).strip(), "title": html.unescape(t.group(1)) if t else ""})
    sa = req(src, r"(function renderStoreAuthority\(elementId, payload\) \{.*?el\.classList\.remove\(\"hidden\"\);\n\s*\})", "renderStoreAuthority")
    store_line = {
        "label": _one(sa, r'overlay \? "[^"]+" : "([^"]+)";', "store label"),
        "overlay_label": _one(sa, r'overlay \? "([^"]+)" : "AbstractCore store";', "overlay label"),
        "claim_overlay": _one(sa, r'const claim = overlay\s*\? "([^"]+)"', "overlay claim"),
        "claim_writable": _one(sa, r': writable\s*\? "([^"]+)"', "writable claim"),
        "claim_readonly": _one(sa, r': writable\s*\? "[^"]+"\s*: "([^"]+)";', "read-only claim"),
        "shape": _one(sa, r"el\.innerHTML = `(\$\{esc\(label\)\} · <code>\$\{esc\(file\)\}</code> — \$\{esc\(claim\)\})`;", "line shape")
        .replace("${esc(label)}", "{label}").replace("<code>${esc(file)}</code>", "{file}").replace("${esc(claim)}", "{claim}"),
        "title": _one(sa, r"el\.title = `(authority: )\$\{authority\}`;", "line title") + "{authority}",
    }
    undiscovered = _one(src, r'\$\("default-modal-message"\)\.textContent = `(Configured model "\$\{selected\}" is not currently in the discovered \$\{catalog\.scope\} catalog for \$\{provider\}\.)`;', "the undiscovered-model sentence")
    return {
        "undiscovered_model": undiscovered.replace("${selected}", "{model}").replace("${catalog.scope}", "{scope}").replace("${provider}", "{provider}"),
        "store_line": store_line,
        "title": head.group(1),
        "subtitle": head.group(2),
        "scope_admin": scope.group(1),
        "scope_user": scope.group(2),
        "apply": {"label": _one(src, r'id="defaults-apply-recommended"[^>]*>.*?<span>([^<]+)</span></button>', "apply label"),
                  "title": _one(src, r'id="defaults-apply-recommended" class="secondary" title="([^"]+)"', "apply title")},
        "refresh": {"label": _one(src, r'id="refresh-catalog"[^>]*>.*?<span>([^<]+)</span></button>', "refresh label"),
                    "title": _one(src, r'id="refresh-catalog" class="secondary" title="([^"]+)"', "refresh title")},
        "columns": re.findall(r"<th>([^<]+)</th>", cols),
        "empty": _one(src, r'class="empty">(No capability routes were returned by Gateway\.)</td>', "the empty sentence"),
        "action_labels": [x.replace("\\u2190", "←") for x in labels.groups()],
        "tips": {
            "configure": tip(r"configure\.dataset\.afTip = `([^`]+)`;", "configure tip"),
            "clear": tip(r"clear\.dataset\.afTip = `([^`]+)`;", "clear tip"),
            "download": tip(r"download\.dataset\.afTip = `([^`]+)`;", "download tip"),
            "copy": tip(r"copy\.dataset\.afTip = `([^`]+)`;", "copy tip"),
        },
        "weights": {**weights, "unknown_downloadable": unknown.group(1), "unknown_not_downloadable": unknown.group(2)},
        "weight_tip_fix": _one(src, r"if \(a\.instruction\) lines\.push\(`(To fix: )\$\{a\.instruction\}`\);", "weightTip To fix"),
        "status": status,
        "banner": {
            "one": _one(src, r'gaps\.length === 1 \? "(One route has)"', "banner one"),
            "many": _one(src, r': `\$\{gaps\.length\} (routes have)`\}', "banner many"),
            "tail": _one(src, r'\} (no model yet) \(\$\{routes\}\)\. `', "banner tail"),
            "recommended": _one(src, r'\+ `(Recommended to get started: )\$\{pairs\}\. `', "banner recommended"),
            "button": _one(src, r'aria-hidden="true">⭳</span><span>(Download missing)</span>', "Download missing"),
        },
        "dialog": {"title": mtitle, "lead": mdesc, "buttons": buttons,
                   "base_url_placeholder": _one(modal, r'id="modal-default-base-url"[^>]*placeholder="([^"]+)"', "base URL placeholder"),
                   "labels": ["Provider", "Model", "Voice", "Reasoning"] if all(f"<label>{x}<select" in modal or f'>{x}<select' in modal for x in ("Provider", "Model", "Voice", "Reasoning")) else fail("modal labels moved")},
    }



def resources_wording() -> dict:
    """Resources: the ◎ Gateway card (title, note, the Workflows paused
    switch, Check now / Update, Restart gateway… / Quit gateway…, the
    restart / quit confirmations), the ▦ Memory & GPU / ▣ Models / ⌸
    Session caches headings, the Models table's columns and row buttons
    (labels + tooltips), "Show configured / cached", the load form, the
    caches' Clear, the confirmations and the empty sentences."""
    src = read_b("console.py")
    sec = _one(src, r'(<div id="tab-models".*?)<!-- Models \(id catalog\)', "the Resources tab markup", re.S)
    def btn(bid: str) -> dict:
        m = re.search(rf'<button id="{bid}"([^>]*)>([^<]+)</button>', sec)
        if not m:
            fail(f"Resources button {bid} moved.")
        t = re.search(r'title="([^"]*)"', m.group(1))
        return {"label": m.group(2).strip(), "title": html.unescape(t.group(1)) if t else ""}
    headings = re.findall(r'<h2 class="section-title"><span class="section-icon" aria-hidden="true">([^<]+)</span><span(?: id="[^"]+")?>([^<]+)</span></h2>', sec)
    pause_title = _one(sec, r'<button id="gateway-host-pause"[^>]*title="([^"]+)"', "the pause switch's title")
    pause_label = _one(sec, r'id="gateway-host-pause".*?af-switch__label">([^<]+)<', "the pause switch's label", re.S)
    show = re.search(r'<label id="models-show-cached-label"[^>]*title="([^"]+)"><input id="models-show-cached" type="checkbox"> <span id="models-show-cached-text">([^<]+)</span>', sec)
    lock = re.search(r'<label class="entity-checkbox" title="([^"]+)"><input id="models-load-lock" type="checkbox"> ([^<]+)</label>', sec)
    if not show or not lock:
        fail("the Show configured / cached toggle or the lock-in-memory checkbox moved.")
    tables = re.findall(r"<thead><tr>((?:<th>[^<]+</th>)+)</tr></thead>", sec)
    if len(tables) != 2:
        fail(f"the Models / Session caches tables moved ({len(tables)} heads).")
    def call(pattern: str, what: str) -> str:
        return _one(src, pattern, what, re.S)
    rows = {
        "estimate": {"label": call(r'est\.textContent = "([^"]+)";', "Estimate"), "title": call(r'est\.title = "([^"]+)";', "Estimate title")},
        "unlock_resident": call(r'\? \(row\.resident === true\s*\? "(Release the memory lock[^"]+)"', "Unlock title (resident)"),
        "unlock_evicted": call(r': "(Release a lock whose model is no longer in memory[^"]+)"\)', "Unlock title (evicted)"),
        "lock_adopt": call(r'\? "(Lock this model in memory — this host loaded it[^"]+)"', "Lock title (adopt)"),
        "lock": call(r': "(Lock this model in memory so nothing can evict it)"\);', "Lock title"),
        "lock_labels": list(re.search(r'lockBtn\.textContent = row\.locked === true \? "([^"]+)" : "([^"]+)";', src).groups()),
        "unload": {"label": call(r'unload\.textContent = "([^"]+)";', "Unload"), "title": call(r'unload\.title = "([^"]+)";', "Unload title")},
        "clear": {"label": call(r'clear\.textContent = "(Clear)";\s*clear\.title = "Clear every prompt cache', "cache Clear"),
                  "title": call(r'clear\.title = "(Clear every prompt cache for this session)";', "cache Clear title")},
    }
    confirms = {
        "restart": call(r'confirmAction\(\{ title: "(Restart AbstractGateway\?)", message: "([^"]+)"', "restart confirm"),
        "quit": call(r'confirmAction\(\{ title: "(Quit AbstractGateway\?)", message: "([^"]+)"', "quit confirm"),
        "unload": call(r'title: "Unload model",\s*message: `([^`]+)`', "unload confirm").replace("${name}", "{name}"),
        "force": call(r'title: "Model locked",\s*message: `([^`]+)`', "force unload confirm").replace("${name}", "{name}"),
        "clear_cache": call(r'title: "Clear session caches",\s*message: `([^`]+)`', "clear caches confirm").replace("${sessionId}", "{session}"),
    }
    return {
        "headings": [{"icon": i, "title": html.unescape(t)} for i, t in headings],
        "gateway_note": _one(sec, r'<span>Gateway</span></h2>\s*<p class="section-note">([^<]+)</p>', "the Gateway note"),
        "refresh_tip": _one(sec, r'id="models-refresh"[^>]*data-af-tip="([^"]+)"', "the Refresh tip"),
        "pause": {"label": pause_label.strip(), "title": pause_title},
        "check": btn("gateway-host-update-check"),
        "update": btn("gateway-host-update-start"),
        "restart": btn("gateway-host-restart"),
        "quit": btn("gateway-host-quit"),
        "kv": [html.unescape(k) for k in re.findall(r'<span class="entity-kv-key">([^<]+)</span>', sec)],
        "show_cached": {"label": show.group(2), "title": show.group(1)},
        "lock_in_memory": {"label": lock.group(2).strip(), "title": lock.group(1)},
        "load": btn("models-load-button"),
        "models_columns": re.findall(r"<th>([^<]+)</th>", tables[0]),
        "caches_columns": re.findall(r"<th>([^<]+)</th>", tables[1]),
        "rows": rows,
        "confirms": {"restart": " ".join(confirms["restart"]), "quit": " ".join(confirms["quit"]),
                     "unload": confirms["unload"], "force": confirms["force"], "clear_cache": confirms["clear_cache"]},
        "empty": {"models": call(r'modelsEmptyRow\(body, 8, "(No models loaded right now\.)"\)', "models empty"),
                  "caches": call(r'modelsEmptyRow\(body, 6, "(No session prompt caches right now\.)"\)', "caches empty")},
    }


B_SCREENS = {
    "about": about_wording,
    "network": network_wording,
    "docs": docs_wording,
    "models": models_wording,
    "sandbox": sandbox_wording,
    "entity": entity_wording,
    "multimodal": multimodal_wording,
    "resources": resources_wording,
}


def b_fixture(screen: str) -> Path:
    return CRATE / "tests" / "fixtures" / f"r15_web_wording_{screen}.json"


def check_b_screens(write: bool) -> int:
    rc = 0
    for screen, fn in B_SCREENS.items():
        text = json.dumps({"_source": "scripts/extract_web_wording.py (do not edit by hand)", **fn()},
                          indent=2, ensure_ascii=False) + "\n"
        path = b_fixture(screen)
        if write:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path}")
        elif not path.is_file():
            fail(f"{path} missing; run with --write.")
        elif path.read_text(encoding="utf-8") != text:
            print(f"{path} differs from the web sources; run with --write, then make the terminal match.")
            rc = 1
        else:
            print(f"r15 web wording ({screen}): fixture matches the web sources")
    return rc


_main_before_b = main


def main() -> int:  # noqa: F811 - chains the reference check, then group B's
    rc = _main_before_b()
    return check_b_screens("--write" in sys.argv[1:]) or rc

if __name__ == "__main__":
    _rc = main()
    raise SystemExit(screens_main("--write" in sys.argv) or _rc)
