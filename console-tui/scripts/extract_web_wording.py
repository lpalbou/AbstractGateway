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
        "show_key_tip": need_first(ui, r'data-af-tip="\$\{oaiStore\.reveal \? "Hide your API key" : "([^"]*)"\}"', "show key tooltip"),
        "hide_key_tip": need_first(ui, r'data-af-tip="\$\{oaiStore\.reveal \? "([^"]*)" : "Show your API key"\}"', "hide key tooltip"),
        "not_in_a_run": need_first(ui, r'disabled title="([^"]*)" data-oai-observer', "observer disabled tooltip"),
        "restart": need(ui, r'oaiStore\.busy === "restart" \? "Restarting\.\.\." : "([^"]*)"\}</button>', "restart label"),
        "check": need(ui, r'oaiStore\.busy === "check" \? "Checking\.\.\." : "([^"]*)"\}</button>', "check label"),
        "new_key": need_first(ui, r'data-oai-action="new-key"\$\{oaiStore\.busy \? " disabled" : ""\}>([^<]*)</button>', "new key label"),
        "copy_example": need(ui, r'data-oai-copy-snippet>([^<]*)</button>', "copy example"),
        "copy": need(ui, r'aria-label="Copy \$\{esc\(label\)\}">([^<]*)</button>', "copy button"),
        "doc_openai": need(ui, r'rel="noopener">(OpenAI API compatibility)</a>', "openai docs link"),
        "doc_core": need(ui, r'rel="noopener">(AbstractCore server)</a>', "abstractcore docs link"),
        "new_key_confirm": need(ui, r'<p>(Make a new key\?[^<]*)</p>', "new key confirmation"),
    }
    for i, (_, label) in enumerate(snips):
        out[f"snippet_{i}"] = label
    for i, (_, label, text) in enumerate(auth):
        out[f"auth_{i}_label"] = label
        out[f"auth_{i}_text"] = text
    return out


SCREEN_FIXTURES["openai"] = openai_words


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


if __name__ == "__main__":
    _rc = main()
    raise SystemExit(screens_main("--write" in sys.argv) or _rc)
